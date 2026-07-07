# subscriber

Watch information sources → have an LLM (DeepSeek) summarize and translate the updates → email you a daily digest.

## Usage

```bash
uv run subscriber run --dry-run                       # fetch all sources, print the digest, don't send email
uv run subscriber run                                 # fetch and send via AgentMail
uv run subscriber run --source ethresear.ch --dry-run # run a single source
```

On the first run: RSS sources take the newest `max_items_per_source` entries and mark the rest as read; `page` sources record a baseline snapshot and only report a summary once the page changes.

## Configuration

Copy the example files and fill in your own values:

```bash
cp .env.example .env
cp config.yaml.example config.yaml
cp prompts.yaml.example prompts.yaml
```

- `.env` — `DEEPSEEK_API_KEY`, `AGENTMAIL_API_KEY` (required for sending mail; generate at <https://agentmail.to>).
- `config.yaml` — LLM model, recipient email, list of sources (`type: rss` / `page` / `browser`; `browser` uses Obscura to render JS pages, `selector` can narrow what is watched).
- `prompts.yaml` — the summarization prompt and reader persona. Edit the persona to steer what gets kept vs. filtered — off-topic entries are dropped (the model outputs `SKIP`).
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

## Known limitations

- `rss` / `page` fetching uses curl_cffi to mimic a Chrome TLS fingerprint, which gets past most anti-bot checks. Sites that require JS rendering or challenges need `type: browser` (depends on `~/.local/bin/obscura`; on complex React pages Obscura's `--dump text` needs a `selector` to isolate the main content).
