#!/usr/bin/env bash
#
# scripts/demo_full_game.sh
# ─────────────────────────
# End-to-end smoke for clawvert API. Runs against a live uvicorn on
# 127.0.0.1:9201 (start it yourself in another terminal with:
#     cd backend && source .venv/bin/activate && uvicorn app.main:app --port 9201
# ).
#
# What it does:
#   1. Registers 4 fresh agents (random suffix so reruns don't collide)
#   2. Creates a 4-player / 1-undercover match
#   3. Joins the other 3 agents — auto-begins on full
#   4. Runs one full speak round
#   5. Reveals the undercover via god-view (replay), then has all 3
#      civilians vote them out
#   6. Prints the leaderboard delta
#
# Designed to be deterministic & idempotent: every run uses fresh names.
#
set -euo pipefail

BASE="${CLAWVERT_BASE:-http://127.0.0.1:9201}"
H='Content-Type: application/json'
SUFFIX="$(date +%s%N | tail -c 7)"  # unique per run
log()   { printf "\033[36m▸\033[0m %s\n" "$*"; }
ok()    { printf "\033[32m✓\033[0m %s\n" "$*"; }
fail()  { printf "\033[31m✗\033[0m %s\n" "$*"; exit 1; }
PY='python3'

# ── 0) Health probe ────────────────────────────────────────────────
log "ping $BASE/healthz"
HEALTH=$(curl -fsS "$BASE/healthz" || fail "API not reachable on $BASE")
ok "health: $HEALTH"

# ── 1) Register 4 agents ───────────────────────────────────────────
declare -a NAMES=("demo_a_$SUFFIX" "demo_b_$SUFFIX" "demo_c_$SUFFIX" "demo_d_$SUFFIX")
declare -a KEYS=()
for n in "${NAMES[@]}"; do
  RESP=$(curl -fsS -X POST "$BASE/api/agents" -H "$H" \
           -d "{\"name\":\"$n\",\"display_name\":\"$n\"}")
  KEY=$($PY -c "import sys,json;print(json.load(sys.stdin)['api_key'])" <<<"$RESP")
  KEYS+=("$KEY")
  ok "registered $n  key=${KEY:0:12}…"
done

# ── 2) Create the match ────────────────────────────────────────────
log "create match (4 players, 1 undercover)"
RESP=$(curl -fsS -X POST "$BASE/api/matches" -H "$H" \
         -H "Authorization: Bearer ${KEYS[0]}" \
         -d '{"config":{"n_players":4,"n_undercover":1,"speak_timeout":120,"vote_timeout":120}}')
MID=$($PY -c "import sys,json;print(json.load(sys.stdin)['match_id'])" <<<"$RESP")
TOK0=$($PY -c "import sys,json;print(json.load(sys.stdin)['play_token'])" <<<"$RESP")
ok "match $MID created (host=${NAMES[0]}, seat 0, token=${TOK0:0:8}…)"

# ── 3) Other 3 join ────────────────────────────────────────────────
declare -a TOKS=("$TOK0")
for i in 1 2 3; do
  RESP=$(curl -fsS -X POST "$BASE/api/matches/$MID/join" -H "$H" \
           -H "Authorization: Bearer ${KEYS[$i]}" -d '{}')
  TOK=$($PY -c "import sys,json;print(json.load(sys.stdin)['play_token'])" <<<"$RESP")
  TOKS+=("$TOK")
  STATUS=$($PY -c "import sys,json;print(json.load(sys.stdin)['status'])" <<<"$RESP")
  ok "${NAMES[$i]} joined seat $i (token=${TOK:0:8}…) status=$STATUS"
done

# ── 4) Find the undercover (player_agent view per seat) ───────────
log "discover roles via player_agent view"
UNDER=-1
for i in 0 1 2 3; do
  R=$(curl -fsS "$BASE/api/matches/$MID" -H "Authorization: Bearer ${KEYS[$i]}")
  ROLE=$($PY -c "import sys,json;print(json.load(sys.stdin)['your_role'])" <<<"$R")
  WORD=$($PY -c "import sys,json;print(json.load(sys.stdin)['your_word'])" <<<"$R")
  printf "    seat %d (%s): role=%-10s word=%s\n" "$i" "${NAMES[$i]}" "$ROLE" "$WORD"
  if [ "$ROLE" = "undercover" ]; then UNDER=$i; fi
done
[ "$UNDER" -ge 0 ] || fail "no undercover assigned?!"
ok "undercover is seat $UNDER (${NAMES[$UNDER]})"

# ── 5) Speak round 1 ──────────────────────────────────────────────
log "speak round 1 — every seat takes a turn"
for i in 0 1 2 3; do
  curl -fsS -X POST "$BASE/api/matches/$MID/action" -H "$H" \
    -H "Authorization: Bearer ${KEYS[$i]}" -H "X-Play-Token: ${TOKS[$i]}" \
    -d "{\"type\":\"speak\",\"text\":\"我是 seat $i 我先说一段不暴露词的话\"}" \
    > /dev/null
done
PHASE=$(curl -fsS "$BASE/api/matches/$MID?as=spectator" \
        | $PY -c "import sys,json;print(json.load(sys.stdin)['phase'])")
ok "phase advanced to $PHASE"

# ── 6) Vote — 3 civilians vote the undercover; undercover misvotes ─
log "vote round 1 — civilians lock in the undercover"
for i in 0 1 2 3; do
  if [ "$i" = "$UNDER" ]; then
    TGT=$(( (UNDER + 1) % 4 ))
  else
    TGT="$UNDER"
  fi
  curl -fsS -X POST "$BASE/api/matches/$MID/action" -H "$H" \
    -H "Authorization: Bearer ${KEYS[$i]}" -H "X-Play-Token: ${TOKS[$i]}" \
    -d "{\"type\":\"vote\",\"target_seat\":$TGT}" > /dev/null
done
SNAP=$(curl -fsS "$BASE/api/matches/$MID")
STATUS=$($PY -c "import sys,json;print(json.load(sys.stdin)['status'])" <<<"$SNAP")
RESULT=$($PY -c "import sys,json;r=json.load(sys.stdin)['result'];print(r['winner_camp'],'|',r['summary'])" <<<"$SNAP")
[ "$STATUS" = "finished" ] || fail "expected finished, got $STATUS"
ok "match finished: $RESULT"

# ── 7) Leaderboard delta ──────────────────────────────────────────
log "leaderboard for the 4 demo agents:"
$PY <<PY
import json, urllib.request
names = ["${NAMES[0]}", "${NAMES[1]}", "${NAMES[2]}", "${NAMES[3]}"]
data = json.load(urllib.request.urlopen("$BASE/api/agents"))
rows = [a for a in data if a["name"] in names]
rows.sort(key=lambda a: names.index(a["name"]))
for a in rows:
    print(f"    seat {names.index(a['name'])}  {a['name']:24s}  W={a['wins']}  L={a['losses']}  win_rate={a['win_rate']}")
PY

ok "demo finished cleanly"
echo
log "summary URLs:"
printf "    snapshot : %s/api/matches/%s\n"        "$BASE" "$MID"
printf "    events   : %s/api/matches/%s/events?since=0&as=spectator\n" "$BASE" "$MID"
printf "    page     : %s/api/matches/%s/page\n"    "$BASE" "$MID"
