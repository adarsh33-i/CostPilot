"""Add-or-drop segment analysis (DA-AD-001..005)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class AddOrDropInput:
    revenue: Decimal
    variable_costs: Decimal
    avoidable_fixed: Decimal
    allocated_fixed: Decimal = ZERO
    lost_units: Decimal = ZERO
    related_contribution_per_unit: Decimal = ZERO
    materiality: Decimal = ZERO
    segment_id: str = ""
    currency: str = "INR"


def run(inputs: AddOrDropInput) -> CalcResult:
    contribution = inputs.revenue - inputs.variable_costs
    lost_contribution = mul(inputs.lost_units, inputs.related_contribution_per_unit)
    segment_margin = contribution - inputs.avoidable_fixed - lost_contribution

    builder = StepBuilder()
    contribution_step = builder.add(
        step_id="s1",
        rule_id="DA-AD-001",
        label="Segment contribution",
        formula="revenue - variable_costs",
        inputs={"revenue": inputs.revenue, "variable_costs": inputs.variable_costs},
        value=contribution,
        unit=inputs.currency,
        source_refs=["input:revenue", "input:variable_costs"],
    )
    avoidable_step = builder.add(
        step_id="s2",
        rule_id="DA-AD-002",
        label="Avoidable fixed costs",
        formula="avoidable_fixed",
        inputs={"avoidable_fixed": inputs.avoidable_fixed},
        value=inputs.avoidable_fixed,
        unit=inputs.currency,
        source_refs=["input:avoidable_fixed"],
        note="Allocated fixed costs are excluded from the decision.",
    )
    lost_step = builder.add(
        step_id="s3",
        rule_id="DA-AD-003",
        label="Lost contribution from related sales",
        formula="lost_units × related_contribution_per_unit",
        inputs={
            "lost_units": inputs.lost_units,
            "related_contribution_per_unit": inputs.related_contribution_per_unit,
        },
        value=lost_contribution,
        unit=inputs.currency,
        source_refs=["input:lost_units", "input:related_contribution_per_unit"],
    )
    margin_step = builder.add(
        step_id="s4",
        rule_id="DA-AD-004",
        label="Segment margin after avoidable costs",
        formula="s1 - s2 - s3",
        inputs={
            "s1": contribution_step.value,
            "s2": avoidable_step.value,
            "s3": lost_step.value,
        },
        value=segment_margin,
        unit=inputs.currency,
        source_refs=["engine:add_or_drop"],
    )

    if segment_margin > inputs.materiality:
        verdict = "keep"
    elif segment_margin < -inputs.materiality:
        verdict = "drop"
    else:
        verdict = "investigate"

    result = CalcResult(
        value=segment_margin,
        unit=inputs.currency,
        favorable=segment_margin > ZERO,
        verdict=verdict,
        steps=builder.steps,
        facts=[
            Fact("f_margin", "s4", "Segment margin", margin_step.value, inputs.currency),
            Fact("f_contribution", "s1", "Segment contribution", contribution_step.value, inputs.currency),
            Fact("f_avoidable", "s2", "Avoidable fixed costs", avoidable_step.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "allocated_fixed_excluded", "Allocated fixed costs continue and are excluded",
                "engine_default", True, confidence=Decimal("1"),
            ),
            Assumption(
                "a2", "avoidable_fixed", "The stated fixed costs are avoidable if the segment is dropped",
                "user_explicit", True, confidence=Decimal("0.8"),
            ),
        ],
        rationale_template=(
            "The segment contributes {{f_contribution}} but carries {{f_avoidable}} of avoidable "
            "fixed costs, leaving a segment margin of {{f_margin}}."
        ),
        remediation=(
            ["Confirm that no related-product sales depend on this segment."]
            if inputs.lost_units == ZERO
            else []
        ),
        meta={
            "decision_type": "add_or_drop",
            "segment_id": inputs.segment_id,
            "allocated_fixed_memo": str(inputs.allocated_fixed),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(
        result,
        identities=[(segment_margin, contribution - inputs.avoidable_fixed - lost_contribution)],
    )
    if not report.ok:
        raise DomainError("AD_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
