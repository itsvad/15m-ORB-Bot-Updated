from __future__ import annotations

from conftest import TRADING_DATE, bar, make_config

from orb_bot.persistence.store import StateStore
from orb_bot.strategy.state_machine import OrbEngine

EQUITY = 50_000.0


def test_day_state_round_trips_through_the_store(tmp_path):
    store = StateStore(tmp_path / "state.db")
    config = make_config()
    engine = OrbEngine(config, TRADING_DATE, EQUITY)

    for i in range(15):
        mm = 30 + i
        px = 5020.0 if i % 2 == 0 else 5000.0
        engine.on_bar(bar(9, mm, px, 5020.0, 5000.0, px))
    engine.on_bar(bar(9, 45, 5020.0, 5020.0, 5000.0, 5020.0))

    from orb_bot.strategy.models import Fill

    engine.on_fill(Fill(order_ref="entry_long", price=5020.5, qty=4, timestamp=bar(9, 46, 0, 0, 0, 0).timestamp))

    store.save_day_state(engine.day)

    restored = store.load_day_state(TRADING_DATE)
    assert restored is not None
    assert restored.position is not None
    assert restored.position.side.value == "long"
    assert restored.position.entry_price == 5020.5
    assert restored.position.qty == 4
    assert restored.entry_qty == 4
    assert restored.trade_done_for_day is True

    # A fresh engine can resume from the restored DayState and keep managing
    # the same position (the actual mid-session-restart recovery path).
    resumed_engine = OrbEngine(config, TRADING_DATE, EQUITY, day_state=restored)
    assert resumed_engine.day.position is not None
    actions = resumed_engine.on_bar(bar(9, 50, 5041.5, 5042.0, 5041.0, 5042.0))
    from orb_bot.strategy.models import ModifyOrder

    assert any(isinstance(a, ModifyOrder) and a.reason == "breakeven" for a in actions)

    store.close()


def test_pause_resume_control_flag(tmp_path):
    store = StateStore(tmp_path / "state.db")
    assert store.is_paused() is False
    store.set_paused(True)
    assert store.is_paused() is True
    store.set_paused(False)
    assert store.is_paused() is False
    store.close()


def test_load_missing_day_state_returns_none(tmp_path):
    store = StateStore(tmp_path / "state.db")
    assert store.load_day_state(TRADING_DATE) is None
    store.close()
