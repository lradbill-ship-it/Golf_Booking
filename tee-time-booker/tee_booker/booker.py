"""The Playwright-driven booking flow.

This is the club-specific part. The flow is generic — log in, open the tee
sheet for the play date, find the first acceptable time, book it, confirm — but
the actual element selectors live in config.yaml so it can be adapted to any
portal without code changes.

Playwright is imported lazily inside `run()` so the rest of the package
(config, scheduler, tests) works without the browser installed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Optional

from .config import Config, Credentials
from .notify import notify


@dataclass
class BookingResult:
    success: bool
    booked_time: Optional[str] = None
    message: str = ""
    screenshot: Optional[str] = None


class TeeBooker:
    def __init__(self, config: Config, credentials: Credentials, *, log=print):
        self.cfg = config
        self.creds = credentials
        self.log = log

    # -- public ----------------------------------------------------------------

    def run(self, play_date: date_cls, *, dry_run: bool = False) -> BookingResult:
        """Log in and attempt to book a preferred time for play_date."""
        from playwright.sync_api import sync_playwright  # lazy import

        rt = self.cfg.runtime
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=rt.headless,
                slow_mo=rt.slow_mo_ms or 0,
            )
            context = browser.new_context()
            page = context.new_page()
            try:
                self._login(page)
                self._open_tee_sheet(page, play_date)
                if dry_run:
                    slot = self._find_available_slot(page)
                    if slot is None:
                        return BookingResult(
                            False, message="DRY RUN: no preferred time visible yet."
                        )
                    label = self._slot_label_text(slot)
                    return BookingResult(
                        True,
                        booked_time=label,
                        message=f"DRY RUN: would book {label!r} (no click made).",
                    )
                return self._attempt_booking(page)
            except Exception as exc:  # noqa: BLE001
                shot = self._screenshot(page, "error")
                return BookingResult(
                    False, message=f"Error: {exc}", screenshot=shot
                )
            finally:
                context.close()
                browser.close()

    # -- steps -----------------------------------------------------------------

    def _login(self, page) -> None:
        s = self.cfg.selectors
        self.log(f"Opening login page {self.cfg.club.login_url}")
        page.goto(self.cfg.club.login_url, wait_until="domcontentloaded")
        page.fill(s["username"], self.creds.username)
        page.fill(s["password"], self.creds.password)
        page.click(s["login_button"])
        marker = s.get("login_success_marker")
        if marker:
            page.wait_for_selector(marker, timeout=20_000)
        else:
            page.wait_for_load_state("networkidle")
        self.log("Logged in.")

    def _open_tee_sheet(self, page, play_date: date_cls) -> None:
        url = self.cfg.tee_sheet_url_for(play_date)
        self.log(f"Opening tee sheet {url}")
        page.goto(url, wait_until="domcontentloaded")
        dp = self.cfg.date_picker or {}
        if dp.get("enabled"):
            self._pick_date(page, play_date)

    def _pick_date(self, page, play_date: date_cls) -> None:
        dp = self.cfg.date_picker
        if dp.get("open_button"):
            page.click(dp["open_button"])
        day_sel = (dp.get("day_cell") or "").replace("{day}", str(play_date.day))
        if day_sel:
            page.click(day_sel)
            page.wait_for_load_state("networkidle")

    def _attempt_booking(self, page) -> BookingResult:
        """Race loop: repeatedly look for a preferred slot and book it."""
        deadline = time.monotonic() + self.cfg.release.retry_window_seconds
        interval = self.cfg.release.retry_interval_seconds
        attempt = 0
        while True:
            attempt += 1
            slot = self._find_available_slot(page)
            if slot is not None:
                label = self._slot_label_text(slot)
                self.log(f"Found available slot {label!r} (attempt {attempt}); booking...")
                if self._book_slot(page, slot):
                    shot = self._screenshot(page, "confirmed")
                    return BookingResult(
                        True,
                        booked_time=label,
                        message=f"Booked {label} for {self.cfg.booking.players} players.",
                        screenshot=shot,
                    )
                self.log("Book click did not confirm; retrying...")
            if time.monotonic() >= deadline:
                shot = self._screenshot(page, "no_slot")
                return BookingResult(
                    False,
                    message=(
                        f"No preferred time became bookable within "
                        f"{self.cfg.release.retry_window_seconds}s "
                        f"({attempt} attempts)."
                    ),
                    screenshot=shot,
                )
            time.sleep(interval)
            page.reload(wait_until="domcontentloaded")

    # -- slot helpers ----------------------------------------------------------

    def _find_available_slot(self, page):
        """Return the first slot element matching a preferred time, or None."""
        s = self.cfg.selectors
        for wanted in self.cfg.booking.preferred_times:
            slots = page.locator(s["time_slot"])
            count = slots.count()
            for i in range(count):
                slot = slots.nth(i)
                label = self._slot_label_text(slot)
                if label and self._times_match(wanted, label):
                    # Must contain a usable book button to count as available.
                    if slot.locator(s["book_button"]).count() > 0:
                        return slot
        return None

    def _slot_label_text(self, slot) -> str:
        s = self.cfg.selectors
        label_sel = s.get("time_slot_label")
        try:
            if label_sel and slot.locator(label_sel).count() > 0:
                return (slot.locator(label_sel).first.inner_text() or "").strip()
            return (slot.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _times_match(wanted: str, label: str) -> bool:
        return TeeBooker._normalize(wanted) in TeeBooker._normalize(label)

    @staticmethod
    def _normalize(s: str) -> str:
        return s.lower().replace(" ", "").replace(":", "")

    def _book_slot(self, page, slot) -> bool:
        s = self.cfg.selectors
        slot.locator(s["book_button"]).first.click()
        confirm = s.get("confirm_button")
        if confirm:
            try:
                page.wait_for_selector(confirm, timeout=10_000)
                page.click(confirm)
            except Exception:  # noqa: BLE001
                pass  # some portals book in one click
        marker = s.get("confirmation_marker")
        if marker:
            try:
                page.wait_for_selector(marker, timeout=15_000)
                return True
            except Exception:  # noqa: BLE001
                return False
        # No confirmation marker configured — assume success after the clicks.
        page.wait_for_load_state("networkidle")
        return True

    # -- misc ------------------------------------------------------------------

    def _screenshot(self, page, tag: str) -> Optional[str]:
        if not self.cfg.runtime.screenshot_on_error and tag != "confirmed":
            return None
        try:
            out_dir = Path(self.cfg.runtime.screenshot_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = out_dir / f"{stamp}-{tag}.png"
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:  # noqa: BLE001
            return None
