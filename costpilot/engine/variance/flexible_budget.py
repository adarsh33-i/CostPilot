"""Flexible-budget performance report (FLX-001..004)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, div, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class FlexibleBudgetLine:
    name: str
    kind: str  # revenue | variable_cost | fixed_cost
    static_budget: Decimal
    actual: Decimal
    variable_rate_per_unit: Decimal | None = None
    account_id: str = ""


@dataclass(frozen=True)
class FlexibleBudgetInput:
    budget_volume: Decimal
    actual_volume: Decimal
    lines: list[FlexibleBudgetLine] = field(default_factory=list)
    period_id: str = ""
    currency: str = "INR"


def run(inputs: FlexibleBudgetInput) -> CalcResult:
    if inputs.budget_volume <= ZERO:
        raise DomainError("FLX_BUDGET_VOLUME", "Budget volume must be positive.")
    if inputs.actual_volume < ZERO:
        raise DomainError("FLX_ACTUAL_VOLUME", "Actual volume cannot be negative.")
    if not inputs.lines:
        raise DomainError("FLX_LINES", "At least one budget line is required.")

    builder = StepBuilder()
    line_results: list[dict] = []
    total_flexible = ZERO
    total_volume = ZERO
    total_variance = ZERO

    for index, line in enumerate(inputs.lines, start=1):
        if line.kind not in {"revenue", "variable_cost", "fixed_cost"}:
            raise DomainError("FLX_KIND", f"Unknown line kind {line.kind!r} for {line.name}.")
        if line.kind == "revenue":
            flexed = mul(div(line.static_budget, inputs.budget_volume), inputs.actual_volume)
        elif line.kind == "variable_cost":
            rate = (
                line.variable_rate_per_unit
                if line.variable_rate_per_unit is not None
                else div(line.static_budget, inputs.budget_volume)
            )
            flexed = mul(rate, inputs.actual_volume)
        else:
            flexed = line.static_budget

        if line.kind == "revenue":
            flexible_variance = line.actual - flexed
            volume_variance = flexed - line.static_budget
        else:
            flexible_variance = flexed - line.actual
            volume_variance = line.static_budget - flexed
        total = flexible_variance + volume_variance

        flex_step = builder.add(
            step_id=f"l{index}_flex",
            rule_id="FLX-001" if line.kind == "revenue" else "FLX-002",
            label=f"Flexed {line.name}",
            formula=(
                "static_revenue ÷ budget_volume × actual_volume"
                if line.kind == "revenue"
                else "variable_rate × actual_volume" if line.kind == "variable_cost" else "static_budget"
            ),
            inputs={
                "static_budget": line.static_budget,
                "budget_volume": inputs.budget_volume,
                "actual_volume": inputs.actual_volume,
                "variable_rate_per_unit": line.variable_rate_per_unit or ZERO,
            },
            value=flexed,
            unit=inputs.currency,
            source_refs=["budget:static", "input:actual_volume"],
        )
        flex_var_step = builder.add(
            step_id=f"l{index}_flex_var",
            rule_id="FLX-003",
            label=f"Flexible-budget variance — {line.name}",
            formula=(
                "actual - flexed" if line.kind == "revenue" else "flexed - actual"
            ),
            inputs={"actual": line.actual, "flexed": flex_step.value},
            value=flexible_variance,
            unit=inputs.currency,
            source_refs=["input:actual"],
        )
        volume_step = builder.add(
            step_id=f"l{index}_volume",
            rule_id="FLX-004",
            label=f"Volume variance — {line.name}",
            formula=(
                "flexed - static" if line.kind == "revenue" else "static - flexed"
            ),
            inputs={"static": line.static_budget, "flexed": flex_step.value},
            value=volume_variance,
            unit=inputs.currency,
            source_refs=["budget:static"],
        )
        builder.add(
            step_id=f"l{index}_total",
            rule_id="FLX-003",
            label=f"Total variance — {line.name}",
            formula="flexible_variance + volume_variance",
            inputs={"flexible": flex_var_step.value, "volume": volume_step.value},
            value=total,
            unit=inputs.currency,
            source_refs=["engine:flexible_budget"],
        )
        total_flexible += flexible_variance
        total_volume += volume_variance
        total_variance += total
        line_results.append(
            {
                "name": line.name,
                "kind": line.kind,
                "account_id": line.account_id,
                "static_budget": str(line.static_budget),
                "flexed_budget": str(flexed),
                "actual": str(line.actual),
                "flexible_variance": str(flexible_variance),
                "volume_variance": str(volume_variance),
                "total_variance": str(total),
                "favorable": total >= ZERO,
            }
        )

    flexible_total_step = builder.add(
        step_id="flex_total",
        rule_id="FLX-003",
        label="Total flexible-budget variance",
        formula="sum(line flexible-budget variances)",
        inputs={"lines": len(inputs.lines)},
        value=total_flexible,
        unit=inputs.currency,
        source_refs=["engine:flexible_budget"],
    )
    volume_total_step = builder.add(
        step_id="volume_total",
        rule_id="FLX-004",
        label="Total volume variance",
        formula="sum(line volume variances)",
        inputs={"lines": len(inputs.lines)},
        value=total_volume,
        unit=inputs.currency,
        source_refs=["engine:flexible_budget"],
    )
    total_step = builder.add(
        step_id="total",
        rule_id="FLX-003",
        label="Total flexible-budget variance, net favorable",
        formula="sum(line total variances)",
        inputs={"lines": len(inputs.lines)},
        value=total_variance,
        unit=inputs.currency,
        source_refs=["engine:flexible_budget"],
    )

    result = CalcResult(
        value=total_variance,
        unit=inputs.currency,
        favorable=total_variance >= ZERO,
        verdict=None,
        steps=builder.steps,
        facts=[
            Fact("f_flexible", "flex_total", "Flexible-budget variance", flexible_total_step.value, inputs.currency),
            Fact("f_volume", "volume_total", "Volume variance component", volume_total_step.value, inputs.currency),
            Fact("f_total", "total", "Total flexible-budget variance", total_step.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "cost_behavior", "Variable costs flex with volume and fixed costs do not",
                "registry", True, confidence=Decimal("1"),
            ),
            Assumption(
                "a2", "single_product_mix", "No mix variance is computed without product-mix data",
                "engine_default", True, confidence=Decimal("1"),
            ),
        ],
        rationale_template=(
            "The flexible-budget variance is {{f_flexible}} after removing the volume effect, "
            "which is shown separately as {{f_volume}}."
        ),
        meta={
            "period_id": inputs.period_id,
            "budget_volume": str(inputs.budget_volume),
            "actual_volume": str(inputs.actual_volume),
            "lines": line_results,
            "flexible_total": str(total_flexible),
            "volume_total": str(total_volume),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(
        result,
        identities=[(total_variance, total_flexible + total_volume)],
    )
    if not report.ok:
        raise DomainError("FLX_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
