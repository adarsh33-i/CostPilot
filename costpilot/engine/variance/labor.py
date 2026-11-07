"""Labour variances: rate, efficiency, and cost (VAR-LAB-001..003)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class LabourVarianceInput:
    actual_output: Decimal
    standard_hours_per_unit: Decimal
    standard_rate: Decimal
    actual_hours: Decimal
    actual_rate: Decimal
    product_id: str = ""
    currency: str = "INR"


def run(inputs: LabourVarianceInput) -> CalcResult:
    if inputs.actual_output <= ZERO or inputs.actual_hours < ZERO:
        raise DomainError("LAB_VOLUME", "Output and hours must be valid.")
    if inputs.standard_rate < ZERO or inputs.actual_rate < ZERO:
        raise DomainError("LAB_RATE", "Labour rates cannot be negative.")

    standard_hours_allowed = mul(inputs.actual_output, inputs.standard_hours_per_unit)
    rate_variance = mul(inputs.actual_hours, inputs.standard_rate - inputs.actual_rate)
    efficiency_variance = mul(
        inputs.standard_rate, standard_hours_allowed - inputs.actual_hours
    )
    cost_variance = rate_variance + efficiency_variance

    builder = StepBuilder()
    rate_step = builder.add(
        step_id="s1",
        rule_id="VAR-LAB-001",
        label="Labour rate variance",
        formula="AH × (SR - AR)",
        inputs={
            "actual_hours": inputs.actual_hours,
            "standard_rate": inputs.standard_rate,
            "actual_rate": inputs.actual_rate,
        },
        value=rate_variance,
        unit=inputs.currency,
        source_refs=["input:actual_hours", "input:actual_rate", "standard_cost:labour_rate"],
    )
    efficiency_step = builder.add(
        step_id="s2",
        rule_id="VAR-LAB-002",
        label="Labour efficiency variance",
        formula="SR × (SH - AH)",
        inputs={
            "standard_rate": inputs.standard_rate,
            "standard_hours_allowed": standard_hours_allowed,
            "actual_hours": inputs.actual_hours,
        },
        value=efficiency_variance,
        unit=inputs.currency,
        source_refs=["input:actual_output", "standard_cost:labour_hours"],
    )
    cost_step = builder.add(
        step_id="s3",
        rule_id="VAR-LAB-003",
        label="Labour cost variance",
        formula="s1 + s2",
        inputs={"s1": rate_step.value, "s2": efficiency_step.value},
        value=cost_variance,
        unit=inputs.currency,
        source_refs=["engine:labour"],
    )

    result = CalcResult(
        value=cost_variance,
        unit=inputs.currency,
        favorable=cost_variance >= ZERO,
        verdict=None,
        steps=builder.steps,
        facts=[
            Fact("f_lrv", "s1", "Labour rate variance", rate_step.value, inputs.currency),
            Fact("f_lev", "s2", "Labour efficiency variance", efficiency_step.value, inputs.currency),
            Fact("f_lcv", "s3", "Labour cost variance", cost_step.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "overtime_policy", "Overtime premium is charged to the rate variance",
                "policy_default", True, evidence="standard-cost policy", confidence=Decimal("0.8"),
            ),
        ],
        rationale_template=(
            "The labour rate variance is {{f_lrv}} and the efficiency variance is {{f_lev}}; "
            "together they produce a labour cost variance of {{f_lcv}}."
        ),
        meta={"product_id": inputs.product_id, "standard_hours_allowed": str(standard_hours_allowed)},
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(
        result, identities=[(cost_variance, rate_variance + efficiency_variance)]
    )
    if not report.ok:
        raise DomainError("LAB_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
