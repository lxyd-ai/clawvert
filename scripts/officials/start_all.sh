#!/usr/bin/env bash
# Start all three Clawvert official bots in the background.
# Each bot writes its log to /tmp/clawvert-bot-<name>.log; PID files
# go to /tmp/clawvert-bot-<name>.pid for `stop_all.sh`.
#
# Required env (in current shell):
#   CLAWVERT_OFFICIAL_BOT_KEY  — must equal backend's official_bot_admin_key
#   CLAWVERT_BASE_URL          — defaults to http://127.0.0.1:9101
#
# Convention: bots reuse the same backend venv (httpx already a dep).

set -e
cd "$(dirname "$0")/../.."

if [ -z "${CLAWVERT_OFFICIAL_BOT_KEY:-}" ]; then
  echo "ERROR: CLAWVERT_OFFICIAL_BOT_KEY env var required" >&2
  exit 2
fi

export CLAWVERT_BASE_URL="${CLAWVERT_BASE_URL:-http://127.0.0.1:9101}"
PYBIN="${PYBIN:-backend/.venv/bin/python}"
if [ ! -x "$PYBIN" ]; then
  echo "ERROR: $PYBIN not found; activate or repair the backend venv first" >&2
  exit 2
fi

PERSONAS=(
  "official-cautious-cat"
  "official-chatty-fox"
  "official-contrarian-owl"
)

for p in "${PERSONAS[@]}"; do
  pidfile="/tmp/clawvert-bot-${p}.pid"
  logfile="/tmp/clawvert-bot-${p}.log"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "✓ ${p} already running (pid $(cat "$pidfile"))"
    continue
  fi
  echo "▶ launching ${p} → ${logfile}"
  nohup "$PYBIN" -m scripts.officials.runner --persona "$p" \
    > "$logfile" 2>&1 &
  echo $! > "$pidfile"
done

sleep 1
echo
for p in "${PERSONAS[@]}"; do
  pidfile="/tmp/clawvert-bot-${p}.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "  ${p}: pid $(cat "$pidfile") (tail -f /tmp/clawvert-bot-${p}.log)"
  else
    echo "  ${p}: FAILED — see /tmp/clawvert-bot-${p}.log"
  fi
done
