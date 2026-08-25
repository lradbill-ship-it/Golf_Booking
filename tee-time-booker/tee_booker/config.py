"""Configuration loading and validation.

Credentials are read from environment variables (.env), never from the YAML
config. Everything else (URLs, selectors, timing, preferences) comes from
config.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
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
    # Log in this many seconds BEFORE the release so authentication happens
    # before the 12:01 traffic surge (logging in at the peak was failing).
    prelogin_seconds: int = 60
    retry_window_seconds: int = 90
    # Seconds between retries. Keep this gentle — reloading too fast trips the
    # site's rate limiter (Cloudflare 1015) and gets the booker temporarily banned.
    retry_interval_seconds: float = 6.0
    # If the Mac has no internet at login time (e.g. Wi-Fi hasn't reconnected at
    # 00:00), keep retrying the login this many seconds before giving up. There's
    # ~13 min of slack before the sheet actually releases, so a brief blip is
    # survivable. 0 disables the retry (fail on the first network error).
    login_retry_seconds: int = 300
    # Gentle gap between login retries when offline.
    login_retry_interval_seconds: float = 10.0

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
class FallbackConfig:
    """What to do when none of `preferred_times` is bookable.

    Tee sheets shift with sunrise: a 6:30 AM slot that exists in June simply
    stops being published in August. Without a fallback the booker would find
    nothing and book nothing on exactly the nights the early times moved. With
    it, the booker settles for the bookable slot closest to what was asked for
    (earlier one wins a tie).

    The bounds below decide whether "closest" is allowed to be a long way off.
    Set `max_minutes_later: null` for the always-book rule: take the nearest
    available time however late it is, on the view that a 2 PM round beats no
    round. Give it a number instead to cap how far it may stray.
    """

    enabled: bool = True
    # How far either side of the preferred range a fallback slot may sit,
    # measured from the earliest/latest preferred time respectively.
    # null = no bound on that side (any time on the sheet qualifies).
    max_minutes_earlier: Optional[int] = 60
    max_minutes_later: Optional[int] = None
    # Don't fall back until this many seconds into the poll window (0 = as soon
    # as the sheet is up). Raise it to give the preferred times a head start.
    after_seconds: int = 0
    # Before settling for a fallback, pause this long and re-scan the preferred
    # times once — cheap insurance against a half-rendered sheet making a
    # preferred slot look unavailable for a moment.
    recheck_seconds: float = 3.0

    def __post_init__(self) -> None:
        for name in ("max_minutes_earlier", "max_minutes_later"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ConfigError(
                    f"booking.fallback.{name} must be 0 or more, or null for no "
                    f"bound; got {value}."
                )


@dataclass
class BookingConfig:
    date: str = ""
    preferred_times: list[str] = field(default_factory=list)
    players: int = 1
    fallback: FallbackConfig = field(default_factory=FallbackConfig)

    def __post_init__(self) -> None:
        # Allow the nested mapping straight from YAML.
        if isinstance(self.fallback, dict):
            known = {f.name for f in fields(FallbackConfig)}
            unknown = set(self.fallback) - known
            if unknown:
                raise ConfigError(
                    "Unknown key(s) under booking.fallback: "
                    + ", ".join(sorted(unknown))
                    + ". Valid keys: "
                    + ", ".join(sorted(known))
                )
            self.fallback = FallbackConfig(**self.fallback)

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
    # Optional multi-step checkout settings (e.g. TeeItUp cart flow). Empty for
    # portals that book in a single confirm click.
    checkout: dict = field(default_factory=dict)
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
        checkout = data.get("checkout") or {}

        cfg = cls(
            club=club,
            release=release,
            booking=booking,
            selectors=selectors,
            date_picker=date_picker,
            runtime=runtime,
            checkout=checkout,
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
