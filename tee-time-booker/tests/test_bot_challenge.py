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
    def __init__(self, body, marker=False, form=True):
        self.body, self.marker, self.form = body, marker, form
        self.gotos, self.filled, self.clicked = [], [], []

    def inner_text(self, _sel):
        return self.body

    def goto(self, url, **kw):
        self.gotos.append(url)

    def wait_for_selector(self, sel, **kw):
        if not self.marker:
            raise TimeoutError("no marker")
        return True

    def locator(self, sel):
        # The sign-in form is present on a real logged-out login page; saying
        # otherwise made this fake a page with no form, which is a different
        # situation entirely.
        return _Loc(1 if (self.form and sel == "#email") else 0)

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


# -- waiting it out, never answering it -------------------------------------
#
# The member's club confirmed they are fine with an autobooker but cannot help
# with Cloudflare. So the booker steps back and asks again later, the way a
# person reloading would, until the challenge stops being shown. It never
# answers, solves or evades it.

from datetime import date as _date  # noqa: E402

from tee_booker import booker as _bk  # noqa: E402


class _PollPage:
    def reload(self, **kw):
        pass


class _Clock:
    """A fake clock that only moves when the code sleeps.

    Stubbing sleep to a no-op would remove the very time advance the backoff
    cap depends on, so the "it gives up" tests would pass for the wrong reason.
    """

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def monotonic(self):
        self.t += 0.0001          # a hair, so loops still make progress
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def _poll_booker(blocked_for, *, backoff=900.0, cap=14400.0):
    """A booker whose page is challenged for the first `blocked_for` checks."""
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(
        release=SimpleNamespace(retry_window_seconds=3540.0, retry_interval_seconds=1.0,
                                challenge_backoff_seconds=backoff,
                                challenge_max_total_seconds=cap),
        booking=SimpleNamespace(players=2, preferred_times=["8:30 AM"], fallback=None),
        selectors={"time_slot": ".slot", "sheet_date_label": ".d"},
    )
    b.logged = []
    b.log = lambda m: b.logged.append(m)
    b.checks = {"n": 0}
    b._screenshot = lambda pg, tag: None
    b._is_logged_out = lambda pg: False
    b._earliest_seen = None
    b._earliest_slot = lambda pg, cur=None: (510, "8:30 AM")
    b._slot_count = lambda pg: 3
    b._sheet_date_matches = lambda pg, d: True
    b._sheet_date_check_on = lambda: True
    b._slot_label_text = lambda slot: "8:30 AM"
    b._find_available_slot = lambda pg, exclude=None: object()
    b._book_slot = lambda pg, slot: "booked"
    b._reopen = lambda pg, d: None

    def is_blocked(pg):
        b.checks["n"] += 1
        return b.checks["n"] <= blocked_for
    b._is_blocked = is_blocked
    return b


def _run(b, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(_bk.time, "sleep", clock.sleep)
    monkeypatch.setattr(_bk.time, "monotonic", clock.monotonic)
    b.clock = clock
    return b._poll_and_book(_PollPage(), _date(2026, 11, 28))


def test_a_challenge_is_waited_out_then_it_books(monkeypatch):
    b = _poll_booker(blocked_for=3)
    result = _run(b, monkeypatch)
    assert result.success, "did not book once the challenge lifted"
    assert result.challenges == 3
    assert result.cleared_after_seconds is not None
    # Three 15-minute backoffs, not three fast retries.
    assert [s for s in b.clock.slept if s >= 900.0] == [900.0, 900.0, 900.0]
    assert any("NOT answering it" in m for m in b.logged)
    assert any("stopped challenging" in m for m in b.logged)


def test_it_never_claims_to_have_answered_the_challenge(monkeypatch):
    b = _poll_booker(blocked_for=2)
    _run(b, monkeypatch)
    joined = " ".join(b.logged).lower()
    for word in ("solv", "bypass", "evad", "captcha"):
        assert word not in joined


def test_a_challenge_that_never_lifts_gives_up_and_says_so(monkeypatch):
    b = _poll_booker(blocked_for=10_000, cap=3600.0)   # 1h cap, 15-min gaps
    result = _run(b, monkeypatch)
    assert not result.success
    assert "never let us through" in result.message
    assert "not answered or worked around" in result.message
    assert result.challenges >= 1


def test_the_wait_is_bounded(monkeypatch):
    # It must not hammer the site all day: a 1h cap at 15-min gaps is ~4 tries.
    b = _poll_booker(blocked_for=10_000, cap=3600.0)
    _run(b, monkeypatch)
    assert len(b.clock.slept) <= 5, f"kept retrying past the cap: {b.clock.slept}"
    assert sum(b.clock.slept) <= 3600.0


# -- the fallback must FIX the problem, not land on the broken path ----------

@pytest.mark.parametrize("endpoint,port", [
    ("http://localhost:9222", 9222),
    ("http://127.0.0.1:9333/", 9333),
    ("http://localhost", None),
    ("", None),
])
def test_cdp_port_is_parsed(endpoint, port):
    assert TeeBooker._cdp_port(endpoint) == port


def test_the_debug_chrome_command_always_uses_a_dedicated_profile():
    # Chrome 136+ refuses --remote-debugging-port on the DEFAULT profile, so a
    # command without --user-data-dir would silently never open the port.
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(runtime=SimpleNamespace(user_data_dir="state/chrome-booking-profile"))
    cmd = b._debug_chrome_command("http://localhost:9222")
    assert "--remote-debugging-port=9222" in cmd
    assert any(a.startswith("--user-data-dir=") for a in cmd)
    assert any(a.endswith("state/chrome-booking-profile") for a in cmd)
    # and an absolute path, since the job runs from launchd
    profile = [a for a in cmd if a.startswith("--user-data-dir=")][0].split("=", 1)[1]
    assert profile.startswith("/")


def test_no_command_without_a_profile_dir():
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(runtime=SimpleNamespace(user_data_dir=None))
    assert b._debug_chrome_command("http://localhost:9222") is None


def test_a_degraded_run_says_so_in_its_message():
    from tee_booker.booker import BookingResult
    r = BookingResult(True, booked_time="8:30 AM", message="Booked 8:30 AM for 2 players.")
    assert r.degraded is False          # default must be the good path
    r.degraded = True
    r.message = "[DEGRADED: used our own browser, not your Chrome] " + r.message
    assert "DEGRADED" in r.message
