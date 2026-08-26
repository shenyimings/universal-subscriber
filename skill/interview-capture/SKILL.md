---
name: interview-capture
description: Capture an interview-experience post (小红书 / 微信公众号 / anything else) that the user forwarded, OCR the screenshots it hides its questions in, and fold the questions into the per-岗位 files under wiki/interview/. Trigger whenever the user hands over a 面经 link — an xhslink.cn or xiaohongshu.com share URL, an mp.weixin.qq.com article, a screenshot of one — and asks to record, consolidate, 沉淀, or 归档 it.
---

# interview-capture

Interview posts are worth keeping but arrive in the worst possible packaging:
小红书 authors routinely put the whole question list inside screenshots and
leave the text body as a one-line sigh, and 公众号 articles defeat the usual
article extractor. This skill turns such a link into clean text and folds it
into the interview bank under `wiki/interview/`.

The whole pipeline is one script plus a hand edit. Do not build anything new
before reading the "Measured facts" section — several obvious approaches are
already known to fail.

This skill ships in two places: `skill/interview-capture/` in the subscriber
repo, and a byte copy at `skill/interview-capture/` inside the wiki repo (edit
one, `cp -rp` the pair, same rule as `wiki-knowledge-base`). Paths below are
written from the subscriber repo; from a bare wiki clone, drop the `wiki/`
prefix — `wiki/interview/` is just `interview/` there.

## Pipeline

1. **Capture** the link with `capture.py` (below). Read the output yourself.
2. **Classify**: which 岗位 (which file), which 批次 (实习 / 校招 / 社招), which
   questions are really 手撕 code problems.
3. **Dedupe against what is already there** — read the target 岗位 file first,
   before writing anything. See the rules below.
4. **Merge** the genuinely new questions in, following the structure rules.
   Never blind-append.
5. **Build** with hugo, then commit inside the wiki repo.

## 1. Capture

`capture.py` sits next to this file. **Just run it — do not read it first.**
It is a PEP 723 script: `uv run` installs its own dependencies (~120MB of OCR
wheels on first run, cached after). Everything you need to drive it is in this
section; open the source only if a run actually errors in a way the flags
below don't explain.

```bash
uv run skill/interview-capture/capture.py <url> --ocr
```

For a 小红书 link, pass `--ocr` unconditionally — the questions live in the
screenshots, and you cannot tell from the URL whether a post has them.
`xhslink.cn` shortlinks work as-is; you do **not** need to resolve them or
find an `xsec_token` yourself (see Measured facts).

| flag | effect |
|---|---|
| `--ocr` | download `imageList` and OCR every image (~6s each). **Always pass this for 小红书** |
| `--browser` | force the obscura path — use when a parser errors out |
| `--json` | raw dict instead of markdown, for scripting |
| `--keep-images DIR` | keep the downloads so you can eyeball a bad OCR |

Routes it picks automatically:

| host | method | time |
|---|---|---|
| `xhslink.cn` / `xiaohongshu.com` | `window.__INITIAL_STATE__` JSON in the served HTML | 1.3s + 6s/image |
| `mp.weixin.qq.com` | slice the `#js_content` container, strip tags | 2.5s |
| anything else | `--browser` → obscura `--dump markdown` | ~25s |

If OCR output looks mangled, look at the image yourself before hand-typing
anything — you can read screenshots directly and RapidOCR is only the cheap
path, not the authority. `--keep-images /tmp/x` saves them, but note the
numbering: the markdown labels blocks `图 1/N`, `图 2/N` (1-indexed) while the
files are `img0.png`, `img1.png` (0-indexed), so 「图 2」 on screen is
`img1.png` on disk, and `img0.png` is often just a cover page. RapidOCR is
strong but not perfect — expect the occasional CJK swap (seen: 挖矿→挖款,
skill→skil, go 1.21→g01.21) that only a glance at the image catches.

A harmless stderr line — `onnxruntime ... GetGpuDevices Failed to open
"/sys/class/drm/card0/..."` — prints on every run. It's RapidOCR probing for a
GPU on a CPU box; OCR completes normally. Ignore it.

## 2. Measured facts (2026-07-30, don't re-litigate these)

- **The share link carries auth — pass it through verbatim, whatever shape it
  is.** Forwarded 小红书 links come in two shapes and both work untouched:
  - a short `http://xhslink.cn/o/XXXX` link — **no token visible**; curl_cffi
    follows the redirect and the token gets attached server-side. Do not try
    to "find" or "add" an `xsec_token` — there isn't one to add, and the
    shortlink alone resolves correctly.
  - a long `xiaohongshu.com/.../item/<id>?xsec_token=...&xsec_source=app_share`
    URL — here the token is in your hand; keep it.

  What fails is a link with the token *removed*: the bare `/explore/<id>` or
  `/discovery/item/<id>` still returns HTTP 200 with a valid-looking
  `__INITIAL_STATE__`, but `noteDetailMap` is empty — a *silent* miss, and a
  real headless browser fails identically (platform auth, not a scraping
  problem). So the one rule is: never normalize, shorten, or truncate a
  forwarded link. `capture.py` raises a clear error if it hits an empty
  `noteDetailMap`, so you'll know rather than getting silent garbage.
- **trafilatura is unusable on 公众号.** On the test article it returned 319
  of 3094 characters and 1 of 8 questions. `favor_recall`, `favor_precision`
  and `include_tables` all return the identical 319 chars; the body *is*
  server-rendered (so it isn't a JS problem), trafilatura just loses it in
  微信's nested `<section>` inline-style soup. The `#js_content` slice in
  `capture.py` gets 8/8.
- **小红书 anti-bot is a non-issue on this path.** `curl_cffi` with
  `impersonate="chrome"` returns 200 with no cookie and no captcha; five rapid
  repeats all succeeded. Images on `sns-webpic-qc.xhscdn.com` download with no
  Referer check and no signature. Re-verified 2026-08-07: three older share
  links still capture fine from the same box on the same day a fourth one
  fails, so a failure is never "小红书 banned us" — check the note first.
- **An empty note is not always a token problem (2026-08-07).** Two distinct
  failures produce an empty `noteDetailMap`, and `capture.py` now separates
  them by reading `serverRequestInfo`:
  - no server error → the link really did lose its `xsec_token`.
  - `errorCode: -510001` / `当前内容无法展示` → **the post is gone**（deleted,
    under review, or author-only）. The shortlink redirect gives it away even
    earlier: it lands on `/explore` with no note id in the path and carries
    `target_note_id=<id>&undertake_note_error=该内容暂时无法查看`. Rebuilding a
    `/discovery/item/<id>?xsec_token=…` URL from those params does not help,
    and obscura renders only the footer. Nothing to debug — ask the user to
    re-share, paste the text, or send screenshots (you can read those
    directly).
- **OCR does not need a vision model.** These are screenshots of note apps —
  rendered text, not photographs — and RapidOCR (CPU, offline, free) gets them
  near-perfectly: 14/14 questions on the test image in 5.9s, losing only a
  list number and some spacing. This matters because DeepSeek, the backend
  this repo uses, has no vision API at all; going VLM means onboarding a
  second vendor for no gain.
- **`--dump assets` does not find 小红书's images.** The carousel renders them
  as CSS `background-image`, so only one thumbnail shows up as a real asset.
  The full-resolution `nd_dft` URLs only exist in `__INITIAL_STATE__`.

## 3. Obscura (the fallback)

Obscura is a Rust headless browser (~70MB binary, ~30MB RAM, instant boot)
serving a Chrome DevTools Protocol port. It is installed at
`~/.local/bin/obscura`. Use it when a parser above breaks — it survives markup
changes because it renders like a browser — and accept the ~25s cost.

```bash
obscura fetch <url> --dump markdown --wait 15 --stealth -q
```

- `--dump text | html | markdown | links | assets | cookies`
- `--dump original` streams the raw body, binary-safe (images, JSON)
- `--eval '<js>'` runs JS in the page and prints the result — **this is the
  useful one for SPAs**; it reaches state the DOM never shows:

```bash
obscura fetch <url> --wait 15 --stealth -q --eval \
  'JSON.stringify(Object.values(window.__INITIAL_STATE__.note.noteDetailMap)[0].note)'
```

`--stealth` gives a consistent cross-layer fingerprint (TLS ClientHello,
User-Agent, `navigator`, WebGL all agreeing on one Chrome identity), masks
`navigator.webdriver`, patches native functions against
`Function.prototype.toString` probes, and blocks 3,520 tracker domains. It
clears Cloudflare's basic JS challenge, Turnstile non-interactive, Akamai BMP,
PerimeterX and DataDome. It will **not** clear interactive CAPTCHAs, and
Obscura has no screenshot support (no rendering engine).

For heavier automation it also speaks CDP — `obscura serve --port 9222`, then
`chromium.connectOverCDP("ws://127.0.0.1:9222")` from Playwright or
`puppeteer.connect({browserWSEndpoint: "ws://127.0.0.1:9222/devtools/browser"})`.
Not needed for this skill's job.

Repo: https://github.com/h4ckf0r0day/obscura

## 4. Editing wiki/interview/

The bank is a directory, split by **岗位** so a reader loads one file, not the
whole thing:

| file | scope |
|---|---|
| `interview/_index.md` | the 目录 — table of the three files, 题量 counts, and the division criteria. No questions live here |
| `interview/security.md` | 传统安全岗：渗透、代码审计 / SDL、应急响应、攻防演练、安全研发，plus the **防护侧** of 大模型 / 智能体 (提示词注入防御、沙箱、大模型防火墙) |
| `interview/llm-algo.md` | 大模型算法岗：预训练与后训练 (SFT / DPO / RLHF / GRPO)、模型结构、多模态、数据构造与评测。**安全大模型 / 安全 Agent 的后训练也在这里**，不在 security |
| `interview/llm-engineering.md` | 大模型工程岗：Agent 框架与运行时、RAG 工程、上下文工程、推理服务，以及后端基础 (Go / 存储 / 中间件 / 网络与操作系统) |

Boundary calls, in this order:

- **做安全 vs 做模型**：拿大模型去解安全问题、给智能体加防护 → security；为安全
  场景训模型（数据构造、SFT、RL、评测指标）→ llm-algo。
- **训模型 vs 用模型**：改权重、设计 reward、调数据配比 → llm-algo；把模型接进
  系统、管上下文、扛并发与成本 → llm-engineering。

The whole directory is deliberately an **island**: not in `index.md`, not in
`pages/`, no `category`/`tags`, links to nothing and nothing links to it — it
is reachable only by knowing `/interview/`. Keep it that way:

- Do **not** add it to `index.md`, and do not add `[[wikilink]]`s pointing at
  it or out of it. `subscriber wiki --lint` never sees it because it lives at
  the wiki root rather than under `pages/`, and `--reindex` will not touch it.
- Do **not** give any of these files `category:` or `tags:` frontmatter —
  those would surface them on the taxonomy pages.
- Keep `build: {list: never}` plus the matching `cascade:` block in
  `_index.md`'s frontmatter — the cascade is what keeps the three 岗位 pages
  out of lists too (the key is `build`, not `_build` — Hugo removed the
  underscore form in 0.145 and errors out on it). They stay out of every list,
  out of the sitemap, and out of the homepage search index
  (`layouts/index.json` only indexes `Section == "pages"`), while still
  rendering at their own URLs.
- `wiki/layouts/interview/list.html` exists because the theme's
  `_default/list.html` renders a child listing, which is empty under
  `list: never`; the override renders `.Content` instead. Don't delete it or
  `/interview/` goes blank.

Structure inside a 岗位 file, fixed:

```
# <岗位>                             <- H1, once, matches the file
## 实习 / ## 校招 / ## 社招 / ## 手撕  <- H2, only these four, in this order
**<子方向>**                         <- optional bold separator, NOT a heading
### <问题>                           <- H3, one question, verbatim-ish
关键词：a、b、c                       <- one line of answer-shaped hints
```

The bold `**<子方向>**` line groups a 批次 that mixes two areas (e.g. `**Agent
工程**` vs `**后端基础**` inside 大模型工程岗). It must stay a bold paragraph,
never a heading — `grep '^###' <file>` enumerating exactly the questions is an
invariant the dedupe step relies on.

Rules:

- **Every** algorithm / LeetCode / 手撕代码 question goes under that file's
  `## 手撕`, regardless of which interview it came from. Leave it out of the
  批次 section. 手撕 lives inside the 岗位 file — there is no global 手撕 page.
- Each question gets a `关键词：` line — pointers that reconstruct an answer,
  not the answer itself. If the source post shipped a full answer (公众号 posts
  often do), compress it to keywords here; the full text stays in the source
  link.
- Record each post once as a `来源：` bullet list directly under its `## 批次`
  heading (or under the `**<子方向>**` line it belongs to), before the
  questions: company, 批次, and the link with its `xsec_token` intact. Keep it
  a bullet list — a `## 来源` heading would collide with the 批次 level.
- 批次 maps to the four H2s like this: 秋招 → 校招, 春招 → 校招, 暑期实习 /
  日常实习 → 实习, 社招 / 社会招聘 → 社招. If the post gives no 批次 evidence,
  put it in the most likely bucket and say so explicitly next to the source
  (`批次未标注，暂置于校招`) rather than guessing silently. 毕设 / 答辩 mentions
  point to 校招.
- Fix OCR debris (`skil` → `skill`, `挖款` → `挖矿`, split lines) while merging.
  Drop screenshot chrome: clock, battery, 字数统计, 未分类, 便签 app names.
- Keep the split coarse: three files, four 批次 each. Do not add a fourth 岗位
  file, and do not spin up a new `**子方向**` separator for a single post — a
  post titled 「AI Agent 应用开发」 goes into the existing `**Agent 工程**`
  group even if its questions lean more algorithm than systems. Same job
  family, same file, same group — in both the 批次 section and 手撕.
- After adding or removing questions, update the 题量 column in
  `interview/_index.md` (`grep -c '^### ' interview/<file>.md`).

### Dedupe before you write

The page grows by reuse, not by accumulation. A second post asking the same
thing is evidence the question is common — it is not a second entry.

Read the existing H3 list first — the target file at minimum, and
`grep -H '^###' wiki/interview/*.md` when the post straddles two 岗位 — then
for each captured question decide:

- **Already there, same file** — write nothing. At most, sharpen the existing
  `关键词：` line if the new post reveals an angle it misses, and add the new
  post to that 批次's `来源：` list.
- **Already there, different 批次 or different 岗位 file** — do not copy the
  H3. Add a pointer where you would have written it. Cross-file pointers carry
  the 岗位: `见「大模型工程岗 › 校招 › <原标题>」`; inside one file drop it:
  `见「校招 › <原标题>」`. One question, one authoritative entry.
- **A near-duplicate** (same concept, different wording — 「如何设计终止条件」
  vs 「怎么避免死循环」) — keep the existing heading, fold anything new into
  its 关键词 line. Prefer the phrasing that generalizes across companies.
- **A follow-up** to an existing question — new H3, placed adjacent to its
  parent so the chain reads in order.
- **Genuinely new** — new H3 in the right file, under the right 批次.

Matching is semantic, not string equality: OCR and different authors rarely
produce identical wording. When unsure whether two questions are the same,
they usually are — merge, and let the 关键词 line carry both angles.

## 5. Build and publish

The wiki is its own git repo, and the `interview/` directory is mounted as a
whole in `wiki/hugo.toml` (already added, next to `about.md`):

```toml
[[module.mounts]]
source = "interview"
target = "content/interview"
```

```bash
cd wiki && hugo --minify
```

Check that the island held. The first three must print `0`, the last must list
four files:

```bash
cd wiki && grep -c 面试题库 public/index.json; grep -c interview public/sitemap.xml; grep -c interview index.md; ls public/interview/index.html public/interview/*/index.html
```

Probe the search index with the **Chinese title**, not the word `interview`:
`grep -c interview public/index.json` returns `1` for an unrelated page that
cites an `interviewkickstart.com` URL in its body — a false positive, not a
leak. Same for `grep -rl interview public/pages/` →
`agent-skills/index.html`. Ignore both.

Also confirm the 目录 actually rendered — it is a section index, and a broken
layout override silently yields an empty page. Hugo minifies attributes, so
grep unquoted:

```bash
cd wiki && grep -o 'href=/interview/[a-z-]*/' public/interview/index.html
```

Expect all three 岗位 links.

Then commit **and push, inside the wiki repo** (never from the subscriber repo
— they are separate remotes; the wiki pushes to shenyimings/wiki-page, and
Vercel redeploys on push):

```bash
cd wiki && git add interview/ hugo.toml layouts/ && git commit -m "interview: <what>" && git push
```

Committing and pushing is the finish line, not an optional extra — the page
isn't published until it's pushed. Do it as the last step of every capture
run unless the user says otherwise.
