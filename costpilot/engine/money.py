"""Decimal-only money and quantity policy.

Floats are rejected at the boundary. Division uses a wide explicit context.
Rounding happens once, at rendering, never inside a calculation.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
from typing import Any

CONTEXT = Context(prec=34)
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

CURRENCY_QUANT = Decimal("0.01")
RATE_QUANT = Decimal("0.0001")
QTY_QUANT = Decimal("0.0001")
PCT_QUANT = Decimal("0.01")


class MoneyError(ValueError):
    """Raised when an input violates the numeric policy."""


def dec(value: Any, field: str = "value") -> Decimal:
    """Construct a Decimal from a string, int, or Decimal. Never from a float."""
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise MoneyError(f"{field} must be a number, not a boolean")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            raise MoneyError(f"{field} is empty")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"{field} is not a decimal: {value!r}") from exc
    else:
        raise MoneyError(
            f"{field} must be a decimal string, int, or Decimal; got {type(value).__name__}"
        )
    if not result.is_finite():
        raise MoneyError(f"{field} must be finite")
    return result


def add(*values: Decimal) -> Decimal:
    total = ZERO
    for value in values:
        if not isinstance(value, Decimal):
            raise MoneyError("engine arithmetic requires Decimal operands")
        total += value
    return total


def mul(left: Decimal, right: Decimal) -> Decimal:
    if not isinstance(left, Decimal) or not isinstance(right, Decimal):
        raise MoneyError("engine arithmetic requires Decimal operands")
    with localcontext(CONTEXT):
        return left * right


def div(left: Decimal, right: Decimal, field: str = "division") -> Decimal:
    if not isinstance(left, Decimal) or not isinstance(right, Decimal):
        raise MoneyError("engine arithmetic requires Decimal operands")
    if right == ZERO:
        raise MoneyError(f"{field} by zero")
    with localcontext(CONTEXT):
        return left / right


def pct(part: Decimal, whole: Decimal, field: str = "percentage") -> Decimal:
    return mul(div(part, whole, field), HUNDRED)


def quantize(value: Decimal, places: Decimal) -> Decimal:
    return value.quantize(places, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> Decimal:
    return quantize(value, CURRENCY_QUANT)


def rate(value: Decimal) -> Decimal:
    return quantize(value, RATE_QUANT)


def qty(value: Decimal) -> Decimal:
    return quantize(value, QTY_QUANT)


def trace_str(value: Decimal) -> str:
    """Full-precision string used in traces and JSON payloads."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def fmt_money(value: Decimal) -> str:
    rounded = money(value)
    text = f"{abs(rounded):,.2f}"
    return f"({text})" if rounded < ZERO else text


def fmt_rate(value: Decimal) -> str:
    return f"{rate(value):,.4f}"


def fmt_qty(value: Decimal) -> str:
    return f"{qty(value):,.4f}".rstrip("0").rstrip(".")


def fmt_pct(value: Decimal) -> str:
    return f"{quantize(value, PCT_QUANT):,.2f}%"


def display_amount(value: Decimal, unit: str) -> str:
    if unit in {"INR", "USD", "EUR", "GBP"}:
        return fmt_money(value)
    if unit == "INR/unit":
        return fmt_money(value)
    if unit == "%":
        return fmt_pct(value)
    if unit == "ratio":
        return fmt_pct(value * HUNDRED)
    if unit == "score":
        return f"{quantize(value, PCT_QUANT):,.2f}"
    if unit.endswith("/unit") or unit.endswith("/hour") or unit.endswith("/kg"):
        return fmt_rate(value)
    if unit in {"units", "kg", "hours", "setups", "orders"}:
        return fmt_qty(value)
    return trace_str(value)
