"""CVP: break-even, margin of safety, operating leverage (DA-CVP-001..005)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, div, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class CvpInput:
    price: Decimal
    variable_cost: Decimal
    fixed_costs: Decimal
    actual_units: Decimal
    target_profit: Decimal = ZERO
    product_id: str = ""
    currency: str = "INR"


def run(inputs: CvpInput) -> CalcResult:
    contribution_per_unit = inputs.price - inputs.variable_cost
    if contribution_per_unit <= ZERO:
        raise DomainError(
            "CVP_CONTRIBUTION_NONPOSITIVE",
            "No break-even volume exists at this price because contribution is zero or negative.",
            "Increase the price, reduce variable cost, or use a total-cost view.",
        )
    break_even_units = div(inputs.fixed_costs, contribution_per_unit, "break-even units")
    break_even_revenue = mul(break_even_units, inputs.price)
    margin_units = inputs.actual_units - break_even_units
    contribution = mul(inputs.actual_units, contribution_per_unit)
    operating_income = contribution - inputs.fixed_costs
    target_units = div(inputs.fixed_costs + inputs.target_profit, contribution_per_unit, "target units")

    builder = StepBuilder()
    cm_step = builder.add(
        step_id="s1",
        rule_id="DA-CVP-001",
        label="Contribution margin per unit",
        formula="price - variable_cost",
        inputs={"price": inputs.price, "variable_cost": inputs.variable_cost},
        value=contribution_per_unit,
        unit=f"{inputs.currency}/unit",
        source_refs=["input:price", "input:variable_cost"],
    )
    be_step = builder.add(
        step_id="s2",
        rule_id="DA-CVP-002",
        label="Break-even units",
        formula="fixed_costs ÷ contribution_per_unit",
        inputs={"fixed_costs": inputs.fixed_costs, "contribution_per_unit": cm_step.value},
        value=break_even_units,
        unit="units",
        source_refs=["input:fixed_costs", "input:price", "input:variable_cost"],
    )
    be_rev_step = builder.add(
        step_id="s3",
        rule_id="DA-CVP-003",
        label="Break-even revenue",
        formula="break_even_units × price",
        inputs={"break_even_units": be_step.value, "price": inputs.price},
        value=break_even_revenue,
        unit=inputs.currency,
        source_refs=["input:price"],
    )
    margin_step = builder.add(
        step_id="s4",
        rule_id="DA-CVP-004",
        label="Margin of safety in units",
        formula="actual_units - break_even_units",
        inputs={"actual_units": inputs.actual_units, "break_even_units": be_step.value},
        value=margin_units,
        unit="units",
        source_refs=["input:actual_units"],
    )
    dol_step = builder.add(
        step_id="s5",
        rule_id="DA-CVP-005",
        label="Degree of operating leverage",
        formula="contribution ÷ operating_income",
        inputs={"contribution": contribution, "operating_income": operating_income},
        value=div(contribution, operating_income, "operating leverage")
        if operating_income != ZERO
        else ZERO,
        unit="ratio",
        source_refs=["engine:cvp"],
        note="Undefined at zero operating income; report as unavailable when income is not positive.",
    )

    warnings: list[Warning] = []
    if operating_income <= ZERO:
        warnings.append(
            Warning(
                "operating_leverage_unavailable",
                "Operating leverage is undefined because operating income is zero or negative.",
                "warning",
            )
        )
    if inputs.actual_units < break_even_units:
        warnings.append(
            Warning(
                "below_break_even",
                "Actual volume is below break-even.",
                "warning",
                "Review price, variable cost, or fixed-cost structure.",
            )
        )

    result = CalcResult(
        value=operating_income,
        unit=inputs.currency,
        favorable=operating_income > ZERO,
        verdict="above break-even" if margin_units >= ZERO else "below break-even",
        steps=builder.steps,
        facts=[
            Fact("f_cm", "s1", "Contribution per unit", cm_step.value, cm_step.unit),
            Fact("f_be_units", "s2", "Break-even units", be_step.value, "units"),
            Fact("f_be_revenue", "s3", "Break-even revenue", be_rev_step.value, inputs.currency),
            Fact("f_margin", "s4", "Margin of safety units", margin_step.value, "units"),
            Fact("f_dol", "s5", "Operating leverage", dol_step.value, "ratio"),
        ],
        warnings=warnings,
        rationale_template=(
            "At a contribution of {{f_cm}} per unit, break-even is {{f_be_units}} units "
            "({{f_be_revenue}} of revenue). The margin of safety is {{f_margin}} units, and "
            "operating leverage is {{f_dol}}."
        ),
        meta={
            "decision_type": "cvp",
            "product_id": inputs.product_id,
            "target_units": str(target_units),
            "operating_income": str(operating_income),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(operating_income, contribution - inputs.fixed_costs)])
    if not report.ok:
        raise DomainError("CVP_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
