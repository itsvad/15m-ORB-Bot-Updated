"""Paper/dry-run broker: simulates fills against real incoming market data
and NEVER places a real order. This is the mode the bot must run in first,
verified across several full sessions against a real Tradovate demo feed,
before TRADOVATE_ENV/ORB_MODE are ever promoted to live.

Market data (1-minute bars, starting equity) is supplied externally via
`feed_bar()` / `set_equity()` - this broker doesn't open any network
connection itself, which keeps it usable both against a live read-only
Tradovate market-data feed (wired up by the app/scheduler layer) and
directly against historical bars in tests, with identical simulation logic
either way.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from orb_bot.broker.base import Broker
from orb_bot.strategy.models import (
    Action,
    Bar,
    CancelOrder,
    ClosePositionMarket,
    Fill,
    ModifyOrder,
    PlaceStopOrder,
    Side,
)

logger = logging.getLogger("orb_bot.dry_run")


class DryRunBroker(Broker):
    def __init__(self, starting_equity: float, point_value: float) -> None:
        super().__init__()
        self._equity = starting_equity
        self._point_value = point_value
        self._working: dict[str, PlaceStopOrder] = {}
        self._last_bar: Bar | None = None
        # Tracked purely for simulated PnL -> simulated equity updates, the
        # same way a real account's equity moves with realized P&L.
        self._position_side: Side | None = None
        self._position_entry_price: float | None = None
        self._position_qty: int = 0

    async def connect(self) -> None:
        logger.info("DryRunBroker active - NO REAL ORDERS WILL BE PLACED.")

    async def close(self) -> None:
        pass

    async def get_equity(self) -> float:
        return self._equity

    def set_equity(self, equity: float) -> None:
        self._equity = equity

    async def get_recent_bars(self, lookback_minutes: int) -> list[Bar]:
        raise NotImplementedError(
            "DryRunBroker does not fetch bars itself; the app layer supplies "
            "historical warm-up bars from the read-only market-data source "
            "it already holds."
        )

    # -- simulated fills -----------------------------------------------

    async def feed_bar(self, bar: Bar) -> None:
        """Feed one real 1-minute bar and simulate any stop-order fills it
        would have triggered. Fill price is simplistically the stop price
        itself (no slippage model) - documented dry-run limitation."""
        self._last_bar = bar
        for order_ref in list(self._working.keys()):
            order = self._working.get(order_ref)
            if order is None:
                continue  # cancelled by a callback fired earlier in this same bar
            if not self._triggers(order, bar):
                continue
            del self._working[order_ref]
            fill = Fill(order_ref=order_ref, price=order.stop_price, qty=order.qty, timestamp=bar.timestamp)
            logger.info(
                "[DRY-RUN SIMULATED FILL] %s %s x%d @ %.2f",
                order_ref, order.side.value, order.qty, order.stop_price,
            )
            if order_ref in ("entry_long", "entry_short"):
                self._position_side = order.side
                self._position_entry_price = order.stop_price
                self._position_qty = order.qty
            elif order_ref == "stop_loss" and self._position_entry_price is not None and self._position_side is not None:
                self._apply_simulated_pnl(order.stop_price, order.qty)
                self._reduce_position(order.qty)
            if self.on_fill:
                await self.on_fill(fill)

    @staticmethod
    def _triggers(order: PlaceStopOrder, bar: Bar) -> bool:
        if order.side is Side.LONG:
            return bar.high >= order.stop_price
        return bar.low <= order.stop_price

    def _apply_simulated_pnl(self, exit_price: float, qty: int) -> None:
        assert self._position_side is not None and self._position_entry_price is not None
        sign = 1 if self._position_side is Side.LONG else -1
        pnl = (exit_price - self._position_entry_price) * sign * qty * self._point_value
        self._equity += pnl
        logger.info("[DRY-RUN SIMULATED PNL] %+.2f -> equity now %.2f", pnl, self._equity)

    def _reduce_position(self, qty: int) -> None:
        self._position_qty = max(0, self._position_qty - qty)
        if self._position_qty == 0:
            self._position_side = None
            self._position_entry_price = None

    # -- action execution (logs only; never places a real order) --------

    async def execute_many(self, actions: list[Action]) -> None:
        for action in actions:
            await self.execute(action)

    async def execute(self, action: Action) -> None:
        if isinstance(action, PlaceStopOrder):
            self._working[action.order_ref] = action
            logger.info(
                "[DRY-RUN] would PLACE %s stop order_ref=%s %s x%d @ %.2f (%s)",
                action.side.value, action.order_ref, action.side.value, action.qty,
                action.stop_price, action.reason,
            )
        elif isinstance(action, CancelOrder):
            was_working = self._working.pop(action.order_ref, None) is not None
            logger.info(
                "[DRY-RUN] would CANCEL order_ref=%s (%s) - was_working=%s",
                action.order_ref, action.reason, was_working,
            )
        elif isinstance(action, ModifyOrder):
            order = self._working.get(action.order_ref)
            if order is not None:
                self._working[action.order_ref] = replace(
                    order,
                    stop_price=action.new_stop_price if action.new_stop_price is not None else order.stop_price,
                    qty=action.new_qty if action.new_qty is not None else order.qty,
                )
            logger.info(
                "[DRY-RUN] would MODIFY order_ref=%s new_stop=%s new_qty=%s (%s)",
                action.order_ref, action.new_stop_price, action.new_qty, action.reason,
            )
        elif isinstance(action, ClosePositionMarket):
            price = self._last_bar.close if self._last_bar is not None else None
            if price is not None and self._position_side is not None and self._position_entry_price is not None:
                self._apply_simulated_pnl(price, action.qty)
                self._reduce_position(action.qty)
            logger.info(
                "[DRY-RUN] would CLOSE %d contract(s) at market (~%s) (%s)",
                action.qty, price, action.reason,
            )
        else:
            # Alert / Notify: the app layer's own dispatcher handles these
            # (decision log + Telegram); logging here too is a harmless
            # fallback if execute() is ever called directly.
            logger.info("[DRY-RUN] %r", action)
