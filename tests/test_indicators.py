from __future__ import annotations

from conftest import bar

from orb_bot.strategy.indicators import BarAggregator, RollingSMA


def test_bar_aggregator_completes_5m_bucket_on_boundary_crossing():
    agg = BarAggregator(5)

    # Bar timestamps are CLOSE times, so the 1-minute bar closing at 9:31
    # covers [9:30, 9:31) and is the first bar inside the [9:30, 9:35) 5m
    # bucket; that bucket only finalizes once the 9:36 bar proves it's over.
    completed = []
    for m in range(31, 41):  # 9:31 .. 9:40 (10 one-minute bars)
        c = 5000.0 + m
        result = agg.add_bar(bar(9, m, c, c + 1, c - 1, c))
        if result is not None:
            completed.append(result)

    assert len(completed) == 1
    first = completed[0]
    assert first.open == 5031.0
    assert first.close == 5035.0
    assert first.high == 5036.0
    assert first.low == 5030.0


def test_rolling_sma_returns_none_until_period_is_filled():
    sma = RollingSMA(3)
    assert sma.update(bar(9, 30, 1, 1, 1, 10.0)) is None
    assert sma.update(bar(9, 31, 1, 1, 1, 20.0)) is None
    assert sma.update(bar(9, 32, 1, 1, 1, 30.0)) == 20.0
    assert sma.update(bar(9, 33, 1, 1, 1, 60.0)) == pytest_approx((20 + 30 + 60) / 3)


def pytest_approx(value):
    import pytest

    return pytest.approx(value, rel=1e-9)
