"""The auto-booker must never make more than one booking per run."""

from types import SimpleNamespace

from tee_booker.booker import TeeBooker


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

    b._find_available_slot = lambda page: (object() if find_slot else None)

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
