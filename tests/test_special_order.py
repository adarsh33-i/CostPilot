"""Golden special-order case and its capacity sensitivity."""

from __future__ import annotations

from decimal import Decimal

import pytest
from helpers import special_order

from costpilot.engine.decisions import run_decision
from costpilot.engine.errors import DomainError
from costpilot.engine.narration import is_slot_only


def fact(result, fact_id: str) -> Decimal:
    return result.fact_map()[fact_id].value


def test_canonical_case_matches_the_blueprint():
    result = special_order()
    assert result.value == Decimal("120000")
    assert fact(result, "f_revenue") == Decimal("760000")
    assert fact(result, "f_contribution") == Decimal("160000")
    assert fact(result, "f_fixed") == Decimal("40000")
    assert fact(result, "f_opportunity") == Decimal("0")
    assert fact(result, "f_floor") == Decimal("320")
    assert result.verdict == "accept, conditionally"
    assert result.unit == "INR"


def test_every_step_has_a_rule_version_and_sources():
    result = special_order()
    assert [step.step_id for step in result.steps] == [f"s{i}" for i in range(1, 8)]
    for step in result.steps:
        assert step.rule_version == "1.0.0"
        assert step.source_refs
        assert step.formula


def test_rationale_is_slot_only_and_cites_every_number():
    result = special_order()
    assert is_slot_only(result.rationale_template)
    assert "160,000.00" in result.rationale
    assert "120,000.00" in result.rationale
    assert "320.00" in result.rationale


def test_validation_report_passes_every_check():
    result = special_order()
    validation = result.meta["validation"]
    assert validation["failures"] == []
    for key in ("schema", "identities", "units", "coverage", "provenance", "slot_narration"):
        assert validation[key] == "pass"


def test_capacity_sensitivity_reproduces_the_half_order_case():
    result = special_order()
    points = {point.label: point for point in result.sensitivity}
    assert points["If no idle capacity"].value == Decimal("-240000")
    assert points["If idle capacity were half the order"].value == Decimal("-60000")
    assert points["If idle capacity covers the order"].value == Decimal("120000")
    half = points["If idle capacity were half the order"]
    assert half.context["displaced_units"] == "1000"
    assert half.context["opportunity_cost"] == "180000"
    assert half.note == "Minimum price 410.00 per unit"


def test_displaced_units_flow_into_profit_and_floor():
    result = special_order(idle_capacity_units=None, displaced_units="1000")
    assert result.value == Decimal("-60000")
    assert fact(result, "f_opportunity") == Decimal("180000")
    assert fact(result, "f_floor") == Decimal("410")
    assert result.verdict == "reject"


def test_constrained_capacity_assumes_the_whole_order_displaces_regular_sales():
    result = special_order(capacity_state="constrained", idle_capacity_units=None)
    assert result.value == Decimal("-240000")
    assert fact(result, "f_floor") == Decimal("500")


def test_variable_selling_cost_kept_on_the_order_reduces_contribution():
    result = special_order(variable_selling_cost_avoided=False)
    assert fact(result, "f_contribution") == Decimal("120000")
    assert result.value == Decimal("80000")
    assert fact(result, "f_floor") == Decimal("340")


def test_utilization_cross_check_warns():
    result = special_order(utilization="0.96")
    codes = {warning.code for warning in result.warnings}
    assert "capacity_cross_check" in codes


def test_capacity_asserted_without_quantity_warns():
    result = special_order(idle_capacity_units=None)
    codes = {warning.code for warning in result.warnings}
    assert "capacity_unquantified" in codes


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"order_units": "0"}, "SO_UNITS_POSITIVE"),
        ({"order_price": "-1"}, "SO_PRICE_POSITIVE"),
        ({"variable_manufacturing_cost": "-5"}, "SO_VARIABLE_COST_NEGATIVE"),
        ({"incremental_fixed_cost": "-1"}, "SO_FIXED_COST_NEGATIVE"),
        ({"capacity_state": "sometimes"}, "SO_CAPACITY_STATE"),
        ({"utilization": "1.2"}, "SO_UTILIZATION_RANGE"),
    ],
)
def test_invalid_inputs_raise_domain_errors_with_remediation(overrides, code):
    base = {
        "order_units": "2000",
        "order_price": "380",
        "variable_manufacturing_cost": "300",
        "regular_price": "500",
        "incremental_fixed_cost": "40000",
        "idle_capacity_units": "2200",
        **overrides,
    }
    with pytest.raises(DomainError) as caught:
        run_decision("special_order", base)
    assert caught.value.code == code
    assert caught.value.remediation


def test_missing_input_is_a_domain_error_not_a_key_error():
    with pytest.raises(DomainError) as caught:
        run_decision("special_order", {"order_units": "10", "order_price": "5"})
    assert caught.value.code == "MISSING_ARGUMENT"
    assert "variable_manufacturing_cost" in caught.value.message


def test_unknown_argument_is_rejected():
    with pytest.raises(DomainError) as caught:
        run_decision("special_order", {"order_units": "10", "order_price": "5", "profit": "99"})
    assert caught.value.code in {"UNKNOWN_ARGUMENT", "MISSING_ARGUMENT"}
