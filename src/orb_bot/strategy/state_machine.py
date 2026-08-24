"""The core Range Breakout strategy engine.

This module is intentionally free of any I/O (no network, no disk, no
clock reads via `datetime.now()`): it is a pure state machine that consumes
timestamped events (bars, fills, order-status updates, equity updates, and
wall-clock ticks) and produces a list of `Action`s for a broker layer to
execute. That separation is what makes it unit-testable against historical
data independent of the live Tradovate connection.

One `OrbEngine` instance manages exactly one trading day. The
scheduler/app layer is responsible for constructing a fresh engine each
session (or resuming one from persisted `DayState` after a restart) and
feeding it events in chronological order.
"""
from __future__ import annotations

import datetime as dt
from collections import deque

from orb_bot.config import AppConfig
from orb_bot.strategy.indicators import BarAggregator, RollingSMA
from orb_bot.strategy.models import (
    Action,
    Alert,
    Bar,
    CancelOrder,
    ClosePositionMarket,
    DayState,
    EquityUpdate,
    Fill,
    ModifyOrder,
    Notify,
    OrderStatusUpdate,
    PlaceStopOrder,
    Position,
    RunnerMode,
    RunnerPhase,
    Side,
)
from orb_bot.strategy.sizing import InvalidSizingInput, compute_position_size
from orb_bot.timeutils import combine_ny


class OrbEngine:
    def __init__(
        self,
        config: AppConfig,
        trading_date: dt.date,
        equity: float,
        fomc_dates: set[dt.date] | None = None,
        day_state: DayState | None = None,
    ) -> None:
        self.config = config
        self.day = day_state if day_state is not None else DayState(date=trading_date)
        self.equity = equity

        session = config.session
        self._or_start = combine_ny(trading_date, session.opening_range_start)
        self._or_end = self._or_start + dt.timedelta(minutes=session.opening_range_minutes)
        self._entry_cutoff = combine_ny(trading_date, session.entry_cutoff)
        self._hard_close = combine_ny(trading_date, session.hard_close)
        self._whipsaw_time = combine_ny(trading_date, config.exits.whipsaw_guard.time)
        self._is_fomc_day = config.fomc.enabled and trading_date in (fomc_dates or set())
        self._fomc_flatten_time = (
            combine_ny(trading_date, config.fomc.flatten_before) if self._is_fomc_day else None
        )

        self._agg_old = BarAggregator(config.exits.runner.old.sma_timeframe_minutes)
        self._sma_old = RollingSMA(config.exits.runner.old.sma_period)
        self._agg_new = BarAggregator(config.exits.runner.new.sma_timeframe_minutes)
        self._sma_new = RollingSMA(config.exits.runner.new.sma_period)
        history_len = max(20, config.exits.runner.new.candle_lookback + 5)
        self._new_candle_history: deque[Bar] = deque(maxlen=history_len)

        self._last_price: float | None = None
        self._entries_canceled = False

    # -- warm start (mid-session restart recovery) --------------------------

    def warm_up_indicators(self, bars: list[Bar]) -> None:
        """Replay already-seen bars through the aggregators/SMAs only - no
        actions are produced and DayState is not touched. Used after a
        restart, once DayState itself has been restored from persistence,
        to rebuild indicator continuity without re-issuing orders."""
        for bar in bars:
            self._feed_aggregators(bar)
            self._last_price = bar.close

    def _feed_aggregators(self, bar: Bar) -> tuple[Bar | None, Bar | None]:
        completed_old = self._agg_old.add_bar(bar)
        if completed_old is not None:
            self._sma_old.update(completed_old)
        completed_new = self._agg_new.add_bar(bar)
        if completed_new is not None:
            self._sma_new.update(completed_new)
            self._new_candle_history.append(completed_new)
        return completed_old, completed_new

    # -- event handlers -------------------------------------------------

    def on_equity_update(self, update: EquityUpdate) -> None:
        self.equity = update.equity

    def on_bar(self, bar: Bar) -> list[Action]:
        actions: list[Action] = []
        self._last_price = bar.close

        if not self.day.or_finalized:
            if self._or_start <= bar.timestamp < self._or_end:
                self.day.opening_range.update(bar)
            if bar.timestamp >= self._or_end:
                actions.extend(self._finalize_opening_range())

        completed_old, completed_new = self._feed_aggregators(bar)

        if self.day.position is not None:
            actions.extend(self._manage_position_on_bar(bar, completed_old, completed_new))

        actions.extend(self._check_time_based_actions(bar.timestamp))
        return actions

    def on_wall_clock(self, now: dt.datetime) -> list[Action]:
        """Called by the scheduler on a plain timer (independent of market
        data), so hard-close/cutoff/whipsaw/FOMC guarantees never depend on
        a bar arriving at exactly the right moment (holidays, early closes,
        feed gaps)."""
        return self._check_time_based_actions(now)

    def on_fill(self, fill: Fill) -> list[Action]:
        actions: list[Action] = []

        if fill.order_ref in (self.day.long_entry_ref, self.day.short_entry_ref):
            if self.day.position is not None:
                # The exact race condition called out in the spec: the
                # opposite entry order filled after we already entered from
                # the other side (same-tick/near-simultaneous race on
                # symmetric OR levels). Never hold the accidental opposing
                # position - flatten it immediately and redundantly cancel.
                actions.append(
                    Alert(
                        "critical",
                        f"Entry-order race condition: {fill.order_ref} filled "
                        f"after a position was already open from the "
                        f"opposite side. Flattening the erroneous fill "
                        f"immediately.",
                    )
                )
                actions.append(
                    ClosePositionMarket(fill.qty, reason="entry_race_condition_cleanup")
                )
                actions.append(
                    CancelOrder(fill.order_ref, reason="post_fill_race_redundant_cancel")
                )
                actions.extend(self._check_daily_loss_limit())
                return actions
            actions.extend(self._handle_entry_fill(fill))
            return actions

        if self.day.position is not None and fill.order_ref == self.day.position.stop_order_ref:
            actions.extend(self._handle_stop_fill(fill))
            return actions

        actions.append(
            Alert(
                "warning",
                f"Unrecognized fill on order_ref={fill.order_ref} "
                f"qty={fill.qty} @ {fill.price} - not matched to any "
                f"tracked order.",
            )
        )
        return actions

    def manual_flatten(self, reason: str = "manual_flatten") -> list[Action]:
        """Public entry point for an operator-initiated emergency flatten
        (e.g. the Telegram /flatten command). Also cancels any still-
        pending entry orders and halts new entries for the rest of the day."""
        actions: list[Action] = []
        if self.day.position is not None:
            p = self.day.position
            ref_price = self._last_price if self._last_price is not None else p.entry_price
            actions.extend(self._exit_position_market(p, ref_price, p.runner_qty, reason))
        if self.day.entry_orders_placed and not self._entries_canceled:
            self._entries_canceled = True
            actions.append(CancelOrder(self.day.long_entry_ref, reason=reason))
            actions.append(CancelOrder(self.day.short_entry_ref, reason=reason))
        self.day.trade_done_for_day = True
        return actions

    def on_order_status(self, update: OrderStatusUpdate) -> list[Action]:
        actions: list[Action] = []
        entry_refs = (self.day.long_entry_ref, self.day.short_entry_ref)
        if update.order_ref in entry_refs and update.status == "Working":
            should_be_gone = self.day.position is not None or self.day.trade_done_for_day
            if should_be_gone:
                actions.append(
                    CancelOrder(update.order_ref, reason="reconciliation_redundant_cancel")
                )
        return actions

    # -- opening range / entry -------------------------------------------

    def _finalize_opening_range(self) -> list[Action]:
        actions: list[Action] = []
        orr = self.day.opening_range
        self.day.or_finalized = True

        if orr.high is None or orr.low is None:
            self.day.trade_done_for_day = True
            actions.append(
                Alert(
                    "critical",
                    "No bars observed during the opening-range window "
                    "(data gap?) - skipping trade for today.",
                )
            )
            return actions

        width = orr.width
        assert width is not None
        f = self.config.opening_range_filter
        passed = f.min_width_points <= width <= f.max_width_points
        self.day.filter_passed = passed

        if not passed:
            self.day.trade_done_for_day = True
            actions.append(
                Notify(
                    "no_trade",
                    {
                        "date": self.day.date.isoformat(),
                        "or_high": orr.high,
                        "or_low": orr.low,
                        "or_width": width,
                        "reason": "or_width_filter",
                    },
                )
            )
            return actions

        buffer = self.config.entry.buffer_points
        stop_distance = width + 2 * buffer
        self.day.stop_distance_points = stop_distance

        try:
            sizing = compute_position_size(
                equity=self.equity,
                sizing_mode=self.config.risk.sizing_mode,
                risk_pct_of_equity=self.config.risk.risk_pct_of_equity,
                fixed_contracts=self.config.risk.fixed_contracts,
                stop_distance_points=stop_distance,
                point_value=self.config.instrument.point_value,
                min_contracts=self.config.risk.min_contracts,
                max_contracts_sanity_cap=self.config.risk.max_contracts_sanity_cap,
            )
        except InvalidSizingInput as exc:
            self.day.trade_done_for_day = True
            actions.append(Alert("critical", str(exc)))
            return actions

        self.day.entry_qty = sizing.qty
        self.day.entry_orders_placed = True

        buy_price = orr.high + buffer
        sell_price = orr.low - buffer
        actions.append(
            PlaceStopOrder(self.day.long_entry_ref, Side.LONG, buy_price, sizing.qty, "or_breakout_long")
        )
        actions.append(
            PlaceStopOrder(self.day.short_entry_ref, Side.SHORT, sell_price, sizing.qty, "or_breakout_short")
        )
        actions.append(
            Notify(
                "entry_orders_placed",
                {
                    "or_high": orr.high,
                    "or_low": orr.low,
                    "or_width": width,
                    "buy_stop": buy_price,
                    "sell_stop": sell_price,
                    "qty": sizing.qty,
                    "risk_dollars": sizing.risk_dollars,
                    "stop_distance_points": stop_distance,
                },
            )
        )
        return actions

    def _handle_entry_fill(self, fill: Fill) -> list[Action]:
        actions: list[Action] = []
        side = Side.LONG if fill.order_ref == self.day.long_entry_ref else Side.SHORT
        opposite_ref = (
            self.day.short_entry_ref if side is Side.LONG else self.day.long_entry_ref
        )

        # Explicit + redundant cancel of the opposite pending order. Never
        # rely solely on an OCO/bracket relationship.
        actions.append(CancelOrder(opposite_ref, reason="opposite_entry_after_fill"))

        buffer = self.config.entry.buffer_points
        orr = self.day.opening_range
        assert orr.high is not None and orr.low is not None
        initial_stop = (orr.low - buffer) if side is Side.LONG else (orr.high + buffer)
        r_unit = abs(fill.price - initial_stop)
        qty = self.day.entry_qty

        position = Position(
            side=side,
            entry_price=fill.price,
            entry_time=fill.timestamp,
            qty=qty,
            initial_stop=initial_stop,
            r_unit_points=r_unit,
            current_stop=initial_stop,
            runner_qty=qty,
            runner_mode=RunnerMode(self.config.exits.runner.mode),
            runner_phase=(
                RunnerPhase.A if self.config.exits.runner.mode == "new" else RunnerPhase.NONE
            ),
        )
        self.day.position = position
        self.day.trade_done_for_day = True  # one trade per day - no re-entry after this

        protective_side = Side.SHORT if side is Side.LONG else Side.LONG
        actions.append(
            PlaceStopOrder(position.stop_order_ref, protective_side, initial_stop, qty, "protective_stop_loss")
        )
        actions.append(
            Notify(
                "entry",
                {
                    "side": side.value,
                    "price": fill.price,
                    "qty": qty,
                    "stop": initial_stop,
                    "r_unit_points": r_unit,
                    "time": fill.timestamp.isoformat(),
                },
            )
        )
        return actions

    # -- position management ---------------------------------------------

    def _manage_position_on_bar(
        self, bar: Bar, completed_old: Bar | None, completed_new: Bar | None
    ) -> list[Action]:
        actions: list[Action] = []
        pos = self.day.position
        if pos is None:
            return actions

        price = bar.close
        r = pos.r_multiple(price)

        be_r = self.config.exits.breakeven_r_multiple
        if not pos.breakeven_moved and r >= be_r:
            pos.breakeven_moved = True
            actions.extend(self._ratchet_stop(pos, pos.entry_price, "breakeven"))

        if not pos.tp1_done and r >= self.config.exits.tp1_r_multiple:
            actions.extend(self._handle_tp1(pos, price))

        if self.day.position is None:
            return actions  # TP1 rounding closed the entire position

        if pos.tp1_done:
            if pos.runner_mode is RunnerMode.OLD:
                actions.extend(self._manage_runner_old(pos, bar, completed_old))
            else:
                actions.extend(self._manage_runner_new(pos, bar, completed_new))

        return actions

    def _handle_tp1(self, pos: Position, price: float) -> list[Action]:
        actions: list[Action] = []
        pos.tp1_done = True
        close_pct = self.config.exits.tp1_close_pct
        close_qty = int(pos.qty * close_pct)  # floor

        if close_qty > 0:
            pnl = self._pnl_usd(pos, price, close_qty)
            self.day.realized_pnl_usd += pnl
            pos.runner_qty = pos.qty - close_qty
            actions.append(ClosePositionMarket(close_qty, reason="tp1"))
            if pos.runner_qty > 0:
                actions.append(
                    ModifyOrder(pos.stop_order_ref, new_qty=pos.runner_qty, reason="tp1_size_down")
                )
            else:
                actions.append(CancelOrder(pos.stop_order_ref, reason="tp1_closed_full_position"))
        else:
            # e.g. a 1-lot at the default 90%: floors to 0 contracts closed.
            # No partial fires; the full size becomes the runner. Logged
            # loudly rather than silently absorbed.
            pos.runner_qty = pos.qty
            actions.append(
                Alert(
                    "warning",
                    f"TP1 reached but {close_pct:.0%} of {pos.qty} contract(s) "
                    f"floors to 0; skipping partial close, full size becomes "
                    f"the runner.",
                )
            )

        actions.append(
            Notify(
                "tp1",
                {
                    "side": pos.side.value,
                    "price": price,
                    "closed_qty": close_qty,
                    "runner_qty": pos.runner_qty,
                    "r_multiple": pos.r_multiple(price),
                },
            )
        )

        if pos.runner_qty <= 0:
            self.day.position = None

        actions.extend(self._check_daily_loss_limit())
        return actions

    def _manage_runner_old(self, pos: Position, bar: Bar, completed_old: Bar | None) -> list[Action]:
        cfg = self.config.exits.runner.old
        r = pos.r_multiple(bar.close)

        if r >= cfg.cap_r_multiple:
            return self._exit_position_market(pos, bar.close, pos.runner_qty, "runner_cap_r")

        actions: list[Action] = []
        sma = self._sma_old.value
        if completed_old is not None and sma is not None:
            actions.extend(self._ratchet_stop(pos, sma, "runner_sma_trail"))
            closed_through = (
                completed_old.close < sma if pos.side is Side.LONG else completed_old.close > sma
            )
            if closed_through:
                actions.extend(
                    self._exit_position_market(pos, bar.close, pos.runner_qty, "runner_sma_close_through")
                )
        return actions

    def _manage_runner_new(self, pos: Position, bar: Bar, completed_new: Bar | None) -> list[Action]:
        actions: list[Action] = []
        sma = self._sma_new.value
        if completed_new is None or sma is None:
            return actions

        tp1_price = self._tp1_price(pos)

        if pos.runner_phase is RunnerPhase.A:
            candidate = max(pos.entry_price, sma) if pos.side is Side.LONG else min(pos.entry_price, sma)
            # Phase A explicitly is NOT ratchet-only (it can move down with
            # the SMA on a pullback) except for the breakeven floor already
            # enforced by max()/min() above, so we set the stop directly.
            if candidate != pos.current_stop:
                pos.current_stop = candidate
                actions.append(
                    ModifyOrder(pos.stop_order_ref, new_stop_price=candidate, reason="runner_phase_a_sma_follow")
                )

            crossed = sma >= tp1_price if pos.side is Side.LONG else sma <= tp1_price
            if crossed:
                pos.runner_phase = RunnerPhase.B
                actions.append(Notify("runner_phase_change", {"phase": "B"}))
                actions.extend(self._manage_runner_new_phase_b(pos))
            return actions

        return self._manage_runner_new_phase_b(pos)

    def _manage_runner_new_phase_b(self, pos: Position) -> list[Action]:
        lookback = self.config.exits.runner.new.candle_lookback
        history = list(self._new_candle_history)
        if len(history) < lookback:
            return []
        reference = history[-lookback]
        candidate = reference.low if pos.side is Side.LONG else reference.high
        # Phase B trails the most recently completed candle's low/high; we
        # ratchet it (never loosen) as a defensive default for a resting
        # protective stop, consistent with the old mode's ratchet rule.
        return self._ratchet_stop(pos, candidate, "runner_phase_b_candle_trail")

    def _tp1_price(self, pos: Position) -> float:
        sign = 1 if pos.side is Side.LONG else -1
        return pos.entry_price + sign * self.config.exits.tp1_r_multiple * pos.r_unit_points

    def _ratchet_stop(self, pos: Position, candidate: float, reason: str) -> list[Action]:
        improved = candidate > pos.current_stop if pos.side is Side.LONG else candidate < pos.current_stop
        if not improved:
            return []
        pos.current_stop = candidate
        return [ModifyOrder(pos.stop_order_ref, new_stop_price=candidate, reason=reason)]

    def _handle_stop_fill(self, fill: Fill) -> list[Action]:
        actions: list[Action] = []
        pos = self.day.position
        assert pos is not None
        pnl = self._pnl_usd(pos, fill.price, fill.qty)
        self.day.realized_pnl_usd += pnl
        actions.append(
            Notify(
                "exit",
                {
                    "side": pos.side.value,
                    "exit_price": fill.price,
                    "qty": fill.qty,
                    "reason": "stop",
                    "r_multiple": pos.r_multiple(fill.price),
                    "pnl_usd": pnl,
                    "time": fill.timestamp.isoformat(),
                },
            )
        )
        self.day.position = None
        actions.extend(self._check_daily_loss_limit())
        return actions

    def _exit_position_market(self, pos: Position, price: float, qty: int, reason: str) -> list[Action]:
        actions: list[Action] = []
        pnl = self._pnl_usd(pos, price, qty)
        self.day.realized_pnl_usd += pnl
        actions.append(ClosePositionMarket(qty, reason=reason))
        actions.append(CancelOrder(pos.stop_order_ref, reason=f"{reason}_redundant_cancel"))
        actions.append(
            Notify(
                "exit",
                {
                    "side": pos.side.value,
                    "exit_price": price,
                    "qty": qty,
                    "reason": reason,
                    "r_multiple": pos.r_multiple(price),
                    "pnl_usd": pnl,
                },
            )
        )
        self.day.position = None
        actions.extend(self._check_daily_loss_limit())
        return actions

    def _pnl_usd(self, pos: Position, exit_price: float, qty: int) -> float:
        sign = 1 if pos.side is Side.LONG else -1
        pts = (exit_price - pos.entry_price) * sign
        return pts * qty * self.config.instrument.point_value

    # -- time-based checks (cutoff / hard close / whipsaw / FOMC / safety) --

    def _check_time_based_actions(self, now: dt.datetime) -> list[Action]:
        actions: list[Action] = []

        if (
            now >= self._entry_cutoff
            and self.day.entry_orders_placed
            and self.day.position is None
            and not self._entries_canceled
            and not self.day.trade_done_for_day
        ):
            self._entries_canceled = True
            self.day.trade_done_for_day = True
            actions.append(CancelOrder(self.day.long_entry_ref, reason="entry_cutoff"))
            actions.append(CancelOrder(self.day.short_entry_ref, reason="entry_cutoff"))
            actions.append(Notify("no_trade", {"reason": "entry_cutoff_no_fill"}))

        pos = self.day.position
        if pos is not None and now >= self._whipsaw_time and not pos.whipsaw_guard_applied:
            pos.whipsaw_guard_applied = True
            if self._last_price is not None:
                r = pos.r_multiple(self._last_price)
                if 0 < r < self.config.exits.breakeven_r_multiple:
                    actions.extend(self._ratchet_stop(pos, pos.entry_price, "whipsaw_guard"))

        if (
            self._fomc_flatten_time is not None
            and self.day.position is not None
            and now >= self._fomc_flatten_time
        ):
            p = self.day.position
            ref_price = self._last_price if self._last_price is not None else p.entry_price
            actions.extend(self._exit_position_market(p, ref_price, p.runner_qty, "fomc_flatten"))

        if now >= self._hard_close:
            if self.day.position is not None:
                p = self.day.position
                ref_price = self._last_price if self._last_price is not None else p.entry_price
                actions.extend(self._exit_position_market(p, ref_price, p.runner_qty, "hard_close"))
            if self.day.entry_orders_placed and not self._entries_canceled:
                self._entries_canceled = True
                actions.append(CancelOrder(self.day.long_entry_ref, reason="hard_close"))
                actions.append(CancelOrder(self.day.short_entry_ref, reason="hard_close"))
            self.day.trade_done_for_day = True

        return actions

    def _check_daily_loss_limit(self) -> list[Action]:
        actions: list[Action] = []
        if self.day.halted:
            return actions

        limit = self.config.safety.daily_loss_limit_usd
        pct_limit = self.config.safety.daily_loss_limit_pct_of_equity
        if pct_limit:
            limit = min(limit, self.equity * pct_limit)

        if self.day.realized_pnl_usd <= -abs(limit):
            self.day.halted = True
            self.day.trade_done_for_day = True
            actions.append(
                Alert(
                    "critical",
                    f"Daily loss limit hit: realized P&L "
                    f"{self.day.realized_pnl_usd:.2f} <= -{limit:.2f}. "
                    f"Halting trading for the day.",
                )
            )
            if self.day.position is not None:
                p = self.day.position
                ref_price = self._last_price if self._last_price is not None else p.entry_price
                actions.extend(self._exit_position_market(p, ref_price, p.runner_qty, "daily_loss_limit"))
            if self.day.entry_orders_placed and not self._entries_canceled:
                self._entries_canceled = True
                actions.append(CancelOrder(self.day.long_entry_ref, reason="daily_loss_limit"))
                actions.append(CancelOrder(self.day.short_entry_ref, reason="daily_loss_limit"))
            actions.append(Notify("halt", {"realized_pnl_usd": self.day.realized_pnl_usd}))

        return actions
