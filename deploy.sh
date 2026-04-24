#!/usr/bin/env bash
# Clawvert production deploy.
#
# Mirrors clawmoku/deploy.sh's safe-by-default playbook:
#   - Code lives at $REMOTE_DIR (default /srv/clawvert); rsync --delete
#     against it.
#   - The SQLite DB lives OUTSIDE the code tree at
#     /var/lib/clawvert/clawvert.db so rsync can never touch it.
#     A systemd Environment line points CLAWVERT_DATABASE_URL there.
#   - Bot creds caches live alongside the DB at /var/lib/clawvert/officials/
#     for the same reason.
#   - Predeploy DB snapshot to /var/backups/clawvert/ as a cheap rollback
#     anchor (cron should keep nightly snapshots independently).
#
# Credentials:
#   The SSH password is NOT checked into git. Provide it via one of:
#     * env var CLAWVERT_PROD_PASSWORD
#     * a line `CLAWVERT_PROD_PASSWORD=...` in ./.env.deploy (gitignored)
#     * an ssh-agent identity that can log into root@HOST
#
# Usage:
#   bash deploy.sh                # full: snapshot + rsync + reinstall + smoke
#   bash deploy.sh snapshot       # just snapshot the DB
#   bash deploy.sh smoke          # just verify endpoints
#   bash deploy.sh backups        # list recent server-side DB backups
#   bash deploy.sh restart-bots   # restart all clawvert-bot@*.service
#
# DO NOT run as `bash deploy.sh | tail -N` — pipeline buffering will
# hide build progress and make a stuck deploy look healthy.
#
# Env overrides (all optional):
#   CLAWVERT_PROD_HOST=<ip>
#   CLAWVERT_PROD_DIR=/srv/clawvert
#   CLAWVERT_PUBLIC_URL=https://spy.clawd.xin
#   CLAWVERT_DB_PATH=/var/lib/clawvert/clawvert.db
#   CLAWVERT_BACKUP_DIR=/var/backups/clawvert
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [ -f .env.deploy ]; then
  set -a; . ./.env.deploy; set +a
fi

HOST="${CLAWVERT_PROD_HOST:-}"
REMOTE_DIR="${CLAWVERT_PROD_DIR:-/srv/clawvert}"
PUBLIC_URL="${CLAWVERT_PUBLIC_URL:-https://spy.clawd.xin}"
DB_PATH="${CLAWVERT_DB_PATH:-/var/lib/clawvert/clawvert.db}"
BACKUP_DIR="${CLAWVERT_BACKUP_DIR:-/var/backups/clawvert}"
PASSWORD="${CLAWVERT_PROD_PASSWORD:-}"

if [ -z "$HOST" ]; then
  echo "ERROR: CLAWVERT_PROD_HOST is not set (export it or write to .env.deploy)" >&2
  exit 2
fi

# ---------- ssh wrapper: use sshpass if password given, else plain ssh ----
SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=8
)
if [ -n "$PASSWORD" ]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    echo "sshpass not installed but CLAWVERT_PROD_PASSWORD is set." >&2
    echo "Install via: brew install sshpass" >&2
    exit 1
  fi
  _ssh()   { sshpass -p "$PASSWORD" ssh   "${SSH_OPTS[@]}" "$@"; }
  _rsync() { sshpass -p "$PASSWORD" rsync "$@"; }
else
  echo "Note: CLAWVERT_PROD_PASSWORD unset; assuming ssh-agent / key-based auth."
  _ssh()   { ssh   "${SSH_OPTS[@]}" "$@"; }
  _rsync() { rsync "$@"; }
fi
rssh() { _ssh "root@$HOST" "$@"; }

# ---------- steps ----------
snapshot_db() {
  local stamp="$1"
  echo "==> [snapshot] best-effort predeploy DB snapshot"
  if rssh "test -f $DB_PATH"; then
    rssh "sudo -u clawvert sqlite3 $DB_PATH \".backup '$BACKUP_DIR/clawvert-predeploy-$stamp.db'\" && \
      chown clawvert:clawvert $BACKUP_DIR/clawvert-predeploy-$stamp.db && \
      sz=\$(du -h $BACKUP_DIR/clawvert-predeploy-$stamp.db | cut -f1) && \
      echo \"    ok: $BACKUP_DIR/clawvert-predeploy-$stamp.db (\$sz)\"" \
    || echo "    !! snapshot failed (non-fatal); continuing deploy"
  else
    echo "    no DB at $DB_PATH (first deploy?); skipping snapshot"
  fi
}

rsync_code() {
  echo "==> [rsync] $REPO_ROOT → $HOST:$REMOTE_DIR"
  echo "            (code tree only; DB at $DB_PATH is outside and untouchable)"
  _rsync -az --delete \
    --exclude='data/' \
    --exclude='backups/' \
    --exclude='backend/.venv/' \
    --exclude='backend/clawvert_backend.egg-info/' \
    --exclude='**/__pycache__/' \
    --exclude='**/.pytest_cache/' \
    --exclude='**/*.egg-info/' \
    --exclude='web/node_modules/' \
    --exclude='web/.next/' \
    --exclude='.git/' \
    --exclude='.env' \
    --exclude='.env.*' \
    -e "ssh -o StrictHostKeyChecking=no" \
    ./ "root@$HOST:$REMOTE_DIR/"
}

remote_install() {
  # Backend-only refresh today: re-run pip install -e on the venv, then
  # restart api + bot services. Symmetrical to clawmoku's remote_build
  # for the API half; the frontend half waits for Phase C.
  echo "==> [install] backend pip install + restart"
  rssh "REMOTE_DIR='$REMOTE_DIR' bash -s" <<'REMOTE_SH'
set -euo pipefail
: "${REMOTE_DIR:?REMOTE_DIR unset}"

STEP() { printf '  • [%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

STEP "chown code tree"
chown -R clawvert:clawvert "$REMOTE_DIR"

STEP "backend: pip install -e (safe — running process restarts at end)"
sudo -u clawvert "$REMOTE_DIR/backend/.venv/bin/pip" install -e "$REMOTE_DIR/backend" --quiet

STEP "backend: pytest sanity (skip if pytest not installed in venv)"
if "$REMOTE_DIR/backend/.venv/bin/python" -c "import pytest" 2>/dev/null; then
  sudo -u clawvert env CLAWVERT_DATABASE_URL=sqlite+aiosqlite:////tmp/clawvert-deploy-test.db \
    "$REMOTE_DIR/backend/.venv/bin/pytest" -q "$REMOTE_DIR/backend/tests" \
    || { echo "  !! pytest failed; aborting deploy"; exit 1; }
  rm -f /tmp/clawvert-deploy-test.db
fi

STEP "restart: clawvert-api (only API downtime, ~2s)"
systemctl restart clawvert-api
sleep 2
systemctl is-active clawvert-api

STEP "restart: clawvert-bot@*.service (rolling, one at a time)"
mapfile -t BOTS < <(systemctl list-units 'clawvert-bot@*.service' \
                    --no-legend --no-pager --plain | awk '{print $1}')
for b in "${BOTS[@]}"; do
  if [ -n "$b" ]; then
    systemctl restart "$b" || true
    sleep 1
  fi
done
REMOTE_SH
}

smoke() {
  echo "==> [smoke] verify endpoints at $PUBLIC_URL"
  local fail=0
  if curl -fsS "$PUBLIC_URL/healthz" > /tmp/clawvert-healthz.json; then
    echo "    healthz OK  $(cat /tmp/clawvert-healthz.json)"
  else
    echo "    !! healthz FAILED"; fail=1
  fi
  # /skill.md should start with the markdown banner ("<!-- clawvert:doc-rewrite ...")
  # OR a "# " heading once we hit it from the public URL.
  if curl -fsS -o /tmp/clawvert-skill.md "$PUBLIC_URL/skill.md" \
     && head -3 /tmp/clawvert-skill.md | grep -qE '^(<!--|# )'; then
    echo "    /skill.md returns markdown ($(wc -l < /tmp/clawvert-skill.md) lines)"
  else
    echo "    !! /skill.md did not return markdown"; fail=1
  fi
  if curl -fsS -o /tmp/clawvert-protocol.md "$PUBLIC_URL/protocol.md" \
     && head -3 /tmp/clawvert-protocol.md | grep -qE '^(<!--|# )'; then
    echo "    /protocol.md returns markdown"
  else
    echo "    !! /protocol.md did not return markdown"; fail=1
  fi
  return $fail
}

list_backups() {
  echo "==> [backups] recent DB backups on $HOST:$BACKUP_DIR"
  rssh "ls -lht $BACKUP_DIR 2>/dev/null | head -20"
}

restart_bots() {
  echo "==> [restart-bots] rolling restart of clawvert-bot@*.service"
  rssh 'mapfile -t BOTS < <(systemctl list-units "clawvert-bot@*.service" \
        --no-legend --no-pager --plain | awk "{print \$1}");
        for b in "${BOTS[@]}"; do
          if [ -n "$b" ]; then
            echo "  • restarting $b"
            systemctl restart "$b" || true
            sleep 1
          fi
        done'
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CMD="${1:-deploy}"

case "$CMD" in
  snapshot)     snapshot_db "$STAMP" ;;
  smoke)        smoke ;;
  backups)      list_backups ;;
  restart-bots) restart_bots ;;
  deploy)
    snapshot_db "$STAMP"
    rsync_code
    remote_install
    smoke
    echo
    echo "==> Done. Predeploy snapshot: $BACKUP_DIR/clawvert-predeploy-$STAMP.db"
    ;;
  *)
    echo "Unknown subcommand: $CMD" >&2
    echo "Usage: bash deploy.sh [deploy|snapshot|smoke|backups|restart-bots]" >&2
    exit 2
    ;;
esac
