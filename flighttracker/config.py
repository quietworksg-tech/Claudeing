"""Settings: environment variables, optionally seeded from a JSON config file.

Lookup order (first hit wins):  env var  ->  config file key  ->  default.
Config file: $FLIGHTTRACKER_CONFIG, else ~/.flighttracker/config.json
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("FLIGHTTRACKER_CONFIG", Path.home() / ".flighttracker" / "config.json"))


def _file_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _get(key: str, default=None, cast=str):
    raw = os.environ.get(f"FLIGHTTRACKER_{key.upper()}")
    if raw is None:
        raw = _file_config().get(key)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class Settings:
    db_path: str = field(default_factory=lambda: _get("db", str(Path.home() / ".flighttracker" / "tracker.db")))
    tz: str = field(default_factory=lambda: _get("tz", "UTC"))

    # alerting
    cooldown_minutes: int = field(default_factory=lambda: _get("cooldown_minutes", 180, int))
    drop_pct: float = field(default_factory=lambda: _get("drop_pct", 8.0, float))
    alert_on_all_time_low: bool = field(default_factory=lambda: _get("alert_on_all_time_low", True, bool))

    # scheduling
    max_workers: int = field(default_factory=lambda: _get("max_workers", 4, int))
    jitter_pct: float = field(default_factory=lambda: _get("jitter_pct", 10.0, float))

    # notification channels (empty = disabled)
    webhook_url: str = field(default_factory=lambda: _get("webhook_url", ""))
    alert_log: str = field(default_factory=lambda: _get("alert_log", ""))
    smtp_host: str = field(default_factory=lambda: _get("smtp_host", ""))
    smtp_port: int = field(default_factory=lambda: _get("smtp_port", 587, int))
    smtp_user: str = field(default_factory=lambda: _get("smtp_user", ""))
    smtp_password: str = field(default_factory=lambda: _get("smtp_password", ""))
    email_from: str = field(default_factory=lambda: _get("email_from", ""))
    email_to: str = field(default_factory=lambda: _get("email_to", ""))
    desktop_notify: bool = field(default_factory=lambda: _get("desktop_notify", False, bool))

    def active_channels(self) -> list[str]:
        channels = ["console"]
        if self.webhook_url:
            channels.append("webhook")
        if self.smtp_host and self.email_to:
            channels.append("email")
        if self.alert_log:
            channels.append("file")
        if self.desktop_notify:
            channels.append("desktop")
        return channels


def load_settings() -> Settings:
    return Settings()
