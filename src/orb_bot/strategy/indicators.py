"""Higher-timeframe candle aggregation and a simple rolling SMA.

The engine is fed 1-minute bars only. Runner-trail SMAs need 5m/15m candles,
so we build them here from completed 1-minute bars - this keeps the engine's
only input contract "a stream of 1-minute bars" which is what's cheaply
available (and unit-testable) from historical data.
"""
from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass

from orb_bot.strategy.models import Bar


def _bucket_start(timestamp: dt.datetime, timeframe_minutes: int) -> dt.datetime:
    """Start of the N-minute bucket a 1-minute bar (identified by its CLOSE
    time) belongs to, anchored to the top of the hour."""
    minute_of_hour = timestamp.hour * 60 + timestamp.minute
    bucket_index = (minute_of_hour - 1) // timeframe_minutes  # -1: close-time -> open-time bucket
    bucket_start_minutes = bucket_index * timeframe_minutes
    day_start = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start + dt.timedelta(minutes=bucket_start_minutes)


class BarAggregator:
    """Rolls up 1-minute bars into completed N-minute candles."""

    def __init__(self, timeframe_minutes: int) -> None:
        self.timeframe_minutes = timeframe_minutes
        self._current_bucket: dt.datetime | None = None
        self._open = self._high = self._low = self._close = 0.0
        self._last_close_time: dt.datetime | None = None

    def add_bar(self, bar: Bar) -> Bar | None:
        """Feed one 1-minute bar. Returns a completed higher-TF Bar exactly
        when this 1-minute bar completes that bucket, else None."""
        bucket = _bucket_start(bar.timestamp, self.timeframe_minutes)

        if self._current_bucket is None:
            self._start_bucket(bucket, bar)
            return None

        if bucket != self._current_bucket:
            completed = self._finalize()
            self._start_bucket(bucket, bar)
            return completed

        self._high = max(self._high, bar.high)
        self._low = min(self._low, bar.low)
        self._close = bar.close
        self._last_close_time = bar.timestamp
        return None

    def _start_bucket(self, bucket: dt.datetime, bar: Bar) -> None:
        self._current_bucket = bucket
        self._open = bar.open
        self._high = bar.high
        self._low = bar.low
        self._close = bar.close
        self._last_close_time = bar.timestamp

    def _finalize(self) -> Bar:
        assert self._last_close_time is not None
        return Bar(
            timestamp=self._last_close_time,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
        )


class RollingSMA:
    """Simple moving average over the close of the last N *completed* bars."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._closes: deque[float] = deque(maxlen=period)

    def update(self, completed_bar: Bar) -> float | None:
        self._closes.append(completed_bar.close)
        if len(self._closes) < self.period:
            return None
        return sum(self._closes) / self.period

    @property
    def value(self) -> float | None:
        if len(self._closes) < self.period:
            return None
        return sum(self._closes) / self.period
