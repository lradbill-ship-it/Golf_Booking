"""The Playwright-driven booking flow.

This is the club-specific part. The flow is generic — log in, open the tee
sheet for the play date, find the first acceptable time, book it, confirm — but
the actual element selectors live in config.yaml so it can be adapted to any
portal without code changes.

Playwright is imported lazily inside `run()` so the rest of the package
(config, scheduler, tests) works without the browser installed.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Optional

from .config import Config, Credentials


@dataclass
class BookingResult:
    success: bool
    booked_time: Optional[str] = None
    message: str = ""
    screenshot: Optional[str] = None
    # Wall-clock moment the fresh tee sheet first showed slot cards (i.e. when
    # the sheet actually released), and how many poll checks it took. Used by
    # the nightly run to log the real release time. None if it never released.
    release_detected_at: Optional[datetime] = None
    attempts: int = 0


class TeeBooker:
    def __init__(self, config: Config, credentials: Credentials, *, log=print):
        self.cfg = config
        self.creds = credentials
        self.log = log

    # -- public ----------------------------------------------------------------

    def run(self, play_date: date_cls, *, dry_run: bool = False,
            release_at=None, tz=None) -> BookingResult:
        """Log in and attempt to book a preferred time for play_date.

        If release_at/tz are given, log in first and then hold until the release
        instant, so authentication completes before the traffic surge instead of
        racing it (logging in at the peak was leaving the booker logged out).
        """
        from playwright.sync_api import sync_playwright  # lazy import

        self._tz = tz  # used to timestamp the observed release moment
        rt = self.cfg.runtime
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=rt.headless,
                slow_mo=rt.slow_mo_ms or 0,
            )
            context = browser.new_context()
            # Drop heavy, non-essential traffic (images/fonts/media + analytics
            # trackers). Lighter footprint = far fewer requests per reload, which
            # keeps the long poll well under the site's rate limiter.
            context.route("**/*", self._maybe_block)
            page = context.new_page()
            try:
                self._login(page)
                if release_at is not None and tz is not None and not dry_run:
                    from .scheduler import seconds_until, wait_until
                    remaining = seconds_until(release_at, tz)
                    if remaining > 0:
                        self.log(f"Logged in early; holding {remaining:.0f}s until release ...")
                        wait_until(release_at, tz, warmup_seconds=0, log=self.log)
                        self.log("Release — opening the tee sheet now.")
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
                return self._attempt_booking(page, play_date)
            except Exception as exc:  # noqa: BLE001
                shot = self._screenshot(page, "error")
                return BookingResult(
                    False, message=f"Error: {exc}", screenshot=shot
                )
            finally:
                # Close both even if the first close raises, so a crashed
                # context can never leak the underlying browser process.
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- steps -----------------------------------------------------------------

    def _login(self, page) -> None:
        from .session import login

        login(page, self.cfg, self.creds, log=self.log)

    def _open_tee_sheet(self, page, play_date: date_cls, *, allow_relogin: bool = True) -> None:
        url = self.cfg.tee_sheet_url_for(play_date)
        self.log(f"Opening tee sheet {url}")
        page.goto(url, wait_until="domcontentloaded")

        # If the member session didn't carry over (the tee sheet shows logged
        # out), log in again and reopen once — a logged-out tee sheet shows no
        # bookable member times.
        marker = self.cfg.selectors.get("login_success_marker")
        if allow_relogin and marker:
            try:
                page.wait_for_selector(marker, timeout=6_000)
            except Exception:  # noqa: BLE001
                self.log("Tee sheet is logged out — re-logging in and reopening.")
                self._login(page)
                return self._open_tee_sheet(page, play_date, allow_relogin=False)

        dp = self.cfg.date_picker or {}
        if dp.get("enabled"):
            self._pick_date(page, play_date)
        # Slots are rendered client-side; give them a moment to appear before we
        # look. Absence is fine here — the race loop reloads until they show.
        try:
            page.wait_for_selector(self.cfg.selectors["time_slot"], timeout=10_000)
        except Exception:  # noqa: BLE001
            pass
        # Visibility for diagnosing the race: how many cards are present.
        try:
            self.log(f"Tee sheet ready: {page.locator(self.cfg.selectors['time_slot']).count()} slot card(s) visible.")
        except Exception:  # noqa: BLE001
            pass

    def _pick_date(self, page, play_date: date_cls) -> None:
        dp = self.cfg.date_picker
        if dp.get("open_button"):
            page.click(dp["open_button"])
        day_sel = (dp.get("day_cell") or "").replace("{day}", str(play_date.day))
        if day_sel:
            page.click(day_sel)
            page.wait_for_load_state("networkidle")

    def _attempt_booking(self, page, play_date=None) -> BookingResult:
        """Poll for a preferred slot, then book exactly ONE.

        Keeps gently re-checking from the moment it starts until a booking lands
        or `release.retry_window_seconds` elapses — so it tolerates the sheet
        being released a little after the nominal release time (it waits for the
        fresh sheet to appear). It backs off on rate-limit blocks and re-logs-in
        if the session drops during the wait.

        Safety guarantee — at most one booking per run: the loop only continues
        while *no* slot has been purchased. `_book_slot` returns one of:
          "booked" — success; return immediately.
          "stop"   — a purchase was attempted but not confirmed (or the cart
                     looked wrong); do NOT retry, to avoid a double booking.
          "retry"  — failed *before* any purchase, so nothing was booked; safe
                     to try again (e.g. the next preferred time).
        """
        start = time.monotonic()
        deadline = start + self.cfg.release.retry_window_seconds
        interval = self.cfg.release.retry_interval_seconds
        attempt = 0
        released_at: Optional[datetime] = None  # when cards first appeared
        while True:
            attempt += 1
            elapsed = time.monotonic() - start

            # Rate-limited? Back off and keep waiting rather than giving up.
            if self._is_blocked(page):
                backoff = max(interval, 60.0)
                self.log(f"[{elapsed:.0f}s] Rate-limited (Cloudflare) — backing off {backoff:.0f}s.")
                if time.monotonic() + backoff >= deadline:
                    return BookingResult(
                        False,
                        message="Still rate-limited when the retry window ended — check the portal.",
                        screenshot=self._screenshot(page, "blocked"),
                        release_detected_at=released_at,
                        attempts=attempt,
                    )
                time.sleep(backoff)
                self._reopen(page, play_date)
                continue

            # The session can lapse during a long wait — re-login only when the
            # page POSITIVELY shows the logged-out state (absence of a marker can
            # just mean the SPA hasn't finished rendering after a reload).
            if play_date is not None and self._is_logged_out(page):
                self.log(f"[{elapsed:.0f}s] Session dropped — re-logging in.")
                self._login(page)
                self._open_tee_sheet(page, play_date, allow_relogin=False)

            cards = self._slot_count(page)
            # First time the fresh sheet shows any cards = the actual release.
            if released_at is None and cards > 0:
                released_at = datetime.now(getattr(self, "_tz", None))
                self.log(
                    f"[{elapsed:.0f}s] Sheet released: {cards} card(s) appeared "
                    f"at {released_at.strftime('%H:%M:%S')}."
                )
            slot = self._find_available_slot(page)
            if slot is not None:
                label = self._slot_label_text(slot)
                self.log(f"[{elapsed:.0f}s] Found {label!r} (check #{attempt}); booking once...")
                status = self._book_slot(page, slot)
                if status == "booked":
                    return BookingResult(
                        True,
                        booked_time=label,
                        message=f"Booked {label} for {self.cfg.booking.players} players.",
                        screenshot=self._screenshot(page, "confirmed"),
                        release_detected_at=released_at,
                        attempts=attempt,
                    )
                if status == "stop":
                    return BookingResult(
                        False,
                        booked_time=label,
                        message=(
                            f"Attempted to book {label} but couldn't confirm it "
                            "(or the cart held extra items). Stopping WITHOUT "
                            "retrying to avoid a possible double booking — please "
                            "check the portal."
                        ),
                        screenshot=self._screenshot(page, "unconfirmed"),
                        release_detected_at=released_at,
                        attempts=attempt,
                    )
                self.log(f"[{elapsed:.0f}s] Couldn't secure {label} (no booking made); will retry.")
            else:
                self.log(
                    f"[{elapsed:.0f}s] check #{attempt}: {cards} card(s), no preferred "
                    "time bookable yet — waiting for the fresh sheet."
                )

            if time.monotonic() >= deadline:
                return BookingResult(
                    False,
                    message=(
                        f"No preferred time became bookable within "
                        f"{self.cfg.release.retry_window_seconds}s ({attempt} checks). The "
                        "sheet may not have released in time, or the times were taken."
                    ),
                    screenshot=self._screenshot(page, "no_slot"),
                    release_detected_at=released_at,
                    attempts=attempt,
                )
            # Jittered wait so reloads aren't a fixed-cadence metronome.
            time.sleep(interval + random.uniform(0, interval * 0.6))
            # Reload and let the client-side sheet render before the next checks
            # (domcontentloaded fires before the SPA paints its cards/nav).
            try:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(self.cfg.selectors["time_slot"], timeout=8_000)
            except Exception:  # noqa: BLE001
                pass  # no cards yet (sheet not open) — handled on the next loop

    def _is_logged_out(self, page) -> bool:
        """True only when the page positively shows the logged-out affordance.

        Uses presence of "Login / Sign Up" rather than the absence of a logged-in
        marker, which can be momentarily missing right after a reload.
        """
        try:
            return "login / sign up" in (page.inner_text("body") or "").lower()
        except Exception:  # noqa: BLE001
            return False

    def _slot_count(self, page) -> int:
        try:
            return page.locator(self.cfg.selectors["time_slot"]).count()
        except Exception:  # noqa: BLE001
            return -1

    def _reopen(self, page, play_date) -> None:
        """Reload the tee sheet (re-navigating, which also re-logins if needed)."""
        try:
            if play_date is not None:
                self._open_tee_sheet(page, play_date)
            else:
                page.reload(wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass

    # Trackers and heavy media we never need — blocking them cuts the request
    # count per reload (gentler on the rate limiter, faster reloads).
    _BLOCK_HOSTS = (
        "datadoghq", "google-analytics", "googletagmanager", "doubleclick",
        "facebook", "hotjar", "segment.io", "fullstory",
    )

    def _maybe_block(self, route):
        try:
            req = route.request
            if req.resource_type in ("image", "media", "font") or any(
                h in req.url for h in self._BLOCK_HOSTS
            ):
                route.abort()
            else:
                route.continue_()
        except Exception:  # noqa: BLE001
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

    # -- slot helpers ----------------------------------------------------------

    _BLOCK_MARKERS = (
        "rate limited", "error 1015", "banned you temporarily",
        "attention required", "just a moment",
    )

    def _is_blocked(self, page) -> bool:
        """True if the page is a Cloudflare rate-limit / challenge interstitial."""
        try:
            text = (page.inner_text("body") or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in text for m in self._BLOCK_MARKERS)

    def _find_available_slot(self, page):
        """Return the first slot matching a preferred time that also fits the party.

        Skips slots whose allowed party size doesn't include booking.players, so
        a "1 golfer only" slot is never chosen for a twosome.
        """
        s = self.cfg.selectors
        for wanted in self.cfg.booking.preferred_times:
            slots = page.locator(s["time_slot"])
            count = slots.count()
            for i in range(count):
                slot = slots.nth(i)
                label = self._slot_label_text(slot)
                if label and self._times_match(wanted, label):
                    # Must be bookable AND allow our group size.
                    if slot.locator(s["book_button"]).count() > 0 and self._slot_allows_players(slot):
                        return slot
        return None

    def _slot_allows_players(self, slot) -> bool:
        """Whether this slot's allowed party size includes booking.players."""
        sel = self.cfg.selectors.get("slot_players_label")
        players = self.cfg.booking.players
        if not sel or not players:
            return True
        try:
            loc = slot.locator(sel)
            if loc.count() == 0:
                return True  # unknown — don't over-filter
            return self._players_allowed(loc.first.inner_text(), players)
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _players_allowed(label_text: str, players: int) -> bool:
        """Parse a party-size label like '1 or 2', '2 - 4', 'up to 4', or '1'."""
        txt = (label_text or "").lower()
        nums = [int(n) for n in re.findall(r"\d+", txt)]
        if not nums:
            return True  # unknown format — don't over-filter
        if "or" in txt:
            return players in nums          # e.g. "1 or 2"
        # "up to N" / "max N" means any party from 1 up to N — so a twosome must
        # not be skipped just because the label only names the upper bound.
        if any(k in txt for k in ("up to", "upto", "maximum", "max")):
            return 1 <= players <= max(nums)
        if len(nums) >= 2:
            return nums[0] <= players <= nums[-1]  # e.g. "2 - 4"
        return players == nums[0]           # e.g. "1"

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

    def _book_slot(self, page, slot) -> str:
        """Book one slot. Returns "booked", "stop", or "retry" (see _attempt_booking)."""
        s = self.cfg.selectors
        # Open this slot's booking panel / detail. A failure here means nothing
        # was purchased (e.g. the slot was just taken), so it's safe to retry.
        try:
            slot.locator(s["book_button"]).first.click()
        except Exception as exc:  # noqa: BLE001
            self.log(f"Couldn't open the booking panel ({exc}); safe to retry.")
            return "retry"

        # Multi-step cart checkout (e.g. TeeItUp): select golfers, add to cart,
        # check out, agree to terms, and complete the purchase.
        if s.get("add_to_cart_button"):
            return self._book_via_cart(page)

        # Legacy single-click confirm flow.
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
                return "booked"
            except Exception:  # noqa: BLE001
                # We clicked confirm but couldn't verify — don't retry blindly.
                return "stop"
        # No confirmation marker configured — assume success after the clicks.
        page.wait_for_load_state("networkidle")
        return "booked"

    def _book_via_cart(self, page) -> str:
        """Drive a cart-based checkout (book → golfers → cart → terms → buy).

        Returns "booked" / "stop" / "retry" (see _attempt_booking). It submits
        the purchase at most once and never returns "retry" after that point, so
        a single run can never create more than one booking.
        """
        s = self.cfg.selectors
        attempted_purchase = False
        try:
            # 1) Choose number of golfers, if the portal asks. {players} is filled
            #    from booking.players (e.g. golfer-select-radio-2 for a twosome).
            golfer_tpl = s.get("golfer_radio")
            players = self.cfg.booking.players
            if golfer_tpl and players:
                sel = golfer_tpl.replace("{players}", str(players))
                try:
                    page.wait_for_selector(sel, timeout=10_000)
                    page.check(sel)
                except Exception:  # noqa: BLE001
                    # Never silently book the wrong party size — skip this slot.
                    # Nothing has been purchased yet, so it's safe to retry.
                    self.log(f"Couldn't select {players} golfer(s) here; skipping (no booking made).")
                    return "retry"

            # 2) Some portals require explicitly selecting the (pre-highlighted) rate.
            rate = s.get("rate_select_button")
            if rate:
                try:
                    if page.locator(rate).count() > 0:
                        page.click(rate)
                except Exception:  # noqa: BLE001
                    pass  # rate already selected

            # 3) Add to cart.
            page.click(s["add_to_cart_button"])

            # SAFETY: only ever check out the single item we just added. If the
            # cart already held items (e.g. a leftover from an interrupted run),
            # abort rather than risk booking several at once.
            cart_item_sel = s.get("cart_item")
            if cart_item_sel:
                try:
                    page.wait_for_selector(cart_item_sel, timeout=10_000)
                    n_items = page.locator(cart_item_sel).count()
                except Exception:  # noqa: BLE001
                    n_items = 1  # couldn't count; add-to-cart guarantees >=1
                if n_items > 1:
                    self.log(
                        f"Cart holds {n_items} items — aborting checkout to avoid "
                        "multiple bookings. Clear the cart on the portal and retry."
                    )
                    return "stop"

            # 4) Check out from the cart drawer/page.
            checkout_btn = s.get("cart_checkout_button")
            if checkout_btn:
                page.wait_for_selector(checkout_btn, timeout=15_000)
                page.click(checkout_btn)

            # 5) Agree to terms & conditions, if there's a checkbox. The selector
            #    may point at the styled wrapper (common with MUI), so prefer a
            #    real checkbox input inside it and fall back to the wrapper.
            terms = s.get("terms_checkbox")
            if terms:
                try:
                    page.wait_for_selector(terms, timeout=20_000)
                    inner = page.locator(f"{terms} input[type='checkbox']")
                    target = inner if inner.count() > 0 else page.locator(terms)
                    try:
                        target.first.check()
                    except Exception:  # noqa: BLE001
                        page.locator(terms).first.click()
                except Exception:  # noqa: BLE001
                    self.log("Terms checkbox not found or already accepted.")

            # 6) Complete the purchase — the point of no return. Once we click
            #    this, we never retry (any later error returns "stop").
            complete = s.get("complete_purchase_button")
            if complete:
                page.wait_for_selector(complete, timeout=15_000)
                attempted_purchase = True
                page.click(complete)

            # 7) Success = we leave the checkout route (portal shows confirmation).
            leaves = (self.cfg.checkout or {}).get("success_when_url_leaves", "/checkout")
            timeout_ms = int((self.cfg.checkout or {}).get("success_timeout_seconds", 20)) * 1000
            try:
                page.wait_for_url(lambda url: leaves not in url, timeout=timeout_ms)
                return "booked"
            except Exception:  # noqa: BLE001
                # Clicked purchase but couldn't confirm — never retry (might have
                # gone through). Caller reports and stops.
                return "stop" if attempted_purchase else "retry"
        except Exception as exc:  # noqa: BLE001
            if attempted_purchase:
                self.log(f"Error after submitting the purchase ({exc}); not retrying.")
                return "stop"
            self.log(f"Booking failed before purchase ({exc}); safe to retry.")
            return "retry"

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
