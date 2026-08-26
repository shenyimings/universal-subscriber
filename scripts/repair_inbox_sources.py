"""Re-archive inbox sources that were saved from the mail preview.

fetch_inbox used to read `msg.preview` (a ~200-char teaser) instead of the
message body, so any mail with the article pasted into it was archived as a
title plus one paragraph — and the summarizer then wrote a confident review of
an article nobody had actually read. This pulls the real bodies back out of
AgentMail, rewrites the affected `wiki/sources/**` files, re-summarizes them
against the persona, and flips them back to `compiled: false` so the wiki
compiler folds the real content in.

    uv run python scripts/repair_inbox_sources.py [--apply] [--config config.yaml]

Without --apply it only reports. Idempotent: a file whose archived text is
already at least as long as the mail body is left alone.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from subscriber import load_env  # noqa: E402
from subscriber.digest import summarize  # noqa: E402
from subscriber.fetch import Update, _agentmail_client, _pick_link  # noqa: E402

# Rewrite only when the mail body is decisively bigger than what we archived,
# so link-forwards (short mail, long fetched article) are never clobbered.
GROWTH = 1.3
MARGIN = 200


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _load_mails(client, inbox_id: str) -> list:
    """Every received mail in the inbox, with full bodies."""
    items, token = [], None
    while True:
        resp = client.inboxes.messages.list(inbox_id, limit=100, page_token=token)
        items += list(resp.messages)
        token = resp.next_page_token
        if not token:
            break
    mails = []
    for item in items:
        if "sent" in (item.labels or []):
            continue
        full = client.inboxes.messages.get(inbox_id, item.message_id)
        mails.append(
            {
                "subject": (item.subject or "(无标题)").strip(),
                "preview": item.preview or "",
                "text": full.text or full.extracted_text or "",
            }
        )
    return mails


def _match(mails: list, title: str, body: str):
    """The mail an archived source came from.

    Subject is the archive's title, which is unique except for the untitled
    mails; those are told apart by the preview still being the archived text.
    """
    same = [m for m in mails if m["subject"] == title.strip()]
    if len(same) == 1:
        return same[0]
    body_n = _norm(body)
    hits = [
        m
        for m in same
        if len(_norm(m["preview"])) >= 40 and body_n.startswith(_norm(m["preview"])[:40])
    ]
    return max(hits, key=lambda m: len(m["text"])) if hits else None


def _split(text: str) -> tuple[dict, str, str]:
    _, front, rest = text.split("---", 2)
    summary, body = rest.split("## 原文", 1)
    return yaml.safe_load(front), summary.split("## 摘要", 1)[-1].strip(), body.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--apply", action="store_true", help="真正写回，默认只报告")
    args = ap.parse_args()

    config_path = Path(args.config).resolve()
    root = config_path.parent
    load_env(root / ".env")
    cfg = yaml.safe_load(config_path.read_text())
    prompts = yaml.safe_load((root / "prompts.yaml").read_text())
    llm_cfg = cfg["llm"]
    max_chars = cfg.get("limits", {}).get("max_chars_per_item", 6000)
    wiki_dir = root / cfg.get("wiki", {}).get("dir", "wiki")

    inbox = next(s for s in cfg["sources"] if s["type"] == "inbox")
    import os

    api_key = os.environ[inbox.get("api_key_env", "AGENTMAIL_API_KEY")]
    mails = _load_mails(_agentmail_client(api_key), inbox["inbox_id"])
    print(f"收到邮件 {len(mails)} 封", file=sys.stderr)

    for path in sorted((wiki_dir / "sources").rglob("*.md")):
        text = path.read_text()
        if f"\nsource: {inbox['name']}" not in text:
            continue
        meta, _, body = _split(text)
        mail = _match(mails, meta["title"], body)
        if mail is None:
            continue
        if len(_norm(mail["text"])) <= len(_norm(body)) * GROWTH + MARGIN:
            continue

        print(f"{len(_norm(body)):>6} -> {len(_norm(mail['text'])):>6}  {path.name}")
        if not args.apply:
            continue

        content = mail["text"].strip()
        update = Update(
            source=meta["source"],
            title=meta["title"],
            link=meta["url"] or "",
            content=content,
            kind="article",
        )
        update.link = _pick_link(content) or update.link
        summary = summarize(update, llm_cfg, prompts, max_chars)
        if summary is None:
            print("  LLM 判定与画像无关(SKIP),只换正文,摘要保留原样", file=sys.stderr)
            summary = _split(text)[1]

        meta["url"] = update.link
        meta["compiled"] = False  # the page was built from the teaser; redo it
        front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        path.write_text(f"---\n{front}\n---\n\n## 摘要\n\n{summary}\n\n## 原文\n\n{content}\n")


if __name__ == "__main__":
    main()
