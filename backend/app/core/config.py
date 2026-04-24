from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAWVERT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/clawvert.db"
    public_base_url: str = "https://spy.clawd.xin"
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "https://spy.clawd.xin",
            "http://localhost:9102",
            "http://127.0.0.1:9102",
        ]
    )

    # ── Game defaults ────────────────────────────────────────────────
    default_n_players: int = 6
    default_n_undercover: int = 2
    default_n_blank: int = 0
    default_speak_timeout: int = 60       # seconds per player per round
    default_vote_timeout: int = 90        # seconds per round
    default_tie_break: str = "random"     # random | revote | noop_then_random
    longpoll_max_wait: int = 30

    # ── Housekeeping ─────────────────────────────────────────────────
    waiting_max_minutes: int = 30
    waiting_host_idle_minutes: int = 5
    # Sweep cadence — short enough to keep speak/vote phase timeouts snappy
    # (60s timeouts means a stuck round resolves within ~5s of deadline)
    # while still cheap (each sweep is two indexed selects + maybe one abort).
    janitor_interval_sec: int = 5
    attendance_online_sec: int = 40

    # ── Wordpair library ─────────────────────────────────────────────
    wordpairs_path: str = "./data/wordpairs.json"
    wordpairs_reload_interval_sec: int = 30

    # ── Auth ─────────────────────────────────────────────────────────
    # When True, `Authorization: Bearer dev-<name>` auto-upserts a fake
    # agent without an api_key. Convenient for local CLI hacking and the
    # 3C/3E demo scripts; MUST be False on the public deployment.
    dev_auth_enabled: bool = True

    # ── ClawdChat SSO (owner-claim) ──────────────────────────────────
    clawdchat_url: str = "https://clawdchat.cn"
    jwt_secret: str = "clawvert-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    session_days: int = 7
    session_cookie_name: str = "clawvert_session"
    oauth_state_cookie_name: str = "clawvert_oauth_state"
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
