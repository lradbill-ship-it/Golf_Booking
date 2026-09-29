#!/usr/bin/env python3
"""Earlier-time watcher: flag a better tee time than the one we hold. READ-ONLY.

The nightly race books the closest time it can get at release. Some mornings
are gone at that instant — PCC's Tuesday sheet is empty from the first tee until
9:40 AM — so it can land hours from the target. This re-checks those dates a few
times a day. Each run:

  1. Reads your reservations and picks the ones worth watching: tee times the
     nightly booker settled for, not ones you booked by hand. (Full rules:
     tee_booker/earlier_watch.py.)
  2. Opens each of those dates' sheets and records the earliest time open to
     your party — the timeline that shows whether a blocked morning ever gives
     times back (`--history`).
  3. When a time at least `earlier_watch.min_improvement_minutes` closer to that
     day's target is open, it flags it: in the log, on the dashboard, and via
     the notify webhook if one is set. Once per opening, not every run.

It never books, never cancels, and never touches the cart.

Flags:
  --plan      Read reservations, print which dates it would watch, and exit.
  --history   Print the recorded watch history and exit.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from tee_booker import earlier_watch as W
from tee_booker import release_history, state_store
from tee_booker.booker import TeeBooker
from tee_booker.config import Config, ConfigError, Credentials
from tee_booker.notify import notify
from tee_booker.reservations import fetch_reservations


def _stamp(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="watch_earlier", description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("--plan", action="store_true", help="Print what it would watch and exit.")
    p.add_argument("--history", action="store_true", help="Print the watch history and exit.")
    return p


def main(argv=None, *, now=None) -> int:
    args = build_parser().parse_args(argv)
    if args.history:
        print(W.summarize(W.HISTORY_FILE))
        return 0
    if state_store.is_paused():
        _stamp("Automation is PAUSED (kill switch on) — not watching.")
        return 0
    try:
        cfg = Config.load(args.config)
        creds = Credentials.from_env(args.env)
    except ConfigError as exc:
        _stamp(f"Configuration problem: {exc}")
        return 2

    wc = cfg.earlier_watch
    now = now or datetime.now(cfg.release.tz)
    if W.in_quiet_window(now, wc.quiet_start, wc.quiet_end):
        _stamp(f"{now:%H:%M} is inside the quiet window {wc.quiet_start}-{wc.quiet_end} "
               "(kept clear for the midnight race) — not running.")
        return 0
    if not W.acquire_lock(W.LOCK_FILE):
        _stamp("Another earlier-time check is already running — exiting.")
        return 0
    try:
        return _run(cfg, creds, now=now, plan_only=args.plan)
    finally:
        W.release_lock(W.LOCK_FILE)


def _run(cfg, creds, *, now, plan_only: bool) -> int:
    wc = cfg.earlier_watch
    reservations = fetch_reservations(cfg, creds, log=_stamp)
    schedule = cfg.raw.get("weekly_schedule") or {}
    players = int(schedule.get("players", cfg.booking.players))
    nightly = release_history.load(release_history.HISTORY_FILE)
    helds, notes = W.plan(
        reservations,
        today=now.date(),
        weekly_schedule=schedule,
        players=players,
        skip_dates=state_store.load_skip_dates(),
        owned=W.owned_times(nightly),
        first_tee=W.first_tee_published(nightly),
        min_days_ahead=wc.min_days_ahead,
        min_improvement=wc.min_improvement_minutes,
    )
    _stamp(f"Earlier-time check: read {len(reservations)} reservation(s); "
           f"{len(helds)} worth watching.")
    if not reservations:
        # Unreadable looks exactly like empty from here — say so, not "all clear".
        _stamp("  Read ZERO reservations: either you hold none, or the portal read failed.")
    for iso, why in notes:
        _stamp(f"  {iso}: {why}")
    for h in helds:
        _stamp(f"  {h.play_date} ({h.weekday.title()}): holding {h.held_label}, "
               f"target {h.target_label} — watching")
    if plan_only:
        return 0
    if not helds:
        W.record("run", path=W.HISTORY_FILE, reservations=len(reservations), checked=0,
                 summary=("nothing worth watching" if reservations
                          else "read 0 reservations — portal read may have failed"))
        return 0

    previous = W.last_scan_by_date(W.load(W.HISTORY_FILE))
    run = TeeBooker(cfg, creds, log=_stamp).scan_for_earlier(
        helds, min_improvement=wc.min_improvement_minutes
    )
    openings = []
    for sc in run.scans:
        iso = str(sc.held.play_date)
        if W.is_new_opening(previous.get(iso), sc.better_time):
            openings.append(sc)
        W.record("scan", path=W.HISTORY_FILE, play_date=iso, weekday=sc.held.weekday,
                 held_time=sc.held.held_label, target=sc.held.target_label,
                 earliest_open=sc.earliest_open, better_time=sc.better_time,
                 improvement=sc.improvement)
    for sc in openings:
        h = sc.held
        notify(f"⛳ Earlier tee time open: {h.weekday.title()} {h.play_date:%b %-d} at "
               f"{sc.better_time} — you hold {h.held_label} ({sc.improvement} min closer to "
               f"{h.target_label}). Book it on the portal, then cancel the {h.held_label} "
               "on the dashboard.", creds.notify_webhook_url, log=_stamp)
    if run.error:
        _stamp(f"Check ended early: {run.error}")
    open_now = [s for s in run.scans if s.better_time]
    W.record("run", path=W.HISTORY_FILE, reservations=len(reservations),
             checked=len(run.scans), blocked=run.blocked, error=run.error or None,
             summary=(f"checked {len(run.scans)} of {len(helds)} date(s); "
                      + (f"better time open on {len(open_now)}" if open_now
                         else "nothing better open")))
    return 1 if (run.error or run.blocked or len(run.scans) < len(helds)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
