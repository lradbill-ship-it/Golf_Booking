"""The nightly release-time log used to learn the real (late) release time.

These touch the filesystem, so the history path is redirected into a tmp file
to stay hermetic.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from tee_booker import release_history


@pytest.fixture
def history_path(tmp_path):
    return str(tmp_path / "release_history.jsonl")


ET = timezone(timedelta(hours=-4))


def test_record_computes_delay_and_round_trips(history_path):
    nominal = datetime(2026, 7, 12, 0, 1, tzinfo=ET)
    released = nominal + timedelta(minutes=13, seconds=18)  # ~the observed ~12:14
    rec = release_history.record(
        play_date=date(2026, 7, 12),
        weekday="Sunday",
        nominal_release=nominal,
        released_at=released,
        booked=True,
        booked_time="7:00 AM",
        attempts=30,
        outcome="Booked 7:00 AM for 2 players.",
        path=history_path,
    )
    assert rec["seconds_after_nominal"] == pytest.approx(798.0)

    loaded = release_history.load(history_path)
    assert len(loaded) == 1
    assert loaded[0]["play_date"] == "2026-07-12"
    assert loaded[0]["booked"] is True
    assert loaded[0]["booked_time"] == "7:00 AM"


def test_record_appends(history_path):
    nominal = datetime(2026, 7, 12, 0, 1, tzinfo=ET)
    for i in range(3):
        release_history.record(
            play_date=date(2026, 7, 12 + i),
            weekday="Day",
            nominal_release=nominal,
            released_at=nominal + timedelta(minutes=10 + i),
            booked=True,
            booked_time="7:00 AM",
            attempts=20,
            outcome="ok",
            path=history_path,
        )
    assert len(release_history.load(history_path)) == 3


def test_record_handles_no_release(history_path):
    """A night where the sheet never released has no delay to compute."""
    nominal = datetime(2026, 7, 12, 0, 1, tzinfo=ET)
    rec = release_history.record(
        play_date=date(2026, 7, 12),
        weekday="Sunday",
        nominal_release=nominal,
        released_at=None,
        booked=False,
        booked_time=None,
        attempts=40,
        outcome="No preferred time became bookable.",
        path=history_path,
    )
    assert rec["released_at"] is None
    assert rec["seconds_after_nominal"] is None


def test_load_missing_file_is_empty(history_path):
    assert release_history.load(history_path) == []


def test_load_skips_corrupt_lines(history_path, tmp_path):
    with open(history_path, "w") as fh:
        fh.write('{"play_date": "2026-07-12"}\n')
        fh.write("{ not json\n")
        fh.write('{"play_date": "2026-07-13"}\n')
    loaded = release_history.load(history_path)
    assert [r["play_date"] for r in loaded] == ["2026-07-12", "2026-07-13"]


def test_summarize_empty(history_path):
    assert "No release history" in release_history.summarize(history_path)


def test_summarize_reports_stats(history_path):
    nominal = datetime(2026, 7, 12, 0, 1, tzinfo=ET)
    for mins in (10, 14, 12):
        release_history.record(
            play_date=date(2026, 7, 12),
            weekday="Sunday",
            nominal_release=nominal,
            released_at=nominal + timedelta(minutes=mins),
            booked=True,
            booked_time="7:00 AM",
            attempts=20,
            outcome="ok",
            path=history_path,
        )
    out = release_history.summarize(history_path)
    assert "3 night(s)" in out
    assert "median +12m00s" in out
    assert "earliest +10m00s" in out
    assert "latest +14m00s" in out


def test_fmt_delta_negative():
    # An early release (before nominal) reads with a minus sign.
    assert release_history._fmt_delta(-65) == "-1m05s"
    assert release_history._fmt_delta(None) == "—"
