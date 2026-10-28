"""Release windows: when the train may open a normal batch (DEP-13).

A train that releases whenever a batch is green deploys all day. With
``[integrate.train] windows`` a normal batch opens only inside a daily window
(local time in ``timezone``, on ``days``), at most ``per_window`` merged per
window instance. Hotfixes -- a hotfix label, ``<state>/priority``, or the
fix-forward issue of a red base -- may go any time, alone.

Time is always passed in (``ts``), so the schedule is tested without a clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RANGE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


class WindowError(ValueError):
    pass


@dataclass(frozen=True)
class Window:
    start: int  # minutes after midnight
    end: int  # exclusive; <= start means the window ends the next day
    label: str

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start


@dataclass(frozen=True)
class Instance:
    """One occurrence of a window: the 05:00-06:00 of one particular day."""

    window: Window
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        return f"{self.start.date().isoformat()} {self.window.label}"


def parse_window(text: str) -> Window:
    m = _RANGE.match(str(text))
    if not m:
        raise WindowError(f"window {text!r} is not HH:MM-HH:MM (24-hour clock), e.g. '05:00-06:00'")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise WindowError(f"window {text!r} is empty (start and end are the same)")
    return Window(start, end, f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}")


def parse_zone(name: str) -> tzinfo | None:
    """An IANA zone, or None for the system's local time."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise WindowError(f"unknown time zone {name!r} (use an IANA name, e.g. 'America/Curacao')") from None


def parse_days(days: list[str]) -> set[int]:
    out = set()
    for d in days:
        key = str(d).strip().lower()[:3]
        if key not in DAYS:
            raise WindowError(f"unknown day {d!r} (use {', '.join(DAYS)})")
        out.add(DAYS.index(key))
    return out


class Schedule:
    def __init__(self, windows: list[str], timezone: str = "", days: list[str] | None = None):
        self.windows = [parse_window(w) for w in windows]
        self.zone = parse_zone(timezone)
        self.zone_name = timezone
        self.days = parse_days(days or []) or set(range(7))

    @property
    def enabled(self) -> bool:
        return bool(self.windows)

    def label(self, ts: float) -> str:
        """The zone as people read it: the configured name, or the local abbreviation."""
        return self.zone_name or self._now(ts).tzname() or "local time"

    def _now(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.zone) if self.zone else datetime.fromtimestamp(ts).astimezone()

    def _at(self, day: date, minutes: int) -> datetime:
        naive = datetime(day.year, day.month, day.day) + timedelta(minutes=minutes)
        # A naive datetime's astimezone() reads it as local time, DST included.
        return naive.replace(tzinfo=self.zone) if self.zone else naive.astimezone()

    def _instance(self, w: Window, day: date) -> Instance:
        end_day = day + timedelta(days=1) if w.crosses_midnight else day
        return Instance(w, self._at(day, w.start), self._at(end_day, w.end))

    def current(self, ts: float) -> Instance | None:
        """The window instance open at ``ts``, if any. ``days`` applies to the day a window starts."""
        if not self.windows:
            return None
        today = self._now(ts).date()
        t = self._now(ts)
        for day in (today, today - timedelta(days=1)):
            if day.weekday() not in self.days:
                continue
            for w in self.windows:
                inst = self._instance(w, day)
                if inst.start <= t < inst.end:
                    return inst
        return None

    def next(self, ts: float) -> Instance | None:
        """The next window instance that starts after ``ts``."""
        t = self._now(ts)
        best: Instance | None = None
        for offset in range(8):
            day = t.date() + timedelta(days=offset)
            if day.weekday() not in self.days:
                continue
            for w in self.windows:
                inst = self._instance(w, day)
                if inst.start > t and (best is None or inst.start < best.start):
                    best = inst
            if best:
                return best
        return best


def duration(seconds: float) -> str:
    """``8h 12m``, ``45m``, ``2d 3h``."""
    m = max(0, int(seconds // 60))
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"
