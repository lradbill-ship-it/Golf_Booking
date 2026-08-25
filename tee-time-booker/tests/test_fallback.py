"""Falling back to the closest available time when preferred ones aren't there.

The motivating case: sunrise slides later through the season, the club stops
publishing a 6:30 AM slot, and an exact-match-only booker would book nothing on
exactly the nights the early times moved.
"""

from types import SimpleNamespace

import pytest

from tee_booker.booker import TeeBooker
from tee_booker.config import BookingConfig, ConfigError, FallbackConfig


# -- fakes standing in for Playwright locators ------------------------------

class _FakeText:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _FakeLocator:
    def __init__(self, items):
        self._items = list(items)

    def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]

    @property
    def first(self):
        return self._items[0]


class _FakeSlot:
    def __init__(self, label, bookable=True, players_label=""):
        self.label = label
        self.bookable = bookable
        self.players_label = players_label

    def locator(self, sel):
        if sel == "button.book":
            return _FakeLocator([object()] if self.bookable else [])
        if sel == ".slot-time":
            return _FakeLocator([_FakeText(self.label)])
        if sel == ".slot-players":
            return _FakeLocator([_FakeText(self.players_label)] if self.players_label else [])
        return _FakeLocator([])

    def inner_text(self):
        return self.label


class _FakePage:
    def __init__(self, slots):
        self.slots = list(slots)

    def locator(self, sel):
        assert sel == ".slot"
        return _FakeLocator(self.slots)

    def reload(self, **kw):
        pass


def _booker(preferred, slots, *, players=2, players_sel="", **fb):
    b = TeeBooker.__new__(TeeBooker)  # bypass __init__ (no browser needed)
    b.cfg = SimpleNamespace(
        booking=SimpleNamespace(
            preferred_times=preferred,
            players=players,
            fallback=FallbackConfig(**fb),
        ),
        selectors={
            "time_slot": ".slot",
            "time_slot_label": ".slot-time",
            "book_button": "button.book",
            "slot_players_label": players_sel,
        },
        release=SimpleNamespace(retry_window_seconds=0.05, retry_interval_seconds=0.001),
    )
    b.log = lambda *a, **k: None
    b._screenshot = lambda page, tag: None
    return b, _FakePage(slots)


def _pick(preferred, labels, **kw):
    """Return the label the fallback would choose, or None."""
    slots = [s if isinstance(s, _FakeSlot) else _FakeSlot(s) for s in labels]
    b, page = _booker(preferred, slots, **kw)
    found = b._find_fallback_slot(page)
    return None if found is None else found[1]


# -- time parsing -----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("6:30 AM", 390),
    ("6:30 am", 390),
    ("6:30AM", 390),
    ("7:05pm", 19 * 60 + 5),
    ("12:00 AM", 0),          # midnight, not noon
    ("12:30 PM", 12 * 60 + 30),
    ("06:30", 390),           # 24-hour label, no meridiem
    ("13:45", 13 * 60 + 45),
    ("6:30 AM  $45  2-4 players", 390),   # extra text on the card
    ("Book 6:40 AM", 400),
    ("", None),
    ("No times available", None),
    ("Sunrise", None),
])
def test_parse_time_minutes(text, expected):
    assert TeeBooker._parse_time_minutes(text) == expected


@pytest.mark.parametrize("minutes,expected", [
    (390, "6:30 AM"), (0, "12:00 AM"), (750, "12:30 PM"), (1290, "9:30 PM"),
])
def test_fmt_minutes(minutes, expected):
    assert TeeBooker._fmt_minutes(minutes) == expected


# -- picking the closest slot ----------------------------------------------

def test_sunrise_case_takes_the_earliest_time_on_the_sheet():
    # 6:30/6:40 no longer exist; the sheet now starts at 7:00.
    assert _pick(["6:30 AM", "6:40 AM"], ["7:00 AM", "7:10 AM", "7:20 AM"]) == "7:00 AM"


def test_closest_wins_over_merely_earliest():
    # 6:00 is earlier, but 7:20 is nearer to the 7:00 that was wanted.
    assert _pick(["7:00 AM"], ["6:00 AM", "7:20 AM"]) == "7:20 AM"


def test_tie_goes_to_the_earlier_slot():
    assert _pick(["7:00 AM"], ["6:40 AM", "7:20 AM"]) == "6:40 AM"


def test_distance_is_measured_to_the_nearest_preferred_time():
    # 7:30 is 10 min from the 7:20 in the list; 6:50 is 20 min from 6:30.
    assert _pick(["6:30 AM", "7:20 AM"], ["6:50 AM", "7:30 AM"]) == "7:30 AM"


def test_a_late_bound_refuses_anything_past_it():
    # Only an afternoon time left, well past an explicit +120 min bound.
    assert _pick(["6:30 AM"], ["1:00 PM"], max_minutes_later=120) is None


def test_later_bound_is_measured_from_the_last_preferred_time():
    kw = {"max_minutes_later": 120}
    assert _pick(["6:30 AM", "7:20 AM"], ["9:00 AM"], **kw) == "9:00 AM"   # 7:20 + 100
    assert _pick(["6:30 AM", "7:20 AM"], ["9:30 AM"], **kw) is None        # 7:20 + 130


def test_earlier_bound_is_respected():
    assert _pick(["7:00 AM"], ["6:05 AM"]) == "6:05 AM"    # 55 min early, inside 60
    assert _pick(["7:00 AM"], ["5:55 AM"]) is None         # 65 min early, outside


def test_bounds_are_configurable():
    assert _pick(["6:30 AM"], ["9:30 AM"], max_minutes_later=180) == "9:30 AM"
    assert _pick(["6:30 AM"], ["7:00 AM"], max_minutes_later=15) is None


def test_unbookable_slots_are_skipped():
    labels = [_FakeSlot("7:00 AM", bookable=False), _FakeSlot("7:10 AM")]
    assert _pick(["6:30 AM"], labels) == "7:10 AM"


def test_slots_that_cannot_fit_the_party_are_skipped():
    labels = [_FakeSlot("7:00 AM", players_label="1"), _FakeSlot("7:10 AM", players_label="1 or 2")]
    assert _pick(["6:30 AM"], labels, players=2, players_sel=".slot-players") == "7:10 AM"


def test_slots_lost_at_checkout_are_excluded():
    slots = [_FakeSlot("7:00 AM"), _FakeSlot("7:10 AM")]
    b, page = _booker(["6:30 AM"], slots)
    found = b._find_fallback_slot(page, exclude={TeeBooker._normalize("7:00 AM")})
    assert found[1] == "7:10 AM"


def test_unparseable_slot_labels_are_ignored():
    slots = [_FakeSlot("Closed for maintenance"), _FakeSlot("7:00 AM")]
    assert _pick(["6:30 AM"], slots) == "7:00 AM"


def test_disabled_fallback_books_nothing():
    assert _pick(["6:30 AM"], ["7:00 AM"], enabled=False) is None


def test_unparseable_preferred_times_disable_the_fallback():
    # Without a parseable anchor there is no "closest" to measure.
    assert _pick(["whenever"], ["7:00 AM"]) is None


def test_reported_offset_is_signed_minutes_from_preferred():
    b, page = _booker(["6:30 AM"], [_FakeSlot("7:00 AM")])
    assert b._find_fallback_slot(page)[2] == 30
    b, page = _booker(["7:00 AM"], [_FakeSlot("6:40 AM")])
    assert b._find_fallback_slot(page)[2] == -20


@pytest.mark.parametrize("offset,expected", [
    (30, "30 min later than preferred"),
    (-20, "20 min earlier than preferred"),
    (0, "at a preferred time"),
])
def test_describe_offset(offset, expected):
    assert TeeBooker._describe_offset(offset) == expected


# -- the gate in front of the fallback --------------------------------------

def test_preferred_time_found_on_the_recheck_wins():
    # A half-rendered sheet can hide a preferred slot for a moment; the
    # re-check must take it rather than settle for the fallback.
    b, page = _booker(["6:30 AM"], [_FakeSlot("7:00 AM")], recheck_seconds=0.01)
    preferred = object()
    b._find_available_slot = lambda page, exclude=None: preferred
    slot, note = b._consider_fallback(page, set(), elapsed=0)
    assert slot is preferred
    assert note == ""


def test_fallback_taken_when_the_recheck_still_finds_nothing():
    b, page = _booker(["6:30 AM"], [_FakeSlot("7:00 AM")], recheck_seconds=0.01)
    b._find_available_slot = lambda page, exclude=None: None
    slot, note = b._consider_fallback(page, set(), elapsed=0)
    assert slot is not None
    assert "closest available" in note and "30 min later" in note


def test_after_seconds_holds_the_fallback_back():
    b, page = _booker(["6:30 AM"], [_FakeSlot("7:00 AM")], after_seconds=60, recheck_seconds=0)
    assert b._consider_fallback(page, set(), elapsed=10)[0] is None
    assert b._consider_fallback(page, set(), elapsed=61)[0] is not None


@pytest.mark.parametrize("kw,expected", [
    ({"max_minutes_later": 120}, "5:30 AM - 9:20 AM"),
    ({}, "5:30 AM or later"),                                   # the default
    ({"max_minutes_earlier": None, "max_minutes_later": 120}, "up to 9:20 AM"),
    ({"max_minutes_earlier": None}, "any published time"),
])
def test_fallback_window_describes_each_combination_of_bounds(kw, expected):
    b, _ = _booker(["6:30 AM", "7:20 AM"], [], **kw)
    assert b.fallback_window() == expected


@pytest.mark.parametrize("preferred,kw", [
    (["6:30 AM"], {"enabled": False}),   # switched off
    (["whenever"], {}),                  # nothing to measure "closest" from
])
def test_fallback_window_is_none_when_there_is_no_fallback(preferred, kw):
    b, _ = _booker(preferred, [], **kw)
    assert b.fallback_window() is None


# -- the poll loop still books exactly once ---------------------------------

def test_poll_loop_books_the_fallback_exactly_once():
    slots = [_FakeSlot("7:00 AM"), _FakeSlot("7:10 AM")]
    b, page = _booker(["6:30 AM"], slots, recheck_seconds=0)
    b._find_available_slot = lambda page, exclude=None: None
    b._slot_count = lambda page: len(slots)
    b._is_blocked = lambda page: False
    calls = []

    def fake_book(page, slot):
        calls.append(slot)
        return "booked"

    b._book_slot = fake_book
    res = b._attempt_booking(page)
    assert res.success is True
    assert len(calls) == 1
    assert res.booked_time == "7:00 AM"
    assert "closest available" in res.message


def test_poll_loop_books_nothing_when_no_slot_is_close_enough():
    b, page = _booker(["6:30 AM"], [_FakeSlot("2:00 PM")], recheck_seconds=0,
                      max_minutes_later=120)
    b._find_available_slot = lambda page, exclude=None: None
    b._slot_count = lambda page: 1
    b._is_blocked = lambda page: False
    b._book_slot = lambda page, slot: pytest.fail("must not book an afternoon time")
    res = b._attempt_booking(page)
    assert res.success is False
    assert "fallback range (5:30 AM - 8:30 AM)" in res.message   # says what it looked at


# -- config -----------------------------------------------------------------

def test_fallback_config_parses_from_nested_yaml_mapping():
    bc = BookingConfig(
        preferred_times=["6:30 AM"],
        fallback={"enabled": False, "max_minutes_later": 45},
    )
    assert bc.fallback.enabled is False
    assert bc.fallback.max_minutes_later == 45
    assert bc.fallback.max_minutes_earlier == 60  # untouched default


def test_fallback_is_on_by_default():
    assert BookingConfig(preferred_times=["6:30 AM"]).fallback.enabled is True


def test_unknown_fallback_key_is_rejected():
    with pytest.raises(ConfigError) as exc:
        BookingConfig(preferred_times=["6:30 AM"], fallback={"max_minutes_latter": 30})
    assert "max_minutes_latter" in str(exc.value)      # names the typo
    assert "max_minutes_later" in str(exc.value)       # and the valid keys


# -- observing how far the sheet has drifted --------------------------------

def test_earliest_slot_reports_the_first_published_time():
    slots = [_FakeSlot("7:20 AM"), _FakeSlot("6:50 AM"), _FakeSlot("8:00 AM")]
    b, page = _booker(["6:30 AM"], slots)
    assert b._earliest_slot(page) == (410, "6:50 AM")


def test_earliest_slot_counts_slots_that_are_already_taken():
    # "When does the sheet start now?" is about what's published, not what's free.
    slots = [_FakeSlot("6:50 AM", bookable=False), _FakeSlot("7:20 AM")]
    b, page = _booker(["6:30 AM"], slots)
    assert b._earliest_slot(page)[1] == "6:50 AM"


def test_earliest_slot_keeps_the_best_seen_across_checks():
    b, page = _booker(["6:30 AM"], [_FakeSlot("7:20 AM")])
    assert b._earliest_slot(page, current=(410, "6:50 AM")) == (410, "6:50 AM")


def test_earliest_slot_survives_a_page_that_cannot_be_read():
    b, _ = _booker(["6:30 AM"], [])

    class _Broken:
        def locator(self, sel):
            raise RuntimeError("detached")

    assert b._earliest_slot(_Broken(), current=(390, "6:30 AM")) == (390, "6:30 AM")


def test_booking_result_carries_the_earliest_published_time():
    slots = [_FakeSlot("7:00 AM"), _FakeSlot("7:10 AM")]
    b, page = _booker(["6:30 AM"], slots, recheck_seconds=0)
    b._find_available_slot = lambda page, exclude=None: None
    b._slot_count = lambda page: len(slots)
    b._is_blocked = lambda page: False
    b._book_slot = lambda page, slot: "booked"
    assert b._attempt_booking(page).earliest_time == "7:00 AM"


def test_end_to_end_sunrise_night_books_the_new_first_tee_time():
    """The whole path with nothing stubbed but the click itself.

    Late-August sheet: 6:30/6:40 are simply not published any more, and 6:50 is
    already gone. The real matcher must find no preferred time, and the fallback
    must take 7:00 — one booking, correctly labelled.
    """
    slots = [
        _FakeSlot("6:50 AM", bookable=False),   # published but already taken
        _FakeSlot("7:00 AM"),
        _FakeSlot("7:10 AM"),
        _FakeSlot("7:20 AM"),
    ]
    b, page = _booker(["6:30 AM", "6:40 AM", "6:50 AM"], slots, recheck_seconds=0.01)
    b._slot_count = lambda page: len(slots)
    b._is_blocked = lambda page: False
    booked = []
    b._book_slot = lambda page, slot: (booked.append(b._slot_label_text(slot)), "booked")[1]

    assert b._find_available_slot(page) is None      # nothing preferred is bookable
    res = b._attempt_booking(page)

    assert res.success is True
    assert booked == ["7:00 AM"]
    assert res.booked_time == "7:00 AM"
    assert res.earliest_time == "6:50 AM"            # how far the sheet has drifted
    assert "10 min later than preferred" in res.message


# -- the always-book rule (no cap on how late) ------------------------------

def test_unbounded_late_side_takes_an_afternoon_time_rather_than_nothing():
    # The standing rule: book *something*. A 2 PM round beats no round.
    assert _pick(["6:30 AM"], ["2:00 PM"]) == "2:00 PM"


def test_unbounded_is_the_default():
    assert FallbackConfig().max_minutes_later is None


def test_unbounded_still_prefers_the_nearest_time():
    # "No cap" must not become "grab the first thing on the sheet".
    assert _pick(["7:00 AM"], ["2:00 PM", "8:15 AM", "11:30 AM"]) == "8:15 AM"


def test_unbounded_late_side_still_honours_the_early_bound():
    # Nothing stops the sheet listing a 4 AM slot; 60 min earlier is still the cap.
    assert _pick(["7:00 AM"], ["4:00 AM", "9:00 PM"]) == "9:00 PM"


def test_poll_loop_books_a_far_later_time_under_the_always_book_rule():
    slots = [_FakeSlot("1:40 PM"), _FakeSlot("3:00 PM")]
    b, page = _booker(["6:30 AM", "6:40 AM"], slots, recheck_seconds=0)
    b._find_available_slot = lambda page, exclude=None: None
    b._slot_count = lambda page: len(slots)
    b._is_blocked = lambda page: False
    booked = []
    b._book_slot = lambda page, slot: (booked.append(b._slot_label_text(slot)), "booked")[1]
    res = b._attempt_booking(page)
    assert res.success is True
    assert booked == ["1:40 PM"]
    assert "420 min later than preferred" in res.message


@pytest.mark.parametrize("bad", [{"max_minutes_later": -1}, {"max_minutes_earlier": -30}])
def test_negative_bounds_are_rejected(bad):
    with pytest.raises(ConfigError):
        FallbackConfig(**bad)


def test_zero_bound_is_allowed_and_means_no_slack():
    # 0 is a real setting (exact range only), distinct from None (no bound).
    assert _pick(["6:30 AM"], ["6:31 AM"], max_minutes_later=0) is None
