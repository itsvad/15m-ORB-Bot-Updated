"""Pure data types shared by the strategy engine, broker adapters and tests.

Nothing in this module touches I/O. `Bar` timestamps are expected to already
be America/New_York-aware (see orb_bot.timeutils) - the engine never
re-derives timezone from ambient state.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class RunnerMode(str, Enum):
    OLD = "old"
    NEW = "new"


class RunnerPhase(str, Enum):
    """Only meaningful for RunnerMode.NEW; old mode has no phases."""

    NONE = "none"
    A = "phase_a"
    B = "phase_b"


@dataclass(frozen=True)
class Bar:
    """A completed OHLC bar at the base timeframe (1 minute)."""

    timestamp: dt.datetime  # bar CLOSE time, NY-aware
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Fill:
    order_ref: str
    price: float
    qty: int
    timestamp: dt.datetime


@dataclass(frozen=True)
class OrderStatusUpdate:
    order_ref: str
    status: str  # "Working" | "Filled" | "Cancelled" | "Rejected"
    timestamp: dt.datetime


@dataclass(frozen=True)
class EquityUpdate:
    equity: float
    timestamp: dt.datetime


# ---- Actions the engine asks the broker layer to perform -----------------


@dataclass(frozen=True)
class PlaceStopOrder:
    order_ref: str
    side: Side
    stop_price: float
    qty: int
    reason: str = ""


@dataclass(frozen=True)
class CancelOrder:
    order_ref: str
    reason: str = ""


@dataclass(frozen=True)
class ModifyOrder:
    order_ref: str
    new_stop_price: float | None = None
    new_qty: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class ClosePositionMarket:
    qty: int
    reason: str = ""


@dataclass(frozen=True)
class Alert:
    level: str  # "warning" | "critical"
    message: str


@dataclass(frozen=True)
class Notify:
    event: str  # "entry" | "tp1" | "breakeven" | "exit" | "no_trade" | "halt"
    payload: dict = field(default_factory=dict)


Action = (
    PlaceStopOrder
    | CancelOrder
    | ModifyOrder
    | ClosePositionMarket
    | Alert
    | Notify
)


@dataclass
class OpeningRange:
    high: float | None = None
    low: float | None = None

    def update(self, bar: Bar) -> None:
        self.high = bar.high if self.high is None else max(self.high, bar.high)
        self.low = bar.low if self.low is None else min(self.low, bar.low)

    @property
    def width(self) -> float | None:
        if self.high is None or self.low is None:
            return None
        return self.high - self.low


@dataclass
class Position:
    side: Side
    entry_price: float
    entry_time: dt.datetime
    qty: int
    initial_stop: float
    r_unit_points: float  # |entry_price - initial_stop|, i.e. 1R in points

    current_stop: float
    stop_order_ref: str = "stop_loss"

    breakeven_moved: bool = False
    whipsaw_guard_applied: bool = False

    tp1_done: bool = False
    runner_qty: int = 0

    runner_mode: RunnerMode = RunnerMode.OLD
    runner_phase: RunnerPhase = RunnerPhase.NONE

    def r_multiple(self, price: float) -> float:
        signed = (price - self.entry_price) if self.side is Side.LONG else (
            self.entry_price - price
        )
        return signed / self.r_unit_points


@dataclass
class DayState:
    date: dt.date
    opening_range: OpeningRange = field(default_factory=OpeningRange)
    or_finalized: bool = False
    filter_passed: bool | None = None
    entry_qty: int = 0
    stop_distance_points: float | None = None

    entry_orders_placed: bool = False
    long_entry_ref: str = "entry_long"
    short_entry_ref: str = "entry_short"

    position: Position | None = None
    trade_done_for_day: bool = False
    halted: bool = False  # daily loss limit tripped
    realized_pnl_usd: float = 0.0
