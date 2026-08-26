"""Fetchers: rss, page, browser, inbox."""

import difflib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import feedparser
import trafilatura
from curl_cffi import requests

from .state import State

TIMEOUT = 30
# Feeds link to arbitrary URLs, including 4K videos. Reading one of those into
# memory (and decoding it as text) is what OOM-killed the 08:00 run on 2026-08-01.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
_BINARY_TYPES = ("video/", "audio/", "image/", "font/")


@dataclass
class Update:
    source: str
    title: str
    link: str
    content: str  # article text, or unified diff for page sources
    kind: str  # "article" | "page_change"


def http_get(url: str) -> str:
    """GET with browser TLS fingerprint to pass most anti-bot checks."""
    resp = requests.get(url, impersonate="chrome", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _is_pdf(url: str, content_type: str, head: bytes) -> bool:
    return (
        "pdf" in content_type.lower()
        or url.lower().endswith(".pdf")
        or head.startswith(b"%PDF")
    )


def extract_pdf_text(data: bytes) -> str | None:
    """Extract text from raw PDF bytes, page by page."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return text.strip() or None


def fetch_and_extract(url: str, with_images: bool = False) -> str | None:
    """GET a URL and extract its article text, handling PDFs (e.g. arXiv links)
    separately from HTML — feeding raw PDF bytes to the HTML extractor produces
    garbage (undecodable binary passed straight through as "text").

    Downloads are streamed and capped: media types are refused outright and
    anything over MAX_DOWNLOAD_BYTES is abandoned, so one oversized link can't
    take the whole run down with it."""
    resp = requests.get(url, impersonate="chrome", timeout=TIMEOUT, stream=True)
    try:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if content_type.lower().startswith(_BINARY_TYPES):
            return None
        data = b""
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            data += chunk
            if len(data) > MAX_DOWNLOAD_BYTES:
                return None
        encoding = resp.encoding or "utf-8"
    finally:
        resp.close()
    if _is_pdf(url, content_type, data[:8]):
        return extract_pdf_text(data)
    html = data.decode(encoding, "replace")
    text = extract_text(html, url=url)
    if not with_images or not text:
        return text
    images = extract_images(html, url)
    return f"{text}\n\n{image_section(images)}" if images else text


def browser_get_text(url: str, selector: str | None = None) -> str:
    """Render a JS-heavy page with Obscura and dump visible text."""
    binary = shutil.which("obscura") or str(Path.home() / ".local/bin/obscura")
    cmd = [binary, "fetch", url, "--dump", "text", "--wait", "15", "--stealth", "-q"]
    if selector:
        cmd += ["--selector", selector]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"obscura 失败: {proc.stderr.strip()[:200]}")
    return proc.stdout


def extract_text(html: str, url: str) -> str | None:
    return trafilatura.extract(html, url=url, include_links=False)


# The compile agent decides which figures are worth keeping, so recall here
# matters more than precision: collect the page's images liberally, cap the
# count, and only drop what is never article content.
MAX_IMAGES = 20
# Lazy-loading sites (知乎, 公众号) park a data: placeholder in src and keep
# the real image in one of these; whichever comes first wins.
_IMG_SRC_ATTRS = ("data-src", "data-original", "data-actualsrc", "src")
_IMG_CHROME_RE = re.compile(
    r"avatar|logo|icon|emoji|sprite|spacer|blank|pixel|qrcode|qr_code|badge|button",
    re.I,
)


def _is_chrome(url: str, elem) -> bool:
    """Site furniture rather than article content."""
    if url.startswith("data:") or url.lower().split("?")[0].endswith(".svg"):
        return True
    # the URL can be innocent while the alt or the class gives it away
    # (Trail of Bits serves its logo from /img/tob.png)
    if any(_IMG_CHROME_RE.search(v) for v in (url, elem.get("alt", ""), elem.get("class", ""))):
        return True
    for attr in ("width", "height"):
        value = elem.get(attr, "")
        if value.isdigit() and int(value) <= 32:
            return True
    return False


def extract_images(html: str, url: str, limit: int = MAX_IMAGES) -> list[tuple[str, str]]:
    """(absolute url, alt) for a page's content images, in document order.

    trafilatura's include_images drops them on most of the pages this project
    ingests, so the img elements are read straight off the tree instead.
    """
    from urllib.parse import urljoin

    from lxml import html as lxml_html

    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return []
    seen: dict[str, str] = {}
    for elem in tree.iter("img"):
        src = next((elem.get(a) for a in _IMG_SRC_ATTRS if elem.get(a)), None)
        if not src or _is_chrome(src, elem):
            continue
        absolute = urljoin(url, src.strip())
        if absolute not in seen:
            seen[absolute] = (elem.get("alt") or "").strip()
        if len(seen) >= limit:
            break
    return list(seen.items())


def image_section(images: list[tuple[str, str]]) -> str:
    """The `## 图片` block appended to an archived source: links, not copies."""
    lines = "\n".join(f"![{alt}]({url})" for url, alt in images)
    return f"## 图片\n\n{lines}"


def fetch_rss(source: dict, state: State, max_items: int) -> list[Update]:
    name = source["name"]
    feed = feedparser.parse(http_get(source["url"]))
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"feed 解析失败: {feed.bozo_exception}")

    updates = []
    for entry in feed.entries:
        item_id = entry.get("id") or entry.get("link")
        if not item_id or state.is_seen(name, item_id):
            continue
        if len(updates) < max_items:
            link = entry.get("link", "")
            content = None
            try:
                content = fetch_and_extract(link, with_images=True)
            except Exception:
                pass  # fall back to the feed's own summary
            if not content:
                content = trafilatura.html2txt(
                    entry.get("summary", "") or entry.get("title", "")
                )
            updates.append(
                Update(
                    source=name,
                    title=entry.get("title", "(无标题)"),
                    link=link,
                    content=content,
                    kind="article",
                )
            )
        # mark everything as seen so the backlog doesn't resurface next run
        state.mark_seen(name, item_id)
    return updates


def fetch_page(source: dict, state: State) -> list[Update]:
    name = source["name"]
    url = source["url"]
    if source["type"] == "browser":
        text = browser_get_text(url, source.get("selector"))
    else:
        text = fetch_and_extract(url)
    if not text or not text.strip():
        raise RuntimeError("正文提取为空")

    old = state.get_snapshot(name)
    state.save_snapshot(name, text)
    if old is None:
        return []  # first run: baseline only
    if old == text:
        return []

    diff = "\n".join(
        line
        for line in difflib.unified_diff(
            old.splitlines(), text.splitlines(), lineterm="", n=1
        )
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    return [
        Update(source=name, title=name, link=url, content=diff, kind="page_change")
    ]


# Mails carry protocol-relative links (`//host/path`) often enough that
# requiring a scheme silently loses them.
_URL_RE = re.compile(r"(?:https?:)?//[^\s)\]>\"']+")
# An inbox mail is either a bare link to follow or the article body pasted in
# full. Below this many characters we assume the former.
LINK_ONLY_CHARS = 300


# Pasted articles start with an image more often than with the article link.
_ASSET_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".mp4", ".webm")


def _normalize_url(url: str) -> str:
    return "https:" + url if url.startswith("//") else url


def _is_asset(url: str) -> bool:
    path, _, query = url.lower().partition("?")
    if path.endswith(_ASSET_SUFFIXES):
        return True
    # Image CDNs hide the type in the query (pbs.twimg.com/...?format=jpg).
    return any(f"format={ext.lstrip('.')}" in query for ext in _ASSET_SUFFIXES)


def _pick_link(text: str) -> str:
    """First non-asset URL in a mail body, or "" if there is none."""
    for url in _URL_RE.findall(text):
        url = _normalize_url(url)
        if not _is_asset(url):
            return url
    return ""


def _agentmail_client(api_key: str):
    from agentmail import AgentMail
    return AgentMail(api_key=api_key)


def message_body(client, inbox_id: str, msg) -> str:
    """Full mail body. `messages.list` only carries a truncated preview."""
    try:
        full = client.inboxes.messages.get(inbox_id, msg.message_id)
    except Exception:
        return msg.preview or ""
    return full.text or full.extracted_text or msg.preview or ""


def _follow(link: str) -> str | None:
    """Fetch a link-only mail's article."""
    return fetch_and_extract(link, with_images=True)


def fetch_inbox(source: dict, state: State, max_items: int) -> list[Update]:
    """Fetch unread received emails from an AgentMail inbox."""

    name = source["name"]
    env_name = source.get("api_key_env", "AGENTMAIL_API_KEY")
    api_key = os.environ.get(env_name)
    if not api_key:
        raise RuntimeError(f"环境变量 {env_name} 未设置")
    client = _agentmail_client(api_key)
    inbox_id = source["inbox_id"]

    resp = client.inboxes.messages.list(inbox_id)
    updates = []
    for msg in resp.messages:
        if "sent" in (msg.labels or []):
            continue
        item_id = msg.message_id
        if state.is_seen(name, item_id):
            continue
        if len(updates) < max_items:
            content = message_body(client, inbox_id, msg)
            link = _pick_link(content)
            if link:
                # A pasted article is already the content; re-fetching would
                # replace it with whatever the first link in it points at.
                if len(content) <= LINK_ONLY_CHARS:
                    try:
                        content = _follow(link) or content
                    except Exception:
                        pass
            updates.append(
                Update(
                    source=name,
                    title=msg.subject or "(无标题)",
                    link=link,
                    content=content,
                    kind="article",
                )
            )
        state.mark_seen(name, item_id)
    return updates


def fetch_source(source: dict, state: State, max_items: int) -> list[Update]:
    if source["type"] == "rss":
        return fetch_rss(source, state, max_items)
    if source["type"] in ("page", "browser"):
        return fetch_page(source, state)
    if source["type"] == "inbox":
        return fetch_inbox(source, state, max_items)
    raise ValueError(f"未知 source type: {source['type']}")
