"""Fixtures for the end-to-end booking tests.

These tests drive the real `TeeBooker` (and a real headless Chromium) against a
local mock portal. They SKIP cleanly when Flask, Playwright, or a launchable
browser isn't available, so a plain `pytest -q` on a machine without the browser
stack stays green.

In most environments Playwright uses the browser it installed via
`playwright install chromium`. If a different Chromium build is pre-installed
(e.g. some sandboxes), point the tests at it with:

    E2E_CHROMIUM_PATH=/path/to/chrome pytest tests/e2e
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

# Make `tee_booker` importable no matter where pytest is invoked from.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HERE = Path(__file__).resolve().parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def chromium_path() -> str | None:
    return os.environ.get("E2E_CHROMIUM_PATH")


@pytest.fixture(scope="session", autouse=True)
def _require_browser(chromium_path):
    """Skip the whole e2e suite unless a headless Chromium can actually launch."""
    pw = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")
    try:
        with pw.sync_playwright() as p:
            kw = {"headless": True}
            if chromium_path:
                kw["executable_path"] = chromium_path
            browser = p.chromium.launch(**kw)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"Chromium can't launch for e2e ({exc}). "
            "Run `playwright install chromium` (or set E2E_CHROMIUM_PATH)."
        )


@pytest.fixture(autouse=True)
def _patch_executable_path(chromium_path):
    """Inject executable_path into TeeBooker's launch when E2E_CHROMIUM_PATH is set.

    Lets the unmodified project code run against a pre-installed browser build.
    """
    if not chromium_path:
        yield
        return
    from playwright.sync_api._generated import BrowserType

    orig = BrowserType.launch

    def launch(self, **kw):
        kw.setdefault("executable_path", chromium_path)
        return orig(self, **kw)

    BrowserType.launch = launch
    try:
        yield
    finally:
        BrowserType.launch = orig


@pytest.fixture(scope="session")
def mock_server():
    """Start the Flask mock portal in a subprocess for the test session."""
    pytest.importorskip("flask", reason="Flask not installed")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "mock_portal.py"), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/count", timeout=1)
                ready = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        if not ready:
            pytest.skip("mock portal did not come up")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


@pytest.fixture(autouse=True)
def _reset_bookings(mock_server):
    urllib.request.urlopen(mock_server + "/reset", timeout=2)
    yield


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("GOLF_USERNAME", "member123")
    monkeypatch.setenv("GOLF_PASSWORD", "hunter2")


@pytest.fixture
def make_cfg(mock_server, tmp_path):
    """Factory: build a validated Config pointing at the mock portal."""
    from tee_booker.config import Config

    def _make(preferred_times, players):
        text = f"""
club:
  login_url: "{mock_server}/login"
  tee_sheet_url: "{mock_server}/teesheet?date={{date}}"
  date_url_format: "%Y-%m-%d"
release:
  days_ahead: 14
  retry_window_seconds: 8
  retry_interval_seconds: 0.5
booking:
  preferred_times: ["08:00 AM"]
  players: 2
selectors:
  username: "#username"
  password: "#password"
  login_button: "button[type=submit]"
  login_success_marker: ".member-dashboard"
  time_slot: ".teetime-slot"
  time_slot_label: ".slot-time"
  book_button: "button.book"
  slot_players_label: ".slot-party"
  confirmation_marker: ".booking-confirmed"
runtime:
  headless: true
  screenshot_on_error: false
"""
        path = tmp_path / "config.yaml"
        path.write_text(text)
        cfg = Config.load(str(path))
        cfg.booking.preferred_times = list(preferred_times)
        cfg.booking.players = players
        return cfg

    return _make
