from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tee_booker.scheduler import seconds_until, wait_until

TZ = ZoneInfo("America/New_York")


def test_seconds_until_future_is_positive():
    target = datetime.now(TZ) + timedelta(seconds=5)
    assert 0 < seconds_until(target, TZ) <= 5


def test_seconds_until_past_is_negative():
    target = datetime.now(TZ) - timedelta(seconds=5)
    assert seconds_until(target, TZ) < 0


def test_wait_until_returns_immediately_for_past_target():
    target = datetime.now(TZ) - timedelta(seconds=1)
    # Should not raise or block.
    wait_until(target, TZ, log=lambda *_: None)


def test_wait_until_fires_warmup_then_returns():
    target = datetime.now(TZ) + timedelta(seconds=1)
    warmups = []
    wait_until(
        target,
        TZ,
        warmup_seconds=5,
        on_warmup=lambda rem: warmups.append(rem),
        log=lambda *_: None,
    )
    assert len(warmups) == 1
    assert datetime.now(TZ) >= target
