---
name: wiki-knowledge-base
description: >-
  Plug into an llm-wiki style personal knowledge base (a git repo or local
  directory) and retrieve knowledge through its index → category/tag → page →
  source hierarchy, tracing pages back to archived originals when evidence is
  needed. Use this skill whenever the user shares a git URL or local path to a
  wiki knowledge base, says "check my wiki / knowledge base", asks a question
  that should be answered from their personal knowledge base, or mentions
  llm-wiki, wikilinks, or a knowledge repo. Even if the user just drops a repo
  containing index.md + pages/ + sources/ without explaining it, start from
  this skill's retrieval path instead of blind full-text search.
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
pages/*.md            knowledge pages (Simplified Chinese), cross-linked with [[wikilinks]]
sources/YYYY/MM/*.md  archived originals (immutable), with summary and full text
log.md                append-only compile log (rarely worth reading)
```

- `index.md` sections look like `## agent-engineering`; each entry is
  `- [[page-stem]] `tag1` `tag2` — one-line description`.
- Page frontmatter: `description` (one-line summary), `category` (single
  value), `tags` (2-4, drawn from one small shared vocabulary), `updated`.
- Source frontmatter: `title` / `source` / `url` (the original web page) /
  `date` / `compiled`, plus `pages:` — which pages this source was compiled
  into (the reverse link).

## Retrieval path: index → category/tag → page → source

Drill down level by level, reading only what each level requires, to keep
context usage bounded:

1. **Read index.md.** It is small; read it whole. Pick the 1-2 relevant
   category sections for the question, then shortlist candidate pages by
   their tags and descriptions. The tag vocabulary is learned here — never
   guess tag names out of thin air.
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

When the hierarchy misses (a new concept may not have been compiled into a
page yet), fall back to full-text search: `grep -ril <keyword> pages/`, and
if that also misses, grep `sources/` — uncompiled or unadopted originals
live there. If neither side has it, say so plainly instead of making
something up.

## When answering

- Base answers on pages (the curated distillation); use sources for
  supporting evidence.
- Cite to a traceable location: the page file name, plus the source's `url`
  when needed.
- The `updated` field in page frontmatter is the last compile date — mention
  it when answering time-sensitive questions.
