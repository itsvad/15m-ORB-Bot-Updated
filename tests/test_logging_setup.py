from __future__ import annotations

import csv
import json

from conftest import TRADING_DATE

from orb_bot.logging_setup import DecisionLogger, TradeLogWriter
from orb_bot.strategy.models import PlaceStopOrder, Side


def test_decision_logger_writes_jsonl(tmp_path):
    log = DecisionLogger(str(tmp_path / "decisions.jsonl"))
    action = PlaceStopOrder("entry_long", Side.LONG, 5020.5, 4, "or_breakout_long")
    log.log_action(TRADING_DATE, action)

    lines = (tmp_path / "decisions.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "PlaceStopOrder"
    assert entry["data"]["side"] == "long"
    assert entry["data"]["stop_price"] == 5020.5


def test_trade_log_writer_creates_header_and_appends_rows(tmp_path):
    path = tmp_path / "trade_log.csv"
    writer = TradeLogWriter(str(path))
    writer.append(
        {
            "date": "2026-06-15",
            "direction": "long",
            "or_high": 5020.0,
            "or_low": 5000.0,
            "or_width": 20.0,
            "qty": 4,
            "entry_price": 5020.5,
            "exit_price": 5063.0,
            "r_multiple": 2.02,
            "pnl_usd": 700.0,
            "outcome": "win",
        }
    )

    with path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"
    assert rows[0]["outcome"] == "win"
    assert rows[0]["exit_time"] == ""  # unspecified fields default to empty
