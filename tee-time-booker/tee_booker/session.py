"""Shared portal login + small URL helpers.

Both the booker and the reservations client log in the same way, so the flow
lives here in one place.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def origin_of(url: str) -> str:
    """Return the scheme://host of a URL (e.g. https://club.book.teeitup.com)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# Reported, never satisfied: these mean the site is challenging automation.
_CHALLENGE_MARKERS = (
    "verify you are human", "performing security verification",
    "security service to protect", "checking your browser", "just a moment",
    "attention required",
)


def _looks_challenged(page) -> bool:
    try:
        return any(m in (page.inner_text("body") or "").lower()
                   for m in _CHALLENGE_MARKERS)
    except Exception:  # noqa: BLE001
        return False


def describe_page(page, *, limit: int = 240) -> str:
    """A one-line description of what the page is actually showing.

    Exists because `Page.fill: Timeout ... waiting for locator("#email")` says
    only that a form was absent — it never says what was there instead. That
    bare error was the whole visible symptom of the 2026-11-07..11 Cloudflare
    block, and of the 2026-11-12 already-signed-in race, which are opposite
    problems with identical messages.
    """
    bits = []
    for name, get in (("url", lambda: page.url),
                      ("title", lambda: page.title())):
        try:
            value = get()
        except Exception:  # noqa: BLE001
            continue
        if value:
            bits.append(f"{name}={value!r}")
    try:
        text = " ".join((page.inner_text("body") or "").split())
    except Exception:  # noqa: BLE001
        text = ""
    if text:
        bits.append(f"page text begins {text[:limit]!r}")
    return "The page showed: " + ("; ".join(bits) if bits else "nothing readable.")


def login(page, cfg, creds, *, log=print) -> None:
    """Fill and submit the login form, then wait for the success marker."""
    s = cfg.selectors
    log(f"Opening login page {cfg.club.login_url}")
    page.goto(cfg.club.login_url, wait_until="domcontentloaded")
    if _looks_challenged(page):
        raise RuntimeError(
            "The login page is a Cloudflare human-verification challenge, not the "
            "sign-in form. Nothing was attempted. This is the club's bot "
            "protection; it is not something the booker works around."
        )
    marker = s.get("login_success_marker")
    user_sel = s["username"]
    # The portal is a single-page app. Opening /login with a LIVE session
    # redirects to the dashboard, and for a moment after domcontentloaded
    # neither the sign-in form nor the signed-in marker is painted yet. Reading
    # the marker right away therefore sees nothing, falls through, and waits 30s
    # to fill a form the page was never going to render — the exact
    # `Timeout ... waiting for locator("#email")` that looked like a Cloudflare
    # block on 2026-11-12. Wait for WHICHEVER arrives, then branch on the one
    # that is actually there.
    if marker:
        try:
            # 30s, matching the budget `page.fill` used to auto-wait for the
            # form on its own — this must not be less patient than the code
            # it replaces, only better at saying what it saw.
            page.wait_for_selector(f"{user_sel}, {marker}", timeout=30_000)
        except Exception:  # noqa: BLE001
            pass  # neither appeared; the fill below reports it in its own words
        try:
            form_present = page.locator(user_sel).count() > 0
            signed_in = page.locator(marker).count() > 0
        except Exception:  # noqa: BLE001
            form_present, signed_in = True, False
        # Prefer signing in when both are somehow on screen: a visible form is
        # the stronger evidence that we are not authenticated.
        if signed_in and not form_present:
            log("Already signed in (saved browser session) — no login needed.")
            return
        if not signed_in and not form_present:
            # Neither. Filling the form would now spend 30s failing and report
            # only that "#email" was missing. Say what was on screen instead —
            # and re-test for a challenge, which may have painted after the
            # check above and must reach the caller's backoff, not this branch.
            if _looks_challenged(page):
                raise RuntimeError(
                    "The login page is a Cloudflare human-verification challenge, not the "
                    "sign-in form. Nothing was attempted. This is the club's bot "
                    "protection; it is not something the booker works around."
                )
            raise RuntimeError(
                "The login page showed neither the sign-in form nor a signed-in "
                "session within 30s, so nothing was attempted. "
                + describe_page(page)
            )
    page.fill(user_sel, creds.username)
    page.fill(s["password"], creds.password)
    page.click(s["login_button"])
    if marker:
        page.wait_for_selector(marker, timeout=20_000)
    else:
        page.wait_for_load_state("networkidle")
    log("Logged in.")
