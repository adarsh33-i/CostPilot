"""Validators for the trust boundary: schema, units, coverage, provenance, slot prose."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from costpilot.engine.trace import CalcResult

PLACEHOLDER = re.compile(r"\{\{f_[a-z0-9_]+\}\}")
DIGIT = re.compile(r"[0-9]")
ALLOWED_UNITS = {
    "INR", "INR/unit", "INR/hour", "INR/kg", "INR/scarce-unit", "%", "units", "kg",
    "hours", "setups", "orders", "ratio", "times",
}


@dataclass
class ValidationReport:
    schema: bool = True
    identities: bool = True
    units: bool = True
    coverage: bool = True
    provenance: bool = True
    slot_narration: bool = True
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.schema
            and self.identities
            and self.units
            and self.coverage
            and self.provenance
            and self.slot_narration
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "pass" if self.schema else "fail",
            "identities": "pass" if self.identities else "fail",
            "units": "pass" if self.units else "fail",
            "coverage": "pass" if self.coverage else "fail",
            "provenance": "pass" if self.provenance else "fail",
            "slot_narration": "pass" if self.slot_narration else "fail",
            "failures": self.failures,
        }


def identity_ok(left: Decimal, right: Decimal, tolerance: Decimal = Decimal("0.01")) -> bool:
    return abs(left - right) <= tolerance


def validate_result(result: CalcResult, identities: list[tuple[Decimal, Decimal]] | None = None) -> ValidationReport:
    report = ValidationReport()

    if not isinstance(result.value, Decimal) or not result.unit:
        report.schema = False
        report.failures.append("result value and unit are required")
    if not result.steps:
        report.schema = False
        report.failures.append("result has no calculation steps")

    step_ids = [step.step_id for step in result.steps]
    if len(step_ids) != len(set(step_ids)):
        report.schema = False
        report.failures.append("duplicate step IDs")

    steps = result.step_map()
    for fact in result.facts:
        step = steps.get(fact.step_id)
        if step is None:
            report.coverage = False
            report.failures.append(f"fact {fact.fact_id} references missing step {fact.step_id}")
            continue
        if fact.value != step.value or fact.unit != step.unit:
            report.coverage = False
            report.failures.append(f"fact {fact.fact_id} does not match its step")

    for step in result.steps:
        if not isinstance(step.value, Decimal):
            report.schema = False
            report.failures.append(f"step {step.step_id} value is not Decimal")
        if step.unit not in ALLOWED_UNITS:
            report.units = False
            report.failures.append(f"step {step.step_id} has undeclared unit {step.unit!r}")
        if not step.source_refs:
            report.provenance = False
            report.failures.append(f"step {step.step_id} has no source refs")

    if identities:
        for left, right in identities:
            if not identity_ok(left, right):
                report.identities = False
                report.failures.append(
                    f"identity failed: {left} != {right} (tolerance 0.01)"
                )

    if result.rationale_template:
        stripped = PLACEHOLDER.sub("", result.rationale_template)
        if DIGIT.search(stripped) or "%" in stripped or "₹" in stripped:
            report.slot_narration = False
            report.failures.append(
                "rationale template contains a literal number; use fact slots only"
            )
        if result.rationale:
            expected = render_rationale(result.rationale_template, result)
            if result.rationale != expected:
                report.slot_narration = False
                report.failures.append("rendered rationale does not match slot facts")

    return report


def render_rationale(template: str, result: CalcResult) -> str:
    facts = result.fact_map()

    def replace(match: re.Match[str]) -> str:
        key = match.group(0)[2:-2]
        if key not in facts:
            raise ValueError(f"rationale cites unknown fact {key}")
        return facts[key].to_dict()["display"]

    return PLACEHOLDER.sub(replace, template)


def strip_placeholders(template: str) -> str:
    return PLACEHOLDER.sub("", template)
