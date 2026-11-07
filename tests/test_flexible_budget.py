"""Flexible-budget report: volume is separated from controllable variance."""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.errors import DomainError
from costpilot.engine.variance import flexible_budget as fb


def report(**overrides):
    payload = dict(
        budget_volume=Decimal("1000"),
        actual_volume=Decimal("1200"),
        lines=[
            fb.FlexibleBudgetLine("Revenue", "revenue", Decimal("100000"), Decimal("117600")),
            fb.FlexibleBudgetLine(
                "Variable cost", "variable_cost", Decimal("60000"), Decimal("74400"), Decimal("60")
            ),
            fb.FlexibleBudgetLine("Fixed cost", "fixed_cost", Decimal("20000"), Decimal("19500")),
        ],
        period_id="2026-09",
    )
    payload.update(overrides)
    return fb.run(fb.FlexibleBudgetInput(**payload))


def test_flexed_lines_and_totals():
    result = report()
    lines = {line["name"]: line for line in result.meta["lines"]}
    assert lines["Revenue"]["flexed_budget"] == "120000"
    assert lines["Revenue"]["flexible_variance"] == "-2400"
    assert lines["Revenue"]["volume_variance"] == "20000"
    assert lines["Variable cost"]["flexed_budget"] == "72000"
    assert lines["Variable cost"]["flexible_variance"] == "-2400"
    assert lines["Variable cost"]["volume_variance"] == "-12000"
    assert lines["Fixed cost"]["flexed_budget"] == "20000"
    assert lines["Fixed cost"]["flexible_variance"] == "500"


def test_separate_flexible_and_volume_totals():
    result = report()
    facts = result.fact_map()
    assert facts["f_flexible"].value == Decimal("-4300")
    assert facts["f_volume"].value == Decimal("8000")
    assert facts["f_total"].value == Decimal("3700")
    assert result.value == Decimal("3700")


def test_flexed_variable_cost_falls_back_to_static_rate():
    result = report(
        lines=[
            fb.FlexibleBudgetLine("Variable cost", "variable_cost", Decimal("60000"), Decimal("74400")),
        ]
    )
    assert result.meta["lines"][0]["flexed_budget"] == "72000"


def test_fixed_line_does_not_flex():
    result = report(
        lines=[fb.FlexibleBudgetLine("Fixed cost", "fixed_cost", Decimal("20000"), Decimal("19500"))]
    )
    assert result.meta["lines"][0]["flexed_budget"] == "20000"
    assert result.meta["lines"][0]["volume_variance"] == "0"


@pytest.mark.parametrize(
    "overrides",
    [
        {"budget_volume": Decimal("0")},
        {"actual_volume": Decimal("-1")},
        {"lines": []},
        {"lines": [fb.FlexibleBudgetLine("Odd", "other", Decimal("1"), Decimal("1"))]},
    ],
)
def test_invalid_flexible_budget_inputs(overrides):
    with pytest.raises(DomainError):
        report(**overrides)
