"""Archive kept articles as markdown files under wiki/sources/.

Each file carries YAML frontmatter (compiled: false until the wiki
compiler ingests it), the Chinese summary, and the extracted full text.
"""

import hashlib
import re
from datetime import date
from pathlib import Path

import yaml

from .fetch import Update


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w一-鿿]+", "-", text.lower()).strip("-")
    return text[:max_len].rstrip("-") or "untitled"


def archive_update(
    update: Update, summary: str, wiki_dir: str | Path, day: date | None = None
) -> Path | None:
    """Write one article to wiki/sources/YYYY/MM/. Returns the path, or None if not archivable.

    page_change diffs are not archived: fragments of a page diff carry no
    lasting knowledge, and the snapshots already live in state.db.
    day overrides the archive date (used by the backfill script).
    """
    if update.kind != "article":
        return None
    today = day or date.today()
    digest8 = hashlib.sha256((update.link or update.title).encode()).hexdigest()[:8]
    name = f"{slugify(update.source)}--{slugify(update.title)}--{digest8}.md"
    path = Path(wiki_dir) / "sources" / f"{today.year}" / f"{today.month:02d}" / name
    if path.exists():
        return path

    meta = {
        "title": update.title,
        "source": update.source,
        "url": update.link,
        "date": today.isoformat(),
        "compiled": False,
    }
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{front}\n---\n\n## 摘要\n\n{summary}\n\n## 原文\n\n{update.content}\n"
    )
    return path
