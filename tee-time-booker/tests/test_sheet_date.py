"""Never buy off a sheet we cannot prove is the requested day.

2026-09-30: the run asked for play date 2026-10-14 (a day the course turned out
to be CLOSED). A reopen died mid-flight, `_reopen` swallowed the error, and the
browser was left on the portal's default sheet — that night's own day. The race
read those 63 cards as Oct 14's, bought 12:40 PM on the WRONG DAY, and logged it
as "Booked 12:40 PM for 2026-10-14". The page is not evidence of which day it
shows; only its date header is.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from tee_booker.booker import TeeBooker

WANTED = date(2026, 10, 14)


# -- fakes ------------------------------------------------------------------

class _Text:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _Loc:
    def __init__(self, items):
        self._items = list(items)

    def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]

    @property
    def first(self):
        return self._items[0]


class _Button:
    """Counts real clicks, so a test can prove nothing was bought."""

    def __init__(self, tally):
        self.tally = tally

    def click(self, *a, **k):
        self.tally.append("clicked")


class _Slot:
    def __init__(self, label, tally):
        self.label, self.tally = label, tally

    def locator(self, sel):
        if sel == "button.book":
            return _Loc([_Button(self.tally)])
        if sel == ".slot-time":
            return _Loc([_Text(self.label)])
        return _Loc([])


class _Sheet:
    """A tee sheet showing `header` with `labels` bookable."""

    def __init__(self, header, labels):
        self.header, self.labels = header, labels
        self.reopened = 0
        self.clicks = []

    def locator(self, sel):
        if sel == ".sheet-date":
            return _Loc([_Text(self.header)] if self.header is not None else [])
        if sel == ".slot":
            return _Loc([_Slot(l, self.clicks) for l in self.labels])
        return _Loc([])

    def reload(self, **kw):
        pass

    def wait_for_load_state(self, *a, **k):
        pass


def _booker(page, *, configured=True, window=0.4):
    """A booker whose REAL _book_slot runs — only the browser is faked.

    Selectors deliberately omit the cart/confirm keys so _book_slot takes its
    simple path and returns "booked" after clicking; every click is tallied on
    the page, so `page.clicks` is the ground truth for "did it buy anything".
    """
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(
        release=SimpleNamespace(retry_window_seconds=window, retry_interval_seconds=0.001),
        booking=SimpleNamespace(players=2, preferred_times=["6:30 AM"], fallback=None),
        selectors={"time_slot": ".slot", "time_slot_label": ".slot-time",
                   "book_button": "button.book",
                   **({"sheet_date_label": ".sheet-date"} if configured else {})},
    )
    b.logged = []
    b.log = lambda m: b.logged.append(m)
    b._earliest_seen = None          # run() sets this; we call _poll_and_book directly
    b._screenshot = lambda pg, tag: None
    b._is_blocked = lambda pg: False
    b._is_logged_out = lambda pg: False
    b._booked_on_portal = lambda pg, d, label: False

    def reopen(pg, d):
        pg.reopened += 1
    b._reopen = reopen
    return b


# -- reading the sheet's own date -------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Oct 14, 2026", date(2026, 10, 14)),
    ("October 14, 2026", date(2026, 10, 14)),
    ("Wed, Oct 14, 2026", date(2026, 10, 14)),
    ("10/14/2026", date(2026, 10, 14)),
    ("2026-10-14", date(2026, 10, 14)),
    ("  Oct  14,  2026 ", date(2026, 10, 14)),
    ("Tee Times", None),
    ("", None),
])
def test_parse_sheet_date(text, expected):
    assert TeeBooker._parse_sheet_date(text) == expected


@pytest.mark.parametrize("header,verdict", [
    ("Oct 14, 2026", True),
    ("Sep 30, 2026", False),      # the actual 2026-09-30 failure
    ("Tee Times", None),          # unparseable
    (None, None),                 # header element absent
])
def test_sheet_date_matches(header, verdict):
    page = _Sheet(header, ["7:00 AM"])
    assert _booker(page)._sheet_date_matches(page, WANTED) is verdict


def test_unconfigured_selector_reports_unknown_not_a_match():
    page = _Sheet("Oct 14, 2026", ["7:00 AM"])
    b = _booker(page, configured=False)
    assert b._sheet_date_check_on() is False
    assert b._sheet_date_matches(page, WANTED) is None


# -- the regression: a full sheet for the WRONG day --------------------------

def test_never_books_from_another_days_sheet():
    # Exactly 2026-09-30: plenty bookable, but it is not the day we asked for.
    page = _Sheet("Sep 30, 2026", ["8:30 AM", "12:40 PM"])
    b = _booker(page)
    result = b._poll_and_book(page, WANTED)
    assert page.clicks == [], "bought a tee time on the wrong day"
    assert not result.success
    assert any("is NOT 2026-10-14" in m for m in b.logged)
    assert page.reopened > 0, "should try to recover by reopening the right date"


def test_a_wrong_day_sheet_is_not_recorded_as_the_release():
    # It also poisoned the drift history: Sep 30's 7:00 AM was logged as Oct 14's.
    page = _Sheet("Sep 30, 2026", ["7:00 AM", "12:40 PM"])
    b = _booker(page)
    b._earliest_seen = None
    result = b._poll_and_book(page, WANTED)
    assert result.release_detected_at is None
    assert b._earliest_seen is None


def test_the_click_itself_refuses_an_unconfirmable_sheet():
    page = _Sheet("Tee Times", ["7:00 AM"])   # date unreadable
    b = _booker(page)
    b._expected_play_date = WANTED
    assert b._book_slot(page, _Slot("7:00 AM", page.clicks)) == "retry"
    assert page.clicks == [], "clicked Book on a sheet it could not identify"


def test_the_right_day_still_books():
    page = _Sheet("Oct 14, 2026", ["6:30 AM"])
    b = _booker(page)
    assert b._poll_and_book(page, WANTED).success
    assert page.clicks == ["clicked"]


def test_an_unconfigured_portal_keeps_working():
    page = _Sheet(None, ["6:30 AM"])
    b = _booker(page, configured=False)
    assert b._poll_and_book(page, WANTED).success
    assert page.clicks == ["clicked"]


# -- closed course vs. times taken ------------------------------------------

def test_a_closed_day_says_so_and_books_nothing_anywhere():
    page = _Sheet("Oct 14, 2026", [])       # right day, zero tee times, all night
    b = _booker(page)
    result = b._poll_and_book(page, WANTED)
    assert not result.success and page.clicks == []
    assert "No tee times were published for 2026-10-14 at all" in result.message
    assert "likely closed" in result.message
    assert "nothing was booked on any other day" in result.message


def test_times_published_but_taken_is_a_different_message():
    # The sheet DID publish times for the right day; none was bookable for us.
    page = _Sheet("Oct 14, 2026", ["9:00 AM"])
    b = _booker(page)
    b._find_available_slot = lambda pg, exclude=None: None
    result = b._poll_and_book(page, WANTED)
    assert "WERE published for this day, so they were taken" in result.message
    assert "likely closed" not in result.message
    assert page.clicks == []


def test_an_unreadable_sheet_is_never_called_a_closure():
    # _slot_count returns -1 when it cannot count. Unreadable is not empty.
    page = _Sheet("Oct 14, 2026", [])
    b = _booker(page)
    b._slot_count = lambda pg: -1
    result = b._poll_and_book(page, WANTED)
    assert "likely closed" not in result.message
