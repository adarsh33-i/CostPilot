"""Divisional performance: ROI, residual income, investment conflict, scorecard."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from costpilot.data.loader import Snapshot
from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, div, fmt_money, fmt_pct, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import CalcResult, Fact, SensitivityPoint, StepBuilder, Warning
from costpilot.engine.validation import validate_result


def _plant_income(snapshot: Snapshot, period_id: str, plant_id: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    revenue = ZERO
    variable = ZERO
    fixed = ZERO
    selling = ZERO
    for (period, plant, product), actual in snapshot.actuals.items():
        if period != period_id or plant != plant_id:
            continue
        standard = snapshot.standard(period_id, plant, product)
        revenue += mul(actual.output_units, standard.price)
        variable += mul(actual.material_qty_used, actual.material_price)
        variable += mul(actual.labor_hours, actual.labor_rate)
        variable += actual.variable_oh_actual
        fixed += actual.fixed_oh_actual
        selling += mul(actual.output_units, standard.variable_selling)
    return revenue, variable + selling, fixed, selling


def _normalise(actual: Decimal, target: Decimal, direction: str) -> Decimal:
    cap = Decimal("1.25")
    if direction == "higher":
        if target == ZERO:
            return Decimal("1") if actual >= target else ZERO
        score = div(actual, target, "kpi")
    else:
        if target == ZERO:
            return Decimal("1") if actual <= target else ZERO
        if actual == ZERO:
            return cap
        score = div(target, actual, "kpi")
    return min(max(score, ZERO), cap)


def _live_actual(snapshot: Snapshot, period_id: str, plant_id: str, source: str) -> Decimal | None:
    if not source.startswith("snapshot:"):
        return None
    metric = source.split(":", 1)[1]
    if metric == "contribution_margin":
        revenue, variable, _, _ = _plant_income(snapshot, period_id, plant_id)
        return div(revenue - variable, revenue, "contribution margin") if revenue else ZERO
    if metric == "material_variance_ratio":
        actual_material = ZERO
        budget_material = ZERO
        for (period, plant, product), actual in snapshot.actuals.items():
            if period != period_id or plant != plant_id:
                continue
            budget = snapshot.budget(period_id, plant, product)
            actual_material += mul(actual.material_qty_used, actual.material_price)
            budget_material += budget.material
        return div(abs(actual_material - budget_material), budget_material, "material variance ratio") if budget_material else ZERO
    return None


def run_performance_eval(
    snapshot: Snapshot,
    *,
    period_id: str,
    division_id: str,
) -> CalcResult:
    if not snapshot.assets or not snapshot.scorecard:
        raise DomainError(
            "PERF_DATA_MISSING",
            "Asset or scorecard data is not loaded.",
            "Load assets.csv and scorecard.csv to run performance measurement.",
        )
    asset = snapshot.assets.get((period_id, division_id))
    if asset is None:
        raise DomainError(
            "PERF_DIVISION_UNKNOWN",
            f"No asset base for {period_id} / {division_id}.",
            "Choose one of: " + ", ".join(sorted(key[1] for key in snapshot.assets)) + ".",
        )
    revenue, variable, fixed, selling = _plant_income(snapshot, period_id, asset.plant_id)
    operating_income = revenue - variable - fixed
    average_assets = div(asset.beginning_assets + asset.ending_assets, Decimal("2"), "average assets")
    roi = div(operating_income, average_assets, "ROI")
    required_income = mul(asset.required_return, average_assets)
    residual_income = operating_income - required_income

    new_roi = div(operating_income + asset.new_investment_income, average_assets + asset.new_investment_assets, "new ROI")
    new_required = mul(asset.required_return, average_assets + asset.new_investment_assets)
    new_ri = operating_income + asset.new_investment_income - new_required
    conflict = new_ri > ZERO and new_roi < roi

    builder = StepBuilder()
    currency = snapshot.currency
    builder.add(
        step_id="perf_income", rule_id="PERF-001", label="Divisional operating income",
        formula="actual revenue - variable costs - fixed overhead",
        inputs={"plant_id": asset.plant_id, "revenue": revenue, "variable": variable, "fixed": fixed},
        value=operating_income, unit=currency,
        source_refs=["production_actuals.csv", "standard_costs.csv"],
    )
    builder.add(
        step_id="perf_assets", rule_id="PERF-001", label="Average operating assets",
        formula="(beginning_assets + ending_assets) / 2",
        inputs={"beginning_assets": asset.beginning_assets, "ending_assets": asset.ending_assets},
        value=average_assets, unit=currency,
        source_refs=[f"assets.csv:{period_id}/{division_id}"],
    )
    builder.add(
        step_id="perf_roi", rule_id="PERF-001", label="Return on investment",
        formula="operating_income / average_operating_assets",
        inputs={"operating_income": operating_income, "average_assets": average_assets},
        value=roi, unit="ratio",
        source_refs=["assets.csv", "production_actuals.csv"],
    )
    builder.add(
        step_id="perf_required_income", rule_id="PERF-002", label="Required return",
        formula="required_return x average_assets",
        inputs={"required_return": asset.required_return, "average_assets": average_assets},
        value=required_income, unit=currency,
        source_refs=[f"assets.csv:{period_id}/{division_id}"],
    )
    builder.add(
        step_id="perf_ri", rule_id="PERF-002", label="Residual income",
        formula="operating_income - required_return x average_assets",
        inputs={"operating_income": operating_income, "required_income": required_income},
        value=residual_income, unit=currency,
        source_refs=["assets.csv", "production_actuals.csv"],
    )
    builder.add(
        step_id="perf_new_roi", rule_id="PERF-003", label="ROI after new investment",
        formula="(income + new_income) / (average_assets + new_assets)",
        inputs={
            "new_investment_income": asset.new_investment_income,
            "new_investment_assets": asset.new_investment_assets,
        },
        value=new_roi, unit="ratio",
        source_refs=[f"assets.csv:{period_id}/{division_id}"],
    )
    builder.add(
        step_id="perf_new_ri", rule_id="PERF-003", label="Residual income after new investment",
        formula="(income + new_income) - required_return x (assets + new_assets)",
        inputs={"new_ri": new_ri},
        value=new_ri, unit=currency,
        source_refs=[f"assets.csv:{period_id}/{division_id}"],
    )

    kpi_rows: list[dict[str, Any]] = []
    weighted = ZERO
    total_weight = ZERO
    for kpi_id, metric in sorted(snapshot.scorecard.items()):
        actual = metric.actual
        if actual is None:
            actual = _live_actual(snapshot, period_id, asset.plant_id, metric.source)
        if actual is None:
            raise DomainError(
                "PERF_KPI_SOURCE",
                f"KPI {kpi_id} has no actual and its source {metric.source!r} is not computable.",
                "Supply actual in scorecard.csv or map the source to a live snapshot metric.",
            )
        score = _normalise(actual, metric.target, metric.direction)
        weighted += mul(metric.weight, score)
        total_weight += metric.weight
        builder.add(
            step_id=f"kpi_{kpi_id.lower().replace('-', '_')}",
            rule_id="PERF-004",
            label=f"{metric.name} score",
            formula="actual / target for higher-is-better; target / actual for lower-is-better",
            inputs={
                "actual": actual,
                "target": metric.target,
                "direction": metric.direction,
                "weight": metric.weight,
            },
            value=score, unit="score",
            source_refs=[f"scorecard.csv:{kpi_id}", metric.source],
        )
        kpi_rows.append(
            {
                "kpi_id": kpi_id,
                "name": metric.name,
                "perspective": metric.perspective,
                "owner": metric.owner,
                "weight": metric.weight,
                "target": metric.target,
                "actual": actual,
                "direction": metric.direction,
                "controllability": metric.controllability,
                "score": score,
                "missed": score < Decimal("1"),
            }
        )
    weighted_score = div(weighted, total_weight, "weighted score")
    builder.add(
        step_id="perf_score", rule_id="PERF-005", label="Weighted scorecard score",
        formula="sum(weight x kpi_score) / sum(weight)",
        inputs={"kpis": len(kpi_rows)},
        value=weighted_score, unit="score",
        source_refs=["scorecard.csv"],
    )
    controllable_weight = sum(
        (row["weight"] for row in kpi_rows if row["controllability"] == "controllable"), ZERO
    )
    controllable_misses = sum(
        (Decimal("1") for row in kpi_rows if row["controllability"] == "controllable" and row["missed"]), ZERO
    )
    builder.add(
        step_id="perf_controllable", rule_id="PERF-006", label="Controllable KPI misses",
        formula="count(controllable KPI score < 1)",
        inputs={"controllable_weight": controllable_weight},
        value=controllable_misses, unit="units",
        source_refs=["scorecard.csv"],
    )

    result = CalcResult(
        value=weighted_score,
        unit="score",
        favorable=weighted_score >= Decimal("1"),
        verdict=(
            f"ROI is {fmt_pct(mul(roi, Decimal('100')))} and residual income is "
            f"{fmt_money(residual_income)}; the weighted scorecard is {weighted_score:.2f}."
        ),
        steps=builder.steps,
        facts=[
            Fact("f_perf_roi", "perf_roi", "Return on investment", roi, "ratio"),
            Fact("f_perf_ri", "perf_ri", "Residual income", residual_income, currency),
            Fact("f_perf_new_roi", "perf_new_roi", "ROI after new investment", new_roi, "ratio"),
            Fact("f_perf_new_ri", "perf_new_ri", "Residual income after new investment", new_ri, currency),
            Fact("f_perf_score", "perf_score", "Weighted scorecard score", weighted_score, "score"),
        ],
        warnings=[
            Warning(
                "PERF_INVESTMENT_CONFLICT",
                "The new investment lowers ROI but raises residual income; ROI rewards deferral and "
                "conflicts with value creation.",
                "warning",
                "Judge the investment on residual income or a required-return hurdle, not ROI alone.",
            )
            if conflict
            else Warning(
                "PERF_INVESTMENT_ALIGNED",
                "ROI and residual income point in the same direction for the new investment.",
                "info",
            )
        ],
        sensitivity=[
            SensitivityPoint(
                label="Residual income at +100bp required return",
                value=operating_income - mul(asset.required_return + Decimal("0.01"), average_assets),
                unit=currency,
                favorable=False,
                note="One percentage point higher capital charge.",
            )
        ],
        rationale_template=(
            "The division earns ROI of {{f_perf_roi}} and residual income of {{f_perf_ri}}; "
            "the new investment would move ROI to {{f_perf_new_roi}} and residual income to "
            "{{f_perf_new_ri}}, while the weighted scorecard stands at {{f_perf_score}}."
        ),
        meta={
            "period_id": period_id,
            "division_id": division_id,
            "plant_id": asset.plant_id,
            "operating_income": str(operating_income),
            "average_assets": str(average_assets),
            "roi": str(roi),
            "residual_income": str(residual_income),
            "new_roi": str(new_roi),
            "new_residual_income": str(new_ri),
            "investment_conflict": conflict,
            "weighted_score": str(weighted_score),
            "controllable_misses": str(controllable_misses),
            "kpis": [
                {
                    **{
                        key: (str(value) if isinstance(value, Decimal) else value)
                        for key, value in row.items()
                    }
                }
                for row in kpi_rows
            ],
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(
        result,
        identities=[
            (residual_income, operating_income - required_income),
            (weighted_score, div(weighted, total_weight, "weighted score")),
        ],
    )
    if not report.ok:
        raise DomainError("PERF_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
