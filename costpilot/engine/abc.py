"""Activity-based and time-driven ABC, plus customer profitability."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from costpilot.data.loader import Snapshot
from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, div, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import CalcResult, Fact, SensitivityPoint, StepBuilder, Warning
from costpilot.engine.validation import validate_result

CROSS_SUBSIDY_THRESHOLD = Decimal("0.10")


def _slug(value: str) -> str:
    return value.lower().replace("-", "_")


def _require_pools(snapshot: Snapshot, period_id: str, plant_id: str) -> None:
    pools = [p for key, p in snapshot.cost_pools.items() if key[0] == period_id and key[1] == plant_id]
    if not pools:
        raise DomainError(
            "ABC_POOLS_MISSING",
            f"No activity cost pools for {period_id} / {plant_id}.",
            "Load activities.csv, cost_pools.csv and driver_events.csv for this period and plant.",
        )


@dataclass
class ProductCosting:
    product_id: str
    output_units: Decimal
    direct_material: Decimal
    direct_labour: Decimal
    abc_overhead: Decimal
    traditional_overhead: Decimal
    abc_unit_cost: Decimal
    traditional_unit_cost: Decimal
    price: Decimal
    abc_margin: Decimal
    distortion: Decimal
    distortion_ratio: Decimal
    flagged: bool
    activity_cost: dict[str, Decimal] = field(default_factory=dict)


def _product_costing(
    snapshot: Snapshot, period_id: str, plant_id: str, basis: str
) -> tuple[list[ProductCosting], list, Decimal, Decimal]:
    _require_pools(snapshot, period_id, plant_id)
    pools = [
        pool for key, pool in sorted(snapshot.cost_pools.items())
        if key[0] == period_id and key[1] == plant_id
    ]
    product_ids = sorted(
        product for (period, plant, product) in snapshot.actuals
        if period == period_id and plant == plant_id
    )
    actuals = {p: snapshot.actual(period_id, plant_id, p) for p in product_ids}
    standards = {p: snapshot.standard(period_id, plant_id, p) for p in product_ids}
    total_overhead = sum((pool.pool_cost for pool in pools), ZERO)
    recorded = snapshot.plant_overhead(period_id, plant_id)

    if basis not in {"labor_hours", "machine_hours"}:
        raise DomainError(
            "ABC_BASIS",
            "Traditional allocation basis must be 'labor_hours' or 'machine_hours'.",
            "Choose labour hours or machine hours for the traditional comparison.",
        )
    total_base = sum(
        (actual.labor_hours if basis == "labor_hours" else actual.actual_machine_hours)
        for actual in actuals.values()
    )
    traditional_rate = div(total_overhead, total_base, "traditional overhead rate")

    rows: list[ProductCosting] = []
    for product_id in product_ids:
        actual = actuals[product_id]
        standard = standards[product_id]
        activity_cost: dict[str, Decimal] = {}
        for pool in pools:
            event = snapshot.driver_events.get((period_id, plant_id, product_id, pool.activity_id))
            quantity = event.driver_quantity if event else ZERO
            activity_cost[pool.pool_id] = mul(quantity, pool.rate)
        abc_overhead = sum(activity_cost.values(), ZERO)
        base_quantity = actual.labor_hours if basis == "labor_hours" else actual.actual_machine_hours
        traditional_overhead = mul(traditional_rate, base_quantity)
        direct_material = mul(actual.material_qty_used, actual.material_price)
        direct_labour = mul(actual.labor_hours, actual.labor_rate)
        abc_unit_cost = div(direct_material + direct_labour + abc_overhead, actual.output_units, "unit cost")
        traditional_unit_cost = div(
            direct_material + direct_labour + traditional_overhead, actual.output_units, "unit cost"
        )
        distortion = traditional_overhead - abc_overhead
        distortion_ratio = div(abs(distortion), abc_overhead, "distortion") if abc_overhead else ZERO
        rows.append(
            ProductCosting(
                product_id=product_id,
                output_units=actual.output_units,
                direct_material=direct_material,
                direct_labour=direct_labour,
                abc_overhead=abc_overhead,
                traditional_overhead=traditional_overhead,
                abc_unit_cost=abc_unit_cost,
                traditional_unit_cost=traditional_unit_cost,
                price=standard.price,
                abc_margin=standard.price - abc_unit_cost,
                distortion=distortion,
                distortion_ratio=distortion_ratio,
                flagged=distortion_ratio >= CROSS_SUBSIDY_THRESHOLD,
                activity_cost=activity_cost,
            )
        )
    return rows, pools, total_overhead, recorded


def run_abc_profitability(
    snapshot: Snapshot,
    *,
    period_id: str,
    plant_id: str,
    basis: str = "labor_hours",
) -> CalcResult:
    rows, pools, total_overhead, recorded = _product_costing(snapshot, period_id, plant_id, basis)
    if not rows:
        raise DomainError(
            "ABC_NO_PRODUCTS",
            f"No products found for {period_id} / {plant_id}.",
            "Choose a period and plant that have production actuals.",
        )

    builder = StepBuilder()
    currency = snapshot.currency
    for pool in pools:
        builder.add(
            step_id=f"pool_{_slug(pool.pool_id)}",
            rule_id="ABC-001",
            label=f"{snapshot.activities[pool.activity_id].name} pool rate",
            formula="pool_cost / driver_quantity",
            inputs={"pool_cost": pool.pool_cost, "driver_quantity": pool.driver_quantity},
            value=pool.rate,
            unit="INR/event",
            source_refs=[f"cost_pools.csv:{period_id}/{plant_id}/{pool.pool_id}"],
        )

    for row in rows:
        builder.add(
            step_id=f"activity_{_slug(row.product_id)}",
            rule_id="ABC-002",
            label=f"{row.product_id} activity overhead",
            formula="sum(driver_quantity x pool_rate)",
            inputs={pool_id: value for pool_id, value in sorted(row.activity_cost.items())},
            value=row.abc_overhead,
            unit=currency,
            source_refs=[f"driver_events.csv:{period_id}/{plant_id}/{row.product_id}"],
        )

    total_base = sum(
        (snapshot.actual(period_id, plant_id, row.product_id).labor_hours
         if basis == "labor_hours"
         else snapshot.actual(period_id, plant_id, row.product_id).actual_machine_hours)
        for row in rows
    )
    builder.add(
        step_id="traditional_rate",
        rule_id="ABC-003",
        label="Traditional overhead rate",
        formula="total_overhead / total_allocation_base",
        inputs={"total_overhead": total_overhead, "total_allocation_base": total_base, "basis": basis},
        value=div(total_overhead, total_base, "rate"),
        unit="INR/hour",
        source_refs=["production_actuals.csv"],
    )

    for row in rows:
        builder.add(
            step_id=f"traditional_{_slug(row.product_id)}",
            rule_id="ABC-004",
            label=f"{row.product_id} traditional overhead",
            formula="traditional_rate x product_base_quantity",
            inputs={"product": row.product_id, "basis": basis},
            value=row.traditional_overhead,
            unit=currency,
            source_refs=["production_actuals.csv", "budgets.csv"],
        )
        builder.add(
            step_id=f"distortion_{_slug(row.product_id)}",
            rule_id="ABC-005",
            label=f"{row.product_id} cost distortion",
            formula="traditional_overhead - abc_overhead",
            inputs={
                "traditional_overhead": row.traditional_overhead,
                "abc_overhead": row.abc_overhead,
            },
            value=row.distortion,
            unit=currency,
            source_refs=["standard_costs.csv"],
        )
        builder.add(
            step_id=f"unitcost_{_slug(row.product_id)}",
            rule_id="ABC-006",
            label=f"{row.product_id} ABC unit cost",
            formula="direct_material + direct_labour + activity_overhead",
            inputs={
                "direct_material": row.direct_material,
                "direct_labour": row.direct_labour,
                "abc_overhead": row.abc_overhead,
                "output_units": row.output_units,
            },
            value=row.abc_unit_cost,
            unit="INR/unit",
            source_refs=[
                f"production_actuals.csv:{period_id}/{plant_id}/{row.product_id}",
                f"standard_costs.csv:{period_id}/{plant_id}/{row.product_id}",
            ],
        )
        builder.add(
            step_id=f"flag_{_slug(row.product_id)}",
            rule_id="ABC-007",
            label=f"{row.product_id} cross-subsidy ratio",
            formula="abs(distortion) / abc_overhead",
            inputs={"distortion": row.distortion, "abc_overhead": row.abc_overhead},
            value=row.distortion_ratio,
            unit="ratio",
            source_refs=["standard_costs.csv"],
        )

    total_abc = sum((row.abc_overhead for row in rows), ZERO)
    total_traditional = sum((row.traditional_overhead for row in rows), ZERO)
    builder.add(
        step_id="abc_total",
        rule_id="ABC-002",
        label="Total overhead assigned by ABC",
        formula="sum(product activity overhead)",
        inputs={"products": len(rows)},
        value=total_abc,
        unit=currency,
        source_refs=["cost_pools.csv", "driver_events.csv"],
    )
    builder.add(
        step_id="traditional_total",
        rule_id="ABC-004",
        label="Total overhead assigned by traditional costing",
        formula="sum(product traditional overhead)",
        inputs={"products": len(rows), "basis": basis},
        value=total_traditional,
        unit=currency,
        source_refs=["production_actuals.csv"],
    )

    facts: list[Fact] = [
        Fact("f_abc_total_overhead", "abc_total", "Overhead assigned by ABC", total_abc, currency),
        Fact("f_abc_traditional_overhead", "traditional_total", "Overhead assigned traditionally", total_traditional, currency),
    ]
    for row in rows:
        fact_id = f"f_abc_unit_cost_{_slug(row.product_id)}"
        facts.append(
            Fact(fact_id, f"unitcost_{_slug(row.product_id)}", f"{row.product_id} ABC unit cost", row.abc_unit_cost, "INR/unit")
        )
        facts.append(
            Fact(
                f"f_abc_distortion_{_slug(row.product_id)}",
                f"distortion_{_slug(row.product_id)}",
                f"{row.product_id} cost distortion",
                row.distortion,
                currency,
            )
        )

    flagged = [row for row in rows if row.flagged]
    warnings: list[Warning] = []
    if flagged:
        warnings.append(
            Warning(
                "ABC_CROSS_SUBSIDY",
                f"{len(flagged)} product(s) are cross-subsidised by more than ten percent: "
                + ", ".join(row.product_id for row in flagged),
                "info",
                "Use the ABC unit cost, not the traditional cost, for pricing and mix decisions.",
            )
        )
    if abs(total_overhead - recorded) > Decimal("0.05"):
        warnings.append(
            Warning(
                "ABC_POOL_TIE",
                f"Activity pools total {total_overhead} but recorded overhead is {recorded}.",
                "warning",
                "Reconcile cost_pools.csv to the GL overhead accounts before relying on allocations.",
            )
        )
    if not flagged:
        warnings.append(
            Warning("ABC_NO_FLAG", "No product crosses the ten percent cross-subsidy threshold.", "info")
        )
    if basis == "labor_hours":
        warnings.append(
            Warning(
                "ABC_BASIS",
                "Traditional comparison allocates overhead on labour hours; switch to machine hours to test basis sensitivity.",
                "info",
            )
        )

    result = CalcResult(
        value=total_abc,
        unit=currency,
        favorable=None,
        verdict=(
            f"{len(flagged)} of {len(rows)} products are cross-subsidised under the traditional "
            f"{basis.replace('_', ' ')} basis."
        ),
        steps=builder.steps,
        facts=facts,
        warnings=warnings,
        sensitivity=[
            SensitivityPoint(
                label=f"{row.product_id} ABC unit cost at +5% price",
                value=mul(row.price, Decimal("1.05")) - row.abc_unit_cost,
                unit="INR/unit",
                favorable=True,
                note="Margin impact of a five percent price move.",
                context={"product_id": row.product_id},
            )
            for row in rows
        ],
        rationale_template=(
            "Activity-based costing assigns {{f_abc_total_overhead}} of overhead compared with "
            "{{f_abc_traditional_overhead}} under the traditional basis; the product table shows "
            "which costs moved and which products are cross-subsidised."
        ),
        meta={
            "period_id": period_id,
            "plant_id": plant_id,
            "basis": basis,
            "products": [
                {
                    "product_id": row.product_id,
                    "output_units": str(row.output_units),
                    "direct_material": str(row.direct_material),
                    "direct_labour": str(row.direct_labour),
                    "abc_overhead": str(row.abc_overhead),
                    "traditional_overhead": str(row.traditional_overhead),
                    "abc_unit_cost": str(row.abc_unit_cost),
                    "traditional_unit_cost": str(row.traditional_unit_cost),
                    "price": str(row.price),
                    "abc_margin": str(row.abc_margin),
                    "distortion": str(row.distortion),
                    "distortion_ratio": str(row.distortion_ratio),
                    "cross_subsidised": row.flagged,
                }
                for row in rows
            ],
            "pools": [
                {
                    "pool_id": pool.pool_id,
                    "activity_id": pool.activity_id,
                    "pool_cost": str(pool.pool_cost),
                    "driver_quantity": str(pool.driver_quantity),
                    "rate": str(pool.rate),
                }
                for pool in pools
            ],
            "recorded_overhead": str(recorded),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(total_abc, sum((r.abc_overhead for r in rows), ZERO))])
    if not report.ok:
        raise DomainError("ABC_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result


def run_tdabc(
    snapshot: Snapshot,
    *,
    period_id: str,
    plant_id: str,
) -> CalcResult:
    _require_pools(snapshot, period_id, plant_id)
    pools = [
        pool for key, pool in sorted(snapshot.cost_pools.items())
        if key[0] == period_id and key[1] == plant_id
    ]
    product_ids = sorted(
        product for (period, plant, product) in snapshot.actuals
        if period == period_id and plant == plant_id
    )
    builder = StepBuilder()
    currency = snapshot.currency
    total_assigned = ZERO
    total_unused = ZERO
    pool_rows: list[dict[str, Decimal | str]] = []
    product_unused: dict[str, Decimal] = {product: ZERO for product in product_ids}

    for pool in pools:
        capacity_rate = div(pool.pool_cost, pool.capacity_events, "capacity rate")
        assigned = mul(capacity_rate, pool.driver_quantity)
        unused = mul(capacity_rate, pool.capacity_events - pool.driver_quantity)
        utilisation = div(pool.driver_quantity, pool.capacity_events, "utilisation")
        total_assigned += assigned
        total_unused += unused
        builder.add(
            step_id=f"tdrate_{_slug(pool.pool_id)}",
            rule_id="TDABC-001",
            label=f"{snapshot.activities[pool.activity_id].name} capacity cost rate",
            formula="pool_cost / capacity_events",
            inputs={"pool_cost": pool.pool_cost, "capacity_events": pool.capacity_events},
            value=capacity_rate,
            unit="INR/event",
            source_refs=[f"cost_pools.csv:{period_id}/{plant_id}/{pool.pool_id}"],
        )
        builder.add(
            step_id=f"tdassigned_{_slug(pool.pool_id)}",
            rule_id="TDABC-002",
            label=f"{pool.pool_id} assigned cost",
            formula="capacity_rate x actual_driver_quantity",
            inputs={"capacity_rate": capacity_rate, "driver_quantity": pool.driver_quantity},
            value=assigned,
            unit=currency,
            source_refs=[f"cost_pools.csv:{period_id}/{plant_id}/{pool.pool_id}"],
        )
        builder.add(
            step_id=f"tdunused_{_slug(pool.pool_id)}",
            rule_id="TDABC-003",
            label=f"{pool.pool_id} unused capacity cost",
            formula="capacity_rate x (capacity_events - actual_quantity)",
            inputs={"capacity_events": pool.capacity_events, "driver_quantity": pool.driver_quantity},
            value=unused,
            unit=currency,
            source_refs=[f"cost_pools.csv:{period_id}/{plant_id}/{pool.pool_id}"],
        )
        builder.add(
            step_id=f"tdutil_{_slug(pool.pool_id)}",
            rule_id="TDABC-004",
            label=f"{pool.pool_id} capacity utilisation",
            formula="actual_quantity / capacity_events",
            inputs={"driver_quantity": pool.driver_quantity, "capacity_events": pool.capacity_events},
            value=utilisation,
            unit="ratio",
            source_refs=[f"cost_pools.csv:{period_id}/{plant_id}/{pool.pool_id}"],
        )
        for product in product_ids:
            event = snapshot.driver_events.get((period_id, plant_id, product, pool.activity_id))
            if event:
                product_unused[product] += mul(capacity_rate, pool.capacity_events - event.driver_quantity)
        pool_rows.append(
            {
                "pool_id": pool.pool_id,
                "activity_id": pool.activity_id,
                "capacity_rate": capacity_rate,
                "assigned": assigned,
                "unused": unused,
                "utilisation": utilisation,
            }
        )

    builder.add(
        step_id="tdabc_total_assigned",
        rule_id="TDABC-002",
        label="Total assigned activity cost",
        formula="sum(pool assigned cost)",
        inputs={"pools": len(pools)},
        value=total_assigned,
        unit=currency,
        source_refs=["cost_pools.csv", "driver_events.csv"],
    )
    builder.add(
        step_id="tdabc_total_unused",
        rule_id="TDABC-003",
        label="Total unused capacity cost",
        formula="sum(pool unused capacity cost)",
        inputs={"pools": len(pools)},
        value=total_unused,
        unit=currency,
        source_refs=["cost_pools.csv"],
    )
    result = CalcResult(
        value=total_unused,
        unit=currency,
        favorable=False,
        verdict=(
            "Time-driven ABC isolates the cost of capacity that was supplied but not used; "
            "the assigned portion is the product cost."
        ),
        steps=builder.steps,
        facts=[
            Fact("f_tdabc_unused", "tdabc_total_unused", "Unused capacity cost", total_unused, currency),
            Fact("f_tdabc_assigned", "tdabc_total_assigned", "Assigned activity cost", total_assigned, currency),
        ],
        warnings=[
            Warning(
                "TDABC_UNUSED_CAPACITY",
                f"{total_unused} of activity cost relates to unused capacity and is not assigned to products.",
                "info",
                "Treat unused capacity as a period cost; do not bury it in product cost.",
            )
        ],
        rationale_template=(
            "Time-driven ABC assigns {{f_tdabc_assigned}} to products and reports "
            "{{f_tdabc_unused}} as unused capacity, so product cost reflects demand rather than supply."
        ),
        meta={
            "period_id": period_id,
            "plant_id": plant_id,
            "assigned_cost": str(total_assigned),
            "unused_capacity_cost": str(total_unused),
            "pools": [
                {
                    "pool_id": row["pool_id"],
                    "capacity_rate": str(row["capacity_rate"]),
                    "assigned": str(row["assigned"]),
                    "unused": str(row["unused"]),
                    "utilisation": str(row["utilisation"]),
                }
                for row in pool_rows
            ],
            "product_unused_capacity": {k: str(v) for k, v in sorted(product_unused.items())},
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result)
    if not report.ok:
        raise DomainError("TDABC_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result


def run_customer_profitability(
    snapshot: Snapshot,
    *,
    period_id: str,
    plant_id: str,
    selected_customer: str | None = None,
) -> CalcResult:
    if not snapshot.customer_activity or not snapshot.customer_cost_rates:
        raise DomainError(
            "CUSTOMER_DATA_MISSING",
            "Customer activity or cost-to-serve rates are not loaded.",
            "Load customers.csv, customer_activity.csv and customer_cost_rates.csv.",
        )
    abc = run_abc_profitability(snapshot, period_id=period_id, plant_id=plant_id)
    unit_costs = {
        row["product_id"]: Decimal(row["abc_unit_cost"]) for row in abc.meta["products"]
    }
    revenue_rates = {key: value.rate for key, value in snapshot.customer_cost_rates.items()}
    order_rate = revenue_rates.get("RATE-ORDER", ZERO)
    service_rate = revenue_rates.get("RATE-SERVICE", ZERO)
    delivery_rate = revenue_rates.get("RATE-DELIVERY", ZERO)

    all_rows = [
        row for key, row in sorted(snapshot.customer_activity.items())
        if key[0] == period_id and key[1] == plant_id
    ]
    if not all_rows:
        raise DomainError(
            "CUSTOMER_ACTIVITY_MISSING",
            f"No customer activity for {period_id} / {plant_id}.",
            "Choose a period and plant with customer activity rows.",
        )
    scope_note: str | None = None
    rows = all_rows
    if selected_customer:
        rows = [row for row in all_rows if row.customer_id == selected_customer]
        if not rows:
            available = sorted({row.customer_id for row in all_rows})
            raise DomainError(
                "CUSTOMER_NOT_IN_SCOPE",
                f"{selected_customer} has no activity for {period_id} / {plant_id}.",
                "Choose one of: " + ", ".join(available) + ".",
            )
        scope_note = f"Scope limited to {selected_customer} from the question."

    builder = StepBuilder()
    currency = snapshot.currency
    aggregate: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        bucket = aggregate.setdefault(
            row.customer_id,
            {"units": ZERO, "revenue": ZERO, "cost": ZERO, "serve": ZERO, "orders": ZERO,
             "tickets": ZERO, "deliveries": ZERO},
        )
        bucket["units"] += row.units
        bucket["revenue"] += row.revenue
        bucket["cost"] += mul(row.units, unit_costs.get(row.product_id, ZERO))
        bucket["orders"] += row.orders
        bucket["tickets"] += row.service_tickets
        bucket["deliveries"] += row.deliveries
        bucket["serve"] += (
            mul(row.orders, order_rate)
            + mul(row.service_tickets, service_rate)
            + mul(row.deliveries, delivery_rate)
        )

    customer_rows: list[dict[str, Decimal | str | bool]] = []
    total_net = ZERO
    for customer_id, values in sorted(aggregate.items()):
        net = values["revenue"] - values["cost"] - values["serve"]
        total_net += net
        margin = div(net, values["revenue"], "margin") if values["revenue"] else ZERO
        builder.add(
            step_id=f"cus_revenue_{_slug(customer_id)}",
            rule_id="CUS-001",
            label=f"{customer_id} revenue",
            formula="sum(customer_activity.revenue)",
            inputs={"units": values["units"]},
            value=values["revenue"],
            unit=currency,
            source_refs=[f"customer_activity.csv:{period_id}/{plant_id}/{customer_id}"],
        )
        builder.add(
            step_id=f"cus_serve_{_slug(customer_id)}",
            rule_id="CUS-002",
            label=f"{customer_id} cost to serve",
            formula="orders x order_rate + tickets x service_rate + deliveries x delivery_rate",
            inputs={
                "orders": values["orders"],
                "service_tickets": values["tickets"],
                "deliveries": values["deliveries"],
            },
            value=values["serve"],
            unit=currency,
            source_refs=["customer_cost_rates.csv"],
        )
        builder.add(
            step_id=f"cus_net_{_slug(customer_id)}",
            rule_id="CUS-003",
            label=f"{customer_id} net margin",
            formula="revenue - abc_product_cost - cost_to_serve",
            inputs={
                "revenue": values["revenue"],
                "abc_product_cost": values["cost"],
                "cost_to_serve": values["serve"],
            },
            value=net,
            unit=currency,
            source_refs=["customer_activity.csv", "customer_cost_rates.csv"],
        )
        builder.add(
            step_id=f"cus_verdict_{_slug(customer_id)}",
            rule_id="CUS-004",
            label=f"{customer_id} loss-making flag",
            formula="net_margin < 0",
            inputs={"net_margin": net},
            value=Decimal("1") if net < ZERO else ZERO,
            unit="ratio",
            source_refs=["customer_activity.csv"],
        )
        customer_rows.append(
            {
                "customer_id": customer_id,
                "name": snapshot.customers[customer_id].name,
                "units": values["units"],
                "revenue": values["revenue"],
                "product_cost": values["cost"],
                "cost_to_serve": values["serve"],
                "net_margin": net,
                "margin_ratio": margin,
                "orders": values["orders"],
                "service_tickets": values["tickets"],
                "deliveries": values["deliveries"],
                "loss_making": net < ZERO,
            }
        )

    builder.add(
        step_id="cus_total_margin",
        rule_id="CUS-003",
        label="Total customer net margin",
        formula="sum(customer net margin)",
        inputs={"customers": len(customer_rows)},
        value=total_net,
        unit=currency,
        source_refs=["customer_activity.csv", "customer_cost_rates.csv"],
    )
    losing = [row for row in customer_rows if row["loss_making"]]
    first = customer_rows[0]
    if len(customer_rows) == 1:
        verdict = (
            f"{first['customer_id']} is {'loss-making' if first['loss_making'] else 'profitable'} "
            "after activity-based product cost and cost to serve."
        )
    else:
        verdict = (
            f"{len(losing)} of {len(customer_rows)} customers are "
            "loss-making after activity-based product cost and cost to serve."
        )
    warnings = [
        Warning(
            "CUS_LOSS_MAKING",
            f"Loss-making customers: {', '.join(str(row['customer_id']) for row in losing) or 'none'}.",
            "warning" if losing else "info",
            "Review order frequency, service intensity and price before exiting a customer.",
        )
    ]
    if scope_note:
        warnings.append(Warning("CUS_SCOPE_FILTER", scope_note, "info"))
    result = CalcResult(
        value=total_net,
        unit=currency,
        favorable=total_net >= ZERO,
        verdict=verdict,
        steps=builder.steps,
        facts=[
            Fact("f_cus_total_margin", "cus_total_margin", "Total customer net margin", total_net, currency),
            Fact("f_cus_revenue", f"cus_revenue_{_slug(str(first['customer_id']))}", "Anchor customer revenue", Decimal(str(first["revenue"])), currency),
            Fact("f_cus_serve", f"cus_serve_{_slug(str(first['customer_id']))}", "Anchor cost to serve", Decimal(str(first["cost_to_serve"])), currency),
        ],
        warnings=warnings,
        sensitivity=[
            SensitivityPoint(
                label=f"{row['customer_id']} margin at +5% price",
                value=Decimal(str(row["net_margin"])) + Decimal(str(row["revenue"])) * Decimal("0.05"),
                unit=currency,
                favorable=True,
                note="Net margin after a five percent price rise with unchanged activity.",
                context={"customer_id": str(row["customer_id"])},
            )
            for row in customer_rows
        ],
        rationale_template=(
            "Customer profitability charges activity-based product cost and cost to serve; "
            "total net margin is {{f_cus_total_margin}} and the largest single cost-to-serve line "
            "is {{f_cus_serve}} for the anchor customer."
        ),
        meta={
            "period_id": period_id,
            "plant_id": plant_id,
            "customer_id": selected_customer or "ALL",
            "customers": [
                {
                    **{key: (str(value) if isinstance(value, Decimal) else value) for key, value in row.items()}
                }
                for row in customer_rows
            ],
            "rates": {
                "RATE-ORDER": str(order_rate),
                "RATE-SERVICE": str(service_rate),
                "RATE-DELIVERY": str(delivery_rate),
            },
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(total_net, sum((Decimal(str(r["net_margin"])) for r in customer_rows), ZERO))])
    if not report.ok:
        raise DomainError("CUS_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
