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
    if marker:
        try:
            if page.locator(marker).count() > 0:
                log("Already signed in (saved browser session) — no login needed.")
                return
        except Exception:  # noqa: BLE001
            pass
    page.fill(s["username"], creds.username)
    page.fill(s["password"], creds.password)
    page.click(s["login_button"])
    if marker:
        page.wait_for_selector(marker, timeout=20_000)
    else:
        page.wait_for_load_state("networkidle")
    log("Logged in.")
