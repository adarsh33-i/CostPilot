"""Read reported financial statements out of an attachment.

The extractor maps printed row labels to canonical statement lines, chooses a
current and a prior period column, and normalises lakh, crore and million
scales. It does not add, subtract or estimate: the engine does the arithmetic.
A label that is not recognised is reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from costpilot.data.documents import parse_number

ONE = Decimal("1")
SCALES = {
    "thousand": Decimal("1000"),
    "thousands": Decimal("1000"),
    "lakh": Decimal("100000"),
    "lakhs": Decimal("100000"),
    "lac": Decimal("100000"),
    "lacs": Decimal("100000"),
    "million": Decimal("1000000"),
    "millions": Decimal("1000000"),
    "crore": Decimal("10000000"),
    "crores": Decimal("10000000"),
    "billion": Decimal("1000000000"),
}


class StatementError(ValueError):
    """A user-facing statement extraction failure with a next step."""

    def __init__(self, code: str, message: str, remediation: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation


INCOME_KEYS = {
    "revenue", "cost_of_sales", "gross_profit", "operating_expenses", "ebitda",
    "depreciation", "ebit", "interest", "pbt", "tax", "net_income",
}
BALANCE_KEYS = {
    "fixed_assets", "other_noncurrent_assets", "inventory", "receivables", "cash",
    "other_current_assets", "total_current_assets", "total_assets", "payables",
    "short_term_debt", "other_current_liabilities", "total_current_liabilities",
    "long_term_debt", "other_noncurrent_liabilities", "share_capital", "reserves",
    "equity", "total_liabilities", "total_liabilities_and_equity",
}
CASH_KEYS = {
    "cfo", "cfi", "cff", "capex", "dividends", "net_change_cash",
    "opening_cash", "closing_cash",
}

# Canonical line -> printed labels. Labels are normalised before matching, so
# punctuation and casing do not matter. Keep the list explicit: a startswith
# rule on short words such as "cash" would steal cash-flow rows.
_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": (
        "revenue", "revenue from operations", "revenue from operation",
        "revenue from ops", "net revenue", "total revenue", "total revenues",
        "turnover", "net turnover", "gross turnover", "sales", "net sales",
        "gross sales", "total sales", "income from operations",
        "operating revenue", "operating income", "total income",
        "total operating income", "income from operation",
    ),
    "cost_of_sales": (
        "cost of sales", "cost of goods sold", "cogs", "cost of revenue",
        "cost of revenues", "cost of materials consumed", "materials consumed",
        "material cost", "material costs", "direct costs", "direct cost",
        "direct expenses", "manufacturing expenses", "cost of production",
        "operating expenses direct",
    ),
    "gross_profit": ("gross profit", "gross margin", "gross income"),
    "operating_expenses": (
        "operating expenses", "operating expense", "opex",
        "total operating expenses", "selling general and administrative expenses",
        "selling general and administrative", "sg and a", "sga",
    ),
    "opex_component": (
        "selling and distribution expenses", "selling and distribution",
        "selling expenses", "distribution expenses", "administrative expenses",
        "employee benefit expenses", "employee benefits expense",
        "other expenses", "general and administrative expenses",
        "research and development expenses",
    ),
    "ebitda": (
        "ebitda", "operating profit before depreciation",
        "operating profit before depreciation and amortisation",
        "operating profit before depreciation and amortization",
        "profit before depreciation", "earnings before interest tax depreciation and amortisation",
    ),
    "depreciation": (
        "depreciation", "depreciation and amortisation", "depreciation and amortization",
        "depreciation amortisation and impairment", "d and a", "amortisation",
        "amortization", "depreciation expense",
    ),
    "ebit": (
        "ebit", "operating profit", "operating income before tax",
        "profit from operations", "profit before interest and tax",
        "operating profit after depreciation",
    ),
    "interest": (
        "interest", "interest expense", "interest expenses", "finance cost",
        "finance costs", "borrowing cost", "borrowing costs",
        "interest and finance charges", "finance charges",
    ),
    "pbt": (
        "profit before tax", "pbt", "income before tax", "profit before taxation",
        "earnings before tax", "profit before exceptional items and tax",
    ),
    "tax": (
        "tax", "tax expense", "income tax", "income tax expense",
        "provision for tax", "current tax", "total tax expense",
        "tax expense current", "deferred tax",
    ),
    "net_income": (
        "net profit", "net income", "profit after tax", "pat",
        "profit for the year", "profit for the period", "net earnings",
        "earnings after tax", "profit after taxation", "net profit after tax",
    ),
    "fixed_assets": (
        "property plant and equipment", "property plant equipment", "ppe",
        "net block", "fixed assets", "net fixed assets", "tangible assets",
        "plant and machinery", "plant property and equipment",
    ),
    "other_noncurrent_assets": (
        "capital work in progress", "capital work in progress assets", "cwip",
        "intangible assets", "intangible asset", "goodwill", "right of use assets",
        "non current investments", "long term investments", "deferred tax asset",
        "other non current assets", "other financial assets non current",
    ),
    "inventory": (
        "inventories", "inventory", "stock", "closing stock", "closing inventory",
        "total inventories", "inventory total",
    ),
    "inventory_raw": (
        "raw materials", "raw material", "raw materials consumed",
        "stores and spares", "consumables", "packing material",
    ),
    "inventory_wip": ("work in progress", "work in progress inventory", "wip"),
    "inventory_finished": ("finished goods", "finished stock", "finished goods inventory"),
    "receivables": (
        "trade receivables", "accounts receivable", "debtors", "sundry debtors",
        "receivables", "bills receivable", "trade debtor",
    ),
    "cash": (
        "cash and cash equivalents", "cash and bank", "cash and bank balances",
        "cash", "bank balances", "cash equivalents", "cash at bank",
    ),
    "other_current_assets": (
        "other current assets", "loans and advances", "short term loans and advances",
        "prepayments", "prepaid expenses", "current investments",
        "short term investments", "other financial assets current",
    ),
    "total_current_assets": ("total current assets", "current assets"),
    "total_assets": ("total assets", "total asset"),
    "payables": (
        "trade payables", "accounts payable", "creditors", "sundry creditors",
        "payables", "bills payable", "trade creditor",
    ),
    "short_term_debt": (
        "short term borrowings", "current borrowings", "short term debt",
        "working capital loan", "bank overdraft", "current maturities of long term debt",
        "short term loans",
    ),
    "other_current_liabilities": (
        "other current liabilities", "provisions", "current provisions",
        "statutory dues", "duties and taxes", "accrued expenses",
        "other financial liabilities current",
    ),
    "total_current_liabilities": ("total current liabilities", "current liabilities"),
    "long_term_debt": (
        "long term borrowings", "long term debt", "term loan", "term loans",
        "non current borrowings", "debentures", "long term loans",
    ),
    "other_noncurrent_liabilities": (
        "deferred tax liability", "lease liabilities", "non current provisions",
        "other non current liabilities", "other financial liabilities non current",
    ),
    "share_capital": (
        "share capital", "equity share capital", "paid up capital",
        "paid up equity capital", "ordinary share capital",
    ),
    "reserves": (
        "reserves and surplus", "retained earnings", "other equity", "surplus",
        "retained profit",
    ),
    "equity": (
        "total equity", "shareholders funds", "shareholders equity", "net worth",
        "total shareholders funds", "total shareholders equity", "equity",
        "owners equity", "total equity and reserves",
    ),
    "total_liabilities": ("total liabilities",),
    "total_liabilities_and_equity": (
        "total liabilities and equity", "total liabilities and shareholders funds",
        "total equity and liabilities", "total liabilities and shareholders equity",
        "total liabilities and owners equity",
    ),
    "cfo": (
        "net cash from operating activities", "net cash provided by operating activities",
        "net cash generated from operating activities",
        "cash flow from operating activities", "net cash from operations",
        "net cash generated from operations", "cash generated from operations",
    ),
    "cfi": (
        "net cash from investing activities", "net cash used in investing activities",
        "cash flow from investing activities", "net cash used in investment activities",
        "net cash from investment activities",
    ),
    "cff": (
        "net cash from financing activities", "net cash used in financing activities",
        "cash flow from financing activities", "net cash from financing",
    ),
    "capex": (
        "purchase of property plant and equipment",
        "purchases of property plant and equipment", "capital expenditure",
        "purchase of fixed assets", "payments for property plant and equipment",
        "purchase of tangible assets",
    ),
    "dividends": ("dividends paid", "dividend paid", "dividend", "dividends"),
    "net_change_cash": (
        "net increase in cash", "net decrease in cash", "net change in cash",
        "net increase decrease in cash", "net increase in cash and cash equivalents",
        "net change in cash and cash equivalents",
    ),
    "opening_cash": (
        "cash at beginning of the year", "cash at the beginning of the year",
        "cash and cash equivalents at the beginning",
        "cash and cash equivalents at beginning of year", "opening cash",
        "cash at beginning",
    ),
    "closing_cash": (
        "cash at end of the year", "cash at the end of the year",
        "cash and cash equivalents at the end",
        "cash and cash equivalents at end of year", "closing cash", "cash at end",
    ),
}

_LOOKUP: dict[str, str] = {}
for _key, _aliases in _ALIASES.items():
    for _alias in _aliases:
        _LOOKUP[_alias] = _key

ADDITIVE = {
    "opex_component",
    "other_current_assets",
    "other_noncurrent_assets",
    "other_current_liabilities",
    "other_noncurrent_liabilities",
}
_ROW_NOISE = re.compile(r"\b(?:refer\s+)?notes?\s*\d+(?:\s*[a-z])?\b", re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_FY = re.compile(r"\bfy\s*(20\d{2}|\d{2})(?:\s*[-/]\s*(\d{2,4}))?", re.IGNORECASE)
_SHORT_YEAR = re.compile(r"\b(?:mar|march|dec|december|jun|june|sep|september)[- ]?(20\d{2}|\d{2})\b", re.IGNORECASE)
_NOTE_HEADERS = {
    "note", "notes", "schedule", "schedules", "ref", "reference", "s no",
    "sr no", "sl no", "serial no", "row", "line", "particulars",
}
_SECTION_WORDS = {
    "assets", "liabilities", "equity", "income", "expenses", "revenue",
    "sources of funds", "application of funds", "balance sheet",
    "profit and loss", "statement of profit and loss", "particulars",
}


def normalize_label(text: Any) -> str:
    value = str(text or "")
    value = _ROW_NOISE.sub(" ", value)
    value = value.replace("&", " and ")
    value = re.sub(r"[^A-Za-z0-9]+", " ", value.lower())
    return " ".join(value.split())


def _section_of(text: str) -> str | None:
    normalized = normalize_label(text)
    if not normalized:
        return None
    if "non current" in normalized or "noncurrent" in normalized:
        if "liabilit" in normalized:
            return "noncurrent_liabilities"
        if "asset" in normalized:
            return "noncurrent_assets"
    if "current" in normalized:
        if "liabilit" in normalized:
            return "current_liabilities"
        if "asset" in normalized:
            return "current_assets"
    if "equity" in normalized or "shareholders" in normalized:
        return "equity"
    return None


def lookup_line(label: Any, section: str | None = None) -> str | None:
    normalized = normalize_label(label)
    if not normalized:
        return None
    key = _LOOKUP.get(normalized)
    if key:
        return key
    for prefix in ("total ", "net ", "gross "):
        if normalized.startswith(prefix):
            key = _LOOKUP.get(normalized[len(prefix) :])
            if key:
                return key
    if normalized in {"provisions", "provision"}:
        return (
            "other_noncurrent_liabilities"
            if section == "noncurrent_liabilities"
            else "other_current_liabilities"
        )
    if normalized in {"investments", "investment"}:
        return (
            "other_current_assets" if section == "current_assets" else "other_noncurrent_assets"
        )
    if normalized in {"borrowings", "borrowing"}:
        return "short_term_debt" if section == "current_liabilities" else "long_term_debt"
    return None


def _scale_from_text(text: Any) -> Decimal | None:
    raw = str(text or "").lower()
    if not raw:
        return None
    if re.search(r"\b(?:in|amount in|rs\.? in|inr in|rupees in)?\s*lakhs?\b", raw):
        return SCALES["lakhs"]
    if re.search(r"\b(?:in|amount in)\s+lacs?\b", raw):
        return SCALES["lacs"]
    if re.search(r"\b(?:in|amount in)\s+crores?\b", raw):
        return SCALES["crores"]
    if re.search(r"\b(?:in|amount in)\s+millions?\b", raw) or "₹ mn" in raw or "rs mn" in raw:
        return SCALES["millions"]
    if re.search(r"\b(?:in|amount in)\s+thousands?\b", raw) or "'000" in raw or "₹ 000" in raw:
        return SCALES["thousands"]
    if re.search(r"\b(?:in|amount in)\s+billions?\b", raw):
        return SCALES["billion"]
    return None


def _year_from_header(header: str) -> int | None:
    raw = str(header or "")
    match = _FY.search(raw)
    if match:
        first = match.group(1)
        year = int(first) if len(first) == 4 else 2000 + int(first)
        return year
    match = _SHORT_YEAR.search(raw)
    if match:
        value = match.group(1)
        return int(value) if len(value) == 4 else 2000 + int(value)
    match = _YEAR.search(raw)
    return int(match.group(1)) if match else None


def _header_role(header: str) -> str | None:
    lowered = normalize_label(header)
    if any(word in lowered for word in ("previous", "prior", "last year", "comparative")):
        return "prior"
    if any(word in lowered for word in ("current", "this year", "latest")):
        return "current"
    if "amount" in lowered or "value" in lowered or "total" == lowered:
        return "amount"
    return None


def _is_numeric(text: Any) -> bool:
    return parse_number(str(text or "")) is not None


def _label_index(table: dict[str, Any]) -> int | None:
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    best_index: int | None = None
    best_score = 0
    best_text = 0
    for index in range(len(headers)):
        hits = 0
        text_cells = 0
        for row in rows[:120]:
            if index >= len(row):
                continue
            cell = str(row[index] or "").strip()
            if not cell:
                continue
            if _is_numeric(cell):
                continue
            text_cells += 1
            if lookup_line(cell) is not None:
                hits += 1
        if hits > best_score or (hits == best_score and text_cells > best_text):
            best_index = index
            best_score = hits
            best_text = text_cells
    if best_index is not None and best_score >= 2:
        return best_index
    # A statement with unfamiliar labels still has one text-heavy column.
    best_index = None
    best_text = 0
    for index in range(len(headers)):
        text_cells = sum(
            1
            for row in rows[:80]
            if index < len(row) and str(row[index] or "").strip() and not _is_numeric(row[index])
        )
        if text_cells > best_text:
            best_index = index
            best_text = text_cells
    return best_index


@dataclass
class _Column:
    index: int
    header: str
    year: int | None
    role: str | None
    scale: Decimal
    numeric: int


def _amount_columns(table: dict[str, Any], label_index: int) -> list[_Column]:
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    columns: list[_Column] = []
    for index, header in enumerate(headers):
        if index == label_index:
            continue
        if normalize_label(header) in _NOTE_HEADERS:
            continue
        numeric = sum(
            1
            for row in rows[:200]
            if index < len(row) and str(row[index] or "").strip() and _is_numeric(row[index])
        )
        if numeric < 2:
            continue
        populated = sum(
            1 for row in rows[:200] if index < len(row) and str(row[index] or "").strip()
        )
        if populated and numeric / populated < 0.4:
            continue
        columns.append(
            _Column(
                index=index,
                header=str(header or ""),
                year=_year_from_header(str(header or "")),
                role=_header_role(str(header or "")),
                scale=_scale_from_text(header) or ONE,
                numeric=numeric,
            )
        )
    return columns


def _table_scale(table: dict[str, Any], title_text: str = "") -> tuple[Decimal, str | None]:
    candidates = [str(table.get("name") or ""), title_text]
    candidates.extend(str(header or "") for header in (table.get("headers") or []))
    for row in (table.get("rows") or [])[:3]:
        candidates.extend(str(cell or "") for cell in row)
    for text in candidates:
        scale = _scale_from_text(text)
        if scale is not None:
            return scale, text.strip()[:120]
    return ONE, None


def _looks_like_header_row(row: list[Any]) -> bool:
    period_cells = sum(
        1
        for cell in row
        if _year_from_header(str(cell or "")) is not None
        or _header_role(str(cell or "")) in {"current", "prior"}
    )
    return period_cells >= 2


def _prepare_table(table: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Promote a printed header row and keep any title text for scale detection."""
    headers = list(table.get("headers") or [])
    rows = [list(row) for row in (table.get("rows") or [])]
    title_parts = list(headers)
    for index in range(min(3, len(rows))):
        if not _looks_like_header_row(rows[index]):
            continue
        title_parts.extend(" ".join(str(cell or "") for cell in row) for row in rows[:index])
        promoted = [
            str(cell or "").strip() or (headers[position] if position < len(headers) else f"Column {position + 1}")
            for position, cell in enumerate(rows[index])
        ]
        headers = promoted
        rows = rows[index + 1 :]
        break
    view = {
        "name": table.get("name"),
        "headers": headers,
        "rows": rows,
        "row_count": len(rows),
        "title": " ".join(title_parts),
    }
    return view, " ".join(title_parts)


@dataclass
class StatementExtract:
    values: dict[str, Decimal] = field(default_factory=dict)
    prior: dict[str, Decimal] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    periods: list[str] = field(default_factory=list)
    current_period: str | None = None
    prior_period: str | None = None
    available: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scales: dict[str, str] = field(default_factory=dict)

    @property
    def line_count(self) -> int:
        return len(self.values)

    def preview(self) -> dict[str, Any]:
        return {
            "available": list(self.available),
            "periods": list(self.periods),
            "current_period": self.current_period,
            "prior_period": self.prior_period,
            "lines": self.line_count,
            "unmatched": len(self.unmatched),
            "scales": dict(self.scales),
        }


def _choose_periods(
    tables: list[tuple[str, dict[str, Any], int, list[_Column]]],
    period: str | None,
) -> tuple[str | None, str | None, int | None]:
    years = sorted(
        {column.year for _, _, _, columns in tables for column in columns if column.year},
        reverse=True,
    )
    labels = []
    for _, _table, _, columns in tables:
        for column in columns:
            if column.header and column.header not in labels:
                labels.append(column.header)
            if column.year:
                alias = _period_label_for_year(column.year)
                if alias and alias not in labels:
                    labels.append(alias)
    if period:
        wanted = normalize_label(period)
        wanted_year = _year_from_header(period)
        match = next(
            (
                label
                for label in labels
                if (wanted and wanted in normalize_label(label))
                or (wanted_year is not None and _year_from_header(label) == wanted_year)
            ),
            None,
        )
        if match is None:
            options = ", ".join(repr(label) for label in labels[:6]) or "no period column"
            raise StatementError(
                "E_STATEMENT_PERIOD",
                f"The file has no column for period {period!r}.",
                f"Use one of these columns: {options}.",
            )
        year = _year_from_header(match)
        prior = next((item for item in years if year is not None and item < year), None)
        return match, _period_label_for_year(prior), year
    if years:
        current = years[0]
        prior = years[1] if len(years) > 1 else None
        return (
            _period_label_for_year(current),
            _period_label_for_year(prior),
            current,
        )
    current = next(
        (column.header for _, _, _, columns in tables for column in columns if column.role == "current"),
        None,
    )
    if current is None:
        current = next(
            (column.header for _, _, _, columns in tables for column in columns if column.role == "amount"),
            labels[0] if labels else None,
        )
    prior = next(
        (column.header for _, _, _, columns in tables for column in columns if column.role == "prior"),
        None,
    )
    return current, prior, None


def _period_label_for_year(year: int | None) -> str | None:
    return f"FY{year}" if year else None


def _column_for(
    columns: list[_Column],
    current_period: str | None,
    current_year: int | None,
    prior_period: str | None,
    prior_year: int | None,
) -> tuple[_Column | None, _Column | None]:
    current: _Column | None = None
    prior: _Column | None = None
    for column in columns:
        if current_year is not None and column.year == current_year:
            current = column
        elif prior_year is not None and column.year == prior_year:
            prior = column
    if current is None and current_period:
        current = next(
            (column for column in columns if normalize_label(column.header) == normalize_label(current_period)),
            None,
        )
    if prior is None and prior_period:
        prior = next(
            (column for column in columns if normalize_label(column.header) == normalize_label(prior_period)),
            None,
        )
    if current is None and current_year is None and len(columns) == 1:
        current = columns[0]
    if current is None:
        current = next((column for column in columns if column.role == "current"), None)
    if current is None and current_year is None and len(columns) > 1:
        current = columns[0]
    if prior is None:
        prior = next((column for column in columns if column.role == "prior"), None)
    if prior is None and prior_year is None and len(columns) > 1:
        extra = [column for column in columns if column is not current]
        if len(extra) == 1:
            prior = extra[0]
    return current, prior


def _rollup(extract: StatementExtract, key: str, parts: tuple[str, ...], label: str) -> None:
    if key in extract.values:
        return
    present = [part for part in parts if part in extract.values]
    if not present:
        return
    extract.values[key] = sum((extract.values[part] for part in present), Decimal("0"))
    if key not in extract.prior and all(part in extract.prior for part in present):
        extract.prior[key] = sum((extract.prior[part] for part in present), Decimal("0"))
    extract.labels.setdefault(key, label)
    extract.sources.setdefault(
        key,
        "; ".join(extract.sources.get(part, part) for part in present),
    )


def _classify(extract: StatementExtract) -> list[str]:
    income = len(INCOME_KEYS & extract.values.keys())
    balance = len(BALANCE_KEYS & extract.values.keys())
    cash = len(CASH_KEYS & extract.values.keys())
    available = []
    if "revenue" in extract.values and income >= 3:
        available.append("income")
    if balance >= 4 and ("total_assets" in extract.values or "fixed_assets" in extract.values):
        available.append("balance")
    if cash >= 2:
        available.append("cashflow")
    return available


def detect_statements(attachment: dict[str, Any], period: str | None = None) -> StatementExtract | None:
    """Return the statement lines found in an attachment, or None."""
    try:
        extract = _parse(attachment, period=period)
    except StatementError:
        return None
    return extract if extract.available else None


def parse_statements(attachment: dict[str, Any], period: str | None = None) -> StatementExtract:
    extract = _parse(attachment, period=period)
    if not extract.available:
        tables = attachment.get("tables") or []
        if not tables:
            raise StatementError(
                "E_STATEMENT_SHAPE",
                "This attachment has no table to read statements from.",
                "Attach an Excel workbook, CSV or a PDF with a statement table.",
            )
        raise StatementError(
            "E_STATEMENT_SHAPE",
            "The attachment does not contain a recognisable income statement, balance sheet or cash flow statement.",
            "Rename the row labels to standard lines such as Revenue, Cost of sales, Gross profit, "
            "Trade receivables, Trade payables, Total assets and Total equity, then upload again.",
        )
    return extract


def _parse(attachment: dict[str, Any], period: str | None = None) -> StatementExtract:
    extract = StatementExtract()
    tables: list[tuple[str, dict[str, Any], int, list[_Column]]] = []
    for raw_table in attachment.get("tables") or []:
        table, _title = _prepare_table(raw_table)
        label_index = _label_index(table)
        if label_index is None:
            continue
        columns = _amount_columns(table, label_index)
        if not columns:
            continue
        tables.append((str(table.get("name") or "table"), table, label_index, columns))
    if not tables:
        return extract

    current_period, prior_period, current_year = _choose_periods(tables, period)
    prior_year = None
    if prior_period:
        prior_year = _year_from_header(prior_period)
    extract.current_period = current_period
    extract.prior_period = prior_period
    labels_seen: set[str] = set()
    for _, _table, _label_column, columns in tables:
        for column in columns:
            if column.header and column.header not in labels_seen:
                labels_seen.add(column.header)
            if column.year:
                alias = _period_label_for_year(column.year)
                if alias and alias not in labels_seen:
                    labels_seen.add(alias)
        if current_period and current_period not in labels_seen:
            labels_seen.add(current_period)
        if prior_period and prior_period not in labels_seen:
            labels_seen.add(prior_period)
    extract.periods = list(labels_seen)

    base_scales = {
        table_name: _table_scale(table, str(table.get("title") or ""))
        for table_name, table, _, _ in tables
    }
    distinct_scales = {value for value, _ in base_scales.values() if value != ONE}
    global_scale = next(iter(distinct_scales)) if len(distinct_scales) == 1 else ONE

    for table_name, table, label_index, columns in tables:
        current, prior = _column_for(
            columns, current_period, current_year, prior_period, prior_year
        )
        if current is None:
            continue
        table_scale, scale_note = base_scales.get(table_name, (ONE, None))
        if current.scale != ONE:
            scale = current.scale
        elif table_scale != ONE:
            scale = table_scale
        else:
            scale = global_scale
            if global_scale != ONE:
                extract.warnings.append(
                    f"{table_name!r} had no stated amount scale; the scale stated on another sheet was used."
                )
        if scale != ONE and str(scale) not in extract.scales:
            if scale_note:
                note = scale_note
            elif global_scale == scale:
                note = next(
                    (
                        candidate
                        for value, candidate in base_scales.values()
                        if value == scale and candidate
                    ),
                    f"amounts scaled from {current.header or table_name}",
                )
            else:
                note = f"amounts scaled from {current.header or table_name}"
            extract.scales[str(scale)] = note
        section: str | None = None
        for position, row in enumerate(table.get("rows") or [], start=1):
            label = str(row[label_index] if label_index < len(row) else "").strip()
            if not label:
                continue
            key = lookup_line(label, section)
            if key is None:
                heading = _section_of(label)
                if heading and not any(
                    _is_numeric(row[column.index]) if column.index < len(row) else False
                    for column in columns
                ):
                    section = heading
                elif normalize_label(label) in _SECTION_WORDS:
                    pass
                elif not _is_numeric(label) and len(extract.unmatched) < 12:
                    extract.unmatched.append(label)
                continue
            raw = row[current.index] if current.index < len(row) else ""
            number = parse_number(str(raw))
            if number is None:
                continue
            value = number * scale
            if key in ADDITIVE and key in extract.values:
                extract.values[key] += value
                if prior is not None:
                    prior_raw = row[prior.index] if prior.index < len(row) else ""
                    prior_number = parse_number(str(prior_raw))
                    if prior_number is not None:
                        extract.prior[key] = extract.prior.get(key, Decimal("0")) + prior_number * scale
            elif key not in extract.values:
                extract.values[key] = value
                extract.labels[key] = label
                extract.sources[key] = f"{table_name} · row {position} · {current.header or 'amount'}"
                if prior is not None:
                    prior_raw = row[prior.index] if prior.index < len(row) else ""
                    prior_number = parse_number(str(prior_raw))
                    if prior_number is not None:
                        extract.prior[key] = prior_number * scale
            else:
                extract.warnings.append(
                    f"{label!r} was ignored because {extract.labels.get(key, key)!r} already supplies "
                    f"{key.replace('_', ' ')}."
                )
    _rollup(extract, "inventory", ("inventory_raw", "inventory_wip", "inventory_finished"), "Inventories")
    _rollup(extract, "operating_expenses", ("opex_component",), "Operating expenses")
    _rollup(extract, "equity", ("share_capital", "reserves"), "Total equity")
    # Statements often print expenses or deductions as negative amounts. The
    # engine works with positive expense lines, so take the magnitude here.
    for key in ("cost_of_sales", "operating_expenses", "depreciation", "interest", "tax"):
        if key in extract.values and extract.values[key] < Decimal("0"):
            extract.values[key] = -extract.values[key]
        if key in extract.prior and extract.prior[key] < Decimal("0"):
            extract.prior[key] = -extract.prior[key]
    extract.available = _classify(extract)
    if len(extract.scales) > 1:
        extract.warnings.append("The file uses more than one amount scale; each column was scaled separately.")
    return extract
