#!/usr/bin/env python3
"""Run the strategy engine + DryRunBroker against a CSV of historical
1-minute bars, with no network access at all - the same event loop shape
the live scheduler uses, just fed historical data instead of a live feed.

Usage:
    python scripts/replay_backtest.py --csv path/to/bars.csv \\
        --config config/config.yaml --date 2026-06-15 --equity 50000

CSV format: columns timestamp,open,high,low,close - timestamp is either
"HH:MM" (assumed to be on --date, America/New_York) or a full ISO8601
string. One file = one trading day.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orb_bot.broker.dry_run_broker import DryRunBroker  # noqa: E402
from orb_bot.config import load_config  # noqa: E402
from orb_bot.strategy.models import Alert, Bar, Notify  # noqa: E402
from orb_bot.strategy.state_machine import OrbEngine  # noqa: E402
from orb_bot.timeutils import NY_TZ  # noqa: E402


def load_bars(csv_path: str, trading_date: dt.date) -> list[Bar]:
    bars = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw_ts = row["timestamp"]
            if len(raw_ts) <= 5:  # "HH:MM"
                hh, mm = raw_ts.split(":")
                ts = dt.datetime(trading_date.year, trading_date.month, trading_date.day, int(hh), int(mm), tzinfo=NY_TZ)
            else:
                ts = dt.datetime.fromisoformat(raw_ts)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=NY_TZ)
            bars.append(
                Bar(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            )
    bars.sort(key=lambda b: b.timestamp)
    return bars


async def run(config_path: str, csv_path: str, trading_date: dt.date, equity: float) -> None:
    config = load_config(config_path)
    engine = OrbEngine(config, trading_date, equity)
    broker = DryRunBroker(starting_equity=equity, point_value=config.instrument.point_value)

    async def on_fill(fill):
        for action in engine.on_fill(fill):
            await _handle(action)

    async def _handle(action) -> None:
        if isinstance(action, Notify):
            print(f"[NOTIFY] {action.event}: {action.payload}")
        elif isinstance(action, Alert):
            print(f"[ALERT:{action.level}] {action.message}")
        else:
            print(f"[ACTION] {action}")
            await broker.execute(action)

    broker.on_fill = on_fill
    await broker.connect()

    bars = load_bars(csv_path, trading_date)
    print(f"Loaded {len(bars)} bars for {trading_date}")

    for b in bars:
        await broker.feed_bar(b)
        for action in engine.on_bar(b):
            await _handle(action)

    # A CSV that ends before hard-close (a data gap, an early close, or just
    # a short sample file) must still guarantee a flatten - this is exactly
    # the wall-clock guarantee the live scheduler provides via on_wall_clock,
    # reproduced here so the replay script matches real behavior.
    from orb_bot.timeutils import combine_ny

    hard_close = combine_ny(trading_date, config.session.hard_close)
    for action in engine.on_wall_clock(hard_close):
        await _handle(action)

    print("\n--- Summary ---")
    print(f"Filter passed: {engine.day.filter_passed}")
    print(f"OR: {engine.day.opening_range.low} - {engine.day.opening_range.high}")
    print(f"Realized P&L: ${engine.day.realized_pnl_usd:.2f}")
    print(f"Starting equity: ${equity:.2f} -> Ending (simulated) equity: ${await broker.get_equity():.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay historical bars through the ORB engine in dry-run")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--equity", type=float, default=50_000.0)
    args = parser.parse_args()

    trading_date = dt.date.fromisoformat(args.date)
    asyncio.run(run(args.config, args.csv, trading_date, args.equity))


if __name__ == "__main__":
    main()
