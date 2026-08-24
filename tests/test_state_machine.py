from __future__ import annotations

from conftest import TRADING_DATE, bar, make_config, ts

from orb_bot.strategy.models import (
    Alert,
    CancelOrder,
    ClosePositionMarket,
    Fill,
    ModifyOrder,
    Notify,
    PlaceStopOrder,
    Side,
)
from orb_bot.strategy.state_machine import OrbEngine

EQUITY = 50_000.0


def feed_or_bars(engine: OrbEngine, or_low: float, or_high: float) -> None:
    """Feed 15 one-minute OR bars (9:30-9:44) oscillating between or_low and
    or_high, then the 9:45 bar that triggers finalization."""
    for i in range(15):
        mm = 30 + i
        price = or_high if i % 2 == 0 else or_low
        engine.on_bar(bar(9, mm, price, or_high, or_low, price))


def place_entries(engine: OrbEngine, or_low: float = 5000.0, or_high: float = 5020.0):
    feed_or_bars(engine, or_low, or_high)
    actions = engine.on_bar(bar(9, 45, or_high, or_high, or_low, or_high))
    return actions


def fill_long(engine: OrbEngine, price: float, qty: int, when=None):
    when = when or ts(9, 46)
    return engine.on_fill(Fill(order_ref="entry_long", price=price, qty=qty, timestamp=when))


class TestOpeningRangeFilter:
    def test_width_too_narrow_skips_trade(self, config):
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        actions = place_entries(engine, or_low=5000.0, or_high=5005.0)  # width=5 < min 10

        assert not any(isinstance(a, PlaceStopOrder) for a in actions)
        no_trade = [a for a in actions if isinstance(a, Notify) and a.event == "no_trade"]
        assert len(no_trade) == 1
        assert no_trade[0].payload["reason"] == "or_width_filter"
        assert engine.day.trade_done_for_day is True
        assert engine.day.filter_passed is False

    def test_width_too_wide_skips_trade(self, config):
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        actions = place_entries(engine, or_low=5000.0, or_high=5040.0)  # width=40 > max 32

        assert not any(isinstance(a, PlaceStopOrder) for a in actions)
        assert engine.day.trade_done_for_day is True

    def test_width_in_range_places_symmetric_stop_orders(self, config):
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        actions = place_entries(engine, or_low=5000.0, or_high=5020.0)  # width=20, in [10,32]

        places = [a for a in actions if isinstance(a, PlaceStopOrder)]
        assert len(places) == 2
        long_order = next(a for a in places if a.side is Side.LONG)
        short_order = next(a for a in places if a.side is Side.SHORT)

        assert long_order.stop_price == 5020.5  # OR high + 0.5 buffer
        assert short_order.stop_price == 4999.5  # OR low - 0.5 buffer
        assert long_order.qty == short_order.qty == engine.day.entry_qty
        assert engine.day.entry_qty > 0

        # risk_dollars = 50000 * 1% = 500; stop_distance = 20 + 2*0.5 = 21pts
        # $/pt (MES) = 5 -> risk/contract = 105 -> floor(500/105) = 4
        assert engine.day.entry_qty == 4

    def test_fixed_contracts_override_ignores_risk_pct(self, config):
        cfg = make_config(risk={"sizing_mode": "fixed", "fixed_contracts": 7})
        engine = OrbEngine(cfg, TRADING_DATE, EQUITY)
        actions = place_entries(engine, or_low=5000.0, or_high=5020.0)

        places = [a for a in actions if isinstance(a, PlaceStopOrder)]
        assert all(a.qty == 7 for a in places)
        assert engine.day.entry_qty == 7


class TestEntryFillAndRaceCondition:
    def test_long_fill_cancels_opposite_and_places_protective_stop(self, config):
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        place_entries(engine)

        actions = fill_long(engine, price=5020.5, qty=4)

        cancels = [a for a in actions if isinstance(a, CancelOrder)]
        assert any(c.order_ref == "entry_short" for c in cancels)

        stops = [a for a in actions if isinstance(a, PlaceStopOrder)]
        assert len(stops) == 1
        assert stops[0].side is Side.SHORT
        assert stops[0].stop_price == 4999.5
        assert stops[0].qty == 4

        pos = engine.day.position
        assert pos is not None
        assert pos.side is Side.LONG
        assert pos.entry_price == 5020.5
        assert pos.initial_stop == 4999.5
        assert pos.r_unit_points == 21.0
        assert engine.day.trade_done_for_day is True

    def test_opposite_fill_after_position_open_is_flattened_not_held(self, config):
        """The exact race condition from the spec: both entry orders'
        prices sit off symmetric OR levels, so a same-tick/near-simultaneous
        fill of the "cancelled" opposite order must never be silently
        combined into/against the open position."""
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        place_entries(engine)
        fill_long(engine, price=5020.5, qty=4)

        actions = engine.on_fill(
            Fill(order_ref="entry_short", price=4999.5, qty=4, timestamp=ts(9, 46))
        )

        assert any(isinstance(a, Alert) and a.level == "critical" for a in actions)
        closes = [a for a in actions if isinstance(a, ClosePositionMarket)]
        assert len(closes) == 1 and closes[0].qty == 4
        assert closes[0].reason == "entry_race_condition_cleanup"
        cancels = [a for a in actions if isinstance(a, CancelOrder) and a.order_ref == "entry_short"]
        assert len(cancels) == 1

        # The original long position is untouched by the race cleanup.
        assert engine.day.position is not None
        assert engine.day.position.side is Side.LONG


class TestExitLogic:
    def _entered_engine(self, config) -> OrbEngine:
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        place_entries(engine)
        fill_long(engine, price=5020.5, qty=4)
        return engine

    def test_breakeven_moves_stop_at_1r(self, config):
        engine = self._entered_engine(config)
        pos = engine.day.position
        assert pos.breakeven_moved is False

        # entry 5020.5, r_unit 21 -> +1R = 5041.5
        actions = engine.on_bar(bar(9, 50, 5041.5, 5042.0, 5041.0, 5042.0))

        moves = [a for a in actions if isinstance(a, ModifyOrder) and a.reason == "breakeven"]
        assert len(moves) == 1
        assert moves[0].new_stop_price == 5020.5
        assert engine.day.position.breakeven_moved is True
        assert engine.day.position.current_stop == 5020.5

    def test_tp1_partial_close_and_stop_resize(self, config):
        engine = self._entered_engine(config)

        # +1.5R = 5020.5 + 31.5 = 5052.0
        actions = engine.on_bar(bar(9, 55, 5053.0, 5053.0, 5052.5, 5053.0))

        closes = [a for a in actions if isinstance(a, ClosePositionMarket)]
        assert len(closes) == 1
        assert closes[0].qty == 3  # floor(4 * 0.90)
        assert closes[0].reason == "tp1"

        resizes = [a for a in actions if isinstance(a, ModifyOrder) and a.reason == "tp1_size_down"]
        assert len(resizes) == 1
        assert resizes[0].new_qty == 1

        assert engine.day.position.tp1_done is True
        assert engine.day.position.runner_qty == 1

    def test_old_runner_hits_cap_r_exit(self, config):
        engine = self._entered_engine(config)
        engine.on_bar(bar(9, 55, 5053.0, 5053.0, 5052.5, 5053.0))  # TP1

        # +2R cap = 5020.5 + 42 = 5062.5
        actions = engine.on_bar(bar(10, 0, 5063.0, 5063.0, 5062.8, 5063.0))

        closes = [a for a in actions if isinstance(a, ClosePositionMarket)]
        assert len(closes) == 1
        assert closes[0].qty == 1
        assert closes[0].reason == "runner_cap_r"
        assert engine.day.position is None

    def test_old_runner_ratchets_and_exits_on_close_through_sma(self, config):
        cfg = make_config(
            exits={
                "breakeven_r_multiple": 1.0,
                "tp1_r_multiple": 1.5,
                "tp1_close_pct": 0.90,
                "runner": {
                    "mode": "old",
                    "old": {"sma_period": 3, "sma_timeframe_minutes": 1, "cap_r_multiple": 2.0},
                    "new": {"sma_period": 9, "sma_timeframe_minutes": 15, "candle_lookback": 1},
                },
                "whipsaw_guard": {"time": "15:00"},
            }
        )
        engine = OrbEngine(cfg, TRADING_DATE, EQUITY)
        place_entries(engine)
        fill_long(engine, price=5020.5, qty=4)
        engine.on_bar(bar(9, 55, 5053.0, 5053.0, 5052.5, 5053.0))  # TP1, runner_qty=1

        # Uptrend: the SMA-trail stop should ratchet up and never retreat.
        # (Bar aggregation always lags by one bar - a bucket only "closes"
        # once the next bar proves it's over - so we read expectations off
        # the engine's own running values instead of hand-computed sums.)
        stops_seen: list[float] = []
        for i, c in enumerate([5055.0, 5057.0, 5059.0, 5061.0]):
            actions = engine.on_bar(bar(10, i, c, c, c - 0.5, c))
            stops_seen += [
                a.new_stop_price
                for a in actions
                if isinstance(a, ModifyOrder) and a.reason == "runner_sma_trail"
            ]

        assert stops_seen, "expected the runner stop to ratchet up during the uptrend"
        assert stops_seen == sorted(stops_seen)  # ratchet-only: never decreases
        peak_stop = engine.day.position.current_stop

        # Sharp reversal: the stop must never retreat below its peak, and a
        # close-through-the-SMA should eventually force the runner exit.
        closed_reason = None
        for i, c in enumerate([5040.0, 5010.0, 4980.0, 4950.0]):
            actions = engine.on_bar(bar(10, 10 + i, c, c, c - 0.5, c))
            trail_moves = [
                a for a in actions if isinstance(a, ModifyOrder) and a.reason == "runner_sma_trail"
            ]
            assert all(m.new_stop_price >= peak_stop for m in trail_moves)
            exits = [a for a in actions if isinstance(a, ClosePositionMarket)]
            if exits:
                closed_reason = exits[0].reason
                break

        assert closed_reason == "runner_sma_close_through"
        assert engine.day.position is None

    def test_new_runner_phase_a_follows_sma_then_switches_to_phase_b(self, config):
        cfg = make_config(
            exits={
                "breakeven_r_multiple": 1.0,
                "tp1_r_multiple": 1.5,
                "tp1_close_pct": 0.90,
                "runner": {
                    "mode": "new",
                    "old": {"sma_period": 9, "sma_timeframe_minutes": 5, "cap_r_multiple": 2.0},
                    "new": {"sma_period": 3, "sma_timeframe_minutes": 1, "candle_lookback": 1},
                },
                "whipsaw_guard": {"time": "15:00"},
            }
        )
        engine = OrbEngine(cfg, TRADING_DATE, EQUITY)
        place_entries(engine)
        fill_long(engine, price=5020.5, qty=4)  # r_unit=21, tp1_price=5052.0

        engine.on_bar(bar(9, 55, 5045.0, 5045.0, 5044.0, 5045.0))  # breakeven, sma warm-up
        engine.on_bar(bar(9, 56, 5048.0, 5048.0, 5047.0, 5048.0))

        actions3 = engine.on_bar(bar(9, 57, 5053.0, 5053.0, 5052.0, 5053.0))  # TP1 + phase A
        tp1 = [a for a in actions3 if isinstance(a, ClosePositionMarket)]
        assert len(tp1) == 1 and tp1[0].reason == "tp1"
        assert engine.day.position.runner_phase.value == "phase_a"
        phase_a_moves = [
            a for a in actions3 if isinstance(a, ModifyOrder) and a.reason == "runner_phase_a_sma_follow"
        ]
        # Phase A is floored at breakeven (5020.5) and follows the SMA up.
        assert len(phase_a_moves) == 1
        assert phase_a_moves[0].new_stop_price >= 5020.5

        # Keep pushing price up until the SMA itself crosses the TP1 price
        # level (5052.0), which is the documented Phase A -> Phase B trigger.
        phase_b_entered = False
        for i, c in enumerate([5060.0, 5070.0, 5080.0, 5090.0]):
            actions = engine.on_bar(bar(9, 58 + i, c, c, c - 3.0, c))
            if any(isinstance(a, Notify) and a.event == "runner_phase_change" for a in actions):
                phase_b_entered = True
                phase_b_moves = [
                    a for a in actions if isinstance(a, ModifyOrder) and a.reason == "runner_phase_b_candle_trail"
                ]
                assert len(phase_b_moves) == 1
                break

        assert phase_b_entered
        assert engine.day.position.runner_phase.value == "phase_b"

        # Phase B keeps trailing forward with price and never retreats.
        stop_before = engine.day.position.current_stop
        for i, c in enumerate([5100.0, 5110.0]):
            engine.on_bar(bar(10, i, c, c, c - 3.0, c))
        assert engine.day.position.current_stop >= stop_before


class TestTimeBasedGuards:
    def test_entry_cutoff_cancels_unfilled_orders(self, config):
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        place_entries(engine)

        actions = engine.on_wall_clock(ts(10, 30))

        cancels = {a.order_ref for a in actions if isinstance(a, CancelOrder)}
        assert cancels == {"entry_long", "entry_short"}
        no_trade = [a for a in actions if isinstance(a, Notify) and a.event == "no_trade"]
        assert no_trade[0].payload["reason"] == "entry_cutoff_no_fill"
        assert engine.day.trade_done_for_day is True

    def test_hard_close_flattens_regardless_of_cutoff(self, config):
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        place_entries(engine)
        fill_long(engine, price=5020.5, qty=4)

        # Well past the 10:30 entry cutoff - an OPEN POSITION is explicitly
        # exempt from that cutoff and must still be flattened at hard close.
        actions = engine.on_bar(bar(15, 55, 5030.0, 5030.5, 5029.5, 5030.0))

        closes = [a for a in actions if isinstance(a, ClosePositionMarket)]
        assert len(closes) == 1
        assert closes[0].reason == "hard_close"
        assert closes[0].qty == 4
        assert engine.day.position is None
        assert engine.day.trade_done_for_day is True

    def test_whipsaw_guard_forces_breakeven_if_not_yet_at_1r(self, config):
        engine = OrbEngine(config, TRADING_DATE, EQUITY)
        place_entries(engine)
        fill_long(engine, price=5020.5, qty=4)

        # r = (5030.5 - 5020.5) / 21 = 0.476 -> profitable but < 1R
        actions = engine.on_bar(bar(15, 0, 5030.5, 5031.0, 5030.0, 5030.5))

        moves = [a for a in actions if isinstance(a, ModifyOrder) and a.reason == "whipsaw_guard"]
        assert len(moves) == 1
        assert moves[0].new_stop_price == 5020.5
        assert engine.day.position.current_stop == 5020.5


class TestDailyLossLimit:
    def test_stop_loss_beyond_limit_halts_trading(self, config):
        cfg = make_config(safety={"daily_loss_limit_usd": 50.0, "daily_loss_limit_pct_of_equity": None, "max_orders_per_day": 1})
        engine = OrbEngine(cfg, TRADING_DATE, EQUITY)
        place_entries(engine)
        fill_long(engine, price=5020.5, qty=4)

        # Stopped out at the initial (pre-breakeven) stop: -21pts * 4 * $5 = -$420
        actions = engine.on_fill(
            Fill(order_ref="stop_loss", price=4999.5, qty=4, timestamp=ts(9, 47))
        )

        assert engine.day.halted is True
        assert any(isinstance(a, Alert) and a.level == "critical" for a in actions)
        halt_notify = [a for a in actions if isinstance(a, Notify) and a.event == "halt"]
        assert len(halt_notify) == 1
