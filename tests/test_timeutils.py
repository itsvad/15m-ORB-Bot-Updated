from __future__ import annotations

import datetime as dt

import pytest

from orb_bot.timeutils import NY_TZ, combine_ny, session_status, to_ny


def test_combine_ny_is_tz_aware():
    moment = combine_ny(dt.date(2026, 6, 15), dt.time(9, 30))
    assert moment.tzinfo is not None
    assert moment.utcoffset() is not None


def test_to_ny_rejects_naive_datetime():
    naive = dt.datetime(2026, 6, 15, 9, 30)
    with pytest.raises(ValueError):
        to_ny(naive)


def test_to_ny_converts_from_other_zone():
    utc = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)
    converted = to_ny(utc)
    assert converted.tzinfo is not None
    # mid-June is EDT (UTC-4)
    assert converted.hour == 9
    assert converted.minute == 30


def test_session_status_known_holiday_and_early_close():
    assert session_status(dt.date(2026, 12, 25)) == "closed"
    assert session_status(dt.date(2026, 12, 24)) == "early_close"
    assert session_status(dt.date(2026, 6, 15)) == "normal"  # ordinary Monday


def test_session_status_weekend_is_closed():
    assert session_status(dt.date(2026, 6, 13)) == "closed"  # a Saturday
