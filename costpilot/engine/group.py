"""Multi-entity group reporting with intercompany and inventory-profit elimination."""

from __future__ import annotations

from decimal import Decimal

from costpilot.data.loader import Snapshot
from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, fmt_money, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result


def _entity_one(snapshot: Snapshot, period_id: str) -> tuple[Decimal, Decimal]:
    revenue = ZERO
    costs = ZERO
    for (period, plant, product), actual in snapshot.actuals.items():
        if period != period_id:
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


def run_group_report(snapshot: Snapshot, *, period_id: str) -> CalcResult:
    if not snapshot.entity_financials:
        raise DomainError(
            "GRP_DATA_MISSING",
            "Entity financials are not loaded.",
            "Load entity_financials.csv with revenue, variable income and absorption income.",
        )
    secondary = [row for (period, _), row in snapshot.entity_financials.items() if period == period_id]
    if not secondary:
        raise DomainError(
            "GRP_PERIOD_MISSING",
            f"No entity financials for {period_id}.",
            "Choose a period present in entity_financials.csv.",
        )
    primary_entity = snapshot.entities[0]["entity_id"]
    primary_revenue, primary_income = _entity_one(snapshot, period_id)
    secondary_revenue = sum((row.revenue for row in secondary), ZERO)
    secondary_income = sum((row.absorption_income for row in secondary), ZERO)
    entity_revenue = primary_revenue + secondary_revenue
    entity_income = primary_income + secondary_income

    ic_rows = [row for row in snapshot.intercompany if row.period_id == period_id]
    ic_revenue = sum((row.revenue for row in ic_rows), ZERO)
    ic_cost = sum((row.cost for row in ic_rows), ZERO)
    ic_margin = ic_revenue - ic_cost
    inventory_profit = sum(
        (mul(row.inventory_change_units, row.fixed_oh_rate) for row in secondary), ZERO
    )
    group_revenue = entity_revenue - ic_revenue
    consolidated_income = entity_income - ic_margin - inventory_profit
    flagged = [row for row in ic_rows if row.status == "flagged_for_review"]

    builder = StepBuilder()
    currency = snapshot.currency
    builder.add(
        step_id="grp_entity_revenue", rule_id="GRP-001", label="Sum of entity revenue",
        formula="primary revenue + secondary revenue",
        inputs={"primary_revenue": primary_revenue, "secondary_revenue": secondary_revenue},
        value=entity_revenue, unit=currency,
        source_refs=["production_actuals.csv", "entity_financials.csv"],
    )
    builder.add(
        step_id="grp_ic_revenue", rule_id="GRP-002", label="Intercompany revenue eliminated",
        formula="sum(intercompany revenue)",
        inputs={"pairs": len(ic_rows)}, value=ic_revenue, unit=currency,
        source_refs=[f"intercompany.csv:{period_id}"],
    )
    builder.add(
        step_id="grp_group_revenue", rule_id="GRP-001", label="Group revenue",
        formula="sum(entity revenue) - intercompany revenue",
        inputs={"entity_revenue": entity_revenue, "ic_revenue": ic_revenue},
        value=group_revenue, unit=currency,
        source_refs=["intercompany.csv", "entity_financials.csv"],
    )
    builder.add(
        step_id="grp_entity_income", rule_id="GRP-004", label="Sum of entity income",
        formula="primary income + secondary absorption income",
        inputs={"primary_income": primary_income, "secondary_income": secondary_income},
        value=entity_income, unit=currency,
        source_refs=["production_actuals.csv", "entity_financials.csv"],
    )
    builder.add(
        step_id="grp_ic_margin", rule_id="GRP-002", label="Intercompany profit eliminated",
        formula="sum(intercompany revenue - intercompany cost)",
        inputs={"ic_revenue": ic_revenue, "ic_cost": ic_cost},
        value=ic_margin, unit=currency,
        source_refs=[f"intercompany.csv:{period_id}"],
    )
    builder.add(
        step_id="grp_inventory_profit", rule_id="GRP-003", label="Inventory profit eliminated",
        formula="inventory_change_units x fixed_oh_rate",
        inputs={
            "inventory_change_units": sum((row.inventory_change_units for row in secondary), ZERO),
            "fixed_oh_rate": secondary[0].fixed_oh_rate,
        },
        value=inventory_profit, unit=currency,
        source_refs=["entity_financials.csv"],
    )
    builder.add(
        step_id="grp_income", rule_id="GRP-004", label="Consolidated operating income",
        formula="sum(entity income) - intercompany margin - inventory profit",
        inputs={
            "entity_income": entity_income,
            "ic_margin": ic_margin,
            "inventory_profit": inventory_profit,
        },
        value=consolidated_income, unit=currency,
        source_refs=["intercompany.csv", "entity_financials.csv"],
    )
    builder.add(
        step_id="grp_flags", rule_id="GRP-005", label="Flagged intercompany pairs",
        formula="count(status = flagged_for_review)",
        inputs={"pairs": len(ic_rows)}, value=Decimal(len(flagged)), unit="units",
        source_refs=[f"intercompany.csv:{period_id}"],
    )

    warnings: list[Warning] = [
        Warning(
            "GRP_INTERCOMPANY_FLAGGED",
            f"{len(flagged)} intercompany pair(s) are flagged for review.",
            "warning" if flagged else "info",
            "Confirm both sides of each pair post the same amount before signing the consolidation.",
        )
    ]
    if ic_rows:
        warnings.append(
            Warning(
                "GRP_ONE_SIDED_PAIRS",
                "Intercompany rows are seller-side only; buyer-side postings are not in the extract.",
                "warning",
                "Add buyer-side intercompany detail or rely on the status flag before elimination.",
            )
        )
    result = CalcResult(
        value=consolidated_income,
        unit=currency,
        favorable=consolidated_income >= ZERO,
        verdict=(
            f"Group revenue of {fmt_money(group_revenue)} consolidates to operating income of "
            f"{fmt_money(consolidated_income)} after eliminating intercompany profit and inventory profit."
        ),
        steps=builder.steps,
        facts=[
            Fact("f_grp_revenue", "grp_group_revenue", "Group revenue", group_revenue, currency),
            Fact("f_grp_income", "grp_income", "Consolidated operating income", consolidated_income, currency),
            Fact("f_grp_ic_margin", "grp_ic_margin", "Intercompany profit eliminated", ic_margin, currency),
            Fact("f_grp_inventory", "grp_inventory_profit", "Inventory profit eliminated", inventory_profit, currency),
            Fact("f_grp_flags", "grp_flags", "Flagged intercompany pairs", Decimal(len(flagged)), "units"),
        ],
        warnings=warnings,
        rationale_template=(
            "Group revenue is {{f_grp_revenue}}; after eliminating {{f_grp_ic_margin}} of intercompany "
            "profit and {{f_grp_inventory}} of inventory profit, consolidated operating income is "
            "{{f_grp_income}} with {{f_grp_flags}} flagged intercompany pairs."
        ),
        meta={
            "period_id": period_id,
            "primary_entity": primary_entity,
            "entities": [
                {"entity_id": primary_entity, "revenue": str(primary_revenue), "income": str(primary_income)}
            ] + [
                {"entity_id": row.entity_id, "revenue": str(row.revenue), "income": str(row.absorption_income)}
                for row in secondary
            ],
            "entity_revenue": str(entity_revenue),
            "entity_income": str(entity_income),
            "intercompany_revenue": str(ic_revenue),
            "intercompany_cost": str(ic_cost),
            "intercompany_margin": str(ic_margin),
            "inventory_profit": str(inventory_profit),
            "group_revenue": str(group_revenue),
            "consolidated_income": str(consolidated_income),
            "flagged_pairs": len(flagged),
            "intercompany": [
                {
                    "seller_entity": row.seller_entity,
                    "buyer_entity": row.buyer_entity,
                    "description": row.description,
                    "revenue": str(row.revenue),
                    "cost": str(row.cost),
                    "status": row.status,
                }
                for row in ic_rows
            ],
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(
        result,
        identities=[(consolidated_income, entity_income - ic_margin - inventory_profit)],
    )
    if not report.ok:
        raise DomainError("GRP_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
