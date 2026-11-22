"""缓存层测试：用临时目录隔离，不碰用户真实缓存。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finder import cache, cli  # noqa: E402


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_put_and_get_roundtrip(self):
        cache.put_cached("Agent Harnesses", [
            {"full_name": "botiverse/oar", "description": "programming interface",
             "stars": 692, "url": "https://github.com/botiverse/oar",
             "pushed_at": "2026-11-21T00:00:00Z", "archived": False},
        ], term_hints=["harnesses"])
        got = cache.get_cached("  agent   harnesses  ")  # 规范化后同键
        self.assertIsNotNone(got)
        self.assertEqual(got["hits"][0]["full_name"], "botiverse/oar")
        self.assertEqual(got["term_hints"][0]["term"], "harnesses")
        self.assertFalse(got["stale"])

    def test_stale_marker(self):
        cache.put_cached("old query", [])
        with mock.patch.object(cache.time, "time", return_value=time.time() + 8 * 86400):
            got = cache.get_cached("old query")
        self.assertTrue(got["stale"])

    def test_refresh_overwrites(self):
        cache.put_cached("q", [{"full_name": "a/old", "url": "x"}])
        cache.put_cached("q", [{"full_name": "b/new", "url": "y"}])
        got = cache.get_cached("q")
        self.assertEqual([h["full_name"] for h in got["hits"]], ["b/new"])

    def test_related_terms(self):
        cache.put_cached("coding agent orchestration", [], term_hints=["harnesses"])
        # 新查询与历史查询有词交集 → 借到术语
        self.assertIn("harnesses", cache.related_terms("coding agent runtime"))
        # 完全无关 → 不借
        self.assertNotIn("harnesses", cache.related_terms("cooking recipe"))

    def test_add_term_hints(self):
        cache.add_term_hints("agent runtime", ["harnesses", "interface"])
        self.assertEqual(set(cache.related_terms("agent runtime unified")),
                         {"harnesses", "interface"})
        # 同一术语重复记录不应重复
        cache.add_term_hints("agent runtime", ["harnesses"])
        self.assertEqual(cache.related_terms("agent runtime").count("harnesses"), 1)

    def test_hints_survive_refresh(self):
        # 核心回归：实时搜索 / --refresh 不能冲掉已积累的术语。
        cache.add_term_hints("agent runtime", ["harnesses"])
        with mock.patch.object(cli, "search", return_value=[{"full_name": "a/x"}]):
            cli.search_with_cache("agent runtime", refresh=True)
        self.assertIn("harnesses", cache.related_terms("agent runtime"))

    def test_limit_is_part_of_cache_key(self):
        items = [{"full_name": f"a/{i}"} for i in range(5)]
        with mock.patch.object(cli, "search", return_value=items) as live:
            first, _ = cli.search_with_cache("q limit", limit=5)
            second, _ = cli.search_with_cache("q limit", limit=20)
        self.assertEqual(len(first), 5)
        self.assertEqual(live.call_count, 2)  # 不同 limit 桶 → 各自实时搜
        self.assertEqual(len(second), 5)  # 假 gh 只返回 5 个，真实场景会返回 20

    def test_clear(self):
        cache.put_cached("q", [])
        self.assertEqual(cache.clear(), 1)
        self.assertIsNone(cache.get_cached("q"))

    def test_cache_unavailable_degrades(self):
        bad = Path(self._tmp.name) / "blocked"
        bad.write_text("not a dir")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(bad)}):
            self.assertIsNone(cache.get_cached("q"))
            cache.put_cached("q", [{"full_name": "a/x"}])  # 不抛异常
            self.assertEqual(cache.related_terms("q"), [])

    def test_search_with_cache_uses_cache(self):
        items = [{"full_name": "a/x", "description": "d", "stars": 1, "url": "u",
                  "pushed_at": "2026-01-01T00:00:00Z", "archived": False}]
        with mock.patch.object(cli, "search", return_value=items) as live:
            first, meta1 = cli.search_with_cache("q one")
            second, meta2 = cli.search_with_cache("q one")
        self.assertEqual(meta1["source"], "live")
        self.assertEqual(meta2["source"], "cache")
        self.assertEqual(live.call_count, 1)
        self.assertEqual(list(second[0].keys()),
                         ["full_name", "description", "stars", "url"])

    def test_search_with_cache_refresh(self):
        items = [{"full_name": "a/x", "description": "d", "stars": 1, "url": "u"}]
        with mock.patch.object(cli, "search", return_value=items) as live:
            cli.search_with_cache("q two")
            _, meta = cli.search_with_cache("q two", refresh=True)
        self.assertEqual(meta["source"], "live")
        self.assertEqual(live.call_count, 2)

    def test_json_contract_unchanged(self):
        items = [{"full_name": "a/x", "description": "d", "stars": 1, "url": "u",
                  "pushed_at": "x", "archived": False}]
        buf = __import__("io").StringIO()
        with mock.patch.object(cli, "search", return_value=items), \
             mock.patch.object(sys, "stdout", buf):
            rc = cli.main(["search", "q three", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(list(payload[0].keys()),
                         ["full_name", "description", "stars", "url"])


if __name__ == "__main__":
    unittest.main()
