---
name: wiki-knowledge-base
description: >-
  Plug into an llm-wiki style personal knowledge base (a git repo or local
  directory) and retrieve knowledge through its category index → page → source
  hierarchy, or by ranked search across its two layers, tracing pages back to
  archived originals when evidence is
  needed. Use this skill whenever the user shares a git URL or local path to a
  wiki knowledge base, says "check my wiki / knowledge base", asks a question
  that should be answered from their personal knowledge base, or mentions
  llm-wiki, wikilinks, or a knowledge repo. Even if the user just drops a repo
  containing index.md + pages/ + sources/ without explaining it, start from
  this skill's retrieval path instead of grepping the whole tree.
---

# wiki-knowledge-base: navigating an llm-wiki knowledge base

This kind of knowledge base is a git repo (design derived from Karpathy's
llm-wiki): machines archive raw articles (sources), an LLM compiles the
durable knowledge into curated pages, and the index is maintained by code.
Treat it as trusted external memory to retrieve from — not as a pile of text
to grep.

## Step 1: get and refresh the repo

- Given a git URL: clone it locally (a shallow clone is enough,
  `git clone --depth 1`). If cloning a private repo fails, report the error
  verbatim and ask the user for an accessible URL or a local path.
- Given a local path: use it directly, but **run `git pull --ff-only` before
  every use of this skill** — the knowledge base grows daily, and skipping
  the pull means answering from stale knowledge. If the pull fails (offline,
  conflict), don't block retrieval: continue with the local copy and tell the
  user the data may be behind.
- The repo is read-only for you. Never modify or commit anything — pages are
  maintained by the knowledge base's own compile pipeline.

## Repo layout

```
index.md              catalog of the whole wiki, grouped by category
index/<category>.md   the same catalog sliced per category — read these, not index.md
pages/*.md            knowledge pages (Simplified Chinese), cross-linked with [[wikilinks]]
sources/YYYY/MM/*.md  archived originals (immutable), with summary and full text
log.md                append-only compile log (rarely worth reading)
```

- `index.md` sections look like `## agent-engineering`; each entry is
  `- [[page-stem]] `tag1` `tag2` — one-line description`. `index/` holds one
  file per category with exactly those entries, plus that category's tag
  vocabulary in its header.
- Page frontmatter: `description` (one-line summary), `category` (single
  value), `tags` (2-4, drawn from one small shared vocabulary), `updated`.
- Source frontmatter: `title` / `source` / `url` (the original web page) /
  `date` / `compiled`, plus `pages:` — which pages this source was compiled
  into (the reverse link).

## Retrieval path: category index → page → source

Drill down level by level, reading only what each level requires, to keep
context usage bounded:

1. **Pick a category, then read only its index.** `ls index/` lists the
   categories — the names are self-describing. Read `index/<category>.md` for
   the 1-2 categories the question falls under, and shortlist candidate pages
   by their tags and descriptions. That file's header lists the tag vocabulary
   actually in use for the category; never guess tag names out of thin air.
   Do not read the whole `index.md` — it is every category at once and the
   irrelevant ones only dilute attention. (Older wikis may have no `index/`
   directory; then fall back to reading the matching `## <category>` section
   out of `index.md`.) If no category obviously fits the question, skip ahead
   to the search section below instead of reading everything.
2. **Read the candidate pages** at `pages/<stem>.md`. Pages are the distilled
   knowledge itself; most questions are answered at this level. In page
   bodies, `[[some-page]]` points to `pages/some-page.md` — follow it when
   you need to expand. Wikilinks marked `（未建）` ("not yet created") or
   pointing at missing files are intentional placeholders for pages worth
   creating later; they are not broken data, so don't report them as errors.
3. **Drill into sources only when you need original evidence.** The
   `## 来源` ("Sources") section at the end of a page lists the archived
   sources it was compiled from, as links like `../sources/2026/07/xxx.md`.
   When you need original details, exact quotes, data provenance, or suspect
   the page's paraphrase drifted, open the source and read its `## 原文`
   (full text); the `url` in the source's frontmatter is the final citation
   to give the user. In the other direction, a source's `pages:` field lists
   every page it was compiled into. Note: the sources section and `pages:`
   reverse links are recent compile outputs — some older pages/sources may
   lack them; when missing, fall back to
   `grep -rl <page name or keyword> sources/`.

## Approximate search with qmd

Descriptions in the index are terse, so a question phrased in the user's own
words often matches no page name. Rather than inferring from `index.md`, search
for it. [qmd](https://github.com/tobi/qmd) is a local markdown search engine
(BM25 + vectors + optional local reranking) and fits this wiki well.

**Use the `wiki-search` script that sits next to this file.** It is
self-contained (python3 + qmd, no other dependencies), locates the wiki
relative to itself, and registers/re-points the qmd collections on every run —
which matters, because a fresh `git clone` lands at a new path and would
otherwise leave qmd indexing a directory that no longer exists.

```bash
npm install -g @tobilu/qmd                  # once per machine
<wiki>/skill/wiki-knowledge-base/wiki-search "沙箱 逃逸"
<wiki>/skill/wiki-knowledge-base/wiki-search "怎么隔离代理" --semantic
<wiki>/skill/wiki-knowledge-base/wiki-search "TOCTOU" --deep -n 10
```

It prints the page layer first, then the archive layer when the pages come up
short, and tells you which pages a source was compiled into. Read what it
points at; do not stop at the search output.

**You can just ask it your question.** The default keyword mode is BM25, which
is purely literal — it finds only wording that actually occurs in the wiki
(`成本治理` hits; the synonymous `控制花销` does not), so a question phrased in
your own words matches nothing. The script detects that zero-hit case and
retries semantically on its own, telling you on stderr that it did. Keyword
alone answers in about a second; the semantic retry costs a few seconds more.

Reach for the flags when you already know better: `--semantic` up front if you
are certain the wiki will not use your words, `--hybrid` for LLM reranking
(best quality, slow on CPU) when both of the others miss. Semantic modes need
`qmd embed` to have been run once (~300MB model; the pass over a grown wiki
takes a while, and it is fine to run it repeatedly — it resumes).

### What the script does, if you have to do it by hand

Index the two layers as two collections, **never as one**. `sources/` is
several times the volume of `pages/` and restates the same material nearly
verbatim, so one merged ranking buries the curated pages under raw archive
text. Keeping them apart lets you search the page layer first and escalate
deliberately:

```bash
qmd collection add <wiki>/pages   --name wiki-pages
qmd collection add <wiki>/sources --name wiki-sources
qmd update    # after every git pull
```

1. **Search pages.** `qmd search "<terms>" -c wiki-pages -n 5 --format md`.
   BM25 is literal and ANDs its terms, so this only works with wording the
   wiki itself uses — 1-3 distinctive nouns, not a question. On zero hits,
   redo it with `qmd vsearch` before concluding anything.
2. **Escalate to sources only when pages come back empty or weak** (top score
   below ~0.5, i.e. the words matched but the topic didn't): rerun with
   `-c wiki-sources`.
3. **Climb back up from a source hit.** Read the hit's frontmatter: `pages:`
   names the pages it was compiled into — go read those, they are the better
   answer. If it says `compiled: false`, the source is still backlog and no
   page covers it yet; answer from the source itself and say so.
4. **Re-enter the hierarchy at any hit.** Open the page, then read
   `index/<its category>.md` to find its siblings.

Other qmd flags worth knowing: `--format md|json|files`, `--full` for the whole
document, `--min-score <num>` to cut noise.

Without qmd installed, fall back to grep: `grep -ril <keyword> pages/`, then
`sources/` — uncompiled or unadopted originals live there. If neither layer
has it, say so plainly instead of making something up.

## When answering

- Base answers on pages (the curated distillation); use sources for
  supporting evidence.
- Cite to a traceable location: the page file name, plus the source's `url`
  when needed.
- The `updated` field in page frontmatter is the last compile date — mention
  it when answering time-sensitive questions.
