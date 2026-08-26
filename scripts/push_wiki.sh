#!/usr/bin/env bash
# Push the wiki (its own git repo inside wiki/, remote: shenyimings/wiki-page)
# to the private remote. Fetch + compile run daily, but the push is throttled:
# after each successful push the next one is scheduled 1-3 days out (random),
# so several days of updates land in a single commit. `--force` ignores the
# schedule. Exits quietly when nothing changed or when the push isn't due yet.
set -euo pipefail

FORCE=0
WIKI_DIR=""
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        *) WIKI_DIR="$arg" ;;
    esac
done

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WIKI_DIR="${WIKI_DIR:-$REPO_DIR/wiki}"
STATE_FILE="$REPO_DIR/data/push_due"

today=$(date +%F)
today_ts=$(date -d "$today" +%s)

if [[ $FORCE -eq 0 && -f $STATE_FILE ]]; then
    due=$(<"$STATE_FILE")
    if due_ts=$(date -d "$due" +%s 2>/dev/null) && (( today_ts < due_ts )); then
        echo "[push_wiki] 未到推送日（下次 $due），跳过。" >&2
        exit 0
    fi
fi

cd "$WIKI_DIR"

git add -A
if git diff --cached --quiet; then
    # 没有内容可推，不消耗本次窗口，明天再试。
    echo "[push_wiki] 无变更，跳过。" >&2
    exit 0
fi
git commit -q -m "wiki: auto update $(date +%F)"
git push -q origin main

gap=$(( RANDOM % 3 + 1 ))
next=$(date -d "$today + $gap days" +%F)
mkdir -p "$(dirname "$STATE_FILE")"
echo "$next" > "$STATE_FILE"
echo "[push_wiki] 已推送 $(git rev-parse --short HEAD)，下次推送不早于 $next（间隔 ${gap} 天）。" >&2
