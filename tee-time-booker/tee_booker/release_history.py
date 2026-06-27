"""Append-only log of when the tee sheet *actually* releases each night.

PCC's nominal release is 00:01 ET, but it has been observed to release ~13
minutes later. This module records one JSON object per line in
``state/release_history.jsonl`` so the real release time can be learned over
several nights — once it's consistent, the polling window can be tightened
(see HANDOFF.md §7). Pure stdlib; safe to import without Playwright.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))      # .../tee_booker
_PROJECT = os.path.dirname(_HERE)                        # project root
STATE_DIR = os.path.join(_PROJECT, "state")
HISTORY_FILE = os.path.join(STATE_DIR, "release_history.jsonl")


def record(
    *,
    play_date,
    weekday: str,
    nominal_release: Optional[datetime],
    released_at: Optional[datetime],
    booked: bool,
    booked_time: Optional[str],
    attempts: int,
    outcome: str,
    path: str = HISTORY_FILE,
) -> dict:
    """Append one night's outcome and return the record that was written.

    ``released_at`` is the wall-clock moment the fresh sheet first showed slot
    cards (None if it never released within the poll window). When both
    timestamps are present, ``seconds_after_nominal`` is how late the release
    was relative to the nominal 00:01.
    """
    delta = None
    if nominal_release is not None and released_at is not None:
        delta = round((released_at - nominal_release).total_seconds(), 1)

    rec = {
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "play_date": str(play_date),
        "weekday": weekday,
        "nominal_release": nominal_release.isoformat() if nominal_release else None,
        "released_at": released_at.isoformat() if released_at else None,
        "seconds_after_nominal": delta,
        "booked": bool(booked),
        "booked_time": booked_time,
        "attempts": int(attempts),
        "outcome": outcome,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def load(path: str = HISTORY_FILE) -> list[dict]:
    """All recorded nights, oldest first. Skips any malformed lines."""
    out: list[dict] = []
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


def _fmt_delta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    sign = "+" if seconds >= 0 else "-"
    s = int(abs(seconds))
    return f"{sign}{s // 60}m{s % 60:02d}s"


def summarize(path: str = HISTORY_FILE) -> str:
    """A human-readable table of recorded nights plus release-time stats."""
    records = load(path)
    if not records:
        return "No release history recorded yet."

    lines = [
        f"Release history ({len(records)} night(s)) — nominal release is 00:01 ET",
        "",
        f"{'play date':<12} {'weekday':<10} {'released at':<10} {'vs 00:01':<9} booked",
        f"{'-'*12} {'-'*10} {'-'*10} {'-'*9} {'-'*6}",
    ]
    deltas: list[float] = []
    for r in records:
        released = r.get("released_at")
        released_hm = released[11:19] if released else "never"
        delta = r.get("seconds_after_nominal")
        if delta is not None:
            deltas.append(delta)
        booked = r.get("booked_time") if r.get("booked") else ("yes" if r.get("booked") else "no")
        lines.append(
            f"{r.get('play_date',''):<12} {r.get('weekday',''):<10} "
            f"{released_hm:<10} {_fmt_delta(delta):<9} {booked or 'no'}"
        )

    if deltas:
        ordered = sorted(deltas)
        n = len(ordered)
        median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
        lines += [
            "",
            f"Observed release delay over {n} night(s) with a release:",
            f"  earliest {_fmt_delta(ordered[0])}   "
            f"median {_fmt_delta(median)}   latest {_fmt_delta(ordered[-1])}",
        ]
        if n < 3:
            lines.append("  (need a few more nights before tightening the poll window)")
    else:
        lines.append("\nNo night has recorded an actual release time yet.")
    return "\n".join(lines)
