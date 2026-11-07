"""Variable-to-absorption costing bridge (BRG-001..002)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class BridgeInput:
    variable_operating_income: Decimal
    beginning_inventory_units: Decimal
    ending_inventory_units: Decimal
    fixed_overhead_rate: Decimal
    period_id: str = ""
    currency: str = "INR"


def run(inputs: BridgeInput) -> CalcResult:
    if inputs.fixed_overhead_rate < ZERO:
        raise DomainError("BRG_RATE", "Fixed overhead rate cannot be negative.")

    change = inputs.ending_inventory_units - inputs.beginning_inventory_units
    fixed_in_inventory = mul(change, inputs.fixed_overhead_rate)
    absorption = inputs.variable_operating_income + fixed_in_inventory

    builder = StepBuilder()
    variable_step = builder.add(
        step_id="s0",
        rule_id="BRG-000",
        label="Variable-costing operating income",
        formula="variable_operating_income",
        inputs={"variable_operating_income": inputs.variable_operating_income},
        value=inputs.variable_operating_income,
        unit=inputs.currency,
        source_refs=["input:variable_operating_income"],
    )
    change_step = builder.add(
        step_id="s1",
        rule_id="BRG-001",
        label="Change in inventory units",
        formula="ending_inventory_units - beginning_inventory_units",
        inputs={
            "ending_inventory_units": inputs.ending_inventory_units,
            "beginning_inventory_units": inputs.beginning_inventory_units,
        },
        value=change,
        unit="units",
        source_refs=["input:beginning_inventory_units", "input:ending_inventory_units"],
    )
    fixed_step = builder.add(
        step_id="s2",
        rule_id="BRG-001",
        label="Fixed overhead carried in inventory change",
        formula="inventory_change_units × fixed_overhead_rate",
        inputs={
            "inventory_change_units": change_step.value,
            "fixed_overhead_rate": inputs.fixed_overhead_rate,
        },
        value=fixed_in_inventory,
        unit=inputs.currency,
        source_refs=["standard_cost:fixed_oh_rate"],
        note="Positive when inventory builds; negative when inventory is released.",
    )
    absorption_step = builder.add(
        step_id="s3",
        rule_id="BRG-002",
        label="Absorption-costing operating income",
        formula="variable_operating_income + fixed_in_inventory",
        inputs={
            "variable_operating_income": inputs.variable_operating_income,
            "fixed_in_inventory": fixed_step.value,
        },
        value=absorption,
        unit=inputs.currency,
        source_refs=["engine:bridge"],
    )

    result = CalcResult(
        value=absorption,
        unit=inputs.currency,
        favorable=absorption >= inputs.variable_operating_income,
        verdict=None,
        steps=builder.steps,
        facts=[
            Fact("f_absorption", "s3", "Absorption operating income", absorption_step.value, inputs.currency),
            Fact("f_variable", "s0", "Variable operating income", variable_step.value, inputs.currency),
            Fact("f_fixed_inventory", "s2", "Fixed overhead in inventory", fixed_step.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "fixed_rate_stable", "The fixed overhead rate is stable across the period",
                "registry", True, confidence=Decimal("1"),
            ),
        ],
        rationale_template=(
            "Variable costing reports {{f_variable}} of operating income; the inventory movement "
            "carries {{f_fixed_inventory}} of fixed overhead, so absorption costing reports "
            "{{f_absorption}}."
        ),
        meta={"period_id": inputs.period_id, "inventory_change_units": str(change)},
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(
        result,
        identities=[(absorption, inputs.variable_operating_income + fixed_in_inventory)],
    )
    if not report.ok:
        raise DomainError("BRG_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
