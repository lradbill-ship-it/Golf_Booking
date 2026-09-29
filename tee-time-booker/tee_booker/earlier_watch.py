"""Watch for an earlier tee time after the nightly race settles for less.

The nightly booker's always-book fallback takes the closest time it can get at
release. Some mornings are unavailable at that instant — PCC's Tuesday sheet is
empty from the first tee until 9:40 AM — so it lands hours from the target.
Blocks like that may hand slots back before play. `watch_earlier.py` re-checks
those dates and flags a better time when one opens. It is READ-ONLY: it never
books or cancels; moving to the better time is the member's call.

This module is the pure part (no browser): which reservations are worth
watching, what counts as better, the quiet window, and the append-only history
in ``state/earlier_watch.jsonl``. The sheet scan is
``TeeBooker.scan_for_earlier``.

A reservation is watched only when ALL hold:
  * confirmed, and the ONLY confirmed reservation on that date;
  * booked by this automation — the nightly history records exactly that date
    and time (a tee time booked by hand was chosen on purpose);
  * a scheduled weekday (non-empty `weekly_schedule`), not on the skip list;
  * the configured party size, and the portal says it can still be cancelled
    (a better time you couldn't swap to is no use);
  * at least `earlier_watch.min_days_ahead` days away;
  * LATER than the day's target by at least `min_improvement_minutes`, and
    later than the first tee time that sheet published — so an EARLIER time
    could exist. Only earlier times are ever flagged; a held time at or before
    the target is left alone even if the target itself opens.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))      # .../tee_booker
_PROJECT = os.path.dirname(_HERE)                        # project root
STATE_DIR = os.path.join(_PROJECT, "state")
HISTORY_FILE = os.path.join(STATE_DIR, "earlier_watch.jsonl")
LOCK_FILE = os.path.join(STATE_DIR, "earlier_watch.lock")

_WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday")


def _minutes(label) -> Optional[int]:
    from .booker import TeeBooker  # the one clock-label parser; no browser import
    return TeeBooker._parse_time_minutes(label or "")


def _fmt(minutes: int) -> str:
    from .booker import TeeBooker
    return TeeBooker._fmt_minutes(minutes)


@dataclass
class HeldBooking:
    """A reservation worth watching for a better time."""

    play_date: date
    weekday: str
    reservation_id: int
    players: int
    held_minutes: int
    target_minutes: int

    @property
    def held_label(self) -> str:
        return _fmt(self.held_minutes)

    @property
    def target_label(self) -> str:
        return _fmt(self.target_minutes)

    @property
    def distance(self) -> int:
        return abs(self.held_minutes - self.target_minutes)


@dataclass
class WatchScan:
    """One held date's tee sheet, as the watcher saw it."""

    held: HeldBooking
    earliest_open: Optional[str] = None   # earliest time our party could book
    better_time: Optional[str] = None     # closest-to-target slot, if better enough
    better_minutes: Optional[int] = None
    improvement: int = 0                  # minutes closer than held (0 = not better enough)


@dataclass
class WatchRun:
    """Everything one `TeeBooker.scan_for_earlier` session saw."""

    scans: list = field(default_factory=list)
    blocked: bool = False                 # hit Cloudflare's rate limiter
    error: str = ""


def improvement(held: HeldBooking, candidate_minutes: Optional[int]) -> int:
    """Minutes closer to the target a candidate is than what we hold (<=0 = not better)."""
    if candidate_minutes is None:
        return 0
    return held.distance - abs(candidate_minutes - held.target_minutes)


def in_quiet_window(now: datetime, start: str, end: str) -> bool:
    """True when `now`'s local clock time falls in [start, end), wrapping midnight."""
    t = now.time().replace(second=0, microsecond=0)
    s = datetime.strptime(start, "%H:%M").time()
    e = datetime.strptime(end, "%H:%M").time()
    if s <= e:
        return s <= t < e
    return t >= s or t < e


def owned_times(nightly_records) -> set:
    """(iso_date, minutes) pairs the nightly booker booked itself."""
    owned = set()
    for r in nightly_records or []:
        if r.get("booked") and r.get("play_date"):
            m = _minutes(r.get("booked_time"))
            if m is not None:
                owned.add((r["play_date"], m))
    return owned


def first_tee_published(nightly_records) -> dict:
    """iso_date -> minutes of the earliest tee time that date's sheet published."""
    out = {}
    for r in nightly_records or []:
        m = _minutes(r.get("earliest_time"))
        if m is not None and r.get("play_date"):
            out[r["play_date"]] = m
    return out


def plan(reservations, *, today: date, weekly_schedule: dict, players: int,
         skip_dates, owned: set, first_tee: dict, min_days_ahead: int,
         min_improvement: int):
    """Pick the reservations worth watching.

    Returns (helds, notes): `helds` is a list of HeldBooking, soonest first;
    `notes` is (iso_date, reason) for every held date passed over, so a run
    that watches nothing can say why.
    """
    by_date: dict = {}
    for r in reservations or []:
        when = r.when
        if when is None:
            continue
        by_date.setdefault(when.date(), []).append(r)

    skip = {str(d) for d in (skip_dates or [])}
    earliest_date = today + timedelta(days=min_days_ahead)
    helds, notes = [], []
    for d in sorted(by_date):
        confirmed = [r for r in by_date[d] if not r.cancelled]
        if d < earliest_date or not confirmed:
            continue  # too soon, or nothing held
        iso = d.isoformat()
        if len(confirmed) > 1:
            notes.append((iso, f"holds {len(confirmed)} reservations — not watched"))
            continue
        r = confirmed[0]
        weekday = _WEEKDAY_KEYS[d.weekday()]
        times = list((weekly_schedule or {}).get(weekday) or [])
        target = next((m for m in map(_minutes, times) if m is not None), None)
        held_m = r.when.hour * 60 + r.when.minute
        if iso in skip:
            notes.append((iso, "on the skip list — not watched"))
            continue
        if target is None:
            notes.append((iso, f"{weekday.title()} isn't a scheduled day — not watched"))
            continue
        if (iso, held_m) not in owned:
            notes.append((iso, f"{_fmt(held_m)} was booked by hand — not watched"))
            continue
        if r.players != players:
            notes.append((iso, f"{r.players} player(s), not {players} — not watched"))
            continue
        if not r.eligible_cancel:
            notes.append((iso, "portal says it can't be cancelled — not watched"))
            continue
        held = HeldBooking(play_date=d, weekday=weekday, reservation_id=r.id,
                           players=r.players, held_minutes=held_m, target_minutes=target)
        if held_m <= target:
            notes.append((iso, f"{held.held_label} is already "
                               + ("on" if held_m == target else "earlier than")
                               + f" the {held.target_label} target"))
            continue
        if held_m - target < min_improvement:
            notes.append((iso, f"{held.held_label} is within {min_improvement} min of the "
                               f"{held.target_label} target"))
            continue
        pub = first_tee.get(iso)
        if pub is not None and held_m <= pub:
            notes.append((iso, f"{held.held_label} was the first tee time that sheet "
                               "published — nothing earlier exists"))
            continue
        helds.append(held)
    return helds, notes


# -- history ----------------------------------------------------------------- #

def record(kind: str, *, path: str = HISTORY_FILE, **fields_) -> dict:
    """Append one record. kind: "run" | "scan"."""
    rec = {"recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "kind": kind}
    rec.update(fields_)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    return rec


def load(path: str = HISTORY_FILE) -> list:
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return out


def last_scan_by_date(records) -> dict:
    """iso_date -> that date's most recent scan record."""
    out = {}
    for r in records or []:
        if r.get("kind") == "scan" and r.get("play_date"):
            out[r["play_date"]] = r
    return out


def latest_run(records):
    """(run_record, [its scan records]) for the most recent completed run.

    A run writes its scans, then its "run" record, so the latest run's scans are
    those after the previous "run" record. (None, []) when nothing has run.
    """
    records = list(records or [])
    run_idx = [i for i, r in enumerate(records) if r.get("kind") == "run"]
    if not run_idx:
        return None, []
    last = run_idx[-1]
    start = run_idx[-2] + 1 if len(run_idx) > 1 else 0
    scans = [r for r in records[start:last] if r.get("kind") == "scan"]
    return records[last], scans


def is_new_opening(previous: Optional[dict], better_time: Optional[str]) -> bool:
    """Whether a better time is worth announcing: present, and not already announced."""
    if not better_time:
        return False
    return previous is None or previous.get("better_time") != better_time


def summarize(path: str = HISTORY_FILE) -> str:
    """When each watched date's earliest open time changed."""
    records = load(path)
    if not records:
        return "No earlier-time checks recorded yet."
    lines = []
    runs = [r for r in records if r.get("kind") == "run"]
    if runs:
        last = runs[-1]
        lines.append(f"Last check {last['recorded_at']} — {last.get('summary', '')}")
    series: dict = {}
    for r in records:
        if r.get("kind") == "scan" and r.get("play_date"):
            series.setdefault(r["play_date"], []).append(r)
    if series:
        lines.append("")
        lines.append("Earliest time open to you, per watched date "
                     "(a change means the sheet gave times back):")
        for iso in sorted(series):
            recs = series[iso]
            lines.append(f"  {iso} {recs[0].get('weekday', '').title():<9} holding "
                         f"{recs[-1].get('held_time')} (target {recs[0].get('target')}), "
                         f"{len(recs)} check(s):")
            prev = object()
            for r in recs:
                cur = (r.get("earliest_open") or "nothing", r.get("better_time"))
                if cur != prev:
                    lines.append(
                        f"      {r['recorded_at'][:16].replace('T', ' ')}  earliest open {cur[0]}"
                        + (f"  ★ better: {r['better_time']} ({r['improvement']} min closer)"
                           if r.get("better_time") else "")
                    )
                    prev = cur
    return "\n".join(lines)


# -- one run at a time ------------------------------------------------------- #

def acquire_lock(path: str = LOCK_FILE, *, stale_after_s: int = 1800) -> bool:
    """Create the lock file; False if another run holds a fresh one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.exists(path) and (datetime.now().timestamp() - os.path.getmtime(path)) > stale_after_s:
            os.remove(path)  # a crashed run left it behind
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def release_lock(path: str = LOCK_FILE) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
