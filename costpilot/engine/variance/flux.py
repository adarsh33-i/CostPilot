"""Account-level flux with an honest unexplained residual (FLX-ACCT-001..002)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result


@dataclass(frozen=True)
class FluxLine:
    account_id: str
    label: str
    kind: str  # revenue | cost
    actual: Decimal
    budget: Decimal
    prior: Decimal
    explained: Decimal = ZERO


@dataclass(frozen=True)
class FluxInput:
    period_id: str
    lines: list[FluxLine] = field(default_factory=list)
    materiality: Decimal = ZERO
    currency: str = "INR"


def run(inputs: FluxInput) -> CalcResult:
    if not inputs.lines:
        raise DomainError("FLUX_LINES", "At least one account is required.")

    builder = StepBuilder()
    line_payload: list[dict] = []
    net_budget = ZERO
    net_prior = ZERO
    net_residual = ZERO
    material_count = 0

    for index, line in enumerate(inputs.lines, start=1):
        if line.kind not in {"revenue", "cost"}:
            raise DomainError("FLUX_KIND", f"Unknown account kind {line.kind!r}.")
        budget_variance = line.actual - line.budget
        prior_variance = line.actual - line.prior
        favorable = budget_variance >= ZERO if line.kind == "revenue" else budget_variance <= ZERO
        # Favorable-positive amount. Costs above budget consume margin.
        favorable_amount = budget_variance if line.kind == "revenue" else -budget_variance
        residual = budget_variance - line.explained
        if abs(budget_variance) >= inputs.materiality and inputs.materiality > ZERO:
            material_count += 1

        builder.add(
            step_id=f"a{index}",
            rule_id="FLX-ACCT-001",
            label=f"Account flux — {line.label}",
            formula="actual - budget",
            inputs={"actual": line.actual, "budget": line.budget, "prior": line.prior},
            value=budget_variance,
            unit=inputs.currency,
            source_refs=[f"gl:{line.account_id}", "budget:account"],
            note="Positive is unfavorable for cost accounts; driver decomposition unavailable.",
        )
        builder.add(
            step_id=f"a{index}_residual",
            rule_id="FLX-ACCT-002",
            label=f"Unexplained residual — {line.label}",
            formula="budget_variance - explained_components",
            inputs={"budget_variance": budget_variance, "explained": line.explained},
            value=residual,
            unit=inputs.currency,
            source_refs=[f"gl:{line.account_id}"],
            note="No price/usage split is fabricated for an account-level flux.",
        )
        net_budget += favorable_amount
        net_prior += prior_variance if line.kind == "revenue" else -prior_variance
        net_residual += favorable_amount - line.explained
        line_payload.append(
            {
                "account_id": line.account_id,
                "label": line.label,
                "kind": line.kind,
                "actual": str(line.actual),
                "budget": str(line.budget),
                "prior": str(line.prior),
                "budget_variance": str(budget_variance),
                "prior_variance": str(prior_variance),
                "favorable": favorable,
                "favorable_amount": str(favorable_amount),
                "explained": str(line.explained),
                "residual": str(residual),
                "decomposition_status": "unavailable",
            }
        )

    total_step = builder.add(
        step_id="total",
        rule_id="FLX-ACCT-001",
        label="Net account flux, favorable-positive",
        formula="sum(favorable amounts)",
        inputs={"accounts": len(inputs.lines)},
        value=net_budget,
        unit=inputs.currency,
        source_refs=["engine:flux"],
    )

    result = CalcResult(
        value=net_budget,
        unit=inputs.currency,
        favorable=net_budget >= ZERO,
        verdict=None,
        steps=builder.steps,
        facts=[
            Fact("f_flux", "total", "Net flux versus budget", total_step.value, inputs.currency),
        ],
        assumptions=[
            Assumption(
                "a1", "flux_level_b", "This is an account-level flux; no driver decomposition is implied",
                "engine_default", True, confidence=Decimal("1"),
            ),
        ],
        warnings=[
            Warning(
                "decomposition_unavailable",
                "Driver-level price/usage decomposition is not available for this scope; "
                "the movement is labeled unexplained rather than estimated.",
                "info",
                "Upload volume and standard-cost detail for a level-C flexible-budget decomposition.",
            )
        ],
        rationale_template=(
            "Net account flux is {{f_flux}}. Driver decomposition is unavailable at this scope, "
            "so the residual remains explicitly unexplained."
        ),
        meta={
            "period_id": inputs.period_id,
            "lines": line_payload,
            "materiality": str(inputs.materiality),
            "material_accounts": material_count,
            "prior_net": str(net_prior),
            "residual_net": str(net_residual),
            "decomposition_status": "unavailable",
        },
    )
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result)
    if not report.ok:
        raise DomainError("FLUX_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
