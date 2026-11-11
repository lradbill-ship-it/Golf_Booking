"""A bot challenge is reported, never answered — and never mistaken for a logout.

2026-11-07..11: the 09:00 release was met by Cloudflare's "Verify you are human"
page. The profile marker was absent, so `_open_tee_sheet` concluded "logged
out", re-logged in, and the login page was the challenge too — so `page.fill`
timed out on #email and the whole run died with a bare Playwright error, four
mornings running. Nothing in the booker recognised the challenge at all.

The booker DETECTS the challenge and stops. It does not try to satisfy, solve or
evade it: that is the club's bot protection working as intended.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from tee_booker.booker import TeeBooker
from tee_booker import session as sess

CHALLENGE = ("pcc-member-booking-engine.book.teeitup.com Performing security "
             "verification This website uses a security service to protect "
             "against malicious bots. Verify you are human")
LOGGED_OUT = "Skip to content PCC Member Booking Engine Tee Times Login / Sign Up"
SIGNED_IN = "Skip to content PCC Member Booking Engine Tee Times Lane Radbill"


class _Loc:
    def __init__(self, n):
        self.n = n

    def count(self):
        return self.n


class _Page:
    def __init__(self, body, marker=False):
        self.body, self.marker = body, marker
        self.gotos, self.filled, self.clicked = [], [], []

    def inner_text(self, _sel):
        return self.body

    def goto(self, url, **kw):
        self.gotos.append(url)

    def wait_for_selector(self, sel, **kw):
        if not self.marker:
            raise TimeoutError("no marker")
        return True

    def locator(self, _sel):
        return _Loc(0)

    def fill(self, sel, value):
        self.filled.append((sel, value))

    def click(self, sel):
        self.clicked.append(sel)

    def wait_for_load_state(self, *a, **k):
        pass


def _booker(page):
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(
        club=SimpleNamespace(login_url="https://club.example.com/login",
                             tee_sheet_url="https://club.example.com/?date={date}",
                             date_url_format="%Y-%m-%d"),
        selectors={"login_success_marker": "[data-testid=core-user-profile]",
                   "time_slot": ".slot"},
        date_picker={},
    )
    b.cfg.tee_sheet_url_for = lambda d: f"https://club.example.com/?date={d}"
    b.logged = []
    b.log = lambda m: b.logged.append(m)
    b.login_calls = 0

    def fake_login(pg):
        b.login_calls += 1
    b._login = fake_login
    return b


# -- recognising it ---------------------------------------------------------

@pytest.mark.parametrize("text,blocked", [
    (CHALLENGE, True),
    ("Checking your browser before accessing", True),
    ("Attention Required! Cloudflare", True),
    ("error 1015 You are being rate limited", True),
    (SIGNED_IN, False),
    (LOGGED_OUT, False),          # a real logout is NOT a block
])
def test_is_blocked_recognises_the_challenge(text, blocked):
    assert _booker(_Page(text))._is_blocked(_Page(text)) is blocked


# -- not mistaking it for a logout ------------------------------------------

def test_a_challenge_is_not_treated_as_logged_out():
    page = _Page(CHALLENGE)
    b = _booker(page)
    b._open_tee_sheet(page, date(2026, 11, 25))
    assert b.login_calls == 0, "tried to log in through a bot challenge"
    assert any("challenging or throttling" in m for m in b.logged)


def test_a_missing_marker_alone_does_not_trigger_a_relogin():
    # The SPA simply had not painted yet. Absence of the marker proves nothing.
    page = _Page(SIGNED_IN, marker=False)
    b = _booker(page)
    b._open_tee_sheet(page, date(2026, 11, 25))
    assert b.login_calls == 0
    assert any("not re-logging in" in m for m in b.logged)


def test_a_real_logout_still_triggers_a_relogin():
    page = _Page(LOGGED_OUT, marker=False)
    b = _booker(page)
    b._open_tee_sheet(page, date(2026, 11, 25))
    assert b.login_calls == 1
    assert any("logged out" in m for m in b.logged)


def test_a_painted_signed_in_sheet_needs_no_login():
    page = _Page(SIGNED_IN, marker=True)
    b = _booker(page)
    b._open_tee_sheet(page, date(2026, 11, 25))
    assert b.login_calls == 0


# -- never typing credentials into a challenge ------------------------------

def test_login_refuses_a_challenge_page_without_typing_anything():
    page = _Page(CHALLENGE)
    cfg = SimpleNamespace(
        club=SimpleNamespace(login_url="https://club.example.com/login"),
        selectors={"username": "#email", "password": "#password",
                   "login_button": "button", "login_success_marker": "#profile"},
    )
    creds = SimpleNamespace(username="someone@example.com", password="s3cret")
    with pytest.raises(RuntimeError, match="human-verification challenge"):
        sess.login(page, cfg, creds, log=lambda _m: None)
    assert page.filled == [], "typed credentials into a bot-challenge page"
    assert page.clicked == []


def test_login_proceeds_normally_on_the_real_form():
    page = _Page(LOGGED_OUT, marker=True)
    cfg = SimpleNamespace(
        club=SimpleNamespace(login_url="https://club.example.com/login"),
        selectors={"username": "#email", "password": "#password",
                   "login_button": "button", "login_success_marker": "#profile"},
    )
    creds = SimpleNamespace(username="someone@example.com", password="s3cret")
    sess.login(page, cfg, creds, log=lambda _m: None)
    assert [f[0] for f in page.filled] == ["#email", "#password"]


@pytest.mark.parametrize("phrase", [
    "verify you are human",
    "performing security verification",
    "security service to protect",
    "checking your browser",
    "just a moment",
    "attention required",
    "error 1015",
])
def test_each_marker_is_detected_on_its_own(phrase):
    # One sentence per marker, so dropping ANY single phrase fails a test. The
    # combined sample above matched on a neighbouring phrase and let a removal
    # slip through.
    page = _Page(f"Some wrapper text. {phrase.title()}. More wrapper text.")
    assert _booker(page)._is_blocked(page) is True
