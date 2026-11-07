"""Variable and fixed overhead variances (VAR-OH-001..006)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import CalcResult, Fact, StepBuilder
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class VariableOverheadInput:
    actual_hours: Decimal
    standard_hours_allowed: Decimal
    standard_rate: Decimal
    actual_variable_overhead: Decimal
    product_id: str = ""
    currency: str = "INR"


@dataclass(frozen=True)
class FixedOverheadInput:
    budgeted_fixed: Decimal
    actual_fixed: Decimal
    standard_hours_allowed: Decimal
    denominator_hours: Decimal
    fixed_rate: Decimal
    product_id: str = ""
    currency: str = "INR"


def variable_overhead(inputs: VariableOverheadInput) -> CalcResult:
    if inputs.actual_hours < ZERO or inputs.standard_hours_allowed < ZERO:
        raise DomainError("VOH_HOURS", "Hours cannot be negative.")
    if inputs.actual_variable_overhead < ZERO:
        raise DomainError("VOH_ACTUAL", "Actual variable overhead cannot be negative.")

    budget_at_actual_hours = mul(inputs.actual_hours, inputs.standard_rate)
    spending = budget_at_actual_hours - inputs.actual_variable_overhead
    efficiency = mul(
        inputs.standard_rate, inputs.standard_hours_allowed - inputs.actual_hours
    )
    total = spending + efficiency
    applied = mul(inputs.standard_hours_allowed, inputs.standard_rate)

    builder = StepBuilder()
    spending_step = builder.add(
        step_id="s1",
        rule_id="VAR-OH-001",
        label="Variable overhead spending variance",
        formula="AH × SR_voh - actual_variable_oh",
        inputs={
            "actual_hours": inputs.actual_hours,
            "standard_rate": inputs.standard_rate,
            "actual_variable_overhead": inputs.actual_variable_overhead,
        },
        value=spending,
        unit=inputs.currency,
        source_refs=["input:actual_hours", "input:actual_variable_overhead"],
    )
    efficiency_step = builder.add(
        step_id="s2",
        rule_id="VAR-OH-002",
        label="Variable overhead efficiency variance",
        formula="(SH - AH) × SR_voh",
        inputs={
            "standard_hours_allowed": inputs.standard_hours_allowed,
            "actual_hours": inputs.actual_hours,
            "standard_rate": inputs.standard_rate,
        },
        value=efficiency,
        unit=inputs.currency,
        source_refs=["input:standard_hours_allowed", "standard_cost:variable_oh_rate"],
    )
    total_step = builder.add(
        step_id="s3",
        rule_id="VAR-OH-003",
        label="Variable overhead total variance",
        formula="s1 + s2",
        inputs={"s1": spending_step.value, "s2": efficiency_step.value},
        value=total,
        unit=inputs.currency,
        source_refs=["engine:variable_overhead"],
    )

    result = CalcResult(
        value=total,
        unit=inputs.currency,
        favorable=total >= ZERO,
        verdict=None,
        steps=builder.steps,
        facts=[
            Fact("f_voh_spending", "s1", "Variable overhead spending", spending_step.value, inputs.currency),
            Fact("f_voh_efficiency", "s2", "Variable overhead efficiency", efficiency_step.value, inputs.currency),
            Fact("f_voh_total", "s3", "Variable overhead total", total_step.value, inputs.currency),
        ],
        rationale_template=(
            "Variable overhead spending is {{f_voh_spending}} and efficiency is "
            "{{f_voh_efficiency}}, giving a total variance of {{f_voh_total}}."
        ),
        meta={
            "product_id": inputs.product_id,
            "applied_variable_overhead": str(applied),
            "budget_at_actual_hours": str(budget_at_actual_hours),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(total, applied - inputs.actual_variable_overhead)])
    if not report.ok:
        raise DomainError("VOH_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result


def fixed_overhead(inputs: FixedOverheadInput) -> CalcResult:
    if inputs.denominator_hours <= ZERO:
        raise DomainError("FOH_DENOMINATOR", "Denominator hours must be positive.")

    spending = inputs.budgeted_fixed - inputs.actual_fixed
    volume = mul(inputs.fixed_rate, inputs.standard_hours_allowed - inputs.denominator_hours)
    total = spending + volume
    applied = mul(inputs.standard_hours_allowed, inputs.fixed_rate)

    builder = StepBuilder()
    spending_step = builder.add(
        step_id="s4",
        rule_id="VAR-OH-004",
        label="Fixed overhead spending variance",
        formula="budgeted_fixed - actual_fixed",
        inputs={"budgeted_fixed": inputs.budgeted_fixed, "actual_fixed": inputs.actual_fixed},
        value=spending,
        unit=inputs.currency,
        source_refs=["input:budgeted_fixed", "input:actual_fixed"],
    )
    volume_step = builder.add(
        step_id="s5",
        rule_id="VAR-OH-005",
        label="Fixed overhead volume variance",
        formula="(SH - denominator_hours) × fixed_rate",
        inputs={
            "standard_hours_allowed": inputs.standard_hours_allowed,
            "denominator_hours": inputs.denominator_hours,
            "fixed_rate": inputs.fixed_rate,
        },
        value=volume,
        unit=inputs.currency,
        source_refs=["input:denominator_hours", "standard_cost:fixed_oh_rate"],
    )
    total_step = builder.add(
        step_id="s6",
        rule_id="VAR-OH-006",
        label="Fixed overhead total variance",
        formula="s4 + s5",
        inputs={"s4": spending_step.value, "s5": volume_step.value},
        value=total,
        unit=inputs.currency,
        source_refs=["engine:fixed_overhead"],
    )

    result = CalcResult(
        value=total,
        unit=inputs.currency,
        favorable=total >= ZERO,
        verdict=None,
        steps=builder.steps,
        facts=[
            Fact("f_foh_spending", "s4", "Fixed overhead spending", spending_step.value, inputs.currency),
            Fact("f_foh_volume", "s5", "Fixed overhead volume", volume_step.value, inputs.currency),
            Fact("f_foh_total", "s6", "Fixed overhead total", total_step.value, inputs.currency),
        ],
        rationale_template=(
            "Fixed overhead spending is {{f_foh_spending}} and the volume variance is "
            "{{f_foh_volume}}, giving a total variance of {{f_foh_total}}."
        ),
        meta={
            "product_id": inputs.product_id,
            "applied_fixed_overhead": str(applied),
            "denominator_hours": str(inputs.denominator_hours),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(total, applied - inputs.actual_fixed)])
    if not report.ok:
        raise DomainError("FOH_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
