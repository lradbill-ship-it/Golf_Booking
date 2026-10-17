#!/usr/bin/env python3
"""Nightly auto-booker for the standing weekly schedule.

Run by launchd shortly before midnight. It:
  1. Computes the play date that opens at the upcoming release
     (release.release_time; PCC moved from 00:01 to 09:00 in Oct 2026)
     (the calendar date of that release + release.days_ahead).
  2. Looks up that weekday's preferred times in config's `weekly_schedule`.
     An empty list means "skip tonight" (e.g. Mondays).
  3. Waits until the exact release instant, then books `players` golfers at the
     first available preferred time — or, if none of them is bookable (the tee
     sheet shifts later as sunrise does), at the closest available time within
     the `booking.fallback` window.

Flags:
  --plan        Print what it would do (date, weekday, times) and exit. No browser.
  --dry-run     Log in and report the slot it would book, without booking.
  --no-wait     Skip waiting for the release instant (book/inspect immediately).
  --date DATE   Override the computed play date (YYYY-MM-DD), for testing.
  --history     Print the recorded release-time history and exit.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from tee_booker import release_history, state_store
from tee_booker.booker import TeeBooker
from tee_booker.config import Config, ConfigError, Credentials
from tee_booker.notify import notify
from tee_booker.scheduler import wait_until

# Monday=0 .. Sunday=6, matching date.weekday().
# A job replayed long after its slot must not sit waiting for the next release.
MAX_PRERELEASE_WAIT_SECONDS = 3 * 3600

WEEKDAY_KEYS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]


def _stamp(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def compute_play_date(now: datetime, days_ahead: int, release_time: str = "00:01",
                      grace_seconds: float = 3600):
    """The play date whose booking window this run belongs to.

    The next release at `release_time`, except that a release which has only
    JUST passed (within `grace_seconds`, i.e. we are still inside the window
    this run would have polled) still belongs to this run — so a job that starts
    a few minutes late targets the sheet it was meant to, instead of skipping a
    day ahead.

    Reads the club's configured release time rather than assuming midnight: PCC
    moved from 00:01 to 09:00 in Oct 2026, and the old rule ("after noon means
    tomorrow's release") would have had a late-firing job wait ~19 hours.
    """
    # No silent fallback: guessing a release time can mistarget the play date by
    # a whole day, which is the failure this function exists to prevent. Config
    # validation rejects a bad value at load, so reaching here means a caller
    # passed one directly.
    try:
        hh, mm = (int(x) for x in str(release_time).split(":"))
        release_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Invalid release_time {release_time!r}; expected HH:MM (24-hour)."
        ) from exc
    if now < release_today or (now - release_today).total_seconds() <= grace_seconds:
        release_cal_date = now.date()
    else:
        release_cal_date = (now + timedelta(days=1)).date()
    return release_cal_date + timedelta(days=days_ahead)


def too_long_to_wait(login_at: datetime, now: datetime,
                     limit: float = MAX_PRERELEASE_WAIT_SECONDS) -> bool:
    """Is the next release so far off that waiting would hold the Mac awake?

    launchd runs a missed calendar job on the next wake, so a job whose slot
    passed hours ago must exit rather than sit on tomorrow's release under
    `caffeinate`.
    """
    return (login_at - now).total_seconds() > limit


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nightly", description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("--date", help="Override play date YYYY-MM-DD (testing).")
    p.add_argument("--plan", action="store_true", help="Print the plan and exit.")
    p.add_argument("--dry-run", action="store_true", help="Don't actually book.")
    p.add_argument("--no-wait", action="store_true", help="Skip waiting for release.")
    p.add_argument("--history", action="store_true",
                   help="Print the recorded release-time history and exit.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.history:
        print(release_history.summarize())
        return 0

    if state_store.is_paused() and not args.plan:
        _stamp("Automation is PAUSED (kill switch on) — doing nothing tonight.")
        return 0

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        _stamp(f"Configuration problem: {exc}")
        return 2

    tz = cfg.release.tz
    now = datetime.now(tz)

    if args.date:
        try:
            play_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            _stamp(f"Invalid --date {args.date!r}; expected YYYY-MM-DD.")
            return 2
    else:
        play_date = compute_play_date(
            now, cfg.release.days_ahead, cfg.release.release_time,
            cfg.release.retry_window_seconds,
        )

    weekday_key = WEEKDAY_KEYS[play_date.weekday()]
    schedule = cfg.raw.get("weekly_schedule") or {}
    times = list(schedule.get(weekday_key) or [])
    players = int(schedule.get("players", cfg.booking.players))

    release_at = cfg.release.release_moment_for(play_date)
    _stamp(
        f"Play date {play_date} ({weekday_key.title()}); release {release_at.isoformat()}; "
        f"times {times or 'NONE (skip)'}; players {players}."
    )
    if times:
        # Apply tonight's times before describing the fallback — the window is
        # measured from them.
        cfg.booking.preferred_times = times
        cfg.booking.players = players
        booker = TeeBooker(cfg, None, log=_stamp)
        window, target = booker.fallback_window(), booker.fallback_target()
        _stamp(
            f"Target {target[1]}. If none of those is bookable, it books the "
            f"closest available time to it — {window}."
            if window and target
            else "If none of those is bookable, nothing gets booked "
                 "(booking.fallback is off)."
        )

    if state_store.is_skipped(play_date) and not args.plan:
        _stamp(f"{play_date} is on the skip list — skipping (per dashboard command).")
        return 0

    if not times:
        _stamp(f"No times configured for {weekday_key.title()} — skipping tonight.")
        return 0

    if args.plan:
        return 0

    try:
        creds = Credentials.from_env(args.env)
    except ConfigError as exc:
        _stamp(f"Configuration problem: {exc}")
        return 2

    if args.no_wait:
        result = TeeBooker(cfg, creds, log=_stamp).run(play_date, dry_run=args.dry_run)
    else:
        # Wait until shortly before release, then let run() log in and hold the
        # final seconds — so we authenticate before the release surge, not during.
        prelogin = int(getattr(cfg.release, "prelogin_seconds", 60) or 0)
        login_at = release_at - timedelta(seconds=prelogin)
        # Never hold the Mac awake for hours. A job replayed well after its slot
        # (launchd runs a missed calendar job on the next wake) would otherwise
        # sit on tomorrow's release with caffeinate holding sleep off.
        waiting = (login_at - now).total_seconds()
        if too_long_to_wait(login_at, now):
            _stamp(f"The next release ({release_at.isoformat()}) is {waiting / 3600:.1f}h "
                   "away — exiting instead of holding the Mac awake. The scheduled job "
                   "fires again before then.")
            return 0
        _stamp(f"Waiting until {login_at.isoformat()} to log in ({prelogin}s before release) ...")
        wait_until(
            login_at,
            tz,
            warmup_seconds=cfg.release.warmup_seconds,
            on_warmup=lambda rem: _stamp(f"Logging in in {rem:.0f}s ..."),
            log=_stamp,
        )
        result = TeeBooker(cfg, creds, log=_stamp).run(
            play_date, dry_run=args.dry_run, release_at=release_at, tz=tz
        )
    msg = (
        f"{'✅' if result.success else '❌'} {play_date} ({weekday_key.title()}): "
        f"{result.message}"
    )
    notify(msg, creds.notify_webhook_url, log=_stamp)
    if result.screenshot:
        _stamp(f"Screenshot: {result.screenshot}")

    # Log when the sheet actually released, to learn the real release time over
    # several nights (only for genuine waited runs — not dry-runs or --no-wait).
    if not args.dry_run and not args.no_wait:
        rec = release_history.record(
            play_date=play_date,
            weekday=weekday_key.title(),
            nominal_release=release_at,
            released_at=result.release_detected_at,
            booked=result.success,
            booked_time=result.booked_time,
            earliest_time=result.earliest_time,
            attempts=result.attempts,
            outcome=result.message,
        )
        if rec["seconds_after_nominal"] is not None:
            _stamp(
                f"Sheet released {release_history._fmt_delta(rec['seconds_after_nominal'])} "
                f"after the nominal {cfg.release.release_time} "
                "(logged to release history)."
            )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
