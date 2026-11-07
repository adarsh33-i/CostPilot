"""Special-order differential analysis (DA-SO-001..008)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, div, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import (
    Assumption,
    CalcResult,
    Fact,
    SensitivityPoint,
    StepBuilder,
    Warning,
)
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class SpecialOrderInput:
    order_units: Decimal
    order_price: Decimal
    variable_manufacturing_cost: Decimal
    regular_price: Decimal
    regular_variable_selling_cost: Decimal = ZERO
    incremental_fixed_cost: Decimal = ZERO
    capacity_state: str = "idle"
    idle_capacity_units: Decimal | None = None
    displaced_units: Decimal | None = None
    variable_selling_cost_avoided: bool = True
    utilization: Decimal | None = None
    confirmed: bool = True
    product_id: str = ""
    plant_id: str = ""
    period_id: str = ""
    currency: str = "INR"


def _check_inputs(inputs: SpecialOrderInput) -> None:
    if inputs.order_units <= ZERO:
        raise DomainError(
            "SO_UNITS_POSITIVE",
            "Order units must be greater than zero.",
            "Enter the order quantity as a positive number.",
        )
    if inputs.order_price <= ZERO:
        raise DomainError(
            "SO_PRICE_POSITIVE",
            "Order price must be greater than zero.",
            "Enter a positive price per unit.",
        )
    if inputs.variable_manufacturing_cost < ZERO:
        raise DomainError(
            "SO_VARIABLE_COST_NEGATIVE",
            "Variable manufacturing cost cannot be negative.",
            "Check the standard cost card for the product.",
        )
    if inputs.incremental_fixed_cost < ZERO:
        raise DomainError(
            "SO_FIXED_COST_NEGATIVE",
            "Incremental fixed cost cannot be negative.",
            "Enter the avoidable fixed cost of the order, or zero.",
        )
    if inputs.capacity_state not in {"idle", "constrained"}:
        raise DomainError(
            "SO_CAPACITY_STATE",
            "Capacity state must be 'idle' or 'constrained'.",
            "Confirm whether the order fits in spare capacity.",
        )
    if inputs.utilization is not None and not (
        ZERO <= inputs.utilization <= Decimal("1")
    ):
        raise DomainError(
            "SO_UTILIZATION_RANGE",
            "Utilization must be between zero and one.",
            "Enter utilization as a fraction, for example 0.95.",
        )


def _displaced_units(inputs: SpecialOrderInput) -> Decimal:
    if inputs.displaced_units is not None:
        return max(ZERO, inputs.displaced_units)
    if inputs.capacity_state == "idle":
        if inputs.idle_capacity_units is None:
            return ZERO
        return max(ZERO, inputs.order_units - inputs.idle_capacity_units)
    if inputs.idle_capacity_units is None:
        return inputs.order_units
    return max(ZERO, inputs.order_units - inputs.idle_capacity_units)


def _evaluate(
    inputs: SpecialOrderInput,
    displaced: Decimal,
    *,
    variable_cost_per_unit: Decimal,
    fixed: Decimal,
    regular_contribution: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    revenue = mul(inputs.order_units, inputs.order_price)
    variable = mul(inputs.order_units, variable_cost_per_unit)
    contribution = revenue - variable
    opportunity = mul(displaced, regular_contribution)
    profit = contribution - fixed - opportunity
    floor = div(variable + fixed + opportunity, inputs.order_units, "floor price")
    return revenue, contribution, opportunity, profit, floor


def run(inputs: SpecialOrderInput) -> CalcResult:
    _check_inputs(inputs)

    variable_selling_on_order = (
        ZERO if inputs.variable_selling_cost_avoided else inputs.regular_variable_selling_cost
    )
    variable_cost_per_unit = inputs.variable_manufacturing_cost + variable_selling_on_order
    regular_contribution = (
        inputs.regular_price
        - inputs.variable_manufacturing_cost
        - inputs.regular_variable_selling_cost
    )
    if regular_contribution < ZERO:
        raise DomainError(
            "SO_REGULAR_CONTRIBUTION_NEGATIVE",
            "Regular contribution is negative, so displacement has no opportunity cost.",
            "Review the regular price and standard cost card before running the decision.",
        )

    displaced = _displaced_units(inputs)
    revenue, contribution, opportunity, profit, floor = _evaluate(
        inputs,
        displaced,
        variable_cost_per_unit=variable_cost_per_unit,
        fixed=inputs.incremental_fixed_cost,
        regular_contribution=regular_contribution,
    )

    builder = StepBuilder()
    product_ref = f"standard_cost:{inputs.product_id}:{inputs.plant_id}:{inputs.period_id}"
    step_revenue = builder.add(
        step_id="s1",
        rule_id="DA-SO-001",
        label="Incremental revenue",
        formula="order_units × order_price",
        inputs={"order_units": inputs.order_units, "order_price": inputs.order_price},
        value=revenue,
        unit=inputs.currency,
        source_refs=[product_ref, "input:order_units", "input:order_price"],
    )
    step_variable = builder.add(
        step_id="s2",
        rule_id="DA-SO-002",
        label="Incremental variable cost",
        formula="order_units × variable_cost_per_unit",
        inputs={
            "order_units": inputs.order_units,
            "variable_manufacturing_cost": inputs.variable_manufacturing_cost,
            "variable_selling_cost_on_order": variable_selling_on_order,
        },
        value=-mul(inputs.order_units, variable_cost_per_unit),
        unit=inputs.currency,
        source_refs=[product_ref],
    )
    step_contribution = builder.add(
        step_id="s3",
        rule_id="DA-SO-003",
        label="Incremental contribution",
        formula="s1 + s2",
        inputs={"s1": step_revenue.value, "s2": step_variable.value},
        value=contribution,
        unit=inputs.currency,
        source_refs=[product_ref],
    )
    step_fixed = builder.add(
        step_id="s4",
        rule_id="DA-SO-004",
        label="Incremental fixed cost",
        formula="incremental_fixed_cost",
        inputs={"incremental_fixed_cost": inputs.incremental_fixed_cost},
        value=inputs.incremental_fixed_cost,
        unit=inputs.currency,
        source_refs=["input:incremental_fixed_cost"],
    )
    step_opportunity = builder.add(
        step_id="s5",
        rule_id="DA-SO-005",
        label="Opportunity cost of displaced sales",
        formula="displaced_units × regular_contribution_per_unit",
        inputs={
            "displaced_units": displaced,
            "regular_contribution_per_unit": regular_contribution,
        },
        value=opportunity,
        unit=inputs.currency,
        source_refs=["input:capacity_state", "input:displaced_units", product_ref],
        note="Allocated existing fixed overhead is excluded as sunk and unavoidable.",
    )
    step_profit = builder.add(
        step_id="s6",
        rule_id="DA-SO-006",
        label="Incremental profit",
        formula="s3 - s4 - s5",
        inputs={
            "s3": step_contribution.value,
            "s4": step_fixed.value,
            "s5": step_opportunity.value,
        },
        value=profit,
        unit=inputs.currency,
        source_refs=[product_ref],
    )
    step_floor = builder.add(
        step_id="s7",
        rule_id="DA-SO-007",
        label="Minimum acceptable price",
        formula="(variable cost + incremental fixed + opportunity cost) ÷ order_units",
        inputs={
            "variable_cost": mul(inputs.order_units, variable_cost_per_unit),
            "incremental_fixed": inputs.incremental_fixed_cost,
            "opportunity_cost": opportunity,
            "order_units": inputs.order_units,
        },
        value=floor,
        unit=f"{inputs.currency}/unit",
        source_refs=[product_ref, "input:order_units"],
    )

    warnings: list[Warning] = []
    if inputs.capacity_state == "idle" and inputs.idle_capacity_units is None:
        warnings.append(
            Warning(
                code="capacity_unquantified",
                message="Idle capacity was asserted but no spare-capacity quantity was supplied.",
                severity="warning",
                remediation="Provide the confirmed idle units, or treat the order as constrained.",
            )
        )
    if inputs.utilization is not None and inputs.utilization >= Decimal("0.95"):
        warnings.append(
            Warning(
                code="capacity_cross_check",
                message="Reported utilization is at or above 95%; the idle-capacity claim needs evidence.",
                severity="warning",
                remediation="Attach the capacity report or confirm displaced units explicitly.",
            )
        )
    if displaced > ZERO:
        warnings.append(
            Warning(
                code="displacement",
                message="The order displaces regular sales, so opportunity cost is included.",
                severity="info",
            )
        )

    assumptions = [
        Assumption(
            id="a1",
            key="capacity_state",
            text="Idle capacity exists for the order window",
            source="user_explicit" if inputs.capacity_state == "idle" else "registry",
            confirmed=inputs.confirmed,
            evidence=(
                f"{inputs.idle_capacity_units} idle units"
                if inputs.idle_capacity_units is not None
                else "qualitative confirmation"
            ),
            confidence=Decimal("1") if inputs.confirmed else Decimal("0.5"),
        ),
        Assumption(
            id="a2",
            key="displacement",
            text="No regular sales are displaced" if displaced == ZERO else "Regular sales are displaced",
            source="user_explicit" if inputs.displaced_units is not None else "user_implied",
            confirmed=inputs.confirmed,
            evidence=f"{displaced} displaced units",
            confidence=Decimal("1") if inputs.displaced_units is not None else Decimal("0.7"),
        ),
        Assumption(
            id="a3",
            key="variable_selling_cost_avoided",
            text="Variable selling cost is avoided on the order",
            source="policy_default",
            confirmed=inputs.confirmed,
            evidence="special-order export treatment",
            confidence=Decimal("0.8"),
        ),
        Assumption(
            id="a4",
            key="allocated_fixed_excluded",
            text="Allocated existing fixed overhead is excluded from the differential",
            source="engine_default",
            confirmed=True,
            evidence="sunk and unavoidable",
            confidence=Decimal("1"),
        ),
        Assumption(
            id="a5",
            key="incremental_fixed_avoidable",
            text="The stated incremental fixed cost is avoidable and specific to the order",
            source="user_explicit",
            confirmed=inputs.confirmed,
            confidence=Decimal("1") if inputs.confirmed else Decimal("0.5"),
        ),
    ]

    # Capacity sensitivity is always computed. Half-order coverage reproduces the
    # canonical 1,000-unit displacement case from the blueprint.
    scenarios: list[tuple[str, Decimal]] = [
        ("If no idle capacity", ZERO),
        (
            "If idle capacity were half the order",
            div(inputs.order_units, Decimal("2"), "sensitivity coverage"),
        ),
        ("If idle capacity covers the order", inputs.order_units),
    ]
    sensitivity: list[SensitivityPoint] = []
    seen: set[Decimal] = set()
    for label, coverage in scenarios:
        if coverage in seen:
            continue
        seen.add(coverage)
        scenario_displaced = max(ZERO, inputs.order_units - coverage)
        _, _, scenario_opportunity, scenario_profit, scenario_floor = _evaluate(
            inputs,
            scenario_displaced,
            variable_cost_per_unit=variable_cost_per_unit,
            fixed=inputs.incremental_fixed_cost,
            regular_contribution=regular_contribution,
        )
        sensitivity.append(
            SensitivityPoint(
                label=label,
                value=scenario_profit,
                unit=inputs.currency,
                favorable=scenario_profit > ZERO,
                note=f"Minimum price {scenario_floor:.2f} per unit",
                context={
                    "idle_capacity_units": str(coverage),
                    "displaced_units": str(scenario_displaced),
                    "opportunity_cost": str(scenario_opportunity),
                    "minimum_price": f"{scenario_floor:.2f}",
                },
            )
        )

    if profit > ZERO and not any(item.severity == "blocking" for item in warnings):
        verdict = "accept, conditionally"
    elif profit < ZERO:
        verdict = "reject"
    else:
        verdict = "investigate"

    conditions = [
        "Idle capacity is real for the order window.",
        "No regular sales are displaced.",
        "The price does not become a precedent for regular customers.",
        "The order passes quality, regulatory, and working-capital checks.",
    ]

    rationale_template = (
        "The order adds {{f_contribution}} of incremental contribution. After "
        "{{f_fixed}} of incremental fixed cost and {{f_opportunity}} of opportunity cost, "
        "incremental profit is {{f_profit}}. The minimum acceptable price is {{f_floor}} per unit."
    )
    result = CalcResult(
        value=profit,
        unit=inputs.currency,
        favorable=profit > ZERO,
        verdict=verdict,
        steps=builder.steps,
        facts=[
            Fact("f_profit", "s6", "Incremental profit", step_profit.value, inputs.currency),
            Fact("f_contribution", "s3", "Incremental contribution", step_contribution.value, inputs.currency),
            Fact("f_revenue", "s1", "Incremental revenue", step_revenue.value, inputs.currency),
            Fact("f_fixed", "s4", "Incremental fixed cost", step_fixed.value, inputs.currency),
            Fact("f_opportunity", "s5", "Opportunity cost", step_opportunity.value, inputs.currency),
            Fact("f_floor", "s7", "Minimum acceptable price", step_floor.value, step_floor.unit),
        ],
        assumptions=assumptions,
        warnings=warnings,
        sensitivity=sensitivity,
        conditions=conditions,
        rationale_template=rationale_template,
        rationale="",
        meta={
            "decision_type": "special_order",
            "product_id": inputs.product_id,
            "plant_id": inputs.plant_id,
            "period_id": inputs.period_id,
            "resolved_inputs": {
                "order_units": str(inputs.order_units),
                "order_price": str(inputs.order_price),
                "variable_manufacturing_cost": str(inputs.variable_manufacturing_cost),
                "regular_price": str(inputs.regular_price),
                "regular_variable_selling_cost": str(inputs.regular_variable_selling_cost),
                "variable_cost_on_order": str(variable_cost_per_unit),
                "incremental_fixed_cost": str(inputs.incremental_fixed_cost),
                "capacity_state": inputs.capacity_state,
                "idle_capacity_units": None
                if inputs.idle_capacity_units is None
                else str(inputs.idle_capacity_units),
                "displaced_units": str(displaced),
                "utilization": None if inputs.utilization is None else str(inputs.utilization),
            },
        },
    )
    result.rationale = render(rationale_template, result)
    report = validate_result(
        result,
        identities=[
            (contribution, revenue - mul(inputs.order_units, variable_cost_per_unit)),
            (profit, contribution - inputs.incremental_fixed_cost - opportunity),
            (
                mul(floor, inputs.order_units),
                mul(inputs.order_units, variable_cost_per_unit)
                + inputs.incremental_fixed_cost
                + opportunity,
            ),
        ],
    )
    if not report.ok:
        raise DomainError("SO_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
