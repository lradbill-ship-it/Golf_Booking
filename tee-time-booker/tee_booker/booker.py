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
    # The earliest tee time published on the sheet that night, bookable or not.
    # Purely observational — it's how you notice sunrise pushing the first tee
    # time from 6:30 to 7:00 and move `preferred_times` deliberately.
    earliest_time: Optional[str] = None


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
                self._login_resiliently(page)
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
                    note = ""
                    if slot is None:
                        # Show the same compromise the real run would make.
                        found = self._find_fallback_slot(page)
                        if found is not None:
                            slot, _label, offset = found
                            target = self.fallback_target()
                            note = (" (closest available — "
                                    f"{self._describe_offset(offset, target[1] if target else '')})")
                    if slot is None:
                        return BookingResult(
                            False,
                            message="DRY RUN: nothing bookable in range visible yet.",
                        )
                    label = self._slot_label_text(slot)
                    return BookingResult(
                        True,
                        booked_time=label,
                        message=f"DRY RUN: would book {label!r}{note} (no click made).",
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

    def scan_for_earlier(self, helds, *, min_improvement: int):
        """Look at each held date's sheet for a better time. READ-ONLY.

        One browser session for the whole check. For every date: open its
        sheet, record the earliest time open to our party, and rank the bookable
        slots against that day's target with the same ranking the nightly
        fallback uses (`_closest_slot`). Never clicks Book, never touches the
        cart. Returns an `earlier_watch.WatchRun`.
        """
        from playwright.sync_api import sync_playwright  # lazy import

        from .earlier_watch import WatchRun

        run = WatchRun()
        rt = self.cfg.runtime
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=rt.headless, slow_mo=rt.slow_mo_ms or 0)
            context = browser.new_context()
            context.route("**/*", self._maybe_block)
            page = context.new_page()
            try:
                self._login_resiliently(page)
                self._scan_for_earlier_on(page, helds, run, min_improvement=min_improvement)
            except Exception as exc:  # noqa: BLE001
                run.error = str(exc)
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
        return run

    def _scan_for_earlier_on(self, page, helds, run, *, min_improvement: int) -> None:
        """The per-date scan behind `scan_for_earlier` (browser-free to test)."""
        from .earlier_watch import WatchScan, improvement

        fb = getattr(self.cfg.booking, "fallback", None)
        back = 60 if fb is None else fb.max_minutes_earlier
        for held in helds:
            self.cfg.booking.players = held.players
            self._open_tee_sheet(page, held.play_date)
            if self._is_blocked(page):
                run.blocked = True
                self.log("Rate-limited (Cloudflare) — ending this check; the next run retries.")
                return
            earliest = self._earliest_bookable(page)
            earliest_ok = None if back is None else held.target_minutes - back
            # Strictly earlier than the held time: this watches for EARLIER tee
            # times, never a move later (even one nearer the target).
            found = self._closest_slot(page, held.target_minutes, earliest_ok,
                                       held.held_minutes - 1)
            label = minutes = None
            gain = 0
            if found is not None:
                _slot, label, offset = found
                minutes = held.target_minutes + offset
                gain = improvement(held, minutes)
            better = gain >= min_improvement
            run.scans.append(WatchScan(
                held=held, earliest_open=earliest,
                better_time=label if better else None,
                better_minutes=minutes if better else None,
                improvement=gain if better else 0,
            ))
            head = (f"{held.play_date} ({held.weekday.title()}): holding {held.held_label}, "
                    f"target {held.target_label}; earliest open {earliest or 'nothing'}")
            self.log(f"{head} — " + (f"BETTER TIME OPEN: {label} ({gain} min closer)."
                                     if better else "nothing better."))

    # -- steps -----------------------------------------------------------------

    def _login(self, page) -> None:
        from .session import login

        login(page, self.cfg, self.creds, log=self.log)

    # Playwright wraps low-level connectivity failures with these net:: codes.
    _NETWORK_ERROR_MARKERS = (
        "err_internet_disconnected", "err_name_not_resolved", "err_connection",
        "err_timed_out", "err_network_changed", "err_address_unreachable",
        "err_proxy_connection_failed", "err_network_access_denied",
    )

    def _looks_like_network_error(self, exc) -> bool:
        msg = str(exc).lower()
        return "net::" in msg or any(m in msg for m in self._NETWORK_ERROR_MARKERS)

    def _login_resiliently(self, page) -> None:
        """Log in, riding out a transient loss of internet at login time.

        The nightly job logs in ~60s before the nominal release, but the sheet
        doesn't actually release for ~13 min, so a brief connectivity blip at
        00:00 (e.g. the Mac's Wi-Fi hasn't reconnected) shouldn't kill the whole
        night. On a *network* error we wait and retry until `login_retry_seconds`
        is exhausted; any other error is re-raised immediately.
        """
        # Note: use explicit None checks, not `or` — a configured 0 is a real
        # value (disable retries / no wait), not a signal to fall back.
        budget = getattr(self.cfg.release, "login_retry_seconds", 0)
        budget = float(budget) if budget is not None else 0.0
        interval = getattr(self.cfg.release, "login_retry_interval_seconds", 10.0)
        interval = float(interval) if interval is not None else 10.0
        deadline = time.monotonic() + budget
        attempt = 0
        while True:
            attempt += 1
            try:
                self._login(page)
                return
            except Exception as exc:  # noqa: BLE001
                if not self._looks_like_network_error(exc) or time.monotonic() >= deadline:
                    raise
                self.log(
                    f"Login attempt {attempt} failed — no connectivity ({exc}). "
                    f"Retrying in {interval:.0f}s ..."
                )
                time.sleep(interval)

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
        """Poll and book, then attach the night's earliest-published tee time."""
        self._earliest_seen = None
        result = self._poll_and_book(page, play_date)
        if self._earliest_seen:
            result.earliest_time = self._earliest_seen[1]
        return result

    def _earliest_slot(self, page, current=None):
        """The earliest tee time on the sheet, as (minutes, label).

        Counts every published slot, bookable or not: the question this answers
        is "when does the sheet start now?", which is what shifts with sunrise.
        `current` is the best seen so far, so it survives across poll checks.
        """
        best = current
        try:
            slots = page.locator(self.cfg.selectors["time_slot"])
            for i in range(slots.count()):
                label = self._slot_label_text(slots.nth(i))
                minutes = self._parse_time_minutes(label)
                if minutes is None:
                    continue
                if best is None or minutes < best[0]:
                    best = (minutes, label)
        except Exception:  # noqa: BLE001
            pass  # observational only — never let it break a booking
        return best

    def _poll_and_book(self, page, play_date=None) -> BookingResult:
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
        taken_labels: set = set()  # preferred times lost to others at checkout
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
            if cards > 0:
                self._earliest_seen = self._earliest_slot(page, self._earliest_seen)
            # First time the fresh sheet shows any cards = the actual release.
            if released_at is None and cards > 0:
                released_at = datetime.now(getattr(self, "_tz", None))
                first = self._earliest_seen[1] if self._earliest_seen else "unknown"
                self.log(
                    f"[{elapsed:.0f}s] Sheet released: {cards} card(s) appeared "
                    f"at {released_at.strftime('%H:%M:%S')}; earliest tee time on the "
                    f"sheet is {first}."
                )
            slot = self._find_available_slot(page, exclude=taken_labels)
            fallback_note = ""
            if slot is None and cards > 0:
                slot, fallback_note = self._consider_fallback(
                    page, taken_labels, elapsed=elapsed
                )
            if slot is not None:
                label = self._slot_label_text(slot)
                self.log(
                    f"[{elapsed:.0f}s] check #{attempt}: found {label!r}{fallback_note}; "
                    "booking once..."
                )
                status = self._book_slot(page, slot)
                if status == "booked":
                    return BookingResult(
                        True,
                        booked_time=label,
                        message=(
                            f"Booked {label} for "
                            f"{self.cfg.booking.players} players{fallback_note}."
                        ),
                        screenshot=self._screenshot(page, "confirmed"),
                        release_detected_at=released_at,
                        attempts=attempt,
                    )
                if status == "stop":
                    # The click is not the record. Screenshot first (verifying
                    # navigates away), then ask the portal's own reservation
                    # list what actually happened.
                    shot = self._screenshot(page, "unconfirmed")
                    verdict = (self._booked_on_portal(page, play_date, label)
                               if play_date is not None else None)
                    if verdict is True:
                        self.log(f"The reservation list shows {label} — it went through after all.")
                        return BookingResult(
                            True,
                            booked_time=label,
                            message=(
                                f"Booked {label} for {self.cfg.booking.players} players "
                                "(slow checkout — confirmed on the reservation list)."
                            ),
                            screenshot=shot,
                            release_detected_at=released_at,
                            attempts=attempt,
                        )
                    if verdict is False:
                        # A point-in-time read. It rules a purchase IN with
                        # certainty; ruling one OUT assumes the portal writes the
                        # reservation before we look. No delayed write has been
                        # observed — but the hung checkouts of 2026-09-23..29 left
                        # one order with no reservation behind, so say what was
                        # seen and when, and let a human settle an odd case.
                        detail = (f"The reservation list showed no booking for {play_date} "
                                  "when checked seconds later, so almost certainly nothing "
                                  "was bought.")
                    else:
                        detail = ("Couldn't read the reservation list to check, so it is "
                                  "unknown whether anything was bought — check the portal.")
                    self.log(detail)
                    return BookingResult(
                        False,
                        booked_time=label,
                        message=(
                            f"Attempted to book {label} but the portal never confirmed it. "
                            + detail
                            + " Stopping WITHOUT retrying, to rule out a double booking."
                        ),
                        screenshot=shot,
                        release_detected_at=released_at,
                        attempts=attempt,
                    )
                if status == "taken":
                    # The slot was grabbed by someone else during checkout. The
                    # portal positively rejected the purchase, so nothing was
                    # booked — skip this time and immediately try the next one.
                    taken_labels.add(self._normalize(label))
                    self.log(
                        f"[{elapsed:.0f}s] {label!r} was taken at checkout; clearing the "
                        "cart and trying the next preferred time."
                    )
                    # Reopen the tee sheet FIRST: the cart-drawer controls only
                    # render off the sheet's header (not the checkout page we're
                    # on after a rejection), so clearing must happen there.
                    self._reopen(page, play_date)
                    self._clear_cart(page)
                    if time.monotonic() >= deadline:
                        return BookingResult(
                            False,
                            message=(
                                "Preferred times kept getting taken at checkout before the "
                                "window closed — nothing booked."
                            ),
                            screenshot=self._screenshot(page, "no_slot"),
                            release_detected_at=released_at,
                            attempts=attempt,
                        )
                    continue  # re-scan now, don't wait the full poll interval
                self.log(f"[{elapsed:.0f}s] Couldn't secure {label} (no booking made); will retry.")
            else:
                self.log(
                    f"[{elapsed:.0f}s] check #{attempt}: {cards} card(s), nothing "
                    "bookable in range yet — waiting for the fresh sheet."
                )

            if time.monotonic() >= deadline:
                return BookingResult(
                    False,
                    message=(
                        f"Nothing bookable within {self.cfg.release.retry_window_seconds}s "
                        f"({attempt} checks) — no preferred time, and "
                        + (f"nothing bookable in the fallback range ({window}) either"
                           if (window := self.fallback_window()) else "no fallback is set")
                        + ". The sheet may not have released in time, or everything "
                        "was taken."
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
            if not getattr(self.cfg.runtime, "block_resources", True):
                return route.continue_()
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

    # Shown when the chosen slot was grabbed by someone else mid-checkout, so the
    # purchase was rejected. A POSITIVE signal that nothing was bought — unlike an
    # ambiguous timeout — so the booker may safely try the next preferred time.
    _INVENTORY_GONE_MARKERS = (
        "no longer available",
        "select another tee time",
        "issue finishing your booking",
        "please select another time",
    )

    # Shown while the portal completes a SUBMITTED purchase ("Processing cart
    # items ( 0 of 1 ) ..."). A POSITIVE signal that the click landed and work is
    # in flight, so waiting longer is right — unlike silence, which proves nothing.
    _PROCESSING_MARKERS = (
        "processing cart items",
        "processing your order",
        "completing your booking",
    )

    def _is_processing(self, page) -> bool:
        """True while the portal says it is still completing the purchase."""
        try:
            text = (page.inner_text("body") or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in text for m in self._PROCESSING_MARKERS)

    def _booked_on_portal(self, page, play_date, label):
        """Does the portal's OWN reservation list show this tee time?

        True / False / None, where None means we could not read the list — which
        is NOT the same as "no booking", and must never be treated as one. Used
        after an unconfirmed checkout: a click that we could not confirm is not
        evidence either way, but the member's reservation list is.
        """
        from .reservations import _parse
        from .session import origin_of

        minutes = self._parse_time_minutes(label or "")
        captured: dict = {}

        def on_response(resp):
            u = resp.url
            if ("kenna.io" in u and "/reservation/history" in u
                    and "playDateMin" in u and resp.request.method == "GET"):
                try:
                    captured["data"] = resp.json()
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        try:
            page.goto(f"{origin_of(self.cfg.club.login_url)}/reservation/history",
                      wait_until="domcontentloaded")
            for _ in range(40):
                if "data" in captured:
                    break
                page.wait_for_timeout(300)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Couldn't open the reservation list to verify ({exc}).")
            return None
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:  # noqa: BLE001
                pass
        if "data" not in captured:
            return None
        for r in _parse(captured["data"]):
            when = r.when
            if r.cancelled or when is None or when.date() != play_date:
                continue
            if minutes is None or when.hour * 60 + when.minute == minutes:
                return True
        return False

    def _inventory_unavailable(self, page) -> bool:
        """True when the portal rejected the purchase because the slot was taken."""
        try:
            text = (page.inner_text("body") or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in text for m in self._INVENTORY_GONE_MARKERS)

    def _find_available_slot(self, page, exclude=None):
        """Return the first slot matching a preferred time that also fits the party.

        Skips slots whose allowed party size doesn't include booking.players, so
        a "1 golfer only" slot is never chosen for a twosome. `exclude` is a set
        of normalized labels to skip — used to pass over a time that was already
        taken out from under us at checkout.
        """
        exclude = exclude or set()
        s = self.cfg.selectors
        for wanted in self.cfg.booking.preferred_times:
            slots = page.locator(s["time_slot"])
            count = slots.count()
            for i in range(count):
                slot = slots.nth(i)
                label = self._slot_label_text(slot)
                if label and self._times_match(wanted, label):
                    if self._normalize(label) in exclude:
                        continue  # already lost this one at checkout
                    # Must be bookable AND allow our group size.
                    if slot.locator(s["book_button"]).count() > 0 and self._slot_allows_players(slot):
                        return slot
        return None

    def fallback_window(self) -> Optional[str]:
        """The clock range the fallback may search, in words.

        Reads as "5:30 AM - 9:20 AM" when both sides are bounded, "5:30 AM or
        later" when only the late side is open, and "any published time" when
        neither is capped. None when there is no fallback to describe — it's
        switched off, or the preferred times have no parseable clock time to
        measure "closest" from. Used by `--plan` and the failure message.
        """
        fb = getattr(self.cfg.booking, "fallback", None)
        if fb is None or not fb.enabled:
            return None
        target = self.fallback_target()
        if target is None:
            return None
        lo = (None if fb.max_minutes_earlier is None
              else self._fmt_minutes(target[0] - fb.max_minutes_earlier))
        hi = (None if fb.max_minutes_later is None
              else self._fmt_minutes(target[0] + fb.max_minutes_later))
        if lo and hi:
            return f"{lo} - {hi}"
        if lo:
            return f"{lo} or later"
        if hi:
            return f"up to {hi}"
        return "any published time"

    @staticmethod
    def _fmt_minutes(minutes: int) -> str:
        """Minutes-since-midnight back to a '6:30 AM' style label."""
        minutes %= 24 * 60
        hour, minute = divmod(minutes, 60)
        suffix = "AM" if hour < 12 else "PM"
        return f"{(hour % 12) or 12}:{minute:02d} {suffix}"

    def _consider_fallback(self, page, exclude, *, elapsed: float):
        """Settle for the closest bookable time once preferred ones are a dead end.

        Returns (slot_or_None, note) where note describes the compromise for the
        log and the confirmation message.
        """
        fb = getattr(self.cfg.booking, "fallback", None)
        if fb is None or not fb.enabled or elapsed < fb.after_seconds:
            return None, ""
        found = self._find_fallback_slot(page, exclude=exclude)
        if found is None:
            return None, ""
        # A half-rendered sheet can briefly hide a preferred slot's book button,
        # so don't settle on the first look: pause, then check the preferred
        # times once more. Cheap next to booking a worse time by mistake.
        if fb.recheck_seconds > 0:
            self.log(
                f"[{elapsed:.0f}s] No preferred time bookable — re-checking in "
                f"{fb.recheck_seconds:.0f}s before settling for the closest."
            )
            time.sleep(fb.recheck_seconds)
            preferred = self._find_available_slot(page, exclude=exclude)
            if preferred is not None:
                return preferred, ""  # it was there after all
            found = self._find_fallback_slot(page, exclude=exclude)
            if found is None:
                return None, ""
        _slot, _label, offset = found
        target = self.fallback_target()
        note = self._describe_offset(offset, target[1] if target else "")
        return _slot, f" (closest available — {note})"

    def fallback_target(self):
        """The one time the fallback orients on, as (minutes, label).

        It is the FIRST entry in `preferred_times` — the day's top choice.
        Everything below it in the list is a ranked second best to try while
        exact matching is still possible; once that is exhausted, "closest"
        means closest to this single target, not to whichever listed time
        happens to sit nearest. So a 6:30 AM weekday and a 7:00 AM weekend
        orient differently just by how each day's list is ordered.

        None when no entry carries a parseable clock time.
        """
        for raw in self.cfg.booking.preferred_times:
            minutes = self._parse_time_minutes(raw)
            if minutes is not None:
                return minutes, self._fmt_minutes(minutes)
        return None

    def _find_fallback_slot(self, page, exclude=None):
        """The bookable slot closest to the target time, when none is free.

        Tee sheets drift with sunrise — a 6:30 AM time that existed in June is
        simply not published in late August — so an exact-match-only booker
        books nothing on precisely the nights the early times moved. This ranks
        every bookable slot by how far it sits from `fallback_target()` and
        takes the closest, with the EARLIER slot winning a tie. When the sheet
        now starts after the target (the sunrise case), that is just "the
        earliest time available".

        Candidates are bounded to `max_minutes_earlier` before the target and
        `max_minutes_later` after it; either bound may be None, meaning that
        side is unbounded and any published time qualifies. Returns
        (slot, label, minutes_from_target) or None.
        """
        fb = getattr(self.cfg.booking, "fallback", None)
        if fb is None or not fb.enabled:
            return None
        target = self.fallback_target()
        if target is None:
            return None  # nothing to measure "closest" against
        target_minutes = target[0]
        earliest_ok = (None if fb.max_minutes_earlier is None
                       else target_minutes - fb.max_minutes_earlier)
        latest_ok = (None if fb.max_minutes_later is None
                     else target_minutes + fb.max_minutes_later)
        return self._closest_slot(page, target_minutes, earliest_ok, latest_ok, exclude)

    def _closest_slot(self, page, target_minutes, earliest_ok=None, latest_ok=None,
                      exclude=None):
        """The bookable, party-size-fitting slot closest to `target_minutes`.

        The one ranking both the nightly fallback and the upgrade watcher use:
        distance from the target, with the EARLIER slot winning a tie. Slots
        outside [earliest_ok, latest_ok] (either may be None = unbounded) or in
        `exclude` are passed over. Returns (slot, label, minutes_from_target)
        or None.
        """
        exclude = exclude or set()
        s = self.cfg.selectors
        slots = page.locator(s["time_slot"])
        best = None  # ((distance, minutes), index, label, signed_offset)
        for i in range(slots.count()):
            slot = slots.nth(i)
            label = self._slot_label_text(slot)
            minutes = self._parse_time_minutes(label)
            if minutes is None:
                continue
            if earliest_ok is not None and minutes < earliest_ok:
                continue
            if latest_ok is not None and minutes > latest_ok:
                continue
            if self._normalize(label) in exclude:
                continue  # already lost this one at checkout
            if slot.locator(s["book_button"]).count() == 0 or not self._slot_allows_players(slot):
                continue
            # Sort by distance from the target, then by clock time: the
            # earlier slot wins a tie.
            key = (abs(minutes - target_minutes), minutes)
            if best is None or key < best[0]:
                best = (key, i, label, minutes - target_minutes)
        if best is None:
            return None
        return slots.nth(best[1]), best[2], best[3]

    def _earliest_bookable(self, page) -> Optional[str]:
        """Label of the earliest slot our party could book right now, or None.

        Unlike `_earliest_slot` (every published time, bookable or not), this is
        what is actually open to us — the number that shows whether a blocked
        morning has started giving times back.
        """
        s = self.cfg.selectors
        best = None
        try:
            slots = page.locator(s["time_slot"])
            for i in range(slots.count()):
                slot = slots.nth(i)
                label = self._slot_label_text(slot)
                minutes = self._parse_time_minutes(label)
                if minutes is None:
                    continue
                if slot.locator(s["book_button"]).count() == 0 or not self._slot_allows_players(slot):
                    continue
                if best is None or minutes < best[0]:
                    best = (minutes, label)
        except Exception:  # noqa: BLE001
            return None  # observational only
        return best[1] if best else None

    @staticmethod
    def _parse_time_minutes(text: str) -> Optional[int]:
        """Minutes since midnight from a label like '6:30 AM', '7:05pm', '06:30'.

        Slot labels often carry more than the time (price, party size), so this
        takes the first clock time it finds. Returns None if there isn't one.
        """
        if not text:
            return None
        m = re.search(r"(\d{1,2}):([0-5]\d)\s*([ap])\.?\s?m\.?", text, re.I)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            if not 1 <= hour <= 12:
                return None
            hour %= 12
            if m.group(3).lower() == "p":
                hour += 12
            return hour * 60 + minute
        m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)  # 24-hour
        if m:
            return int(m.group(1)) * 60 + int(m.group(2))
        return None

    @staticmethod
    def _describe_offset(minutes: int, target_label: str = "") -> str:
        """How far a fallback sits from the target, e.g. '40 min later than 7:00 AM'."""
        against = f" than {target_label}" if target_label else ""
        if minutes == 0:
            return f"exactly {target_label}" if target_label else "on target"
        return f"{abs(minutes)} min {'later' if minutes > 0 else 'earlier'}{against}"

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

            # 7) Resolve the outcome as soon as it's known (see below).
            return self._await_checkout_outcome(page, attempted_purchase)
        except Exception as exc:  # noqa: BLE001
            if attempted_purchase:
                self.log(f"Error after submitting the purchase ({exc}); not retrying.")
                return "stop"
            self.log(f"Booking failed before purchase ({exc}); safe to retry.")
            return "retry"

    def _await_checkout_outcome(self, page, attempted_purchase: bool) -> str:
        """After clicking purchase, return "booked" / "taken" / "stop" / "retry".

        Polls for whichever verdict lands first instead of blindly waiting out
        the full timeout:
          - the URL leaves the checkout route  -> "booked" (confirmation shown);
          - the portal says the slot is gone   -> "taken" (a safe rejection —
            nothing was bought, so the caller may try the next preferred time).
        A lost race is therefore detected in ~1s rather than ~20s, freeing the
        booker to grab the next time before it's taken too. Falling through to
        the timeout means we never saw a verdict, so we "stop" (a purchase may
        have gone through) rather than risk a double booking.
        """
        leaves = (self.cfg.checkout or {}).get("success_when_url_leaves", "/checkout")
        timeout_s = float((self.cfg.checkout or {}).get("success_timeout_seconds", 20))
        patience_s = float((self.cfg.checkout or {}).get("processing_timeout_seconds", 180))
        deadline = time.monotonic() + timeout_s
        started = time.monotonic()
        extended = False
        while True:
            # The portal telling us it is still working is a reason to wait, not
            # to walk away: from 2026-09-23 every night died here at 20s with
            # "Processing cart items ( 0 of 1 )" still on screen.
            if attempted_purchase and not extended and self._is_processing(page):
                extended = True
                deadline = max(deadline, time.monotonic() + patience_s)
                self.log(f"Portal says it is still processing the purchase — "
                         f"waiting up to {patience_s:.0f}s more for it to finish.")
            try:
                if leaves not in page.url:
                    return "booked"
            except Exception:  # noqa: BLE001
                pass
            if attempted_purchase and self._inventory_unavailable(page):
                self.log("Portal rejected the purchase: that time was just taken.")
                return "taken"
            if time.monotonic() >= deadline:
                if extended:
                    self.log(f"Still not confirmed after {time.monotonic() - started:.0f}s "
                             "of processing.")
                return "stop" if attempted_purchase else "retry"
            page.wait_for_timeout(500)

    def _clear_cart(self, page) -> None:
        """Best-effort empty the cart so a stale (now-unavailable) item can't
        block or inflate the next checkout. Safe to call on an empty cart.

        The per-item kebab + delete controls only exist INSIDE the cart drawer,
        so this first opens the drawer (via `cart_open_button`) when the items
        aren't already visible, then removes them one by one. Needs
        `cart_open_button` + `cart_item` (kebab) + `cart_item_remove`; if any is
        unset it does nothing and we fall back to the >1-item guard in
        `_book_via_cart`, which still prevents any over-booking.
        """
        s = self.cfg.selectors
        opener = s.get("cart_open_button")
        kebab = s.get("cart_item")
        remove = s.get("cart_item_remove")
        if not opener or not kebab or not remove:
            return
        try:
            # Open the drawer if its items aren't already on screen. The cart
            # icon only renders when the cart is non-empty, so its absence means
            # there's nothing to clear.
            if page.locator(kebab).count() == 0:
                if page.locator(opener).count() == 0:
                    return
                page.locator(opener).first.click()
                try:
                    page.wait_for_selector(kebab, timeout=5_000)
                except Exception:  # noqa: BLE001
                    return  # drawer didn't open / already empty
            for _ in range(6):  # bounded; remove one item per pass
                if page.locator(kebab).count() == 0:
                    break
                page.locator(kebab).first.click()
                page.locator(remove).first.click()
                page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

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
