"""Lightweight notifications.

Currently supports a generic webhook (Slack/Discord-style "text" payload) plus
console output. Kept dependency-free by using urllib from the standard library.
"""

from __future__ import annotations

import json
import urllib.request


def notify(message: str, webhook_url: str = "", *, log=print) -> None:
    log(message)
    if not webhook_url:
        return
    try:
        data = json.dumps({"text": message}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)  # noqa: S310 (user-supplied URL)
    except Exception as exc:  # noqa: BLE001 — notifications must never crash the run
        log(f"(notification failed: {exc})")
