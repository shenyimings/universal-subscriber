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

Set it up once per machine (`<wiki>` = the wiki directory):

```bash
npm install -g @tobilu/qmd
qmd collection add <wiki>/pages   --name wiki-pages
qmd collection add <wiki>/sources --name wiki-sources
```

`qmd update` re-indexes after a `git pull` — cheap, run it whenever you pull.
`qmd embed` downloads a ~300MB embedding model and is only needed for the
semantic modes below; BM25 works without it.

**Index the two layers as two collections, never as one.** `sources/` is
several times the volume of `pages/` and restates the same material nearly
verbatim, so one merged ranking buries the curated pages under raw archive
text. Keeping them apart lets you search the page layer first and escalate
deliberately:

1. **Search pages.** `qmd search "<terms>" -c wiki-pages -n 5 --format md`
2. **Escalate to sources only when pages come back empty or weak** (top score
   below ~0.5, i.e. the words matched but the topic didn't): rerun with
   `-c wiki-sources`.
3. **Climb back up from a source hit.** Read the hit's frontmatter: `pages:`
   names the pages it was compiled into — go read those, they are the better
   answer. If it says `compiled: false`, the source is still backlog and no
   page covers it yet; answer from the source itself and say so.
4. **Re-enter the hierarchy at any hit.** Open the page, then read
   `index/<its category>.md` to find its siblings.

Which mode to use:

- `qmd search` — BM25, no model, instant. Terms are ANDed, so keep the query
  to 1-3 distinctive words; long natural-language queries return nothing.
  This is the default choice.
- `qmd vsearch` — vector similarity. Use when the question's wording will not
  appear literally: paraphrases, an English question against Chinese pages,
  or a concept with several common names. Requires `qmd embed`.
- `qmd query` — hybrid retrieval with LLM reranking. Highest quality, but runs
  local models and is slow on CPU. Reach for it only when the other two miss.

Useful flags: `-n <num>` results, `--format md|json|files`, `--full` for the
whole document, `--min-score <num>` to cut noise.

If the wiki's own pipeline repo (universal-subscriber) is at hand, that whole
escalation is one command: `uv run subscriber search "<terms>"`, with
`--mode semantic|hybrid`, `--deep` to always include sources, and
`--setup` to register the two collections.

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
