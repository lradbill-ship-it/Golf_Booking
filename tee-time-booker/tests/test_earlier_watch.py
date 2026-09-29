"""The earlier-time watcher: what it watches, what it flags, and that it only looks.

Motivating case: PCC's Tuesday sheet is empty from the first tee until 9:40 AM
at release, so the nightly fallback lands at 9:40. The watcher re-checks such
dates and flags an EARLIER time if the block gives one back. It never books.

Every date here is a literal paired with a literal `today`/`now` — nothing is
read from the real clock, so these can't age into failures.
"""

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from tee_booker import earlier_watch as W
from tee_booker.booker import TeeBooker
from tee_booker.config import ConfigError, FallbackConfig, WatchConfig
from tee_booker.reservations import Reservation

SCHEDULE = {
    "monday": [],
    "tuesday": ["6:30 AM", "6:40 AM", "7:00 AM"],
    "wednesday": ["6:30 AM", "6:40 AM"],
    "sunday": ["7:00 AM", "7:10 AM"],
}
TODAY = date(2026, 9, 16)  # a Wednesday


def _res(iso_dt, *, rid=1, players=2, cancelled=False, eligible=True):
    return Reservation(id=rid, confirmation="c", status="Cancelled" if cancelled else "Confirmed",
                       cancelled=cancelled, eligible_cancel=eligible, time_iso=iso_dt,
                       players=0 if cancelled else players, holes=18)


def _plan(reservations, *, owned=None, first_tee=None, skips=(), players=2,
          min_days_ahead=1, min_improvement=10, today=TODAY):
    if owned is None:  # default: the automation booked every reservation given
        owned = {(r.when.date().isoformat(), r.when.hour * 60 + r.when.minute)
                 for r in reservations if not r.cancelled}
    return W.plan(reservations, today=today, weekly_schedule=SCHEDULE, players=players,
                  skip_dates=skips, owned=owned, first_tee=first_tee or {},
                  min_days_ahead=min_days_ahead, min_improvement=min_improvement)


def _watched(result):
    return [(h.play_date.isoformat(), h.held_label) for h in result[0]]


# -- which reservations get watched ----------------------------------------------

def test_watches_the_tuesday_fallback():
    helds, notes = _plan([_res("2026-09-29T09:40:00")], first_tee={"2026-09-29": 420})
    assert [(h.play_date, h.held_label, h.target_label) for h in helds] == [
        (date(2026, 9, 29), "9:40 AM", "6:30 AM")]
    assert notes == []


def test_hand_booked_time_is_left_alone():
    r = _res("2026-09-29T10:00:00")
    helds, notes = _plan([r], owned=set())
    assert helds == []
    assert notes == [("2026-09-29", "10:00 AM was booked by hand — not watched")]


def test_owned_needs_the_exact_time_not_just_the_date():
    # The automation booked 9:40 that date, but you hold 10:00 now.
    helds, _ = _plan([_res("2026-09-29T10:00:00")], owned={("2026-09-29", 9 * 60 + 40)})
    assert helds == []


def test_two_reservations_on_one_date_are_left_alone():
    rs = [_res("2026-09-29T09:40:00", rid=1), _res("2026-09-29T12:00:00", rid=2)]
    helds, notes = _plan(rs)
    assert helds == [] and "holds 2 reservations" in notes[0][1]


def test_cancelled_reservation_is_not_watched_and_not_noted():
    helds, notes = _plan([_res("2026-09-22T09:40:00", cancelled=True)])
    assert helds == [] and notes == []


def test_a_cancelled_one_does_not_count_against_the_confirmed_one():
    rs = [_res("2026-09-29T09:40:00", rid=1, cancelled=True), _res("2026-09-29T09:40:00", rid=2)]
    assert _watched(_plan(rs)) == [("2026-09-29", "9:40 AM")]


def test_skip_list_wins():
    helds, notes = _plan([_res("2026-09-29T09:40:00")], skips=["2026-09-29"])
    assert helds == [] and "skip list" in notes[0][1]


def test_unscheduled_weekday_is_left_alone():
    helds, notes = _plan([_res("2026-09-28T09:40:00")])  # Monday: empty list
    assert helds == [] and "isn't a scheduled day" in notes[0][1]


def test_other_party_size_is_left_alone():
    helds, notes = _plan([_res("2026-09-29T09:40:00", players=4)])
    assert helds == [] and "4 player(s), not 2" in notes[0][1]


def test_uncancellable_reservation_is_left_alone():
    helds, notes = _plan([_res("2026-09-29T09:40:00", eligible=False)])
    assert helds == [] and "can't be cancelled" in notes[0][1]


def test_too_soon_is_skipped_quietly():
    # Tomorrow is inside min_days_ahead=2.
    helds, notes = _plan([_res("2026-09-17T09:40:00")], min_days_ahead=2)
    assert helds == [] and notes == []


def test_held_on_target_is_not_watched():
    helds, notes = _plan([_res("2026-09-27T07:00:00")])  # Sunday target 7:00
    assert helds == [] and notes[0][1] == "7:00 AM is already on the 7:00 AM target"


def test_held_earlier_than_target_is_not_watched():
    # 6:50 on a 7:00-target Sunday: the target opening up would be a LATER time.
    helds, notes = _plan([_res("2026-09-27T06:50:00")])
    assert helds == [] and "earlier than the 7:00 AM target" in notes[0][1]


def test_within_min_improvement_is_not_watched():
    helds, notes = _plan([_res("2026-09-27T07:10:00")], min_improvement=20)
    assert helds == [] and "within 20 min" in notes[0][1]


def test_first_tee_published_means_nothing_earlier_exists():
    helds, notes = _plan([_res("2026-09-30T07:00:00")], first_tee={"2026-09-30": 420})
    assert helds == [] and "first tee time that sheet published" in notes[0][1]


def test_later_than_first_tee_is_watched():
    # Sheet opened at 6:50, we hold 7:00 — a freed 6:50 would be earlier.
    assert _watched(_plan([_res("2026-09-23T07:00:00")], first_tee={"2026-09-23": 410})) == [
        ("2026-09-23", "7:00 AM")]


def test_soonest_first():
    rs = [_res("2026-09-29T09:40:00", rid=1), _res("2026-09-27T07:40:00", rid=2)]
    assert [d for d, _ in _watched(_plan(rs))] == ["2026-09-27", "2026-09-29"]


# -- helpers ----------------------------------------------------------------------

def _held(held="9:40 AM", target="6:30 AM", day=date(2026, 9, 29)):
    return W.HeldBooking(play_date=day, weekday="tuesday", reservation_id=1, players=2,
                         held_minutes=TeeBooker._parse_time_minutes(held),
                         target_minutes=TeeBooker._parse_time_minutes(target))


def test_improvement_is_minutes_closer_to_target():
    h = _held()
    assert W.improvement(h, 7 * 60 + 10) == 150
    assert W.improvement(h, 9 * 60 + 40) == 0
    assert W.improvement(h, 10 * 60) < 0
    assert W.improvement(h, None) == 0


@pytest.mark.parametrize("hhmm,inside", [
    ("23:29", False), ("23:30", True), ("00:15", True), ("01:29", True),
    ("01:30", False), ("12:00", False),
])
def test_quiet_window_wraps_midnight(hhmm, inside):
    h, m = map(int, hhmm.split(":"))
    assert W.in_quiet_window(datetime(2026, 9, 16, h, m), "23:30", "01:30") is inside


def test_quiet_window_same_day():
    assert W.in_quiet_window(datetime(2026, 9, 16, 13, 0), "12:00", "14:00")
    assert not W.in_quiet_window(datetime(2026, 9, 16, 14, 0), "12:00", "14:00")


def test_owned_and_first_tee_come_from_nightly_history():
    recs = [
        {"play_date": "2026-09-29", "booked": True, "booked_time": "9:40 AM", "earliest_time": "7:00 AM"},
        {"play_date": "2026-09-15", "booked": False, "booked_time": "6:50 AM", "earliest_time": "6:50 AM"},
        {"play_date": "2026-09-01", "booked": True, "booked_time": None},
    ]
    assert W.owned_times(recs) == {("2026-09-29", 580)}
    assert W.first_tee_published(recs) == {"2026-09-29": 420, "2026-09-15": 410}


def test_an_opening_is_announced_once():
    assert W.is_new_opening(None, "7:10 AM")
    assert not W.is_new_opening({"better_time": "7:10 AM"}, "7:10 AM")
    assert W.is_new_opening({"better_time": "7:10 AM"}, "7:00 AM")
    assert W.is_new_opening({"better_time": None}, "7:10 AM")
    assert not W.is_new_opening(None, None)


def test_summary_shows_only_changes(tmp_path):
    path = str(tmp_path / "w.jsonl")
    base = dict(play_date="2026-09-29", weekday="tuesday", held_time="9:40 AM", target="6:30 AM")
    W.record("scan", path=path, earliest_open="9:40 AM", better_time=None, improvement=0, **base)
    W.record("scan", path=path, earliest_open="9:40 AM", better_time=None, improvement=0, **base)
    W.record("scan", path=path, earliest_open="7:10 AM", better_time="7:10 AM", improvement=150, **base)
    W.record("run", path=path, summary="checked 1 of 1 date(s); better time open on 1")
    out = W.summarize(path)
    assert "3 check(s)" in out
    assert out.count("earliest open 9:40 AM") == 1          # the repeat is folded
    assert "★ better: 7:10 AM (150 min closer)" in out
    assert "better time open on 1" in out


def test_summary_when_empty(tmp_path):
    assert W.summarize(str(tmp_path / "none.jsonl")) == "No earlier-time checks recorded yet."


def test_lock_is_exclusive_and_stale_locks_clear(tmp_path):
    path = str(tmp_path / "w.lock")
    assert W.acquire_lock(path)
    assert not W.acquire_lock(path)
    assert W.acquire_lock(path, stale_after_s=-1)  # treat as abandoned
    W.release_lock(path)
    W.release_lock(path)  # idempotent


# -- config -----------------------------------------------------------------------

def test_watch_config_defaults():
    c = WatchConfig.from_raw(None)
    assert (c.min_improvement_minutes, c.min_days_ahead, c.quiet_start, c.quiet_end) == (
        10, 1, "23:30", "01:30")


@pytest.mark.parametrize("raw", [
    {"min_improvement_minutes": 0},
    {"min_days_ahead": -1},
    {"quiet_start": "11pm"},
    {"enabled": True},          # unknown key: there is no booking switch
    ["not", "a", "mapping"],
])
def test_watch_config_rejects_bad_values(raw):
    with pytest.raises(ConfigError):
        WatchConfig.from_raw(raw)


# -- the sheet scan (fakes stand in for Playwright) -------------------------------

class _Text:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _Loc:
    def __init__(self, items):
        self._items = list(items)

    def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]

    @property
    def first(self):
        return self._items[0]


class _BookButton:
    """Fails the test if anything tries to press it."""

    def click(self, *a, **k):
        raise AssertionError("the watcher must never press Book")


class _Slot:
    def __init__(self, label, players="1 or 2", bookable=True):
        self.label, self.players, self.bookable = label, players, bookable

    def locator(self, sel):
        if sel == "button.book":
            return _Loc([_BookButton()] if self.bookable else [])
        if sel == ".slot-time":
            return _Loc([_Text(self.label)])
        if sel == ".slot-players":
            return _Loc([_Text(self.players)])
        return _Loc([])


class _Page:
    def __init__(self, sheets, blocked=()):
        self.sheets, self.blocked, self.current = sheets, set(blocked), None

    def locator(self, sel):
        assert sel == ".slot"
        return _Loc(self.sheets.get(self.current, []))


def _scanner(sheets, *, blocked=()):
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(
        booking=SimpleNamespace(preferred_times=[], players=2, fallback=FallbackConfig()),
        selectors={"time_slot": ".slot", "time_slot_label": ".slot-time",
                   "book_button": "button.book", "slot_players_label": ".slot-players"},
    )
    b.log = lambda *a, **k: None
    page = _Page(sheets, blocked)

    def _open(pg, d, **kw):
        pg.current = d
    b._open_tee_sheet = _open
    b._is_blocked = lambda pg: pg.current in pg.blocked
    b._clear_cart = lambda pg: pytest.fail("the watcher must never touch the cart")
    return b, page


def _scan(held, labels, *, min_improvement=10):
    b, page = _scanner({held.play_date: labels})
    run = W.WatchRun()
    b._scan_for_earlier_on(page, [held], run, min_improvement=min_improvement)
    return run.scans[0]


def test_flags_an_earlier_time_when_the_block_gives_one_back():
    sc = _scan(_held(), [_Slot("9:40 AM"), _Slot("7:10 AM"), _Slot("11:10 AM", "2 - 4")])
    assert (sc.better_time, sc.improvement, sc.earliest_open) == ("7:10 AM", 150, "7:10 AM")


def test_nothing_flagged_when_only_our_own_slot_is_open():
    # Today's real Sep 29 sheet: our 9:40 still has room, nothing earlier.
    sc = _scan(_held(), [_Slot("9:40 AM"), _Slot("11:10 AM", "2 - 4")])
    assert (sc.better_time, sc.improvement, sc.earliest_open) == (None, 0, "9:40 AM")


def test_a_one_golfer_slot_is_not_an_opening_for_a_twosome():
    sc = _scan(_held(), [_Slot("7:10 AM", "1"), _Slot("8:00 AM", "2 - 4")])
    assert (sc.better_time, sc.earliest_open) == ("8:00 AM", "8:00 AM")


def test_a_later_time_is_never_flagged_even_if_nearer_the_target():
    # Held 6:50 against a 7:00 target: 7:00 is "closer" but it is LATER.
    sc = _scan(_held(held="6:50 AM", target="7:00 AM"), [_Slot("7:00 AM"), _Slot("7:10 AM")])
    assert sc.better_time is None and sc.improvement == 0


def test_an_unbookable_card_is_not_an_opening():
    sc = _scan(_held(), [_Slot("7:10 AM", bookable=False)])
    assert sc.better_time is None and sc.earliest_open is None


def test_small_gains_below_the_minimum_are_not_flagged():
    h = _held(held="7:00 AM", target="6:30 AM")
    assert _scan(h, [_Slot("6:50 AM")], min_improvement=10).better_time == "6:50 AM"
    assert _scan(h, [_Slot("6:50 AM")], min_improvement=30).better_time is None


def test_picks_the_time_closest_to_target_not_just_any_earlier_one():
    sc = _scan(_held(), [_Slot("8:30 AM"), _Slot("7:00 AM"), _Slot("9:00 AM")])
    assert sc.better_time == "7:00 AM"


def test_rate_limit_ends_the_check_and_says_so():
    d1, d2 = date(2026, 9, 27), date(2026, 9, 29)
    b, page = _scanner({d1: [_Slot("7:00 AM")], d2: [_Slot("7:10 AM")]}, blocked=[d1])
    run = W.WatchRun()
    b._scan_for_earlier_on(page, [_held(day=d1), _held(day=d2)], run, min_improvement=10)
    assert run.blocked and run.scans == []


# -- the runner -------------------------------------------------------------------

@pytest.fixture
def runner(tmp_path, monkeypatch):
    """watch_earlier.main wired to fakes: no browser, no portal, temp state."""
    import watch_earlier as WE
    from tee_booker import release_history, state_store

    monkeypatch.setattr(W, "HISTORY_FILE", str(tmp_path / "earlier_watch.jsonl"))
    monkeypatch.setattr(W, "LOCK_FILE", str(tmp_path / "earlier_watch.lock"))
    monkeypatch.setattr(state_store, "PAUSE_FLAG", str(tmp_path / "paused.flag"))
    monkeypatch.setattr(state_store, "SKIPS_FILE", str(tmp_path / "skips.json"))
    nightly = tmp_path / "release_history.jsonl"
    nightly.write_text(json.dumps({"play_date": "2026-09-29", "booked": True,
                                   "booked_time": "9:40 AM", "earliest_time": "7:00 AM"}) + "\n")
    monkeypatch.setattr(release_history, "HISTORY_FILE", str(nightly))

    cfg = SimpleNamespace(
        earlier_watch=WatchConfig(),
        release=SimpleNamespace(tz=None),
        raw={"weekly_schedule": SCHEDULE},
        booking=SimpleNamespace(players=2),
    )
    monkeypatch.setattr(WE.Config, "load", classmethod(lambda cls, path: cfg))
    monkeypatch.setattr(WE.Credentials, "from_env",
                        classmethod(lambda cls, path: SimpleNamespace(notify_webhook_url="")))

    state = SimpleNamespace(fetches=0, scans=0, notes=[], sheet=[],
                            reservations=[_res("2026-09-29T09:40:00")])

    def fetch(cfg_, creds_, log=print):
        state.fetches += 1
        return state.reservations
    monkeypatch.setattr(WE, "fetch_reservations", fetch)

    def scan(self, helds, *, min_improvement):
        state.scans += 1
        run = W.WatchRun()
        for h in helds:
            better = next((s for s in state.sheet if s < h.held_minutes), None)
            gain = W.improvement(h, better) if better is not None else 0
            run.scans.append(W.WatchScan(
                held=h, earliest_open=TeeBooker._fmt_minutes(min(state.sheet)) if state.sheet else None,
                better_time=TeeBooker._fmt_minutes(better) if gain >= min_improvement else None,
                better_minutes=better if gain >= min_improvement else None,
                improvement=gain if gain >= min_improvement else 0))
        return run
    monkeypatch.setattr(WE.TeeBooker, "scan_for_earlier", scan)
    monkeypatch.setattr(WE, "notify", lambda msg, url, log=print: state.notes.append(msg))
    monkeypatch.setattr(WE, "_stamp", lambda msg: None)
    return WE, state


NOON = datetime(2026, 9, 16, 12, 0)


def test_quiet_window_run_does_not_touch_the_portal(runner):
    WE, state = runner
    assert WE.main([], now=datetime(2026, 9, 16, 23, 58)) == 0
    assert state.fetches == 0 and state.scans == 0


def test_paused_run_does_not_touch_the_portal(runner, tmp_path):
    WE, state = runner
    (tmp_path / "paused.flag").write_text("paused\n")
    assert WE.main([], now=NOON) == 0
    assert state.fetches == 0


def test_plan_reads_reservations_but_opens_no_sheets(runner):
    WE, state = runner
    assert WE.main(["--plan"], now=NOON) == 0
    assert state.fetches == 1 and state.scans == 0
    assert W.load(W.HISTORY_FILE) == []


def test_an_opening_is_announced_once_then_again_when_it_improves(runner):
    WE, state = runner
    state.sheet = [9 * 60 + 40]
    assert WE.main([], now=NOON) == 0
    assert state.notes == []                                   # nothing earlier yet

    state.sheet = [7 * 60 + 30, 9 * 60 + 40]
    WE.main([], now=NOON)
    WE.main([], now=NOON)                                      # same opening, still open
    assert len(state.notes) == 1 and "7:30 AM" in state.notes[0] and "9:40 AM" in state.notes[0]

    state.sheet = [7 * 60, 9 * 60 + 40]
    WE.main([], now=NOON)
    assert len(state.notes) == 2 and "at 7:00 AM" in state.notes[1]

    scans = [r for r in W.load(W.HISTORY_FILE) if r["kind"] == "scan"]
    assert [s["better_time"] for s in scans] == [None, "7:30 AM", "7:30 AM", "7:00 AM"]


def test_zero_reservations_is_recorded_as_possibly_unreadable(runner):
    WE, state = runner
    state.reservations = []
    assert WE.main([], now=NOON) == 0
    run = [r for r in W.load(W.HISTORY_FILE) if r["kind"] == "run"][-1]
    assert "portal read may have failed" in run["summary"] and state.scans == 0


def test_a_partial_check_exits_nonzero(runner, monkeypatch):
    WE, state = runner
    monkeypatch.setattr(WE.TeeBooker, "scan_for_earlier",
                        lambda self, helds, *, min_improvement: W.WatchRun(blocked=True))
    assert WE.main([], now=NOON) == 1
    run = [r for r in W.load(W.HISTORY_FILE) if r["kind"] == "run"][-1]
    assert run["blocked"] is True and run["summary"].startswith("checked 0 of 1")


def test_lock_prevents_overlapping_runs(runner):
    WE, state = runner
    assert W.acquire_lock(W.LOCK_FILE)
    assert WE.main([], now=NOON) == 0
    assert state.fetches == 0
    W.release_lock(W.LOCK_FILE)


def test_latest_run_is_the_scans_since_the_previous_run(tmp_path):
    path = str(tmp_path / "w.jsonl")
    W.record("scan", path=path, play_date="2026-09-27")
    W.record("run", path=path, summary="first")
    W.record("scan", path=path, play_date="2026-09-29")
    W.record("scan", path=path, play_date="2026-09-23")
    W.record("run", path=path, summary="second")
    run, scans = W.latest_run(W.load(path))
    assert run["summary"] == "second"
    assert [s["play_date"] for s in scans] == ["2026-09-29", "2026-09-23"]
    assert W.latest_run([]) == (None, [])
