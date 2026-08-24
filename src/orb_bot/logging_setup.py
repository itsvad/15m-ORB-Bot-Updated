"""Structured decision logging + the trade log.

Two durable outputs, independent of the Python `logging` console output:

- decisions.jsonl: one JSON object per line for every Action the engine
  emits (place/cancel/modify order, market close, alert, notify) plus bar-
  level context, so a human can replay exactly what the bot decided and why
  during dry-run review.
- trade_log.csv: one row per completed round-trip trade, with the fields
  called out in the spec (date, direction, OR width, outcome, R-multiple,
  entry/exit price/time) so live results can be compared directly against
  backtested expectancy. Column names here are a reasonable default -
  rename/reorder to match the actual trade-log spreadsheet's headers if
  they differ; this wasn't available to check against in this session.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import logging.handlers
from pathlib import Path

from orb_bot.strategy.models import Action, Alert, Notify

TRADE_LOG_FIELDS = [
    "date",
    "direction",
    "or_high",
    "or_low",
    "or_width",
    "qty",
    "entry_price",
    "entry_time",
    "exit_price",
    "exit_time",
    "exit_reason",
    "r_multiple",
    "pnl_usd",
    "outcome",
]


def configure_logging(level: str, log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return  # already configured (e.g. re-entrant call)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "orb_bot.log", maxBytes=10_000_000, backupCount=5
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(file_handler)


class DecisionLogger:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log_action(self, trading_date: dt.date, action: Action) -> None:
        entry = {
            "ts": dt.datetime.utcnow().isoformat(),
            "trading_date": trading_date.isoformat(),
            "type": type(action).__name__,
            "data": _action_to_dict(action),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def log_bar_context(self, trading_date: dt.date, note: str, **fields) -> None:
        entry = {
            "ts": dt.datetime.utcnow().isoformat(),
            "trading_date": trading_date.isoformat(),
            "type": "context",
            "note": note,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def _action_to_dict(action: Action) -> dict:
    d = dict(vars(action))
    for k, v in list(d.items()):
        if hasattr(v, "value"):  # enums (Side, etc.)
            d[k] = v.value
    return d


class TradeLogWriter:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS).writeheader()

    def append(self, row: dict) -> None:
        full_row = {k: row.get(k, "") for k in TRADE_LOG_FIELDS}
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS).writerow(full_row)
