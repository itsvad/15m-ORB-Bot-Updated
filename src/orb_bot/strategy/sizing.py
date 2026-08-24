"""Position sizing and the size/point-value sanity rails.

Two sizing modes, selected by `risk.sizing_mode`:
- "risk_pct" (default): qty = risk_dollars / (stop_distance_points *
  point_value), floored to a whole contract, minimum `min_contracts`.
- "fixed": qty is always `fixed_contracts`, ignoring equity/risk_pct
  entirely - useful for a flat "always trade N contracts" override.

Either way, `max_contracts_sanity_cap` is a hard rail that always applies -
a fixed-contract override doesn't bypass it. A misconfigured point value or
an absurd computed size must fail loudly rather than silently submitting a
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
    sizing_mode: str,
    risk_pct_of_equity: float,
    fixed_contracts: int | None,
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

    if sizing_mode == "fixed":
        if fixed_contracts is None or fixed_contracts <= 0:
            raise InvalidSizingInput(
                f"sizing_mode is 'fixed' but fixed_contracts={fixed_contracts} "
                f"is not a positive integer"
            )
        qty = fixed_contracts
        risk_dollars = qty * stop_distance_points * point_value
    elif sizing_mode == "risk_pct":
        if equity <= 0:
            raise InvalidSizingInput(f"Refusing to size a trade with equity={equity}")
        risk_dollars = equity * risk_pct_of_equity
        raw_qty = risk_dollars / (stop_distance_points * point_value)
        qty = max(min_contracts, int(raw_qty))  # floor toward zero, then apply floor
    else:
        raise InvalidSizingInput(f"Unknown sizing_mode={sizing_mode!r}")

    if qty <= 0:
        raise InvalidSizingInput(
            f"Computed size <= 0 (sizing_mode={sizing_mode}); refusing to "
            f"submit a zero-size order"
        )
    if qty > max_contracts_sanity_cap:
        raise InvalidSizingInput(
            f"Computed size {qty} exceeds sanity cap "
            f"{max_contracts_sanity_cap} (sizing_mode={sizing_mode}, "
            f"equity={equity}, risk_pct={risk_pct_of_equity}, "
            f"stop_distance={stop_distance_points}pts, point_value={point_value}). "
            f"This usually means a misconfigured point_value, equity feed, "
            f"or fixed_contracts override - refusing to trade."
        )

    return SizingResult(
        qty=qty, risk_dollars=risk_dollars, stop_distance_points=stop_distance_points
    )
