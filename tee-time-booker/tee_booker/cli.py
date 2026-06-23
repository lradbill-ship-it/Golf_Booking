"""Command-line interface.

Subcommands:
  book      Book now (or schedule for the release instant) for a play date.
  schedule  Alias for `book --wait` — wait until the release moment, then book.
  inspect   Open the portal in a visible browser so you can capture selectors.
"""

from __future__ import annotations

import argparse
import sys

from .booker import TeeBooker
from .config import Config, ConfigError, Credentials
from .notify import notify
from .scheduler import wait_until


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    sp.add_argument("--env", default=".env", help="Path to .env file")
    sp.add_argument("--date", help="Play date YYYY-MM-DD (overrides config)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tee-booker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    book = sub.add_parser("book", help="Attempt a booking")
    _add_common(book)
    book.add_argument(
        "--wait",
        action="store_true",
        help="Wait until the computed release instant, then book (the race).",
    )
    book.add_argument(
        "--dry-run",
        action="store_true",
        help="Log in and report what would be booked without clicking book.",
    )

    schedule = sub.add_parser("schedule", help="Wait for release, then book")
    _add_common(schedule)
    schedule.add_argument("--dry-run", action="store_true")

    inspect = sub.add_parser("inspect", help="Open the portal to capture selectors")
    _add_common(inspect)

    return parser


def _load(args) -> tuple[Config, Credentials]:
    cfg = Config.load(args.config)
    creds = Credentials.from_env(args.env)
    return cfg, creds


def _run_booking(cfg: Config, creds: Credentials, *, play_date, wait: bool, dry_run: bool) -> int:
    booker = TeeBooker(cfg, creds)

    if wait:
        release_at = cfg.release.release_moment_for(play_date)
        print(
            f"Play date {play_date} opens at {release_at.isoformat()} "
            f"({cfg.release.timezone}). Waiting..."
        )
        wait_until(
            release_at,
            cfg.release.tz,
            warmup_seconds=cfg.release.warmup_seconds,
            on_warmup=lambda rem: print(f"Release in {rem:.0f}s — getting ready."),
        )
        print("Release! Attempting booking now.")

    result = booker.run(play_date, dry_run=dry_run)
    msg = (
        f"✅ {result.message}" if result.success else f"❌ {result.message}"
    )
    notify(msg, creds.notify_webhook_url)
    if result.screenshot:
        print(f"Screenshot: {result.screenshot}")
    return 0 if result.success else 1


def _run_inspect(cfg: Config, creds: Credentials) -> int:
    from playwright.sync_api import sync_playwright

    print(
        "Opening a visible browser. Navigate your booking flow, right-click the "
        "username field / password field / login button / a tee-time slot / the "
        "book button, choose Inspect, and copy their CSS selectors into config.yaml.\n"
        "Close the browser window when done."
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context().new_page()
        page.goto(cfg.club.login_url)
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:  # noqa: BLE001
            pass
        browser.close()
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg, creds = _load(args)
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 2

    if args.command == "inspect":
        return _run_inspect(cfg, creds)

    play_date = cfg.booking.resolved_date(getattr(args, "date", None))
    wait = args.command == "schedule" or getattr(args, "wait", False)
    dry_run = getattr(args, "dry_run", False)
    return _run_booking(cfg, creds, play_date=play_date, wait=wait, dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
