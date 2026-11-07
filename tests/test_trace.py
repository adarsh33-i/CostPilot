"""Trace, rule registry, slot narration, and validation behaviour."""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.narration import is_slot_only, render
from costpilot.engine.rules import all_rules, get_rule
from costpilot.engine.rules.registry import RuleError
from costpilot.engine.trace import CalcResult, Fact, StepBuilder
from costpilot.engine.validation import validate_result


def test_rule_catalogue_is_versioned_and_cited():
    rules = all_rules()
    assert len(rules) >= 40
    for rule in rules:
        assert rule.version
        assert rule.citation
        assert rule.formula


def test_unknown_rule_is_rejected():
    with pytest.raises(RuleError):
        get_rule("NOPE-001")


def test_duplicate_step_ids_are_rejected():
    builder = StepBuilder()
    kwargs = dict(
        step_id="s1",
        rule_id="DA-SO-001",
        label="Revenue",
        formula="units × price",
        inputs={"units": Decimal("1")},
        value=Decimal("1"),
        unit="INR",
        source_refs=["input:units"],
    )
    builder.add(**kwargs)
    with pytest.raises(ValueError):
        builder.add(**kwargs)


def test_slot_only_narration_rejects_literal_numbers():
    assert is_slot_only("Profit is {{f_profit}}.")
    assert not is_slot_only("Profit is 120000.")
    assert not is_slot_only("Margin is 12%.")


def test_render_fails_on_unknown_fact():
    result = CalcResult(
        value=Decimal("1"), unit="INR", favorable=True, verdict=None, steps=[], facts=[]
    )
    with pytest.raises(ValueError):
        render("Value {{f_missing}}", result)


def test_validator_catches_a_fact_that_does_not_match_its_step():
    builder = StepBuilder()
    step = builder.add(
        step_id="s1",
        rule_id="DA-SO-001",
        label="Revenue",
        formula="units × price",
        inputs={"units": Decimal("1")},
        value=Decimal("10"),
        unit="INR",
        source_refs=["input:units"],
    )
    result = CalcResult(
        value=Decimal("10"),
        unit="INR",
        favorable=True,
        verdict=None,
        steps=builder.steps,
        facts=[Fact("f_wrong", step.step_id, "Revenue", Decimal("11"), "INR")],
    )
    report = validate_result(result)
    assert not report.ok
    assert report.coverage is False
    assert "does not match its step" in " ".join(report.failures)


def test_validator_catches_a_literal_number_in_the_template():
    from costpilot.engine.decisions import run_decision

    result = run_decision(
        "special_order",
        {
            "order_units": "2000",
            "order_price": "380",
            "variable_manufacturing_cost": "300",
            "regular_price": "500",
            "regular_variable_selling_cost": "20",
            "incremental_fixed_cost": "40000",
            "idle_capacity_units": "2200",
        },
    )
    result.rationale_template = "Profit is 120000."
    report = validate_result(result)
    assert not report.ok
    assert report.slot_narration is False
