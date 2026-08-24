"""Timezone-safe time helpers.

Every timestamp the strategy reasons about is pinned to America/New_York
explicitly via `zoneinfo` - never an ambient/server timezone, and never a
timezone implied by whatever the market data feed happens to send. This is
called out directly in the strategy spec because it was a real bug class in
the reference Pine Script build.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def now_ny() -> dt.datetime:
    """Current wall-clock time in America/New_York."""
    return dt.datetime.now(tz=NY_TZ)


def to_ny(moment: dt.datetime) -> dt.datetime:
    """Convert an aware datetime to America/New_York. Raises on naive input."""
    if moment.tzinfo is None:
        raise ValueError(
            "to_ny() received a naive datetime; every timestamp in this "
            "system must be explicitly timezone-aware to avoid ambient-tz bugs"
        )
    return moment.astimezone(NY_TZ)


def combine_ny(date: dt.date, time: dt.time) -> dt.datetime:
    """Build an America/New_York-aware datetime from a date and a time."""
    return dt.datetime.combine(date, time, tzinfo=NY_TZ)


def is_weekday(date: dt.date) -> bool:
    return date.weekday() < 5


# Explicit, maintained list of full-day CME/NYSE market holidays and early
# (1:00pm ET) closes affecting the ES/MES day session. This intentionally
# mirrors the FOMC-date approach: maintained data, not scraped or inferred,
# because a wrong guess here means the bot silently waits for bars that will
# never arrive. Update annually from the CME Group holiday calendar.
# Format: "YYYY-MM-DD" -> "closed" | "early_close"
MARKET_CALENDAR_EXCEPTIONS: dict[str, str] = {
    "2026-01-01": "closed",       # New Year's Day
    "2026-01-19": "closed",       # MLK Day
    "2026-02-16": "closed",       # Presidents Day
    "2026-04-03": "closed",       # Good Friday
    "2026-05-25": "closed",       # Memorial Day
    "2026-06-19": "closed",       # Juneteenth
    "2026-07-03": "early_close",  # Day before Independence Day
    "2026-07-04": "closed",       # Independence Day (observed)
    "2026-09-07": "closed",       # Labor Day
    "2026-11-26": "closed",       # Thanksgiving
    "2026-11-27": "early_close",  # Day after Thanksgiving
    "2026-12-24": "early_close",  # Christmas Eve
    "2026-12-25": "closed",       # Christmas
}


def session_status(date: dt.date) -> str:
    """'closed', 'early_close', or 'normal' for a given NY calendar date.

    This is a best-effort, explicitly-maintained guard, NOT a substitute for
    the wall-clock hard-close timer in the scheduler: the scheduler forces a
    flatten at the configured hard-close wall-clock time (or sooner on an
    early-close day) regardless of whether any bar/tick ever arrives at that
    exact moment, so a stale or incomplete calendar can never cause a missed
    flatten - it can only cause a trade to be (correctly) skipped or an
    early flatten to fire a bit early.
    """
    if not is_weekday(date):
        return "closed"
    return MARKET_CALENDAR_EXCEPTIONS.get(date.isoformat(), "normal")
