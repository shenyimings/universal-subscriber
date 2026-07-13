#!/usr/bin/env bash
# Push the wiki (its own git repo inside wiki/, remote: shenyimings/wiki-page)
# to the private remote. Runs after the daily `subscriber wiki`; exits quietly
# when nothing changed.
set -euo pipefail

WIKI_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)/wiki}"
cd "$WIKI_DIR"

git add -A
if git diff --cached --quiet; then
    echo "[push_wiki] 无变更，跳过。" >&2
    exit 0
fi
git commit -q -m "wiki: auto update $(date +%F)"
git push -q origin main
echo "[push_wiki] 已推送 $(git rev-parse --short HEAD)。" >&2
