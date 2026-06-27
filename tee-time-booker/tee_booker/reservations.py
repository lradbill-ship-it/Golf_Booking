"""List and cancel reservations on the TeeItUp / Kenna portal.

Listing works by logging in, opening the Reservations page, and capturing the
backend JSON the page fetches for itself (host *.kenna.io,
`/reservation/history?playDateMin=...`). That avoids re-implementing the
portal's auth and avoids scraping a detail page per reservation.

Cancelling works by navigating to `/reservation/history/{id}/cancel`, which the
portal processes immediately (there is no extra confirm step on their side —
so callers must do their own confirmation before calling this).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import Config, Credentials
from .session import login, origin_of


@dataclass
class Reservation:
    id: int
    confirmation: str
    status: str            # "Confirmed" or "Cancelled"
    cancelled: bool
    eligible_cancel: bool
    time_iso: Optional[str]
    players: Optional[int]
    holes: Optional[int]

    @property
    def when(self) -> Optional[datetime]:
        if not self.time_iso:
            return None
        try:
            return datetime.fromisoformat(self.time_iso)
        except ValueError:
            return None

    @property
    def date_label(self) -> str:
        w = self.when
        return w.strftime("%a, %b %-d %Y") if w else "—"

    @property
    def time_label(self) -> str:
        w = self.when
        return w.strftime("%-I:%M %p") if w else "—"


def _parse(payload: dict) -> list[Reservation]:
    raw = ((payload or {}).get("reservations") or {}).get("Reservations") or []
    out: list[Reservation] = []
    for r in raw:
        inv = r.get("Invoice") or {}
        status_code = r.get("Status")
        out.append(
            Reservation(
                id=r.get("ReservationID"),
                confirmation=r.get("ConfirmationNumber") or "",
                status="Cancelled" if status_code == 0 else "Confirmed",
                cancelled=status_code == 0,
                eligible_cancel=bool(r.get("EligibleForCancellation")),
                time_iso=inv.get("Time"),
                players=inv.get("PlayerCount"),
                holes=inv.get("HoleCount"),
            )
        )
    out.sort(key=lambda x: x.time_iso or "")
    return out


def fetch_reservations(cfg: Config, creds: Credentials, *, log=print) -> list[Reservation]:
    """Return upcoming reservations (soonest first)."""
    from playwright.sync_api import sync_playwright

    base = origin_of(cfg.club.login_url)
    captured: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.runtime.headless)
        page = browser.new_context().new_page()

        def on_response(resp):
            u = resp.url
            if (
                "kenna.io" in u
                and "/reservation/history" in u
                and "playDateMin" in u
                and resp.request.method == "GET"
            ):
                try:
                    captured["data"] = resp.json()
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        try:
            login(page, cfg, creds, log=log)
            page.goto(f"{base}/reservation/history", wait_until="domcontentloaded")
            # Give the page a moment to issue (and us to capture) the API call.
            for _ in range(20):
                if "data" in captured:
                    break
                page.wait_for_timeout(300)
        finally:
            browser.close()

    return _parse(captured.get("data") or {})


def _select_mui_option(page, want: str | None) -> None:
    """Pick an option from an already-open MUI Select menu.

    If `want` is given, choose the option whose text equals it (falling back to
    position); otherwise choose the first option.
    """
    options = page.locator('[role="option"]')
    options.first.wait_for(timeout=10_000)
    if want is None:
        options.first.click()
        return
    count = options.count()
    for i in range(count):
        if options.nth(i).inner_text().strip() == str(want):
            options.nth(i).click()
            return
    options.last.click()


def cancel_reservation(
    cfg: Config, creds: Credentials, reservation_id, players_to_cancel, *,
    reason: str = "Other", log=print
) -> bool:
    """Cancel `players_to_cancel` players on a reservation. True if confirmed.

    The portal's cancel form (Number of players + Reason + Submit) only renders
    when reached through the app, so we open the reservation's detail page and
    click "Cancel or Modify" — loading the /cancel URL directly does not work.
    Cancellation is irreversible; confirm with the user before calling.
    """
    from playwright.sync_api import sync_playwright

    base = origin_of(cfg.club.login_url)
    ok = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.runtime.headless)
        page = browser.new_context().new_page()
        try:
            login(page, cfg, creds, log=log)
            log(f"Cancelling {players_to_cancel} player(s) on reservation {reservation_id} ...")
            page.goto(
                f"{base}/reservation/history/{reservation_id}/details",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector(
                '[data-testid="res-history-cancel-modify-button"]', timeout=20_000
            )
            page.click('[data-testid="res-history-cancel-modify-button"]')
            page.wait_for_selector(
                '[data-testid="cancellation-request-submit-button"]', timeout=20_000
            )

            # Number of players to cancel (MUI Select).
            try:
                page.click('[data-testid="cancellation-request-players-to-cancel-input"]')
                _select_mui_option(page, str(players_to_cancel))
            except Exception as exc:  # noqa: BLE001
                log(f"Couldn't set players-to-cancel ({exc}).")

            # Reason for cancellation (required by the portal). Always "Other"
            # so we never have to ask — "Other" is one of the menu options.
            try:
                page.click('[data-testid="cancellation-request-reson-for-cancellation-input"]')
                _select_mui_option(page, reason)
            except Exception as exc:  # noqa: BLE001
                log(f"Couldn't set cancellation reason ({exc}).")

            page.click('[data-testid="cancellation-request-submit-button"]')
            try:
                page.wait_for_function(
                    "() => /successfully received your cancellation|your cancellation request/i"
                    ".test(document.body.innerText)",
                    timeout=20_000,
                )
                ok = True
            except Exception:  # noqa: BLE001
                ok = "successfully received" in (page.inner_text("body") or "").lower()
        finally:
            browser.close()
    log(f"Cancellation {'succeeded' if ok else 'could not be confirmed'}.")
    return ok
