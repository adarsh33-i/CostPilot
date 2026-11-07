"""Narration is a template over engine facts, never free-written arithmetic."""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.narration import render_validated, truncate, validate
from costpilot.engine.trace import CalcResult, CalcStep, Fact


@pytest.fixture()
def result():
    step = CalcStep(
        step_id="s1",
        label="Gross margin",
        rule_id="VAR-001",
        rule_version="1.0.0",
        formula="revenue - cost",
        inputs={"revenue": "2000", "cost": "765.50"},
        value=Decimal("1234.50"),
        unit="INR",
        source_refs=["production_actuals.csv"],
    )
    return CalcResult(
        value=Decimal("1234.50"),
        unit="INR",
        favorable=True,
        verdict="Favorable",
        steps=[step],
        facts=[
            Fact("f_margin", "s1", "Gross margin", Decimal("1234.50"), "INR"),
            Fact("f_pct", "s1", "Margin ratio", Decimal("0.1234"), "ratio"),
        ],
    )


def test_valid_template_is_rendered_with_engine_values(result):
    narration = render_validated(
        "Gross margin is {{f_margin}} and the ratio is {{f_pct|PCT1}}.", result
    )
    assert narration["fallback_used"] is False
    assert narration["validation"]["ok"] is True
    assert "Gross margin is 1,234.50" in narration["text"]
    assert "12.3%" in narration["text"]


def test_unknown_slot_falls_back_to_deterministic_sentence(result):
    narration = render_validated("Gross margin is {{f_invented}}.", result, intent="variance_analysis")
    assert narration["fallback_used"] is True
    assert "f_invented" not in narration["text"]
    assert "1,234.50" in narration["text"]
    checks = {item["check"]: item["status"] for item in narration["attempts"][0]["validation"]["checks"]}
    assert checks["unknown_slot"] == "fail"


def test_hand_written_number_is_rejected(result):
    narration = render_validated("Gross margin is 1,234.50.", result)
    assert narration["fallback_used"] is True
    checks = {item["check"]: item["status"] for item in narration["attempts"][0]["validation"]["checks"]}
    assert checks["raw_numerals"] == "fail"


def test_spelled_quantity_is_rejected(result):
    narration = render_validated(
        "Margin is {{f_margin}} after saving two thousand rupees.", result
    )
    assert narration["fallback_used"] is True


def test_decision_words_stay_out_of_narration(result):
    report = validate("We should accept because the margin is {{f_margin}}.", result)
    assert report.ok is False
    assert any("verdict_fidelity" in issue for issue in report.issues)


def test_optimistic_language_on_negative_facts_is_flagged(result):
    negative = CalcResult(
        value=Decimal("-500"),
        unit="INR",
        favorable=False,
        verdict="Adverse",
        steps=result.steps,
        facts=[Fact("f_loss", "s1", "Loss", Decimal("-500"), "INR")],
    )
    report = validate("There is upside in the {{f_loss}} result.", negative)
    polarity = next(item for item in report.checks if item["check"] == "polarity")
    assert polarity["status"] == "warn"


def test_unknown_format_fails_loudly(result):
    from costpilot.engine.narration import render

    with pytest.raises(ValueError):
        render("{{f_margin|NOT_A_FORMAT}}", result)
    narration = render_validated("Margin is {{f_margin|CURRENCY}}.", result)
    assert narration["fallback_used"] is True
    checks = {item["check"]: item["status"] for item in narration["attempts"][0]["validation"]["checks"]}
    assert checks["placeholder_format"] == "fail"


def test_regeneration_gets_one_attempt_then_falls_back(result):
    calls = {"count": 0}

    def regenerate(report):
        calls["count"] += 1
        return "Margin is 999."

    narration = render_validated("Margin is {{f_missing}}.", result, regenerate=regenerate)
    assert calls["count"] == 1
    assert narration["fallback_used"] is True
    assert len(narration["attempts"]) >= 3


def test_truncate_prefers_sentence_boundary():
    text = "First sentence is short. Second sentence is also short. " + "x " * 200
    shortened = truncate(text, limit=10)
    assert "First sentence is short." in shortened
    assert len(shortened.split()) <= 10
