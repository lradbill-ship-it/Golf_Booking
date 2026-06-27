"""The kill-switch flag and per-date skip list shared by dashboard + nightly.

These touch the filesystem, so each test redirects state_store's paths into a
tmp dir to stay hermetic.
"""

import os
from datetime import date

import pytest

from tee_booker import state_store


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(state_store, "SKIPS_FILE", str(tmp_path / "skips.json"))
    monkeypatch.setattr(state_store, "PAUSE_FLAG", str(tmp_path / "paused.flag"))


def test_pause_flag_round_trip():
    assert state_store.is_paused() is False
    state_store.set_paused(True)
    assert state_store.is_paused() is True
    state_store.set_paused(False)
    assert state_store.is_paused() is False
    # Disarming twice is a no-op, not an error.
    state_store.set_paused(False)
    assert state_store.is_paused() is False


def test_skip_dates_add_dedup_and_sort():
    state_store.add_skip_dates(["2026-07-10", "2026-07-08"])
    state_store.add_skip_dates(["2026-07-08", "2026-07-09"])  # dup is ignored
    assert state_store.load_skip_dates() == ["2026-07-08", "2026-07-09", "2026-07-10"]


def test_remove_skip_dates():
    state_store.add_skip_dates(["2026-07-08", "2026-07-09"])
    state_store.remove_skip_dates(["2026-07-08"])
    assert state_store.load_skip_dates() == ["2026-07-09"]
    # Removing something absent is harmless.
    state_store.remove_skip_dates(["2026-12-25"])
    assert state_store.load_skip_dates() == ["2026-07-09"]


def test_is_skipped():
    state_store.add_skip_dates(["2026-07-08"])
    assert state_store.is_skipped(date(2026, 7, 8)) is True
    assert state_store.is_skipped(date(2026, 7, 9)) is False


def test_missing_file_reads_as_empty():
    assert state_store.load_skip_dates() == []


def test_corrupt_skips_file_reads_as_empty(tmp_path):
    with open(state_store.SKIPS_FILE, "w") as fh:
        fh.write("{not valid json")
    assert state_store.load_skip_dates() == []
