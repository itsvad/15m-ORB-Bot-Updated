"""Position sizing and the size/point-value sanity rails.

Position size = risk_dollars / (stop_distance_points * point_value), floored
to a whole contract, minimum 1. A misconfigured point value or an
absurd computed size must fail loudly rather than silently submitting a
wrong-sized order (per the project's hard safety requirements).
"""
from __future__ import annotations

from dataclasses import dataclass


class InvalidSizingInput(ValueError):
    """Raised instead of silently placing a zero/garbage-sized order."""


@dataclass(frozen=True)
class SizingResult:
    qty: int
    risk_dollars: float
    stop_distance_points: float


def compute_position_size(
    *,
    equity: float,
    risk_pct_of_equity: float,
    stop_distance_points: float,
    point_value: float,
    min_contracts: int,
    max_contracts_sanity_cap: int,
) -> SizingResult:
    if point_value <= 0:
        raise InvalidSizingInput(
            f"Refusing to size a trade with invalid point_value={point_value}"
        )
    if stop_distance_points <= 0:
        raise InvalidSizingInput(
            f"Refusing to size a trade with non-positive stop distance "
            f"({stop_distance_points} pts)"
        )
    if equity <= 0:
        raise InvalidSizingInput(f"Refusing to size a trade with equity={equity}")

    risk_dollars = equity * risk_pct_of_equity
    raw_qty = risk_dollars / (stop_distance_points * point_value)
    qty = max(min_contracts, int(raw_qty))  # floor toward zero, then apply floor

    if qty <= 0:
        raise InvalidSizingInput(
            f"Computed size <= 0 (raw_qty={raw_qty}); refusing to submit a "
            f"zero-size order"
        )
    if qty > max_contracts_sanity_cap:
        raise InvalidSizingInput(
            f"Computed size {qty} exceeds sanity cap "
            f"{max_contracts_sanity_cap} (equity={equity}, "
            f"risk_pct={risk_pct_of_equity}, stop_distance={stop_distance_points}pts, "
            f"point_value={point_value}). This usually means a misconfigured "
            f"point_value or equity feed - refusing to trade."
        )

    return SizingResult(
        qty=qty, risk_dollars=risk_dollars, stop_distance_points=stop_distance_points
    )
