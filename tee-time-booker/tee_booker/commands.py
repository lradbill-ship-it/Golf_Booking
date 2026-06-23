"""A tiny, local, rule-based command parser for the dashboard.

Turns plain English like "cancel my reservation on June 30th" or "don't book
this Thursday or this weekend" into a structured action + a list of dates.
Entirely offline — no API, no cost to run. Always preview + confirm before
acting, since natural language is fuzzy.

Recognized actions:
  cancel  — cancel existing reservation(s) on the given date(s)
  skip    — tell the auto-booker not to book the given date(s)
  unskip  — undo a skip for the given date(s)
  pause   — global kill switch on (no dates)
  arm     — global kill switch off / resume (no dates)
  unknown — couldn't parse
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "weds": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_WD_ALT = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MON_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass
class ParsedCommand:
    action: str
    dates: list[date] = field(default_factory=list)
    note: str = ""              # warnings / clarifications shown to the user

    @property
    def iso_dates(self) -> list[str]:
        return [d.isoformat() for d in self.dates]


def _soonest_weekday(today: date, wd: int) -> date:
    return today + timedelta(days=(wd - today.weekday()) % 7)


def _week_bounds(today: date, offset_weeks: int = 0) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset_weeks)
    return monday, monday + timedelta(days=6)


def _extract_dates(text: str, today: date) -> list[date]:
    found: set[date] = set()

    # today / tomorrow
    if re.search(r"\btoday\b", text):
        found.add(today)
    if re.search(r"\btomorrow\b", text):
        found.add(today + timedelta(days=1))

    # weekend phrases (handle before generic weekday/"week" scan)
    for m in re.finditer(r"\b(next\s+|this\s+)?weekend\b", text):
        sat = _soonest_weekday(today, 5)
        if (m.group(1) or "").strip() == "next":
            sat += timedelta(days=7)
        found.update({sat, sat + timedelta(days=1)})

    # week phrases
    for m in re.finditer(r"\b(next|this)\s+week\b", text):
        start, end = _week_bounds(today, 1 if m.group(1) == "next" else 0)
        # only future-relevant days (today onward)
        d = max(start, today)
        while d <= end:
            found.add(d)
            d += timedelta(days=1)

    # weekday names, optionally prefixed with "next"
    for m in re.finditer(rf"\b(next\s+|this\s+)?({_WD_ALT})\b", text):
        # skip if this is actually part of "next week"/"this weekend" already handled
        wd = WEEKDAYS[m.group(2)]
        d = _soonest_weekday(today, wd)
        if (m.group(1) or "").strip() == "next":
            d += timedelta(days=7)
        found.add(d)

    # Month-name dates: "June 30", "jun 30th"
    for m in re.finditer(rf"\b({_MON_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text):
        found.add(_year_adjust(today, MONTHS[m.group(1)], int(m.group(2))))

    # Numeric m/d or m/d/yy(yy)
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text):
        mo, day, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12 and 1 <= day <= 31:
            if yr:
                year = int(yr) + (2000 if int(yr) < 100 else 0)
                try:
                    found.add(date(year, mo, day))
                except ValueError:
                    pass
            else:
                found.add(_year_adjust(today, mo, day))

    # ISO yyyy-mm-dd
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        try:
            found.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass

    return sorted(found)


def _year_adjust(today: date, month: int, day: int) -> date:
    """Assume the current year; if that date already passed, roll to next year."""
    try:
        d = date(today.year, month, day)
    except ValueError:
        return date(today.year, month, min(day, 28))
    return d if d >= today else date(today.year + 1, month, day)


def parse_command(text: str, today: date | None = None) -> ParsedCommand:
    today = today or date.today()
    low = " " + text.lower().strip() + " "
    dates = _extract_dates(low, today)

    has_cancel = re.search(r"\bcancel\b", low) is not None
    has_resume = re.search(r"\b(un[\s-]?skip|un[\s-]?pause|resume|re[\s-]?arm|arm|allow|re[\s-]?enable|start booking)\b", low) is not None
    has_stop = re.search(r"\b(do ?n'?t|do not|skip|no|pause|hold off|stop|don'?t)\b", low) is not None
    mentions_booking = re.search(r"\b(book|booking|reservation|reservations|tee\s*time|tee\s*times)\b", low) is not None

    if has_cancel:
        if not dates:
            return ParsedCommand("unknown", note="Cancel what? Add a date, e.g. 'cancel June 30'.")
        return ParsedCommand("cancel", dates)

    if has_resume and not has_stop:
        if dates:
            return ParsedCommand("unskip", dates)
        return ParsedCommand("arm")

    if has_stop or (not mentions_booking and dates and re.search(r"\bno\b", low)):
        if dates:
            return ParsedCommand("skip", dates)
        # No dates -> treat as a global pause only if clearly about everything.
        if re.search(r"\b(pause|stop|kill|everything|all|hold off)\b", low):
            return ParsedCommand("pause")
        return ParsedCommand("unknown", note="Which date(s)? e.g. 'skip this Thursday' or 'pause everything'.")

    return ParsedCommand(
        "unknown",
        note="Didn't understand. Try: 'cancel June 30', 'skip this Thursday', "
             "'don't book this week', 'pause everything', or 'resume'.",
    )
