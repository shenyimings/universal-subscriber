"""Fetchers: rss (feed + article pages) and page (watch a URL for changes)."""

import difflib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import feedparser
import trafilatura
from curl_cffi import requests

from .state import State

TIMEOUT = 30


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
                content = extract_text(http_get(link), link)
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
        text = extract_text(http_get(url), url)
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


def fetch_source(source: dict, state: State, max_items: int) -> list[Update]:
    if source["type"] == "rss":
        return fetch_rss(source, state, max_items)
    if source["type"] in ("page", "browser"):
        return fetch_page(source, state)
    raise ValueError(f"未知 source type: {source['type']}")
