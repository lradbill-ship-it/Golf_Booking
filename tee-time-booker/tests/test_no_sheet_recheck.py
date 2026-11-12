"""A day that published NO tee sheet must be looked at again.

2026-11-12: the 09:00 release published nothing at all for 2026-11-26
(Thanksgiving). The run did the right thing — it refused to buy a consolation
tee time on another day — but the 14-days-ahead schedule then never returned to
that date, so a holiday sheet posted late would have gone unnoticed forever.

These tests pin the second look. The REAL `_recheck_on`, `_sheet_date_verdict`,
`_slot_count`, `_find_available_slot` and `_book_slot` all run; only navigation
and the reservation-list network call are faked. Every click is tallied, so
"it did not book" is proved by the click count, not by a status string.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from tee_booker import no_sheet_watch as nsw
from tee_booker.booker import TeeBooker
from tee_booker.no_sheet_watch import Pending, RecheckOutcome

THANKSGIVING = date(2026, 11, 26)


# -- fakes ------------------------------------------------------------------

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


class _Button:
    def __init__(self, tally):
        self.tally = tally

    def click(self, *a, **k):
        self.tally.append("clicked")


class _Slot:
    def __init__(self, label, tally):
        self.label, self.tally = label, tally

    def locator(self, sel):
        if sel == "button.book":
            return _Loc([_Button(self.tally)])
        if sel == ".slot-time":
            return _Loc([_Text(self.label)])
        return _Loc([])


class _Sheet:
    """A tee sheet. `headers` may be a list, popped one reading at a time, so a
    test can make the date header arrive late (the SPA repaint) or never."""

    def __init__(self, header, labels):
        self.headers = list(header) if isinstance(header, list) else [header]
        self.labels = list(labels)
        self.clicks = []
        self.opened = []

    def _header(self):
        return self.headers[0] if len(self.headers) == 1 else self.headers.pop(0)

    def locator(self, sel):
        if sel == ".sheet-date":
            h = self._header()
            return _Loc([_Text(h)] if h is not None else [])
        if sel == ".slot":
            return _Loc([_Slot(l, self.clicks) for l in self.labels])
        return _Loc([])

    def reload(self, **kw):
        pass

    def wait_for_load_state(self, *a, **k):
        pass


def _booker(page, *, already=False, blocked=False, settle=0.05):
    """A booker whose real sweep runs; only navigation and the network are faked.

    `already` is what the portal's reservation list reports: False (no booking),
    True (already booked), or None (could not be read).
    """
    b = TeeBooker.__new__(TeeBooker)
    b.cfg = SimpleNamespace(
        release=SimpleNamespace(retry_window_seconds=0.2, retry_interval_seconds=0.001,
                                sheet_date_settle_seconds=settle),
        booking=SimpleNamespace(players=2, preferred_times=["6:30 AM"], fallback=None),
        selectors={"time_slot": ".slot", "time_slot_label": ".slot-time",
                   "book_button": "button.book", "sheet_date_label": ".sheet-date"},
    )
    b.logged = []
    b.log = lambda m: b.logged.append(m)
    b._screenshot = lambda pg, tag: None
    b._is_blocked = lambda pg: blocked
    b._is_logged_out = lambda pg: False
    b._booked_on_portal = lambda pg, d, label: already
    b._open_tee_sheet = lambda pg, d, **kw: pg.opened.append(d)
    return b


def _pending(d=THANKSGIVING, *, times=("6:30 AM",), weekday="Thursday", players=2):
    return Pending(play_date=d, weekday=weekday, times=list(times), players=players)


def _sweep(page, b, pendings=None, **kw):
    outcomes = []
    b._recheck_on(page, pendings or [_pending()], outcomes, **kw)
    return outcomes


# -- the store --------------------------------------------------------------

def test_remember_keeps_the_first_sighting(tmp_path):
    f = str(tmp_path / "p.json")
    nsw.remember(THANKSGIVING, "Thursday", path=f)
    first = nsw.load(f)["2026-11-26"]["first_seen"]
    nsw.remember(THANKSGIVING, "Thursday", path=f)
    assert nsw.load(f)["2026-11-26"]["first_seen"] == first


def test_forget_removes_and_reports(tmp_path):
    f = str(tmp_path / "p.json")
    nsw.remember(THANKSGIVING, "Thursday", path=f)
    assert nsw.forget(THANKSGIVING, path=f) is True
    assert nsw.forget(THANKSGIVING, path=f) is False
    assert nsw.load(f) == {}


def test_prune_drops_past_and_today_keeps_future():
    entries = {"2026-11-10": {}, "2026-11-12": {}, "2026-11-26": {}, "nonsense": {}}
    kept, dropped = nsw.prune(entries, date(2026, 11, 12))
    assert set(kept) == {"2026-11-26"}
    # Today itself goes: a sheet appearing on the morning of play is no use to a
    # 14-days-ahead booker, and must not silently buy a round starting in hours.
    assert set(dropped) == {"2026-11-10", "2026-11-12", "nonsense"}


def test_due_takes_the_nearest_dates_and_caps_them():
    base = date(2026, 11, 12)
    entries = {str(base + timedelta(days=i)): {"weekday": "X"} for i in range(1, 12)}
    due = nsw.due(entries, base, limit=3)
    assert [p.play_date for p in due] == [base + timedelta(days=i) for i in (1, 2, 3)]


def test_store_is_capped_so_one_bad_week_cannot_balloon_it(tmp_path):
    f = str(tmp_path / "p.json")
    base = date(2027, 1, 1)
    entries = {str(base + timedelta(days=i)): {"weekday": "X"}
               for i in range(nsw.MAX_STORED + 15)}
    nsw.save(entries, f)
    saved = nsw.load(f)
    assert len(saved) == nsw.MAX_STORED
    assert min(saved) == "2027-01-01", "kept the nearest, which are the winnable ones"


def test_unreadable_file_is_an_empty_list_not_a_crash(tmp_path):
    f = tmp_path / "p.json"
    f.write_text("{not json")
    assert nsw.load(str(f)) == {}


@pytest.mark.parametrize("status,resolved", [
    ("booked", True),
    ("already_booked", True),
    ("still_empty", False),
    ("unverified", False),      # "could not read the list" is not "no booking"
    ("unreadable_day", False),  # ...nor is "could not read the date"
    ("unconfirmed", False),
    ("blocked", False),
    ("error", False),
])
def test_only_a_positive_reading_drops_a_date(status, resolved):
    assert RecheckOutcome(THANKSGIVING, status=status).resolved is resolved


# -- the sweep --------------------------------------------------------------

def test_still_empty_keeps_the_date_and_books_nothing():
    page = _Sheet("Nov 26, 2026", [])
    b = _booker(page)
    [out] = _sweep(page, b)
    assert out.status == "still_empty"
    assert out.resolved is False
    assert page.clicks == []


def test_a_sheet_that_appears_gets_booked():
    # The whole point: the holiday sheet posted late.
    page = _Sheet("Nov 26, 2026", ["6:30 AM", "7:00 AM"])
    b = _booker(page, already=False)
    [out] = _sweep(page, b)
    assert out.status == "booked", out.message
    assert out.booked_time == "6:30 AM"
    assert out.resolved is True
    assert page.clicks == ["clicked"], "exactly one purchase"


def test_it_never_books_a_day_you_already_hold():
    page = _Sheet("Nov 26, 2026", ["6:30 AM"])
    b = _booker(page, already=True)
    [out] = _sweep(page, b)
    assert out.status == "already_booked"
    assert page.clicks == [], "would have been a duplicate reservation"


def test_an_unreadable_reservation_list_books_nothing_and_stays_pending():
    # None is not False. We cannot prove he has no booking, so we do not buy one.
    page = _Sheet("Nov 26, 2026", ["6:30 AM"])
    b = _booker(page, already=None)
    [out] = _sweep(page, b)
    assert out.status == "unverified"
    assert out.resolved is False
    assert page.clicks == []


def test_it_never_books_off_another_days_sheet():
    page = _Sheet("Nov 20, 2026", ["6:30 AM"])
    b = _booker(page, already=False)
    [out] = _sweep(page, b)
    assert out.status == "wrong_day"
    assert page.clicks == []


def test_an_unreadable_date_is_reported_as_unreadable_not_as_the_wrong_day():
    # 2026-11-12: an empty sheet has no cards to wait on, so the header can be
    # missing entirely. Reporting that as "the wrong day" blamed the portal for
    # something we simply had not read.
    page = _Sheet(None, ["6:30 AM"])
    b = _booker(page, already=False)
    [out] = _sweep(page, b)
    assert out.status == "unreadable_day"
    assert page.clicks == []


def test_a_header_that_arrives_late_is_waited_for_not_failed():
    # First two reads find no header (SPA still painting), then it appears.
    page = _Sheet([None, None, "Nov 26, 2026"], ["6:30 AM"])
    b = _booker(page, already=False, settle=2.0)
    [out] = _sweep(page, b)
    assert out.status == "booked", out.message
    assert page.clicks == ["clicked"]


def test_a_stale_header_from_the_previous_date_is_not_read_as_a_mismatch():
    # Navigating 20 -> 26 can leave the old date on screen for a repaint or two.
    page = _Sheet(["Nov 20, 2026", "Nov 20, 2026", "Nov 26, 2026"], ["6:30 AM"])
    b = _booker(page, already=False, settle=2.0)
    [out] = _sweep(page, b)
    assert out.status == "booked", out.message


def test_a_weekday_with_no_configured_times_is_not_booked_at_all():
    page = _Sheet("Nov 23, 2026", ["6:30 AM"])
    b = _booker(page, already=False)
    [out] = _sweep(page, b, [_pending(date(2026, 11, 23), times=(), weekday="Monday")])
    assert out.status == "not_scheduled"
    assert page.clicks == []
    assert page.opened == [], "should not even open the sheet"


def test_a_block_ends_the_whole_sweep_instead_of_hammering_the_portal():
    page = _Sheet("Nov 26, 2026", ["6:30 AM"])
    b = _booker(page, blocked=True)
    outs = _sweep(page, b, [_pending(), _pending(date(2026, 11, 27), weekday="Friday")])
    assert [o.status for o in outs] == ["blocked"], "stopped after the first"
    assert page.clicks == []


def test_dry_run_reports_the_booking_it_would_make_and_makes_none():
    page = _Sheet("Nov 26, 2026", ["6:30 AM"])
    b = _booker(page, already=False)
    [out] = _sweep(page, b, dry_run=True)
    assert out.status == "would_book"
    assert out.booked_time == "6:30 AM"
    assert page.clicks == []


def test_a_sheet_we_cannot_count_is_not_treated_as_empty():
    class _Broken(_Sheet):
        def locator(self, sel):
            if sel == ".slot":
                raise RuntimeError("DOM detached")
            return super().locator(sel)

    page = _Broken("Nov 26, 2026", ["6:30 AM"])
    b = _booker(page, already=False)
    [out] = _sweep(page, b)
    assert out.status == "unreadable", "-1 cards is 'could not look', not 'zero'"
    assert out.resolved is False


def test_the_sheet_is_reopened_and_rechecked_after_reading_the_reservation_list():
    # Reading the list navigates away. Clicking without reopening would be the
    # 2026-09-30 wrong-day bug all over again.
    page = _Sheet("Nov 26, 2026", ["6:30 AM"])
    b = _booker(page, already=False)
    _sweep(page, b)
    assert page.opened == [THANKSGIVING, THANKSGIVING], "opened before AND after"


# -- the nightly wiring -----------------------------------------------------

class _FakeBooker:
    """Stands in for TeeBooker in the sweep, recording what it was handed."""

    instances = []

    def __init__(self, cfg, creds, *, log=print):
        self.cfg, self.creds, self.log = cfg, creds, log
        self.seen = None
        self.outcomes = []
        _FakeBooker.instances.append(self)

    def recheck_no_sheet_dates(self, pendings, *, dry_run=False):
        self.seen = list(pendings)
        self.dry_run = dry_run
        return list(self.outcomes)


def _nightly_cfg(schedule):
    return SimpleNamespace(
        release=SimpleNamespace(tz=None),
        booking=SimpleNamespace(players=2),
        raw={"weekly_schedule": schedule},
    )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    import nightly

    _FakeBooker.instances = []
    monkeypatch.setattr(nightly, "TeeBooker", _FakeBooker)
    sent = []
    monkeypatch.setattr(nightly, "notify", lambda m, url, log=None: sent.append(m))
    monkeypatch.setattr(nsw, "PENDING_FILE", str(tmp_path / "p.json"))
    return SimpleNamespace(nightly=nightly, sent=sent)


def test_the_sweep_uses_todays_schedule_not_the_one_in_force_when_it_was_queued(wired):
    # The Sat/Sun window moved to 8:30-9:40 on 2026-11-11. A date queued before
    # that must be re-checked against the NEW window.
    saturday = date(2026, 11, 28)
    nsw.remember(saturday, "Saturday")
    cfg = _nightly_cfg({"saturday": ["8:30 AM", "8:40 AM"], "players": 2})
    wired.nightly.sweep_no_sheet_dates(cfg, SimpleNamespace(notify_webhook_url=None),
                                       today=date(2026, 11, 12))
    [pending] = _FakeBooker.instances[0].seen
    assert pending.times == ["8:30 AM", "8:40 AM"]
    assert pending.weekday == "Saturday"


def test_a_booked_date_leaves_the_list_and_is_announced(wired):
    nsw.remember(THANKSGIVING, "Thursday")
    cfg = _nightly_cfg({"thursday": ["6:30 AM"], "players": 2})
    _FakeBooker.instances = []

    import nightly

    class _Books(_FakeBooker):
        def recheck_no_sheet_dates(self, pendings, *, dry_run=False):
            self.seen = list(pendings)
            return [RecheckOutcome(THANKSGIVING, "Thursday", status="booked",
                                   booked_time="6:30 AM", message="BOOKED 6:30 AM")]

    nightly.TeeBooker = _Books
    nightly.sweep_no_sheet_dates(cfg, SimpleNamespace(notify_webhook_url=None),
                                 today=date(2026, 11, 12))
    assert nsw.load() == {}, "a booked date must stop being re-checked"
    assert any("BOOKED 6:30 AM" in m for m in wired.sent)


def test_an_unsettled_date_stays_on_the_list_with_its_check_counted(wired):
    nsw.remember(THANKSGIVING, "Thursday")
    cfg = _nightly_cfg({"thursday": ["6:30 AM"], "players": 2})

    import nightly

    class _Unsure(_FakeBooker):
        def recheck_no_sheet_dates(self, pendings, *, dry_run=False):
            return [RecheckOutcome(THANKSGIVING, "Thursday", status="unverified")]

    nightly.TeeBooker = _Unsure
    nightly.sweep_no_sheet_dates(cfg, SimpleNamespace(notify_webhook_url=None),
                                 today=date(2026, 11, 12))
    entry = nsw.load()["2026-11-26"]
    assert entry["checks"] == 1
    assert entry["last_checked"]


def test_the_date_this_run_just_attempted_is_not_swept_again(wired):
    nsw.remember(THANKSGIVING, "Thursday")
    cfg = _nightly_cfg({"thursday": ["6:30 AM"], "players": 2})
    out = wired.nightly.sweep_no_sheet_dates(
        cfg, SimpleNamespace(notify_webhook_url=None),
        today=date(2026, 11, 12), exclude=THANKSGIVING)
    assert out == []
    assert _FakeBooker.instances == [], "should not open a browser for nothing"


def test_dates_that_have_passed_are_dropped_from_the_file(wired):
    nsw.remember(date(2026, 11, 1), "Sunday")
    nsw.remember(THANKSGIVING, "Thursday")
    cfg = _nightly_cfg({"thursday": ["6:30 AM"], "players": 2})
    wired.nightly.sweep_no_sheet_dates(cfg, SimpleNamespace(notify_webhook_url=None),
                                       today=date(2026, 11, 12))
    assert set(nsw.load()) == {"2026-11-26"}


def test_a_dry_run_changes_nothing_on_disk(wired):
    nsw.remember(THANKSGIVING, "Thursday")
    before = nsw.load()
    cfg = _nightly_cfg({"thursday": ["6:30 AM"], "players": 2})

    import nightly

    class _Books(_FakeBooker):
        def recheck_no_sheet_dates(self, pendings, *, dry_run=False):
            return [RecheckOutcome(THANKSGIVING, "Thursday", status="booked")]

    nightly.TeeBooker = _Books
    nightly.sweep_no_sheet_dates(cfg, SimpleNamespace(notify_webhook_url=None),
                                 today=date(2026, 11, 12), dry_run=True)
    assert nsw.load() == before


# -- main() actually reaching all of the above ------------------------------
#
# The checks above exercise `sweep_no_sheet_dates` directly. That proves the
# sweep works; it proves nothing about anyone CALLING it. A capability with no
# caller is a description, so these drive the real `main()`.


def _main_env(monkeypatch, tmp_path, result, *, schedule=None):
    """Run nightly.main() against fakes and report what it did."""
    import nightly
    from tee_booker import config as cfgmod

    calls = SimpleNamespace(swept=[], excluded=object(), booked_for=None)

    cfg = SimpleNamespace(
        release=SimpleNamespace(
            tz=None, days_ahead=14, release_time="09:00",
            retry_window_seconds=3540, prelogin_seconds=60, warmup_seconds=0,
            release_moment_for=lambda d: datetime(2026, 11, 12, 9, 0),
        ),
        booking=SimpleNamespace(players=2, preferred_times=[], fallback=None),
        raw={"weekly_schedule": schedule or {"thursday": ["6:30 AM"], "players": 2}},
    )

    class _Fake:
        def __init__(self, c, creds, *, log=print):
            pass

        def fallback_window(self):
            return None

        def fallback_target(self):
            return None

        def run(self, play_date, **kw):
            calls.booked_for = play_date
            return result

    monkeypatch.setattr(cfgmod.Config, "load", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(nightly.Config, "load", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(nightly.Credentials, "from_env",
                        staticmethod(lambda *a, **k: SimpleNamespace(
                            username="u", password="p", notify_webhook_url=None)))
    monkeypatch.setattr(nightly, "TeeBooker", _Fake)
    monkeypatch.setattr(nightly, "notify", lambda *a, **k: None)
    monkeypatch.setattr(nightly.state_store, "is_paused", lambda: False)
    monkeypatch.setattr(nightly.state_store, "is_skipped", lambda d: False)
    monkeypatch.setattr(nsw, "PENDING_FILE", str(tmp_path / "p.json"))

    def _sweep(c, creds, *, dry_run=False, today=None, exclude=None):
        calls.swept.append(exclude)
        return []
    monkeypatch.setattr(nightly, "sweep_no_sheet_dates", _sweep)
    return nightly, calls


def test_main_queues_a_no_sheet_day_and_then_sweeps(tmp_path, monkeypatch):
    from tee_booker.booker import BookingResult

    result = BookingResult(False, message="no sheet at all", no_sheet=True)
    nightly, calls = _main_env(monkeypatch, tmp_path, result)
    nightly.main(["--no-wait", "--date", "2026-11-26"])

    assert calls.booked_for == THANKSGIVING, "the night's own run still happened"
    assert set(nsw.load()) == {"2026-11-26"}, "main() never queued the date"
    assert calls.swept == [THANKSGIVING], \
        "main() never swept, or swept without excluding tonight's own date"


def test_main_does_not_queue_a_day_whose_times_were_merely_taken(tmp_path, monkeypatch):
    from tee_booker.booker import BookingResult

    # "Tee times WERE published, they were taken" is a different outcome and
    # must not come back every morning for two weeks.
    result = BookingResult(False, message="nothing bookable; they were taken")
    nightly, calls = _main_env(monkeypatch, tmp_path, result)
    nightly.main(["--no-wait", "--date", "2026-11-26"])

    assert nsw.load() == {}
    assert calls.swept == [THANKSGIVING], "the sweep still runs on a normal night"


def test_main_sweeps_even_after_a_successful_booking(tmp_path, monkeypatch):
    from tee_booker.booker import BookingResult

    result = BookingResult(True, booked_time="6:30 AM", message="Booked 6:30 AM")
    nightly, calls = _main_env(monkeypatch, tmp_path, result)
    assert nightly.main(["--no-wait", "--date", "2026-11-26"]) == 0
    assert calls.swept == [THANKSGIVING], "a good night still owes the list a look"


def test_main_sweeps_on_a_night_with_nothing_scheduled(tmp_path, monkeypatch):
    from tee_booker.booker import BookingResult

    # Monday: no times configured. The night's own run is skipped, but a
    # pending date still deserves its daily look.
    result = BookingResult(False, message="unused")
    nightly, calls = _main_env(monkeypatch, tmp_path, result,
                               schedule={"monday": [], "players": 2})
    nightly.main(["--no-wait", "--date", "2026-11-23"])

    assert calls.booked_for is None, "nothing should have been booked on a Monday"
    assert calls.swept == [None], "the sweep must still run"


def test_main_dry_run_queues_nothing_and_sweeps_nothing(tmp_path, monkeypatch):
    from tee_booker.booker import BookingResult

    result = BookingResult(False, message="no sheet at all", no_sheet=True)
    nightly, calls = _main_env(monkeypatch, tmp_path, result)
    nightly.main(["--no-wait", "--dry-run", "--date", "2026-11-26"])

    assert nsw.load() == {}
    assert calls.swept == []
