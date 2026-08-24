"""Tradovate REST + WebSocket client.

Grounded directly in Tradovate's own example source (tradovate/example-api-js
on GitHub - tutorial/Access and tutorial/WebSockets) since api.tradovate.com
and partner.tradovate.com were not reachable from this environment's network
policy. Concretely verified from that source:

- Base URLs: https://demo.tradovateapi.com/v1 (paper) and
  https://live.tradovateapi.com/v1 (live); WS at
  wss://demo.tradovateapi.com/v1/websocket / wss://live.tradovateapi.com/v1/websocket,
  and market data at wss://md.tradovateapi.com/v1/websocket.
- Auth: POST /auth/accesstokenrequest with
  {name, password, appId, appVersion, cid, sec, deviceId} (cid/sec are an
  API application's client id/secret; name/password are the trading
  account's login) returns {accessToken, mdAccessToken, expirationTime,
  userId, userStatus, name} or {errorText} / a {'p-ticket','p-time'} time-
  penalty retry challenge. All REST calls send `Authorization: Bearer
  <accessToken>`.
- Orders: POST /order/placeOrder with {accountSpec, accountId, action,
  symbol, orderQty, orderType, price, stopPrice, timeInForce, isAutomated,
  clOrdId, ...}. `isAutomated: true` is required for algorithmic/bot orders
  per Tradovate's own tutorial (exchange policy). Cancellation is
  POST /order/cancelorder with {orderId}.
- WebSocket frames are single characters: 'o' (open), 'h' (heartbeat),
  'a' (a JSON array of payloads), 'c' (closed). Requests are newline-
  delimited text: "{endpoint}\\n{reqId}\\n{query}\\n{body}", e.g.
  "authorize\\n1\\n\\n{accessToken}"; the server acks with
  `[{"s":200,"i":<reqId>}]` on that same socket.

What is NOT independently confirmed from a reachable primary source in this
session (network egress to api.tradovate.com/partner.tradovate.com/
deepwiki.com was blocked) and MUST be verified against a live demo
connection before this is trusted for real order flow: the exact
`user/syncrequest` snapshot shape and the live entity-update event schema
for order/fill/position pushes. `TradovateUserSocket._handle_array_frame`
below is written against the widely-documented/community-consistent shape
(`{"e": "props", "d": {"entityType": ..., "entity": ...}}` for updates, and
an initial sync snapshot with orders/fills/positions/cashBalances lists) but
treat it as a best-effort starting point, not gospel - this is exactly why
the project starts in dry-run mode and confirms decisions visually before
any live order is ever placed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
import logging
from dataclasses import dataclass

import httpx
import websockets

from orb_bot.config import RuntimeSecrets
from orb_bot.strategy.models import Bar

logger = logging.getLogger("orb_bot.tradovate")

URLS = {
    "demo": {
        "rest": "https://demo.tradovateapi.com/v1",
        "ws": "wss://demo.tradovateapi.com/v1/websocket",
    },
    "live": {
        "rest": "https://live.tradovateapi.com/v1",
        "ws": "wss://live.tradovateapi.com/v1/websocket",
    },
    "md_ws": "wss://md.tradovateapi.com/v1/websocket",
}


class TradovateAuthError(RuntimeError):
    pass


@dataclass
class TokenState:
    access_token: str | None = None
    md_access_token: str | None = None
    expiration: dt.datetime | None = None

    def valid(self) -> bool:
        return bool(
            self.access_token
            and self.expiration
            and dt.datetime.now(dt.timezone.utc) < self.expiration
        )


class TradovateRestClient:
    """Thin async REST wrapper handling auth, retries and the token
    lifecycle. All non-auth calls attach `Authorization: Bearer <token>`."""

    def __init__(self, secrets: RuntimeSecrets) -> None:
        self.secrets = secrets
        self.base_url = URLS[secrets.tradovate_env]["rest"]
        self.token = TokenState()
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=15.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def authenticate(self) -> TokenState:
        payload = {
            "name": self.secrets.tradovate_username,
            "password": self.secrets.tradovate_password,
            "appId": self.secrets.tradovate_app_id,
            "appVersion": self.secrets.tradovate_app_version,
            "cid": self.secrets.tradovate_cid,
            "sec": self.secrets.tradovate_secret,
            "deviceId": self.secrets.tradovate_device_id,
        }
        data = await self._auth_request(payload)
        return self.token

    async def _auth_request(self, payload: dict) -> dict:
        resp = await self._http.post("/auth/accesstokenrequest", json=payload)
        resp.raise_for_status()
        data = resp.json()

        if data.get("p-ticket"):
            if data.get("p-captcha"):
                raise TradovateAuthError(
                    "Tradovate returned a captcha challenge on auth - cannot "
                    "retry programmatically. Log in via the web/desktop app "
                    "once to clear it, then retry."
                )
            wait_s = float(data.get("p-time", 5))
            logger.warning("Tradovate time-penalty on auth; retrying in %.0fs", wait_s)
            await asyncio.sleep(wait_s)
            retry_payload = {**payload, "p-ticket": data["p-ticket"]}
            return await self._auth_request(retry_payload)

        if data.get("errorText"):
            raise TradovateAuthError(f"Tradovate auth failed: {data['errorText']}")

        self.token.access_token = data["accessToken"]
        self.token.md_access_token = data.get("mdAccessToken")
        # expirationTime is ISO8601 UTC per Tradovate's docs/examples.
        self.token.expiration = dt.datetime.fromisoformat(
            data["expirationTime"].replace("Z", "+00:00")
        )
        return data

    async def _ensure_token(self) -> str:
        if not self.token.valid():
            await self.authenticate()
        assert self.token.access_token is not None
        return self.token.access_token

    async def request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        token = await self._ensure_token()
        resp = await self._http.request(
            method,
            path,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def get(self, path: str) -> dict:
        return await self.request("GET", path)

    async def post(self, path: str, json_body: dict) -> dict:
        return await self.request("POST", path, json_body)

    # -- account -----------------------------------------------------

    async def list_accounts(self) -> list[dict]:
        return await self.get("/account/list")

    async def cash_balance_snapshot(self, account_id: int) -> dict:
        return await self.post("/cashBalance/getCashBalanceSnapshot", {"accountId": account_id})

    # -- orders --------------------------------------------------------

    async def place_order(self, **fields) -> dict:
        body = {"isAutomated": True, **fields}
        return await self.post("/order/placeOrder", body)

    async def cancel_order(self, order_id: int) -> dict:
        return await self.post("/order/cancelorder", {"orderId": order_id})

    async def modify_order(self, order_id: int, **fields) -> dict:
        return await self.post("/order/modifyorder", {"orderId": order_id, **fields})

    async def liquidate_position(self, account_id: int, symbol: str) -> dict:
        return await self.post(
            "/order/liquidateposition", {"accountId": account_id, "symbol": symbol}
        )


class TradovateSocket:
    """Generic Tradovate frame-protocol WebSocket client.

    Frames: 'o' open, 'h' heartbeat (reply with 'h' or nothing per docs -
    Tradovate's examples show only heartbeats being logged, not replied to,
    since the socket library handles ping/pong at a lower level), 'a' JSON
    array of payloads, 'c' closed.
    """

    def __init__(self, url: str, access_token_getter) -> None:
        self.url = url
        self._get_token = access_token_getter
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._req_id = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._authorized = asyncio.Event()
        self.on_array_frame = None  # set by owner: Callable[[list[dict]], Awaitable[None]]
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.url, ping_interval=20, ping_timeout=20)
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws:
            await self._ws.close()

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        async for message in self._ws:
            frame_type, body = message[:1], message[1:]
            if frame_type == "o":
                token = await self._get_token()
                await self._send_raw(f"authorize\n0\n\n{token}")
            elif frame_type == "h":
                continue
            elif frame_type == "a":
                payloads = json.loads(body)
                for p in payloads:
                    if p.get("i") == 0 and p.get("s") == 200:
                        self._authorized.set()
                    req_id = p.get("i")
                    if req_id in self._pending:
                        self._pending.pop(req_id).set_result(p)
                if self.on_array_frame:
                    await self.on_array_frame(payloads)
            elif frame_type == "c":
                logger.warning("Tradovate WS closed: %s", body)

    async def _send_raw(self, text: str) -> None:
        assert self._ws is not None
        await self._ws.send(text)

    async def wait_authorized(self, timeout: float = 15.0) -> None:
        await asyncio.wait_for(self._authorized.wait(), timeout=timeout)

    async def request(self, endpoint: str, body: dict | None = None, query: str = "") -> dict:
        await self.wait_authorized()
        req_id = next(self._req_id)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        body_str = json.dumps(body) if body is not None else ""
        await self._send_raw(f"{endpoint}\n{req_id}\n{query}\n{body_str}")
        return await asyncio.wait_for(fut, timeout=15.0)


def bars_from_chart_response(chart_data: dict) -> list[Bar]:
    """Convert a Tradovate chart/getChart-style bar list into our Bar type.
    NOT independently verified against a live response in this session
    (see module docstring) - field names (`timestamp`/`open`/`high`/`low`
    /`close`) follow the commonly documented md/getChart bar shape and
    should be spot-checked against the demo feed before relying on it."""
    from orb_bot.timeutils import to_ny

    bars: list[Bar] = []
    for b in chart_data.get("bars", []):
        ts = dt.datetime.fromisoformat(b["timestamp"].replace("Z", "+00:00"))
        bars.append(
            Bar(
                timestamp=to_ny(ts),
                open=b["open"],
                high=b["high"],
                low=b["low"],
                close=b["close"],
            )
        )
    return bars


class TradovateMarketData:
    """Read-only market-data access (recent 1-minute bars), independent of
    any order-placement path. This is what lets dry-run mode watch a real
    Tradovate demo feed without the DryRunBroker (which deliberately knows
    nothing about market data - see its docstring) ever needing order-level
    credentials or touching the order-placement code path at all.

    `TradovateBroker` also uses this internally for its own `get_recent_bars`,
    so there is exactly one implementation of the (best-effort, unverified -
    see this module's docstring) md/getChart bar-fetching logic.
    """

    def __init__(
        self, secrets: RuntimeSecrets, symbol: str, rest_client: "TradovateRestClient | None" = None
    ) -> None:
        self.secrets = secrets
        self.symbol = symbol
        # A caller that already holds an authenticated TradovateRestClient
        # (e.g. TradovateBroker) can share it here rather than this class
        # opening a second, redundant REST connection/token lifecycle.
        self.rest = rest_client if rest_client is not None else TradovateRestClient(secrets)
        self._owns_rest = rest_client is None

    async def connect(self) -> None:
        await self.rest.authenticate()

    async def close(self) -> None:
        if self._owns_rest:
            await self.rest.close()

    async def _get_md_token(self) -> str:
        if not self.rest.token.valid():
            await self.rest.authenticate()
        assert self.rest.token.md_access_token is not None
        return self.rest.token.md_access_token

    async def get_recent_bars(self, lookback_minutes: int) -> list[Bar]:
        md_socket = TradovateSocket(URLS["md_ws"], self._get_md_token)
        await md_socket.connect()
        try:
            resp = await md_socket.request(
                "md/getChart",
                {
                    "symbol": self.symbol,
                    "chartDescription": {
                        "underlyingType": "MinuteBar",
                        "elementSize": 1,
                        "elementSizeUnit": "UnderlyingUnits",
                    },
                    "timeRange": {"asMuchAsElements": lookback_minutes},
                },
            )
            return bars_from_chart_response(resp.get("d", resp))
        finally:
            await md_socket.close()
