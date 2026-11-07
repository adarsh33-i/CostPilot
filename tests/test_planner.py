"""Planner: intent routing, typed arguments and honest clarification."""

from __future__ import annotations

import pytest

from costpilot.platform.planner import plan_message


def plan(snapshot, text):
    return plan_message(snapshot, text)


def test_variance_plan_resolves_scope(snapshot):
    payload = plan(snapshot, "Variance for PRD-B at PLT-01 in 2026-09")
    assert payload["intent"] == "variance_analysis"
    assert payload["status"] == "ready"
    assert payload["arguments"]["period_id"] == "2026-09"
    assert payload["arguments"]["plant_id"] == "PLT-01"
    assert payload["arguments"]["product_id"] == "PRD-B"
    assert payload["tool"] == "run_variance_analysis"
    assert payload["tool_version"]
    assert payload["confidence"] >= "0.9"
    assert payload["clarification"] is None


def test_city_and_product_name_aliases(snapshot):
    payload = plan(snapshot, "Show the variance for servo drive in Pune during 2026-08")
    assert payload["arguments"]["plant_id"] == "PLT-01"
    assert payload["arguments"]["product_id"] == "PRD-C"
    assert payload["arguments"]["period_id"] == "2026-08"


def test_abc_machine_hours_basis(snapshot):
    payload = plan(snapshot, "ABC profitability for PLT-01 in 2026-09 using machine hours")
    assert payload["intent"] == "abc_profitability"
    assert payload["arguments"]["basis"] == "machine_hours"


def test_scenario_overlay_and_confirmation(snapshot):
    payload = plan(snapshot, "Budget scenario for PLT-01 in 2026-09: material price rises 3%")
    assert payload["intent"] == "budget_scenario"
    assert payload["arguments"]["overlay"] == {"material_price": "0.03"}
    assert payload["requires_confirmation"] is True
    assumption = next(item for item in payload["assumptions"] if item["key"] == "overlay")
    assert assumption["source"] == "user_explicit"
    assert assumption["confirm"] is True


def test_scenario_signed_direction(snapshot):
    payload = plan(snapshot, "Budget scenario for PLT-01 in 2026-09: volume falls 2%")
    assert payload["arguments"]["overlay"] == {"volume": "-0.02"}


def test_scenario_accepts_spelled_percent(snapshot):
    payload = plan(snapshot, "Scenario: material price up 3 percent for PLT-01")
    assert payload["intent"] == "budget_scenario"
    assert payload["arguments"]["overlay"] == {"material_price": "0.03"}
    assert payload["status"] == "needs_confirmation"


def test_scenario_without_drivers_asks_one_question(snapshot):
    payload = plan(snapshot, "Budget scenario for PLT-01 in 2026-09")
    assert payload["intent"] == "budget_scenario"
    assert payload["status"] == "needs_clarification"
    assert "which budget drivers" in payload["clarification"]["question"].lower()
    assert payload["clarification"]["options"]


def test_customer_scope_is_typed(snapshot):
    payload = plan(snapshot, "Customer profitability for PLT-01 in 2026-09 for CUS-05")
    assert payload["intent"] == "customer_profitability"
    assert payload["arguments"]["customer_id"] == "CUS-05"
    assumption = next(item for item in payload["assumptions"] if item["key"] == "customer_id")
    assert assumption["source"] == "user_explicit"


def test_performance_division(snapshot):
    payload = plan(snapshot, "Divisional performance for DIV-02 in 2026-09")
    assert payload["intent"] == "performance_eval"
    assert payload["arguments"]["division_id"] == "DIV-02"


def test_monitoring_rule_and_materiality(snapshot):
    payload = plan(
        snapshot,
        "Alert monitoring for 2026-09 with materiality 75,000: material variance above 50,000",
    )
    assert payload["intent"] == "monitoring"
    assert payload["arguments"]["materiality"] == "75000"
    assert payload["arguments"]["rules"] == ["material variance above 50,000"]


def test_group_and_connector_intents(snapshot):
    assert plan(snapshot, "Group consolidation for 2026-09")["intent"] == "group_report"
    connector = plan(snapshot, "Preview the SAP connector reconciliation")
    assert connector["intent"] == "connector_preview"
    assert connector["arguments"]["connector"] == "sap"


def test_unknown_question_is_clarified(snapshot):
    payload = plan(snapshot, "What should I have for lunch?")
    assert payload["intent"] == "clarify"
    assert payload["status"] == "needs_clarification"
    assert payload["clarification"]["options"]
    assert payload["confidence"] < "0.60"


def test_unknown_period_offers_options(snapshot):
    payload = plan(snapshot, "Variance for PRD-B at PLT-01 in 2026-13")
    assert payload["status"] == "needs_clarification"
    assert "2026-13" in payload["clarification"]["question"]
    assert payload["clarification"]["options"] == ["2026-07", "2026-08", "2026-09"]


def test_ambiguous_question_offers_interpretations(snapshot):
    payload = plan(snapshot, "Special order performance review for PLT-01")
    assert payload["status"] == "needs_clarification"
    assert len(payload["clarification"]["options"]) >= 2


def test_defaults_are_labelled_not_silent(snapshot):
    payload = plan(snapshot, "Variance for PRD-B")
    sources = {item["key"]: item["source"] for item in payload["assumptions"]}
    assert sources["period_id"] == "engine_default"
    assert sources["plant_id"] == "engine_default"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Show the variable-to-absorption bridge", "costing_bridge"),
        ("Special order: 1200 units at 940 each", "special_order"),
        ("TDABC for PLT-01", "tdabc"),
        ("Master budget for PLT-01", "budget_chain"),
        ("Rolling forecast for PLT-01", "rolling_forecast"),
        ("Forecast quality for PLT-01", "forecast_quality"),
    ],
)
def test_intent_routing(snapshot, text, intent):
    assert plan(snapshot, text)["intent"] == intent
