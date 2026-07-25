"""qmd-backed search over the wiki, honouring its two-layer structure.

The wiki is not a flat pile of markdown: `pages/` is the curated distillation
and `sources/` the immutable archive it was compiled from. Throwing both into
one index and ranking them together buries pages under near-duplicate source
text, so this module searches them as separate qmd collections and only
escalates:

  1. search `pages` — the answer normally lives here;
  2. if the best page hit is weak (or --deep), search `sources` too;
  3. a source hit is reported through its `pages:` frontmatter, i.e. as
     "this evidence was compiled into page X", so the caller stays on the
     page level unless the source is still uncompiled.

qmd itself is an external CLI (`npm i -g @tobilu/qmd`); this is a thin
adapter that owns the layering, not a wrapper around every qmd flag.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from .wiki import parse_front

PAGES_COLLECTION = "wiki-pages"
SOURCES_COLLECTION = "wiki-sources"

# qmd search 子命令：BM25 精确词 / 向量近似 / 混合并重排（慢，需本地模型）
MODES = {"keyword": "search", "semantic": "vsearch", "hybrid": "query"}

# pages 命中最高分低于此值就当作「没答上」，下探 sources。qmd 的分数是
# 归一化的相关度，0.5 大致是「词面对上但不是主题」的水平。
WEAK_SCORE = 0.5


def _qmd_bin() -> str:
    """Locate the qmd CLI; systemd's PATH does not include the node prefix."""
    found = shutil.which("qmd")
    if found:
        return found
    local = Path.home() / ".local" / "node22" / "bin" / "qmd"
    return str(local) if local.exists() else "qmd"


def _run(args: list[str]) -> str:
    env = dict(os.environ)
    node_bin = Path.home() / ".local" / "node22" / "bin"
    if node_bin.exists():
        env["PATH"] = f"{node_bin}:{env.get('PATH', '')}"
    proc = subprocess.run(
        [_qmd_bin(), *args], capture_output=True, text=True, env=env, timeout=300
    )
    if proc.returncode != 0:
        raise RuntimeError(f"qmd {' '.join(args)} 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _query(collection: str, query: str, mode: str, limit: int) -> list[dict]:
    args = [MODES[mode], query, "-c", collection, "-n", str(limit), "--format", "json"]
    out = _run(args).strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def _rel(file: str, collection: str) -> str:
    """qmd://wiki-pages/foo.md -> foo.md"""
    prefix = f"qmd://{collection}/"
    return file[len(prefix):] if file.startswith(prefix) else file


def setup_collections(wiki_dir: Path) -> None:
    """Register the two collections and re-index. Idempotent, so the daily
    units can call it to keep the index fresh."""
    existing = _run(["collection", "list"])
    for name, path in (
        (PAGES_COLLECTION, wiki_dir / "pages"),
        (SOURCES_COLLECTION, wiki_dir / "sources"),
    ):
        if name in existing:
            continue
        _run(["collection", "add", str(path), "--name", name])
    _run(["update"])


def _page_meta(wiki_dir: Path, stem: str) -> dict:
    path = wiki_dir / "pages" / f"{stem}.md"
    if not path.exists():
        return {}
    return parse_front(path.read_text())[0]


def _source_pages(wiki_dir: Path, rel: str) -> tuple[list[str], dict]:
    path = wiki_dir / "sources" / rel
    if not path.exists():
        return [], {}
    meta = parse_front(path.read_text())[0]
    pages = [str(p) for p in (meta.get("pages") or [])]
    return pages, meta


def search_wiki(
    wiki_dir: Path,
    query: str,
    mode: str = "keyword",
    limit: int = 5,
    deep: bool = False,
) -> dict:
    """Layered search. Returns {"pages": [...], "sources": [...], "deep": bool}."""
    if mode not in MODES:
        raise ValueError(f"未知检索模式 {mode}，可选：{', '.join(MODES)}")

    pages = []
    for hit in _query(PAGES_COLLECTION, query, mode, limit):
        stem = _rel(hit.get("file", ""), PAGES_COLLECTION).removesuffix(".md")
        meta = _page_meta(wiki_dir, stem)
        pages.append({
            "page": f"{stem}.md",
            "score": hit.get("score", 0.0),
            "category": meta.get("category", ""),
            "description": meta.get("description", ""),
            "snippet": hit.get("snippet", ""),
        })

    best = max((p["score"] for p in pages), default=0.0)
    escalate = deep or best < WEAK_SCORE
    sources = []
    if escalate:
        known = {p["page"] for p in pages}
        for hit in _query(SOURCES_COLLECTION, query, mode, limit):
            rel = _rel(hit.get("file", ""), SOURCES_COLLECTION)
            compiled_into, meta = _source_pages(wiki_dir, rel)
            sources.append({
                "source": rel,
                "score": hit.get("score", 0.0),
                "title": meta.get("title", ""),
                "url": meta.get("url", ""),
                "compiled": bool(meta.get("compiled")),
                # 已沉淀且页面还没被 pages 检索捞到的，才值得提示回溯
                "pages": [p for p in compiled_into if p not in known],
                "snippet": hit.get("snippet", ""),
            })

    return {"pages": pages, "sources": sources, "deep": escalate}


def format_results(result: dict) -> str:
    lines = []
    if result["pages"]:
        lines.append("## pages（沉淀页面，优先读这些）")
        for p in result["pages"]:
            lines.append(f"- pages/{p['page']}  {p['score']:.2f}  [{p['category']}]")
            if p["description"]:
                lines.append(f"    {p['description']}")
    else:
        lines.append("## pages 无命中")

    if result["deep"]:
        lines.append("")
        if result["sources"]:
            lines.append("## sources（原始归档，取证或页面未覆盖时用）")
            for s in result["sources"]:
                flag = "" if s["compiled"] else "  未编译"
                lines.append(f"- sources/{s['source']}  {s['score']:.2f}{flag}")
                if s["title"]:
                    lines.append(f"    {s['title']}")
                if s["pages"]:
                    lines.append(f"    已沉淀进: {', '.join(s['pages'])}")
        else:
            lines.append("## sources 无命中")
    return "\n".join(lines)
