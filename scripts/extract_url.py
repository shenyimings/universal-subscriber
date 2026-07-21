"""抓取一个 URL 并抽取正文文本，供 wiki agent 的 fetch_url 工具调用。

用法:
    uv run python scripts/extract_url.py <url>

成功时正文写到 stdout；失败时报错信息写到 stderr 并以非零退出。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from subscriber.fetch import fetch_and_extract


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("用法: extract_url.py <url>")
    url = sys.argv[1]
    text = fetch_and_extract(url)
    if not text:
        sys.exit(f"正文抽取失败: {url}")
    print(text)


if __name__ == "__main__":
    main()
