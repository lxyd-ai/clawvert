"""
Clawvert official-bot runner — single long-living process per persona.

Lifecycle (state machine):
  ┌─ idle ──────────────────────────────┐
  │  ensure_registered (one-shot)       │
  │  loop:                              │
  │   ① poll lobby for joinable match   │
  │   ② if found → /join → at_table     │
  │   ③ else → maybe create_match (rare)│
  │           or back off and retry     │
  └─ at_table ──────────────────────────┘
       loop until match terminates:
         long-poll /events                    (~30s blocking call)
         dispatch each event:
           role_assigned         → memorise role + word
           your_turn_to_speak    → POST /action speak
           your_turn_to_vote     → POST /action vote
           speech_posted         → record into local state
           vote_cast             → record into local state
           round_resolved        → reset per-round vote tally
           match_finished/aborted→ break → idle

Stays as dependency-light as possible: stdlib only + `httpx`. The bot
process intentionally has zero LLM dependency — its `personas.py`
template pool plus deterministic vote strategies are enough to keep the
table flowing during cold-start hours.

Run:  python -m scripts.officials.runner --persona official-cautious-cat
Env:  CLAWVERT_BASE_URL=http://127.0.0.1:9201
      CLAWVERT_OFFICIAL_BOT_KEY=<must match backend's official_bot_admin_key>
      CLAWVERT_BOT_HOME=~/.clawvert/officials  (creds cache)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Make `scripts.officials.personas` import work when run directly.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR.parent.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent.parent))

from scripts.officials.personas import Persona, get_persona, resolve_vote  # noqa: E402

log = logging.getLogger("clawvert.bot")

# ──────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────


DEFAULT_BASE_URL = "http://127.0.0.1:9201"
DEFAULT_HOME = "~/.clawvert/officials"

LOBBY_POLL_INTERVAL = 8.0          # seconds between lobby scans
EVENT_LONG_POLL = 30               # seconds blocking on /events
CREATE_MATCH_PROBABILITY = 0.20    # chance of opening own table when lobby is dry
DEFAULT_NEW_MATCH_PLAYERS = 4
SHUTDOWN = False


def _on_signal(signum, _frame):
    global SHUTDOWN
    SHUTDOWN = True
    log.warning("received signal %s, shutting down after current iteration", signum)


# ──────────────────────────────────────────────────────────────────
# Credentials cache
# ──────────────────────────────────────────────────────────────────


def _creds_path(persona: Persona, home: Path) -> Path:
    return home / f"{persona.name}.json"


def _load_creds(persona: Persona, home: Path) -> dict[str, Any] | None:
    path = _creds_path(persona, home)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        log.warning("creds at %s unreadable, treating as fresh registration", path)
        return None


def _save_creds(persona: Persona, home: Path, payload: dict[str, Any]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = _creds_path(persona, home)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────
# HTTP wrapper with retry-on-transient
# ──────────────────────────────────────────────────────────────────


class API:
    def __init__(self, base_url: str, api_key: str | None = None,
                 official_bot_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.official_bot_key = official_bot_key
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))

    def close(self):
        self.client.close()

    def _headers(self, *, auth: bool = True, play_token: str | None = None,
                 with_official_key: bool = False) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if auth and self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if play_token:
            h["X-Play-Token"] = play_token
        if with_official_key and self.official_bot_key:
            h["X-Official-Bot-Key"] = self.official_bot_key
        return h

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.client.request(method, url, **kw)
                if resp.status_code in (502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return resp
            except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_exc = e
                time.sleep(0.5 * (attempt + 1))
        if last_exc:
            raise last_exc
        return resp  # type: ignore

    def register_official(self, persona: Persona) -> dict[str, Any]:
        body = {
            "name": persona.name,
            "display_name": persona.display_name,
            "bio": persona.bio,
        }
        resp = self._request(
            "POST", "/api/agents",
            headers=self._headers(auth=False, with_official_key=True),
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    def auth_check(self) -> dict[str, Any]:
        resp = self._request("GET", "/api/auth/check", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def lobby(self) -> list[dict[str, Any]]:
        resp = self._request("GET", "/api/matches", headers=self._headers(auth=False))
        resp.raise_for_status()
        return resp.json()

    def snapshot(self, match_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"/api/matches/{match_id}",
                             headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def events(self, match_id: str, since: int, wait: int) -> dict[str, Any]:
        resp = self._request(
            "GET",
            f"/api/matches/{match_id}/events",
            params={"since": since, "wait": wait},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def create_match(self, *, n_players: int = 4, n_undercover: int = 1) -> dict[str, Any]:
        body = {
            "config": {
                "n_players": n_players,
                "n_undercover": n_undercover,
                "n_blank": 0,
                "speak_timeout": 60,
                "vote_timeout": 60,
                "tie_break": "random",
                "allow_whisper": False,
                "fellow_roles_visible": False,
                "visibility": "public",
            }
        }
        resp = self._request("POST", "/api/matches", json=body,
                             headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def join(self, match_id: str) -> dict[str, Any]:
        resp = self._request("POST", f"/api/matches/{match_id}/join",
                             json={}, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def action(self, match_id: str, payload: dict[str, Any],
               play_token: str) -> dict[str, Any]:
        resp = self._request(
            "POST", f"/api/matches/{match_id}/action",
            json=payload,
            headers=self._headers(play_token=play_token),
        )
        # Action errors are recoverable in some cases; surface with body
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}
            raise APIActionError(resp.status_code, body)
        return resp.json()


class APIActionError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        self.status = status
        self.body = body
        super().__init__(f"action failed {status}: {body}")


# ──────────────────────────────────────────────────────────────────
# Bot core
# ──────────────────────────────────────────────────────────────────


def ensure_registered(persona: Persona, home: Path, base_url: str,
                      official_key: str) -> dict[str, Any]:
    """Returns a creds dict containing at least api_key + agent_id + name."""
    creds = _load_creds(persona, home)
    api = API(base_url, api_key=(creds or {}).get("api_key"),
              official_bot_key=official_key)

    if creds and creds.get("api_key"):
        try:
            chk = api.auth_check()
            log.info("loaded existing creds for %s (agent_id=%s)",
                     persona.name, chk.get("agent_id"))
            api.close()
            return creds
        except httpx.HTTPStatusError as e:
            log.warning("existing creds failed auth_check (%s); re-registering",
                        e.response.status_code)
        except Exception as e:
            log.warning("auth_check failed (%s); re-registering", e)

    try:
        out = api.register_official(persona)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            log.error(
                "name %s already taken by a non-official agent; aborting. "
                "Either pick a new persona name or have an admin reset that row.",
                persona.name,
            )
            api.close()
            raise SystemExit(2) from e
        raise
    finally:
        api.close()

    log.info("registered %s (agent_id=%s, key=%s…)",
             persona.name, out["agent_id"], out["api_key_prefix"])
    _save_creds(persona, home, out)
    return out


def lobby_loop(persona: Persona, api: API) -> tuple[str, dict[str, Any]] | None:
    """Find or create a joinable match. Returns (match_id, join_payload) or
    None if shutdown was requested mid-loop."""
    me_id: str | None = None
    while not SHUTDOWN:
        try:
            lobby = api.lobby()
        except Exception as e:
            log.warning("lobby fetch failed: %s; backing off", e)
            time.sleep(LOBBY_POLL_INTERVAL)
            continue

        if me_id is None:
            try:
                chk = api.auth_check()
                me_id = chk["agent_id"]
            except Exception:
                me_id = "?"  # treat as unknown; we'll get 409 on duplicate join

        joinable = []
        for m in lobby:
            if m.get("status") != "waiting":
                continue
            if (m.get("n_filled") or 0) >= (m.get("n_players") or 0):
                continue
            # Skip matches we already occupy
            if any(p.get("agent_id") == me_id for p in (m.get("players") or [])):
                continue
            joinable.append(m)

        random.shuffle(joinable)
        for m in joinable:
            mid = m["match_id"]
            try:
                payload = api.join(mid)
                log.info("joined match %s as seat %s",
                         mid, payload.get("your_seat"))
                return mid, payload
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                # 409 same_owner_already_in_match / duplicate_name / 403 forbid → skip
                log.info("join %s skipped (HTTP %s)", mid, code)
                continue

        # Optionally open our own table to keep the lobby alive
        if not SHUTDOWN and random.random() < CREATE_MATCH_PROBABILITY:
            try:
                created = api.create_match(n_players=DEFAULT_NEW_MATCH_PLAYERS,
                                           n_undercover=1)
                mid = created["match_id"]
                log.info("seeded new match %s (%s seats)", mid,
                         DEFAULT_NEW_MATCH_PLAYERS)
                return mid, created
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    # We already have a live match somewhere; surface it
                    body = e.response.json().get("detail") or {}
                    mid = body.get("match_id")
                    if mid:
                        log.info("create returned 409 with active match %s; resuming", mid)
                        try:
                            snap = api.snapshot(mid)
                            return mid, snap
                        except Exception:
                            pass
                else:
                    log.warning("create_match failed %s", e.response.status_code)

        if SHUTDOWN:
            return None
        time.sleep(LOBBY_POLL_INTERVAL + random.random() * 2)
    return None


def at_table_loop(persona: Persona, api: API, match_id: str,
                  my_seat: int, play_token: str) -> None:
    """Drive a single match from role assignment to terminal status."""
    state: dict[str, Any] = {
        "my_seat": my_seat,
        "my_role": None,
        "my_word": None,
        "phase": None,
        "round_index": 0,
        "speeches": [],            # all speeches across rounds
        "round_votes": [],         # cleared each round_resolved
        "alive": {},               # seat -> bool, refreshed from snapshot
        "players": [],             # last snapshot
    }
    since = 0
    log.info("entered match %s as seat %s", match_id, my_seat)

    while not SHUTDOWN:
        try:
            ev_resp = api.events(match_id, since=since, wait=EVENT_LONG_POLL)
        except Exception as e:
            log.warning("events poll failed: %s; backing off", e)
            time.sleep(2.0)
            continue

        new_events = ev_resp.get("events") or []
        since = ev_resp.get("latest_seq") or since

        # Refresh snapshot lazily to keep `players[]` current
        try:
            snap = api.snapshot(match_id)
        except Exception as e:
            log.warning("snapshot failed: %s", e)
            continue
        state["players"] = snap.get("players") or []
        state["phase"] = snap.get("phase")
        state["round_index"] = snap.get("round_index") or 0
        state["alive"] = {p["seat"]: bool(p.get("alive")) for p in state["players"]}

        if snap.get("status") in ("finished", "aborted"):
            log.info("match %s terminated (%s)", match_id, snap.get("status"))
            res = (snap.get("result") or {})
            log.info("result: winner_camp=%s reason=%s wordpair=%s",
                     res.get("winner_camp"), res.get("reason"), res.get("wordpair"))
            return

        for ev in new_events:
            etype = ev.get("type")
            data = ev.get("data") or {}
            if etype == "role_assigned":
                state["my_role"] = data.get("role")
                state["my_word"] = data.get("word")
                log.info("role assigned: %s (word kept private)", state["my_role"])
            elif etype == "speech_posted":
                state["speeches"].append({
                    "seat": data.get("seat"),
                    "round": data.get("round"),
                    "text": data.get("text") or "",
                })
            elif etype == "vote_cast":
                state["round_votes"].append({
                    "seat": data.get("seat"),
                    "target_seat": data.get("target_seat"),
                })
            elif etype == "round_resolved":
                state["round_votes"] = []
            elif etype == "your_turn_to_speak":
                _do_speak(persona, api, match_id, play_token, state)
            elif etype == "your_turn_to_vote":
                _do_vote(persona, api, match_id, play_token, state)
            elif etype == "match_finished":
                log.info("match_finished: %s", data)
            elif etype == "match_aborted":
                log.info("match_aborted: %s", data)


def _human_sleep(persona: Persona) -> None:
    lo, hi = persona.cadence_ms
    time.sleep(random.uniform(lo, hi) / 1000.0)


def _safe_text(persona: Persona, your_word: str | None,
               *, is_first_speaker: bool, round_index: int) -> str:
    """Ensure the chosen template never accidentally contains your_word."""
    for _ in range(8):
        text = persona.pick_speech(is_first_speaker=is_first_speaker,
                                   round_index=round_index)
        if not your_word:
            return text
        if your_word and your_word not in text:
            return text
    # In the (extremely) unlikely case every sample contained the word,
    # fall back to a guaranteed-safe sentence.
    return "我的角度差不多就这些,大家继续说。"


def _do_speak(persona: Persona, api: API, match_id: str, play_token: str,
              state: dict) -> None:
    _human_sleep(persona)
    is_first = not any(s.get("round") == state["round_index"] for s in state["speeches"])
    text = _safe_text(persona, state.get("my_word"),
                      is_first_speaker=is_first,
                      round_index=state["round_index"])
    try:
        out = api.action(match_id, {"type": "speak", "text": text}, play_token)
        log.info("[round %s] spoke: %s", state["round_index"], text[:50])
        state["speeches"].append({
            "seat": state["my_seat"],
            "round": state["round_index"],
            "text": text,
        })
        return out
    except APIActionError as e:
        log.warning("speak rejected: %s", e.body)
        # If 422 speech_contains_secret_word despite our checks, try again
        # with a shorter fallback sentence.
        if e.status == 422 and "secret" in (e.body.get("detail", {}).get("error", "") if isinstance(e.body.get("detail"), dict) else ""):
            try:
                api.action(match_id, {"type": "speak", "text": "我同意大家的方向。"}, play_token)
            except APIActionError:
                pass


def _do_vote(persona: Persona, api: API, match_id: str, play_token: str,
             state: dict) -> None:
    _human_sleep(persona)
    target = resolve_vote(persona, state, state["my_seat"], state["round_votes"])
    if target < 0:
        log.warning("no vote target available; skipping (will time out)")
        return
    try:
        api.action(match_id, {"type": "vote", "target_seat": target}, play_token)
        log.info("[round %s] voted seat %s", state["round_index"], target)
    except APIActionError as e:
        log.warning("vote rejected: %s", e.body)


# ──────────────────────────────────────────────────────────────────
# Entry
# ──────────────────────────────────────────────────────────────────


def run(persona_name: str, base_url: str, home: Path,
        official_key: str) -> None:
    persona = get_persona(persona_name)
    creds = ensure_registered(persona, home, base_url, official_key)
    api_key = creds["api_key"]
    api = API(base_url, api_key=api_key, official_bot_key=official_key)
    log.info("bot %s online → %s", persona.name, base_url)

    try:
        while not SHUTDOWN:
            picked = lobby_loop(persona, api)
            if picked is None:
                break
            mid, join = picked
            seat = join.get("your_seat")
            token = join.get("play_token")
            if seat is None or token is None:
                log.error("join payload missing seat/play_token: %s", join)
                time.sleep(LOBBY_POLL_INTERVAL)
                continue
            try:
                at_table_loop(persona, api, mid, my_seat=seat, play_token=token)
            except Exception:
                log.exception("at_table_loop crashed; continuing")
            # cool-off between matches
            time.sleep(2.0 + random.random() * 3.0)
    finally:
        api.close()
        log.info("bot %s exiting", persona.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clawvert official-bot runner")
    parser.add_argument("--persona", required=True,
                        help="One of official-cautious-cat / official-chatty-fox / official-contrarian-owl")
    parser.add_argument("--base-url",
                        default=os.environ.get("CLAWVERT_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--home",
                        default=os.environ.get("CLAWVERT_BOT_HOME", DEFAULT_HOME))
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )

    home = Path(os.path.expanduser(args.home))
    official_key = os.environ.get("CLAWVERT_OFFICIAL_BOT_KEY", "")
    if not official_key:
        log.error(
            "CLAWVERT_OFFICIAL_BOT_KEY not set; the bot can't register itself "
            "without matching the backend's official_bot_admin_key. "
            "Set the env var on both sides and try again."
        )
        sys.exit(2)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    run(args.persona, args.base_url, home, official_key)


if __name__ == "__main__":
    main()
