"""Phase 2 engines: ABC, TDABC, budgets, performance, monitoring and group."""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.errors import DomainError


def value(envelope):
    return Decimal(envelope["result"]["value"])


def test_abc_compares_traditional_and_driver_based_cost(service):
    envelope = service.run_abc_profitability(period_id="2026-09", plant_id="PLT-01")
    result = envelope["result"]
    meta = result["meta"]
    assert meta["basis"] == "labor_hours"
    assert len(meta["products"]) == 3
    assert len(meta["pools"]) >= 2
    assert result["verdict"].startswith("3 of 3 products")
    for row in meta["products"]:
        assert row["abc_unit_cost"] != row["traditional_unit_cost"]
        assert row["abc_margin"] is not None
    assert envelope["validation"]["failures"] == []


def test_abc_machine_hours_basis_changes_the_allocation(service):
    labour = service.run_abc_profitability(
        period_id="2026-09", plant_id="PLT-01", basis="labor_hours"
    )["result"]
    machine = service.run_abc_profitability(
        period_id="2026-09", plant_id="PLT-01", basis="machine_hours"
    )["result"]
    assert machine["meta"]["basis"] == "machine_hours"
    assert machine["meta"]["products"] != labour["meta"]["products"]


def test_tdabc_separates_unused_capacity(service):
    envelope = service.run_tdabc(period_id="2026-09", plant_id="PLT-01")
    meta = envelope["result"]["meta"]
    assert Decimal(meta["assigned_cost"]) > 0
    assert Decimal(meta["unused_capacity_cost"]) > 0
    assert meta["product_unused_capacity"]
    assert envelope["validation"]["failures"] == []


def test_customer_profitability_filters_one_customer(service):
    envelope = service.run_customer_profitability(
        period_id="2026-09", plant_id="PLT-01", customer_id="CUS-05"
    )
    result = envelope["result"]
    assert result["meta"]["customer_id"] == "CUS-05"
    rows = result["meta"]["customers"]
    assert len(rows) == 1
    assert rows[0]["customer_id"] == "CUS-05"
    assert rows[0]["loss_making"] is True
    assert "CUS-05 is loss-making" in result["verdict"]
    assert value(envelope) == Decimal(rows[0]["net_margin"])
    assert any(w["code"] == "CUS_SCOPE_FILTER" for w in result["warnings"])


def test_customer_not_in_scope_suggests_available_customers(service):
    with pytest.raises(DomainError) as exc:
        service.run_customer_profitability(
            period_id="2026-09", plant_id="PLT-01", customer_id="CUS-99"
        )
    assert exc.value.code == "CUSTOMER_NOT_IN_SCOPE"
    assert "CUS-01" in exc.value.remediation


def test_budget_chain_balances_to_cash(service):
    envelope = service.run_budget(period_id="2026-09", plant_id="PLT-01")
    meta = envelope["result"]["meta"]
    assert Decimal(str(envelope["result"]["value"])) == Decimal("896680")
    assert Decimal(meta["operating_income"]) == Decimal("896680")
    assert Decimal(meta["closing_cash"]) == Decimal("2205674")
    assert envelope["validation"]["failures"] == []


def test_scenario_uses_fractional_drivers(service):
    baseline = service.run_budget(period_id="2026-09", plant_id="PLT-01")["result"]
    scenario = service.run_scenario(
        period_id="2026-09", plant_id="PLT-01", overlay={"material_price": "0.03"}
    )
    meta = scenario["result"]["meta"]
    assert Decimal(meta["baseline"]) == Decimal(baseline["value"])
    assert Decimal(meta["delta"]) < 0
    assert Decimal(meta["delta"]) > Decimal("-100000")
    assert scenario["result"]["favorable"] is False
    assert meta["overlay"] == {"material_price": "0.03"}


def test_scenario_rejects_percent_sized_values(service):
    with pytest.raises(DomainError) as exc:
        service.run_scenario(
            period_id="2026-09", plant_id="PLT-01", overlay={"material_price": "3"}
        )
    assert exc.value.code == "BUD_SCENARIO_RANGE"
    assert "0.03" in exc.value.remediation


def test_rolling_forecast_and_quality(service):
    rolling = service.run_rolling_forecast(plant_id="PLT-01")
    meta = rolling["result"]["meta"]
    assert Decimal(rolling["result"]["value"]) == Decimal(meta["forecast_income"])
    assert Decimal(meta["forecast_income"]) > Decimal(meta["static_budget_income"])

    quality = service.run_forecast_quality(plant_id="PLT-01")
    assert Decimal(quality["result"]["value"]) == Decimal("1")
    assert "sandbagging" in quality["result"]["verdict"]


def test_performance_separates_roi_and_residual_income(service):
    envelope = service.run_performance(period_id="2026-09", division_id="DIV-01")
    meta = envelope["result"]["meta"]
    assert Decimal(meta["roi"]).quantize(Decimal("0.001")) == Decimal("0.388")
    assert Decimal(meta["residual_income"]) == Decimal("669921.3458")
    assert Decimal(meta["weighted_score"]) == Decimal("0.8235116445094694546584418848")
    assert meta["investment_conflict"] is True
    assert envelope["result"]["steps"][0]["rule_id"].startswith("PERF-")


def test_monitoring_applies_materiality(service):
    envelope = service.run_monitoring(
        period_id="2026-09",
        rules=["material variance above 50,000"],
        materiality="50000",
    )
    meta = envelope["result"]["meta"]
    assert meta["materiality"] == "50000"
    assert meta["active_count"] == 1
    assert Decimal(meta["digest"][0]["value"]) > Decimal("50000")
    stored = service.run_saved_alerts(period_id="2026-09")
    assert stored["result"]["meta"]["active_count"] >= 0


def test_monitoring_rejects_unknown_metric(service):
    with pytest.raises(DomainError) as exc:
        service.run_monitoring(period_id="2026-09", rules=["foo metric above 10"])
    assert exc.value.code in {"MON_METRIC_UNKNOWN", "E_ENGINE_DOMAIN"}


def test_group_eliminates_intercompany(service):
    envelope = service.run_group_report(period_id="2026-09")
    meta = envelope["result"]["meta"]
    assert Decimal(meta["entity_revenue"]) == Decimal("8180400")
    assert Decimal(meta["group_revenue"]) == Decimal("7160400")
    assert Decimal(meta["intercompany_revenue"]) > 0
    assert Decimal(meta["consolidated_income"]) == Decimal("2502701.0184")
    assert len(meta["entities"]) == 2
    assert envelope["validation"]["failures"] == []


def test_formula_injection_is_neutralised_in_exports(service):
    from costpilot.platform.exports import safe_text

    assert safe_text("=HYPERLINK(1)").startswith("'")
    assert safe_text("+1") == "'+1"
    assert safe_text("normal text") == "normal text"
