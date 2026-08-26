"""把一张图片存进 wiki/imgs/，供 wiki agent 的 save_image 工具调用。

用法:
    uv run python scripts/save_image.py <url> <name> <wiki-dir>

成功时把存下来的文件名写到 stdout；失败时报错信息写到 stderr 并以非零退出。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from subscriber.images import save_image


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit("用法: save_image.py <url> <name> <wiki-dir>")
    url, name, wiki_dir = sys.argv[1:]
    try:
        print(save_image(url, name, wiki_dir))
    except Exception as exc:
        sys.exit(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
