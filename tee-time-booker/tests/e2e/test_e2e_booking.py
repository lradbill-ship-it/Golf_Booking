"""End-to-end tests: drive the real TeeBooker + a headless browser against the
local mock portal (see conftest.py). These prove login, slot-matching, the
one-booking-per-run guarantee, and party-size filtering all work against a live
DOM — not just in unit isolation.

Skips automatically when Playwright/Flask/Chromium aren't available.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date

import pytest

pytest.importorskip("playwright", reason="e2e booking test needs Playwright")
pytest.importorskip("flask", reason="e2e mock portal needs Flask")

from tee_booker.booker import TeeBooker  # noqa: E402
from tee_booker.config import Credentials  # noqa: E402

PLAY = date(2026, 7, 9)


def _bookings(base: str) -> list:
    with urllib.request.urlopen(base + "/count", timeout=2) as r:
        return json.load(r)["bookings"]


def _run(cfg):
    return TeeBooker(cfg, Credentials.from_env()).run(PLAY, dry_run=False)


def test_dry_run_finds_slot_but_books_nothing(make_cfg, mock_server):
    cfg = make_cfg(["08:00 AM", "08:10 AM"], players=2)
    result = TeeBooker(cfg, Credentials.from_env()).run(PLAY, dry_run=True)

    assert result.success
    assert result.booked_time.strip().startswith("08:00")
    assert _bookings(mock_server) == []  # nothing was actually booked


def test_books_first_preferred_time_exactly_once(make_cfg, mock_server):
    cfg = make_cfg(["08:00 AM", "08:10 AM"], players=2)
    result = _run(cfg)

    assert result.success
    assert result.booked_time.strip().startswith("08:00")  # first preference wins
    bookings = _bookings(mock_server)
    assert len(bookings) == 1  # the one-booking-per-run guarantee
    assert bookings[0]["time"] == "08:00 AM"


def test_respects_party_size_when_choosing_slot(make_cfg, mock_server):
    # 08:00 is "1 - 4" (fits 4), 07:50 is "1 or 2" (does not). A foursome that
    # would accept either must pick 08:00 and skip 07:50.
    cfg = make_cfg(["07:50 AM", "08:00 AM"], players=4)
    result = _run(cfg)

    assert result.success
    assert result.booked_time.strip().startswith("08:00")
    assert _bookings(mock_server)[0]["time"] == "08:00 AM"


def test_refuses_to_book_a_too_small_slot(make_cfg, mock_server):
    # The only acceptable time (07:50) only allows "1 or 2"; for a foursome the
    # booker must refuse rather than silently book a smaller group.
    cfg = make_cfg(["07:50 AM"], players=4)
    result = _run(cfg)

    assert not result.success
    assert _bookings(mock_server) == []  # zero bookings made


def test_empty_sheet_reports_no_release_not_a_failure(make_cfg, mock_server):
    # The club hasn't released times: the sheet renders with zero slots. The
    # booker should report this as "no_release" (benign), not a missed booking.
    urllib.request.urlopen(mock_server + "/mode?empty=1", timeout=2)
    cfg = make_cfg(["08:00 AM"], players=2)
    cfg.release.retry_window_seconds = 2  # keep the test quick
    result = _run(cfg)

    assert not result.success
    assert result.outcome == "no_release"
    assert _bookings(mock_server) == []


def test_released_but_unmatched_reports_missed(make_cfg, mock_server):
    # Times ARE on the sheet, but none match the preference — that's a real
    # miss ("missed"), distinct from the empty-sheet "no_release" case.
    cfg = make_cfg(["06:00 AM"], players=2)  # 06:00 isn't on the sheet
    cfg.release.retry_window_seconds = 2
    result = _run(cfg)

    assert not result.success
    assert result.outcome == "missed"
    assert _bookings(mock_server) == []
