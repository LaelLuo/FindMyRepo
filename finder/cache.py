"""搜索结果本地缓存（sqlite3 标准库，零外部依赖）。

存到用户配置目录（LOCALAPPDATA），不写进项目仓库。
缓存不可用时降级为「不缓存、直接实时搜索」，不影响搜索本身。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

TTL_DAYS = 7
APP_DIR_NAME = "gh-repo-finder"
DB_FILE_NAME = "cache.sqlite3"


def cache_dir() -> Path:
    """缓存目录：优先 LOCALAPPDATA，其次 HOME/.cache。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".cache" / APP_DIR_NAME


def db_path() -> Path:
    return cache_dir() / DB_FILE_NAME


def normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def _cache_key(query: str, limit: int) -> str:
    """缓存键含 limit 桶：同一查询请求不同条数不应互相截断。"""
    bucket = "all" if limit >= MAX_HITS else str(limit)
    return f"{normalize_query(query)}||{bucket}"


MAX_HITS = 100


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_key TEXT NOT NULL UNIQUE,
            query_text TEXT NOT NULL,
            created_at REAL NOT NULL,
            refreshed_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hits (
            search_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            description TEXT,
            stars INTEGER,
            url TEXT,
            pushed_at TEXT,
            archived INTEGER,
            term_hint TEXT,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (search_id, full_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS term_hints (
            search_id INTEGER NOT NULL,
            term TEXT NOT NULL,
            source_query TEXT NOT NULL,
            PRIMARY KEY (search_id, term)
        )
    """)
    return conn


def _age_days(ts: float) -> float:
    return (time.time() - ts) / 86400


def get_cached(query: str, limit: int = MAX_HITS) -> dict[str, Any] | None:
    """取同查询缓存；未命中或缓存损坏返回 None。"""
    key = _cache_key(query, limit)
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                "SELECT id, query_text, created_at, refreshed_at FROM searches WHERE query_key=?",
                (key,),
            ).fetchone()
            if not row:
                return None
            search_id, query_text, created_at, refreshed_at = row
            hit_rows = conn.execute(
                """SELECT full_name, description, stars, url, pushed_at, archived, term_hint, fetched_at
                   FROM hits WHERE search_id=? ORDER BY rowid""",
                (search_id,),
            ).fetchall()
            hint_rows = conn.execute(
                "SELECT term, source_query FROM term_hints WHERE search_id=? ORDER BY rowid",
                (search_id,),
            ).fetchall()
        hits = [
            {
                "full_name": r[0], "description": r[1], "stars": r[2], "url": r[3],
                "pushed_at": r[4], "archived": bool(r[5]), "term_hint": r[6],
            }
            for r in hit_rows
        ]
        return {
            "query": query_text,
            "hits": hits,
            "term_hints": [{"term": r[0], "source_query": r[1]} for r in hint_rows],
            "age_days": round(_age_days(refreshed_at), 2),
            "stale": _age_days(refreshed_at) > TTL_DAYS,
        }
    except (sqlite3.Error, OSError):
        return None


def put_cached(
    query: str,
    items: list[dict[str, Any]],
    term_hints: list[str] | None = None,
    limit: int = MAX_HITS,
) -> None:
    """写入/刷新缓存；失败静默降级，不影响搜索。"""
    key = _cache_key(query, limit)
    now = time.time()
    try:
        with closing(_connect()) as conn:
            conn.execute(
                """INSERT INTO searches (query_key, query_text, created_at, refreshed_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(query_key) DO UPDATE SET
                     query_text=excluded.query_text, refreshed_at=excluded.refreshed_at""",
                (key, query.strip(), now, now),
            )
            search_id = conn.execute(
                "SELECT id FROM searches WHERE query_key=?", (key,)
            ).fetchone()[0]
            # 先删旧命中再写，保证 --refresh 后不残留已消失的结果。
            # 术语提示是长期积累的，只有显式传入 term_hints 才整体替换，否则保留。
            conn.execute("DELETE FROM hits WHERE search_id=?", (search_id,))
            if term_hints is not None:
                conn.execute("DELETE FROM term_hints WHERE search_id=?", (search_id,))
            for item in items:
                conn.execute(
                    """INSERT OR REPLACE INTO hits
                       (search_id, full_name, description, stars, url, pushed_at, archived, term_hint, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        search_id, item.get("full_name", ""), item.get("description"),
                        item.get("stars"), item.get("url"), item.get("pushed_at"),
                        int(bool(item.get("archived"))), item.get("term_hint"), now,
                    ),
                )
            for term in term_hints or []:
                conn.execute(
                    "INSERT OR IGNORE INTO term_hints (search_id, term, source_query) VALUES (?,?,?)",
                    (search_id, term, query.strip()),
                )
            conn.commit()
    except (sqlite3.Error, OSError):
        pass


def add_term_hints(query: str, terms: list[str]) -> None:
    """把迭代中发现的术语记录到该查询（若查询无缓存则先建空记录）。"""
    key = _cache_key(query, MAX_HITS)
    now = time.time()
    try:
        with closing(_connect()) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO searches (query_key, query_text, created_at, refreshed_at)
                   VALUES (?,?,?,?)""",
                (key, query.strip(), now, now),
            )
            search_id = conn.execute(
                "SELECT id FROM searches WHERE query_key=?", (key,)
            ).fetchone()[0]
            for term in terms:
                if term and term.strip():
                    conn.execute(
                        "INSERT OR IGNORE INTO term_hints (search_id, term, source_query) VALUES (?,?,?)",
                        (search_id, term.strip(), query.strip()),
                    )
            conn.commit()
    except (sqlite3.Error, OSError):
        pass


def related_terms(query: str) -> list[str]:
    """取与当前查询有词交集的历史术语，供 skill 迭代借鉴（只是提示，不是结果）。"""
    words = set(normalize_query(query).split())
    if not words:
        return []
    try:
        with closing(_connect()) as conn:
            rows = conn.execute("SELECT term, source_query FROM term_hints").fetchall()
    except (sqlite3.Error, OSError):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for term, source in rows:
        if term in seen:
            continue
        if set(normalize_query(term).split()) & words or set(normalize_query(source).split()) & words:
            seen.add(term)
            out.append(term)
    return out[:10]


def clear() -> int:
    """清空缓存，返回删除的查询数；失败返回 -1。"""
    try:
        with closing(_connect()) as conn:
            n = conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
            conn.execute("DELETE FROM hits")
            conn.execute("DELETE FROM term_hints")
            conn.execute("DELETE FROM searches")
            conn.commit()
        return n
    except (sqlite3.Error, OSError):
        return -1


def export_json() -> str:
    """把缓存内容导出为 JSON（便于查看/迁移）。"""
    try:
        with closing(_connect()) as conn:
            searches = conn.execute(
                "SELECT query_key, query_text, created_at, refreshed_at FROM searches"
            ).fetchall()
            data = []
            for key, text, created, refreshed in searches:
                hits = conn.execute(
                    """SELECT h.full_name, h.description, h.stars, h.url, h.pushed_at, h.archived, h.term_hint
                       FROM hits h JOIN searches s ON s.id=h.search_id WHERE s.query_key=?""",
                    (key,),
                ).fetchall()
                data.append({
                    "query": text,
                    "refreshed_at": refreshed,
                    "hits": [{"full_name": h[0], "description": h[1], "stars": h[2],
                              "url": h[3], "pushed_at": h[4], "archived": bool(h[5]),
                              "term_hint": h[6]} for h in hits],
                })
        return json.dumps(data, ensure_ascii=False, indent=2)
    except (sqlite3.Error, OSError):
        return "[]"
