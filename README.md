# universal-subscriber

![coverage](https://img.shields.io/badge/coverage-77%25-yellow)

Watch the information sources you care about, let an LLM read them for you, and keep what matters — twice:

1. **Daily digest.** New articles and page changes are summarized (and translated into Chinese) by an LLM, filtered against your reader persona, and emailed to you once a day.
2. **Knowledge wiki.** Everything that survives the relevance filter is archived with its full text, then compiled into a curated markdown wiki — concept pages that accumulate and cross-reference knowledge over time, instead of throwing it away after one email.

The wiki design follows [Karpathy's llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern: raw sources are immutable, the LLM maintains the pages, and the bookkeeping (index, log, health checks) is done in plain code so it cannot drift.

## Usage

```bash
uv run subscriber run --dry-run                       # fetch all sources, print the digest, don't send email
uv run subscriber run                                 # fetch, archive, and send via AgentMail
uv run subscriber run --source ethresear.ch --dry-run # run a single source

uv run subscriber wiki                                # compile archived sources into wiki pages
uv run subscriber wiki --limit 10                     # compile at most 10 sources this run
uv run subscriber wiki --lint                         # health check: broken links, orphan pages, backlog
uv run subscriber wiki --lint --fix                   # LLM-assisted broken-link repair, then the health check
```

On the first run: RSS sources take the newest `max_items_per_source` entries and mark the rest as read; `page` sources record a baseline snapshot and only report a summary once the page changes.

To bootstrap the wiki from each source's history (bypassing the seen-state), there is a one-off backfill script:

```bash
uv run python scripts/backfill.py --max-items 15      # newest 15 items per source
```

## How the wiki works

```
wiki/
  sources/YYYY/MM/*.md   archived articles: frontmatter + summary + full text (immutable)
  pages/*.md             curated knowledge pages, cross-linked with [[wikilinks]]
  index.md               one line per page, grouped by category with tags, rebuilt from page frontmatter on every compile
  log.md                 append-only ingest log
```

`subscriber run` archives every article that passes the persona filter into `wiki/sources/` with `compiled: false`. `subscriber wiki` then feeds each pending source to the LLM twice: once to plan which pages should absorb it (update an existing page, or create a new one when a concept deserves its own entry) along with a category and tags for each page, and once per page to merge the new knowledge into the page content. Sources are marked `compiled: true` afterwards and never flow through the LLM again.

Pages belong to one of a fixed set of categories and carry a few tags from a shared vocabulary; both live in the frontmatter and the index, which is how the planning step learns them. When writing a page, the LLM only sees the index slice of that page's own category, so cross-references stay within a category instead of linking everything to everything.

Wikilinks pointing at pages that do not exist yet are allowed on purpose — they mark concepts worth writing up later. `subscriber wiki --lint --fix` keeps this from getting out of hand: it has the LLM redirect near-miss names to existing pages, degrade links not worth a page to plain text, and keep the genuinely valuable ones (marked as unbuilt) within a fixed share of all links. The text replacements are applied by code; the LLM only rules on each link.

The wiki is plain markdown with YAML frontmatter and `[[wikilinks]]`, so it opens directly in Obsidian, and any LLM agent pointed at the directory can answer questions from it — read `index.md` first, then follow links. Since it is just files, syncing it elsewhere is a `git push`: keep the wiki directory in a private repo and you can attach the same knowledge base to whatever tool you use, on any machine, without running a server.

Run `subscriber wiki --lint` occasionally. It reports broken wikilinks, pages nothing links to, and how many sources are waiting to be compiled — the failure mode to watch for is pages silently going stale, not the compiler crashing.

## Configuration

Copy the example files and fill in your own values:

```bash
cp .env.example .env
cp config.yaml.example config.yaml
cp prompts.yaml.example prompts.yaml
```

- `.env` — `DEEPSEEK_API_KEY`, `AGENTMAIL_API_KEY` (required for sending mail; generate at <https://agentmail.to>).
- `config.yaml` — LLM model, recipient email, list of sources (`type: rss` / `page` / `browser` / `inbox`; `browser` uses Obscura to render JS pages, `selector` can narrow what is watched). Optional `wiki.dir` overrides where the wiki lives (default `wiki/`).
- `prompts.yaml` — all prompts: digest summarization, the reader persona, and the wiki compiler. The persona steers both what gets kept in the digest and what enters the wiki — off-topic entries are dropped (the model outputs `SKIP`).
- `data/state.db` — SQLite state (already-seen items, page snapshots). Delete it to reset.

### Adding a new source

Look for an RSS endpoint first — many sites have one without linking it: Substack `/feed`, Medium `/feed/@user`, Discourse forums `/latest.rss`, GitHub Pages blogs `/index.xml` or `/feed/`. Fall back to `type: page` only when there is no feed.

## Scheduled runs (daily at 8am)

Configured via a systemd user timer (no cron on this box): `~/.config/systemd/user/subscriber.{service,timer}`, logs at `data/run.log`.

```bash
systemctl --user list-timers subscriber.timer   # next run time
systemctl --user start subscriber.service       # trigger once manually
systemctl --user disable --now subscriber.timer # stop
```

## Testing

```bash
uv run pytest                           # run all tests
uv run pytest --cov --cov-report=term-missing  # with coverage report
```

All LLM and network calls are mocked in tests. The coverage badge is updated automatically by CI on each push to main.

## Known limitations

- `rss` / `page` fetching uses curl_cffi to mimic a Chrome TLS fingerprint, which gets past most anti-bot checks. Sites that require JS rendering or challenges need `type: browser` (depends on `~/.local/bin/obscura`; on complex React pages Obscura's `--dump text` needs a `selector` to isolate the main content).
- The backfill script cannot reconstruct history for `browser` sources (no article list in a rendered text dump), and its link heuristic for `page` sources only finds articles nested under the listing page's own path.
- Wiki compilation is sequential and costs one to four LLM calls per source; use `--limit` to spread a large backlog over several runs.
