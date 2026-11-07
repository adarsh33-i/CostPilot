"""Decimal policy: no floats, no silent rounding, display-only formatting."""

from __future__ import annotations

from decimal import Decimal

import pytest

from costpilot.engine.money import (
    MoneyError,
    dec,
    div,
    fmt_money,
    fmt_pct,
    fmt_qty,
    fmt_rate,
    mul,
    trace_str,
)


def test_dec_accepts_strings_ints_and_decimals():
    assert dec("1,234.50") == Decimal("1234.50")
    assert dec(" 42 ") == Decimal("42")
    assert dec(7) == Decimal("7")
    assert dec(Decimal("0.5")) == Decimal("0.5")


@pytest.mark.parametrize("value", [1.5, True, None, "", "abc", "NaN", "Infinity"])
def test_dec_rejects_non_decimal_inputs(value):
    with pytest.raises(MoneyError):
        dec(value)


def test_arithmetic_requires_decimal_operands():
    with pytest.raises(MoneyError):
        mul(Decimal("2"), 3)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        div(Decimal("2"), 0)


def test_trace_strings_keep_precision():
    assert trace_str(Decimal("120000.00")) == "120000"
    assert trace_str(Decimal("-6509.484")) == "-6509.484"
    assert trace_str(Decimal("0")) == "0"


def test_display_rounds_once_and_parenthesises_negatives():
    assert fmt_money(Decimal("120000")) == "120,000.00"
    assert fmt_money(Decimal("-60000.004")) == "(60,000.00)"
    assert fmt_rate(Decimal("37.50")) == "37.5000"
    assert fmt_qty(Decimal("1800.0000")) == "1,800"
    assert fmt_pct(Decimal("12.345")) == "12.35%"
