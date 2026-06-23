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


def login(page, cfg, creds, *, log=print) -> None:
    """Fill and submit the login form, then wait for the success marker."""
    s = cfg.selectors
    log(f"Opening login page {cfg.club.login_url}")
    page.goto(cfg.club.login_url, wait_until="domcontentloaded")
    page.fill(s["username"], creds.username)
    page.fill(s["password"], creds.password)
    page.click(s["login_button"])
    marker = s.get("login_success_marker")
    if marker:
        page.wait_for_selector(marker, timeout=20_000)
    else:
        page.wait_for_load_state("networkidle")
    log("Logged in.")
