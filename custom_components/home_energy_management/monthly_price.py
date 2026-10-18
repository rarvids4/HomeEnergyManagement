"""Rolling month-to-date average spot price.

Monthly-average contracts bill every kWh at the average spot price of
the calendar month.  The month is not over when we plan, so the
optimizer uses the running average from day 1 up to today.

Nordpool only exposes today + tomorrow, so the per-day averages are kept
here (persisted by the coordinator in HA storage) and back-filled from
the recorder's price-state history after a fresh install.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

# A back-filled day must be covered by price history for at least this
# share of its 24 h, otherwise a partial day would skew the average.
_MIN_DAY_COVERAGE = 0.99


class MonthlyPriceTracker:
    """Per-day spot averages for the current month.

    ``days`` maps ISO dates to ``{"avg": price, "n": hours}`` so the
    month average is weighted by hour, i.e. equal to the plain mean of
    every hourly price in the month.
    """

    def __init__(self, days: dict[str, dict[str, Any]] | None = None) -> None:
        self.days: dict[str, dict[str, Any]] = dict(days or {})

    def has_day(self, day: date) -> bool:
        return day.isoformat() in self.days

    def record_day(self, day: date, prices: list[float], hours: int | None = None) -> bool:
        """Store the average of *day*'s hourly *prices*.  True when changed.

        *hours* overrides the weight (used for back-filled days, where
        *prices* is a single time-weighted average).
        """
        if not prices:
            return False
        entry = {
            "avg": round(sum(prices) / len(prices), 5),
            "n": hours if hours is not None else len(prices),
        }
        key = day.isoformat()
        if self.days.get(key) == entry:
            return False
        self.days[key] = entry
        return True

    def _month_entries(self, today: date) -> list[dict[str, Any]]:
        first = today.replace(day=1).isoformat()
        last = today.isoformat()
        return [e for k, e in self.days.items() if first <= k <= last]

    def month_average(self, today: date) -> float | None:
        """Hour-weighted mean of the recorded days from the 1st to *today*."""
        entries = self._month_entries(today)
        hours = sum(e["n"] for e in entries)
        if not hours:
            return None
        return sum(e["avg"] * e["n"] for e in entries) / hours

    def days_recorded(self, today: date) -> int:
        return len(self._month_entries(today))

    def month_days(self, today: date) -> dict[str, float]:
        """Recorded daily averages from the 1st to *today*, by ISO date."""
        first = today.replace(day=1).isoformat()
        last = today.isoformat()
        return {k: self.days[k]["avg"] for k in sorted(self.days) if first <= k <= last}

    def prune(self, today: date) -> bool:
        """Drop days before the current month.  True when anything was removed."""
        first = today.replace(day=1).isoformat()
        old = [k for k in self.days if k < first]
        for k in old:
            del self.days[k]
        return bool(old)

    def missing_days(self, today: date) -> list[date]:
        """Days from the 1st up to (not including) *today* with no record."""
        day = today.replace(day=1)
        missing = []
        while day < today:
            if not self.has_day(day):
                missing.append(day)
            day += timedelta(days=1)
        return missing


def daily_averages_from_history(
    points: list[tuple[datetime, float]],
    end: datetime,
) -> dict[date, float]:
    """Time-weighted average price per day from price-state changes.

    *points* are ``(local naive timestamp, price)`` pairs sorted by time;
    each price holds until the next change, the last one until *end*.
    Only days covered for at least 99 % of their 24 h are returned.
    """
    weighted: dict[date, float] = {}
    covered: dict[date, float] = {}

    for i, (start, price) in enumerate(points):
        stop = points[i + 1][0] if i + 1 < len(points) else end
        stop = min(stop, end)
        t = start
        while t < stop:
            midnight = datetime.combine(t.date() + timedelta(days=1), datetime.min.time())
            seg_end = min(stop, midnight)
            secs = (seg_end - t).total_seconds()
            weighted[t.date()] = weighted.get(t.date(), 0.0) + price * secs
            covered[t.date()] = covered.get(t.date(), 0.0) + secs
            t = seg_end

    return {
        day: weighted[day] / secs
        for day, secs in covered.items()
        if secs >= 86400 * _MIN_DAY_COVERAGE
    }


def daily_averages_from_statistics(
    rows: list[tuple[datetime, float]],
) -> dict[date, tuple[float, int]]:
    """Average price and hour count per day from hourly long-term statistics.

    *rows* are ``(local naive hour start, price)`` pairs.  The recorder
    keeps these indefinitely, so they cover days whose states have been
    purged.  For a price sensor without a mean the value is the price at
    the end of the hour — exact for hourly prices, the last quarter for
    15-min prices.  Only days with at least 23 hours (a DST-short day)
    are returned.
    """
    by_day: dict[date, list[float]] = {}
    for start, price in rows:
        by_day.setdefault(start.date(), []).append(price)
    return {
        day: (sum(vals) / len(vals), len(vals))
        for day, vals in by_day.items()
        if len(vals) >= 23
    }
