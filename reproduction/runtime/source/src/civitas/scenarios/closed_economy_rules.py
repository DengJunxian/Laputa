# SPDX-License-Identifier: MIT
# Portions Copyright (c) 2026 SimulacraBusiness
# Portions Copyright (c) 2026 Agent Economy contributors
# Adapted for EcoSim; original permissions retained in docs/batch4/licenses/.
"""Small, actually extracted pure rules, with integer EcoSim boundaries.

EconSimulacra aa1302ed9908e487008cdf999ee8607694bddc83:
  agents/households/policy.py::ActionCapabilities (methods preserved);
  stylized_models.py::ShoppingModel.get_budget/generate_order_action.
Manyworld ed20ff4395581a5b4257d3fbd6b7cc971f97c318:
  engine/daily_time.py::DailyTime.produce, delivered-minute divmod.
This module never mutates a ledger or advances simulation time.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionCapabilities:
    """Extracted immutable disabled-action filter; typed intent names are keys."""
    disabled: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ActionCapabilities":
        disabled = config.get("disabledActions") or ()
        if isinstance(disabled, str):
            raise TypeError("disabledActions must be a sequence, not a string.")
        return cls(frozenset(str(action) for action in disabled))

    def is_enabled(self, action_key: str) -> bool:
        return action_key not in self.disabled

    def filter(self, action_fragment: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in action_fragment.items()
                if self.is_enabled(key)}


def _integer(value: int, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def household_budget_minor(cash_minor: int, reserve_minor: int,
                           max_basket_share_bps: int) -> int:
    """ShoppingModel.get_budget, adapted to exact minor-unit arithmetic."""
    _integer(cash_minor, "cash")
    _integer(reserve_minor, "reserve")
    _integer(max_basket_share_bps, "basket share")
    if max_basket_share_bps > 10000:
        raise ValueError("Basket share exceeds one")
    return max(0, min(cash_minor - reserve_minor,
                      cash_minor * max_basket_share_bps // 10000))


def affordable_quantity(target_units: int, own_units: int, seller_units: int,
                        budget_minor: int, unit_price_minor: int) -> int:
    """Extracted target/stock/affordability cap; zero-priced goods are excluded."""
    for name, value in (("target", target_units), ("own units", own_units),
                        ("seller units", seller_units), ("budget", budget_minor)):
        _integer(value, name)
    _integer(unit_price_minor, "price", 1)
    desired = min(max(0, target_units - own_units), seller_units)
    return min(desired, budget_minor // unit_price_minor)


def production_from_minutes(productive_minutes: int, output_per_worker: int,
                            normal_workday_minutes: int = 480,
                            carry_numerator: int = 0) -> tuple[int, int]:
    """Extracted Semantics 18 integer work-to-output conversion and carry."""
    _integer(productive_minutes, "productive minutes")
    _integer(output_per_worker, "output per worker", 1)
    _integer(normal_workday_minutes, "normal workday", 1)
    _integer(carry_numerator, "carry")
    if carry_numerator >= normal_workday_minutes:
        raise ValueError("Carry must be a proper residual")
    return divmod(productive_minutes * output_per_worker + carry_numerator,
                  normal_workday_minutes)
