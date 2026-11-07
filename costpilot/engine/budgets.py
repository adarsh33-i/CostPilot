"""Master budget chain, scenarios, rolling forecast and forecast-quality checks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from costpilot.data.loader import Snapshot
from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, div, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import CalcResult, Fact, SensitivityPoint, StepBuilder, Warning
from costpilot.engine.validation import validate_result


@dataclass
class BudgetChain:
    sales: Decimal
    production_units: Decimal
    material_usage: Decimal
    purchases: Decimal
    material_cost: Decimal
    labour: Decimal
    variable_oh: Decimal
    fixed_oh: Decimal
    selling: Decimal
    operating_income: Decimal
    collections: Decimal
    payments: Decimal
    tax: Decimal
    opening_cash: Decimal
    closing_cash: Decimal
    products: list[dict[str, Any]]


def _plants(snapshot: Snapshot, plant_id: str | None) -> list[str]:
    if plant_id:
        if not any(row["plant_id"] == plant_id for row in snapshot.plants):
            raise DomainError(
                "BUD_PLANT_UNKNOWN",
                f"Plant {plant_id} is not in the snapshot.",
                "Choose one of: " + ", ".join(row["plant_id"] for row in snapshot.plants) + ".",
            )
        return [plant_id]
    return [row["plant_id"] for row in snapshot.plants]


def _products(snapshot: Snapshot, period_id: str, plant_id: str) -> list[str]:
    return sorted(
        product for (period, plant, product) in snapshot.standards
        if period == period_id and plant == plant_id
    )


def _chain(
    snapshot: Snapshot,
    period_id: str,
    plants: list[str],
    overlay: dict[str, Decimal] | None = None,
) -> BudgetChain:
    overlay = overlay or {}
    volume_factor = Decimal("1") + overlay.get("volume", ZERO)
    price_factor = Decimal("1") + overlay.get("price", ZERO)
    material_factor = Decimal("1") + overlay.get("material_price", ZERO)
    labour_factor = Decimal("1") + overlay.get("labour_rate", ZERO)
    overhead_factor = Decimal("1") + overlay.get("overhead", ZERO)

    total = {key: ZERO for key in (
        "sales", "production_units", "material_usage", "purchases", "material_cost",
        "labour", "variable_oh", "fixed_oh", "selling", "operating_income",
        "collections", "payments", "tax", "opening_cash", "closing_cash",
    )}
    product_rows: list[dict[str, Any]] = []
    for plant in plants:
        cash = snapshot.budget_cash.get((period_id, plant))
        if cash is None:
            raise DomainError(
                "BUD_CASH_MISSING",
                f"No cash assumptions for {period_id} / {plant}.",
                "Load budget_cash.csv with the opening cash and collection/payment rates.",
            )
        plant_open = cash.opening_cash
        plant_sales = ZERO
        plant_payments_base = ZERO
        for product in _products(snapshot, period_id, plant):
            assumption = snapshot.budget_assumptions.get((period_id, plant, product))
            if assumption is None:
                raise DomainError(
                    "BUD_ASSUMPTION_MISSING",
                    f"No budget assumption for {period_id} / {plant} / {product}.",
                    "Load budget_assumptions.csv for every product-plant-period.",
                )
            standard = snapshot.standard(period_id, plant, product)
            sales_units = mul(assumption.sales_units, volume_factor)
            price = mul(standard.price, price_factor)
            production = sales_units + assumption.ending_fg_units - assumption.beginning_fg_units
            usage = mul(production, standard.std_material_qty)
            purchases = usage + assumption.ending_rm_qty - assumption.beginning_rm_qty
            material_cost = mul(mul(purchases, standard.std_material_price), material_factor)
            labour = mul(mul(mul(production, standard.std_labor_hours), standard.std_labor_rate), labour_factor)
            variable_oh = mul(mul(production, standard.variable_oh), overhead_factor)
            fixed_oh = mul(mul(standard.budget_volume, standard.fixed_oh_rate), overhead_factor)
            selling = mul(sales_units, standard.variable_selling)
            operating_income = (
                mul(sales_units, price) - material_cost - labour - variable_oh - fixed_oh - selling
            )
            total["sales"] += mul(sales_units, price)
            total["production_units"] += production
            total["material_usage"] += usage
            total["purchases"] += purchases
            total["material_cost"] += material_cost
            total["labour"] += labour
            total["variable_oh"] += variable_oh
            total["fixed_oh"] += fixed_oh
            total["selling"] += selling
            total["operating_income"] += operating_income
            plant_sales += mul(sales_units, price)
            plant_payments_base += material_cost + labour + variable_oh + fixed_oh
            product_rows.append(
                {
                    "product_id": product,
                    "plant_id": plant,
                    "sales_units": sales_units,
                    "price": price,
                    "production_units": production,
                    "material_usage": usage,
                    "purchases": purchases,
                    "material_cost": material_cost,
                    "labour": labour,
                    "variable_oh": variable_oh,
                    "fixed_oh": fixed_oh,
                    "selling": selling,
                    "operating_income": operating_income,
                }
            )
        collections = mul(plant_sales, cash.collection_rate)
        payments = mul(plant_payments_base, cash.payment_rate) + cash.capex
        tax = mul(max(ZERO, total["operating_income"]), cash.tax_rate)
        closing = plant_open + collections - payments - tax
        total["collections"] += collections
        total["payments"] += payments
        total["tax"] += tax
        total["opening_cash"] += plant_open
        total["closing_cash"] += closing

    return BudgetChain(**total, products=product_rows)


def _float_ok(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def run_budget_chain(
    snapshot: Snapshot,
    *,
    period_id: str,
    plant_id: str | None = None,
) -> CalcResult:
    if not snapshot.budget_assumptions:
        raise DomainError(
            "BUD_ASSUMPTIONS_MISSING",
            "Budget assumptions are not loaded.",
            "Load budget_assumptions.csv and budget_cash.csv.",
        )
    plants = _plants(snapshot, plant_id)
    chain = _chain(snapshot, period_id, plants)
    builder = StepBuilder()
    currency = snapshot.currency
    builder.add(
        step_id="bud_sales", rule_id="BUD-001", label="Sales budget",
        formula="sales_units x standard_price",
        inputs={"plants": len(plants)},
        value=chain.sales, unit=currency,
        source_refs=["budget_assumptions.csv", "standard_costs.csv"],
    )
    builder.add(
        step_id="bud_production", rule_id="BUD-002", label="Production budget",
        formula="sales_units + ending_fg_units - beginning_fg_units",
        inputs={"products": len(chain.products)},
        value=chain.production_units, unit="units",
        source_refs=["budget_assumptions.csv"],
    )
    builder.add(
        step_id="bud_usage", rule_id="BUD-003", label="Material usage budget",
        formula="production_units x std_material_qty",
        inputs={"products": len(chain.products)},
        value=chain.material_usage, unit="units",
        source_refs=["budget_assumptions.csv", "standard_costs.csv"],
    )
    builder.add(
        step_id="bud_purchases", rule_id="BUD-004", label="Material purchases budget",
        formula="usage + ending_rm_qty - beginning_rm_qty",
        inputs={"usage": chain.material_usage},
        value=chain.purchases, unit="units",
        source_refs=["budget_assumptions.csv"],
    )
    builder.add(
        step_id="bud_labour", rule_id="BUD-005", label="Direct labour budget",
        formula="production_units x std_labor_hours x std_labor_rate",
        inputs={"production_units": chain.production_units},
        value=chain.labour, unit=currency,
        source_refs=["standard_costs.csv"],
    )
    builder.add(
        step_id="bud_overhead", rule_id="BUD-006", label="Manufacturing overhead budget",
        formula="production_units x variable_oh + budget_volume x fixed_oh_rate",
        inputs={"variable_oh": chain.variable_oh, "fixed_oh": chain.fixed_oh},
        value=chain.variable_oh + chain.fixed_oh, unit=currency,
        source_refs=["standard_costs.csv"],
    )
    builder.add(
        step_id="bud_material_cost", rule_id="BUD-004", label="Material purchase cost",
        formula="purchases x standard_material_price",
        inputs={"purchases": chain.purchases},
        value=chain.material_cost, unit=currency,
        source_refs=["standard_costs.csv"],
    )
    builder.add(
        step_id="bud_oi", rule_id="BUD-010", label="Master budget operating income",
        formula="sales - material - labour - overhead - selling",
        inputs={
            "sales": chain.sales,
            "material": chain.material_cost,
            "labour": chain.labour,
            "overhead": chain.variable_oh + chain.fixed_oh,
            "selling": chain.selling,
        },
        value=chain.operating_income, unit=currency,
        source_refs=["budget_assumptions.csv", "standard_costs.csv"],
    )
    builder.add(
        step_id="bud_collections", rule_id="BUD-007", label="Cash collections",
        formula="sales x collection_rate",
        inputs={"sales": chain.sales},
        value=chain.collections, unit=currency,
        source_refs=["budget_cash.csv"],
    )
    builder.add(
        step_id="bud_payments", rule_id="BUD-008", label="Cash payments",
        formula="(material + labour + overhead) x payment_rate + capex",
        inputs={"payments": chain.payments},
        value=chain.payments, unit=currency,
        source_refs=["budget_cash.csv"],
    )
    builder.add(
        step_id="bud_closing_cash", rule_id="BUD-009", label="Closing cash",
        formula="opening_cash + collections - payments - tax",
        inputs={
            "opening_cash": chain.opening_cash,
            "collections": chain.collections,
            "payments": chain.payments,
            "tax": chain.tax,
        },
        value=chain.closing_cash, unit=currency,
        source_refs=["budget_cash.csv"],
    )

    facts = [
        Fact("f_bud_sales", "bud_sales", "Budgeted sales", chain.sales, currency),
        Fact("f_bud_production", "bud_production", "Budgeted production", chain.production_units, "units"),
        Fact("f_bud_material", "bud_material_cost", "Material purchase cost", chain.material_cost, currency),
        Fact("f_bud_labour", "bud_labour", "Direct labour cost", chain.labour, currency),
        Fact("f_bud_overhead", "bud_overhead", "Manufacturing overhead", chain.variable_oh + chain.fixed_oh, currency),
        Fact("f_bud_oi", "bud_oi", "Budgeted operating income", chain.operating_income, currency),
        Fact("f_bud_closing_cash", "bud_closing_cash", "Budgeted closing cash", chain.closing_cash, currency),
    ]
    result = CalcResult(
        value=chain.operating_income,
        unit=currency,
        favorable=chain.operating_income >= ZERO,
        verdict=(
            "The master budget produces positive operating income and a closing cash balance "
            "after capital expenditure."
            if chain.operating_income >= ZERO and chain.closing_cash >= ZERO
            else "The master budget leaves operating income or closing cash below zero; revise the assumptions."
        ),
        steps=builder.steps,
        facts=facts,
        warnings=[
            Warning(
                "BUD_CASH_BUFFER",
                f"Closing cash is {chain.closing_cash}; keep at least one month of payments as a buffer."
                if chain.closing_cash < chain.payments * Decimal("0.5")
                else f"Closing cash is {chain.closing_cash}, above half of monthly payments.",
                "warning" if chain.closing_cash < chain.payments * Decimal("0.5") else "info",
                "Reduce capex or accelerate collections if the buffer is thin.",
            )
        ],
        sensitivity=[
            SensitivityPoint(
                label="Operating income at +5% volume",
                value=_chain(snapshot, period_id, plants, {"volume": Decimal("0.05")}).operating_income,
                unit=currency,
                favorable=True,
                note="Full master-budget rebuild at five percent more volume.",
            ),
            SensitivityPoint(
                label="Operating income at +5% material price",
                value=_chain(snapshot, period_id, plants, {"material_price": Decimal("0.05")}).operating_income,
                unit=currency,
                favorable=False,
                note="Full master-budget rebuild at five percent higher material cost.",
            ),
        ],
        rationale_template=(
            "The master budget links sales of {{f_bud_sales}} to production of {{f_bud_production}}, "
            "material purchases of {{f_bud_material}}, labour of {{f_bud_labour}} and overhead of "
            "{{f_bud_overhead}}, producing operating income of {{f_bud_oi}} and closing cash of "
            "{{f_bud_closing_cash}}."
        ),
        meta={
            "period_id": period_id,
            "plant_id": plant_id or "ALL",
            "sales": str(chain.sales),
            "production_units": str(chain.production_units),
            "material_usage": str(chain.material_usage),
            "purchases": str(chain.purchases),
            "material_cost": str(chain.material_cost),
            "labour": str(chain.labour),
            "variable_oh": str(chain.variable_oh),
            "fixed_oh": str(chain.fixed_oh),
            "selling": str(chain.selling),
            "operating_income": str(chain.operating_income),
            "collections": str(chain.collections),
            "payments": str(chain.payments),
            "tax": str(chain.tax),
            "opening_cash": str(chain.opening_cash),
            "closing_cash": str(chain.closing_cash),
            "products": [
                {key: str(value) for key, value in row.items() if isinstance(value, Decimal)}
                | {"product_id": row["product_id"], "plant_id": row["plant_id"]}
                for row in chain.products
            ],
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(
        result,
        identities=[
            (chain.operating_income, chain.sales - chain.material_cost - chain.labour
             - chain.variable_oh - chain.fixed_oh - chain.selling)
        ],
    )
    if not report.ok:
        raise DomainError("BUD_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result


SCENARIO_KEYS = {"volume", "price", "material_price", "labour_rate", "overhead"}


def run_budget_scenario(
    snapshot: Snapshot,
    *,
    period_id: str,
    plant_id: str | None = None,
    overlay: dict[str, str | Decimal] | None = None,
    label: str = "Scenario",
) -> CalcResult:
    overlay = {key: _float_ok(value) for key, value in (overlay or {}).items()}
    unknown = sorted(set(overlay) - SCENARIO_KEYS)
    if unknown:
        raise DomainError(
            "BUD_SCENARIO_KEY",
            f"Unknown scenario drivers: {', '.join(unknown)}.",
            "Use volume, price, material_price, labour_rate or overhead.",
        )
    out_of_range = sorted(
        f"{key}={value}" for key, value in overlay.items() if abs(value) > 1
    )
    if out_of_range:
        raise DomainError(
            "BUD_SCENARIO_RANGE",
            "Scenario drivers must be fractions; " + ", ".join(out_of_range) + " exceed 100%.",
            "Send 0.03 for a three percent rise, or express the change as a percentage in the question.",
        )
    plants = _plants(snapshot, plant_id)
    baseline = _chain(snapshot, period_id, plants)
    scenario = _chain(snapshot, period_id, plants, overlay)
    builder = StepBuilder()
    currency = snapshot.currency
    builder.add(
        step_id="scenario_baseline", rule_id="BUD-010", label="Baseline operating income",
        formula="master budget baseline",
        inputs={"plants": len(plants)}, value=baseline.operating_income, unit=currency,
        source_refs=["budget_assumptions.csv"],
    )
    builder.add(
        step_id="scenario_oi", rule_id="BUD-012", label=f"{label} operating income",
        formula="base_value x (1 + overlay_factor) at each driver",
        inputs={key: value for key, value in overlay.items()} or {"overlay": "none"},
        value=scenario.operating_income, unit=currency,
        source_refs=["budget_assumptions.csv", "standard_costs.csv"],
    )
    builder.add(
        step_id="scenario_delta", rule_id="BUD-013", label="Scenario variance",
        formula="scenario - baseline",
        inputs={"baseline": baseline.operating_income, "scenario": scenario.operating_income},
        value=scenario.operating_income - baseline.operating_income, unit=currency,
        source_refs=["budget_assumptions.csv"],
    )
    delta = scenario.operating_income - baseline.operating_income
    result = CalcResult(
        value=scenario.operating_income,
        unit=currency,
        favorable=delta >= ZERO,
        verdict=(
            f"{label} changes operating income by "
            f"{'an increase' if delta >= ZERO else 'a decrease'} versus the baseline."
        ),
        steps=builder.steps,
        facts=[
            Fact("f_scn_baseline", "scenario_baseline", "Baseline operating income", baseline.operating_income, currency),
            Fact("f_scn_oi", "scenario_oi", f"{label} operating income", scenario.operating_income, currency),
            Fact("f_scn_delta", "scenario_delta", "Scenario variance", delta, currency),
        ],
        warnings=[] if overlay else [
            Warning("BUD_SCENARIO_EMPTY", "No overlay drivers were supplied; the scenario equals the baseline.", "info")
        ],
        rationale_template=(
            "The baseline operating income is {{f_scn_baseline}}; under the scenario it becomes "
            "{{f_scn_oi}}, a change of {{f_scn_delta}}."
        ),
        meta={
            "period_id": period_id,
            "plant_id": plant_id or "ALL",
            "label": label,
            "overlay": {key: str(value) for key, value in overlay.items()},
            "baseline": str(baseline.operating_income),
            "scenario": str(scenario.operating_income),
            "delta": str(delta),
            "scenario_cash": str(scenario.closing_cash),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(delta, scenario.operating_income - baseline.operating_income)])
    if not report.ok:
        raise DomainError("BUD_SCENARIO_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result


def _actual_income(snapshot: Snapshot, period_id: str, plants: list[str]) -> tuple[Decimal, Decimal]:
    revenue = ZERO
    costs = ZERO
    for (period, plant, product), actual in snapshot.actuals.items():
        if period != period_id or plant not in plants:
            continue
        standard = snapshot.standard(period_id, plant, product)
        revenue += mul(actual.output_units, standard.price)
        costs += (
            mul(actual.material_qty_used, actual.material_price)
            + mul(actual.labor_hours, actual.labor_rate)
            + actual.variable_oh_actual + actual.fixed_oh_actual
            + mul(actual.output_units, standard.variable_selling)
        )
    return revenue, revenue - costs


def run_rolling_forecast(
    snapshot: Snapshot,
    *,
    plant_id: str | None = None,
) -> CalcResult:
    plants = _plants(snapshot, plant_id)
    closed = [row["period_id"] for row in snapshot.periods if row.get("status") == "closed"]
    open_periods = [row["period_id"] for row in snapshot.periods if row.get("status") != "closed"]
    if not closed or not open_periods:
        raise DomainError(
            "BUD_ROLLING_PERIODS",
            "A rolling forecast needs at least one closed and one open period.",
            "Mark periods as closed or open in periods.csv.",
        )
    actual_revenue = ZERO
    actual_income = ZERO
    for period_id in closed:
        revenue, income = _actual_income(snapshot, period_id, plants)
        actual_revenue += revenue
        actual_income += income
    remaining_revenue = ZERO
    remaining_income = ZERO
    for period_id in open_periods:
        chain = _chain(snapshot, period_id, plants)
        remaining_revenue += chain.sales
        remaining_income += chain.operating_income
    forecast_income = actual_income + remaining_income
    full_budget_income = ZERO
    for period_row in snapshot.periods:
        full_budget_income += _chain(snapshot, period_row["period_id"], plants).operating_income

    builder = StepBuilder()
    currency = snapshot.currency
    builder.add(
        step_id="rf_actual", rule_id="BUD-011", label="Actual to date",
        formula="sum(closed-period actual operating income)",
        inputs={"periods": len(closed)}, value=actual_income, unit=currency,
        source_refs=["production_actuals.csv"],
    )
    builder.add(
        step_id="rf_remaining", rule_id="BUD-011", label="Remaining budget",
        formula="sum(open-period budget operating income)",
        inputs={"periods": len(open_periods)}, value=remaining_income, unit=currency,
        source_refs=["budget_assumptions.csv"],
    )
    builder.add(
        step_id="rf_forecast", rule_id="BUD-011", label="Rolling forecast",
        formula="actual_to_date + remaining_budget",
        inputs={"actual_to_date": actual_income, "remaining_budget": remaining_income},
        value=forecast_income, unit=currency,
        source_refs=["production_actuals.csv", "budget_assumptions.csv"],
    )
    builder.add(
        step_id="rf_full_budget", rule_id="BUD-013", label="Full-year static budget",
        formula="sum(all-period budget operating income)",
        inputs={"periods": len(snapshot.periods)},
        value=full_budget_income, unit=currency,
        source_refs=["budget_assumptions.csv"],
    )
    delta = forecast_income - full_budget_income
    result = CalcResult(
        value=forecast_income,
        unit=currency,
        favorable=delta >= ZERO,
        verdict=(
            "The rolling forecast lands above the static budget."
            if delta >= ZERO else "The rolling forecast lands below the static budget."
        ),
        steps=builder.steps,
        facts=[
            Fact("f_rf_actual", "rf_actual", "Actual to date", actual_income, currency),
            Fact("f_rf_remaining", "rf_remaining", "Remaining budget", remaining_income, currency),
            Fact("f_rf_forecast", "rf_forecast", "Rolling forecast income", forecast_income, currency),
            Fact("f_rf_delta", "rf_full_budget", "Static budget income", full_budget_income, currency),
        ],
        warnings=[
            Warning(
                "BUD_ROLLING_METHOD",
                "The rolling forecast keeps actuals for closed periods and the approved budget for open periods.",
                "info",
            )
        ],
        rationale_template=(
            "Actual-to-date income of {{f_rf_actual}} plus the remaining budget of {{f_rf_remaining}} "
            "gives a rolling forecast of {{f_rf_forecast}}, compared with a static budget of {{f_rf_delta}}."
        ),
        meta={
            "plant_id": plant_id or "ALL",
            "closed_periods": closed,
            "open_periods": open_periods,
            "actual_revenue": str(actual_revenue),
            "remaining_revenue": str(remaining_revenue),
            "actual_income": str(actual_income),
            "remaining_income": str(remaining_income),
            "forecast_income": str(forecast_income),
            "static_budget_income": str(full_budget_income),
            "variance": str(delta),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(forecast_income, actual_income + remaining_income)])
    if not report.ok:
        raise DomainError("BUD_ROLLING_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result


BIAS_THRESHOLD = Decimal("0.05")


def run_forecast_quality(snapshot: Snapshot, *, plant_id: str | None = None) -> CalcResult:
    plants = _plants(snapshot, plant_id)
    closed = [row["period_id"] for row in snapshot.periods if row.get("status") == "closed"]
    builder = StepBuilder()
    samples: list[dict[str, Any]] = []
    sandbagged = 0
    for period_id in closed:
        for plant in plants:
            for product in _products(snapshot, period_id, plant):
                budget = snapshot.budget(period_id, plant, product)
                actual = snapshot.actual(period_id, plant, product)
                actual_revenue = mul(actual.output_units, snapshot.standard(period_id, plant, product).price)
                if actual_revenue == ZERO:
                    continue
                bias = div(budget.revenue - actual_revenue, actual_revenue, "forecast bias")
                flag = bias > BIAS_THRESHOLD
                sandbagged += 1 if flag else 0
                samples.append(
                    {
                        "period_id": period_id,
                        "plant_id": plant,
                        "product_id": product,
                        "budget_revenue": budget.revenue,
                        "actual_revenue": actual_revenue,
                        "bias": bias,
                        "sandbagging": flag,
                    }
                )
    if not samples:
        raise DomainError(
            "BUD_FORECAST_SAMPLE",
            "No closed-period budget-to-actual pairs are available.",
            "Close at least one period with both budgets and actuals.",
        )
    for index, sample in enumerate(samples):
        builder.add(
            step_id=f"bias_{index}",
            rule_id="BUD-014",
            label=f"{sample['product_id']} forecast bias",
            formula="(budget_revenue - actual_revenue) / actual_revenue",
            inputs={
                "budget_revenue": sample["budget_revenue"],
                "actual_revenue": sample["actual_revenue"],
            },
            value=sample["bias"],
            unit="ratio",
            source_refs=[f"budgets.csv:{sample['period_id']}/{sample['plant_id']}/{sample['product_id']}"],
        )
    builder.add(
        step_id="bias_count",
        rule_id="BUD-014",
        label="Sandbagging flags",
        formula="count(bias > threshold)",
        inputs={"samples": len(samples), "threshold": BIAS_THRESHOLD},
        value=Decimal(sandbagged),
        unit="units",
        source_refs=["budgets.csv", "production_actuals.csv"],
    )
    largest = max(samples, key=lambda row: abs(row["bias"]))
    result = CalcResult(
        value=Decimal(sandbagged),
        unit="units",
        favorable=sandbagged == 0,
        verdict=(
            f"{sandbagged} of {len(samples)} budget lines are more than five percent below actual, "
            "a sandbagging signal."
            if sandbagged else "No budget line shows a sandbagging bias above five percent."
        ),
        steps=builder.steps,
        facts=[
            Fact("f_bias_count", "bias_count", "Sandbagging flags", Decimal(sandbagged), "units"),
            Fact(
                "f_bias_largest",
                f"bias_{samples.index(largest)}",
                "Largest forecast bias",
                largest["bias"],
                "ratio",
            ),
        ],
        warnings=[
            Warning(
                "BUD_SANDBAGGING",
                f"Largest under-forecast is {largest['product_id']} at {largest['bias']:.2%}.",
                "warning" if sandbagged else "info",
                "Ask the owner to explain persistent under-forecasting before accepting the plan.",
            )
        ],
        rationale_template=(
            "Forecast quality compares approved budgets with actual revenue; {{f_bias_count}} lines "
            "show more than five percent under-forecast, and the largest bias is {{f_bias_largest}}."
        ),
        meta={
            "plant_id": plant_id or "ALL",
            "threshold": str(BIAS_THRESHOLD),
            "samples": [
                {
                    **{
                        key: (str(value) if isinstance(value, Decimal) else value)
                        for key, value in sample.items()
                    }
                }
                for sample in samples
            ],
            "sandbagging_flags": sandbagged,
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result)
    if not report.ok:
        raise DomainError("BUD_FORECAST_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
