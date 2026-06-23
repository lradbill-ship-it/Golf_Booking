"""Shared runtime state: the kill-switch flag and the per-date skip list.

Used by both the dashboard (writes) and nightly.py (reads). Everything lives
under <project>/state/, which is gitignored.
"""

from __future__ import annotations

import json
import os
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))      # .../tee_booker
_PROJECT = os.path.dirname(_HERE)                        # project root
STATE_DIR = os.path.join(_PROJECT, "state")
SKIPS_FILE = os.path.join(STATE_DIR, "skips.json")
PAUSE_FLAG = os.path.join(STATE_DIR, "paused.flag")


def _ensure_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


# -- kill switch ------------------------------------------------------------- #

def is_paused() -> bool:
    return os.path.exists(PAUSE_FLAG)


def set_paused(paused: bool) -> None:
    if paused:
        _ensure_dir()
        with open(PAUSE_FLAG, "w") as fh:
            fh.write("paused\n")
    elif os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)


# -- per-date skip list ------------------------------------------------------ #

def load_skip_dates() -> list[str]:
    """Sorted list of ISO date strings the auto-booker should skip."""
    try:
        with open(SKIPS_FILE) as fh:
            data = json.load(fh)
        return sorted(set(data.get("skip_dates", [])))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_skip_dates(dates) -> list[str]:
    _ensure_dir()
    cleaned = sorted({str(d) for d in dates})
    with open(SKIPS_FILE, "w") as fh:
        json.dump({"skip_dates": cleaned}, fh, indent=2)
    return cleaned


def add_skip_dates(iso_dates) -> list[str]:
    return save_skip_dates(set(load_skip_dates()) | {str(d) for d in iso_dates})


def remove_skip_dates(iso_dates) -> list[str]:
    return save_skip_dates(set(load_skip_dates()) - {str(d) for d in iso_dates})


def is_skipped(d: date) -> bool:
    return d.isoformat() in set(load_skip_dates())
