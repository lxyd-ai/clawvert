#!/usr/bin/env bash
# Copy the live SQLite DB from one Clawvert server to another.
# Useful when migrating boxes or when bringing up a staging twin.
#
# Usage:
#   FROM_HOST=8.x.x.x  TO_HOST=8.y.y.y  bash deploy/migrate_db.sh
#
# What it does (in order):
#   1. SSH to FROM_HOST, run sqlite3 .backup → /tmp/clawvert-migrate.db
#      (online, doesn't pause clawvert-api)
#   2. scp the snapshot back to your local machine
#   3. scp it to TO_HOST:/var/lib/clawvert/clawvert.db.new
#   4. SSH to TO_HOST, stop clawvert-api + bots, atomic mv,
#      restart everything, sanity-check
#
# DOES NOT touch the OS user, venv, code tree — those are bootstrap.sh's job.
set -euo pipefail

if [ -z "${FROM_HOST:-}" ] || [ -z "${TO_HOST:-}" ]; then
  echo "ERROR: FROM_HOST and TO_HOST env vars required" >&2
  exit 2
fi
DB_PATH="${DB_PATH:-/var/lib/clawvert/clawvert.db}"
PASSWORD="${CLAWVERT_PROD_PASSWORD:-}"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ServerAliveInterval=15)
if [ -n "$PASSWORD" ]; then
  command -v sshpass >/dev/null 2>&1 || { echo "install sshpass first" >&2; exit 1; }
  _ssh() { sshpass -p "$PASSWORD" ssh "${SSH_OPTS[@]}" "$@"; }
  _scp() { sshpass -p "$PASSWORD" scp "${SSH_OPTS[@]}" "$@"; }
else
  _ssh() { ssh "${SSH_OPTS[@]}" "$@"; }
  _scp() { scp "${SSH_OPTS[@]}" "$@"; }
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL="/tmp/clawvert-migrate-$STAMP.db"

echo "==> [1/4] online snapshot on $FROM_HOST"
_ssh "root@$FROM_HOST" \
  "sudo -u clawvert sqlite3 $DB_PATH \".backup '/tmp/clawvert-migrate-$STAMP.db'\" && \
   chown clawvert:clawvert /tmp/clawvert-migrate-$STAMP.db && \
   du -h /tmp/clawvert-migrate-$STAMP.db"

echo "==> [2/4] scp snapshot back to local"
_scp "root@$FROM_HOST:/tmp/clawvert-migrate-$STAMP.db" "$LOCAL"
ls -lh "$LOCAL"

echo "==> [3/4] scp snapshot to $TO_HOST:$DB_PATH.new"
_scp "$LOCAL" "root@$TO_HOST:$DB_PATH.new"

echo "==> [4/4] atomic swap on $TO_HOST + restart"
_ssh "root@$TO_HOST" "
set -e
chown clawvert:clawvert $DB_PATH.new
systemctl stop 'clawvert-bot@*.service' || true
systemctl stop clawvert-api
[ -f $DB_PATH ] && mv $DB_PATH ${DB_PATH%.db}.pre-migrate-$STAMP.db
mv $DB_PATH.new $DB_PATH
systemctl start clawvert-api
sleep 2
systemctl is-active clawvert-api && echo '    api up'
mapfile -t BOTS < <(systemctl list-unit-files 'clawvert-bot@*.service' \
                    --no-legend --no-pager --state=enabled | awk '{print \$1}')
for b in \"\${BOTS[@]}\"; do
  systemctl start \"\$b\" || true
done
"

echo
echo "==> Done. Original DB on $TO_HOST archived to ${DB_PATH%.db}.pre-migrate-$STAMP.db"
echo "   Local copy retained at $LOCAL"
