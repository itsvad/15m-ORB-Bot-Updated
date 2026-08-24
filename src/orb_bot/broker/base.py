"""Abstract broker interface the engine's Actions are executed against.

Two implementations exist:
- `DryRunBroker` (orb_bot.broker.dry_run_broker): consumes real market data
  and *simulates* order fills, logging every decision without ever placing a
  real order. This is the mode the bot must run in, verified across several
  full sessions, before live order placement is enabled.
- `TradovateBroker` (orb_bot.broker.tradovate_client): places real orders
  against Tradovate's REST/WebSocket API.

Both speak the same event contract: the engine emits `Action`s referencing
orders by a stable internal `order_ref` (e.g. "entry_long", "stop_loss"),
and the broker layer is responsible for mapping those to real/simulated
broker order IDs and for delivering `Fill` / `OrderStatusUpdate` events back
via the callbacks set on the broker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from orb_bot.strategy.models import (
    Action,
    Bar,
    Fill,
    OrderStatusUpdate,
)

FillHandler = Callable[[Fill], Awaitable[None]]
OrderStatusHandler = Callable[[OrderStatusUpdate], Awaitable[None]]
BarHandler = Callable[[Bar], Awaitable[None]]


class Broker(ABC):
    """Everything the app/scheduler layer needs from a broker connection."""

    def __init__(self) -> None:
        self.on_fill: FillHandler | None = None
        self.on_order_status: OrderStatusHandler | None = None
        self.on_bar: BarHandler | None = None

    @abstractmethod
    async def connect(self) -> None:
        """Authenticate / open connections. Must be safe to call once at
        startup; raises if the connection or credentials are invalid."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down connections cleanly."""

    @abstractmethod
    async def get_equity(self) -> float:
        """Current account equity (net liq), used for position sizing."""

    @abstractmethod
    async def get_recent_bars(self, lookback_minutes: int) -> list[Bar]:
        """1-minute bars for the last `lookback_minutes`, used to warm up
        indicator state after a restart mid-session."""

    @abstractmethod
    async def execute(self, action: Action) -> None:
        """Execute one engine Action (place/cancel/modify order, close
        position, alert, notify)."""

    @abstractmethod
    async def execute_many(self, actions: list[Action]) -> None:
        """Execute a batch of Actions in order. Implementations should
        execute cancels before dependent placements when both are present
        in the same batch (the engine already orders its Action lists this
        way, e.g. cancel-opposite-then-place-protective-stop)."""
