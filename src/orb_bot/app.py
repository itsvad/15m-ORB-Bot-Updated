"""Wires config + secrets + broker + persistence + Telegram + logging
together and runs the always-on session loop.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging

from orb_bot.broker.bar_source import BarSource, PollingTradovateBarSource, ReplayBarSource
from orb_bot.broker.base import Broker
from orb_bot.broker.dry_run_broker import DryRunBroker
from orb_bot.broker.tradovate_broker import TradovateBroker
from orb_bot.broker.tradovate_client import TradovateMarketData
from orb_bot.config import AppConfig, RuntimeSecrets, load_config
from orb_bot.fomc import load_fomc_dates
from orb_bot.logging_setup import DecisionLogger, TradeLogWriter, configure_logging
from orb_bot.notify.telegram_bot import TelegramNotifier
from orb_bot.persistence.store import StateStore
from orb_bot.scheduler import next_session_start, session_window_for, sleep_until
from orb_bot.strategy.models import (
    Action,
    Alert,
    CancelOrder,
    ClosePositionMarket,
    ModifyOrder,
    Notify,
    PlaceStopOrder,
)
from orb_bot.strategy.state_machine import OrbEngine
from orb_bot.timeutils import now_ny

logger = logging.getLogger("orb_bot.app")

DEFAULT_STARTING_EQUITY = 50_000.0  # used only for a brand-new dry-run with no prior session


class AppContext:
    def __init__(
        self,
        config: AppConfig,
        secrets: RuntimeSecrets,
        broker: Broker,
        store: StateStore,
        decision_log: DecisionLogger,
        trade_log: TradeLogWriter,
        telegram: TelegramNotifier | None,
        fomc_dates: set[dt.date],
        config_path: str = "config/config.yaml",
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.secrets = secrets
        self.broker = broker
        self.store = store
        self.decision_log = decision_log
        self.trade_log = trade_log
        self.telegram = telegram
        self.fomc_dates = fomc_dates
        self._trade_context: dict = {}
        self._current_engine: OrbEngine | None = None
        self.bar_source: BarSource | None = None
        # Read-only market-data connection. In live mode this is the same
        # object as broker.market_data (shares its authenticated REST
        # client); in dry-run mode it's a standalone connection so dry-run
        # can watch a real demo feed without the order-placement broker
        # ever being involved. None if no Tradovate credentials are
        # configured at all (replay-only mode - see scripts/replay_backtest.py).
        self.market_data: TradovateMarketData | None = None
        self._owns_market_data = False


def build_app_context(config_path: str) -> AppContext:
    config = load_config(config_path)
    secrets = RuntimeSecrets.from_env()
    configure_logging(config.logging.level, config.logging.log_dir)

    store = StateStore(secrets.state_db_path)
    decision_log = DecisionLogger(config.logging.decision_log_file)
    trade_log = TradeLogWriter(config.logging.trade_log_file)

    has_tradovate_credentials = bool(
        secrets.tradovate_username and secrets.tradovate_password
    )

    if secrets.mode == "live":
        broker: Broker = TradovateBroker(secrets, config.instrument.symbol)
        logger.warning("LIVE MODE - real orders will be placed against %s", secrets.tradovate_env)
    else:
        last_equity_raw = store.get_control("last_equity")
        starting_equity = float(last_equity_raw) if last_equity_raw else DEFAULT_STARTING_EQUITY
        broker = DryRunBroker(starting_equity, config.instrument.point_value)
        logger.info("DRY-RUN MODE - no real orders will ever be placed.")

    telegram = None
    if secrets.telegram_bot_token and secrets.telegram_allowed_chat_ids:
        telegram = TelegramNotifier(
            secrets.telegram_bot_token,
            secrets.telegram_allowed_chat_ids,
            store,
            status_provider=lambda: _status_text(ctx),
            flatten_callback=lambda: _emergency_flatten(ctx),
        )
    else:
        logger.warning("Telegram not configured (missing token or allowed chat ids) - remote control disabled")

    fomc_dates = load_fomc_dates(config.fomc.dates_file) if config.fomc.enabled else set()

    ctx = AppContext(config, secrets, broker, store, decision_log, trade_log, telegram, fomc_dates, config_path)

    if isinstance(broker, TradovateBroker):
        # Live mode: reuse the broker's own market-data connection (shares
        # its authenticated REST client) rather than opening a second one.
        ctx.market_data = broker.market_data
        ctx._owns_market_data = False
    elif has_tradovate_credentials:
        # Dry-run mode with real credentials configured: watch a real demo
        # feed without the order-placement broker ever being involved.
        ctx.market_data = TradovateMarketData(secrets, config.instrument.symbol)
        ctx._owns_market_data = True
    else:
        logger.warning(
            "No Tradovate credentials configured - dry-run has no live bar "
            "source. Use scripts/replay_backtest.py against historical "
            "bars, or set TRADOVATE_USERNAME/TRADOVATE_PASSWORD in .env."
        )

    return ctx


async def _status_text(ctx: AppContext) -> str:
    engine = ctx._current_engine
    equity = await ctx.broker.get_equity()
    if engine is None:
        return f"No session currently running. Equity: ${equity:,.2f}. Paused: {ctx.store.is_paused()}"
    day = engine.day
    pos = day.position
    lines = [
        f"Date: {day.date.isoformat()}",
        f"Equity: ${equity:,.2f}",
        f"Paused: {ctx.store.is_paused()}",
        f"Halted (daily loss limit): {day.halted}",
        f"OR: {day.opening_range.low} - {day.opening_range.high} (finalized={day.or_finalized})",
        f"Filter passed: {day.filter_passed}",
        f"Trade done for day: {day.trade_done_for_day}",
    ]
    if pos:
        lines.append(
            f"Position: {pos.side.value} x{pos.runner_qty} @ {pos.entry_price} "
            f"stop={pos.current_stop} tp1_done={pos.tp1_done}"
        )
    else:
        lines.append("Position: none")
    return "\n".join(lines)


async def _emergency_flatten(ctx: AppContext) -> str:
    engine = ctx._current_engine
    if engine is None or engine.day.position is None:
        ctx.store.set_paused(True)
        return "No open position. Trading has been paused as a precaution."
    pos = engine.day.position
    side, qty = pos.side.value, pos.runner_qty
    actions = engine.manual_flatten(reason="manual_telegram_flatten")
    await dispatch_actions(ctx, engine, actions)
    ctx.store.set_paused(True)
    return f"Flattened {side} x{qty}. Trading paused - send /resume to re-enable."


async def dispatch_actions(ctx: AppContext, engine: OrbEngine, actions: list[Action]) -> None:
    for action in actions:
        ctx.decision_log.log_action(engine.day.date, action)

        if isinstance(action, PlaceStopOrder):
            is_new_entry = action.order_ref in (engine.day.long_entry_ref, engine.day.short_entry_ref)
            if is_new_entry and ctx.store.is_paused():
                logger.warning("Skipping entry order %s - trading is PAUSED", action.order_ref)
                continue
            await ctx.broker.execute(action)
        elif isinstance(action, (CancelOrder, ModifyOrder, ClosePositionMarket)):
            await ctx.broker.execute(action)
        elif isinstance(action, Alert):
            logger.warning("ALERT[%s] %s", action.level, action.message)
            if ctx.telegram:
                await ctx.telegram.notify(action)
        elif isinstance(action, Notify):
            _track_trade_context(ctx, action)
            if ctx.telegram:
                await ctx.telegram.notify(action)


def _track_trade_context(ctx: AppContext, action: Notify) -> None:
    p = action.payload
    if action.event == "entry_orders_placed":
        ctx._trade_context.update(
            {"or_high": p["or_high"], "or_low": p["or_low"], "or_width": p["or_width"]}
        )
    elif action.event == "entry":
        ctx._trade_context.update(
            {
                "direction": p["side"],
                "entry_price": p["price"],
                "entry_time": p["time"],
                "qty": p["qty"],
            }
        )
    elif action.event == "exit":
        row = {
            **ctx._trade_context,
            "date": ctx._current_engine.day.date.isoformat() if ctx._current_engine else "",
            "exit_price": p["exit_price"],
            "exit_time": p.get("time", ""),
            "exit_reason": p["reason"],
            "r_multiple": round(p["r_multiple"], 3),
            "pnl_usd": round(p["pnl_usd"], 2),
            "outcome": "win" if p["pnl_usd"] > 0 else ("loss" if p["pnl_usd"] < 0 else "scratch"),
        }
        ctx.trade_log.append(row)
        ctx.store.record_trade(row)


async def run_trading_session(ctx: AppContext, trading_date: dt.date) -> None:
    restored = ctx.store.load_day_state(trading_date)
    if restored is not None:
        logger.info("Resuming trading_date=%s from persisted state", trading_date)

    equity = await ctx.broker.get_equity()
    engine = OrbEngine(ctx.config, trading_date, equity, ctx.fomc_dates, day_state=restored)
    ctx._current_engine = engine
    ctx._trade_context = {}

    if restored is not None and restored.or_finalized:
        bar_getter = ctx.market_data.get_recent_bars if ctx.market_data else ctx.broker.get_recent_bars
        try:
            warmup_bars = await bar_getter(120)
            engine.warm_up_indicators(warmup_bars)
        except NotImplementedError:
            logger.warning(
                "No market-data source available to supply warm-up bars "
                "after restart; runner SMA history will rebuild from live "
                "bars only."
            )

    _, session_end = session_window_for(ctx.config, trading_date)

    while True:
        now = now_ny()
        if now >= session_end:
            break

        bar, timed_out = await _next_bar_or_timeout(ctx.bar_source, timeout=30.0)
        if bar is None and not timed_out:
            break  # a ReplayBarSource has been exhausted

        if bar is not None:
            if isinstance(ctx.broker, DryRunBroker):
                await ctx.broker.feed_bar(bar)
            actions = engine.on_bar(bar)
        else:
            actions = engine.on_wall_clock(now_ny())

        await dispatch_actions(ctx, engine, actions)
        ctx.store.save_day_state(engine.day)

    if isinstance(ctx.broker, DryRunBroker):
        ctx.store.set_control("last_equity", str(await ctx.broker.get_equity()))
    ctx._current_engine = None
    logger.info("Session for %s complete.", trading_date)


async def _next_bar_or_timeout(bar_source: BarSource, timeout: float) -> tuple[object | None, bool]:
    if isinstance(bar_source, ReplayBarSource):
        return await bar_source.next_bar(), False
    try:
        bar = await asyncio.wait_for(bar_source.next_bar(), timeout=timeout)
        return bar, False
    except asyncio.TimeoutError:
        return None, True


async def run_forever(ctx: AppContext, bar_source_factory) -> None:
    """The always-on loop: sleep outside the trading window, run one full
    session, repeat - forever."""
    await ctx.broker.connect()
    if ctx._owns_market_data and ctx.market_data is not None:
        await ctx.market_data.connect()
    if ctx.telegram:
        await ctx.telegram.start()

    try:
        while True:
            # Reload config.yaml fresh before scheduling the next session, so
            # changes made via the settings UI (or hand-edited) take effect
            # starting the next session - never mid-trade, and never by
            # mutating a running engine's parameters underneath it.
            try:
                ctx.config = load_config(ctx.config_path)
            except Exception:
                logger.exception(
                    "Failed to reload %s - keeping the previously loaded config", ctx.config_path
                )

            start = next_session_start(ctx.config)
            logger.info("Next session starts at %s", start)
            await sleep_until(start)

            trading_date = now_ny().date()
            try:
                # Constructing the bar source (e.g. no Tradovate credentials
                # configured yet) can fail just as easily as the session
                # itself - both need to land in the same retry-not-crash
                # path, or a misconfigured .env would crash-loop the whole
                # process instead of just logging and waiting.
                ctx.bar_source = bar_source_factory()
                await run_trading_session(ctx, trading_date)
            except Exception:
                logger.exception("Session for %s crashed - will retry next scheduled session", trading_date)
                if ctx.telegram:
                    await ctx.telegram.send_text(
                        f"\U0001F6A8 Session for {trading_date} crashed - see logs."
                    )
            # Ensure we don't immediately re-enter the same session window
            # after a same-day restart/crash.
            await asyncio.sleep(5)
    finally:
        await ctx.broker.close()
        if ctx._owns_market_data and ctx.market_data is not None:
            await ctx.market_data.close()
        if ctx.telegram:
            await ctx.telegram.stop()


def _default_bar_source_factory(ctx: AppContext) -> BarSource:
    if ctx.market_data is not None:
        return PollingTradovateBarSource(ctx.market_data.get_recent_bars)
    raise RuntimeError(
        "No live bar source available: no Tradovate credentials are "
        "configured (see .env's TRADOVATE_USERNAME/TRADOVATE_PASSWORD). "
        "For now, run scripts/replay_backtest.py against historical bars "
        "to exercise the full pipeline without live credentials."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Range Breakout Strategy trading bot")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    ctx = build_app_context(args.config)
    asyncio.run(run_forever(ctx, lambda: _default_bar_source_factory(ctx)))


if __name__ == "__main__":
    main()
