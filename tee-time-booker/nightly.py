#!/usr/bin/env python3
"""Nightly auto-booker for the standing weekly schedule.

Run by launchd shortly before midnight. It:
  1. Computes the play date that opens at the upcoming 00:01 release
     (the calendar date of that release + release.days_ahead).
  2. Looks up that weekday's preferred times in config's `weekly_schedule`.
     An empty list means "skip tonight" (e.g. Mondays).
  3. Waits until the exact release instant, then books `players` golfers at the
     first available preferred time.

Flags:
  --plan        Print what it would do (date, weekday, times) and exit. No browser.
  --dry-run     Log in and report the slot it would book, without booking.
  --no-wait     Skip waiting for the release instant (book/inspect immediately).
  --date DATE   Override the computed play date (YYYY-MM-DD), for testing.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from tee_booker import state_store
from tee_booker.booker import TeeBooker
from tee_booker.config import Config, ConfigError, Credentials
from tee_booker.notify import notify
from tee_booker.scheduler import wait_until

# Monday=0 .. Sunday=6, matching date.weekday().
WEEKDAY_KEYS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]


def _stamp(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def compute_play_date(now: datetime, days_ahead: int):
    """The play date that opens at the next 00:01 release.

    Run in the evening, the next release is tomorrow's 00:01, which opens
    (tomorrow + days_ahead). After midnight it's (today + days_ahead).
    """
    release_cal_date = (now + timedelta(days=1)).date() if now.hour >= 12 else now.date()
    return release_cal_date + timedelta(days=days_ahead)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nightly", description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("--date", help="Override play date YYYY-MM-DD (testing).")
    p.add_argument("--plan", action="store_true", help="Print the plan and exit.")
    p.add_argument("--dry-run", action="store_true", help="Don't actually book.")
    p.add_argument("--no-wait", action="store_true", help="Skip waiting for release.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

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
        play_date = compute_play_date(now, cfg.release.days_ahead)

    weekday_key = WEEKDAY_KEYS[play_date.weekday()]
    schedule = cfg.raw.get("weekly_schedule") or {}
    times = list(schedule.get(weekday_key) or [])
    players = int(schedule.get("players", cfg.booking.players))

    release_at = cfg.release.release_moment_for(play_date)
    _stamp(
        f"Play date {play_date} ({weekday_key.title()}); release {release_at.isoformat()}; "
        f"times {times or 'NONE (skip)'}; players {players}."
    )

    if state_store.is_skipped(play_date) and not args.plan:
        _stamp(f"{play_date} is on the skip list — skipping (per dashboard command).")
        return 0

    if not times:
        _stamp(f"No times configured for {weekday_key.title()} — skipping tonight.")
        return 0

    # Apply tonight's choices to the config the booker reads.
    cfg.booking.preferred_times = times
    cfg.booking.players = players

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
        # final seconds — so we authenticate before the 12:01 surge, not during.
        prelogin = int(getattr(cfg.release, "prelogin_seconds", 60) or 0)
        login_at = release_at - timedelta(seconds=prelogin)
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
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
