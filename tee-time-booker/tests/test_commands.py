from datetime import date

from tee_booker.commands import parse_command

# Fixed reference day: Tuesday, 2026-06-23.
TODAY = date(2026, 6, 23)


def iso(*ds):
    return [date(*d).isoformat() for d in ds]


def test_cancel_with_month_name():
    c = parse_command("cancel my reservation on June 30th", TODAY)
    assert c.action == "cancel"
    assert c.iso_dates == ["2026-06-30"]


def test_cancel_without_date_is_unknown():
    c = parse_command("cancel my reservation", TODAY)
    assert c.action == "unknown"


def test_skip_this_thursday():
    c = parse_command("don't book this Thursday", TODAY)
    assert c.action == "skip"
    assert c.iso_dates == ["2026-06-25"]  # soonest Thursday >= Tue Jun 23


def test_skip_next_thursday():
    c = parse_command("skip next Thursday", TODAY)
    assert c.action == "skip"
    assert c.iso_dates == ["2026-07-02"]


def test_skip_this_week_and_thursday():
    c = parse_command("Don't make any reservations this week or this Thursday", TODAY)
    assert c.action == "skip"
    # this week = today .. Sunday (Jun 23–28); Thursday (25) already inside
    assert c.iso_dates == iso((2026, 6, 23), (2026, 6, 24), (2026, 6, 25),
                              (2026, 6, 26), (2026, 6, 27), (2026, 6, 28))


def test_skip_this_weekend():
    c = parse_command("no reservations this weekend", TODAY)
    assert c.action == "skip"
    assert c.iso_dates == ["2026-06-27", "2026-06-28"]  # Sat, Sun


def test_skip_numeric_and_iso_dates():
    assert parse_command("skip 7/8", TODAY).iso_dates == ["2026-07-08"]
    assert parse_command("skip 2026-07-09", TODAY).iso_dates == ["2026-07-09"]


def test_pause_and_arm_global():
    assert parse_command("pause everything", TODAY).action == "pause"
    assert parse_command("resume", TODAY).action == "arm"
    assert parse_command("arm the booker", TODAY).action == "arm"


def test_unskip_with_date():
    c = parse_command("unskip June 30", TODAY)
    assert c.action == "unskip"
    assert c.iso_dates == ["2026-06-30"]


def test_tomorrow():
    assert parse_command("skip tomorrow", TODAY).iso_dates == ["2026-06-24"]


def test_gibberish_is_unknown():
    assert parse_command("make me a sandwich", TODAY).action == "unknown"


def test_past_month_date_rolls_to_next_year():
    # On Jul 1, "June 30" already passed this year -> next year.
    c = parse_command("cancel June 30", date(2026, 7, 1))
    assert c.iso_dates == ["2027-06-30"]
