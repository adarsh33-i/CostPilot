"""Sell-or-process-further analysis (DA-SP-001..004)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class SellOrProcessInput:
    units: Decimal
    split_off_price: Decimal
    processed_price: Decimal
    processing_cost_per_unit: Decimal
    incremental_fixed: Decimal = ZERO
    joint_cost: Decimal = ZERO
    product_id: str = ""
    currency: str = "INR"


def run(inputs: SellOrProcessInput) -> CalcResult:
    if inputs.units <= ZERO:
        raise DomainError("SP_UNITS_POSITIVE", "Units must be positive.", "Enter the joint-product volume.")
    incremental_revenue = mul(inputs.units, inputs.processed_price - inputs.split_off_price)
    incremental_cost = mul(inputs.units, inputs.processing_cost_per_unit) + inputs.incremental_fixed
    advantage = incremental_revenue - incremental_cost

    builder = StepBuilder()
    revenue_step = builder.add(
        step_id="s1",
        rule_id="DA-SP-001",
        label="Incremental revenue from further processing",
        formula="units × (processed_price - split_off_price)",
        inputs={
            "units": inputs.units,
            "processed_price": inputs.processed_price,
            "split_off_price": inputs.split_off_price,
        },
        value=incremental_revenue,
        unit=inputs.currency,
        source_refs=["input:units", "input:processed_price", "input:split_off_price"],
    )
    cost_step = builder.add(
        step_id="s2",
        rule_id="DA-SP-002",
        label="Incremental processing cost",
        formula="units × processing_cost_per_unit + incremental_fixed",
        inputs={
            "units": inputs.units,
            "processing_cost_per_unit": inputs.processing_cost_per_unit,
            "incremental_fixed": inputs.incremental_fixed,
        },
        value=incremental_cost,
        unit=inputs.currency,
        source_refs=["input:processing_cost_per_unit", "input:incremental_fixed"],
    )
    advantage_step = builder.add(
        step_id="s3",
        rule_id="DA-SP-003",
        label="Incremental advantage of processing",
        formula="s1 - s2",
        inputs={"s1": revenue_step.value, "s2": cost_step.value},
        value=advantage,
        unit=inputs.currency,
        source_refs=["engine:sell_or_process"],
    )

    verdict = "process further" if advantage > ZERO else "sell at split-off"
    if advantage == ZERO:
        verdict = "investigate"

    result = CalcResult(
        value=advantage,
        unit=inputs.currency,
        favorable=advantage > ZERO,
        verdict=verdict,
        steps=builder.steps,
        facts=[
            Fact("f_advantage", "s3", "Incremental advantage", advantage_step.value, inputs.currency),
            Fact("f_revenue", "s1", "Incremental revenue", revenue_step.value, inputs.currency),
            Fact("f_cost", "s2", "Incremental processing cost", cost_step.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "joint_cost_sunk", "The joint cost is sunk and excluded from the comparison",
                "engine_default", True, confidence=Decimal("1"),
            ),
        ],
        rationale_template=(
            "Further processing adds {{f_revenue}} of revenue at {{f_cost}} of incremental cost, "
            "so the incremental advantage is {{f_advantage}}."
        ),
        meta={
            "decision_type": "sell_or_process",
            "product_id": inputs.product_id,
            "joint_cost_memo": str(inputs.joint_cost),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(advantage, incremental_revenue - incremental_cost)])
    if not report.ok:
        raise DomainError("SP_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
