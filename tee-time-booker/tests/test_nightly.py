"""The nightly orchestrator's play-date arithmetic.

The booker fires at 23:58, so in the evening the next 00:01 release belongs to
*tomorrow*; run after midnight it belongs to *today*. compute_play_date is the
single source of truth for that — get it wrong and the wrong day gets booked.
"""

from datetime import date, datetime

from nightly import compute_play_date


def test_evening_run_targets_tomorrow_plus_window():
    # 23:58 on Jun 27 -> next release is Jun 28 00:01 -> play Jun 28 + 14.
    now = datetime(2026, 6, 27, 23, 58)
    assert compute_play_date(now, 14) == date(2026, 7, 12)


def test_noon_counts_as_evening():
    # The boundary is hour >= 12; noon already rolls to tomorrow's release.
    now = datetime(2026, 6, 27, 12, 0)
    assert compute_play_date(now, 14) == date(2026, 7, 12)


def test_after_midnight_run_targets_today_plus_window():
    # 00:05 on Jun 28 -> this release is Jun 28 00:01 -> play Jun 28 + 14.
    now = datetime(2026, 6, 28, 0, 5)
    assert compute_play_date(now, 14) == date(2026, 7, 12)


def test_window_size_is_respected():
    now = datetime(2026, 6, 27, 23, 58)
    assert compute_play_date(now, 7) == date(2026, 7, 5)


def test_month_rollover():
    now = datetime(2026, 6, 20, 23, 0)  # evening -> release Jun 21
    assert compute_play_date(now, 14) == date(2026, 7, 5)
