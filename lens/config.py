"""Configuration management with SQLite persistence and environment overrides."""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any

from lens.store import LensStore

ENV_SETTING_MAP: dict[str, str] = {
    "public_url": "LENS_PUBLIC_URL",
    "poll_interval_seconds": "LENS_POLL_INTERVAL",
    "pr_cap": "LENS_PR_CAP",
    "idle_days_default": "LENS_IDLE_DAYS",
    "session_secret": "LENS_SESSION_SECRET",
    "setup_code": "LENS_SETUP_CODE",
}

DEFAULTS: dict[str, Any] = {
    "public_url": "http://localhost:8080",
    "poll_interval_seconds": 300,
    "pr_cap": 20,
    "idle_days_default": 14,
    "setup_complete": False,
}


class LensConfig:
    """Reads and writes configuration with read-only env overrides (Section 10.1)."""

    def __init__(self, store: LensStore) -> None:
        self.store = store

    def is_env_overridden(self, key: str) -> bool:
        env_var = ENV_SETTING_MAP.get(key)
        return bool(env_var and os.environ.get(env_var, "").strip() != "")

    def get(self, key: str, default: Any = None) -> Any:
        env_var = ENV_SETTING_MAP.get(key)
        if env_var:
            raw = os.environ.get(env_var, "").strip()
            if raw != "":
                if isinstance(DEFAULTS.get(key), int):
                    try:
                        return int(raw)
                    except ValueError:
                        return DEFAULTS[key]
                return raw
        val = self.store.get_setting(key)
        if val is not None:
            return val
        if key in DEFAULTS:
            return DEFAULTS[key]
        return default

    def set(self, key: str, value: Any) -> None:
        if self.is_env_overridden(key):
            return
        self.store.set_setting(key, value)

    def get_session_secret(self) -> str:
        env_sec = os.environ.get("LENS_SESSION_SECRET", "").strip()
        if env_sec:
            return env_sec
        existing = self.store.get_secret_setting("session_secret")
        if existing:
            return existing
        new_sec = secrets.token_hex(32)
        self.store.set_secret_setting("session_secret", new_sec)
        return new_sec

    def ensure_setup_code(self) -> str | None:
        """Generate a one-time setup code on first start if setup is not complete.

        Returns the plaintext code only if newly generated (or from LENS_SETUP_CODE),
        or None if setup is already complete or a code already exists.
        """
        if bool(self.get("setup_complete", False)):
            return None
        env_code = os.environ.get("LENS_SETUP_CODE", "").strip()
        if env_code:
            return env_code
        existing_hash = self.store.get_setting("setup_code_hash")
        if existing_hash:
            return None
        code = f"{secrets.token_hex(3)}-{secrets.token_hex(3)}".upper()
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        self.store.set_setting("setup_code_hash", digest)
        return code

    def verify_setup_code(self, candidate: str) -> bool:
        """Verify the one-time setup code entered in the browser wizard."""
        env_code = os.environ.get("LENS_SETUP_CODE", "").strip()
        cleaned = (candidate or "").strip().upper()
        if env_code:
            return secrets.compare_digest(cleaned, env_code.upper())
        stored_hash = str(self.store.get_setting("setup_code_hash") or "")
        if not stored_hash:
            return False
        cand_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        return secrets.compare_digest(cand_hash, stored_hash)

    def mark_setup_complete(self) -> None:
        self.store.set_setting("setup_complete", True)
        self.store.delete_setting("setup_code_hash")

    def poll_interval_seconds(self) -> int:
        val = int(self.get("poll_interval_seconds", 300))
        return max(60, val)

    def pr_cap(self) -> int:
        val = int(self.get("pr_cap", 20))
        return max(1, val)
