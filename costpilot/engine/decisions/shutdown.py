"""Operate-or-shutdown analysis (DA-SD-001..003)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class ShutdownInput:
    units: Decimal
    price: Decimal
    variable_cost: Decimal
    avoidable_fixed: Decimal
    restart_cost: Decimal = ZERO
    materiality: Decimal = ZERO
    segment_id: str = ""
    currency: str = "INR"


def run(inputs: ShutdownInput) -> CalcResult:
    contribution_per_unit = inputs.price - inputs.variable_cost
    contribution = mul(inputs.units, contribution_per_unit)
    savings = inputs.avoidable_fixed - inputs.restart_cost
    advantage = savings - contribution

    builder = StepBuilder()
    lost_step = builder.add(
        step_id="s1",
        rule_id="DA-SD-001",
        label="Contribution lost on shutdown",
        formula="units × (price - variable_cost)",
        inputs={
            "units": inputs.units,
            "price": inputs.price,
            "variable_cost": inputs.variable_cost,
        },
        value=contribution,
        unit=inputs.currency,
        source_refs=["input:units", "input:price", "input:variable_cost"],
    )
    savings_step = builder.add(
        step_id="s2",
        rule_id="DA-SD-002",
        label="Avoidable fixed costs saved, net of restart cost",
        formula="avoidable_fixed - restart_cost",
        inputs={"avoidable_fixed": inputs.avoidable_fixed, "restart_cost": inputs.restart_cost},
        value=savings,
        unit=inputs.currency,
        source_refs=["input:avoidable_fixed", "input:restart_cost"],
    )
    advantage_step = builder.add(
        step_id="s3",
        rule_id="DA-SD-003",
        label="Advantage of shutting down",
        formula="s2 - s1",
        inputs={"s2": savings_step.value, "s1": lost_step.value},
        value=advantage,
        unit=inputs.currency,
        source_refs=["engine:shutdown"],
    )

    warnings: list[Warning] = []
    if inputs.units <= ZERO:
        warnings.append(
            Warning(
                "zero_volume",
                "No volume is planned; the shutdown comparison is driven by fixed-cost savings.",
                "warning",
            )
        )
    if advantage > inputs.materiality:
        verdict = "shut down"
    elif advantage < -inputs.materiality:
        verdict = "continue operating"
    else:
        verdict = "investigate"

    result = CalcResult(
        value=advantage,
        unit=inputs.currency,
        favorable=advantage > ZERO,
        verdict=verdict,
        steps=builder.steps,
        facts=[
            Fact("f_advantage", "s3", "Advantage of shutting down", advantage_step.value, inputs.currency),
            Fact("f_lost", "s1", "Contribution lost", lost_step.value, inputs.currency),
            Fact("f_savings", "s2", "Fixed costs saved", savings_step.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "fixed_savings", "The stated fixed costs are avoidable during the shutdown",
                "user_explicit", True, confidence=Decimal("0.8"),
            ),
        ],
        warnings=warnings,
        rationale_template=(
            "Shutting down would save {{f_savings}} of fixed cost but lose {{f_lost}} of "
            "contribution, for a net advantage of {{f_advantage}}."
        ),
        meta={"decision_type": "operate_or_shutdown", "segment_id": inputs.segment_id},
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(advantage, savings - contribution)])
    if not report.ok:
        raise DomainError("SD_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
