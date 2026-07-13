"""Wiki compiler: ingest archived sources into a curated markdown wiki.

Layout under the wiki dir:
  sources/YYYY/MM/*.md  raw archived articles (written by archive.py, immutable)
  pages/*.md            curated knowledge pages, cross-linked with [[wikilinks]]
  index.md              page catalog, rebuilt deterministically from frontmatter
  log.md                append-only ingest log

The LLM only does two things per source: plan which pages to touch (JSON),
and write/merge one page at a time. Index and log are maintained in code so
they cannot drift.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

import yaml

from .digest import _client

_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")

# 固定分类，索引按此分节；不在列表内的归入 uncategorized。
CATEGORIES = [
    "agent-engineering",
    "ai-security",
    "blockchain-security",
    "llm-systems",
    "program-analysis",
]


def parse_front(text: str) -> tuple[dict, str]:
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    return meta, text[m.end():]


def dump_front(meta: dict, body: str) -> str:
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{front}\n---\n{body}"


def pending_sources(wiki_dir: Path) -> list[Path]:
    files = []
    for path in sorted((wiki_dir / "sources").rglob("*.md")):
        meta, _ = parse_front(path.read_text())
        if meta.get("compiled") is False:
            files.append((meta.get("date", ""), path))
    return [p for _, p in sorted(files)]


def _page_entries(wiki_dir: Path) -> list[tuple[str, dict]]:
    pages_dir = wiki_dir / "pages"
    pages = sorted(pages_dir.glob("*.md")) if pages_dir.exists() else []
    return [(p.stem, parse_front(p.read_text())[0]) for p in pages]


def _index_line(stem: str, meta: dict) -> str:
    tags = " ".join(f"`{t}`" for t in meta.get("tags") or [])
    tags = f" {tags}" if tags else ""
    return f"- [[{stem}]]{tags} — {meta.get('description', '')}"


def _category_of(meta: dict) -> str:
    cat = meta.get("category", "")
    return cat if cat in CATEGORIES else "uncategorized"


def category_index(wiki_dir: Path, category: str) -> str:
    """同一分类下的页面清单，供 wiki_page prompt 做同类互引上下文。"""
    lines = [
        _index_line(stem, meta)
        for stem, meta in _page_entries(wiki_dir)
        if _category_of(meta) == category
    ]
    return "\n".join(lines) if lines else "(该分类暂无页面)"


def rebuild_index(wiki_dir: Path) -> None:
    entries = _page_entries(wiki_dir)
    lines = ["# 索引", "", "由编译器自动重建,请勿手工编辑。", ""]
    for cat in CATEGORIES + ["uncategorized"]:
        group = [(s, m) for s, m in entries if _category_of(m) == cat]
        if not group:
            continue
        lines += [f"## {cat}", ""]
        lines += [_index_line(s, m) for s, m in group]
        lines.append("")
    (wiki_dir / "index.md").write_text("\n".join(lines).rstrip("\n") + "\n")


def append_log(wiki_dir: Path, action: str, detail: str) -> None:
    line = f"## [{date.today().isoformat()}] {action} | {detail}\n"
    with (wiki_dir / "log.md").open("a") as f:
        f.write(line)


def _chat(llm_cfg: dict, prompt: str) -> str:
    resp = _client(llm_cfg).chat.completions.create(
        model=llm_cfg.get("wiki_model", llm_cfg["model"]),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_json_array(text: str) -> list:
    """Extract the JSON array from an LLM reply, tolerating code fences."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []


def _parse_plan(text: str) -> list[dict]:
    plan = _parse_json_array(text)
    out = []
    for item in plan:
        file = str(item.get("file", "")).strip()
        if not file.endswith(".md") or "/" in file:
            continue
        category = str(item.get("category", "")).strip()
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        out.append({
            "file": file,
            "action": item.get("action", "update"),
            "focus": item.get("focus", ""),
            "category": category if category in CATEGORIES else "",
            "tags": [str(t).strip().lower() for t in tags if str(t).strip()],
        })
    return out[:3]


def _split_source_body(body: str) -> tuple[str, str]:
    """Return (summary, full text) from an archived source file body."""
    if "## 原文" in body:
        summary, _, content = body.partition("## 原文")
        return summary.replace("## 摘要", "").strip(), content.strip()
    return "", body.strip()


def compile_source(
    src_path: Path, wiki_dir: Path, llm_cfg: dict, prompts: dict, max_chars: int
) -> list[str]:
    """Ingest one archived source; returns the page files touched."""
    meta, body = parse_front(src_path.read_text())
    summary, content = _split_source_body(body)
    index_text = (wiki_dir / "index.md").read_text() if (wiki_dir / "index.md").exists() else "(空)"

    plan_prompt = prompts["wiki_plan"].format(
        persona=prompts.get("persona", "").strip(),
        index=index_text,
        categories=", ".join(CATEGORIES),
        title=meta.get("title", ""),
        source=meta.get("source", ""),
        url=meta.get("url", ""),
        summary=summary,
        content=content[:max_chars],
    )
    plan = _parse_plan(_chat(llm_cfg, plan_prompt))

    touched = []
    pages_dir = wiki_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for item in plan:
        page_path = pages_dir / item["file"]
        existing = page_path.read_text() if page_path.exists() else "(新页面,尚无内容)"
        category = item["category"] or _category_of(parse_front(existing)[0])
        page_prompt = prompts["wiki_page"].format(
            persona=prompts.get("persona", "").strip(),
            file=item["file"],
            focus=item["focus"],
            index=category_index(wiki_dir, category),
            category=category,
            existing=existing,
            title=meta.get("title", ""),
            url=meta.get("url", ""),
            date=meta.get("date", ""),
            summary=summary,
            content=content[:max_chars],
        )
        page = _chat(llm_cfg, page_prompt)
        page = re.sub(r"\A```(?:markdown)?\n|\n```\Z", "", page).strip() + "\n"
        page_meta, page_body = parse_front(page)
        if "description" not in page_meta:
            page_meta["description"] = item["focus"][:80]
        if item["category"] and page_meta.get("category") not in CATEGORIES:
            page_meta["category"] = item["category"]
        if item["tags"] and not page_meta.get("tags"):
            page_meta["tags"] = item["tags"]
        page_meta["updated"] = date.today().isoformat()
        page_path.write_text(dump_front(page_meta, page_body))
        touched.append(item["file"])

    # mark the source compiled so it never flows through the LLM again
    meta["compiled"] = True
    src_path.write_text(dump_front(meta, body))
    detail = f"{meta.get('title', src_path.stem)} -> {', '.join(touched) or '(无沉淀)'}"
    append_log(wiki_dir, "ingest", detail)
    return touched


def compile_wiki(
    wiki_dir: Path, llm_cfg: dict, prompts: dict, max_chars: int, limit: int | None = None
) -> int:
    """Compile pending sources into pages; returns how many sources were processed."""
    pending = pending_sources(wiki_dir)
    if limit:
        pending = pending[:limit]
    for i, src in enumerate(pending, 1):
        try:
            touched = compile_source(src, wiki_dir, llm_cfg, prompts, max_chars)
            print(f"[wiki {i}/{len(pending)}] {src.name}: {touched or 'skip'}", file=sys.stderr)
        except Exception as e:
            print(f"[wiki {i}/{len(pending)}] {src.name} 失败: {e}", file=sys.stderr)
        rebuild_index(wiki_dir)
    return len(pending)


def lint_wiki(wiki_dir: Path) -> list[str]:
    """Deterministic health checks: broken wikilinks, orphan pages, backlog size."""
    issues = []
    pages_dir = wiki_dir / "pages"
    pages = {p.stem: p.read_text() for p in pages_dir.glob("*.md")} if pages_dir.exists() else {}

    linked = set()
    for name, text in pages.items():
        for target in _WIKILINK_RE.findall(text):
            target = target.strip()
            linked.add(target)
            if target not in pages:
                issues.append(f"坏链: pages/{name}.md -> [[{target}]]")
    for name in pages:
        if name not in linked:
            issues.append(f"孤儿页(无入链): pages/{name}.md")

    backlog = len(pending_sources(wiki_dir))
    if backlog:
        issues.append(f"待编译 sources: {backlog} 篇(运行 subscriber wiki)")
    return issues


# --lint --fix: 坏链修复。保留配额 = 全库 wikilink 出现次数 * _KEEP_RATIO。
_KEEP_RATIO = 0.10
_UNBUILT = "（未建）"


def _broken_link_stats(pages: dict[str, str]) -> tuple[int, dict[str, dict]]:
    """Return (total link occurrences, broken target -> {count, pages})."""
    total = 0
    broken: dict[str, dict] = {}
    for name, text in pages.items():
        for target in _WIKILINK_RE.findall(text):
            target = target.strip()
            total += 1
            if target not in pages:
                info = broken.setdefault(target, {"count": 0, "pages": set()})
                info["count"] += 1
                info["pages"].add(name)
    return total, broken


def _apply_fix(text: str, target: str, action: str, to: str = "") -> str:
    t = re.escape(target)
    if action == "rename" and to:
        return re.sub(r"\[\[" + t + r"(?=[\]|#])", f"[[{to}", text)
    if action == "drop":
        # [[t|alias]] -> alias, [[t]] / [[t#sec]] -> t; 顺带清掉旧的（未建）标记
        text = re.sub(r"\[\[" + t + r"\|([^\]]*)\]\](?:" + _UNBUILT + ")?", r"\1", text)
        return re.sub(r"\[\[" + t + r"(?:#[^\]]*)?\]\](?:" + _UNBUILT + ")?", target, text)
    if action == "keep":
        return re.sub(
            r"(\[\[" + t + r"(?:[|#][^\]]*)?\]\])(?!" + _UNBUILT + ")",
            r"\1" + _UNBUILT,
            text,
        )
    return text


def _strip_stale_markers(text: str, existing: set[str]) -> str:
    """Remove（未建）markers whose target has since become a real page."""
    def repl(m: re.Match) -> str:
        return m.group(1) if m.group(2).strip() in existing else m.group(0)

    return re.sub(r"(\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\])" + _UNBUILT, repl, text)


def fix_wikilinks(wiki_dir: Path, llm_cfg: dict, prompts: dict) -> None:
    """LLM-assisted broken-link repair: rename near misses, drop noise,
    keep (and mark) the few targets genuinely worth creating later."""
    pages_dir = wiki_dir / "pages"
    pages = (
        {p.stem: p.read_text() for p in sorted(pages_dir.glob("*.md"))}
        if pages_dir.exists()
        else {}
    )
    total, broken = _broken_link_stats(pages)
    broken_count = sum(v["count"] for v in broken.values())
    if not broken:
        print("[fix] 没有坏链，无需修复。", file=sys.stderr)
        return

    budget = int(total * _KEEP_RATIO)
    stems = "\n".join(sorted(pages))
    targets = sorted(broken.items(), key=lambda kv: -kv[1]["count"])
    decisions: dict[str, dict] = {}
    remaining = budget
    for i in range(0, len(targets), 100):
        chunk = targets[i : i + 100]
        listing = "\n".join(
            f"- {t}（出现 {info['count']} 次；页面: {', '.join(sorted(info['pages']))}）"
            for t, info in chunk
        )
        prompt = prompts["wiki_fix"].format(
            pages=stems,
            broken=listing,
            total=total,
            broken_count=broken_count,
            budget=max(remaining, 0),
        )
        for item in _parse_json_array(_chat(llm_cfg, prompt)):
            target = str(item.get("target", "")).strip()
            action = str(item.get("action", "")).strip()
            to = str(item.get("to", "")).strip()
            if target not in broken or action not in ("rename", "drop", "keep"):
                continue
            if action == "rename" and to not in pages:
                continue
            decisions[target] = {"action": action, "to": to}
            if action == "keep":
                remaining -= broken[target]["count"]

    existing = set(pages)
    for stem in pages:
        text = pages[stem]
        for target, d in decisions.items():
            text = _apply_fix(text, target, d["action"], d["to"])
        text = _strip_stale_markers(text, existing)
        if text != pages[stem]:
            (pages_dir / f"{stem}.md").write_text(text)
            pages[stem] = text

    counts = {"rename": 0, "drop": 0, "keep": 0}
    for d in decisions.values():
        counts[d["action"]] += 1
    unhandled = len(broken) - len(decisions)
    new_total, new_broken = _broken_link_stats(pages)
    new_count = sum(v["count"] for v in new_broken.values())

    def pct(n: int, m: int) -> str:
        return f"{n / m * 100:.1f}%" if m else "0%"

    summary = (
        f"rename {counts['rename']} / drop {counts['drop']} / keep {counts['keep']}"
        f" / 未处理 {unhandled}（按目标计）；坏链 {broken_count}/{total}"
        f"（{pct(broken_count, total)}）-> {new_count}/{new_total}"
        f"（{pct(new_count, new_total)}）"
    )
    print(f"[fix] {summary}", file=sys.stderr)
    append_log(wiki_dir, "fix", summary)
