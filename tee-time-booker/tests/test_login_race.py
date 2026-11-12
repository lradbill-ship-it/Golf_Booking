"""Opening /login with a LIVE session must not wait 30s to fill a missing form.

2026-11-12: the portal is a single-page app. With a live session, /login
redirects to the dashboard, and for a moment after `domcontentloaded` neither
the sign-in form nor the signed-in marker is painted. The old code read the
marker once, saw nothing, fell through, and spent 30 seconds waiting to fill
`#email` on a page that was never going to render one.

The resulting error — `Page.fill: Timeout ... waiting for locator("#email")` —
is byte-for-byte the error the Cloudflare block produced on 2026-11-07..11.
Two opposite problems, one indistinguishable message. These tests pin both the
fix and the diagnostics that tell them apart.
"""

from types import SimpleNamespace

import pytest

from tee_booker.session import describe_page, login

FORM = "#email"
MARKER = "[data-testid='core-user-profile']"


class _Loc:
    def __init__(self, n):
        self.n = n

    def count(self):
        return self.n


class _Page:
    def __init__(self, *, form=False, signed_in=False, body="", title="PCC",
                 body_after=None):
        self.form, self.signed_in = form, signed_in
        self.body, self._title = body, title
        # What the body becomes after the FIRST read, so a test can make a
        # challenge paint late — after login() has already looked once.
        self.body_after = body_after
        self.body_reads = 0
        self.url = "https://pcc.example/login"
        self.filled, self.clicked, self.waited = [], [], []

    def goto(self, url, **kw):
        self.url = url

    def title(self):
        return self._title

    def inner_text(self, sel):
        self.body_reads += 1
        if self.body_after is not None and self.body_reads > 1:
            return self.body_after
        return self.body

    def locator(self, sel):
        if sel == FORM:
            return _Loc(1 if self.form else 0)
        if sel == MARKER:
            return _Loc(1 if self.signed_in else 0)
        return _Loc(0)

    def wait_for_selector(self, sel, **kw):
        self.waited.append(sel)

    def fill(self, sel, value):
        self.filled.append(sel)

    def click(self, sel):
        self.clicked.append(sel)

    def wait_for_load_state(self, *a, **k):
        pass


def _cfg():
    return SimpleNamespace(
        club=SimpleNamespace(login_url="https://pcc.example/login"),
        selectors={"username": FORM, "password": "#password",
                   "login_button": "button[type=submit]",
                   "login_success_marker": MARKER},
    )


_CREDS = SimpleNamespace(username="u", password="p")


def _login(page):
    login(page, _cfg(), _CREDS, log=lambda m: None)


def test_a_live_session_skips_the_form_instead_of_timing_out_on_it():
    page = _Page(signed_in=True)
    _login(page)
    assert page.filled == [], "typed into a form that was not there"
    assert page.clicked == []


def test_a_logged_out_page_still_signs_in_normally():
    page = _Page(form=True)
    _login(page)
    assert page.filled == [FORM, "#password"]
    assert page.clicked == ["button[type=submit]"]


def test_when_both_appear_it_signs_in_rather_than_assuming_a_session():
    # A visible form is the stronger evidence that we are NOT authenticated.
    page = _Page(form=True, signed_in=True)
    _login(page)
    assert page.filled == [FORM, "#password"]


def test_neither_form_nor_session_reports_what_the_page_showed():
    page = _Page(body="Site under maintenance, back at noon", title="Maintenance")
    with pytest.raises(RuntimeError) as err:
        _login(page)
    msg = str(err.value)
    assert "neither the sign-in form nor a signed-in session" in msg
    assert "Maintenance" in msg or "under maintenance" in msg, \
        "the error must say what WAS there, not only what was missing"
    assert page.filled == [], "must not spend 30s filling a form that is absent"


def test_a_challenge_that_paints_late_is_still_reported_as_a_challenge():
    # LATE means late: the page is clean when login() first looks, and only
    # then turns into the challenge. The up-front check cannot catch this one,
    # so only the second look can — and it must, because the challenge takes
    # the caller's backoff path while the generic error does not.
    page = _Page(body="", body_after="Verify you are human before continuing")
    with pytest.raises(RuntimeError) as err:
        _login(page)
    assert "human-verification challenge" in str(err.value)
    assert page.filled == []


def test_the_late_challenge_fake_is_genuinely_late():
    # Guards the test above from going vacuous: if the challenge text were
    # visible on the first read, the up-front check would catch it and the
    # second look would never be exercised.
    page = _Page(body="", body_after="Verify you are human")
    assert page.inner_text("body") == ""
    assert "human" in page.inner_text("body")


def test_a_challenge_present_up_front_is_unchanged():
    page = _Page(body="Just a moment ... checking your browser")
    with pytest.raises(RuntimeError) as err:
        _login(page)
    assert "human-verification challenge" in str(err.value)


def test_describe_page_names_url_title_and_text():
    page = _Page(body="Attention Required! Cloudflare", title="Access denied")
    page.url = "https://pcc.example/login"
    out = describe_page(page)
    assert "pcc.example/login" in out
    assert "Access denied" in out
    assert "Cloudflare" in out


def test_describe_page_survives_a_page_that_answers_nothing():
    class _Dead:
        @property
        def url(self):
            raise RuntimeError("gone")

        def title(self):
            raise RuntimeError("gone")

        def inner_text(self, sel):
            raise RuntimeError("gone")

    assert "nothing readable" in describe_page(_Dead())
