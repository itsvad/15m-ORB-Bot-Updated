"""End-to-end dry-run wiring test: OrbEngine + DryRunBroker driven purely by
historical-style 1-minute bars, with no network access at all. This is the
same event loop shape the live scheduler uses (bar in -> engine actions out
-> broker.execute / feed_bar), just fed synthetic historical data instead of
a live Tradovate feed - i.e. exactly the "unit-testable against historical
data independent of the live connection" requirement, exercised against the
dry-run broker specifically (never a real order placed).
"""
from __future__ import annotations

import asyncio

from conftest import TRADING_DATE, bar, make_config

from orb_bot.broker.dry_run_broker import DryRunBroker
from orb_bot.strategy.models import Alert, Notify
from orb_bot.strategy.state_machine import OrbEngine

EQUITY = 50_000.0


async def _run_day(engine: OrbEngine, broker: DryRunBroker, bars) -> list:
    events: list = []

    async def on_fill(fill):
        for action in engine.on_fill(fill):
            events.append(action)
            if isinstance(action, (Alert, Notify)):
                continue
            await broker.execute(action)

    broker.on_fill = on_fill
    await broker.connect()

    for b in bars:
        # Market data arrives first: any resting simulated stop order that
        # this bar's range would have triggered fills (and feeds back
        # through on_fill -> engine.on_fill -> broker.execute) before the
        # engine's own bar-close-driven checks (TP1/breakeven/cap/etc.) run
        # against the same bar.
        await broker.feed_bar(b)

        actions = engine.on_bar(b)
        for action in actions:
            events.append(action)
            if isinstance(action, (Alert, Notify)):
                continue
            await broker.execute(action)

    return events


def _synthetic_day_bars():
    bars = []
    # OR: 9:30-9:44, high=5020, low=5000 (width 20, passes filter)
    for i in range(15):
        mm = 30 + i
        px = 5020.0 if i % 2 == 0 else 5000.0
        bars.append(bar(9, mm, px, 5020.0, 5000.0, px))
    bars.append(bar(9, 45, 5020.0, 5020.0, 5000.0, 5020.0))  # finalizes OR, places entries

    # Breakout up through 5020.5, then a strong rally to blow through TP1
    # and the old-mode 2R cap so the whole trade lifecycle executes.
    bars.append(bar(9, 46, 5021.0, 5022.0, 5020.5, 5022.0))  # fills entry_long @5020.5
    bars.append(bar(9, 50, 5042.0, 5043.0, 5041.5, 5042.0))  # +1R -> breakeven
    bars.append(bar(9, 55, 5053.0, 5054.0, 5052.0, 5053.0))  # +1.5R -> TP1
    bars.append(bar(10, 0, 5063.0, 5064.0, 5062.5, 5063.0))  # +2R cap -> runner exit

    return bars


def test_dry_run_full_day_never_calls_a_real_order_placement_path():
    config = make_config()
    engine = OrbEngine(config, TRADING_DATE, EQUITY)
    broker = DryRunBroker(starting_equity=EQUITY, point_value=config.instrument.point_value)

    events = asyncio.run(_run_day(engine, broker, _synthetic_day_bars()))

    notify_events = [e.event for e in events if isinstance(e, Notify)]
    assert "entry_orders_placed" in notify_events
    assert "entry" in notify_events
    assert "tp1" in notify_events
    assert "exit" in notify_events

    # The day fully resolved: no working simulated orders left dangling and
    # the position is closed out.
    assert broker._working == {}
    assert engine.day.position is None
    assert engine.day.trade_done_for_day is True

    # Equity moved with the simulated P&L (this trade should be net
    # profitable given the rally scripted above).
    assert broker._equity != EQUITY
    assert broker._equity > EQUITY
