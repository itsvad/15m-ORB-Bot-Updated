"""Always-on scheduling: sleeps outside the trading window and wakes
automatically each session - this is what lets the bot run as a persistent
service on a small VM rather than something started by hand each morning.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from orb_bot.config import AppConfig
from orb_bot.timeutils import combine_ny, now_ny, session_status

logger = logging.getLogger("orb_bot.scheduler")

WARMUP_BUFFER_MINUTES = 10  # connect/authenticate/warm up before the OR opens
WIND_DOWN_BUFFER_MINUTES = 5  # keep the session alive a bit past hard-close


def session_window_for(config: AppConfig, trading_date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = combine_ny(trading_date, config.session.opening_range_start) - dt.timedelta(
        minutes=WARMUP_BUFFER_MINUTES
    )
    end = combine_ny(trading_date, config.session.hard_close) + dt.timedelta(
        minutes=WIND_DOWN_BUFFER_MINUTES
    )
    return start, end


def next_session_start(config: AppConfig, after: dt.datetime | None = None) -> dt.datetime:
    """The next wall-clock time (America/New_York) the bot should wake up
    and start a trading session, skipping weekends/holidays. On an
    early-close day the session still starts normally - only hard_close is
    effectively earlier in practice since the market itself is closed, and
    the wall-clock hard-close guard in the engine flattens regardless."""
    now = after or now_ny()
    candidate_date = now.date()

    for _ in range(14):  # scan up to two weeks ahead - always terminates well before that
        status = session_status(candidate_date)
        if status != "closed":
            start, _end = session_window_for(config, candidate_date)
            if now <= start:
                return start
            # Already past today's warmup start - is today's session still
            # in progress (worth resuming, e.g. after a restart) or over?
            _start, end = session_window_for(config, candidate_date)
            if now < end:
                return now  # resume immediately, mid-session
        candidate_date = candidate_date + dt.timedelta(days=1)

    raise RuntimeError("Could not find a trading session in the next 14 days - check the market calendar")


async def sleep_until(target: dt.datetime, poll_seconds: float = 30.0) -> None:
    while True:
        remaining = (target - now_ny()).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, poll_seconds))
