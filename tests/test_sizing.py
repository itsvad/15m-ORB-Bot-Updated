from __future__ import annotations

import pytest

from orb_bot.strategy.sizing import InvalidSizingInput, compute_position_size


def test_basic_sizing_floors_to_whole_contract():
    result = compute_position_size(
        equity=50_000.0,
        risk_pct_of_equity=0.01,  # $500 risk budget
        stop_distance_points=21.0,
        point_value=5.0,  # MES
        min_contracts=1,
        max_contracts_sanity_cap=20,
    )
    # 500 / (21 * 5) = 4.76 -> floors to 4
    assert result.qty == 4
    assert result.risk_dollars == 500.0


def test_undersized_risk_still_returns_min_contracts():
    result = compute_position_size(
        equity=1_000.0,
        risk_pct_of_equity=0.01,  # $10 risk budget
        stop_distance_points=21.0,
        point_value=5.0,
        min_contracts=1,
        max_contracts_sanity_cap=20,
    )
    assert result.qty == 1  # floored up to the configured minimum


def test_sanity_cap_rejects_absurd_size():
    with pytest.raises(InvalidSizingInput, match="sanity cap"):
        compute_position_size(
            equity=50_000.0,
            risk_pct_of_equity=0.01,
            stop_distance_points=1.0,  # unrealistically tight stop
            point_value=5.0,
            min_contracts=1,
            max_contracts_sanity_cap=20,
        )


def test_invalid_point_value_fails_loudly():
    with pytest.raises(InvalidSizingInput):
        compute_position_size(
            equity=50_000.0,
            risk_pct_of_equity=0.01,
            stop_distance_points=21.0,
            point_value=0.0,
            min_contracts=1,
            max_contracts_sanity_cap=20,
        )


def test_zero_stop_distance_fails_loudly():
    with pytest.raises(InvalidSizingInput):
        compute_position_size(
            equity=50_000.0,
            risk_pct_of_equity=0.01,
            stop_distance_points=0.0,
            point_value=5.0,
            min_contracts=1,
            max_contracts_sanity_cap=20,
        )
