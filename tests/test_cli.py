"""CLI 最小测试：用 fake gh 验证调用、去重、格式化与错误路径。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finder import cli  # noqa: E402


def _gh_result(names: list[str]) -> str:
    return "\n".join(
        json.dumps({
            "full_name": name,
            "description": f"desc of {name}",
            "stars": 1,
            "url": f"https://github.com/{name}",
        }, ensure_ascii=False)
        for name in names
    )


class TestSearch(unittest.TestCase):
    def test_dedup_and_limit(self):
        fake = mock.Mock(returncode=0, stdout=_gh_result(["a/x", "a/x", "b/y"]), stderr="")
        with mock.patch.object(cli.shutil, "which", return_value="/fake/gh"), \
             mock.patch.object(cli.subprocess, "run", return_value=fake):
            items = cli.search("whatever", limit=2)
        self.assertEqual([i["full_name"] for i in items], ["a/x", "b/y"])

    def test_empty_query(self):
        with self.assertRaises(ValueError):
            cli.search("   ")

    def test_gh_missing(self):
        with mock.patch.object(cli.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "gh 命令行"):
                cli.search("x")

    def test_auth_error(self):
        fake = mock.Mock(returncode=1, stdout="", stderr="Bad credentials")
        with mock.patch.object(cli.shutil, "which", return_value="/fake/gh"), \
             mock.patch.object(cli.subprocess, "run", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "gh auth login"):
                cli.search("x")

    def test_rate_limit_error_containing_authenticated(self):
        # GitHub 未认证限流提示里也有 "Authenticated"，不能被误判为未登录。
        msg = ("API rate limit exceeded for unauthenticated requests. "
               "Authenticated requests get a higher rate limit.")
        fake = mock.Mock(returncode=1, stdout="", stderr=msg)
        with mock.patch.object(cli.shutil, "which", return_value="/fake/gh"), \
             mock.patch.object(cli.subprocess, "run", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "限流"):
                cli.search("x")

    def test_timeout(self):
        with mock.patch.object(cli.shutil, "which", return_value="/fake/gh"), \
             mock.patch.object(cli.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("gh", 30)):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                cli.search("x")

    def test_limit_below_one(self):
        with self.assertRaisesRegex(ValueError, "limit"):
            cli.search("x", limit=0)

    def test_leading_dash_query(self):
        # 以 '-' 开头的 GitHub 否定语法不应被 argparse 当成选项。
        fake = mock.Mock(returncode=0, stdout=_gh_result(["a/x"]), stderr="")
        with mock.patch.object(cli.shutil, "which", return_value="/fake/gh"), \
             mock.patch.object(cli.subprocess, "run", return_value=fake) as run:
            rc = cli.main(["search", "-language:python", "--json"])
        self.assertEqual(rc, 0)
        cmd = run.call_args.args[0]
        self.assertTrue(any(arg == "q=-language:python" for arg in cmd), cmd)

    def test_main_json_contract(self):
        fake = mock.Mock(returncode=0, stdout=_gh_result(["a/x"]), stderr="")
        buf = __import__("io").StringIO()
        with mock.patch.object(cli.shutil, "which", return_value="/fake/gh"), \
             mock.patch.object(cli.subprocess, "run", return_value=fake), \
             mock.patch.object(sys, "stdout", buf):
            rc = cli.main(["search", "x", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(list(payload[0].keys()),
                         ["full_name", "description", "stars", "url"])
        self.assertEqual(payload[0]["full_name"], "a/x")

    def test_main_error_exit_code(self):
        with mock.patch.object(cli.shutil, "which", return_value=None), \
             mock.patch.object(sys, "stderr", __import__("io").StringIO()):
            self.assertEqual(cli.main(["search", "x"]), 1)


if __name__ == "__main__":
    unittest.main()
