"""Trace models. Every number in a response carries its calculation chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from costpilot.engine.money import display_amount, trace_str


@dataclass(frozen=True)
class Assumption:
    id: str
    key: str
    text: str
    source: str
    confirmed: bool
    evidence: str | None = None
    confidence: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "key": self.key,
            "text": self.text,
            "source": self.source,
            "confirmed": self.confirmed,
        }
        if self.evidence:
            payload["evidence"] = self.evidence
        if self.confidence is not None:
            payload["confidence"] = trace_str(self.confidence)
        return payload


@dataclass(frozen=True)
class Warning:
    code: str
    message: str
    severity: str = "warning"
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


@dataclass(frozen=True)
class SensitivityPoint:
    label: str
    value: Decimal
    unit: str
    favorable: bool | None = None
    note: str | None = None
    context: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": trace_str(self.value),
            "display": display_amount(self.value, self.unit),
            "unit": self.unit,
            "favorable": self.favorable,
            "note": self.note,
            "context": self.context,
        }


@dataclass(frozen=True)
class CalcStep:
    step_id: str
    label: str
    rule_id: str
    rule_version: str
    formula: str
    inputs: dict[str, str]
    value: Decimal
    unit: str
    source_refs: list[str]
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step_id": self.step_id,
            "label": self.label,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "formula": self.formula,
            "inputs": self.inputs,
            "value": trace_str(self.value),
            "display": display_amount(self.value, self.unit),
            "unit": self.unit,
            "source_refs": self.source_refs,
        }
        if self.note:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class Fact:
    fact_id: str
    step_id: str
    label: str
    value: Decimal
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "step_id": self.step_id,
            "label": self.label,
            "value": trace_str(self.value),
            "display": display_amount(self.value, self.unit),
            "unit": self.unit,
        }


@dataclass
class CalcResult:
    value: Decimal
    unit: str
    favorable: bool | None
    verdict: str | None
    steps: list[CalcStep]
    facts: list[Fact]
    assumptions: list[Assumption] = field(default_factory=list)
    warnings: list[Warning] = field(default_factory=list)
    sensitivity: list[SensitivityPoint] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    rationale_template: str = ""
    rationale: str = ""
    remediation: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def fact_map(self) -> dict[str, Fact]:
        return {fact.fact_id: fact for fact in self.facts}

    def step_map(self) -> dict[str, CalcStep]:
        return {step.step_id: step for step in self.steps}

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": trace_str(self.value),
            "display": display_amount(self.value, self.unit),
            "unit": self.unit,
            "favorable": self.favorable,
            "verdict": self.verdict,
            "steps": [step.to_dict() for step in self.steps],
            "facts": [fact.to_dict() for fact in self.facts],
            "assumptions": [item.to_dict() for item in self.assumptions],
            "warnings": [item.to_dict() for item in self.warnings],
            "sensitivity": [item.to_dict() for item in self.sensitivity],
            "conditions": self.conditions,
            "rationale_template": self.rationale_template,
            "rationale": self.rationale,
            "remediation": self.remediation,
            "meta": self.meta,
        }


class StepBuilder:
    """Builds ordered steps and checks that every value came from the engine."""

    def __init__(self) -> None:
        self.steps: list[CalcStep] = []

    def add(
        self,
        *,
        step_id: str,
        rule_id: str,
        label: str,
        formula: str,
        inputs: dict[str, Any],
        value: Decimal,
        unit: str,
        source_refs: list[str] | tuple[str, ...] = (),
        note: str | None = None,
    ) -> CalcStep:
        from costpilot.engine.rules.registry import get_rule

        rule = get_rule(rule_id)
        if any(step.step_id == step_id for step in self.steps):
            raise ValueError(f"duplicate step_id {step_id}")
        clean_inputs = {
            key: trace_str(item) if isinstance(item, Decimal) else str(item)
            for key, item in inputs.items()
        }
        step = CalcStep(
            step_id=step_id,
            label=label,
            rule_id=rule.id,
            rule_version=rule.version,
            formula=formula,
            inputs=clean_inputs,
            value=value,
            unit=unit,
            source_refs=list(source_refs),
            note=note,
        )
        self.steps.append(step)
        return step

    def value(self, step_id: str) -> Decimal:
        for step in self.steps:
            if step.step_id == step_id:
                return step.value
        raise KeyError(step_id)
