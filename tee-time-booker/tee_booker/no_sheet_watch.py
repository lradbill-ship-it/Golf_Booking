"""Dates the booker ruled "no tee sheet published at all", kept for a re-check.

Why this exists
---------------
On 2026-11-12 the 09:00 release published nothing whatsoever for 2026-11-26
(Thanksgiving). The run did the right thing: it refused to buy a consolation
tee time on some other day (the 2026-09-30 trap) and reported the day as
closed. But the nightly schedule only ever looks at ONE date, 14 days out — so
once that morning passed, nobody was ever going to look at Nov 26 again. If the
club posts a holiday sheet late, or a sheet simply releases behind schedule, the
date is lost in silence.

This module is the list of dates still owed a look. A date joins it when a run
ends with the "no sheet at all" verdict, and leaves it when:

  * the re-check books it,
  * the portal's own reservation list already shows a booking for it
    (e.g. the member booked it by phone), or
  * it is no longer in the future.

Three-state care: a date is only ever DROPPED on a positive reading. "We could
not read the reservation list" keeps the date pending — an unreadable answer is
not a "no" (and not a "yes" either, which is why it also never books).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime
from typing import Optional

_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(_PROJECT, "state")
PENDING_FILE = os.path.join(STATE_DIR, "no_sheet_dates.json")

# How many pending dates one run will actually re-check. A systematic failure
# (the club moves the release time again, say) would otherwise flag EVERY day
# and turn a quick sweep into an hour of browsing. Nearest dates win.
MAX_PER_RUN = 5
# How many we are willing to remember at all, for the same reason.
MAX_STORED = 30


@dataclass
class Pending:
    """One date awaiting a re-check, with the times to use if a sheet appears.

    `times` and `players` are derived from the CURRENT config at sweep time,
    never stored — a stored copy would quietly go stale the moment the weekly
    schedule changes (as it did for Sat/Sun on 2026-11-11).
    """

    play_date: date_cls
    weekday: str
    first_seen: Optional[str] = None
    checks: int = 0
    last_checked: Optional[str] = None
    times: list = field(default_factory=list)
    players: int = 2


def _ensure_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def _iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def load(path: Optional[str] = None) -> dict:
    """The stored map {"YYYY-MM-DD": {...}}. Unreadable file -> empty map."""
    path = path or PENDING_FILE
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(entries: dict, path: Optional[str] = None) -> dict:
    path = path or PENDING_FILE
    _ensure_dir()
    # Keep the nearest dates when over the cap: those are the ones still
    # winnable. Dropping the far ones loses the least.
    if len(entries) > MAX_STORED:
        keep = sorted(entries)[:MAX_STORED]
        entries = {k: entries[k] for k in keep}
    with open(path, "w") as fh:
        json.dump(entries, fh, indent=2, sort_keys=True)
    return entries


def remember(play_date, weekday: str, *, now: Optional[datetime] = None,
             path: Optional[str] = None) -> dict:
    """Add (or refresh) a date. Returns the entry written."""
    path = path or PENDING_FILE
    entries = load(path)
    key = _iso(play_date)
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    entry = entries.get(key) or {}
    entry.setdefault("first_seen", stamp)
    entry["weekday"] = weekday
    entry.setdefault("checks", 0)
    entries[key] = entry
    save(entries, path)
    return entry


def forget(play_date, *, path: Optional[str] = None) -> bool:
    """Drop a date. True if it was there."""
    path = path or PENDING_FILE
    entries = load(path)
    if entries.pop(_iso(play_date), None) is None:
        return False
    save(entries, path)
    return True


def mark_checked(play_date, *, now: Optional[datetime] = None,
                 path: Optional[str] = None) -> None:
    path = path or PENDING_FILE
    entries = load(path)
    entry = entries.get(_iso(play_date))
    if entry is None:
        return
    entry["checks"] = int(entry.get("checks") or 0) + 1
    entry["last_checked"] = (now or datetime.now()).isoformat(timespec="seconds")
    save(entries, path)


def prune(entries: dict, today: date_cls):
    """Split into (still future, dropped-as-past). Today itself is dropped.

    A sheet appearing on the morning of play is no use to a 14-day-ahead
    booker, and silently buying a round starting in two hours is not what the
    member asked for. The date stops being watched the day it arrives.
    """
    kept, dropped = {}, []
    for key, value in entries.items():
        try:
            d = datetime.strptime(key, "%Y-%m-%d").date()
        except ValueError:
            dropped.append(key)   # unparseable key: don't keep it forever
            continue
        (kept.__setitem__(key, value) if d > today else dropped.append(key))
    return kept, dropped


def due(entries: dict, today: date_cls, *, limit: int = MAX_PER_RUN) -> list:
    """The dates to re-check this run: still future, nearest first, capped."""
    kept, _dropped = prune(entries, today)
    out = []
    for key in sorted(kept)[:limit]:
        value = kept[key] or {}
        out.append(Pending(
            play_date=datetime.strptime(key, "%Y-%m-%d").date(),
            weekday=str(value.get("weekday") or ""),
            first_seen=value.get("first_seen"),
            checks=int(value.get("checks") or 0),
            last_checked=value.get("last_checked"),
        ))
    return out


def summarize(entries: Optional[dict] = None, *, today: Optional[date_cls] = None,
              path: Optional[str] = None) -> str:
    entries = load(path or PENDING_FILE) if entries is None else entries
    kept, _ = prune(entries, today or date_cls.today())
    if not kept:
        return "No dates are awaiting a re-check."
    lines = ["Dates with no tee sheet, awaiting a re-check:"]
    for key in sorted(kept):
        v = kept[key] or {}
        lines.append(
            f"  {key} ({v.get('weekday') or '?'}) — first seen "
            f"{(v.get('first_seen') or '?')[:10]}, {v.get('checks') or 0} re-check(s)"
        )
    return "\n".join(lines)


@dataclass
class RecheckOutcome:
    """What the sweep found for one date. `status` is the machine-readable part.

    Statuses that DROP the date: booked, already_booked.
    Every other status keeps it pending — including the ones that mean "I could
    not tell", because an unreadable answer is not a negative one.
    """

    play_date: date_cls
    weekday: str = ""
    status: str = "still_empty"
    cards: int = 0
    booked_time: Optional[str] = None
    message: str = ""

    DROPS = ("booked", "already_booked")

    @property
    def resolved(self) -> bool:
        return self.status in self.DROPS

    @property
    def booked(self) -> bool:
        return self.status == "booked"
