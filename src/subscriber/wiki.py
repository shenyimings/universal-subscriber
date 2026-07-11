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


def rebuild_index(wiki_dir: Path) -> None:
    pages = sorted((wiki_dir / "pages").glob("*.md")) if (wiki_dir / "pages").exists() else []
    lines = ["# 索引", "", "由编译器自动重建,请勿手工编辑。", ""]
    for p in pages:
        meta, _ = parse_front(p.read_text())
        lines.append(f"- [[{p.stem}]] — {meta.get('description', '')}")
    (wiki_dir / "index.md").write_text("\n".join(lines) + "\n")


def append_log(wiki_dir: Path, action: str, detail: str) -> None:
    line = f"## [{date.today().isoformat()}] {action} | {detail}\n"
    with (wiki_dir / "log.md").open("a") as f:
        f.write(line)


def _chat(llm_cfg: dict, prompt: str) -> str:
    resp = _client(llm_cfg).chat.completions.create(
        model=llm_cfg["model"],
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


def _parse_plan(text: str) -> list[dict]:
    """Extract the JSON array from an LLM reply, tolerating code fences."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        plan = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for item in plan:
        file = str(item.get("file", "")).strip()
        if not file.endswith(".md") or "/" in file:
            continue
        out.append({
            "file": file,
            "action": item.get("action", "update"),
            "focus": item.get("focus", ""),
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
        page_prompt = prompts["wiki_page"].format(
            persona=prompts.get("persona", "").strip(),
            file=item["file"],
            focus=item["focus"],
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
