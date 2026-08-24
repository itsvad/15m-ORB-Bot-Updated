from __future__ import annotations

import datetime as dt

import pytest

from orb_bot.config import AppConfig
from orb_bot.strategy.models import Bar
from orb_bot.timeutils import NY_TZ

TRADING_DATE = dt.date(2026, 6, 15)  # an arbitrary Monday


def ts(hh: int, mm: int, date: dt.date = TRADING_DATE) -> dt.datetime:
    return dt.datetime(date.year, date.month, date.day, hh, mm, tzinfo=NY_TZ)


def bar(
    hh: int,
    mm: int,
    o: float,
    h: float,
    l: float,
    c: float,
    date: dt.date = TRADING_DATE,
) -> Bar:
    return Bar(timestamp=ts(hh, mm, date), open=o, high=h, low=l, close=c)


def make_config(**overrides) -> AppConfig:
    raw = {
        "instrument": {"symbol": "MES", "point_value": 5.0, "tick_size": 0.25},
        "session": {
            "opening_range_start": "09:30",
            "opening_range_minutes": 15,
            "entry_cutoff": "10:30",
            "hard_close": "15:55",
            "timezone": "America/New_York",
        },
        "opening_range_filter": {"min_width_points": 10.0, "max_width_points": 32.0},
        "entry": {"buffer_points": 0.5},
        "risk": {"risk_pct_of_equity": 0.01, "min_contracts": 1, "max_contracts_sanity_cap": 20},
        "exits": {
            "breakeven_r_multiple": 1.0,
            "tp1_r_multiple": 1.5,
            "tp1_close_pct": 0.90,
            "runner": {
                "mode": "old",
                "old": {"sma_period": 9, "sma_timeframe_minutes": 5, "cap_r_multiple": 2.0},
                "new": {"sma_period": 9, "sma_timeframe_minutes": 15, "candle_lookback": 1},
            },
            "whipsaw_guard": {"time": "15:00"},
        },
        "fomc": {"enabled": False, "flatten_before": "14:00", "dates_file": "data/fomc_dates.yaml"},
        "safety": {
            "daily_loss_limit_usd": 500.0,
            "daily_loss_limit_pct_of_equity": None,
            "max_orders_per_day": 1,
        },
        "logging": {
            "log_dir": "logs",
            "decision_log_file": "logs/decisions.jsonl",
            "trade_log_file": "logs/trade_log.csv",
            "level": "INFO",
        },
    }
    for key, value in overrides.items():
        # shallow-merge one level deep, which covers every override used in tests
        section = dict(raw[key])
        section.update(value)
        raw[key] = section
    return AppConfig.model_validate(raw)


@pytest.fixture
def config() -> AppConfig:
    return make_config()
