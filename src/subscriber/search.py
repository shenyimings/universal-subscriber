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
import re
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


# qmd 的 qmd:// 路径是 slug 化的（`a--b.md` 会变成 `a-b.md`），不是磁盘上的
# 相对路径，所以命中回文件要反解。不去猜 qmd 的 slug 规则，而是两边都压成
# 「只留字母数字和汉字」再比对；归档文件名都带 8 位 hash 后缀，撞不上。
_SQUASH_RE = re.compile(r"[^0-9a-z一-鿿/]+")


def _squash(rel: str) -> str:
    return _SQUASH_RE.sub("", rel.lower())


def _resolve(base: Path, rel: str) -> Path | None:
    """Map a qmd-reported relative path back onto the file on disk."""
    direct = base / rel
    if direct.exists():
        return direct
    want = _squash(rel)
    for path in base.rglob("*.md"):
        if _squash(path.relative_to(base).as_posix()) == want:
            return path
    return None


def _registered_path(name: str) -> Path | None:
    """The directory qmd currently has under this collection name, if any."""
    shown = _run(["collection", "show", name])
    m = re.search(r"^\s*Path:\s*(.+)$", shown, re.MULTILINE)
    return Path(m.group(1).strip()) if m else None


def setup_collections(wiki_dir: Path) -> None:
    """Register the two collections and re-index. Idempotent, so the daily
    units can call it to keep the index fresh.

    Reconciles by path, not just by name: qmd stores absolute paths, and the
    portable wiki-search script re-points these same collection names at
    whatever clone it is running from. Checking the name alone would leave a
    collection indexing someone else's copy of the wiki.
    """
    for name, path in (
        (PAGES_COLLECTION, wiki_dir / "pages"),
        (SOURCES_COLLECTION, wiki_dir / "sources"),
    ):
        current = _registered_path(name)
        if current == path:
            continue
        if current is not None:
            _run(["collection", "remove", name])
        _run(["collection", "add", str(path), "--name", name])
    _run(["update"])


def _front_of(base: Path, rel: str) -> dict:
    path = _resolve(base, rel)
    return parse_front(path.read_text())[0] if path else {}


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
        name = _rel(hit.get("file", ""), PAGES_COLLECTION)
        meta = _front_of(wiki_dir / "pages", name)
        pages.append({
            "page": name,
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
            meta = _front_of(wiki_dir / "sources", rel)
            compiled_into = [str(p) for p in (meta.get("pages") or [])]
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
