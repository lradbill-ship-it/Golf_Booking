"""The auto-booker must never make more than one booking per run."""

from types import SimpleNamespace

import pytest

from tee_booker.booker import TeeBooker


@pytest.mark.parametrize("label,players,expected", [
    ("1 or 2", 2, True),
    ("1 or 2", 1, True),
    ("1 or 2", 3, False),
    ("2 - 4", 2, True),
    ("2 - 4", 1, False),
    ("2 - 4", 4, True),
    ("2-4", 3, True),
    ("1", 1, True),
    ("1", 2, False),     # a single-golfer slot must be rejected for a twosome
    ("1 - 3", 2, True),
    ("1 - 3", 4, False),
    ("up to 4", 2, True),    # upper-bound-only labels must still admit a twosome
    ("up to 4", 4, True),
    ("up to 4", 5, False),
    ("max 4 players", 2, True),
    ("maximum 2", 2, True),
    ("maximum 2", 3, False),
    ("", 2, True),       # unknown label -> don't over-filter
])
def test_players_allowed(label, players, expected):
    assert TeeBooker._players_allowed(label, players) is expected


class _FakePage:
    def reload(self, **kw):
        pass


def _booker(book_returns, find_slot=True, window=0.5):
    """A TeeBooker with the Playwright-touching steps stubbed out."""
    b = TeeBooker.__new__(TeeBooker)  # bypass __init__ (no browser/config needed)
    b.cfg = SimpleNamespace(
        release=SimpleNamespace(retry_window_seconds=window, retry_interval_seconds=0.001),
        booking=SimpleNamespace(players=2),
    )
    b.log = lambda *a, **k: None
    b.book_calls = 0
    returns = list(book_returns)

    b._find_available_slot = lambda page, exclude=None: (object() if find_slot else None)

    def fake_book(page, slot):
        b.book_calls += 1
        return returns.pop(0) if returns else "retry"

    b._book_slot = fake_book
    b._slot_label_text = lambda slot: "6:30 AM"
    b._screenshot = lambda page, tag: None
    return b


def test_books_exactly_once_on_success():
    b = _booker(["booked"])
    res = b._attempt_booking(_FakePage())
    assert res.success is True
    assert b.book_calls == 1


def test_never_retries_after_unconfirmed_purchase():
    # "stop" means a purchase may have gone through — must NOT book again.
    b = _booker(["stop", "booked"])  # the 2nd value is a trap; it must stay unused
    res = b._attempt_booking(_FakePage())
    assert res.success is False
    assert b.book_calls == 1
    assert "double booking" in res.message.lower()


def test_safe_retry_before_purchase_then_books_once():
    # "retry" means nothing was purchased, so trying again is allowed —
    # but it still ends with a single successful booking.
    b = _booker(["retry", "retry", "booked"])
    res = b._attempt_booking(_FakePage())
    assert res.success is True
    assert b.book_calls == 3


def test_no_slot_never_books():
    b = _booker([], find_slot=False, window=0.05)
    res = b._attempt_booking(_FakePage())
    assert res.success is False
    assert b.book_calls == 0
    assert "no preferred time" in res.message.lower()


def test_result_reports_attempt_count():
    b = _booker(["booked"])
    res = b._attempt_booking(_FakePage())
    assert res.attempts == 1


def test_release_detected_when_cards_appear():
    # Once the sheet shows cards, the result carries the moment it released.
    b = _booker(["booked"])
    b._slot_count = lambda page: 4
    res = b._attempt_booking(_FakePage())
    assert res.release_detected_at is not None


def test_release_not_detected_while_sheet_empty():
    # No cards ever => nothing to record as a release.
    b = _booker([], find_slot=False, window=0.05)
    b._slot_count = lambda page: 0
    res = b._attempt_booking(_FakePage())
    assert res.release_detected_at is None


# -- recovery when a slot is taken out from under us at checkout -------------- #

def test_taken_slot_is_safe_to_try_next_time():
    # "taken" = the portal positively rejected the purchase (nothing bought),
    # so the booker should try again and still end with exactly one booking.
    b = _booker(["taken", "booked"], window=2)
    b._reopen = lambda page, play_date=None: None
    b._clear_cart = lambda page: None
    res = b._attempt_booking(_FakePage())
    assert res.success is True
    assert b.book_calls == 2


def test_taken_records_the_lost_label_to_skip_it():
    # The time we lost is normalized into the exclude set passed to the finder.
    b = _booker(["taken", "booked"], window=2)
    b._reopen = lambda page, play_date=None: None
    b._clear_cart = lambda page: None
    seen_excludes = []
    b._find_available_slot = lambda page, exclude=None: (
        seen_excludes.append(set(exclude or set())) or object()
    )
    b._attempt_booking(_FakePage())
    # First scan has nothing excluded; the retry scan skips the taken label.
    assert seen_excludes[0] == set()
    assert "630am" in seen_excludes[1]


def test_taken_gives_up_when_window_closes():
    # If every preferred time keeps getting taken, the run ends (no booking)
    # with a "taken" explanation rather than looping forever.
    b = _booker([], window=0.02)
    b._reopen = lambda page, play_date=None: None
    b._clear_cart = lambda page: None

    def always_taken(page, slot):
        b.book_calls += 1
        return "taken"

    b._book_slot = always_taken
    res = b._attempt_booking(_FakePage())
    assert res.success is False
    assert b.book_calls >= 1
    assert "taken" in res.message.lower()


# -- checkout outcome resolution (fast-fail) --------------------------------- #

class _CheckoutPage:
    def __init__(self, url, body=""):
        self.url = url
        self._body = body

    def inner_text(self, _sel):
        return self._body

    def wait_for_timeout(self, _ms):
        pass


def _outcome_booker(timeout_s=0.0):
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(
        checkout={"success_when_url_leaves": "/checkout", "success_timeout_seconds": timeout_s}
    )
    b.log = lambda *a, **k: None
    return b


def test_outcome_booked_when_url_leaves_checkout():
    b = _outcome_booker()
    assert b._await_checkout_outcome(_CheckoutPage("https://x/confirmation"), True) == "booked"


def test_outcome_taken_when_inventory_gone():
    b = _outcome_booker(timeout_s=5)
    page = _CheckoutPage("https://x/checkout", "The selected inventory is no longer available.")
    assert b._await_checkout_outcome(page, True) == "taken"


def test_outcome_stop_on_ambiguous_timeout_after_purchase():
    b = _outcome_booker(timeout_s=0.0)
    assert b._await_checkout_outcome(_CheckoutPage("https://x/checkout", ""), True) == "stop"


def test_outcome_retry_if_no_purchase_attempted():
    b = _outcome_booker(timeout_s=0.0)
    assert b._await_checkout_outcome(_CheckoutPage("https://x/checkout", ""), False) == "retry"


def test_inventory_unavailable_detects_marker():
    b = TeeBooker.__new__(TeeBooker)
    yes = SimpleNamespace(inner_text=lambda _s: "Please select another time to complete")
    no = SimpleNamespace(inner_text=lambda _s: "Reservation confirmed!")
    assert b._inventory_unavailable(yes) is True
    assert b._inventory_unavailable(no) is False
