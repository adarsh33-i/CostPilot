"""Account flux keeps an honest unexplained residual and never invents drivers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.errors import DomainError
from costpilot.engine.variance import flux


def lines():
    return [
        flux.FluxLine("4010", "Revenue", "revenue", Decimal("90"), Decimal("100"), Decimal("95")),
        flux.FluxLine("5010", "Material", "cost", Decimal("110"), Decimal("100"), Decimal("90")),
    ]


def test_flux_is_favorable_positive_and_labelled_unexplained():
    result = flux.run(flux.FluxInput(period_id="2026-09", lines=lines(), materiality=Decimal("5")))
    rows = {row["account_id"]: row for row in result.meta["lines"]}
    assert rows["4010"]["budget_variance"] == "-10"
    assert rows["4010"]["favorable"] is False
    assert rows["4010"]["favorable_amount"] == "-10"
    assert rows["5010"]["budget_variance"] == "10"
    assert rows["5010"]["favorable"] is False
    assert rows["5010"]["favorable_amount"] == "-10"
    assert rows["5010"]["residual"] == "10"
    assert rows["5010"]["decomposition_status"] == "unavailable"
    assert result.value == Decimal("-20")
    assert result.meta["decomposition_status"] == "unavailable"


def test_favorable_cost_underspend_is_positive():
    result = flux.run(
        flux.FluxInput(
            period_id="2026-09",
            lines=[flux.FluxLine("5010", "Material", "cost", Decimal("90"), Decimal("100"), Decimal("95"))],
        )
    )
    row = result.meta["lines"][0]
    assert row["budget_variance"] == "-10"
    assert row["favorable"] is True
    assert row["favorable_amount"] == "10"
    assert result.value == Decimal("10")


def test_materiality_counts_material_accounts():
    result = flux.run(flux.FluxInput(period_id="2026-09", lines=lines(), materiality=Decimal("9")))
    assert result.meta["material_accounts"] == 2


def test_explained_components_reduce_the_residual():
    result = flux.run(
        flux.FluxInput(
            period_id="2026-09",
            lines=[
                flux.FluxLine(
                    "5010", "Material", "cost", Decimal("110"), Decimal("100"), Decimal("90"),
                    explained=Decimal("6"),
                )
            ],
        )
    )
    assert result.meta["lines"][0]["residual"] == "4"


def test_flux_warns_that_decomposition_is_unavailable():
    result = flux.run(flux.FluxInput(period_id="2026-09", lines=lines()))
    warning = next(w for w in result.warnings if w.code == "decomposition_unavailable")
    assert warning.remediation


def test_flux_requires_accounts_and_known_kinds():
    with pytest.raises(DomainError):
        flux.run(flux.FluxInput(period_id="2026-09", lines=[]))
    with pytest.raises(DomainError):
        flux.run(flux.FluxInput(
            period_id="2026-09",
            lines=[flux.FluxLine("1", "Odd", "other", Decimal("1"), Decimal("1"), Decimal("1"))],
        ))
