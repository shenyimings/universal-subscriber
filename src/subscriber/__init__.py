"""subscriber: 监控信息源更新，LLM 总结翻译成中文日报，邮件送达。"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import yaml

from .archive import archive_update
from .digest import build_digest
from .fetch import fetch_source
from .state import State


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    parser = argparse.ArgumentParser(prog="subscriber", description=__doc__)
    parser.add_argument(
        "command",
        choices=["run", "wiki", "search"],
        help="run: 抓取并生成日报;wiki: 把已归档的 sources 编译进知识库;"
        "search: 在 wiki 里按 pages -> sources 分层检索",
    )
    parser.add_argument("query", nargs="?", help="search: 检索词")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--source", help="只跑指定名称的源")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印日报，不发送邮件"
    )
    parser.add_argument("--lint", action="store_true", help="wiki: 只做健康检查")
    parser.add_argument(
        "--fix", action="store_true", help="wiki: 与 --lint 连用，先做一轮坏链修复"
    )
    parser.add_argument("--limit", type=int, help="wiki: 本次最多编译几篇;search: 每层返回几条")
    parser.add_argument(
        "--reindex", action="store_true", help="wiki: 只重建 index.md（供 agent 调用）"
    )
    parser.add_argument(
        "--mode",
        choices=["keyword", "semantic", "hybrid"],
        default="keyword",
        help="search: keyword=BM25 精确词;semantic=向量近似;hybrid=混合并重排（慢）",
    )
    parser.add_argument(
        "--deep", action="store_true", help="search: 无论 pages 命中如何都下探 sources"
    )
    parser.add_argument(
        "--setup", action="store_true", help="search: 注册/刷新 qmd collection 后退出"
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    root = config_path.parent
    load_env(root / ".env")
    cfg = yaml.safe_load(config_path.read_text())
    prompts = yaml.safe_load((root / "prompts.yaml").read_text())

    state = State(root / "data" / "state.db")
    limits = cfg.get("limits", {})
    max_items = limits.get("max_items_per_source", 5)
    max_chars = limits.get("max_chars_per_item", 6000)
    wiki_dir = root / cfg.get("wiki", {}).get("dir", "wiki")

    if args.command == "search":
        from .search import format_results, search_wiki, setup_collections

        if args.setup:
            setup_collections(wiki_dir)
            print("qmd collection 已就绪。", file=sys.stderr)
            return
        if not args.query:
            sys.exit("search 需要检索词，例如：subscriber search '智能体评估'")
        result = search_wiki(
            wiki_dir, args.query, args.mode, args.limit or 5, args.deep
        )
        print(format_results(result))
        return

    if args.command == "wiki":
        from .wiki import compile_wiki, fix_wikilinks, lint_wiki, rebuild_index

        if args.reindex:
            rebuild_index(wiki_dir)
            return
        if args.lint:
            if args.fix:
                fix_wikilinks(wiki_dir, cfg["llm"], prompts)
            issues = lint_wiki(wiki_dir)
            print("\n".join(issues) if issues else "wiki 状态健康。")
            return
        n = compile_wiki(wiki_dir, cfg["llm"], prompts, max_chars, args.limit)
        print(f"[wiki] 本次编译 {n} 篇 source。", file=sys.stderr)
        return

    sources = cfg["sources"]
    if args.source:
        sources = [s for s in sources if s["name"] == args.source]
        if not sources:
            sys.exit(f"未找到名为 {args.source!r} 的源")

    updates = []
    for source in sources:
        try:
            found = fetch_source(source, state, max_items)
            print(f"[fetch] {source['name']}: {len(found)} 条更新", file=sys.stderr)
            updates.extend(found)
        except Exception as e:
            print(f"[fetch] {source['name']} 失败: {e}", file=sys.stderr)

    if not updates:
        print("没有更新，不生成日报。", file=sys.stderr)
        return

    digest = build_digest(
        updates,
        cfg["llm"],
        prompts,
        max_chars,
        on_keep=lambda u, s: archive_update(u, s, wiki_dir),
    )
    if digest is None:
        print("所有更新均与关注方向无关，不生成日报。", file=sys.stderr)
        return
    print(digest)

    if args.dry_run:
        print("\n[dry-run] 未发送邮件。", file=sys.stderr)
        return

    from .deliver import send_email

    email_cfg = cfg["email"]
    subject = f"{email_cfg.get('subject_prefix', '[信息日报]')} {date.today().isoformat()}"
    send_email(digest, subject, email_cfg["to"], state)
    print(f"[deliver] 已发送至 {email_cfg['to']}", file=sys.stderr)
