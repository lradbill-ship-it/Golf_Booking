"""Configuration loading and validation.

Credentials are read from environment variables (.env), never from the YAML
config. Everything else (URLs, selectors, timing, preferences) comes from
config.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, time as time_cls, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when configuration or credentials are missing or invalid."""


@dataclass
class Credentials:
    username: str
    password: str
    notify_webhook_url: str = ""

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "Credentials":
        # load_dotenv is a no-op if the file is absent; real env vars win.
        load_dotenv(dotenv_path=env_file, override=False)
        username = os.environ.get("GOLF_USERNAME", "").strip()
        password = os.environ.get("GOLF_PASSWORD", "")
        if not username or not password:
            raise ConfigError(
                "Missing credentials. Set GOLF_USERNAME and GOLF_PASSWORD in your "
                "environment or a .env file (copy .env.example to .env)."
            )
        return cls(
            username=username,
            password=password,
            notify_webhook_url=os.environ.get("NOTIFY_WEBHOOK_URL", "").strip(),
        )


@dataclass
class ReleaseConfig:
    days_ahead: int = 14
    release_time: str = "00:01"
    timezone: str = "America/New_York"
    warmup_seconds: int = 30
    retry_window_seconds: int = 90
    retry_interval_seconds: float = 1.0

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _parsed_release_time(self) -> time_cls:
        try:
            hh, mm = self.release_time.split(":")
            return time_cls(int(hh), int(mm))
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(
                f"Invalid release_time {self.release_time!r}; expected HH:MM"
            ) from exc

    def release_moment_for(self, play_date: date_cls) -> datetime:
        """The exact timezone-aware instant the given play date opens for booking."""
        open_day = play_date - timedelta(days=self.days_ahead)
        rt = self._parsed_release_time()
        return datetime.combine(open_day, rt, tzinfo=self.tz)


@dataclass
class BookingConfig:
    date: str = ""
    preferred_times: list[str] = field(default_factory=list)
    players: int = 1

    def resolved_date(self, override: Optional[str]) -> date_cls:
        raw = (override or self.date or "").strip()
        if not raw:
            raise ConfigError(
                "No play date. Pass --date YYYY-MM-DD or set booking.date in config.yaml."
            )
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ConfigError(f"Invalid date {raw!r}; expected YYYY-MM-DD.") from exc


@dataclass
class ClubConfig:
    login_url: str = ""
    tee_sheet_url: str = ""
    date_url_format: str = "%Y-%m-%d"


@dataclass
class RuntimeConfig:
    headless: bool = True
    screenshot_on_error: bool = True
    screenshot_dir: str = "screenshots"
    slow_mo_ms: int = 0


@dataclass
class Config:
    club: ClubConfig
    release: ReleaseConfig
    booking: BookingConfig
    selectors: dict
    date_picker: dict
    runtime: RuntimeConfig
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(
                f"Config file {path!r} not found. Copy config.example.yaml to {path} "
                "and fill in your club's details."
            )
        data = yaml.safe_load(p.read_text()) or {}

        club = ClubConfig(**(data.get("club") or {}))
        release = ReleaseConfig(**(data.get("release") or {}))
        booking = BookingConfig(**(data.get("booking") or {}))
        runtime = RuntimeConfig(**(data.get("runtime") or {}))
        selectors = data.get("selectors") or {}
        date_picker = data.get("date_picker") or {}

        cfg = cls(
            club=club,
            release=release,
            booking=booking,
            selectors=selectors,
            date_picker=date_picker,
            runtime=runtime,
            raw=data,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.club.login_url or "example.com" in self.club.login_url:
            raise ConfigError(
                "club.login_url is still the placeholder. Set it to your club's real login page."
            )
        if not self.club.tee_sheet_url:
            raise ConfigError("club.tee_sheet_url is required.")
        required_selectors = [
            "username",
            "password",
            "login_button",
            "time_slot",
            "book_button",
        ]
        missing = [s for s in required_selectors if not self.selectors.get(s)]
        if missing:
            raise ConfigError(
                "Missing required selectors in config.yaml: " + ", ".join(missing)
            )
        if not self.booking.preferred_times:
            raise ConfigError("booking.preferred_times must list at least one time.")

    def tee_sheet_url_for(self, play_date: date_cls) -> str:
        formatted = play_date.strftime(self.club.date_url_format)
        return self.club.tee_sheet_url.replace("{date}", formatted)
