"""The seven decision workflows, their verdicts, and the materiality boundary."""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.decisions import run_decision
from costpilot.engine.errors import DomainError


def test_make_or_buy_uses_relevant_costs():
    result = run_decision(
        "make_or_buy",
        {
            "units": "1000",
            "variable_cost_per_unit": "300",
            "avoidable_fixed": "50000",
            "purchase_price": "360",
            "materiality": "1000",
        },
    )
    assert result.value == Decimal("10000")
    assert result.verdict == "make"
    assert result.fact_map()["f_make"].value == Decimal("350000")
    assert result.fact_map()["f_buy"].value == Decimal("360000")


def test_make_or_buy_inside_the_materiality_band_is_investigate():
    result = run_decision(
        "make_or_buy",
        {
            "units": "1000",
            "variable_cost_per_unit": "300",
            "avoidable_fixed": "50000",
            "purchase_price": "360",
            "materiality": "20000",
        },
    )
    assert result.verdict == "investigate"


def test_add_or_drop_excludes_allocated_fixed_costs():
    result = run_decision(
        "add_or_drop",
        {
            "revenue": "900000",
            "variable_costs": "540000",
            "avoidable_fixed": "120000",
            "allocated_fixed": "80000",
            "lost_units": "200",
            "related_contribution_per_unit": "60",
            "materiality": "1000",
        },
    )
    assert result.value == Decimal("228000")
    assert result.verdict == "keep"
    assert result.meta["allocated_fixed_memo"] == "80000"
    assert result.fact_map()["f_contribution"].value == Decimal("360000")


def test_sell_or_process_ignores_the_joint_cost():
    result = run_decision(
        "sell_or_process",
        {
            "units": "5000",
            "split_off_price": "120",
            "processed_price": "180",
            "processing_cost_per_unit": "30",
            "incremental_fixed": "50000",
            "joint_cost": "900000",
        },
    )
    assert result.value == Decimal("100000")
    assert result.verdict == "process further"
    assert result.meta["joint_cost_memo"] == "900000"


def test_operate_or_shutdown_compares_savings_with_lost_contribution():
    result = run_decision(
        "operate_or_shutdown",
        {
            "units": "0",
            "price": "400",
            "variable_cost": "320",
            "avoidable_fixed": "150000",
            "restart_cost": "20000",
            "materiality": "1000",
        },
    )
    assert result.value == Decimal("130000")
    assert result.verdict == "shut down"


def test_product_mix_ranks_by_contribution_per_scarce_unit():
    result = run_decision(
        "product_mix",
        {
            "capacity_units": "10000",
            "products": [
                {
                    "product_id": "A",
                    "price": "100",
                    "variable_cost": "60",
                    "demand_units": "400",
                    "resource_units_per_unit": "2",
                },
                {
                    "product_id": "B",
                    "price": "90",
                    "variable_cost": "40",
                    "demand_units": "600",
                    "resource_units_per_unit": "1",
                },
            ],
        },
    )
    assert result.value == Decimal("46000")
    assert [row["product_id"] for row in result.meta["ranking"]] == ["B", "A"]
    assert result.meta["ranking"][0]["contribution_per_resource"] == "50"


def test_cvp_break_even_and_operating_leverage():
    result = run_decision(
        "cvp",
        {
            "price": "500",
            "variable_cost": "300",
            "fixed_costs": "400000",
            "actual_units": "3000",
        },
    )
    facts = result.fact_map()
    assert facts["f_be_units"].value == Decimal("2000")
    assert facts["f_be_revenue"].value == Decimal("1000000")
    assert facts["f_margin"].value == Decimal("1000")
    assert facts["f_dol"].value == Decimal("3")
    assert result.value == Decimal("200000")


def test_cvp_with_no_contribution_returns_a_domain_error():
    with pytest.raises(DomainError) as caught:
        run_decision(
            "cvp",
            {"price": "300", "variable_cost": "300", "fixed_costs": "1000", "actual_units": "10"},
        )
    assert caught.value.code == "CVP_CONTRIBUTION_NONPOSITIVE"
    assert caught.value.remediation


def test_unsupported_decision_lists_the_registered_set():
    with pytest.raises(DomainError) as caught:
        run_decision("close_the_books", {})
    assert caught.value.code == "UNSUPPORTED_DECISION"
    assert "special_order" in caught.value.remediation
