"""The nightly orchestrator's play-date arithmetic.

The booker fires at 23:58, so in the evening the next 00:01 release belongs to
*tomorrow*; run after midnight it belongs to *today*. compute_play_date is the
single source of truth for that — get it wrong and the wrong day gets booked.
"""

from datetime import date, datetime, timedelta

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


# -- a release time that MOVED ----------------------------------------------
#
# PCC ran a 00:01 release for months, then moved to 09:00 in Oct 2026 and the
# midnight sheet simply stopped appearing. The old rule ("after noon means
# tomorrow's release") was written for a 23:58 job and silently mistargets a
# morning one, so the play date now follows the configured release time.

from nightly import MAX_PRERELEASE_WAIT_SECONDS, too_long_to_wait  # noqa: E402

NINE = "09:00"


def test_morning_job_targets_todays_release():
    # The job fires at 08:55 for a 09:00 release: that is TODAY's sheet.
    now = datetime(2026, 10, 18, 8, 55)
    assert compute_play_date(now, 14, NINE) == date(2026, 11, 1)


def test_a_release_that_just_passed_still_belongs_to_this_run():
    # Started late (or still polling at 09:30) — same sheet, not tomorrow's.
    now = datetime(2026, 10, 18, 9, 30)
    assert compute_play_date(now, 14, NINE, grace_seconds=3540) == date(2026, 11, 1)


def test_well_after_the_release_rolls_to_tomorrow():
    now = datetime(2026, 10, 18, 18, 13)      # evening: next release is tomorrow 09:00
    assert compute_play_date(now, 14, NINE, grace_seconds=3540) == date(2026, 11, 2)


def test_the_grace_boundary():
    release = datetime(2026, 10, 18, 9, 0)
    inside = release + timedelta(seconds=3540)
    outside = release + timedelta(seconds=3541)
    assert compute_play_date(inside, 14, NINE, grace_seconds=3540) == date(2026, 11, 1)
    assert compute_play_date(outside, 14, NINE, grace_seconds=3540) == date(2026, 11, 2)


def test_the_old_midnight_release_is_unchanged():
    # The previous behaviour must still hold for a club that releases at 00:01.
    assert compute_play_date(datetime(2026, 6, 27, 23, 58), 14, "00:01") == date(2026, 7, 12)
    assert compute_play_date(datetime(2026, 6, 28, 0, 5), 14, "00:01") == date(2026, 7, 12)
    assert compute_play_date(datetime(2026, 6, 27, 12, 0), 14, "00:01") == date(2026, 7, 12)


def test_a_malformed_release_time_is_refused_not_guessed():
    # Guessing the release time mistargets the play date by a whole day, which
    # is exactly what this function exists to prevent.
    import pytest
    with pytest.raises(ValueError, match="release_time"):
        compute_play_date(datetime(2026, 10, 18, 8, 55), 14, "not a time")


def test_config_rejects_a_bad_release_time_at_load(tmp_path):
    import pytest
    from tee_booker.config import Config, ConfigError
    from pathlib import Path as _P
    good = (_P(__file__).resolve().parent.parent / "config.example.yaml").read_text()
    bad = tmp_path / "config.yaml"
    bad.write_text(good.replace('release_time: "09:00"', 'release_time: "9am"'))
    with pytest.raises(ConfigError, match="release_time"):
        Config.load(str(bad))


def test_a_job_replayed_hours_late_exits_rather_than_waiting():
    now = datetime(2026, 10, 18, 14, 0)
    tomorrow_login = datetime(2026, 10, 19, 8, 59)
    assert too_long_to_wait(tomorrow_login, now) is True
    # the normal case: 08:55 fires for a 09:00 release
    assert too_long_to_wait(datetime(2026, 10, 18, 8, 59),
                            datetime(2026, 10, 18, 8, 55)) is False
    assert MAX_PRERELEASE_WAIT_SECONDS <= 6 * 3600


def test_it_reads_the_release_time_not_the_clocks_noon():
    # Discriminating case: the OLD rule was "after noon means tomorrow". For a
    # club releasing at 13:00, 12:30 is BEFORE the release, so it is still
    # today's sheet. The morning and evening cases above agree with the old rule
    # by coincidence; this one does not, so it is what pins the new behaviour.
    assert compute_play_date(datetime(2026, 10, 18, 12, 30), 14, "13:00") == date(2026, 11, 1)
    assert compute_play_date(datetime(2026, 10, 18, 11, 0), 14, "10:00",
                             grace_seconds=600) == date(2026, 11, 2)
