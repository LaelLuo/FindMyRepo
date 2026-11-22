"""单轮 GitHub 仓库搜索；多轮迭代由 agent skill 驱动。

只依赖 Python 标准库与已登录的 gh CLI，不引入第三方包、模型或外部服务。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import Any

GH_TIMEOUT_SECONDS = 30
MAX_PER_PAGE = 100


def _run_gh(query: str, limit: int) -> list[dict[str, Any]]:
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("未找到 gh 命令行；请先安装 GitHub CLI。")

    # GitHub 仓库搜索的每页上限是 100；单次请求不做分页，迭代由 skill 负责。
    count = max(1, min(limit, MAX_PER_PAGE))
    cmd = [
        gh, "api", "-X", "GET", "search/repositories",
        "-f", f"q={query}",
        "-f", f"per_page={count}",
        "--jq", (".items[] | {full_name, description, stars: .stargazers_count, "
                 "url: .html_url, pushed_at, updated_at, archived}"),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh 搜索超时（{GH_TIMEOUT_SECONDS} 秒）；请检查网络后重试。")

    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or "未知错误"
        low = msg.lower()
        # 限流必须先判：GitHub 的未认证限流提示里也含 "Authenticated"，先判 auth 会误报。
        if "403" in msg or "rate limit" in low or "secondary rate" in low:
            raise RuntimeError("GitHub 搜索达到限流（认证用户约 30 次/分钟）；请稍后重试。")
        if "401" in msg or "bad credentials" in low or "authentication token" in low:
            raise RuntimeError("gh 未登录或凭证无效；请先运行 `gh auth login`。")
        raise RuntimeError(f"gh 搜索失败：{msg}")

    items: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """执行单轮搜索并按仓库全名去重。"""
    if not query or not query.strip():
        raise ValueError("查询不能为空。")
    if limit < 1:
        raise ValueError("limit 必须至少为 1。")
    results = _run_gh(query.strip(), limit)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in results:
        name = item.get("full_name", "")
        if name and name in seen:
            continue
        if name:
            seen.add(name)
        unique.append(item)
    return unique[:limit]


def search_with_cache(
    query: str, limit: int = 20, refresh: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """带缓存的单轮搜索。返回 (结果, 元信息)。

    同查询未过期直接复用；新查询实时搜索但把历史术语作为提示返回给 agent。
    缓存读写失败一律降级为实时搜索，不影响搜索本体。
    """
    from . import cache  # 延迟导入，保持 CLI 冷启动轻

    if not query or not query.strip():
        raise ValueError("查询不能为空。")
    if limit < 1:
        raise ValueError("limit 必须至少为 1。")

    cached = None if refresh else cache.get_cached(query, limit)
    meta: dict[str, Any] = {"source": "live", "stale": False, "term_hints": []}
    if cached and cached["hits"]:
        items = [
            {k: hit[k] for k in ("full_name", "description", "stars", "url")
             if k in hit}
            for hit in cached["hits"]
        ][:limit]
        meta.update({
            "source": "cache",
            "stale": cached["stale"],
            "age_days": cached["age_days"],
        })
        return items, meta

    items = search(query, limit)
    cache.put_cached(query, items, limit=limit)
    meta["term_hints"] = [
        h for h in cache.related_terms(query)
        if cache.normalize_query(h) != cache.normalize_query(query)
    ]
    return items, meta


def _prepare_argv(argv: list[str]) -> list[str]:
    """让以 '-' 开头的查询（GitHub 否定语法）无需用户手动加 '--'。

    选项只在查询词之前解析；第一个非选项 token 起全部是查询。
    查询若是多词，会合并成单个参数，避免被 argparse 当成多个位置参数。
    """
    if not argv or argv[0] != "search":
        return argv
    flags = {"--json", "--refresh", "-h", "--help"}
    options: list[str] = []
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in flags:
            options.append(token)
            i += 1
        elif token == "--limit" and i + 1 < len(argv):
            options.extend([token, argv[i + 1]])
            i += 2
        else:
            query = " ".join(argv[i:])
            if query.startswith("-"):
                return [argv[0], query, *options]
            return argv
    return argv


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="finder",
        description="单轮 GitHub 仓库搜索（迭代发现由 agent skill 驱动）。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("search", help="按查询词搜索仓库")
    sp.add_argument("query", help="GitHub 搜索查询词或自然语言意图")
    sp.add_argument("--limit", type=int, default=20,
                    help=f"返回条数，1-{MAX_PER_PAGE}（超出自动截断），默认 20")
    sp.add_argument("--json", action="store_true", help="以 JSON 数组输出")
    sp.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新搜索")

    cp = sub.add_parser("cache", help="管理本地搜索缓存")
    cp.add_argument("action", choices=["path", "clear", "export"],
                    help="path=显示位置；clear=清空；export=导出 JSON")
    cp.add_argument("--json", action="store_true", help="export 时以 JSON 输出（默认写文件）")

    tp = sub.add_parser("hint", help="给查询记录迭代中发现的术语")
    tp.add_argument("query", help="对应的查询词")
    tp.add_argument("terms", nargs="+", help="发现的术语（可多个）")

    args = parser.parse_args(_prepare_argv(raw))

    if args.command == "cache":
        from . import cache
        if args.action == "path":
            print(cache.db_path())
        elif args.action == "clear":
            n = cache.clear()
            print("缓存已清空" if n >= 0 else "清空失败")
        elif args.action == "export":
            data = cache.export_json()
            if args.json:
                print(data)
            else:
                try:
                    out = cache.cache_dir() / "export.json"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(data, encoding="utf-8")
                    print(f"已导出到 {out}")
                except OSError as exc:
                    print(f"导出失败（无法写缓存目录）：{exc}", file=sys.stderr)
                    print(data)  # 降级：至少把内容打到 stdout
                    return 1
        return 0

    if args.command == "hint":
        from . import cache
        cache.add_term_hints(args.query, args.terms)
        print(f"已记录 {len(args.terms)} 个术语提示")
        return 0

    try:
        items, meta = search_with_cache(args.query, args.limit, args.refresh)
    except (RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    label = "来自缓存" if meta["source"] == "cache" else "实时"
    if meta["source"] == "cache" and meta.get("stale"):
        label += "（可能过期，建议 --refresh）"
    terms = meta.get("term_hints") or []

    if args.json:
        # 保持既有输出契约：--json 仍是数组，且每项只含原四字段。
        shaped = [
            {k: item.get(k) for k in ("full_name", "description", "stars", "url")}
            for item in items
        ]
        print(json.dumps(shaped, ensure_ascii=False, indent=2))
        print(f"# source={meta['source']} stale={meta['stale']} terms={json.dumps(terms, ensure_ascii=False)}",
              file=sys.stderr)
    else:
        print(f"[{label}]")
        if terms:
            print("历史术语提示：" + "、".join(terms))
        if not items:
            print("未找到仓库。")
        for i, item in enumerate(items, 1):
            desc = (item.get("description") or "").replace("\n", " ").strip()
            stars = item.get("stars", 0)
            print(f"{i}. {item.get('full_name', '')}  ★{stars}")
            if desc:
                print(f"   {desc}")
            print(f"   {item.get('url', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
