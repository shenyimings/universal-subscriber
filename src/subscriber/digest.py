"""Summarize updates into a Chinese digest via an OpenAI-compatible LLM API.

Prompt templates live in prompts.yaml so they can be edited without touching code.
"""

import os
import sys
from datetime import date

from openai import OpenAI

from .fetch import Update


def _client(llm_cfg: dict) -> OpenAI:
    api_key = os.environ.get(llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    if not api_key:
        raise RuntimeError(f"环境变量 {llm_cfg.get('api_key_env')} 未设置")
    return OpenAI(api_key=api_key, base_url=llm_cfg["base_url"])


def summarize(
    update: Update, llm_cfg: dict, prompts: dict, max_chars: int
) -> str | None:
    """Return a Chinese summary, or None if the LLM deems it irrelevant (SKIP)."""
    tpl = prompts["article"] if update.kind == "article" else prompts["page"]
    prompt = tpl.format(
        persona=prompts.get("persona", "").strip(),
        source=update.source,
        title=update.title,
        link=update.link,
        content=update.content[:max_chars],
    )
    resp = _client(llm_cfg).chat.completions.create(
        model=llm_cfg["model"],
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text or text.splitlines()[0].strip().upper().startswith("SKIP"):
        return None
    return text


def build_digest(
    updates: list[Update],
    llm_cfg: dict,
    prompts: dict,
    max_chars: int,
    on_keep=None,
) -> str | None:
    """Group updates by source and render a plain-text digest.

    Returns None if every update was filtered out as irrelevant.
    on_keep(update, summary) is called for each item that survives the
    SKIP filter (used to archive articles into the wiki).
    """
    by_source: dict[str, list[Update]] = {}
    for u in updates:
        by_source.setdefault(u.source, []).append(u)

    lines = [f"信息日报 · {date.today().isoformat()}", ""]
    kept = 0
    for source, items in by_source.items():
        section = [f"■ {source}"]
        for u in items:
            try:
                summary = summarize(u, llm_cfg, prompts, max_chars)
            except Exception as e:
                # Don't let one failing LLM call sink the whole batch; the
                # item has already been marked seen by the fetcher.
                print(f"[digest] LLM 失败,跳过条目 {u.title}: {e}", file=sys.stderr)
                continue
            if summary is None:
                print(f"[digest] 过滤无关条目: {u.title}", file=sys.stderr)
                continue
            if on_keep:
                on_keep(u, summary)
            section += ["", summary, f"链接: {u.link}"]
            kept += 1
        if len(section) > 1:
            lines += section + ["", "─" * 40, ""]
    if kept == 0:
        return None
    return "\n".join(lines).strip()
