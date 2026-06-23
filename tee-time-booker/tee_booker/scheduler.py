"""Precise scheduling for the release-time race.

The hard part of grabbing a tee time the instant it opens is firing at exactly
the right moment. This module sleeps efficiently until just before the target,
then hands control back so the booker can attempt immediately at the instant.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo


def now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def seconds_until(target: datetime, tz: ZoneInfo) -> float:
    """Seconds from now until target (negative if target is in the past)."""
    return (target - datetime.now(tz)).total_seconds()


def wait_until(
    target: datetime,
    tz: ZoneInfo,
    *,
    warmup_seconds: int = 30,
    on_warmup: Callable[[float], None] | None = None,
    log: Callable[[str], None] = print,
) -> None:
    """Block until `target`.

    Sleeps in coarse chunks while far away, then busy-waits the final second for
    sub-second accuracy. Calls `on_warmup` once when `warmup_seconds` remain so
    the caller can pre-warm a browser/login before the race.
    """
    warmed = False
    while True:
        remaining = seconds_until(target, tz)
        if remaining <= 0:
            return
        if not warmed and remaining <= warmup_seconds:
            warmed = True
            if on_warmup is not None:
                on_warmup(remaining)
        if remaining > 60:
            log(f"Waiting {remaining/60:.1f} min until release at {target.isoformat()} ...")
            time.sleep(min(remaining - 60, 300))
        elif remaining > 1.5:
            time.sleep(remaining - 1.0)
        else:
            # Final stretch: busy-wait for precision.
            end = time.monotonic() + remaining
            while time.monotonic() < end:
                time.sleep(0.001)
            return
