"""Product-mix ranking under one scarce resource (DA-PM-001..004)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, div, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class MixProduct:
    product_id: str
    price: Decimal
    variable_cost: Decimal
    demand_units: Decimal
    resource_units_per_unit: Decimal = Decimal("1")
    name: str = ""


@dataclass(frozen=True)
class ProductMixInput:
    capacity_units: Decimal
    products: list[MixProduct] = field(default_factory=list)
    resource_name: str = "machine hours"
    currency: str = "INR"


def run(inputs: ProductMixInput) -> CalcResult:
    if inputs.capacity_units <= ZERO:
        raise DomainError("PM_CAPACITY_POSITIVE", "Scarce capacity must be positive.")
    if not inputs.products:
        raise DomainError("PM_PRODUCTS_REQUIRED", "At least one product is required.")

    analysis: list[dict] = []
    for product in inputs.products:
        if product.resource_units_per_unit <= ZERO:
            raise DomainError(
                "PM_RESOURCE_UNITS_POSITIVE",
                f"Resource consumption for {product.product_id} must be positive.",
            )
        contribution = product.price - product.variable_cost
        per_resource = div(contribution, product.resource_units_per_unit, "contribution per resource")
        analysis.append(
            {
                "product": product,
                "contribution": contribution,
                "per_resource": per_resource,
            }
        )
    analysis.sort(key=lambda row: (-row["per_resource"], row["product"].product_id))

    remaining = inputs.capacity_units
    allocated: list[dict] = []
    for row in analysis:
        product: MixProduct = row["product"]
        if product.demand_units <= ZERO or remaining <= ZERO:
            units = ZERO
        else:
            units = min(product.demand_units, div(remaining, product.resource_units_per_unit))
        row["units"] = units
        row["resource_used"] = mul(units, product.resource_units_per_unit)
        row["total_contribution"] = mul(units, row["contribution"])
        remaining -= row["resource_used"]
        allocated.append(row)

    total_contribution = sum((row["total_contribution"] for row in allocated), ZERO)

    builder = StepBuilder()
    for index, row in enumerate(allocated, start=1):
        product: MixProduct = row["product"]
        builder.add(
            step_id=f"p{index}_contribution",
            rule_id="DA-PM-001",
            label=f"Contribution per unit — {product.product_id}",
            formula="price - variable_cost",
            inputs={"price": product.price, "variable_cost": product.variable_cost},
            value=row["contribution"],
            unit=inputs.currency,
            source_refs=[f"product:{product.product_id}"],
        )
        builder.add(
            step_id=f"p{index}_rank",
            rule_id="DA-PM-002",
            label=f"Contribution per {inputs.resource_name} — {product.product_id}",
            formula="contribution ÷ resource_units_per_unit",
            inputs={
                "contribution": row["contribution"],
                "resource_units_per_unit": product.resource_units_per_unit,
            },
            value=row["per_resource"],
            unit=f"{inputs.currency}/scarce-unit",
            source_refs=[f"product:{product.product_id}"],
        )
        builder.add(
            step_id=f"p{index}_mix",
            rule_id="DA-PM-004",
            label=f"Allocated units — {product.product_id}",
            formula="min(demand, remaining_capacity ÷ resource_units_per_unit)",
            inputs={
                "demand_units": product.demand_units,
                "resource_units_per_unit": product.resource_units_per_unit,
            },
            value=row["units"],
            unit="units",
            source_refs=[f"product:{product.product_id}", "input:capacity_units"],
            note=(
                f"Uses {row['resource_used']} {inputs.resource_name}; contributes "
                f"{row['total_contribution']}."
            ),
        )
    total_step = builder.add(
        step_id="total",
        rule_id="DA-PM-004",
        label="Total contribution at the optimal mix",
        formula="sum(allocated units × contribution per unit)",
        inputs={"capacity_units": inputs.capacity_units},
        value=total_contribution,
        unit=inputs.currency,
        source_refs=["engine:product_mix"],
    )

    binding = [row for row in allocated if row["units"] < row["product"].demand_units]
    warnings: list[Warning] = []
    if len(binding) > 1:
        warnings.append(
            Warning(
                "multiple_binding_constraints",
                "More than one product is capacity-constrained; verify that a single resource truly binds.",
                "warning",
                "Model the second constraint explicitly; contribution-per-unit ranking is not sufficient.",
            )
        )

    mix_text = ", ".join(
        f"{row['product'].product_id}: {row['units']}" for row in allocated if row["units"] > ZERO
    )
    result = CalcResult(
        value=total_contribution,
        unit=inputs.currency,
        favorable=None,
        verdict="optimal mix",
        steps=builder.steps,
        facts=[
            Fact("f_total", "total", "Total contribution at optimal mix", total_step.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "single_scarce_resource", "One scarce resource binds; demand and resource rates are known",
                "user_explicit", True, confidence=Decimal("0.9"),
            ),
        ],
        warnings=warnings,
        rationale_template=(
            "Ranking by contribution per scarce resource produces the mix below. "
            "Total contribution at this mix is {{f_total}}."
        ),
        meta={
            "decision_type": "product_mix",
            "resource_name": inputs.resource_name,
            "capacity_units": str(inputs.capacity_units),
            "mix": mix_text,
            "slack": str(remaining),
            "ranking": [
                {
                    "product_id": row["product"].product_id,
                    "contribution_per_unit": str(row["contribution"]),
                    "contribution_per_resource": str(row["per_resource"]),
                    "allocated_units": str(row["units"]),
                }
                for row in allocated
            ],
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result)
    if not report.ok:
        raise DomainError("PM_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
