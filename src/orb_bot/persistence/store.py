"""Durable local state store (SQLite).

The bot must survive a restart mid-session without losing track of the
day's state: has an entry already fired, is a position open, what phase is
the runner in, has the daily loss limit already been tripped, is trading
currently paused via Telegram, etc. All of that lives here, not in
in-memory-only state.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from orb_bot.strategy.models import (
    DayState,
    OpeningRange,
    Position,
    RunnerMode,
    RunnerPhase,
    Side,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_state (
    trading_date TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date TEXT NOT NULL,
    side TEXT NOT NULL,
    or_high REAL,
    or_low REAL,
    or_width REAL,
    entry_price REAL,
    entry_time TEXT,
    exit_price REAL,
    exit_time TEXT,
    exit_reason TEXT,
    qty INTEGER,
    r_multiple REAL,
    pnl_usd REAL,
    created_at TEXT NOT NULL
);
"""


def _position_to_dict(pos: Position | None) -> dict | None:
    if pos is None:
        return None
    return {
        "side": pos.side.value,
        "entry_price": pos.entry_price,
        "entry_time": pos.entry_time.isoformat(),
        "qty": pos.qty,
        "initial_stop": pos.initial_stop,
        "r_unit_points": pos.r_unit_points,
        "current_stop": pos.current_stop,
        "stop_order_ref": pos.stop_order_ref,
        "breakeven_moved": pos.breakeven_moved,
        "whipsaw_guard_applied": pos.whipsaw_guard_applied,
        "tp1_done": pos.tp1_done,
        "runner_qty": pos.runner_qty,
        "runner_mode": pos.runner_mode.value,
        "runner_phase": pos.runner_phase.value,
    }


def _position_from_dict(d: dict | None) -> Position | None:
    if d is None:
        return None
    return Position(
        side=Side(d["side"]),
        entry_price=d["entry_price"],
        entry_time=dt.datetime.fromisoformat(d["entry_time"]),
        qty=d["qty"],
        initial_stop=d["initial_stop"],
        r_unit_points=d["r_unit_points"],
        current_stop=d["current_stop"],
        stop_order_ref=d["stop_order_ref"],
        breakeven_moved=d["breakeven_moved"],
        whipsaw_guard_applied=d["whipsaw_guard_applied"],
        tp1_done=d["tp1_done"],
        runner_qty=d["runner_qty"],
        runner_mode=RunnerMode(d["runner_mode"]),
        runner_phase=RunnerPhase(d["runner_phase"]),
    )


def day_state_to_json(day: DayState) -> str:
    payload = {
        "date": day.date.isoformat(),
        "opening_range": {"high": day.opening_range.high, "low": day.opening_range.low},
        "or_finalized": day.or_finalized,
        "filter_passed": day.filter_passed,
        "entry_qty": day.entry_qty,
        "stop_distance_points": day.stop_distance_points,
        "entry_orders_placed": day.entry_orders_placed,
        "long_entry_ref": day.long_entry_ref,
        "short_entry_ref": day.short_entry_ref,
        "position": _position_to_dict(day.position),
        "trade_done_for_day": day.trade_done_for_day,
        "halted": day.halted,
        "realized_pnl_usd": day.realized_pnl_usd,
    }
    return json.dumps(payload)


def day_state_from_json(raw: str) -> DayState:
    d = json.loads(raw)
    orr = OpeningRange(high=d["opening_range"]["high"], low=d["opening_range"]["low"])
    return DayState(
        date=dt.date.fromisoformat(d["date"]),
        opening_range=orr,
        or_finalized=d["or_finalized"],
        filter_passed=d["filter_passed"],
        entry_qty=d["entry_qty"],
        stop_distance_points=d["stop_distance_points"],
        entry_orders_placed=d["entry_orders_placed"],
        long_entry_ref=d["long_entry_ref"],
        short_entry_ref=d["short_entry_ref"],
        position=_position_from_dict(d["position"]),
        trade_done_for_day=d["trade_done_for_day"],
        halted=d["halted"],
        realized_pnl_usd=d["realized_pnl_usd"],
    )


class StateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- day state --------------------------------------------------------

    def save_day_state(self, day: DayState) -> None:
        self._conn.execute(
            "INSERT INTO day_state (trading_date, state_json, updated_at) VALUES (?, ?, ?)\n"
            "ON CONFLICT(trading_date) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (day.date.isoformat(), day_state_to_json(day), dt.datetime.utcnow().isoformat()),
        )
        self._conn.commit()

    def load_day_state(self, trading_date: dt.date) -> DayState | None:
        row = self._conn.execute(
            "SELECT state_json FROM day_state WHERE trading_date = ?", (trading_date.isoformat(),)
        ).fetchone()
        if row is None:
            return None
        return day_state_from_json(row[0])

    # -- control (pause/resume, etc.) -------------------------------------

    def set_control(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO control (key, value) VALUES (?, ?)\n"
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_control(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def is_paused(self) -> bool:
        return self.get_control("paused", "false") == "true"

    def set_paused(self, paused: bool) -> None:
        self.set_control("paused", "true" if paused else "false")

    # -- trade log (queryable, mirrors the CSV trade log) ------------------

    def record_trade(self, row: dict) -> None:
        self._conn.execute(
            """INSERT INTO trades
               (trading_date, side, or_high, or_low, or_width, entry_price, entry_time,
                exit_price, exit_time, exit_reason, qty, r_multiple, pnl_usd, created_at)
               VALUES (:trading_date, :side, :or_high, :or_low, :or_width, :entry_price,
                       :entry_time, :exit_price, :exit_time, :exit_reason, :qty,
                       :r_multiple, :pnl_usd, :created_at)""",
            {**row, "created_at": dt.datetime.utcnow().isoformat()},
        )
        self._conn.commit()
