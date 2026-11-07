"""Standard-cost variances on the PRD-B / PLT-01 / 2026-09 fixture row.

Hand-computed expectations:
  output 1,800; used 5,437.8 kg at 60.78; hours 2,903.04 at 50.60.
  standards: 3.0 kg at 60; 1.6 h at 50; VOH 40 per unit (25/h); fixed 60 per unit.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.errors import DomainError
from costpilot.engine.variance import labor, material, overhead


def material_run(**overrides):
    defaults = dict(
        actual_output=Decimal("1800"),
        standard_qty_per_unit=Decimal("3"),
        standard_price=Decimal("60"),
        actual_qty_used=Decimal("5437.8"),
        actual_price=Decimal("60.78"),
    )
    defaults.update(overrides)
    return material.run(material.MaterialVarianceInput(**defaults))


def test_material_price_usage_and_cost_variances():
    result = material_run()
    facts = result.fact_map()
    assert facts["f_mpv"].value == Decimal("-4241.484")
    assert facts["f_muv"].value == Decimal("-2268")
    assert facts["f_mcv"].value == Decimal("-6509.484")
    assert result.value == Decimal("-6509.484")
    assert result.meta["validation"]["failures"] == []


def test_purchase_basis_supplement_does_not_change_the_cost_variance():
    result = material_run(purchased_qty=Decimal("6000"))
    facts = result.fact_map()
    assert facts["f_mpv_purchase"].value == Decimal("-4680")
    assert facts["f_mcv"].value == Decimal("-6509.484")
    assert any(w.code == "purchase_usage_basis" for w in result.warnings)


def test_labour_rate_efficiency_and_cost_variances():
    result = labor.run(
        labor.LabourVarianceInput(
            actual_output=Decimal("1800"),
            standard_hours_per_unit=Decimal("1.6"),
            standard_rate=Decimal("50"),
            actual_hours=Decimal("2903.04"),
            actual_rate=Decimal("50.60"),
        )
    )
    facts = result.fact_map()
    assert facts["f_lrv"].value == Decimal("-1741.824")
    assert facts["f_lev"].value == Decimal("-1152")
    assert facts["f_lcv"].value == Decimal("-2893.824")


def test_variable_overhead_spending_and_efficiency():
    result = overhead.variable_overhead(
        overhead.VariableOverheadInput(
            actual_hours=Decimal("2903.04"),
            standard_hours_allowed=Decimal("2880"),
            standard_rate=Decimal("25"),
            actual_variable_overhead=Decimal("73011.46"),
        )
    )
    facts = result.fact_map()
    assert facts["f_voh_spending"].value == Decimal("-435.46")
    assert facts["f_voh_efficiency"].value == Decimal("-576")
    assert facts["f_voh_total"].value == Decimal("-1011.46")


def test_fixed_overhead_volume_variance_is_zero_at_denominator_activity():
    result = overhead.fixed_overhead(
        overhead.FixedOverheadInput(
            budgeted_fixed=Decimal("108000"),
            actual_fixed=Decimal("108432"),
            standard_hours_allowed=Decimal("2880"),
            denominator_hours=Decimal("2880"),
            fixed_rate=Decimal("37.5"),
        )
    )
    facts = result.fact_map()
    assert facts["f_foh_spending"].value == Decimal("-432")
    assert facts["f_foh_volume"].value == Decimal("0")
    assert facts["f_foh_total"].value == Decimal("-432")


def test_fixed_overhead_volume_variance_uses_denominator_hours():
    result = overhead.fixed_overhead(
        overhead.FixedOverheadInput(
            budgeted_fixed=Decimal("108000"),
            actual_fixed=Decimal("108000"),
            standard_hours_allowed=Decimal("2880"),
            denominator_hours=Decimal("3000"),
            fixed_rate=Decimal("36"),
        )
    )
    assert result.fact_map()["f_foh_volume"].value == Decimal("-4320")


@pytest.mark.parametrize(
    "call",
    [
        lambda: material_run(actual_output=Decimal("0")),
        lambda: material_run(actual_price=Decimal("-1")),
        lambda: labor.run(
            labor.LabourVarianceInput(
                Decimal("1800"), Decimal("1.6"), Decimal("50"), Decimal("2903.04"), Decimal("-1")
            )
        ),
        lambda: overhead.variable_overhead(
            overhead.VariableOverheadInput(
                Decimal("100"), Decimal("90"), Decimal("25"), Decimal("-1")
            )
        ),
        lambda: overhead.fixed_overhead(
            overhead.FixedOverheadInput(
                Decimal("1"), Decimal("1"), Decimal("1"), Decimal("0"), Decimal("1")
            )
        ),
    ],
)
def test_invalid_variance_inputs_are_domain_errors(call):
    with pytest.raises(DomainError):
        call()
