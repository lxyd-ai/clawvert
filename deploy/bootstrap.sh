#!/usr/bin/env bash
# Clawvert — New server bootstrap (one-time first-deploy setup).
# Mirrors clawmoku's bootstrap with these differences:
#   - ports 9201 / 9202 (avoid clash with clawddz @ 9101 / 9102 on same box)
#   - data dir at /var/lib/clawvert/  (DB lives at clawvert.db there;
#     officials/ subdir holds bot creds caches)
#   - bot processes run as systemd instances of clawvert-bot@.service
#   - frontend not yet deployed — nginx falls back to /protocol.md until
#     Phase C (Next.js) ships
#
# Prereqs on target server:
#   Ubuntu 22.04+, Python 3.11+, nginx, root SSH access
#
# Run from LOCAL machine:
#   GITHUB_REPO=https://github.com/lxyd-ai/clawvert.git \
#   PROD_HOST=<ip> \
#   PROD_PASSWORD=<ssh-password>            # or use ssh-agent / key auth
#   JWT_SECRET=<32-byte-hex>                # `openssl rand -hex 32`
#   OFFICIAL_BOT_KEY=<32-byte-hex>          # secret shared between API + bots
#   bash deploy/bootstrap.sh
#
# Or SSH to the server and run directly (set env vars inline).
#
# NOTE: secrets are never checked into git. Stash them in ./.env.deploy
# (gitignored) which this script sources if present.
set -euo pipefail

# When piped over SSH (`bash -s`), BASH_SOURCE is unset / the cwd may be
# random; only resolve REPO_ROOT and source .env.deploy when we have a real
# script path on disk. The SSH path passes everything explicitly via the
# preamble built below, so the remote side doesn't need .env.deploy.
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ -f "$REPO_ROOT/.env.deploy" ]; then
    set -a; . "$REPO_ROOT/.env.deploy"; set +a
  fi
fi

GITHUB_REPO="${GITHUB_REPO:-https://github.com/lxyd-ai/clawvert.git}"
REMOTE_DIR="${REMOTE_DIR:-/srv/clawvert}"
DATA_DIR="${DATA_DIR:-/var/lib/clawvert}"
DOMAIN_PRIMARY="${DOMAIN_PRIMARY:-spy.clawd.xin}"
DOMAIN_ALIAS="${DOMAIN_ALIAS:-clawvert.clawdchat.cn}"
API_PORT="${API_PORT:-9201}"
WEB_PORT="${WEB_PORT:-9202}"
CLAWDCHAT_URL="${CLAWDCHAT_URL:-https://clawdchat.cn}"
JWT_SECRET="${JWT_SECRET:-REPLACE_ME_WITH_32BYTES_HEX}"
OFFICIAL_BOT_KEY="${OFFICIAL_BOT_KEY:-}"
BOT_PERSONAS="${BOT_PERSONAS:-official-cautious-cat official-chatty-fox official-contrarian-owl}"

# ── If running locally, SSH to remote ─────────────────────────────────────────
if [[ -n "${PROD_HOST:-}" ]]; then
  echo "==> Bootstrapping remote $PROD_HOST via SSH"

  # Build the env preamble explicitly, single-quoting each value with proper
  # escape so shells that contain spaces / quotes (BOT_PERSONAS!) survive.
  # Avoids the previous `eval` trap that tokenised "a b c" into 3 commands.
  shellquote() {
    # POSIX-portable single-quoting: 'foo' → 'foo', "a'b" → 'a'\''b'
    printf "'%s'" "${1//\'/\'\\\'\'}"
  }
  preamble=""
  for var in GITHUB_REPO REMOTE_DIR DATA_DIR DOMAIN_PRIMARY DOMAIN_ALIAS \
             API_PORT WEB_PORT CLAWDCHAT_URL JWT_SECRET OFFICIAL_BOT_KEY \
             BOT_PERSONAS; do
    val="${!var-}"
    preamble+="export $var=$(shellquote "$val")"$'\n'
  done

  if [[ -n "${PROD_PASSWORD:-}" ]]; then
    if ! command -v sshpass >/dev/null 2>&1; then
      echo "ERROR: sshpass not installed; brew install sshpass" >&2
      exit 1
    fi
    SSH=(sshpass -p "$PROD_PASSWORD" ssh -o StrictHostKeyChecking=no "root@$PROD_HOST" "bash -s")
  else
    SSH=(ssh -o StrictHostKeyChecking=no "root@$PROD_HOST" "bash -s")
  fi

  { printf '%s' "$preamble"; cat "${BASH_SOURCE[0]}"; } | "${SSH[@]}"
  echo "==> Remote bootstrap complete."
  exit 0
fi

# ── Running on the server itself below ────────────────────────────────────────
echo "==> [1/9] Install system packages (Python 3.11, sqlite3, certbot, nginx)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 \
  nginx certbot python3-certbot-nginx git curl 2>/dev/null || true
python3 --version

echo "==> [2/9] Create clawvert system user"
if ! id clawvert &>/dev/null; then
  useradd -r -s /bin/bash -d "$REMOTE_DIR" clawvert
fi

echo "==> [3/9] Clone repository"
# git complains about "dubious ownership" when root touches a clawvert-owned
# repo (post-chown re-runs). Whitelist explicitly so re-bootstrap is idempotent.
git config --global --add safe.directory "$REMOTE_DIR" 2>/dev/null || true
if [[ -d "$REMOTE_DIR/.git" ]]; then
  echo "    repo already exists, pulling latest"
  sudo -u clawvert git -C "$REMOTE_DIR" pull --ff-only \
    || git -C "$REMOTE_DIR" pull --ff-only   # fallback if dir isn't yet chowned
else
  mkdir -p "$(dirname "$REMOTE_DIR")"
  git clone "$GITHUB_REPO" "$REMOTE_DIR"
fi

echo "==> [4/9] Create data / log directories"
mkdir -p "$DATA_DIR/officials" /var/log/clawvert /var/backups/clawvert
chown -R clawvert:clawvert "$REMOTE_DIR" "$DATA_DIR" /var/log/clawvert /var/backups/clawvert

echo "==> [5/9] Python venv + install backend"
# Create venv as the clawvert user too — otherwise root owns it and the
# subsequent `sudo -u clawvert pip install` hits Permission denied on
# site-packages (the original lesson from clawmoku 2026-04-22).
if [[ ! -d "$REMOTE_DIR/backend/.venv" ]]; then
  sudo -u clawvert python3 -m venv "$REMOTE_DIR/backend/.venv"
fi
sudo -u clawvert "$REMOTE_DIR/backend/.venv/bin/pip" install -e "$REMOTE_DIR/backend" --quiet

echo "==> [6/9] systemd service files + secrets drop-in"
cp "$REMOTE_DIR/deploy/clawvert-api.service"   /etc/systemd/system/clawvert-api.service
cp "$REMOTE_DIR/deploy/clawvert-bot@.service"  /etc/systemd/system/clawvert-bot@.service

# Update public base URL in API unit
sed -i "s|CLAWVERT_PUBLIC_BASE_URL=.*|CLAWVERT_PUBLIC_BASE_URL=https://$DOMAIN_PRIMARY\"|g" \
  /etc/systemd/system/clawvert-api.service

# API drop-in: secrets that don't belong in the repo
mkdir -p /etc/systemd/system/clawvert-api.service.d
cat > /etc/systemd/system/clawvert-api.service.d/auth.conf <<EOF
[Service]
Environment=CLAWVERT_JWT_SECRET=$JWT_SECRET
Environment=CLAWVERT_CLAWDCHAT_URL=$CLAWDCHAT_URL
Environment=CLAWVERT_SESSION_COOKIE_SECURE=true
Environment=CLAWVERT_SESSION_COOKIE_SAMESITE=lax
EOF
if [[ -n "$OFFICIAL_BOT_KEY" ]]; then
  cat >> /etc/systemd/system/clawvert-api.service.d/auth.conf <<EOF
Environment=CLAWVERT_OFFICIAL_BOT_ADMIN_KEY=$OFFICIAL_BOT_KEY
EOF
fi

# Bot drop-in: every bot instance needs the same admin key in env
if [[ -n "$OFFICIAL_BOT_KEY" ]]; then
  mkdir -p /etc/systemd/system/clawvert-bot@.service.d
  cat > /etc/systemd/system/clawvert-bot@.service.d/auth.conf <<EOF
[Service]
Environment=CLAWVERT_OFFICIAL_BOT_KEY=$OFFICIAL_BOT_KEY
EOF
fi

systemctl daemon-reload
systemctl enable clawvert-api

if [[ -n "$OFFICIAL_BOT_KEY" ]]; then
  echo "==> [6.5/9] Enable official bot personas: $BOT_PERSONAS"
  for persona in $BOT_PERSONAS; do
    systemctl enable "clawvert-bot@$persona.service"
  done
else
  echo "==> [6.5/9] OFFICIAL_BOT_KEY not provided → skipping bot enablement."
  echo "          (Re-run bootstrap with OFFICIAL_BOT_KEY=<hex> to add later.)"
fi

echo "==> [7/9] nginx config"
NGINX_CONF="/etc/nginx/sites-available/clawvert"
cat > "$NGINX_CONF" <<NGINX
server {
    listen 80;
    server_name $DOMAIN_PRIMARY $DOMAIN_ALIAS;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name $DOMAIN_PRIMARY $DOMAIN_ALIAS;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN_PRIMARY/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN_PRIMARY/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    access_log /var/log/nginx/clawvert.access.log;
    error_log  /var/log/nginx/clawvert.error.log;

    proxy_read_timeout 90s;
    proxy_send_timeout 90s;
    proxy_connect_timeout 10s;
    proxy_buffering off;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Host \$host;

    location /api/ {
        proxy_pass http://127.0.0.1:$API_PORT/api/;
    }
    location = /skill.md    { proxy_pass http://127.0.0.1:$API_PORT/skill.md; }
    location = /protocol.md { proxy_pass http://127.0.0.1:$API_PORT/protocol.md; }
    location = /healthz     { proxy_pass http://127.0.0.1:$API_PORT/healthz; }

    # Frontend not yet deployed — root falls back to the protocol doc until
    # Phase C ships the Next.js app on port $WEB_PORT. After that, swap to
    #   proxy_pass http://127.0.0.1:$WEB_PORT;
    location / {
        proxy_pass http://127.0.0.1:$API_PORT/protocol.md;
    }
}
NGINX

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/clawvert

echo "==> [8/9] TLS cert (Let's Encrypt; needs DNS pointing here)"
if [[ ! -f "/etc/letsencrypt/live/$DOMAIN_PRIMARY/fullchain.pem" ]]; then
  certbot certonly --nginx \
    -d "$DOMAIN_PRIMARY" \
    --non-interactive --agree-tos --register-unsafely-without-email \
    2>&1 || {
    echo "    !! certbot failed — DNS may not point here yet."
    echo "    Workaround: temporary self-signed so nginx can start, run"
    echo "      certbot --nginx -d $DOMAIN_PRIMARY -d $DOMAIN_ALIAS"
    echo "    once DNS resolves."
    mkdir -p "/etc/letsencrypt/live/$DOMAIN_PRIMARY"
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
      -keyout "/etc/letsencrypt/live/$DOMAIN_PRIMARY/privkey.pem" \
      -out    "/etc/letsencrypt/live/$DOMAIN_PRIMARY/fullchain.pem" \
      -subj   "/CN=$DOMAIN_PRIMARY" 2>/dev/null
  }
fi

nginx -t && systemctl reload nginx

echo "==> [9/9] Start services"
systemctl start clawvert-api
sleep 2
systemctl is-active clawvert-api && echo "    clawvert-api: up"
if [[ -n "$OFFICIAL_BOT_KEY" ]]; then
  for persona in $BOT_PERSONAS; do
    systemctl start "clawvert-bot@$persona.service"
    systemctl is-active "clawvert-bot@$persona.service" \
      && echo "    clawvert-bot@$persona: up"
  done
fi

echo
echo "==> Bootstrap complete on $(hostname). Smoke checks:"
echo "    curl -sS https://$DOMAIN_PRIMARY/healthz"
echo "    curl -sS https://$DOMAIN_PRIMARY/skill.md | head -5"
echo "    journalctl -u clawvert-api -f"
echo "    journalctl -u 'clawvert-bot@*' -f"
