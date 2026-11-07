"""Helpers shared by tests (imported by conftest as well)."""

from __future__ import annotations


def special_order(**overrides):
    from costpilot.engine.decisions import run_decision

    arguments = {
        "order_units": "2000",
        "order_price": "380",
        "variable_manufacturing_cost": "300",
        "regular_price": "500",
        "regular_variable_selling_cost": "20",
        "incremental_fixed_cost": "40000",
        "capacity_state": "idle",
        "idle_capacity_units": "2200",
    }
    arguments.update(overrides)
    return run_decision("special_order", arguments)
