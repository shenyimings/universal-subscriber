#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "curl-cffi>=0.15.0",
#     "rapidocr-onnxruntime>=1.4.0",
#     "pillow>=11.0.0",
# ]
# ///
"""Turn an interview-notes link into plain markdown text ready for interview.md.

Handles the three shapes that actually show up:

  xiaohongshu  the note JSON is embedded in the page as __INITIAL_STATE__;
               the questions are often NOT in the text but inside screenshots,
               so --ocr downloads imageList and runs RapidOCR over it.
  wechat       mp.weixin.qq.com article body lives in #js_content. trafilatura
               mis-extracts these pages badly (measured: 319 of 3094 chars,
               1 of 8 questions), so this slices the container directly.
  anything     falls back to obscura (headless browser) — slower, but survives
               a markup change that breaks the two parsers above.

Usage:
    ./capture.py <url> [--ocr] [--browser] [--json] [--keep-images DIR]

First run pulls ~120MB of OCR deps into the uv cache; later runs are instant.
"""

import argparse
import html as htmllib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT = 30
IMPERSONATE = "chrome"


# --------------------------------------------------------------------------
# fetching


def http_get(url: str, binary: bool = False):
    from curl_cffi import requests

    resp = requests.get(url, impersonate=IMPERSONATE, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.content if binary else resp.text


def browser_get(url: str, dump: str = "markdown", eval_js: str | None = None) -> str:
    """Obscura fallback. ~25s per page vs ~1.5s for the direct parsers."""
    binary = shutil.which("obscura") or str(Path.home() / ".local/bin/obscura")
    cmd = [binary, "fetch", url, "--wait", "15", "--stealth", "-q"]
    cmd += ["--eval", eval_js] if eval_js else ["--dump", dump]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"obscura failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


# --------------------------------------------------------------------------
# xiaohongshu

_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})</script>", re.S)
# obscura equivalent of the regex below, for when the page stops server-rendering
XHS_EVAL = (
    "JSON.stringify(Object.values("
    "window.__INITIAL_STATE__.note.noteDetailMap)[0].note)"
)


def parse_xhs(html: str) -> dict:
    m = _STATE_RE.search(html)
    if not m:
        raise RuntimeError("__INITIAL_STATE__ not found (page markup changed?)")
    # the blob is JS, not JSON: bare `undefined` appears as a value
    raw = re.sub(r"(?<=[:\[,])undefined(?=[,\]\}])", "null", m.group(1))
    state = json.loads(raw)
    detail = (state.get("note") or {}).get("noteDetailMap") or {}
    notes = [v.get("note") for v in detail.values() if isinstance(v, dict) and v.get("note")]
    if not notes:
        # two different failures land here; the error code tells them apart.
        # search the whole page: a shortlink to a dead note redirects to
        # /explore, where the code shows up outside serverRequestInfo.
        if "-510001" in html or "无法展示" in html:
            raise RuntimeError(
                "note is empty and the server says 当前内容无法展示 (-510001) — "
                "the post itself is unavailable (deleted / under review / "
                "author-only). The link is fine; a headless browser fails the "
                "same way. Ask for the text or a screenshot instead."
            )
        raise RuntimeError(
            "noteDetailMap is empty and the server reported no error — the URL "
            "almost certainly lost its xsec_token. Share links must be passed "
            "through verbatim; the bare /explore/<id> form returns a 200 with "
            "no note in it."
        )
    note = notes[0]
    return {
        "platform": "xiaohongshu",
        "title": (note.get("title") or "").strip(),
        "text": (note.get("desc") or "").strip(),
        "author": ((note.get("user") or {}).get("nickname") or "").strip(),
        "time": note.get("time"),
        "tags": [t.get("name") for t in (note.get("tagList") or []) if t.get("name")],
        "images": [
            im.get("urlDefault") or im.get("url")
            for im in (note.get("imageList") or [])
            if im.get("urlDefault") or im.get("url")
        ],
    }


# --------------------------------------------------------------------------
# wechat

_WX_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]*)"')
_WX_AUTHOR_RE = re.compile(r'id="js_name"[^>]*>\s*([^<]+?)\s*<')


def parse_wechat(html: str) -> dict:
    start = html.find('id="js_content"')
    if start < 0:
        raise RuntimeError("#js_content not found (page markup changed?)")
    start = html.find(">", start) + 1  # skip the rest of the opening tag
    seg = html[start : start + 400_000]
    end = seg.find('id="content_bottom_area"')
    if end > 0:
        seg = seg[:end]
    seg = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", seg, flags=re.S)
    seg = re.sub(r"</(p|section|div|br|li|h[1-6]|tr)>", "\n", seg)
    seg = re.sub(r"<[^>]*$", "", seg)  # drop a tag the slice cut in half
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", seg))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

    tm = _WX_TITLE_RE.search(html)
    am = _WX_AUTHOR_RE.search(html)
    imgs = re.findall(r'data-src="(https://mmbiz\.qpic\.cn/[^"]+)"', html)
    return {
        "platform": "wechat",
        "title": htmllib.unescape(tm.group(1)) if tm else "",
        "text": text,
        "author": am.group(1) if am else "",
        "time": None,
        "tags": [],
        "images": list(dict.fromkeys(imgs)),
    }


# --------------------------------------------------------------------------
# OCR


def ocr_images(urls: list[str], keep: Path | None = None) -> list[str]:
    """Download each image and OCR it. ~6s per image on CPU.

    Screenshots of notes/docs (which is what interview posts are) come out
    near-perfect; expect stray numbering and spacing that the LLM step fixes.
    """
    if not urls:
        return []
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    out = []
    tmp = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="capture-ocr-"))
    tmp.mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(urls):
        try:
            data = http_get(url, binary=True)
            src = tmp / f"img{i}.bin"
            src.write_bytes(data)
            png = tmp / f"img{i}.png"
            Image.open(src).convert("RGB").save(png)
            res, _ = engine(str(png))
            out.append("\n".join(line[1] for line in (res or [])))
        except Exception as exc:  # one bad image must not sink the note
            out.append(f"(OCR failed: {type(exc).__name__}: {exc})")
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# --------------------------------------------------------------------------


def capture(url: str, ocr: bool, browser: bool, keep: Path | None) -> dict:
    if browser:
        return {
            "platform": "browser",
            "title": "",
            "text": browser_get(url),
            "author": "",
            "time": None,
            "tags": [],
            "images": [],
            "ocr": [],
        }
    if "xhslink" in url or "xiaohongshu.com" in url:
        note = parse_xhs(http_get(url))
    elif "mp.weixin.qq.com" in url:
        note = parse_wechat(http_get(url))
    else:
        raise SystemExit(
            "no dedicated parser for this host — rerun with --browser to go "
            "through obscura, or add a parser here"
        )
    note["ocr"] = ocr_images(note["images"], keep) if ocr else []
    note["url"] = url
    return note


def render(note: dict) -> str:
    lines = []
    if note.get("title"):
        lines.append(f"# {note['title']}")
    meta = [f"来源: {note.get('url', '')}"]
    if note.get("author"):
        meta.append(f"作者: {note['author']}")
    if note.get("tags"):
        meta.append("标签: " + " ".join(note["tags"]))
    lines.append("\n".join(f"- {m}" for m in meta))
    if note.get("text"):
        lines.append("## 正文\n\n" + note["text"])
    for i, block in enumerate(note.get("ocr") or []):
        lines.append(f"## 图 {i + 1}/{len(note['ocr'])} (OCR)\n\n{block}")
    if note.get("images") and not note.get("ocr"):
        lines.append(
            "## 图片（未 OCR，加 --ocr 重跑）\n\n"
            + "\n".join(f"- {u}" for u in note["images"])
        )
    return "\n\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--ocr", action="store_true", help="OCR the note's images")
    ap.add_argument("--browser", action="store_true", help="force the obscura path")
    ap.add_argument("--json", action="store_true", help="dump the raw dict")
    ap.add_argument("--keep-images", type=Path, help="keep downloads in this dir")
    args = ap.parse_args()

    note = capture(args.url, args.ocr, args.browser, args.keep_images)
    if args.json:
        json.dump(note, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(render(note))


if __name__ == "__main__":
    main()
