"""Store an article's images inside the wiki.

Sources keep image URLs as links (see fetch.extract_images); only the figures
the compile agent judges worth keeping are downloaded here, into wiki/imgs/,
and referenced from pages as ![[name.ext]].
"""

import re
from pathlib import Path

from curl_cffi import requests

from .fetch import TIMEOUT

IMG_DIR = "imgs"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# The model names the file; the extension is ours, taken from what the server
# sent, so the name can never disagree with the bytes.
IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def save_image(url: str, name: str, wiki_dir: str | Path) -> str:
    """Download one image into wiki/imgs/. Returns the stored file name.

    Idempotent for the same name and bytes; a different image under a name
    already in use is an error rather than a silent overwrite.
    """
    if not IMAGE_NAME_RE.match(name):
        raise ValueError(f"图片名称 {name!r} 必须是小写字母数字连字符,不带扩展名")

    resp = requests.get(url, impersonate="chrome", timeout=TIMEOUT, stream=True)
    try:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = _EXT_BY_TYPE.get(content_type)
        if ext is None:
            raise ValueError(f"{url} 返回的不是图片({content_type or '无类型'})")
        data = b""
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(f"{url} 过大(超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB)")
    finally:
        resp.close()

    path = Path(wiki_dir) / IMG_DIR / f"{name}{ext}"
    if path.exists():
        if path.read_bytes() == data:
            return path.name
        raise ValueError(f"{path.name} 已存在且内容不同,换一个名字")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path.name
