"""Broker adapter that places real orders against Tradovate.

Order placement/cancellation/modification go over REST (well-grounded - see
tradovate_client.py's docstring for sourcing). Fill and order-status events
are consumed from the user-data WebSocket's sync stream; that schema is
best-effort (not independently confirmed against a live response in this
session) and must be validated against the demo account before this class
is ever pointed at TRADOVATE_ENV=live.

This module is NOT exercised by unit tests (it requires live network
access to Tradovate); the state machine and sizing logic it merely
routes actions to are tested independently in tests/test_state_machine.py.
"""
from __future__ import annotations

import datetime as dt
import logging

from orb_bot.broker.base import Broker
from orb_bot.broker.tradovate_client import (
    URLS,
    TradovateMarketData,
    TradovateRestClient,
    TradovateSocket,
)
from orb_bot.config import RuntimeSecrets
from orb_bot.strategy.models import (
    Action,
    Alert,
    Bar,
    CancelOrder,
    ClosePositionMarket,
    Fill,
    ModifyOrder,
    Notify,
    OrderStatusUpdate,
    PlaceStopOrder,
    Side,
)
from orb_bot.timeutils import to_ny

logger = logging.getLogger("orb_bot.tradovate_broker")

_SIDE_TO_ACTION = {Side.LONG: "Buy", Side.SHORT: "Sell"}


class TradovateBroker(Broker):
    def __init__(self, secrets: RuntimeSecrets, symbol: str) -> None:
        super().__init__()
        self.secrets = secrets
        self.symbol = symbol
        self.rest = TradovateRestClient(secrets)
        self.user_ws = TradovateSocket(
            URLS[secrets.tradovate_env]["ws"], self._get_access_token
        )
        # Shares this broker's authenticated REST client rather than opening
        # a second token lifecycle for market data.
        self.market_data = TradovateMarketData(secrets, symbol, rest_client=self.rest)
        self.account_id: int | None = None
        self.account_spec: str | None = None
        # order_ref (engine-internal) <-> Tradovate order id
        self._ref_to_order_id: dict[str, int] = {}
        self._order_id_to_ref: dict[int, str] = {}
        # Tracked from fills so market-close actions (which carry no side,
        # only qty) send the correct opposite-side order rather than
        # guessing - a wrong direction here would add to the position
        # instead of closing it. _position_qty is decremented by every
        # closing fill (partial TP1 included) so we only clear the side
        # once the position is actually flat.
        self._position_side: Side | None = None
        self._position_qty: int = 0

    async def _get_access_token(self) -> str:
        if not self.rest.token.valid():
            await self.rest.authenticate()
        assert self.rest.token.access_token is not None
        return self.rest.token.access_token

    async def connect(self) -> None:
        await self.rest.authenticate()
        accounts = await self.rest.list_accounts()
        if not accounts:
            raise RuntimeError("Tradovate auth succeeded but no accounts were returned")
        account = accounts[0]
        self.account_id = account["id"]
        self.account_spec = account["name"]
        logger.info("Connected to Tradovate account %s (%s)", self.account_spec, self.secrets.tradovate_env)

        self.user_ws.on_array_frame = self._handle_array_frame
        await self.user_ws.connect()
        await self.user_ws.request(
            "user/syncrequest", {"users": [account["userId"]]}
        )

    async def close(self) -> None:
        await self.user_ws.close()
        await self.rest.close()

    async def get_equity(self) -> float:
        assert self.account_id is not None
        snapshot = await self.rest.cash_balance_snapshot(self.account_id)
        # netLiq / cashBalance field naming per the commonly documented
        # cashBalance snapshot shape - verify against your account's actual
        # response before trusting this for live sizing.
        for key in ("netLiq", "netLiquidationValue", "cashBalance", "amount"):
            if key in snapshot:
                return float(snapshot[key])
        raise RuntimeError(
            f"Could not find an equity field in cash balance snapshot: {snapshot!r}"
        )

    async def get_recent_bars(self, lookback_minutes: int) -> list[Bar]:
        return await self.market_data.get_recent_bars(lookback_minutes)

    # -- action execution --------------------------------------------------

    async def execute_many(self, actions: list[Action]) -> None:
        for action in actions:
            await self.execute(action)

    async def execute(self, action: Action) -> None:
        if isinstance(action, PlaceStopOrder):
            await self._place_stop_order(action)
        elif isinstance(action, CancelOrder):
            await self._cancel_order(action)
        elif isinstance(action, ModifyOrder):
            await self._modify_order(action)
        elif isinstance(action, ClosePositionMarket):
            await self._close_position_market(action)
        elif isinstance(action, Alert):
            logger.warning("ALERT[%s]: %s", action.level, action.message)
        elif isinstance(action, Notify):
            logger.info("NOTIFY[%s]: %s", action.event, action.payload)

    async def _place_stop_order(self, action: PlaceStopOrder) -> None:
        resp = await self.rest.place_order(
            accountId=self.account_id,
            accountSpec=self.account_spec,
            action=_SIDE_TO_ACTION[action.side],
            symbol=self.symbol,
            orderQty=action.qty,
            orderType="Stop",
            stopPrice=action.stop_price,
            timeInForce="Day",
            clOrdId=action.order_ref,
        )
        order_id = resp.get("orderId")
        if order_id is None:
            logger.error("placeOrder for %s did not return an orderId: %s", action.order_ref, resp)
            return
        self._ref_to_order_id[action.order_ref] = order_id
        self._order_id_to_ref[order_id] = action.order_ref

    async def _cancel_order(self, action: CancelOrder) -> None:
        order_id = self._ref_to_order_id.get(action.order_ref)
        if order_id is None:
            logger.info("cancel requested for unknown/already-gone order_ref=%s (%s)", action.order_ref, action.reason)
            return
        # Explicit + redundant: issue the cancel, and if it doesn't clearly
        # succeed, retry once more before giving up loudly (never assume a
        # single call was enough - see the race-condition lesson).
        for attempt in range(2):
            try:
                await self.rest.cancel_order(order_id)
                return
            except Exception:
                logger.exception(
                    "cancelOrder failed for order_ref=%s order_id=%s (attempt %d)",
                    action.order_ref, order_id, attempt + 1,
                )
        logger.critical(
            "FAILED to cancel order_ref=%s order_id=%s after retries - manual "
            "intervention may be required", action.order_ref, order_id,
        )

    async def _modify_order(self, action: ModifyOrder) -> None:
        order_id = self._ref_to_order_id.get(action.order_ref)
        if order_id is None:
            logger.error("modify requested for unknown order_ref=%s", action.order_ref)
            return
        fields: dict = {}
        if action.new_stop_price is not None:
            fields["stopPrice"] = action.new_stop_price
        if action.new_qty is not None:
            fields["orderQty"] = action.new_qty
        await self.rest.modify_order(order_id, **fields)

    async def _close_position_market(self, action: ClosePositionMarket) -> None:
        if self._position_side is None:
            logger.critical(
                "ClosePositionMarket(qty=%d, reason=%s) requested but this "
                "broker has no record of an open position's side - refusing "
                "to guess a direction (a wrong side would ADD to the "
                "position instead of closing it). Use liquidate_position "
                "or investigate immediately.", action.qty, action.reason,
            )
            return
        closing_side = Side.SHORT if self._position_side is Side.LONG else Side.LONG
        await self.rest.place_order(
            accountId=self.account_id,
            accountSpec=self.account_spec,
            action=_SIDE_TO_ACTION[closing_side],
            symbol=self.symbol,
            orderQty=action.qty,
            orderType="Market",
            timeInForce="Day",
            clOrdId=f"close_{action.reason}",
        )

    # -- WS fill/order-status sync (best-effort schema; verify live) ------

    async def _handle_array_frame(self, payloads: list[dict]) -> None:
        for p in payloads:
            entity_type = p.get("d", {}).get("entityType") if isinstance(p.get("d"), dict) else None
            if entity_type == "fill" and self.on_fill:
                fill_data = p["d"]["entity"]
                order_id = fill_data.get("orderId")
                order_ref = self._order_id_to_ref.get(order_id)
                if order_ref is None:
                    logger.warning("Fill for unmapped orderId=%s: %s", order_id, fill_data)
                    continue
                qty = int(fill_data["qty"])
                if order_ref == "entry_long":
                    self._position_side = Side.LONG
                    self._position_qty = qty
                elif order_ref == "entry_short":
                    self._position_side = Side.SHORT
                    self._position_qty = qty
                elif order_ref == "stop_loss" or order_ref.startswith("close_"):
                    self._position_qty = max(0, self._position_qty - qty)
                    if self._position_qty == 0:
                        self._position_side = None
                await self.on_fill(
                    Fill(
                        order_ref=order_ref,
                        price=float(fill_data["price"]),
                        qty=int(fill_data["qty"]),
                        timestamp=to_ny(
                            dt.datetime.fromisoformat(
                                fill_data["timestamp"].replace("Z", "+00:00")
                            )
                        ),
                    )
                )
            elif entity_type == "order" and self.on_order_status:
                order_data = p["d"]["entity"]
                order_ref = self._order_id_to_ref.get(order_data.get("id"))
                status = order_data.get("ordStatus")
                if order_ref and status:
                    await self.on_order_status(
                        OrderStatusUpdate(
                            order_ref=order_ref,
                            status=status,
                            timestamp=to_ny(dt.datetime.now(dt.timezone.utc)),
                        )
                    )
