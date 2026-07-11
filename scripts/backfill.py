"""一次性回填脚本:把各 source 的历史文章抓下来,初始化 wiki/sources/ 知识库。

绕过 state.db 的 seen 状态,每个源最多 N 条(由近到远),走与日报相同的
摘要 + SKIP 过滤,通过者归档。browser 类源跳过(JS 渲染页拿不到文章列表)。

用法:
    uv run python scripts/backfill.py [--source NAME] [--max-items 15]
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from subscriber import load_env  # noqa: E402
from subscriber.archive import archive_update  # noqa: E402
from subscriber.digest import summarize  # noqa: E402
from subscriber.fetch import Update, extract_text, http_get  # noqa: E402


def entry_date(entry) -> date | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return date(t.tm_year, t.tm_mon, t.tm_mday)
    return None


def rss_items(source: dict, max_items: int) -> list[tuple[str, str, date | None]]:
    """(title, link, published) for the newest max_items entries."""
    feed = feedparser.parse(http_get(source["url"]))
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"feed 解析失败: {feed.bozo_exception}")
    items = []
    for entry in feed.entries[:max_items]:
        link = entry.get("link")
        if link:
            items.append((entry.get("title", "(无标题)"), link, entry_date(entry)))
    return items


def page_items(source: dict, max_items: int) -> list[tuple[str, str, date | None]]:
    """Heuristic: article links on a listing page extend the listing's own path."""
    import re

    url = source["url"]
    html = http_get(url)
    base = urlparse(url)
    base_path = base.path.rstrip("/")
    seen, items = set(), []
    for href in re.findall(r'href="([^"#?]+)"', html):
        full = urljoin(url, href)
        p = urlparse(full)
        if p.netloc != base.netloc:
            continue
        # must be strictly deeper than the listing page itself
        if not p.path.rstrip("/").startswith(base_path + "/"):
            continue
        if full in seen:
            continue
        seen.add(full)
        items.append((p.path.rstrip("/").rsplit("/", 1)[-1], full, None))
        if len(items) >= max_items:
            break
    return items


def process_item(
    source_name: str, title: str, link: str, published: date | None,
    llm_cfg: dict, prompts: dict, max_chars: int, wiki_dir: Path,
) -> str:
    try:
        content = extract_text(http_get(link), link)
    except Exception as e:
        return f"  跳过(抓取失败): {link} ({e})"
    if not content or len(content) < 200:
        return f"  跳过(正文过短): {link}"
    update = Update(source=source_name, title=title, link=link,
                    content=content, kind="article")
    try:
        summary = summarize(update, llm_cfg, prompts, max_chars)
    except Exception as e:
        return f"  跳过(LLM 失败): {title} ({e})"
    if summary is None:
        return f"  SKIP(无关): {title}"
    path = archive_update(update, summary, wiki_dir, day=published)
    return f"  归档: {path}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="只回填指定名称的源")
    parser.add_argument("--max-items", type=int, default=15)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    load_env(root / ".env")
    cfg = yaml.safe_load((root / "config.yaml").read_text())
    prompts = yaml.safe_load((root / "prompts.yaml").read_text())
    llm_cfg = cfg["llm"]
    max_chars = cfg.get("limits", {}).get("max_chars_per_item", 6000)
    wiki_dir = root / cfg.get("wiki", {}).get("dir", "wiki")

    sources = cfg["sources"]
    if args.source:
        sources = [s for s in sources if s["name"] == args.source]

    jobs = []  # (source_name, title, link, published)
    for source in sources:
        name, stype = source["name"], source["type"]
        try:
            if stype == "rss":
                items = rss_items(source, args.max_items)
            elif stype == "page":
                items = page_items(source, args.max_items)
            else:
                print(f"[{name}] 跳过({stype} 类源不支持回填)")
                continue
        except Exception as e:
            print(f"[{name}] 列表抓取失败: {e}")
            continue
        print(f"[{name}] 待处理 {len(items)} 条")
        jobs += [(name, t, l, d) for t, l, d in items]

    start = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_item, n, t, l, d, llm_cfg, prompts, max_chars, wiki_dir): (n, t)
            for n, t, l, d in jobs
        }
        for fut in as_completed(futures):
            done += 1
            name, _ = futures[fut]
            print(f"[{done}/{len(jobs)}] [{name}]{fut.result()}", flush=True)
    print(f"完成,耗时 {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
