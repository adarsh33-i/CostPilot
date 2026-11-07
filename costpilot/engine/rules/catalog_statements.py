"""Rulebook for financial statements and ratio analysis.

Every statement line and every ratio cites one of these rules. Reported lines
are read from the attachment; derived subtotals, the turnover model and all
ratios are calculated with Decimal. The rules never invent a missing figure.
"""

from __future__ import annotations

from costpilot.engine.rules.registry import Rule, register_many

V = "1.0.0"


def _r(rule_id: str, title: str, formula: str, citation: str) -> Rule:
    return Rule(rule_id, V, title, "statements", formula, citation)


RULES: tuple[Rule, ...] = (
    _r(
        "FIN-001",
        "Reported statement line",
        "read the amount printed on the attachment row and apply the stated scale once",
        "Financial statement policy 2.1",
    ),
    _r(
        "FIN-002",
        "Income statement subtotal",
        "revenue - cost of sales; gross profit - operating expenses; EBITDA - depreciation; EBIT - finance cost; profit before tax - tax",
        "Financial statement policy 2.2",
    ),
    _r(
        "FIN-003",
        "Turnover model driver",
        "driver x stated ratio, or driver x days / 365",
        "Financial statement policy 2.3",
    ),
    _r(
        "FIN-004",
        "Balance sheet total",
        "sum the asset lines and the liability and equity lines; assets must equal liabilities plus equity",
        "Financial statement policy 2.4",
    ),
    _r(
        "FIN-005",
        "Balance check",
        "total assets - (total liabilities + total equity)",
        "Financial statement policy 2.4",
    ),
    _r(
        "FIN-006",
        "Working capital",
        "current assets - current liabilities; net working capital = inventory + receivables + other current assets - payables - other current liabilities",
        "Financial statement policy 2.5",
    ),
    _r(
        "FIN-007",
        "Indirect cash flow",
        "net profit + depreciation - increase in net working capital - capital expenditure + net borrowings - dividends",
        "Financial statement policy 2.6",
    ),
    _r(
        "FIN-008",
        "Profitability ratio",
        "margin = result / revenue x 100; return = result / capital employed x 100",
        "Financial statement policy 2.7",
    ),
    _r(
        "FIN-009",
        "Liquidity ratio",
        "current assets / current liabilities; (current assets - inventory) / current liabilities; cash / current liabilities",
        "Financial statement policy 2.7",
    ),
    _r(
        "FIN-010",
        "Leverage ratio",
        "total debt / equity; (total debt - cash) / EBITDA; EBIT / finance cost",
        "Financial statement policy 2.7",
    ),
    _r(
        "FIN-011",
        "Efficiency ratio",
        "balance / flow x 365; cash conversion cycle = inventory days + receivable days - payable days; revenue / total assets",
        "Financial statement policy 2.7",
    ),
    _r(
        "FIN-012",
        "Growth ratio",
        "(current period - prior period) / prior period x 100",
        "Financial statement policy 2.8",
    ),
    _r(
        "FIN-013",
        "Stated turnover",
        "use the turnover stated by the user once, in the stated currency",
        "Financial statement policy 2.3",
    ),
)

register_many(RULES)
