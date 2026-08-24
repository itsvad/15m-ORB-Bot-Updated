from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import make_config


def test_default_config_loads():
    cfg = make_config()
    assert cfg.instrument.symbol == "MES"
    assert cfg.instrument.point_value == 5.0


def test_wrong_point_value_for_known_symbol_is_rejected():
    with pytest.raises(ValidationError, match="does not match the known contract spec"):
        make_config(instrument={"symbol": "MES", "point_value": 50.0, "tick_size": 0.25})


def test_es_point_value_accepted():
    cfg = make_config(instrument={"symbol": "ES", "point_value": 50.0, "tick_size": 0.25})
    assert cfg.instrument.point_value == 50.0


def test_opening_range_filter_min_must_be_less_than_max():
    with pytest.raises(ValidationError):
        make_config(opening_range_filter={"min_width_points": 30.0, "max_width_points": 10.0})


def test_entry_cutoff_before_or_end_is_rejected():
    with pytest.raises(ValidationError):
        make_config(
            session={
                "opening_range_start": "09:30",
                "opening_range_minutes": 60,
                "entry_cutoff": "10:00",  # OR doesn't even end until 10:30
                "hard_close": "15:55",
                "timezone": "America/New_York",
            }
        )


def test_risk_pct_sizing_mode_is_the_default():
    cfg = make_config()
    assert cfg.risk.sizing_mode == "risk_pct"
    assert cfg.risk.fixed_contracts is None


def test_fixed_sizing_mode_requires_a_fixed_contracts_value():
    with pytest.raises(ValidationError, match="fixed_contracts must be set"):
        make_config(risk={"sizing_mode": "fixed", "fixed_contracts": None})


def test_fixed_sizing_mode_rejects_a_value_above_the_sanity_cap():
    with pytest.raises(ValidationError, match="exceeds max_contracts_sanity_cap"):
        make_config(
            risk={"sizing_mode": "fixed", "fixed_contracts": 50, "max_contracts_sanity_cap": 20}
        )


def test_fixed_sizing_mode_accepted_within_the_cap():
    cfg = make_config(risk={"sizing_mode": "fixed", "fixed_contracts": 3})
    assert cfg.risk.sizing_mode == "fixed"
    assert cfg.risk.fixed_contracts == 3
