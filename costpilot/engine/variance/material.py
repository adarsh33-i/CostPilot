"""Material variances: price, usage, and cost (VAR-MAT-001..004)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class MaterialVarianceInput:
    actual_output: Decimal
    standard_qty_per_unit: Decimal
    standard_price: Decimal
    actual_qty_used: Decimal
    actual_price: Decimal
    purchased_qty: Decimal | None = None
    product_id: str = ""
    currency: str = "INR"


def run(inputs: MaterialVarianceInput) -> CalcResult:
    if inputs.actual_output <= ZERO or inputs.actual_qty_used < ZERO:
        raise DomainError("MAT_VOLUME", "Output and usage quantities must be valid.")
    if inputs.standard_price < ZERO or inputs.actual_price < ZERO:
        raise DomainError("MAT_PRICE", "Material prices cannot be negative.")

    standard_qty_allowed = mul(inputs.actual_output, inputs.standard_qty_per_unit)
    price_variance = mul(inputs.actual_qty_used, inputs.standard_price - inputs.actual_price)
    usage_variance = mul(inputs.standard_price, standard_qty_allowed - inputs.actual_qty_used)
    cost_variance = price_variance + usage_variance

    builder = StepBuilder()
    price_step = builder.add(
        step_id="s1",
        rule_id="VAR-MAT-001",
        label="Material price variance",
        formula="AQ_used × (SP - AP)",
        inputs={
            "actual_qty_used": inputs.actual_qty_used,
            "standard_price": inputs.standard_price,
            "actual_price": inputs.actual_price,
        },
        value=price_variance,
        unit=inputs.currency,
        source_refs=["input:actual_qty_used", "input:actual_price", "standard_cost:material_price"],
    )
    usage_step = builder.add(
        step_id="s2",
        rule_id="VAR-MAT-002",
        label="Material usage variance",
        formula="SP × (SQ - AQ_used)",
        inputs={
            "standard_price": inputs.standard_price,
            "standard_qty_allowed": standard_qty_allowed,
            "actual_qty_used": inputs.actual_qty_used,
        },
        value=usage_variance,
        unit=inputs.currency,
        source_refs=["input:actual_output", "standard_cost:material_qty"],
    )
    cost_step = builder.add(
        step_id="s3",
        rule_id="VAR-MAT-003",
        label="Material cost variance",
        formula="s1 + s2",
        inputs={"s1": price_step.value, "s2": usage_step.value},
        value=cost_variance,
        unit=inputs.currency,
        source_refs=["engine:material"],
    )

    warnings: list[Warning] = []
    purchase_step = None
    if inputs.purchased_qty is not None and inputs.purchased_qty != inputs.actual_qty_used:
        purchase_variance = mul(
            inputs.purchased_qty, inputs.standard_price - inputs.actual_price
        )
        purchase_step = builder.add(
            step_id="s4",
            rule_id="VAR-MAT-004",
            label="Purchase-basis price variance (supplement)",
            formula="purchased_qty × (SP - AP)",
            inputs={
                "purchased_qty": inputs.purchased_qty,
                "standard_price": inputs.standard_price,
                "actual_price": inputs.actual_price,
            },
            value=purchase_variance,
            unit=inputs.currency,
            source_refs=["input:purchased_qty"],
            note="Supplement only; not added to the material cost variance.",
        )
        warnings.append(
            Warning(
                "purchase_usage_basis",
                "Purchased quantity differs from quantity used; two price variances are reported.",
                "info",
                "Use the usage-basis variance to tie out to the material cost variance.",
            )
        )

    facts = [
        Fact("f_mpv", "s1", "Material price variance", price_step.value, inputs.currency),
        Fact("f_muv", "s2", "Material usage variance", usage_step.value, inputs.currency),
        Fact("f_mcv", "s3", "Material cost variance", cost_step.value, inputs.currency),
    ]
    if purchase_step is not None:
        facts.append(
            Fact(
                "f_mpv_purchase",
                "s4",
                "Purchase-basis price variance",
                purchase_step.value,
                inputs.currency,
            )
        )

    result = CalcResult(
        value=cost_variance,
        unit=inputs.currency,
        favorable=cost_variance >= ZERO,
        verdict=None,
        steps=builder.steps,
        facts=facts,
        assumptions=[
            Assumption(
                "a1", "usage_basis", "Price variance uses quantity used so it ties to the cost variance",
                "engine_default", True, confidence=Decimal("1"),
            ),
        ],
        warnings=warnings,
        rationale_template=(
            "The material price variance is {{f_mpv}} and the usage variance is {{f_muv}}; "
            "together they produce a material cost variance of {{f_mcv}}."
        ),
        meta={"product_id": inputs.product_id, "standard_qty_allowed": str(standard_qty_allowed)},
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(
        result, identities=[(cost_variance, price_variance + usage_variance)]
    )
    if not report.ok:
        raise DomainError("MAT_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
