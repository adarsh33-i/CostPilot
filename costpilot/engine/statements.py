"""Deterministic financial statements, ratio analysis and turnover models.

Two modes share one rulebook:

* ``reported`` reads the printed lines from an attachment, recomputes missing
  subtotals from accounting identities, and warns when a printed subtotal
  disagrees with its components. A missing cash flow is never invented.
* ``model`` builds a fully consistent income statement, balance sheet and
  indirect cash flow from turnover and explicit assumptions. Cash is the
  balancing item, so the balance sheet and the cash flow tie by construction.

Every statement line and every ratio is a Decimal step with a registered rule,
a formula, its inputs, and a source. The AI model narrates the result; it does
not choose a number.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from costpilot.data.statements import StatementError, parse_statements
from costpilot.engine.errors import DomainError
from costpilot.engine.money import (
    HUNDRED,
    ONE,
    ZERO,
    MoneyError,
    dec,
    display_amount,
    div,
    mul,
    trace_str,
)
from costpilot.engine.narration import render
from costpilot.engine.trace import Assumption, CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result

CURRENCIES = {"INR", "USD", "EUR", "GBP"}
REPORTED_RULE = "FIN-001"
INCOME_RULE = "FIN-002"
BALANCE_RULE = "FIN-004"
CASH_RULE = "FIN-007"

GROUP_LABELS = {
    "income": "Income statement",
    "balance": "Balance sheet",
    "cashflow": "Cash flow statement",
}

INCOME_LINES = (
    ("revenue", "Revenue", ""),
    ("cost_of_sales", "Cost of sales", ""),
    ("gross_profit", "Gross profit", ""),
    ("operating_expenses", "Operating expenses", ""),
    ("ebitda", "EBITDA", ""),
    ("depreciation", "Depreciation and amortisation", ""),
    ("ebit", "Operating profit (EBIT)", ""),
    ("interest", "Finance cost", ""),
    ("pbt", "Profit before tax", ""),
    ("tax", "Tax expense", ""),
    ("net_income", "Net profit", ""),
)
BALANCE_LINES = (
    ("fixed_assets", "Property, plant and equipment", "Non-current assets"),
    ("other_noncurrent_assets", "Other non-current assets", "Non-current assets"),
    ("inventory", "Inventories", "Current assets"),
    ("receivables", "Trade receivables", "Current assets"),
    ("cash", "Cash and cash equivalents", "Current assets"),
    ("other_current_assets", "Other current assets", "Current assets"),
    ("total_current_assets", "Total current assets", "Current assets"),
    ("total_assets", "Total assets", ""),
    ("share_capital", "Share capital", "Equity"),
    ("reserves", "Reserves and surplus", "Equity"),
    ("equity", "Total equity", "Equity"),
    ("long_term_debt", "Long-term borrowings", "Non-current liabilities"),
    ("other_noncurrent_liabilities", "Other non-current liabilities", "Non-current liabilities"),
    ("payables", "Trade payables", "Current liabilities"),
    ("short_term_debt", "Short-term borrowings", "Current liabilities"),
    ("other_current_liabilities", "Other current liabilities", "Current liabilities"),
    ("total_current_liabilities", "Total current liabilities", "Current liabilities"),
    ("total_liabilities", "Total liabilities", ""),
    ("total_liabilities_and_equity", "Total liabilities and equity", ""),
)
CASHFLOW_LINES = (
    ("cfo", "Net cash from operating activities", "Operating"),
    ("capex", "Capital expenditure", "Investing"),
    ("cfi", "Net cash from investing activities", "Investing"),
    ("dividends", "Dividends paid", "Financing"),
    ("cff", "Net cash from financing activities", "Financing"),
    ("net_change_cash", "Net change in cash", ""),
    ("opening_cash", "Cash at the beginning", ""),
    ("closing_cash", "Cash at the end", ""),
)

LINE_DEFS = {"income": INCOME_LINES, "balance": BALANCE_LINES, "cashflow": CASHFLOW_LINES}
LINE_META: dict[str, tuple[str, str, str]] = {}
for _group, _lines in LINE_DEFS.items():
    for _key, _label, _section in _lines:
        LINE_META[_key] = (_group, _label, _section)

DERIVED_RULES = {"income": INCOME_RULE, "balance": BALANCE_RULE, "cashflow": CASH_RULE}

# Assumption key -> (default, label, basis). Shares are fractions, days are
# days of the named driver, and the opening balance is an amount.
MODEL_ASSUMPTIONS: dict[str, tuple[Decimal, str, str]] = {
    "gross_margin": (Decimal("0.40"), "Gross margin", "of turnover"),
    "opex_ratio": (Decimal("0.25"), "Operating expenses", "of turnover"),
    "depreciation_ratio": (Decimal("0.04"), "Depreciation", "of turnover"),
    "interest_rate": (Decimal("0.08"), "Interest rate", "on modelled debt"),
    "tax_rate": (Decimal("0.25"), "Tax rate", "of profit before tax"),
    "inventory_days": (Decimal("30"), "Inventory days", "of cost of sales"),
    "receivable_days": (Decimal("30"), "Receivable days", "of turnover"),
    "payable_days": (Decimal("30"), "Payable days", "of cost of sales"),
    "fixed_asset_ratio": (Decimal("0.30"), "Net fixed assets", "of turnover"),
    "debt_ratio": (Decimal("0.20"), "Debt", "of turnover"),
    "short_term_debt_share": (Decimal("0.40"), "Short-term debt", "of total debt"),
    "other_current_liabilities_ratio": (Decimal("0.03"), "Other current liabilities", "of turnover"),
    "other_noncurrent_liabilities_ratio": (
        Decimal("0.02"),
        "Other non-current liabilities",
        "of turnover",
    ),
    "share_capital_ratio": (Decimal("0.10"), "Share capital", "of turnover"),
    "dividend_payout": (Decimal("0.00"), "Dividend payout", "of net profit"),
    "capex_ratio": (Decimal("0.15"), "Capital expenditure", "of net fixed assets"),
    "opening_working_capital": (ZERO, "Opening net working capital", "at the start"),
}
BOUNDED_SHARES = {"gross_margin", "interest_rate", "tax_rate", "short_term_debt_share", "dividend_payout"}
SHARE_KEYS = {
    "gross_margin", "opex_ratio", "depreciation_ratio", "interest_rate", "tax_rate",
    "short_term_debt_share", "other_current_liabilities_ratio",
    "other_noncurrent_liabilities_ratio", "share_capital_ratio", "dividend_payout",
    "fixed_asset_ratio", "debt_ratio", "capex_ratio",
}


class _Book:
    """A result under construction: steps, facts and the source list."""

    def __init__(self, currency: str, source_refs: list[str]) -> None:
        self.builder = StepBuilder()
        self.facts: dict[str, Fact] = {}
        self.currency = currency
        self.source_refs = list(source_refs)

    def add(self, **kwargs: Any):
        refs = kwargs.pop("source_refs", self.source_refs)
        return self.builder.add(source_refs=refs, **kwargs)

    def fact(self, fact_id: str, step: Any, label: str | None = None) -> None:
        self.facts[fact_id] = Fact(fact_id, step.step_id, label or step.label, step.value, step.unit)


def _attachment_name(attachment: dict[str, Any] | None) -> str:
    return str((attachment or {}).get("name") or "attachment")


def _sum_present(values: dict[str, Decimal], keys: tuple[str, ...]) -> Decimal | None:
    present = [values[key] for key in keys if key in values]
    return sum(present, ZERO) if present else None


def _total_debt(values: dict[str, Decimal]) -> Decimal | None:
    return _sum_present(values, ("short_term_debt", "long_term_debt"))


def _derive(base: dict[str, Decimal]) -> tuple[dict[str, Decimal], dict[str, dict[str, Any]]]:
    """Fill missing subtotals from accounting identities, never overwriting."""
    values = dict(base)
    derived: dict[str, dict[str, Any]] = {}

    def put(key: str, formula: str, inputs: tuple[str, ...], fn: Any) -> None:
        if key in values or any(name not in values for name in inputs):
            return
        values[key] = fn(values)
        derived[key] = {"formula": formula, "inputs": {name: values[name] for name in inputs}}

    def put_sum(key: str, parts: tuple[str, ...], formula: str) -> None:
        if key in values:
            return
        present = [part for part in parts if part in values]
        if len(present) < 2:
            return
        values[key] = sum((values[part] for part in present), ZERO)
        derived[key] = {
            "formula": formula,
            "inputs": {part: values[part] for part in present},
        }

    put("gross_profit", "revenue - cost of sales", ("revenue", "cost_of_sales"),
        lambda v: v["revenue"] - v["cost_of_sales"])
    put("ebitda", "gross profit - operating expenses", ("gross_profit", "operating_expenses"),
        lambda v: v["gross_profit"] - v["operating_expenses"])
    put("ebit", "EBITDA - depreciation", ("ebitda", "depreciation"),
        lambda v: v["ebitda"] - v["depreciation"])
    put("pbt", "operating profit - finance cost", ("ebit", "interest"),
        lambda v: v["ebit"] - v["interest"])
    put("net_income", "profit before tax - tax", ("pbt", "tax"),
        lambda v: v["pbt"] - v["tax"])

    put_sum("total_current_assets",
            ("inventory", "receivables", "cash", "other_current_assets"),
            "inventory + receivables + cash + other current assets")
    put("total_assets",
        "fixed assets + other non-current assets + total current assets",
        ("fixed_assets", "total_current_assets"),
        lambda v: v["fixed_assets"] + v.get("other_noncurrent_assets", ZERO) + v["total_current_assets"])
    put_sum("total_current_liabilities",
            ("payables", "short_term_debt", "other_current_liabilities"),
            "payables + short-term borrowings + other current liabilities")
    put("equity", "share capital + reserves", ("share_capital", "reserves"),
        lambda v: v["share_capital"] + v["reserves"])
    put("total_liabilities",
        "total current liabilities + long-term borrowings + other non-current liabilities",
        ("total_current_liabilities",),
        lambda v: v["total_current_liabilities"]
        + v.get("long_term_debt", ZERO)
        + v.get("other_noncurrent_liabilities", ZERO))
    put("total_liabilities_and_equity", "total liabilities + total equity",
        ("total_liabilities", "equity"),
        lambda v: v["total_liabilities"] + v["equity"])

    if (
        "net_change_cash" not in values
        and all(key in values for key in ("cfo", "cfi", "cff"))
    ):
        values["net_change_cash"] = values["cfo"] + values["cfi"] + values["cff"]
        derived["net_change_cash"] = {
            "formula": "net cash from operating + investing + financing activities",
            "inputs": {key: values[key] for key in ("cfo", "cfi", "cff")},
        }
    put("closing_cash", "opening cash + net change in cash",
        ("opening_cash", "net_change_cash"),
        lambda v: v["opening_cash"] + v["net_change_cash"])
    return values, derived


def _conflicts(values: dict[str, Decimal]) -> list[tuple[str, Decimal, str]]:
    """Recompute printed subtotals from their components for a sanity check."""
    checks: list[tuple[str, Decimal, str]] = []

    def sub(key: str, left: str, right: str, description: str) -> None:
        if key in values and left in values and right in values:
            checks.append((key, values[left] - values[right], description))

    sub("gross_profit", "revenue", "cost_of_sales", "revenue minus cost of sales")
    sub("ebitda", "gross_profit", "operating_expenses", "gross profit minus operating expenses")
    sub("ebit", "ebitda", "depreciation", "EBITDA minus depreciation")
    sub("pbt", "ebit", "interest", "operating profit minus finance cost")
    sub("net_income", "pbt", "tax", "profit before tax minus tax")

    def total(key: str, parts: tuple[str, ...], description: str) -> None:
        if key not in values:
            return
        present = [part for part in parts if part in values]
        if len(present) < 2:
            return
        checks.append((key, sum((values[part] for part in present), ZERO), description))

    total("total_current_assets",
          ("inventory", "receivables", "cash", "other_current_assets"),
          "the current asset lines")
    total("total_current_liabilities",
          ("payables", "short_term_debt", "other_current_liabilities"),
          "the current liability lines")
    if "total_assets" in values and "fixed_assets" in values and "total_current_assets" in values:
        checks.append((
            "total_assets",
            values["fixed_assets"]
            + values.get("other_noncurrent_assets", ZERO)
            + values["total_current_assets"],
            "fixed assets plus current assets",
        ))
    if "total_liabilities" in values and "total_current_liabilities" in values:
        checks.append((
            "total_liabilities",
            values["total_current_liabilities"]
            + values.get("long_term_debt", ZERO)
            + values.get("other_noncurrent_liabilities", ZERO),
            "current and non-current liabilities",
        ))
    if "equity" in values and "share_capital" in values and "reserves" in values:
        checks.append(("equity", values["share_capital"] + values["reserves"],
                       "share capital plus reserves"))
    if (
        "total_liabilities_and_equity" in values
        and "total_liabilities" in values
        and "equity" in values
    ):
        checks.append((
            "total_liabilities_and_equity",
            values["total_liabilities"] + values["equity"],
            "total liabilities plus equity",
        ))
    return checks


def _reported_meta(
    extract: Any,
    values: dict[str, Decimal],
    prior: dict[str, Decimal],
    derived: dict[str, dict[str, Any]],
    prior_derived: dict[str, dict[str, Any]],
    attachment_name: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    current: dict[str, dict[str, Any]] = {}
    prior_meta: dict[str, dict[str, Any]] = {}
    printed_formula = "read the printed amount and apply the stated scale once"
    for key in values:
        if key not in LINE_META:
            continue
        group = LINE_META[key][0]
        if key in derived:
            item = derived[key]
            current[key] = {
                "rule": DERIVED_RULES[group],
                "formula": item["formula"],
                "inputs": item["inputs"],
                "refs": [attachment_name, "derived from reported lines"],
                "note": "derived from reported lines",
            }
        else:
            source = extract.sources.get(key, "attachment")
            current[key] = {
                "rule": REPORTED_RULE,
                "formula": printed_formula,
                "inputs": {
                    "line": extract.labels.get(key, key),
                    "period": extract.current_period or "current",
                },
                "refs": [attachment_name, source],
                "note": source,
            }
    for key in prior:
        if key not in LINE_META:
            continue
        group = LINE_META[key][0]
        if key in prior_derived:
            item = prior_derived[key]
            prior_meta[key] = {
                "rule": DERIVED_RULES[group],
                "formula": item["formula"],
                "inputs": item["inputs"],
                "refs": [attachment_name, "derived from reported lines"],
                "note": "derived from reported lines",
            }
        else:
            source = extract.sources.get(key, "attachment")
            prior_meta[key] = {
                "rule": REPORTED_RULE,
                "formula": printed_formula,
                "inputs": {
                    "line": extract.labels.get(key, key),
                    "period": extract.prior_period or "prior",
                },
                "refs": [attachment_name, source],
                "note": source,
            }
    return current, prior_meta


def _emit_rows(
    book: _Book,
    values: dict[str, Decimal],
    prior: dict[str, Decimal],
    meta: dict[str, dict[str, Any]],
    prior_meta: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {"income": [], "balance": [], "cashflow": []}
    for group in ("income", "balance", "cashflow"):
        for key, label, section in LINE_DEFS[group]:
            if key not in values:
                continue
            entry = meta[key]
            step = book.add(
                step_id=f"line_{key}",
                rule_id=entry["rule"],
                label=label,
                formula=entry["formula"],
                inputs=entry["inputs"],
                value=values[key],
                unit=book.currency,
                source_refs=entry["refs"],
                note=entry.get("note"),
            )
            book.fact(f"f_{key}", step, label)
            row = {
                "line": key,
                "label": label,
                "group": GROUP_LABELS[group],
                "section": section,
                "value": trace_str(values[key]),
                "display": display_amount(values[key], book.currency),
                "unit": book.currency,
                "rule": entry["rule"],
                "formula": entry["formula"],
                "source": entry.get("note") or ", ".join(entry["refs"]),
                "derived": entry["rule"] != REPORTED_RULE,
            }
            if key in prior:
                p_entry = prior_meta.get(key, entry)
                book.add(
                    step_id=f"line_{key}_prior",
                    rule_id=p_entry["rule"],
                    label=f"{label} (prior)",
                    formula=p_entry["formula"],
                    inputs=p_entry["inputs"],
                    value=prior[key],
                    unit=book.currency,
                    source_refs=p_entry["refs"],
                    note=p_entry.get("note"),
                )
                row["prior"] = trace_str(prior[key])
                row["prior_display"] = display_amount(prior[key], book.currency)
            rows[group].append(row)
    return rows


def _balance_check(
    book: _Book, values: dict[str, Decimal], warnings: list[Warning]
) -> dict[str, Any] | None:
    assets = values.get("total_assets")
    claims = values.get("total_liabilities_and_equity")
    if assets is None or claims is None:
        return None
    difference = assets - claims
    materiality = max(ONE, abs(assets) * Decimal("0.001"))
    step = book.add(
        step_id="balance_check",
        rule_id="FIN-005",
        label="Balance sheet check",
        formula="total assets - (total liabilities + total equity)",
        inputs={"total_assets": assets, "total_liabilities_and_equity": claims},
        value=difference,
        unit=book.currency,
        source_refs=book.source_refs,
    )
    book.fact("f_balance_diff", step, "Balance sheet difference")
    ok = abs(difference) <= materiality
    if not ok:
        warnings.append(
            Warning(
                "FIN_BALANCE",
                "Total assets do not equal total liabilities plus equity.",
                "warning",
                "Check the printed lines, or rename the rows to the standard statement labels "
                "and upload again.",
            )
        )
    return {
        "difference": trace_str(difference),
        "display": display_amount(difference, book.currency),
        "materiality": trace_str(materiality),
        "ok": ok,
    }


def _reported(
    attachment: dict[str, Any] | None,
    *,
    period: str | None,
    currency: str,
    source_refs: list[str],
    warnings: list[Warning],
) -> tuple[_Book, dict[str, Any]]:
    if attachment is None:
        raise DomainError(
            "E_STATEMENT_SOURCE",
            "Reported statements need an attachment.",
            "Attach the Excel, PDF or CSV that holds the statements, or state the annual turnover "
            "so the statements can be modelled.",
        )
    try:
        extract = parse_statements(attachment, period=period)
    except StatementError as exc:
        raise DomainError(exc.code, exc.message, exc.remediation) from exc

    attachment_name = _attachment_name(attachment)
    book = _Book(currency, source_refs)
    values, derived = _derive(extract.values)
    prior, prior_derived = _derive(extract.prior)

    for key, expected, description in _conflicts(values):
        if key in derived:
            continue
        difference = values[key] - expected
        if abs(difference) > ONE:
            label = LINE_META.get(key, (None, key, None))[1]
            warnings.append(
                Warning(
                    "FIN_CONFLICT",
                    f"The printed {label.lower()} differs from {description} by "
                    f"{display_amount(difference, currency)}; the printed subtotal is kept.",
                    "info",
                    "Check the component rows if the difference is unexpected.",
                )
            )

    if "net_change_cash" in extract.values and all(
        key in extract.values for key in ("cfo", "cfi", "cff")
    ):
        expected = sum((extract.values[key] for key in ("cfo", "cfi", "cff")), ZERO)
        difference = extract.values["net_change_cash"] - expected
        if abs(difference) > ONE:
            warnings.append(
                Warning(
                    "FIN_CASHFLOW",
                    f"The printed net change in cash differs from the three activities by "
                    f"{display_amount(difference, currency)}.",
                    "info",
                    "Check the operating, investing and financing totals.",
                )
            )
    if all(key in extract.values for key in ("opening_cash", "net_change_cash", "closing_cash")):
        expected = extract.values["opening_cash"] + extract.values["net_change_cash"]
        difference = extract.values["closing_cash"] - expected
        if abs(difference) > ONE:
            warnings.append(
                Warning(
                    "FIN_CASHFLOW",
                    f"The closing cash differs from opening cash plus the net change by "
                    f"{display_amount(difference, currency)}.",
                    "info",
                    "Check the opening and closing cash lines.",
                )
            )
    if not any(key in extract.values for key in ("cfo", "closing_cash")):
        warnings.append(
            Warning(
                "FIN_NO_CASHFLOW",
                "No cash flow statement was found in the attachment, so none is shown; "
                "it was not estimated.",
                "info",
                "Attach a cash flow statement, or state the annual turnover so a cash flow "
                "can be modelled from the balance sheet movements.",
            )
        )
    for message in extract.warnings:
        warnings.append(Warning("FIN_EXTRACT", message, "info"))

    meta_current, meta_prior = _reported_meta(
        extract, values, prior, derived, prior_derived, attachment_name
    )
    rows = _emit_rows(book, values, prior, meta_current, meta_prior)

    balance = _balance_check(book, values, warnings)
    return book, {
        "mode": "reported",
        "attachment_id": attachment.get("attachment_id"),
        "attachment_name": attachment.get("name"),
        "currency": currency,
        "period": extract.current_period,
        "prior_period": extract.prior_period,
        "statement_periods": list(extract.periods),
        "scales": dict(extract.scales),
        "unmatched": list(extract.unmatched),
        "statements": rows,
        "balance_check": balance,
        "values": values,
        "prior": prior,
    }


def _model_values(
    revenue: Decimal,
    prior_revenue: Decimal | None,
    provided: dict[str, Any],
    sources: dict[str, str],
    currency: str,
) -> tuple[dict[str, Decimal], list[Assumption]]:
    merged: dict[str, Decimal] = {}
    assumptions: list[Assumption] = []
    for key, (default, label, basis) in MODEL_ASSUMPTIONS.items():
        raw = provided.get(key)
        if raw is None:
            value = default
            source = "engine_default"
            confirmed = False
        else:
            try:
                value = dec(raw, key)
            except MoneyError as exc:
                raise DomainError(
                    "E_STATEMENT_ASSUMPTION",
                    f"{label} is not a valid number: {exc}.",
                    "Give every assumption as a plain decimal, for example 0.40 for a 40 percent "
                    "margin or 30 for thirty days.",
                ) from exc
            source = sources.get(key, "user_explicit")
            confirmed = True
        if value < ZERO:
            raise DomainError(
                "E_STATEMENT_ASSUMPTION",
                f"{label} cannot be negative.",
                "Use zero or a positive number for every statement assumption.",
            )
        if key in BOUNDED_SHARES and value > ONE:
            raise DomainError(
                "E_STATEMENT_ASSUMPTION",
                f"{label} is a share and must be between zero and one.",
                "Use 0.40 for forty percent rather than 40.",
            )
        merged[key] = value
        assumptions.append(
            Assumption(
                id=f"a_{key}",
                key=key,
                text=_assumption_text(key, value, currency, label, basis),
                source=source,
                confirmed=confirmed,
                confidence=ONE if confirmed else None,
            )
        )
    return merged, assumptions


def _assumption_text(
    key: str, value: Decimal, currency: str, label: str, basis: str
) -> str:
    if key == "opening_working_capital":
        return f"{label} is {display_amount(value, currency)}"
    if key.endswith("_days"):
        return f"{label} is {display_amount(value, 'days')} {basis}"
    if key in SHARE_KEYS:
        return f"{label} is {display_amount(mul(value, HUNDRED), '%')} {basis}"
    return f"{label} is {trace_str(value)} {basis}"


def _modelled(
    revenue: Decimal,
    prior_revenue: Decimal | None,
    assumptions: dict[str, Decimal],
    *,
    period: str | None,
    currency: str,
    source_refs: list[str],
    warnings: list[Warning],
) -> tuple[_Book, dict[str, Any], dict[str, Any]]:
    book = _Book(currency, source_refs)
    a = assumptions
    values: dict[str, Decimal] = {}
    meta: dict[str, dict[str, Any]] = {}

    def add(
        key: str,
        label: str,
        rule: str,
        formula: str,
        inputs: dict[str, Decimal],
        value: Decimal,
        source_key: str | None = None,
        refs: list[str] | None = None,
    ) -> Decimal:
        values[key] = value
        if refs is None:
            refs = (
                ["turnover input", f"assumption:{source_key}"]
                if source_key
                else ["modelled statements"]
            )
        meta[key] = {"rule": rule, "formula": formula, "inputs": dict(inputs), "refs": refs}
        return value

    def driver(key: str, label: str, source_key: str, basis: Decimal, formula: str) -> Decimal:
        return add(
            key,
            label,
            "FIN-003",
            formula,
            {"turnover": revenue, source_key: basis},
            mul(revenue, basis),
            source_key,
        )

    add(
        "revenue",
        "Revenue",
        "FIN-013",
        "use the turnover stated by the user once",
        {"turnover": revenue},
        revenue,
        refs=["turnover input"],
    )
    cogs = add(
        "cost_of_sales",
        "Cost of sales",
        "FIN-003",
        "turnover x (1 - gross margin)",
        {"turnover": revenue, "gross_margin": a["gross_margin"]},
        mul(revenue, ONE - a["gross_margin"]),
        "gross_margin",
    )
    gross_profit = add(
        "gross_profit",
        "Gross profit",
        INCOME_RULE,
        "revenue - cost of sales",
        {"revenue": revenue, "cost_of_sales": cogs},
        revenue - cogs,
    )
    opex = driver(
        "operating_expenses",
        "Operating expenses",
        "opex_ratio",
        a["opex_ratio"],
        "turnover x operating expense ratio",
    )
    ebitda = add(
        "ebitda",
        "EBITDA",
        INCOME_RULE,
        "gross profit - operating expenses",
        {"gross_profit": gross_profit, "operating_expenses": opex},
        gross_profit - opex,
    )
    depreciation = driver(
        "depreciation",
        "Depreciation and amortisation",
        "depreciation_ratio",
        a["depreciation_ratio"],
        "turnover x depreciation ratio",
    )
    ebit = add(
        "ebit",
        "Operating profit (EBIT)",
        INCOME_RULE,
        "EBITDA - depreciation",
        {"ebitda": ebitda, "depreciation": depreciation},
        ebitda - depreciation,
    )
    debt = driver("debt", "Total borrowings", "debt_ratio", a["debt_ratio"],
                  "turnover x debt ratio")
    short_debt = add(
        "short_term_debt",
        "Short-term borrowings",
        "FIN-003",
        "total debt x short-term share",
        {"debt": debt, "short_term_debt_share": a["short_term_debt_share"]},
        mul(debt, a["short_term_debt_share"]),
        "short_term_debt_share",
    )
    add(
        "long_term_debt",
        "Long-term borrowings",
        BALANCE_RULE,
        "total debt - short-term borrowings",
        {"debt": debt, "short_term_debt": short_debt},
        debt - short_debt,
    )
    interest = add(
        "interest",
        "Finance cost",
        "FIN-003",
        "total debt x interest rate",
        {"debt": debt, "interest_rate": a["interest_rate"]},
        mul(debt, a["interest_rate"]),
        "interest_rate",
    )
    pbt = add(
        "pbt",
        "Profit before tax",
        INCOME_RULE,
        "operating profit - finance cost",
        {"ebit": ebit, "interest": interest},
        ebit - interest,
    )
    taxable = pbt if pbt > ZERO else ZERO
    tax = add(
        "tax",
        "Tax expense",
        "FIN-003",
        "positive profit before tax x tax rate",
        {"taxable_profit": taxable, "tax_rate": a["tax_rate"]},
        mul(taxable, a["tax_rate"]),
        "tax_rate",
    )
    net_income = add(
        "net_income",
        "Net profit",
        INCOME_RULE,
        "profit before tax - tax",
        {"pbt": pbt, "tax": tax},
        pbt - tax,
    )

    fixed_assets = driver(
        "fixed_assets",
        "Property, plant and equipment",
        "fixed_asset_ratio",
        a["fixed_asset_ratio"],
        "turnover x fixed asset ratio",
    )
    inventory = add(
        "inventory",
        "Inventories",
        "FIN-003",
        "cost of sales x inventory days / 365",
        {"cost_of_sales": cogs, "inventory_days": a["inventory_days"]},
        div(mul(cogs, a["inventory_days"]), Decimal("365")),
        "inventory_days",
    )
    receivables = add(
        "receivables",
        "Trade receivables",
        "FIN-003",
        "turnover x receivable days / 365",
        {"turnover": revenue, "receivable_days": a["receivable_days"]},
        div(mul(revenue, a["receivable_days"]), Decimal("365")),
        "receivable_days",
    )
    payables = add(
        "payables",
        "Trade payables",
        "FIN-003",
        "cost of sales x payable days / 365",
        {"cost_of_sales": cogs, "payable_days": a["payable_days"]},
        div(mul(cogs, a["payable_days"]), Decimal("365")),
        "payable_days",
    )
    other_cl = ZERO
    if a["other_current_liabilities_ratio"] > ZERO:
        other_cl = driver(
            "other_current_liabilities",
            "Other current liabilities",
            "other_current_liabilities_ratio",
            a["other_current_liabilities_ratio"],
            "turnover x other current liability ratio",
        )
    other_ncl = ZERO
    if a["other_noncurrent_liabilities_ratio"] > ZERO:
        other_ncl = driver(
            "other_noncurrent_liabilities",
            "Other non-current liabilities",
            "other_noncurrent_liabilities_ratio",
            a["other_noncurrent_liabilities_ratio"],
            "turnover x other non-current liability ratio",
        )
    share_capital = driver(
        "share_capital",
        "Share capital",
        "share_capital_ratio",
        a["share_capital_ratio"],
        "turnover x share capital ratio",
    )
    dividends = add(
        "dividends",
        "Dividends paid",
        "FIN-003",
        "net profit x dividend payout",
        {"net_income": net_income, "dividend_payout": a["dividend_payout"]},
        mul(net_income, a["dividend_payout"]),
        "dividend_payout",
    )
    reserves = add(
        "reserves",
        "Reserves and surplus",
        INCOME_RULE,
        "net profit - dividends",
        {"net_income": net_income, "dividends": dividends},
        net_income - dividends,
    )
    equity = add(
        "equity",
        "Total equity",
        BALANCE_RULE,
        "share capital + reserves",
        {"share_capital": share_capital, "reserves": reserves},
        share_capital + reserves,
    )
    total_cl = add(
        "total_current_liabilities",
        "Total current liabilities",
        BALANCE_RULE,
        "payables + short-term borrowings + other current liabilities",
        {"payables": payables, "short_term_debt": short_debt,
         "other_current_liabilities": other_cl},
        payables + short_debt + other_cl,
    )
    total_liabilities = add(
        "total_liabilities",
        "Total liabilities",
        BALANCE_RULE,
        "total current liabilities + long-term borrowings + other non-current liabilities",
        {"total_current_liabilities": total_cl, "long_term_debt": debt - short_debt,
         "other_noncurrent_liabilities": other_ncl},
        total_cl + (debt - short_debt) + other_ncl,
    )
    cash = add(
        "cash",
        "Cash and cash equivalents",
        BALANCE_RULE,
        "total liabilities + equity - non-cash assets; cash is the balancing item",
        {"total_liabilities": total_liabilities, "equity": equity,
         "fixed_assets": fixed_assets, "inventory": inventory, "receivables": receivables},
        total_liabilities + equity - fixed_assets - inventory - receivables,
    )
    if cash < ZERO:
        warnings.append(
            Warning(
                "FIN_NEGATIVE_CASH",
                "The modelled balance sheet needs negative cash; the assumptions are too asset "
                "heavy for the funding assumed.",
                "warning",
                "Raise share capital or debt, shorten receivable or inventory days, or lower the "
                "fixed asset ratio.",
            )
        )
    total_ca = add(
        "total_current_assets",
        "Total current assets",
        BALANCE_RULE,
        "inventory + receivables + cash",
        {"inventory": inventory, "receivables": receivables, "cash": cash},
        inventory + receivables + cash,
    )
    add(
        "total_assets",
        "Total assets",
        BALANCE_RULE,
        "fixed assets + total current assets",
        {"fixed_assets": fixed_assets, "total_current_assets": total_ca},
        fixed_assets + total_ca,
    )
    add(
        "total_liabilities_and_equity",
        "Total liabilities and equity",
        BALANCE_RULE,
        "total liabilities + total equity",
        {"total_liabilities": total_liabilities, "equity": equity},
        total_liabilities + equity,
    )

    nwc = add(
        "cash_nwc",
        "Net working capital",
        "FIN-006",
        "inventory + receivables - payables - other current liabilities",
        {"inventory": inventory, "receivables": receivables, "payables": payables,
         "other_current_liabilities": other_cl},
        inventory + receivables - payables - other_cl,
    )
    increase_nwc = nwc - a["opening_working_capital"]
    capex = add(
        "capex",
        "Capital expenditure",
        "FIN-003",
        "net fixed assets x capital expenditure ratio",
        {"fixed_assets": fixed_assets, "capex_ratio": a["capex_ratio"]},
        mul(fixed_assets, a["capex_ratio"]),
        "capex_ratio",
    )
    cfo = add(
        "cfo",
        "Net cash from operating activities",
        CASH_RULE,
        "net profit + depreciation - increase in net working capital",
        {"net_income": net_income, "depreciation": depreciation,
         "increase_in_net_working_capital": increase_nwc},
        net_income + depreciation - increase_nwc,
    )
    cfi = add(
        "cfi",
        "Net cash from investing activities",
        CASH_RULE,
        "- capital expenditure",
        {"capex": capex},
        -capex,
    )
    cff = add(
        "cff",
        "Net cash from financing activities",
        CASH_RULE,
        "net borrowings - dividends; borrowings are unchanged in the first model year",
        {"dividends": dividends},
        -dividends,
    )
    net_change = add(
        "net_change_cash",
        "Net change in cash",
        CASH_RULE,
        "operating + investing + financing cash flows",
        {"cfo": cfo, "cfi": cfi, "cff": cff},
        cfo + cfi + cff,
    )
    add(
        "closing_cash",
        "Cash at the end",
        CASH_RULE,
        "cash on the modelled balance sheet",
        {"cash": cash},
        cash,
    )
    opening_cash = cash - net_change
    add(
        "opening_cash",
        "Cash at the beginning",
        CASH_RULE,
        "closing cash - net change in cash",
        {"closing_cash": cash, "net_change_cash": net_change},
        opening_cash,
    )

    rows = _emit_rows(book, values, {}, meta, {})
    balance = _balance_check(book, values, warnings)
    return book, {
        "mode": "model",
        "attachment_id": None,
        "attachment_name": None,
        "currency": currency,
        "period": period,
        "prior_period": None,
        "statement_periods": [period] if period else [],
        "scales": {},
        "unmatched": [],
        "statements": rows,
        "balance_check": balance,
        "values": values,
        "prior": {"revenue": prior_revenue} if prior_revenue is not None else {},
    }


def _ratio(
    book: _Book,
    rows: list[dict[str, Any]],
    *,
    key: str,
    label: str,
    group: str,
    rule: str,
    unit: str,
    formula: str,
    num: Decimal | None,
    den: Decimal | None,
    num_label: str,
    den_label: str,
    prior_num: Decimal | None = None,
    prior_den: Decimal | None = None,
    multiplier: Decimal = ONE,
    note: str | None = None,
    fact: bool = False,
) -> tuple[Decimal | None, Decimal | None]:
    if num is None or den is None or den == ZERO:
        return None, None
    value = div(mul(num, multiplier), den)
    if unit == "%":
        value = mul(value, HUNDRED)
    step = book.add(
        step_id=f"ratio_{key}",
        rule_id=rule,
        label=label,
        formula=formula,
        inputs={num_label: num, den_label: den},
        value=value,
        unit=unit,
    )
    if fact:
        book.fact(f"f_{key}", step, label)
    prior_value = None
    if prior_num is not None and prior_den is not None and prior_den != ZERO:
        prior_value = div(mul(prior_num, multiplier), prior_den)
        if unit == "%":
            prior_value = mul(prior_value, HUNDRED)
        book.add(
            step_id=f"ratio_{key}_prior",
            rule_id=rule,
            label=f"{label} (prior)",
            formula=formula,
            inputs={num_label: prior_num, den_label: prior_den},
            value=prior_value,
            unit=unit,
        )
    rows.append({
        "key": key,
        "group": group,
        "name": label,
        "value": trace_str(value),
        "display": display_amount(value, unit),
        "unit": unit,
        "prior": trace_str(prior_value) if prior_value is not None else None,
        "prior_display": (
            display_amount(prior_value, unit) if prior_value is not None else None
        ),
        "rule": rule,
        "formula": formula,
        "note": note,
    })
    return value, prior_value


def _growth(
    book: _Book,
    rows: list[dict[str, Any]],
    *,
    key: str,
    label: str,
    current: Decimal | None,
    base: Decimal | None,
    note: str | None = None,
    fact: bool = False,
) -> Decimal | None:
    if current is None or base is None or base == ZERO:
        return None
    value = mul(div(current - base, base), HUNDRED)
    step = book.add(
        step_id=f"ratio_{key}",
        rule_id="FIN-012",
        label=label,
        formula="(current period - prior period) / prior period x 100",
        inputs={"current": current, "prior": base},
        value=value,
        unit="%",
    )
    if fact:
        book.fact(f"f_{key}", step, label)
    rows.append({
        "key": key,
        "group": "Growth",
        "name": label,
        "value": trace_str(value),
        "display": display_amount(value, "%"),
        "unit": "%",
        "prior": None,
        "prior_display": None,
        "rule": "FIN-012",
        "formula": "change against the prior period",
        "note": note,
    })
    return value


def _ratio_rows(
    book: _Book, values: dict[str, Decimal], prior: dict[str, Decimal]
) -> tuple[list[dict[str, Any]], dict[str, Decimal | None]]:
    rows: list[dict[str, Any]] = []
    seen: dict[str, Decimal | None] = {}
    revenue = values.get("revenue")
    gross_profit = values.get("gross_profit")
    ebitda = values.get("ebitda")
    ebit = values.get("ebit")
    net_income = values.get("net_income")
    equity = values.get("equity")
    assets = values.get("total_assets")
    current_assets = values.get("total_current_assets")
    current_liabilities = values.get("total_current_liabilities")
    inventory = values.get("inventory")
    receivables = values.get("receivables")
    payables = values.get("payables")
    cash = values.get("cash")
    cost_of_sales = values.get("cost_of_sales")
    interest = values.get("interest")
    debt = _total_debt(values)
    prior_debt = _total_debt(prior)
    capital_employed = equity + debt if equity is not None and debt is not None else equity
    prior_capital = (
        prior.get("equity") + prior_debt
        if prior.get("equity") is not None and prior_debt is not None
        else prior.get("equity")
    )

    def run(key: str, label: str, group: str, rule: str, unit: str, formula: str,
            num: Decimal | None, den: Decimal | None, num_label: str, den_label: str,
            *, prior_num: Decimal | None = None, prior_den: Decimal | None = None,
            multiplier: Decimal = ONE, note: str | None = None,
            fact: bool = False) -> Decimal | None:
        value, _ = _ratio(
            book, rows, key=key, label=label, group=group, rule=rule, unit=unit,
            formula=formula, num=num, den=den, num_label=num_label, den_label=den_label,
            prior_num=prior_num, prior_den=prior_den, multiplier=multiplier,
            note=note, fact=fact,
        )
        seen[key] = value
        return value

    run("gross_margin", "Gross margin", "Profitability", "FIN-008", "%",
        "gross profit / revenue x 100", gross_profit, revenue, "gross_profit", "revenue",
        prior_num=prior.get("gross_profit"), prior_den=prior.get("revenue"),
        note="Share of turnover left after cost of sales", fact=True)
    run("operating_margin", "Operating margin", "Profitability", "FIN-008", "%",
        "operating profit / revenue x 100", ebit, revenue, "ebit", "revenue",
        prior_num=prior.get("ebit"), prior_den=prior.get("revenue"),
        note="Operating profit as a share of turnover", fact=True)
    run("ebitda_margin", "EBITDA margin", "Profitability", "FIN-008", "%",
        "EBITDA / revenue x 100", ebitda, revenue, "ebitda", "revenue",
        prior_num=prior.get("ebitda"), prior_den=prior.get("revenue"))
    run("net_margin", "Net margin", "Profitability", "FIN-008", "%",
        "net profit / revenue x 100", net_income, revenue, "net_income", "revenue",
        prior_num=prior.get("net_income"), prior_den=prior.get("revenue"),
        note="Net profit as a share of turnover", fact=True)
    run("return_on_equity", "Return on equity", "Profitability", "FIN-008", "%",
        "net profit / total equity x 100", net_income, equity, "net_income", "equity",
        prior_num=prior.get("net_income"), prior_den=prior.get("equity"))
    run("return_on_capital_employed", "Return on capital employed", "Profitability", "FIN-008",
        "%", "operating profit / (debt + equity) x 100", ebit, capital_employed, "ebit",
        "capital_employed", prior_num=prior.get("ebit"), prior_den=prior_capital,
        note="Operating profit over debt plus equity")
    run("return_on_assets", "Return on assets", "Profitability", "FIN-008", "%",
        "net profit / total assets x 100", net_income, assets, "net_income", "total_assets",
        prior_num=prior.get("net_income"), prior_den=prior.get("total_assets"))

    run("current_ratio", "Current ratio", "Liquidity", "FIN-009", "times",
        "current assets / current liabilities", current_assets, current_liabilities,
        "total_current_assets", "total_current_liabilities",
        prior_num=prior.get("total_current_assets"),
        prior_den=prior.get("total_current_liabilities"),
        note="Current assets cover of current liabilities", fact=True)
    if current_assets is not None and inventory is not None:
        run("quick_ratio", "Quick ratio", "Liquidity", "FIN-009", "times",
            "(current assets - inventory) / current liabilities",
            current_assets - inventory, current_liabilities,
            "current_assets_less_inventory", "total_current_liabilities",
            prior_num=(
                prior["total_current_assets"] - prior["inventory"]
                if "total_current_assets" in prior and "inventory" in prior
                else None
            ),
            prior_den=prior.get("total_current_liabilities"),
            note="Liquidity without inventory")
    run("cash_ratio", "Cash ratio", "Liquidity", "FIN-009", "times",
        "cash / current liabilities", cash, current_liabilities, "cash",
        "total_current_liabilities", prior_num=prior.get("cash"),
        prior_den=prior.get("total_current_liabilities"))
    if current_assets is not None and current_liabilities is not None:
        working_capital = current_assets - current_liabilities
        step = book.add(
            step_id="working_capital",
            rule_id="FIN-006",
            label="Working capital",
            formula="total current assets - total current liabilities",
            inputs={"total_current_assets": current_assets,
                    "total_current_liabilities": current_liabilities},
            value=working_capital,
            unit=book.currency,
        )
        book.fact("f_working_capital", step, "Working capital")
        prior_value = None
        if "total_current_assets" in prior and "total_current_liabilities" in prior:
            prior_value = prior["total_current_assets"] - prior["total_current_liabilities"]
            book.add(
                step_id="working_capital_prior",
                rule_id="FIN-006",
                label="Working capital (prior)",
                formula="total current assets - total current liabilities",
                inputs={"total_current_assets": prior["total_current_assets"],
                        "total_current_liabilities": prior["total_current_liabilities"]},
                value=prior_value,
                unit=book.currency,
            )
        rows.append({
            "key": "working_capital",
            "group": "Liquidity",
            "name": "Working capital",
            "value": trace_str(working_capital),
            "display": display_amount(working_capital, book.currency),
            "unit": book.currency,
            "prior": trace_str(prior_value) if prior_value is not None else None,
            "prior_display": (
                display_amount(prior_value, book.currency) if prior_value is not None else None
            ),
            "rule": "FIN-006",
            "formula": "current assets less current liabilities",
            "note": "Short-term funds tied up in operations",
        })
        seen["working_capital"] = working_capital

    run("debt_equity", "Debt-to-equity", "Leverage", "FIN-010", "times",
        "total debt / total equity", debt, equity, "total_debt", "equity",
        prior_num=prior_debt, prior_den=prior.get("equity"),
        note="Borrowings as a multiple of equity", fact=True)
    if debt is not None and cash is not None:
        run("net_debt_ebitda", "Net debt to EBITDA", "Leverage", "FIN-010", "times",
            "(total debt - cash) / EBITDA", debt - cash, ebitda, "net_debt", "ebitda",
            prior_num=(
                prior_debt - prior["cash"]
                if prior_debt is not None and "cash" in prior
                else None
            ),
            prior_den=prior.get("ebitda"))
    run("interest_cover", "Interest cover", "Leverage", "FIN-010", "times",
        "operating profit / finance cost", ebit, interest, "ebit", "interest",
        prior_num=prior.get("ebit"), prior_den=prior.get("interest"),
        note="Operating profit cover of finance cost", fact=True)
    run("debt_ratio", "Debt ratio", "Leverage", "FIN-010", "%",
        "total debt / total assets x 100", debt, assets, "total_debt", "total_assets",
        prior_num=prior_debt, prior_den=prior.get("total_assets"))

    inventory_days, prior_inventory_days = _ratio(
        book, rows, key="inventory_days", label="Inventory days", group="Efficiency",
        rule="FIN-011", unit="days", formula="inventory / cost of sales x 365",
        num=inventory, den=cost_of_sales, num_label="inventory", den_label="cost_of_sales",
        prior_num=prior.get("inventory"), prior_den=prior.get("cost_of_sales"),
        multiplier=Decimal("365"), note="Days of cost of sales held as inventory",
    )
    seen["inventory_days"] = inventory_days
    receivable_days, prior_receivable_days = _ratio(
        book, rows, key="receivable_days", label="Receivable days", group="Efficiency",
        rule="FIN-011", unit="days", formula="trade receivables / revenue x 365",
        num=receivables, den=revenue, num_label="receivables", den_label="revenue",
        prior_num=prior.get("receivables"), prior_den=prior.get("revenue"),
        multiplier=Decimal("365"), note="Days of turnover held as receivables",
    )
    seen["receivable_days"] = receivable_days
    payable_days, prior_payable_days = _ratio(
        book, rows, key="payable_days", label="Payable days", group="Efficiency",
        rule="FIN-011", unit="days", formula="trade payables / cost of sales x 365",
        num=payables, den=cost_of_sales, num_label="payables", den_label="cost_of_sales",
        prior_num=prior.get("payables"), prior_den=prior.get("cost_of_sales"),
        multiplier=Decimal("365"), note="Days of cost of sales funded by suppliers",
    )
    seen["payable_days"] = payable_days
    if None not in (inventory_days, receivable_days, payable_days):
        cycle = inventory_days + receivable_days - payable_days
        step = book.add(
            step_id="ratio_cash_conversion_cycle",
            rule_id="FIN-011",
            label="Cash conversion cycle",
            formula="inventory days + receivable days - payable days",
            inputs={"inventory_days": inventory_days,
                    "receivable_days": receivable_days,
                    "payable_days": payable_days},
            value=cycle,
            unit="days",
        )
        book.fact("f_ccc", step, "Cash conversion cycle")
        prior_cycle = None
        if None not in (prior_inventory_days, prior_receivable_days, prior_payable_days):
            prior_cycle = (
                prior_inventory_days + prior_receivable_days - prior_payable_days
            )
            book.add(
                step_id="ratio_cash_conversion_cycle_prior",
                rule_id="FIN-011",
                label="Cash conversion cycle (prior)",
                formula="inventory days + receivable days - payable days",
                inputs={"inventory_days": prior_inventory_days,
                        "receivable_days": prior_receivable_days,
                        "payable_days": prior_payable_days},
                value=prior_cycle,
                unit="days",
            )
        rows.append({
            "key": "cash_conversion_cycle",
            "group": "Efficiency",
            "name": "Cash conversion cycle",
            "value": trace_str(cycle),
            "display": display_amount(cycle, "days"),
            "unit": "days",
            "prior": trace_str(prior_cycle) if prior_cycle is not None else None,
            "prior_display": (
                display_amount(prior_cycle, "days") if prior_cycle is not None else None
            ),
            "rule": "FIN-011",
            "formula": "inventory days + receivable days - payable days",
            "note": "Days between paying suppliers and collecting customers",
        })
        seen["cash_conversion_cycle"] = cycle
    run("asset_turnover", "Asset turnover", "Efficiency", "FIN-011", "times",
        "revenue / total assets", revenue, assets, "revenue", "total_assets",
        prior_num=prior.get("revenue"), prior_den=prior.get("total_assets"),
        note="Turnover generated per unit of assets", fact=True)

    seen["revenue_growth"] = _growth(
        book, rows, key="revenue_growth", label="Revenue growth",
        current=revenue, base=prior.get("revenue"), fact=True,
        note="Change in turnover against the prior period",
    )
    _growth(book, rows, key="gross_profit_growth", label="Gross profit growth",
            current=gross_profit, base=prior.get("gross_profit"))
    _growth(book, rows, key="net_income_growth", label="Net profit growth",
            current=net_income, base=prior.get("net_income"))
    _growth(book, rows, key="total_assets_growth", label="Total assets growth",
            current=assets, base=prior.get("total_assets"))
    _growth(book, rows, key="equity_growth", label="Equity growth",
            current=equity, base=prior.get("equity"))
    return rows, seen


def _template(fact_keys: set[str]) -> str:
    def has(*keys: str) -> bool:
        return all(key in fact_keys for key in keys)

    sentences: list[str] = []
    if has("f_revenue", "f_net_income", "f_net_margin"):
        sentences.append(
            "The statements show revenue of {{f_revenue}} and net profit of "
            "{{f_net_income}}, a net margin of {{f_net_margin}}."
        )
    elif has("f_revenue", "f_net_income"):
        sentences.append(
            "The statements show revenue of {{f_revenue}} and net profit of "
            "{{f_net_income}}."
        )
    elif has("f_revenue"):
        sentences.append("Revenue is {{f_revenue}}.")
    elif has("f_total_assets", "f_equity"):
        sentences.append(
            "The balance sheet carries total assets of {{f_total_assets}} and total "
            "equity of {{f_equity}}."
        )
    elif "f_total_assets" in fact_keys:
        sentences.append("The balance sheet carries total assets of {{f_total_assets}}.")
    if "f_balance_diff" in fact_keys:
        sentences.append(
            "The balance check leaves a difference of {{f_balance_diff}} between assets "
            "and liabilities plus equity."
        )
    if "f_current_ratio" in fact_keys:
        sentences.append("The current ratio is {{f_current_ratio}}.")
    if "f_debt_equity" in fact_keys:
        sentences.append("Debt-to-equity is {{f_debt_equity}}.")
    if "f_interest_cover" in fact_keys:
        sentences.append("Interest cover is {{f_interest_cover}}.")
    if "f_ccc" in fact_keys:
        sentences.append("The cash conversion cycle is {{f_ccc}}.")
    if "f_asset_turnover" in fact_keys:
        sentences.append("Asset turnover is {{f_asset_turnover}}.")
    if "f_cfo" in fact_keys:
        sentences.append("Operating activities generated {{f_cfo}} of cash.")
    if "f_revenue_growth" in fact_keys:
        sentences.append("Revenue changed by {{f_revenue_growth}} against the prior period.")
    if not sentences:
        sentences.append("The statement lines are shown below.")
    return " ".join(sentences)


def _observations(
    values: dict[str, Decimal],
    ratios: dict[str, Decimal | None],
    *,
    mode: str,
    currency: str,
    balance: dict[str, Any] | None,
) -> list[str]:
    notes: list[str] = []
    net_margin = ratios.get("net_margin")
    if net_margin is not None:
        notes.append(f"Net margin is {display_amount(net_margin, '%')}.")
        if net_margin < ZERO:
            notes.append("The period ended in a loss.")
        elif net_margin >= Decimal("10"):
            notes.append("Net margin is in double digits.")
    gross_margin = ratios.get("gross_margin")
    if gross_margin is not None and net_margin is not None and gross_margin > ZERO:
        absorbed = gross_margin - net_margin
        notes.append(
            "Costs outside cost of sales absorb "
            f"{display_amount(absorbed, '%')} of turnover."
        )
    current_ratio = ratios.get("current_ratio")
    if current_ratio is not None:
        if current_ratio < ONE:
            notes.append(
                "Current liabilities exceed current assets, so short-term funding is tight."
            )
        elif current_ratio >= Decimal("2"):
            notes.append(
                "Liquidity is comfortable: current assets are at least twice current liabilities."
            )
    debt_equity = ratios.get("debt_equity")
    if debt_equity is not None and debt_equity > Decimal("1.5"):
        notes.append("Gearing is above one and a half times equity.")
    interest_cover = ratios.get("interest_cover")
    if interest_cover is not None and interest_cover < Decimal("2"):
        notes.append("Operating profit covers finance cost less than twice.")
    cycle = ratios.get("cash_conversion_cycle")
    if cycle is not None and cycle > Decimal("90"):
        notes.append(
            "The cash conversion cycle exceeds a quarter, so cash is tied up in working capital."
        )
    cfo = values.get("cfo")
    if cfo is not None and cfo < ZERO:
        notes.append("Operating activities consumed cash in the period.")
    revenue_growth = ratios.get("revenue_growth")
    if revenue_growth is not None:
        notes.append(
            f"Revenue changed by {display_amount(revenue_growth, '%')} against the prior period."
        )
    if balance is not None:
        if balance["ok"]:
            notes.append("The balance sheet checks within materiality.")
        else:
            notes.append(
                f"The balance sheet is out by {display_amount(Decimal(balance['difference']), currency)}, "
                "above materiality."
            )
    if mode == "model":
        notes.append(
            "The statements are modelled from turnover and assumptions; cash is the balancing item."
        )
    return notes[:10]


def _verdict(
    mode: str,
    values: dict[str, Decimal],
    *,
    currency: str,
    period: str | None,
    attachment_name: str | None,
    balance: dict[str, Any] | None,
) -> str:
    revenue = values.get("revenue")
    net_income = values.get("net_income")
    assets = values.get("total_assets")
    cash = values.get("cash")
    facts: list[str] = []
    if revenue is not None:
        facts.append(f"revenue {display_amount(revenue, currency)}")
    if net_income is not None:
        facts.append(f"net profit {display_amount(net_income, currency)}")
    if assets is not None:
        facts.append(f"total assets {display_amount(assets, currency)}")
    if cash is not None:
        facts.append(f"cash {display_amount(cash, currency)}")
    if mode == "model":
        lead = f"Modelled statements for {period or 'the period'}"
        text = lead + (" from turnover: " + ", ".join(facts) + "." if facts else ".")
        text += " The balance sheet balances by construction."
        return text
    lead = f"Read {attachment_name or 'the attachment'}"
    text = lead + (" for " + (period or "the reported period") if period else "")
    text += (": " + ", ".join(facts) + "." if facts else ".")
    if balance is not None:
        if balance["ok"]:
            text += " The balance sheet checks within materiality."
        else:
            text += (
                f" The balance sheet is out by {display_amount(Decimal(balance['difference']), currency)}."
            )
    return text


def run_financial_statements(
    attachment: dict[str, Any] | None = None,
    *,
    turnover: Any | None = None,
    prior_turnover: Any | None = None,
    currency: str = "INR",
    period: str | None = None,
    mode: str = "auto",
    assumptions: dict[str, Any] | None = None,
    assumption_sources: dict[str, str] | None = None,
) -> CalcResult:
    """Build the statements and ratios, or read them from an attachment.

    ``mode`` is ``reported``, ``model`` or ``auto``. In ``auto`` an attachment
    that holds recognisable statements wins; otherwise turnover builds the
    statements.
    """
    currency = str(currency or "INR").upper()
    if currency not in CURRENCIES:
        raise DomainError(
            "E_STATEMENT_CURRENCY",
            f"{currency} is not a supported currency.",
            "Use INR, USD, EUR or GBP.",
        )
    mode = str(mode or "auto").strip().lower()
    if mode not in {"auto", "reported", "model"}:
        raise DomainError(
            "E_STATEMENT_MODE",
            f"{mode!r} is not a statement mode.",
            "Use auto, reported or model.",
        )

    revenue: Decimal | None = None
    if turnover is not None:
        try:
            revenue = dec(turnover, "turnover")
        except MoneyError as exc:
            raise DomainError(
                "E_STATEMENT_TURNOVER",
                f"The turnover is not a valid number: {exc}.",
                "State a plain number, for example 12000000 or 1.2 crore.",
            ) from exc
        if revenue <= ZERO:
            raise DomainError(
                "E_STATEMENT_TURNOVER",
                "Turnover must be greater than zero.",
                "State the annual turnover as a positive amount.",
            )
    prior_revenue: Decimal | None = None
    if prior_turnover is not None:
        try:
            prior_revenue = dec(prior_turnover, "prior turnover")
        except MoneyError as exc:
            raise DomainError(
                "E_STATEMENT_TURNOVER",
                f"The prior turnover is not a valid number: {exc}.",
                "State the prior annual turnover as a plain number.",
            ) from exc
        if prior_revenue <= ZERO:
            raise DomainError(
                "E_STATEMENT_TURNOVER",
                "Prior turnover must be greater than zero.",
                "State a positive prior annual turnover.",
            )

    warnings: list[Warning] = []
    sources = assumption_sources or {}
    attachment_name = _attachment_name(attachment) if attachment else None
    if mode == "auto":
        if attachment is not None:
            try:
                parse_statements(attachment, period=period)
            except StatementError as exc:
                if exc.code == "E_STATEMENT_PERIOD":
                    raise DomainError(exc.code, exc.message, exc.remediation) from exc
                if revenue is None:
                    raise DomainError(
                        "E_STATEMENT_SOURCE",
                        "The attachment does not contain recognisable financial statements and no "
                        "turnover was stated.",
                        "Rename the statement rows to standard labels, or state the annual turnover "
                        "so the statements can be modelled.",
                    ) from exc
                mode = "model"
            else:
                mode = "reported"
        elif revenue is not None:
            mode = "model"
        else:
            raise DomainError(
                "E_STATEMENT_SOURCE",
                "Financial statements need either an attachment or an annual turnover.",
                "Attach a statement file or state the annual turnover, for example "
                "'prepare statements on turnover of 12 crore'.",
            )

    if mode == "reported":
        if revenue is not None:
            warnings.append(
                Warning(
                    "FIN_TURNOVER_IGNORED",
                    "Turnover was stated, but the reported statements in the attachment were used.",
                    "info",
                    "Remove the attachment or ask for modelled statements to use the turnover.",
                )
            )
        book, payload = _reported(
            attachment,
            period=period,
            currency=currency,
            source_refs=[attachment_name or "attachment", "reported statements"],
            warnings=warnings,
        )
        assumptions_used: list[Assumption] = []
    else:
        if revenue is None:
            raise DomainError(
                "E_STATEMENT_TURNOVER",
                "Modelled statements need an annual turnover.",
                "State the turnover, for example 'turnover 12 crore'.",
            )
        model_inputs, assumptions_used = _model_values(
            revenue, prior_revenue, assumptions or {}, sources, currency
        )
        book, payload = _modelled(
            revenue,
            prior_revenue,
            model_inputs,
            period=period,
            currency=currency,
            source_refs=["turnover input", "modelled statements"],
            warnings=warnings,
        )

    values = payload.pop("values")
    prior = payload.pop("prior")
    ratio_rows, ratio_values = _ratio_rows(book, values, prior)
    fact_keys = set(book.facts)
    template = _template(fact_keys)
    observations = _observations(
        values, ratio_values, mode=mode, currency=currency, balance=payload.get("balance_check")
    )

    net_income = values.get("net_income")
    revenue_value = values.get("revenue")
    total_assets = values.get("total_assets")
    if net_income is not None:
        headline, favorable = net_income, net_income > ZERO
    elif revenue_value is not None:
        headline, favorable = revenue_value, None
    elif total_assets is not None:
        headline, favorable = total_assets, None
    else:
        headline, favorable = ZERO, None

    identities: list[tuple[Decimal, Decimal]] = []
    if mode == "model":
        assets_total = values.get("total_assets")
        claims_total = values.get("total_liabilities_and_equity")
        if assets_total is not None and claims_total is not None:
            identities.append((assets_total, claims_total))
        opening = values.get("opening_cash")
        closing = values.get("closing_cash")
        change = values.get("net_change_cash")
        if opening is not None and closing is not None and change is not None:
            identities.append((closing, opening + change))

    result = CalcResult(
        value=headline,
        unit=currency,
        favorable=favorable,
        verdict=_verdict(
            mode,
            values,
            currency=currency,
            period=payload.get("period"),
            attachment_name=attachment_name,
            balance=payload.get("balance_check"),
        ),
        steps=book.builder.steps,
        facts=list(book.facts.values()),
        assumptions=assumptions_used,
        warnings=warnings,
        rationale_template=template,
        meta={
            **payload,
            "statements": payload.get("statements"),
            "ratios": ratio_rows,
            "observations": observations,
            "revenue": trace_str(revenue_value) if revenue_value is not None else None,
            "net_income": trace_str(net_income) if net_income is not None else None,
            "total_assets": trace_str(total_assets) if total_assets is not None else None,
            "cash": trace_str(values.get("cash")) if "cash" in values else None,
            "currency": currency,
        },
    )
    result.rationale = render(template, result)
    report = validate_result(result, identities=identities)
    if not report.ok:
        raise DomainError("E_STATEMENT_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result
