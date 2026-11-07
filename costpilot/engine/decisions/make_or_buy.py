"""Make-or-buy differential analysis (DA-MB-001..004)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class MakeOrBuyInput:
    units: Decimal
    variable_cost_per_unit: Decimal
    avoidable_fixed: Decimal
    purchase_price: Decimal
    incremental_fixed_to_buy: Decimal = ZERO
    opportunity_cost: Decimal = ZERO
    materiality: Decimal = ZERO
    product_id: str = ""
    currency: str = "INR"


def run(inputs: MakeOrBuyInput) -> CalcResult:
    if inputs.units <= ZERO:
        raise DomainError("MB_UNITS_POSITIVE", "Units must be positive.", "Enter an annual volume.")
    if inputs.purchase_price < ZERO:
        raise DomainError("MB_PRICE_NEGATIVE", "Purchase price cannot be negative.")

    make_cost = (
        mul(inputs.units, inputs.variable_cost_per_unit)
        + inputs.avoidable_fixed
        + inputs.opportunity_cost
    )
    buy_cost = mul(inputs.units, inputs.purchase_price) + inputs.incremental_fixed_to_buy
    advantage = buy_cost - make_cost

    builder = StepBuilder()
    make = builder.add(
        step_id="s1",
        rule_id="DA-MB-001",
        label="Relevant cost to make",
        formula="units × variable_cost + avoidable_fixed + opportunity_cost",
        inputs={
            "units": inputs.units,
            "variable_cost_per_unit": inputs.variable_cost_per_unit,
            "avoidable_fixed": inputs.avoidable_fixed,
            "opportunity_cost": inputs.opportunity_cost,
        },
        value=make_cost,
        unit=inputs.currency,
        source_refs=["input:units", "input:variable_cost_per_unit", "input:avoidable_fixed"],
    )
    buy = builder.add(
        step_id="s2",
        rule_id="DA-MB-002",
        label="Relevant cost to buy",
        formula="units × purchase_price + incremental_fixed_to_buy",
        inputs={
            "units": inputs.units,
            "purchase_price": inputs.purchase_price,
            "incremental_fixed_to_buy": inputs.incremental_fixed_to_buy,
        },
        value=buy_cost,
        unit=inputs.currency,
        source_refs=["input:units", "input:purchase_price"],
    )
    advantage_step = builder.add(
        step_id="s3",
        rule_id="DA-MB-003",
        label="Advantage of making",
        formula="s2 - s1",
        inputs={"s2": buy.value, "s1": make.value},
        value=advantage,
        unit=inputs.currency,
        source_refs=["engine:make_or_buy"],
    )

    if advantage > inputs.materiality:
        verdict = "make"
    elif advantage < -inputs.materiality:
        verdict = "buy"
    else:
        verdict = "investigate"

    result = CalcResult(
        value=advantage,
        unit=inputs.currency,
        favorable=advantage > ZERO,
        verdict=verdict,
        steps=builder.steps,
        facts=[
            Fact("f_advantage", "s3", "Advantage of making", advantage_step.value, inputs.currency),
            Fact("f_make", "s1", "Relevant cost to make", make.value, inputs.currency),
            Fact("f_buy", "s2", "Relevant cost to buy", buy.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "avoidable_fixed", "Only avoidable fixed costs are included",
                "user_explicit", True, confidence=Decimal("1"),
            ),
            Assumption(
                "a2", "opportunity_cost", "Freed capacity has the stated opportunity cost",
                "user_explicit", True, confidence=Decimal("0.8"),
            ),
            Assumption(
                "a3", "allocated_fixed_excluded",
                "Allocated fixed overhead that continues either way is excluded",
                "engine_default", True, confidence=Decimal("1"),
            ),
        ],
        rationale_template=(
            "Making costs {{f_make}} and buying costs {{f_buy}}, so the advantage of making "
            "is {{f_advantage}}. The engine verdict follows the stated materiality threshold."
        ),
        meta={"decision_type": "make_or_buy", "product_id": inputs.product_id},
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(advantage, buy_cost - make_cost)])
    if not report.ok:
        raise DomainError("MB_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
