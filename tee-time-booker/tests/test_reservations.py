"""Parsing the Kenna reservation-history JSON.

The portal's payload is the booker's only window into existing bookings, and the
status codes are unintuitive (1 = Confirmed, 0 = Cancelled). _parse is pure, so
lock its behaviour down here.
"""

from tee_booker.reservations import _parse


def _payload(*reservations):
    return {"reservations": {"Reservations": list(reservations)}}


def test_confirmed_vs_cancelled_status_codes():
    data = _payload(
        {"ReservationID": 1, "Status": 1, "Invoice": {"Time": "2026-07-11T07:00:00"}},
        {"ReservationID": 2, "Status": 0, "Invoice": {"Time": "2026-07-12T07:00:00"}},
    )
    res = _parse(data)
    assert (res[0].status, res[0].cancelled) == ("Confirmed", False)
    assert (res[1].status, res[1].cancelled) == ("Cancelled", True)


def test_sorted_by_time_soonest_first():
    data = _payload(
        {"ReservationID": 1, "Status": 1, "Invoice": {"Time": "2026-07-20T07:00:00"}},
        {"ReservationID": 2, "Status": 1, "Invoice": {"Time": "2026-07-05T07:00:00"}},
    )
    res = _parse(data)
    assert [r.id for r in res] == [2, 1]


def test_fields_are_extracted():
    data = _payload({
        "ReservationID": 42,
        "ConfirmationNumber": "ABC123",
        "Status": 1,
        "EligibleForCancellation": True,
        "Invoice": {"Time": "2026-07-11T07:00:00", "PlayerCount": 2, "HoleCount": 18},
    })
    r = _parse(data)[0]
    assert r.confirmation == "ABC123"
    assert r.eligible_cancel is True
    assert (r.players, r.holes) == (2, 18)
    assert r.date_label == "Sat, Jul 11 2026"
    assert r.time_label == "7:00 AM"


def test_missing_invoice_and_fields_are_tolerated():
    r = _parse(_payload({"ReservationID": 7, "Status": 1}))[0]
    assert r.players is None and r.holes is None
    assert r.when is None
    assert r.date_label == "—" and r.time_label == "—"
    assert r.eligible_cancel is False  # absent -> not eligible


def test_empty_and_malformed_payloads():
    assert _parse({}) == []
    assert _parse(None) == []
    assert _parse({"reservations": {}}) == []
