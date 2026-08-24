"""Where 1-minute bars come from, decoupled from how the session loop
consumes them - the same orchestration code drives a live Tradovate feed,
a dry-run session watching that same live feed, and a historical replay
used for tests/manual verification, by swapping the BarSource.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from abc import ABC, abstractmethod

from orb_bot.strategy.models import Bar


class BarSource(ABC):
    @abstractmethod
    async def next_bar(self) -> Bar | None:
        """Block until the next 1-minute bar is available, or return None
        when the source is exhausted (replay only - a live source never
        returns None, it just keeps waiting)."""


class ReplayBarSource(BarSource):
    """Feeds a fixed, pre-loaded list of bars - used for backtests and for
    demonstrating the full dry-run pipeline without a live connection."""

    def __init__(self, bars: list[Bar], pace_seconds: float = 0.0) -> None:
        self._bars = iter(bars)
        self._pace_seconds = pace_seconds

    async def next_bar(self) -> Bar | None:
        if self._pace_seconds:
            await asyncio.sleep(self._pace_seconds)
        return next(self._bars, None)


class PollingTradovateBarSource(BarSource):
    """v1 live bar source: polls Tradovate's chart data on a fixed interval
    and yields newly-completed 1-minute bars only (dedup by timestamp).

    NOTE: a true WebSocket `md/subscribeChart` push feed would be lower-
    latency and is a good follow-up; polling is a deliberate first-session
    simplification and should be validated against the demo feed for
    correctness/latency before being trusted for live entries.
    """

    def __init__(self, get_recent_bars_fn, poll_interval_seconds: float = 5.0) -> None:
        self._get_recent_bars = get_recent_bars_fn
        self._poll_interval = poll_interval_seconds
        self._last_seen: dt.datetime | None = None
        self._queue: list[Bar] = []

    async def next_bar(self) -> Bar | None:
        while not self._queue:
            bars = await self._get_recent_bars(5)
            new_bars = (
                sorted(
                    (b for b in bars if self._last_seen is None or b.timestamp > self._last_seen),
                    key=lambda b: b.timestamp,
                )
                if bars
                else []
            )
            if new_bars:
                self._last_seen = new_bars[-1].timestamp
                self._queue = new_bars
                break
            await asyncio.sleep(self._poll_interval)
        return self._queue.pop(0)
