# Range Breakout Strategy Bot

Automated ES/MES opening-range-breakout trading bot targeting the Tradovate
API. **This is a first-session scaffold: it starts and stays in dry-run mode
until you've watched it decide correctly against a real Tradovate demo feed
for several full sessions.** Nothing in this repo places a live order by
default, and `TRADOVATE_ENV=live` is refused unless `ORB_MODE=live` is also
set explicitly (`orb_bot/config.py::RuntimeSecrets`).

## What's implemented in this session

- Full project scaffolding, dependency management (`pyproject.toml`), and a
  single YAML config (`config/config.yaml`) covering every strategy
  parameter called out in the spec - nothing is hardcoded.
- The core strategy state machine (`src/orb_bot/strategy/`): opening-range
  calculation, the width filter, symmetric stop-entry placement, the
  entry-race defensive handling, breakeven/TP1/both runner-trail modes, the
  whipsaw guard, hard-close, FOMC flatten, and the daily-loss-limit/sizing
  safety rails. It is pure (no I/O) and unit-tested against synthetic
  historical bar sequences independent of any live connection
  (`tests/test_state_machine.py`, 14 scenarios).
- Tradovate REST + WebSocket client (`src/orb_bot/broker/tradovate_client.py`,
  `tradovate_broker.py`), grounded in Tradovate's own public example source
  - see **Tradovate API research** below for exactly what was and wasn't
  confirmed, and why.
- A dry-run broker (`broker/dry_run_broker.py`) that simulates stop-order
  fills against real incoming bars and logs every decision without ever
  calling a real order-placement endpoint.
- SQLite-backed state persistence (`persistence/store.py`) so a restart
  mid-session doesn't lose the day's state (entries placed, position/runner
  phase, halted/paused flags) - resuming re-attaches a fresh `OrbEngine` to
  the persisted `DayState`.
- Telegram notifications + `/status`, `/pause`, `/resume`, `/flatten`
  (`notify/telegram_bot.py`).
- Structured JSONL decision log + a CSV trade log
  (`logging_setup.py`) with the fields called out in the spec (date,
  direction, OR width, outcome, R-multiple, entry/exit price/time).
- Scheduler (`scheduler.py`) that sleeps outside the trading window and
  wakes automatically each session, plus `app.py`, which wires everything
  together into an always-on service, and a wall-clock-driven hard-close
  check (`OrbEngine.on_wall_clock`) that fires independent of whether a bar
  ever arrives at exactly 15:55 (holidays/gaps/early closes).
- A settings web UI (`orb_bot/webui.py`, `python -m orb_bot.webui`) for
  adjusting the tunable parameters without hand-editing YAML - see
  **Settings UI** below.
- `scripts/replay_backtest.py`: runs the exact same engine + dry-run broker
  against a CSV of historical 1-minute bars, no network required. Try it:

  ```bash
  python scripts/replay_backtest.py --csv data/sample_bars/2026-06-15.csv \
      --date 2026-06-15 --equity 50000
  ```

## Getting started

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # 40 tests, no network needed
cp .env.example .env        # fill in Tradovate demo + Telegram credentials
python -m orb_bot.app --config config/config.yaml
```

`ORB_MODE` defaults to `dry_run` in `.env.example` - leave it there.

## Settings UI

A small local web UI lets you adjust the tunable parameters - OR width
range to target, R-multiples (breakeven/TP1), how much % is left running
past TP1, runner mode + its SMA/timeframe/lookback params, trading times,
the point buffer outside the range, risk %, sizing sanity cap, FOMC toggle,
and the daily loss limit - without hand-editing YAML.

```bash
export ORB_WEBUI_PASSWORD=choose-something-strong   # required, no default
python -m orb_bot.webui --config config/config.yaml --port 8787
```

Then open `http://127.0.0.1:8787` (HTTP Basic auth, username `admin` by
default). It reads and writes `config.yaml` directly, validated against the
exact same `AppConfig` schema the bot enforces at startup - an invalid
combination (e.g. min width > max width, or a point value that doesn't
match the selected symbol) is rejected with a clear error and never
written. Every save keeps a timestamped backup of the previous version
under `config/backups/`.

**Changes apply starting the next trading session, never mid-trade** -
`run_forever` reloads `config.yaml` fresh before scheduling each session,
so nothing about a currently-open position's risk parameters can change
underneath it.

It binds to `127.0.0.1` by default. To reach it from your phone/laptop,
either SSH-tunnel (`ssh -L 8787:127.0.0.1:8787 you@your-vm`) or put a TLS
reverse proxy in front of it - Basic auth over plain HTTP beyond localhost
is not safe for something that edits live risk parameters. See
`deploy/orb-bot-webui.service` to run it as its own systemd unit alongside
`deploy/orb-bot.service`.

## Config

Every parameter from the spec lives in `config/config.yaml`, with the
documented defaults (OR 9:30 + 15min, entry cutoff 10:30, hard close 15:55,
OR width filter 10-32pts, 0.5pt buffer, 1% equity risk, breakeven at +1R,
TP1 at +1.5R closing 90%, both runner modes present and selected via
`exits.runner.mode: old|new`, whipsaw guard at 15:00, $500 daily loss
limit, FOMC handling off by default). Secrets and the dry-run/live switch
are environment variables only (`.env`, see `.env.example`) - never in the
YAML, so config can be committed safely.

`data/fomc_dates.yaml` is an explicit, manually-maintained list (not
scraped), same as the spec asked for; update it yearly from the Fed's
published schedule. `orb_bot/timeutils.py::MARKET_CALENDAR_EXCEPTIONS` is
the equivalent explicit list for market holidays/early closes - update it
the same way.

## Architecture notes / why it's structured this way

- **`OrbEngine` is pure.** It never reads a clock or touches the network -
  it consumes `Bar`/`Fill`/`OrderStatusUpdate`/wall-clock events and emits
  `Action`s (`PlaceStopOrder`, `CancelOrder`, `ModifyOrder`,
  `ClosePositionMarket`, `Alert`, `Notify`) referencing orders by a stable
  internal `order_ref` rather than a broker order ID. That's what makes it
  testable against historical data and swappable between the dry-run and
  live brokers without touching the strategy logic.
- **The entry-order race condition** called out in the spec (symmetric OR
  levels mean a stop-loss price can equal the "cancelled" opposite entry's
  price) is handled two ways: the opposite entry is cancelled explicitly the
  instant either side fills (never relying on an OCO/bracket), *and*
  `OrbEngine.on_fill` treats a fill on the opposite entry ref arriving after
  a position is already open as a race and immediately flattens the
  erroneous fill rather than ever holding an accidental hedge position. See
  `TestEntryFillAndRaceCondition` in `tests/test_state_machine.py`.
- **Market-order closes (TP1, hard close, FOMC flatten, SMA close-through)
  are treated as immediately effective** in engine bookkeeping (qty/PnL
  updated the moment the action is issued, at that decision's reference
  price), while stop-type orders (entries, the protective/trailing stop)
  genuinely wait for a real `Fill` event, since only those are actually
  price-contingent. This is a deliberate v1 simplification, documented in
  `state_machine.py` - a broker-side slippage/reconciliation pass for
  market orders would be a reasonable follow-up before scaling size.
- **Bar ingestion is polling-based in this session** (`broker/bar_source.py`
  `PollingTradovateBarSource`), not a WebSocket `md/subscribeChart` push
  feed. This was a deliberate scoping choice given the API-research
  constraints below; a push subscription is a good next iteration once
  poll-based dry-run behavior has been visually verified.

## Tradovate API research

`api.tradovate.com` and `partner.tradovate.com` (the official docs) and
`deepwiki.com` were unreachable from this environment's network egress
policy. What's implemented is grounded instead in Tradovate's own example
source (`github.com/tradovate/example-api-js`, `tutorial/Access` and
`tutorial/WebSockets`), which is real first-party code, not a summary:

- Base URLs (`tutorial/tutorialsURLs.js`): `https://demo.tradovateapi.com/v1`,
  `https://live.tradovateapi.com/v1`, WS at
  `wss://{demo,live}.tradovateapi.com/v1/websocket`, market data at
  `wss://md.tradovateapi.com/v1/websocket`.
- Auth (`EX-4a-Place-An-Order/src/connect.js`): `POST /auth/accesstokenrequest`
  with `{name, password, appId, appVersion, cid, sec, deviceId}`, a Bearer
  `accessToken` in the response, and a `p-ticket`/`p-time` time-penalty retry
  challenge that must be honored (implemented in
  `tradovate_client.py::TradovateRestClient._auth_request`).
- Orders (`EX-4a-Place-An-Order/src/placeOrder.js` + its README):
  `POST /order/placeOrder` with `{accountSpec, accountId, action, symbol,
  orderQty, orderType, stopPrice, timeInForce, isAutomated, ...}` -
  `isAutomated: true` is called out by Tradovate's own tutorial as required
  for algorithmic orders, so it's hardcoded `True` in every order this bot
  places. Cancellation is `POST /order/cancelorder` with `{orderId}`.
- WS frame protocol (`tutorial/WebSockets/EX-05-WebSockets-Start/README.md`):
  single-character frame types `o`/`h`/`a`/`c`, newline-delimited requests
  (`"{endpoint}\n{reqId}\n{query}\n{body}"`), authorize via
  `"authorize\n{reqId}\n\n{accessToken}"`.

**Not independently confirmed in this session** - the exact `user/syncrequest`
snapshot shape and the live order/fill/position push-event schema
(`TradovateBroker._handle_array_frame`), and the exact `md/getChart` bar
response shape (`bars_from_chart_response`). Both are implemented against
the commonly-documented/community-consistent shape and clearly flagged
in-code, but **must be checked against a real demo connection** before
being trusted - which is exactly why dry-run verification comes first.

## Open items / things worth a second pair of eyes

1. **The trade-log CSV column names are a reasonable default**
   (`logging_setup.py::TRADE_LOG_FIELDS`), not copied from your actual
   spreadsheet (I didn't have access to it). Rename/reorder to match.
2. **TP1 close-quantity rounding**: at the default 90%/1-lot combination,
   `int(1 * 0.90) == 0`, so no partial fires and the full size becomes the
   runner - logged loudly (`Alert("warning", ...)`) rather than silently
   dropped. Worth confirming this is the intended behavior vs., say,
   rounding to nearest instead of flooring.
3. **Phase B of the "new" runner mode is ratcheted** (never retreats) even
   though the spec's wording for phase B doesn't say so explicitly - I
   applied the same "never loosens a resting stop" principle used
   everywhere else. Flag if that's not intended.
4. **Live push bar streaming vs. polling** - see architecture notes above.
5. Position sizing here **includes** the entry buffer twice in the stop
   distance (`(OR high + buffer) - (OR low - buffer) = width + 2*buffer`),
   matching "stop-loss on the opposite side of the OR, same buffer
   distance" read literally. Double-check that matches your backtest's
   convention exactly (some implementations size off OR width alone).

## Deployment

`deploy/orb-bot.service` is a systemd unit for a small Linux VM
(DigitalOcean/AWS-equivalent). It runs `python -m orb_bot.app`, which sleeps
outside the trading window and wakes automatically each session -
`WARMUP_BUFFER_MINUTES`/`WIND_DOWN_BUFFER_MINUTES` in `scheduler.py` control
how early/late it stays connected around the session.

```bash
sudo cp deploy/orb-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orb-bot
```

## Safety rails (hardcoded, not config-toggleable off)

- Daily loss limit force-flattens and halts the rest of the day
  (`OrbEngine._check_daily_loss_limit`).
- Position sizing refuses (raises loudly, never silently submits) a zero,
  negative, or absurd computed size, and a misconfigured point value is
  rejected at config load time against known contract specs
  (`config.py::KNOWN_POINT_VALUES`, `sizing.py::InvalidSizingInput`).
- One trade per day is enforced structurally (`safety.max_orders_per_day`
  is pinned to exactly `1` in the config schema).
