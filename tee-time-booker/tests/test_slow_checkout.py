"""A submitted purchase the portal is still processing, and who decides the truth.

From 2026-09-23, six nights in a row died the same way: the booker clicked buy,
the portal sat on "Processing cart items ( 0 of 1 ) ...", and the 20-second
timeout expired. Nothing was booked and the run reported an ambiguous failure.
Two rules come out of that:
  * the portal saying it is still working is a reason to WAIT, not to walk away;
  * a checkout we could not confirm is settled by the member's reservation
    list — the primary record — not by the page we happened to be looking at.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from tee_booker.booker import BookingResult, TeeBooker


class _Loc:
    def __init__(self, items):
        self._items = list(items)

    def count(self):
        return len(self._items)

    @property
    def first(self):
        return self._items[0]

PROCESSING = "Processing cart items ( 0 of 1 ) ..."
PLAY_DATE = date(2026, 10, 13)


class _CheckoutPage:
    """A checkout page whose body/url can change after N polls."""

    def __init__(self, url, body="", becomes=None, after=0):
        self.url, self._body = url, body
        self._becomes, self._after, self.polls = becomes, after, 0

    def inner_text(self, _sel):
        return self._body

    def wait_for_timeout(self, _ms):
        self.polls += 1
        if self._becomes and self.polls >= self._after:
            self.url, self._body = self._becomes

    def goto(self, *a, **k):
        pass

    def locator(self, _sel):            # the sheet's date header, matching PLAY_DATE
        return _Loc([SimpleNamespace(inner_text=lambda: "Oct 13, 2026")])

    def on(self, *a, **k):
        pass

    def remove_listener(self, *a, **k):
        pass


def _booker(timeout_s=0.05, patience_s=5.0):
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(checkout={
        "success_when_url_leaves": "/checkout",
        "success_timeout_seconds": timeout_s,
        "processing_timeout_seconds": patience_s,
    })
    b.logged = []
    b.log = lambda m: b.logged.append(m)
    return b


# -- the processing signal --------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    (PROCESSING, True),
    ("PROCESSING CART ITEMS ( 0 of 1 )", True),
    ("Completing your booking...", True),
    ("Reservation confirmed!", False),
    ("", False),
])
def test_processing_marker(text, expected):
    b = TeeBooker.__new__(TeeBooker)
    assert b._is_processing(SimpleNamespace(inner_text=lambda _s: text)) is expected


def test_waits_past_the_short_timeout_while_the_portal_is_processing():
    # The exact failure: still "processing" when the 20s timeout would expire,
    # then the portal finishes. Must come back "booked", not "stop".
    b = _booker(timeout_s=0.05, patience_s=5.0)
    page = _CheckoutPage("https://x/checkout", PROCESSING,
                         becomes=("https://x/confirmation", "Reservation confirmed!"), after=4)
    assert b._await_checkout_outcome(page, True) == "booked"
    assert any("still processing" in m for m in b.logged)


def test_gives_up_when_processing_never_finishes():
    b = _booker(timeout_s=0.05, patience_s=0.2)
    page = _CheckoutPage("https://x/checkout", PROCESSING)
    assert b._await_checkout_outcome(page, True) == "stop"
    assert any("Still not confirmed" in m for m in b.logged)


def test_processing_does_not_extend_when_no_purchase_was_attempted():
    # Nothing was submitted, so there is nothing to be patient about.
    b = _booker(timeout_s=0.05, patience_s=30.0)
    page = _CheckoutPage("https://x/checkout", PROCESSING)
    assert b._await_checkout_outcome(page, False) == "retry"
    assert not any("still processing" in m for m in b.logged)


def test_a_slot_taken_mid_processing_still_resolves_fast():
    b = _booker(timeout_s=5.0, patience_s=30.0)
    page = _CheckoutPage("https://x/checkout", "no longer available")
    assert b._await_checkout_outcome(page, True) == "taken"


# -- the primary record decides --------------------------------------------

def _res(iso_dt, cancelled=False):
    return {"ReservationID": 1, "ConfirmationNumber": "c", "Status": 0 if cancelled else 1,
            "EligibleForCancellation": True,
            "Invoice": {"Time": iso_dt, "PlayerCount": 2, "HoleCount": 18}}


def _portal_booker(payload, *, capture=True):
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(club=SimpleNamespace(login_url="https://club.example.com/login"))
    b.log = lambda *a, **k: None
    page = _CheckoutPage("https://x/checkout")

    def on(event, handler):
        if capture and event == "response":
            handler(SimpleNamespace(
                url="https://phx-api.kenna.io/reservation/history?playDateMin=2026-10-13",
                request=SimpleNamespace(method="GET"), json=lambda: payload))
    page.on = on
    return b, page


def test_portal_says_booked():
    b, page = _portal_booker({"reservations": {"Reservations": [_res("2026-10-13T07:30:00")]}})
    assert b._booked_on_portal(page, PLAY_DATE, "7:30 AM") is True


@pytest.mark.parametrize("payload", [
    {"reservations": {"Reservations": []}},                                   # nothing at all
    {"reservations": {"Reservations": [_res("2026-10-13T09:40:00")]}},        # another time
    {"reservations": {"Reservations": [_res("2026-10-14T07:30:00")]}},        # another date
    {"reservations": {"Reservations": [_res("2026-10-13T07:30:00", True)]}},  # cancelled
])
def test_portal_says_not_booked(payload):
    b, page = _portal_booker(payload)
    assert b._booked_on_portal(page, PLAY_DATE, "7:30 AM") is False


def test_unreadable_list_is_none_not_false():
    # An empty read must never be reported as "nothing was booked".
    b, page = _portal_booker(None, capture=False)
    assert b._booked_on_portal(page, PLAY_DATE, "7:30 AM") is None


# -- what the run reports ---------------------------------------------------

def _run_booker(verdict):
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(
        release=SimpleNamespace(retry_window_seconds=0.5, retry_interval_seconds=0.001),
        booking=SimpleNamespace(players=2),
        selectors={"sheet_date_label": ".sheet-date"},
    )
    b.log = lambda *a, **k: None
    b.book_calls = 0
    b._find_available_slot = lambda page, exclude=None: object()
    b._slot_label_text = lambda slot: "7:30 AM"
    b._screenshot = lambda page, tag: f"shot-{tag}.png"
    b._slot_count = lambda page: 5
    b._is_blocked = lambda page: False
    b._is_logged_out = lambda page: False
    b._earliest_slot = lambda page, current=None: (450, "7:30 AM")
    b._booked_on_portal = lambda page, d, label: verdict
    b._earliest_seen = None          # run() sets this; we call _poll_and_book directly

    def fake_book(page, slot):
        b.book_calls += 1
        return "stop"
    b._book_slot = fake_book
    return b


def test_a_slow_checkout_that_really_booked_is_reported_as_success():
    b = _run_booker(True)
    result = b._poll_and_book(_CheckoutPage("https://x/checkout"), PLAY_DATE)
    assert result.success and result.booked_time == "7:30 AM"
    assert "reservation list" in result.message
    assert b.book_calls == 1


def test_a_verified_miss_reports_what_was_seen_and_does_not_retry():
    # The read rules a purchase IN with certainty and OUT only on the evidence
    # available, so the wording is hedged and the no-retry rule stands either way.
    b = _run_booker(False)
    result = b._poll_and_book(_CheckoutPage("https://x/checkout"), PLAY_DATE)
    assert not result.success
    assert "showed no booking" in result.message and "checked seconds later" in result.message
    assert b.book_calls == 1          # the no-retry guarantee is untouched


def test_an_unreadable_list_stays_honestly_unknown():
    b = _run_booker(None)
    result = b._poll_and_book(_CheckoutPage("https://x/checkout"), PLAY_DATE)
    assert not result.success
    assert "unknown whether anything was bought" in result.message
    assert "nothing was bought" not in result.message
    assert b.book_calls == 1


def test_the_screenshot_is_taken_before_navigating_away_to_verify():
    b = _run_booker(True)
    result = b._poll_and_book(_CheckoutPage("https://x/checkout"), PLAY_DATE)
    assert result.screenshot == "shot-unconfirmed.png"


# -- driving the portal like a real browser ---------------------------------

class _Route:
    def __init__(self, url="https://x/a.png", rtype="image"):
        self.request = SimpleNamespace(url=url, resource_type=rtype)
        self.action = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


def _router(block):
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(runtime=SimpleNamespace(block_resources=block))
    return b


def test_resources_are_blocked_by_default():
    r = _Route()
    _router(True)._maybe_block(r)
    assert r.action == "abort"


def test_nothing_is_blocked_when_the_switch_is_off():
    # Lets the booker drive checkout the way the member's own browser does.
    for rtype in ("image", "font", "media", "script"):
        r = _Route(rtype=rtype)
        _router(False)._maybe_block(r)
        assert r.action == "continue", rtype


def test_analytics_hosts_still_blocked_only_when_switched_on():
    r = _Route(url="https://browser-intake-datadoghq.com/api/v2/rum", rtype="xhr")
    _router(True)._maybe_block(r)
    assert r.action == "abort"
    r2 = _Route(url="https://browser-intake-datadoghq.com/api/v2/rum", rtype="xhr")
    _router(False)._maybe_block(r2)
    assert r2.action == "continue"
