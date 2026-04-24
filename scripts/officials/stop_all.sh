#!/usr/bin/env bash
# Stop all running Clawvert official bots launched by start_all.sh.
set -e

PERSONAS=(
  "official-cautious-cat"
  "official-chatty-fox"
  "official-contrarian-owl"
)

for p in "${PERSONAS[@]}"; do
  pidfile="/tmp/clawvert-bot-${p}.pid"
  if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      echo "■ stopping ${p} (pid ${pid})"
      kill -TERM "$pid" || true
      # graceful first
      for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
      kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  else
    echo "  ${p}: no pidfile, skipping"
  fi
done

echo "all stopped."
