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
        "--jq", ".items[] | {full_name, description, stars: .stargazers_count, url: .html_url}",
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


def _prepare_argv(argv: list[str]) -> list[str]:
    """让以 '-' 开头的查询（GitHub 否定语法）无需用户手动加 '--'。

    子解析器里 '--' 之后的所有 token 都会变成位置参数，所以必须把选项放在
    '--' 之前，把查询放在 '--' 之后。
    """
    if len(argv) >= 2 and argv[0] == "search" and argv[1].startswith("-"):
        if argv[1] not in {"--limit", "--json", "-h", "--help"}:
            return [argv[0], *argv[2:], "--", argv[1]]
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

    args = parser.parse_args(_prepare_argv(raw))
    try:
        items = search(args.query, args.limit)
    except (RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
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
