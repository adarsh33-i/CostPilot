"""Continuous monitoring: deterministic alert-rule parsing, evaluation and digest."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from costpilot.data.loader import Snapshot
from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, div, mul
from costpilot.engine.narration import render
from costpilot.engine.trace import CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result

METRIC_ALIASES = {
    "material variance": "material_variance",
    "material cost variance": "material_variance",
    "material price variance": "material_price_variance",
    "material usage variance": "material_usage_variance",
    "labour variance": "labour_variance",
    "labor variance": "labour_variance",
    "labour cost variance": "labour_variance",
    "overhead variance": "overhead_variance",
    "revenue variance": "revenue_variance",
    "contribution margin": "contribution_margin",
    "material price index": "material_price_index",
    "utilisation": "utilization",
    "utilization": "utilization",
    "operating income": "operating_income",
}
AMOUNT_METRICS = {
    "material_variance", "material_price_variance", "material_usage_variance",
    "labour_variance", "overhead_variance", "revenue_variance", "operating_income",
}
LOWER_IS_BETTER = {"material_variance", "material_price_variance", "material_usage_variance",
                   "labour_variance", "overhead_variance"}

_NUMBER = re.compile(r"(?<![\w.])(-?\d[\d,]*\.?\d*)\s*(k|m|lakh|lakhs|crore|%)?", re.IGNORECASE)


def parse_alert_rule(text: str) -> dict[str, Any]:
    lowered = text.lower()
    metric = None
    for phrase, key in sorted(METRIC_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if phrase in lowered:
            metric = key
            break
    if metric is None:
        raise DomainError(
            "MON_METRIC_UNKNOWN",
            f"The alert rule does not name a supported metric: {text!r}.",
            "Use material variance, labour variance, overhead variance, revenue variance, "
            "contribution margin, material price index, utilisation or operating income.",
        )
    comparator = "above"
    if any(word in lowered for word in ("below", "under", "less than", "lower than", "<")):
        comparator = "below"
    elif any(word in lowered for word in ("above", "over", "greater than", "exceeds", ">")):
        comparator = "above"

    plant_id = None
    plant_match = re.search(r"\b(PLT-\d+)\b", text, re.IGNORECASE)
    if plant_match:
        plant_id = plant_match.group(1).upper()

    match = None
    for candidate in _NUMBER.finditer(text):
        # Ignore the plant token and numbers embedded in the metric phrase.
        start = candidate.start()
        if plant_match and plant_match.start() <= start < plant_match.end():
            continue
        match = candidate
        break
    if match is None:
        raise DomainError(
            "MON_THRESHOLD_MISSING",
            f"The alert rule has no threshold: {text!r}.",
            "Include a numeric threshold, for example 'material variance above 50,000'.",
        )
    threshold = Decimal(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        threshold *= Decimal("1000")
    elif suffix == "m":
        threshold *= Decimal("1000000")
    elif suffix in {"lakh", "lakhs"}:
        threshold *= Decimal("100000")
    elif suffix == "crore":
        threshold *= Decimal("10000000")
    elif suffix == "%":
        threshold /= Decimal("100")

    severity = "warning"
    if "critical" in lowered or "urgent" in lowered:
        severity = "critical"
    if metric in {"contribution_margin", "utilization", "material_price_index"} and suffix != "%":
        # A bare ratio threshold such as 30 means thirty percent.
        if threshold > Decimal("2"):
            threshold /= Decimal("100")
    return {
        "text": text,
        "metric": metric,
        "comparator": comparator,
        "threshold": threshold,
        "plant_id": plant_id,
        "severity": severity,
    }


def _metric_values(snapshot: Snapshot, period_id: str, plant_id: str | None) -> dict[str, Decimal]:
    plant_ids = [plant_id] if plant_id else [row["plant_id"] for row in snapshot.plants]
    revenue_actual = ZERO
    revenue_budget = ZERO
    material_actual = ZERO
    material_budget = ZERO
    material_actual_value = ZERO
    material_standard_value = ZERO
    labour_actual = ZERO
    labour_budget = ZERO
    overhead_actual = ZERO
    overhead_budget = ZERO
    selling_actual = ZERO
    capacity = ZERO
    output = ZERO
    for (period, plant, product), actual in snapshot.actuals.items():
        if period != period_id or plant not in plant_ids:
            continue
        standard = snapshot.standard(period_id, plant, product)
        budget = snapshot.budget(period_id, plant, product)
        revenue_actual += mul(actual.output_units, standard.price)
        revenue_budget += budget.revenue
        material_actual += mul(actual.material_qty_used, actual.material_price)
        material_budget += budget.material
        material_actual_value += mul(actual.material_qty_used, actual.material_price)
        material_standard_value += mul(actual.material_qty_used, standard.std_material_price)
        labour_actual += mul(actual.labor_hours, actual.labor_rate)
        labour_budget += budget.labor
        overhead_actual += actual.variable_oh_actual + actual.fixed_oh_actual
        overhead_budget += budget.variable_oh + budget.fixed_oh
        selling_actual += mul(actual.output_units, standard.variable_selling)
        capacity += standard.capacity_units
        output += actual.output_units
    variable = material_actual + labour_actual + overhead_actual + selling_actual
    contribution = div(revenue_actual - variable, revenue_actual, "contribution margin") if revenue_actual else ZERO
    return {
        "revenue_variance": revenue_actual - revenue_budget,
        "material_variance": material_actual - material_budget,
        "material_price_variance": material_actual_value - material_standard_value,
        "material_usage_variance": material_actual - material_budget,
        "labour_variance": labour_actual - labour_budget,
        "overhead_variance": overhead_actual - overhead_budget,
        "contribution_margin": contribution,
        "material_price_index": div(material_actual_value, material_standard_value, "price index") if material_standard_value else ZERO,
        "utilization": div(output, capacity, "utilization") if capacity else ZERO,
        "operating_income": revenue_actual - material_actual - labour_actual - overhead_actual - selling_actual,
    }


def run_monitoring(
    snapshot: Snapshot,
    *,
    period_id: str,
    rules: list[str | dict[str, Any]] | None = None,
    materiality: str | Decimal = "50000",
) -> CalcResult:
    if not snapshot.periods:
        raise DomainError("MON_NO_PERIOD", "No periods are loaded.", "Load periods.csv.")
    effective = rules or [
        "alert when material variance is above 50,000",
        "alert when overhead variance is above 40,000",
        "alert when contribution margin is below 30%",
        "alert when utilisation is below 60%",
    ]
    parsed: list[dict[str, Any]] = []
    for rule in effective:
        if isinstance(rule, str):
            parsed.append(parse_alert_rule(rule))
        elif isinstance(rule, dict):
            metric = str(rule.get("metric", ""))
            if metric not in METRIC_ALIASES.values():
                raise DomainError(
                    "MON_METRIC_UNKNOWN",
                    f"Unknown alert metric {metric!r}.",
                    "Use one of: " + ", ".join(sorted(set(METRIC_ALIASES.values()))) + ".",
                )
            parsed.append(
                {
                    "text": str(rule.get("text", f"{metric} {rule.get('comparator', 'above')} {rule.get('threshold')}")),
                    "metric": metric,
                    "comparator": str(rule.get("comparator", "above")),
                    "threshold": Decimal(str(rule.get("threshold", "0"))),
                    "plant_id": rule.get("plant_id"),
                    "severity": str(rule.get("severity", "warning")),
                }
            )
        else:
            raise DomainError("MON_RULE_TYPE", "Alert rules must be strings or objects.", "Rewrite the rule as text.")

    materiality_amount = Decimal(str(materiality))
    if materiality_amount < ZERO:
        raise DomainError("MON_MATERIALITY", "Materiality cannot be negative.", "Enter a zero or positive amount.")

    builder = StepBuilder()
    currency = snapshot.currency
    builder.add(
        step_id="mon_rules", rule_id="MON-001", label="Parsed alert rules",
        formula="count(parsed rules)",
        inputs={"rules": len(parsed)}, value=Decimal(len(parsed)), unit="units",
        source_refs=["alert rule text"],
    )
    alerts: list[dict[str, Any]] = []
    total_alerted = ZERO
    for index, rule in enumerate(parsed):
        values = _metric_values(snapshot, period_id, rule.get("plant_id"))
        value = values.get(rule["metric"], ZERO)
        if rule["comparator"] == "above":
            triggered = value > rule["threshold"]
        else:
            triggered = value < rule["threshold"]
        material = True
        if rule["metric"] in AMOUNT_METRICS:
            material = abs(value) >= materiality_amount
        effective_trigger = triggered and material
        if effective_trigger and rule["metric"] in AMOUNT_METRICS:
            total_alerted += abs(value)
        builder.add(
            step_id=f"mon_rule_{index}",
            rule_id="MON-002",
            label=f"Alert {index + 1}: {rule['metric']}",
            formula=f"value {rule['comparator']} threshold",
            inputs={
                "value": value,
                "threshold": rule["threshold"],
                "comparator": rule["comparator"],
                "materiality": materiality_amount,
            },
            value=value, unit="ratio" if rule["metric"] not in AMOUNT_METRICS else currency,
            source_refs=["production_actuals.csv", "budgets.csv", "standard_costs.csv"],
        )
        builder.add(
            step_id=f"mon_material_{index}",
            rule_id="MON-003",
            label=f"Alert {index + 1} materiality",
            formula="abs(variance) >= materiality_amount",
            inputs={"variance": value, "materiality_amount": materiality_amount},
            value=Decimal("1") if material else ZERO,
            unit="ratio",
            source_refs=["monitoring policy"],
        )
        alerts.append(
            {
                "rule": rule["text"],
                "metric": rule["metric"],
                "plant_id": rule.get("plant_id") or "ALL",
                "value": value,
                "threshold": rule["threshold"],
                "comparator": rule["comparator"],
                "severity": rule["severity"],
                "triggered": triggered,
                "material": material,
                "alert": effective_trigger,
                "impact": abs(value) if rule["metric"] in AMOUNT_METRICS else ZERO,
            }
        )
    active = [alert for alert in alerts if alert["alert"]]
    ordered = sorted(active, key=lambda alert: alert["impact"], reverse=True)
    builder.add(
        step_id="mon_active", rule_id="MON-002", label="Active alerts",
        formula="count(triggered and material)",
        inputs={"rules": len(parsed)}, value=Decimal(len(active)), unit="units",
        source_refs=["alert rules"],
    )
    builder.add(
        step_id="mon_digest", rule_id="MON-004", label="Digest total impact",
        formula="sum(abs(impact)) for active amount alerts",
        inputs={"alerts": len(active)}, value=total_alerted, unit=currency,
        source_refs=["production_actuals.csv", "budgets.csv"],
    )
    suppressed = [alert for alert in alerts if alert["triggered"] and not alert["material"]]
    result = CalcResult(
        value=total_alerted,
        unit=currency,
        favorable=len(active) == 0,
        verdict=(
            f"{len(active)} alert(s) pass the materiality screen and are ready for the digest."
            if active else "No alert passes the materiality screen this period."
        ),
        steps=builder.steps,
        facts=[
            Fact("f_mon_rules", "mon_rules", "Parsed alert rules", Decimal(len(parsed)), "units"),
            Fact("f_mon_active", "mon_active", "Active alerts", Decimal(len(active)), "units"),
            Fact("f_mon_impact", "mon_digest", "Digest impact", total_alerted, currency),
        ],
        warnings=[
            Warning(
                "MON_BELOW_MATERIALITY",
                f"{len(suppressed)} triggered rule(s) are below materiality and were suppressed.",
                "info",
                "Review suppressed alerts before month-end; a cluster of small items can still be material.",
            )
        ] if suppressed else [],
        rationale_template=(
            "The monitoring job parsed {{f_mon_rules}} alert rules, found {{f_mon_active}} active "
            "alerts and a total material impact of {{f_mon_impact}}."
        ),
        meta={
            "period_id": period_id,
            "materiality": str(materiality_amount),
            "alerts": [
                {
                    **{
                        key: (str(value) if isinstance(value, Decimal) else value)
                        for key, value in alert.items()
                    }
                }
                for alert in alerts
            ],
            "digest": [
                {
                    **{
                        key: (str(value) if isinstance(value, Decimal) else value)
                        for key, value in alert.items()
                    }
                }
                for alert in ordered
            ],
            "active_count": len(active),
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result, identities=[(total_alerted, sum((alert["impact"] for alert in active), ZERO))])
    if not report.ok:
        raise DomainError("MON_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
