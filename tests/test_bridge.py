"""Variable-to-absorption bridge."""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.costing import bridge
from costpilot.engine.errors import DomainError


def run(begin, end, rate="60", variable="100000"):
    return bridge.run(
        bridge.BridgeInput(
            variable_operating_income=Decimal(variable),
            beginning_inventory_units=Decimal(begin),
            ending_inventory_units=Decimal(end),
            fixed_overhead_rate=Decimal(rate),
        )
    )


def test_inventory_build_carries_fixed_overhead_into_absorption_income():
    result = run("100", "130")
    facts = result.fact_map()
    assert facts["f_fixed_inventory"].value == Decimal("1800")
    assert facts["f_absorption"].value == Decimal("101800")
    assert facts["f_variable"].value == Decimal("100000")
    assert result.value == Decimal("101800")


def test_inventory_release_reduces_absorption_income():
    result = run("130", "100")
    assert result.fact_map()["f_fixed_inventory"].value == Decimal("-1800")
    assert result.value == Decimal("98200")


def test_negative_fixed_rate_is_rejected():
    with pytest.raises(DomainError):
        run("100", "130", rate="-1")
