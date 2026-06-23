import os
from datetime import date

import pytest

from tee_booker.config import (
    BookingConfig,
    Config,
    ConfigError,
    Credentials,
    ReleaseConfig,
)


def test_release_moment_is_two_weeks_before_at_release_time():
    rc = ReleaseConfig(days_ahead=14, release_time="00:01", timezone="America/New_York")
    play = date(2026, 7, 7)
    moment = rc.release_moment_for(play)
    assert moment.date() == date(2026, 6, 23)
    assert (moment.hour, moment.minute) == (0, 1)
    # July is daylight time in New York -> UTC-4.
    assert moment.utcoffset().total_seconds() == -4 * 3600


def test_release_moment_handles_standard_time():
    rc = ReleaseConfig(days_ahead=14, release_time="00:01", timezone="America/New_York")
    # Play date in January -> release in December -> EST (UTC-5).
    moment = rc.release_moment_for(date(2026, 1, 20))
    assert moment.utcoffset().total_seconds() == -5 * 3600


def test_booking_date_resolution_prefers_override():
    bc = BookingConfig(date="2026-07-07", preferred_times=["08:00 AM"], players=4)
    assert bc.resolved_date("2026-08-01") == date(2026, 8, 1)
    assert bc.resolved_date(None) == date(2026, 7, 7)


def test_booking_date_requires_value():
    bc = BookingConfig(date="", preferred_times=["08:00 AM"])
    with pytest.raises(ConfigError):
        bc.resolved_date(None)


def test_invalid_date_raises():
    bc = BookingConfig(date="07/07/2026", preferred_times=["08:00 AM"])
    with pytest.raises(ConfigError):
        bc.resolved_date(None)


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("GOLF_USERNAME", "me")
    monkeypatch.setenv("GOLF_PASSWORD", "secret")
    creds = Credentials.from_env(env_file="/nonexistent")
    assert creds.username == "me"
    assert creds.password == "secret"


def test_credentials_missing_raises(monkeypatch):
    monkeypatch.delenv("GOLF_USERNAME", raising=False)
    monkeypatch.delenv("GOLF_PASSWORD", raising=False)
    with pytest.raises(ConfigError):
        Credentials.from_env(env_file="/nonexistent")


def test_tee_sheet_url_substitutes_date():
    cfg = Config(
        club=type("C", (), {"login_url": "x", "tee_sheet_url": "https://c/ts?d={date}", "date_url_format": "%Y-%m-%d"})(),
        release=ReleaseConfig(),
        booking=BookingConfig(preferred_times=["08:00 AM"]),
        selectors={},
        date_picker={},
        runtime=type("R", (), {})(),
    )
    assert cfg.tee_sheet_url_for(date(2026, 7, 7)) == "https://c/ts?d=2026-07-07"
