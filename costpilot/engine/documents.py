"""Deterministic analysis of an attached document.

The model may choose an operation and name a table or column; this module reads
the printed values and calculates with Decimal. It never guesses a missing
figure, and every step cites the attachment it came from.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from costpilot.data.documents import (
    choose_table,
    column_values,
    find_column,
    header_unit,
    numeric_headers,
    parse_number,
    snippets,
)
from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, add, display_amount, div
from costpilot.engine.narration import render
from costpilot.engine.trace import CalcResult, Fact, StepBuilder, Warning
from costpilot.engine.validation import validate_result

OPERATIONS = {
    "profile": "Summarise a table: rows, numeric columns, totals and averages.",
    "sum": "Add the printed values in one column.",
    "mean": "Average the printed values in one column.",
    "min": "Find the smallest printed value in one column.",
    "max": "Find the largest printed value in one column.",
    "count": "Count rows, or numeric values in one column.",
    "difference": "Subtract one column total from another.",
    "top": "List the highest or lowest rows by one column.",
    "search": "Find the rows or passages that mention a term.",
    "ask": "Quote the passages that answer a question and list the figures printed there.",
}
CURRENCY_PREFIX = {
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
}
_SCALE = {
    "k": Decimal("1000"),
    "m": Decimal("1000000"),
    "lakh": Decimal("100000"),
    "lakhs": Decimal("100000"),
    "crore": Decimal("10000000"),
    "cr": Decimal("10000000"),
}
_FIGURE = re.compile(
    r"(?<![\w.,\-])(?:(₹|Rs\.?|INR|\$|€|£)\s*)?"
    r"(\d[\d,]*(?:\.\d+)?)\s*(%|percent\b|lakhs?\b|crores?\b|cr\b|k\b|m\b)?",
    re.IGNORECASE,
)


def _attachment_name(attachment: dict[str, Any]) -> str:
    return str(attachment.get("name") or "attachment")


def _source(attachment: dict[str, Any], table: dict[str, Any] | None = None) -> list[str]:
    refs = [_attachment_name(attachment)]
    if table is not None:
        refs.append(f"table:{table['name']}")
    return refs


def _require_table(attachment: dict[str, Any], table_name: str | None) -> dict[str, Any]:
    tables = attachment.get("tables") or []
    if not tables:
        raise DomainError(
            "E_ATTACHMENT_TABLE",
            "This attachment has no table to analyse.",
            "Attach an Excel workbook or CSV, or ask a quotation question such as "
            "'what does the document say about stock?'.",
        )
    table = choose_table(attachment, table_name)
    if table is None:
        names = ", ".join(repr(row["name"]) for row in tables)
        raise DomainError(
            "E_ATTACHMENT_TABLE",
            f"The attachment has no table named {table_name!r}.",
            f"Use one of these tables: {names}.",
        )
    return table


def _require_column(table: dict[str, Any], name: str | None) -> str:
    if not name:
        raise DomainError(
            "E_ATTACHMENT_COLUMN",
            "The question does not name a column.",
            "Name the column, for example 'sum Actual' or 'average of Margin %'.",
        )
    header = find_column(table, name)
    if header is None:
        headers = ", ".join(repr(item) for item in table["headers"])
        raise DomainError(
            "E_ATTACHMENT_COLUMN",
            f"The table {table['name']!r} has no column named {name!r}.",
            f"Use one of these columns: {headers}.",
        )
    return header


def _parsed_values(table: dict[str, Any], header: str) -> tuple[list[str], list[tuple[int, str, Decimal]]]:
    values = column_values(table, header)
    parsed = []
    for position, raw in enumerate(values, start=1):
        number = parse_number(raw)
        if number is not None:
            parsed.append((position, raw, number))
    return values, parsed


def _numeric_column(
    table: dict[str, Any], name: str | None
) -> tuple[str, str, list[str], list[tuple[int, str, Decimal]]]:
    header = _require_column(table, name)
    values, parsed = _parsed_values(table, header)
    if not parsed:
        numeric = numeric_headers(table)
        hint = ", ".join(repr(item) for item in numeric) if numeric else "none"
        raise DomainError(
            "E_ATTACHMENT_NO_NUMBERS",
            f"The column {header!r} has no printed numbers.",
            f"Use a numeric column. Numeric columns in this table: {hint}.",
        )
    unit = header_unit(header, values)
    return header, unit, values, parsed


def _row_dict(table: dict[str, Any], position: int) -> dict[str, Any]:
    row = table["rows"][position - 1]
    return {"row": position, **{header: row[index] if index < len(row) else "" for index, header in enumerate(table["headers"])}}


def _skipped(values: list[str], parsed: list[tuple[int, str, Decimal]], header: str) -> list[Warning]:
    missing = len(values) - len(parsed)
    if missing <= 0:
        return []
    return [
        Warning(
            "DOC_NON_NUMERIC",
            f"{missing} value(s) in {header!r} were blank or not numeric and were left out.",
            "info",
            "Check the skipped cells if the total looks low.",
        )
    ]


def _percent_sum_warning(unit: str, operation: str) -> list[Warning]:
    if unit == "%" and operation in {"sum", "difference"}:
        return [
            Warning(
                "DOC_PERCENT_SUM",
                "Adding percentages assumes every row is a share of the same base.",
                "info",
                "Prefer an average or a weighted total if the bases differ.",
            )
        ]
    return []


def _profile(
    attachment: dict[str, Any], table: dict[str, Any], builder: StepBuilder, limit: int
) -> CalcResult:
    row_count = Decimal(int(table["row_count"]))
    builder.add(
        step_id="doc_rows",
        rule_id="DOC-001",
        label="Rows in the table",
        formula="count(non-empty data rows)",
        inputs={"table": table["name"]},
        value=row_count,
        unit="units",
        source_refs=_source(attachment, table),
    )
    numerics = numeric_headers(table)
    profile_rows: list[dict[str, Any]] = []
    for index, header in enumerate(numerics[: max(1, min(limit, 8))]):
        values, parsed = _parsed_values(table, header)
        if not parsed:
            continue
        unit = header_unit(header, values)
        total = add(*(item[2] for item in parsed))
        mean = div(total, Decimal(len(parsed)), "document mean")
        lows = min(item[2] for item in parsed)
        highs = max(item[2] for item in parsed)
        builder.add(
            step_id=f"doc_profile_{index}",
            rule_id="DOC-003",
            label=f"Mean {header}",
            formula="sum(parsed values) / count(parsed values)",
            inputs={"column": header, "count": len(parsed)},
            value=mean,
            unit=unit,
            source_refs=_source(attachment, table),
            note=f"min {display_amount(lows, unit)}, max {display_amount(highs, unit)}",
        )
        profile_rows.append(
            {
                "column": header,
                "unit": unit,
                "count": len(parsed),
                "sum": str(total),
                "mean": str(mean),
                "min": str(lows),
                "max": str(highs),
            }
        )
    verdict = (
        f"The table {table['name']!r} has {int(row_count)} data rows across "
        f"{len(table['headers'])} columns, and {len(numerics)} numeric column(s) were profiled."
    )
    result = CalcResult(
        value=row_count,
        unit="units",
        favorable=None,
        verdict=verdict,
        steps=builder.steps,
        facts=[Fact("f_doc_rows", "doc_rows", "Rows in the table", row_count, "units")],
        rationale_template="The attached table holds {{f_doc_rows}} data rows; the column profile is below.",
        meta={
            "attachment_id": attachment.get("attachment_id"),
            "attachment_name": attachment.get("name"),
            "operation": "profile",
            "table": table["name"],
            "row_count": int(table["row_count"]),
            "column_count": len(table["headers"]),
            "numeric_columns": numerics,
            "profile": profile_rows,
            "headers": table["headers"],
            "rows": [_row_dict(table, index) for index in range(1, min(int(row_count), 8) + 1)],
        },
    )
    return _finish(result)


def _column_operation(
    attachment: dict[str, Any],
    table: dict[str, Any],
    builder: StepBuilder,
    operation: str,
    column: str | None,
) -> CalcResult:
    header, unit, values, parsed = _numeric_column(table, column)
    total = add(*(item[2] for item in parsed))
    count = Decimal(len(parsed))
    warnings = _skipped(values, parsed, header) + _percent_sum_warning(unit, operation)

    if operation == "sum":
        builder.add(
            step_id="doc_sum",
            rule_id="DOC-002",
            label=f"Sum of {header}",
            formula="sum(parsed values)",
            inputs={"column": header, "count": len(parsed)},
            value=total,
            unit=unit,
            source_refs=_source(attachment, table),
        )
        facts = [Fact("f_doc_sum", "doc_sum", f"Sum of {header}", total, unit)]
        value = total
        verdict = f"The printed values in {header!r} add to {display_amount(total, unit)} across {len(parsed)} row(s)."
        template = "The attached document gives {{f_doc_sum}} for the selected column."
    elif operation == "mean":
        builder.add(
            step_id="doc_sum",
            rule_id="DOC-002",
            label=f"Sum of {header}",
            formula="sum(parsed values)",
            inputs={"column": header, "count": len(parsed)},
            value=total,
            unit=unit,
            source_refs=_source(attachment, table),
        )
        mean = div(total, count, "document mean")
        builder.add(
            step_id="doc_mean",
            rule_id="DOC-003",
            label=f"Mean of {header}",
            formula="sum(parsed values) / count(parsed values)",
            inputs={"column": header, "total": total, "count": len(parsed)},
            value=mean,
            unit=unit,
            source_refs=_source(attachment, table),
        )
        facts = [Fact("f_doc_mean", "doc_mean", f"Mean of {header}", mean, unit)]
        value = mean
        verdict = f"The average of {header!r} is {display_amount(mean, unit)} over {len(parsed)} printed value(s)."
        template = "The average for the selected column is {{f_doc_mean}}."
    elif operation in {"min", "max"}:
        value = min(item[2] for item in parsed) if operation == "min" else max(item[2] for item in parsed)
        builder.add(
            step_id="doc_extreme",
            rule_id="DOC-004",
            label=f"{'Lowest' if operation == 'min' else 'Highest'} {header}",
            formula="min(parsed values)" if operation == "min" else "max(parsed values)",
            inputs={"column": header, "count": len(parsed)},
            value=value,
            unit=unit,
            source_refs=_source(attachment, table),
        )
        facts = [Fact("f_doc_extreme", "doc_extreme", f"{operation.title()} of {header}", value, unit)]
        word = "smallest" if operation == "min" else "largest"
        verdict = f"The {word} printed value in {header!r} is {display_amount(value, unit)}."
        template = "The selected column's extreme value is {{f_doc_extreme}}."
    elif operation == "count":
        builder.add(
            step_id="doc_count",
            rule_id="DOC-001",
            label=f"Numeric values in {header}",
            formula="count(parsed values)",
            inputs={"column": header, "rows": len(values)},
            value=count,
            unit="units",
            source_refs=_source(attachment, table),
        )
        facts = [Fact("f_doc_count", "doc_count", f"Numeric values in {header}", count, "units")]
        value = count
        verdict = f"{int(count)} of {len(values)} row(s) in {header!r} carry a printed number."
        template = "The selected column has {{f_doc_count}} numeric values."
    else:  # pragma: no cover - guarded by caller
        raise DomainError("E_ATTACHMENT_OPERATION", f"Unsupported operation {operation!r}.", "Use profile, sum, mean, min, max, count, difference, top, search or ask.")

    result = CalcResult(
        value=value,
        unit=facts[0].unit,
        favorable=None,
        verdict=verdict,
        steps=builder.steps,
        facts=facts,
        warnings=warnings,
        rationale_template=template,
        meta={
            "attachment_id": attachment.get("attachment_id"),
            "attachment_name": attachment.get("name"),
            "operation": operation,
            "table": table["name"],
            "column": header,
            "unit": unit,
            "count": len(parsed),
            "headers": table["headers"],
            "rows": [],
        },
    )
    return _finish(result)


def _difference(
    attachment: dict[str, Any],
    table: dict[str, Any],
    builder: StepBuilder,
    column: str | None,
    compare_column: str | None,
) -> CalcResult:
    left_header, left_unit, left_values, left = _numeric_column(table, column)
    right_header, right_unit, right_values, right = _numeric_column(table, compare_column)
    left_total = add(*(item[2] for item in left))
    right_total = add(*(item[2] for item in right))
    difference = left_total - right_total
    unit = left_unit if left_unit == right_unit else "number"
    warnings = (
        _skipped(left_values, left, left_header)
        + _skipped(right_values, right, right_header)
    )
    if left_unit != right_unit:
        warnings.append(
            Warning(
                "DOC_MIXED_UNITS",
                f"{left_header!r} is in {left_unit} and {right_header!r} is in {right_unit}; the difference is shown as a plain number.",
                "warning",
                "Compare columns that share a unit before drawing a conclusion.",
            )
        )
    builder.add(
        step_id="doc_left",
        rule_id="DOC-002",
        label=f"Total {left_header}",
        formula="sum(parsed values)",
        inputs={"column": left_header, "count": len(left)},
        value=left_total,
        unit=left_unit,
        source_refs=_source(attachment, table),
    )
    builder.add(
        step_id="doc_right",
        rule_id="DOC-002",
        label=f"Total {right_header}",
        formula="sum(parsed values)",
        inputs={"column": right_header, "count": len(right)},
        value=right_total,
        unit=right_unit,
        source_refs=_source(attachment, table),
    )
    builder.add(
        step_id="doc_difference",
        rule_id="DOC-005",
        label=f"{left_header} minus {right_header}",
        formula="sum(left column) - sum(right column)",
        inputs={"left": left_header, "right": right_header},
        value=difference,
        unit=unit,
        source_refs=_source(attachment, table),
    )
    facts = [
        Fact("f_doc_left", "doc_left", f"Total {left_header}", left_total, left_unit),
        Fact("f_doc_right", "doc_right", f"Total {right_header}", right_total, right_unit),
        Fact("f_doc_diff", "doc_difference", f"{left_header} minus {right_header}", difference, unit),
    ]
    verdict = (
        f"{left_header!r} totals {display_amount(left_total, left_unit)} and "
        f"{right_header!r} totals {display_amount(right_total, right_unit)}; "
        f"the difference is {display_amount(difference, unit)}."
    )
    result = CalcResult(
        value=difference,
        unit=unit,
        favorable=None,
        verdict=verdict,
        steps=builder.steps,
        facts=facts,
        warnings=warnings,
        rationale_template=(
            "{{f_doc_left}} minus {{f_doc_right}} gives a difference of {{f_doc_diff}}."
        ),
        meta={
            "attachment_id": attachment.get("attachment_id"),
            "attachment_name": attachment.get("name"),
            "operation": "difference",
            "table": table["name"],
            "column": left_header,
            "compare_column": right_header,
            "left_total": str(left_total),
            "right_total": str(right_total),
            "left_unit": left_unit,
            "right_unit": right_unit,
            "headers": table["headers"],
            "rows": [],
        },
    )
    return _finish(result)


def _top(
    attachment: dict[str, Any],
    table: dict[str, Any],
    builder: StepBuilder,
    column: str | None,
    limit: int,
    direction: str,
) -> CalcResult:
    header, unit, values, parsed = _numeric_column(table, column)
    descending = str(direction or "top").lower() not in {"bottom", "low", "lowest", "asc", "ascending"}
    ordered = sorted(parsed, key=lambda item: item[2], reverse=descending)
    size = max(1, min(int(limit or 5), 20))
    selected = ordered[:size]
    headline = selected[0][2] if selected else ZERO
    builder.add(
        step_id="doc_top",
        rule_id="DOC-006",
        label=f"{'Highest' if descending else 'Lowest'} {header}",
        formula="sort rows by column and take the first value",
        inputs={"column": header, "direction": "descending" if descending else "ascending", "limit": size},
        value=headline,
        unit=unit,
        source_refs=_source(attachment, table),
    )
    builder.add(
        step_id="doc_selected",
        rule_id="DOC-006",
        label="Rows selected",
        formula="count(selected rows)",
        inputs={"column": header, "limit": size},
        value=Decimal(len(selected)),
        unit="units",
        source_refs=_source(attachment, table),
    )
    facts = [
        Fact("f_doc_top", "doc_top", f"{'Highest' if descending else 'Lowest'} {header}", headline, unit),
        Fact("f_doc_selected", "doc_selected", "Rows selected", Decimal(len(selected)), "units"),
    ]
    word = "highest" if descending else "lowest"
    verdict = (
        f"The {word} printed value in {header!r} is {display_amount(headline, unit)}; "
        f"the {len(selected)} selected row(s) are listed below."
    )
    result = CalcResult(
        value=headline,
        unit=unit,
        favorable=None,
        verdict=verdict,
        steps=builder.steps,
        facts=facts,
        warnings=_skipped(values, parsed, header),
        rationale_template=(
            "The {{f_doc_top}} value is the top line for this column; "
            "{{f_doc_selected}} rows are shown in the table."
        ),
        meta={
            "attachment_id": attachment.get("attachment_id"),
            "attachment_name": attachment.get("name"),
            "operation": "top",
            "table": table["name"],
            "column": header,
            "direction": "descending" if descending else "ascending",
            "headers": table["headers"],
            "rows": [_row_dict(table, position) for position, _, _ in selected],
        },
    )
    return _finish(result)


def _matches(value: str, query: str) -> bool:
    left = " ".join(str(value).lower().split())
    right = " ".join(str(query).lower().split())
    if not right:
        return False
    if right in left:
        return True
    left_number = parse_number(left)
    right_number = parse_number(right)
    return left_number is not None and right_number is not None and left_number == right_number


def _search(
    attachment: dict[str, Any],
    table: dict[str, Any] | None,
    builder: StepBuilder,
    query: str | None,
    column: str | None,
    limit: int,
) -> CalcResult:
    if not query or not str(query).strip():
        raise DomainError(
            "E_ATTACHMENT_QUERY",
            "The question does not include a term to search for.",
            "Give a value or a word, for example 'find rows for PRD-A'.",
        )
    needle = str(query).strip()
    size = max(1, min(int(limit or 10), 40))
    matches: list[dict[str, Any]] = []
    if table is not None:
        header = None
        if column:
            header = _require_column(table, column)
        for position in range(1, int(table["row_count"]) + 1):
            row = _row_dict(table, position)
            cells = [row.get(item, "") for item in table["headers"]]
            if header is not None:
                cells = [row.get(header, "")]
            if any(_matches(cell, needle) for cell in cells):
                matches.append(row)
                if len(matches) >= size:
                    break
    if not matches:
        for quote in snippets(str(attachment.get("text") or ""), needle, limit=size):
            matches.append({"passage": quote})
    if not matches:
        raise DomainError(
            "E_ATTACHMENT_NO_MATCH",
            f"No row or passage mentions {needle!r}.",
            "Try a shorter term, a product code, or a column value that appears in the document.",
        )
    builder.add(
        step_id="doc_matches",
        rule_id="DOC-006",
        label="Matching rows",
        formula="count(rows or passages that contain the query)",
        inputs={"query": needle, "table": table["name"] if table else "text"},
        value=Decimal(len(matches)),
        unit="units",
        source_refs=_source(attachment, table),
    )
    facts = [Fact("f_doc_matches", "doc_matches", "Matching rows", Decimal(len(matches)), "units")]
    verdict = f"{len(matches)} row(s) or passage(s) in the attachment mention {needle!r}."
    result = CalcResult(
        value=Decimal(len(matches)),
        unit="units",
        favorable=None,
        verdict=verdict,
        steps=builder.steps,
        facts=facts,
        rationale_template="The attachment has {{f_doc_matches}} matching row(s) or passage(s).",
        meta={
            "attachment_id": attachment.get("attachment_id"),
            "attachment_name": attachment.get("name"),
            "operation": "search",
            "table": table["name"] if table else None,
            "column": column,
            "query": needle,
            "headers": table["headers"] if table else ["Passage"],
            "rows": matches,
        },
    )
    return _finish(result)


def _figure_rows(text: str) -> list[dict[str, Any]]:
    # Table markers and row labels are structure, not printed figures.
    text = re.sub(r"\[Table:[^\]]*\]", " ", text)
    text = re.sub(r"(?<![\w.,])[0-9]{1,3}\s*:(?=[\s|])", " ", text)
    figures: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in _FIGURE.finditer(text):
        prefix, raw, suffix = match.group(1), match.group(2), match.group(3)
        try:
            value = Decimal(raw.replace(",", ""))
        except Exception:
            continue
        unit = "number"
        if prefix:
            unit = CURRENCY_PREFIX.get(prefix.lower(), "number")
        elif suffix and (suffix.lower() == "%" or suffix.lower().startswith("percent")):
            unit = "%"
        if suffix and suffix.lower() in _SCALE:
            value = value * _SCALE[suffix.lower()]
        if not prefix and not suffix and value == value.to_integral_value() and Decimal("1900") <= value <= Decimal("2100"):
            continue
        key = (str(value), unit)
        if key in seen:
            continue
        seen.add(key)
        figures.append(
            {
                "printed": match.group(0).strip(),
                "value": str(value),
                "display": display_amount(value, unit),
                "unit": unit,
                "context": " ".join(text[max(0, match.start() - 60) : match.end() + 60].split()),
            }
        )
        if len(figures) >= 12:
            break
    return figures


def _ask(
    attachment: dict[str, Any],
    table: dict[str, Any] | None,
    builder: StepBuilder,
    question: str | None,
) -> CalcResult:
    if not question or not str(question).strip():
        raise DomainError(
            "E_ATTACHMENT_QUERY",
            "The question is empty.",
            "Ask what the document says about a topic, or ask for a total on a named column.",
        )
    needle = str(question).strip()
    text = str(attachment.get("text") or "")
    quotes = snippets(text, needle, limit=4)
    if not quotes and table is not None:
        for position in range(1, min(int(table["row_count"]), 40) + 1):
            row = _row_dict(table, position)
            if any(_matches(str(cell), needle) for cell in row.values()):
                quotes.append(" | ".join(str(cell) for cell in row.values() if cell != row["row"]))
    if not quotes:
        raise DomainError(
            "E_ATTACHMENT_NO_MATCH",
            f"The attachment has no passage matching {needle!r}.",
            "Name a topic that appears in the document, or ask for a total on a named column.",
        )
    figures = _figure_rows("\n".join(quotes))
    builder.add(
        step_id="doc_quotes",
        rule_id="DOC-007",
        label="Matching passages",
        formula="copy passages containing the question terms",
        inputs={"question": needle[:120]},
        value=Decimal(len(quotes)),
        unit="units",
        source_refs=_source(attachment, table),
    )
    facts = [Fact("f_doc_quotes", "doc_quotes", "Matching passages", Decimal(len(quotes)), "units")]
    for index, figure in enumerate(figures, start=1):
        step_id = f"doc_fig_{index}"
        builder.add(
            step_id=step_id,
            rule_id="DOC-007",
            label=f"Printed figure {index}: {figure['printed']}",
            formula="read the figure printed in the passage; scale lakh or crore once",
            inputs={"printed": figure["printed"]},
            value=Decimal(figure["value"]),
            unit=figure["unit"],
            source_refs=_source(attachment, table),
            note=figure["context"][:240],
        )
        facts.append(
            Fact(
                f"f_doc_fig_{index}",
                step_id,
                f"Printed figure: {figure['printed']}",
                Decimal(figure["value"]),
                figure["unit"],
            )
        )
    builder.add(
        step_id="doc_figure_count",
        rule_id="DOC-007",
        label="Figures found",
        formula="count(unique printed figures in the quoted passages)",
        inputs={"passages": len(quotes)},
        value=Decimal(len(figures)),
        unit="units",
        source_refs=_source(attachment, table),
    )
    facts.append(
        Fact("f_doc_figures", "doc_figure_count", "Figures found", Decimal(len(figures)), "units")
    )
    verdict = (
        f"The attachment has {len(quotes)} matching passage(s); "
        f"{len(figures)} printed figure(s) are shown with the passage they came from."
    )
    result = CalcResult(
        value=Decimal(len(quotes)),
        unit="units",
        favorable=None,
        verdict=verdict,
        steps=builder.steps,
        facts=facts,
        rationale_template=(
            "I found {{f_doc_quotes}} matching passage(s) and {{f_doc_figures}} printed figure(s) "
            "in the attached document."
        ),
        meta={
            "attachment_id": attachment.get("attachment_id"),
            "attachment_name": attachment.get("name"),
            "operation": "ask",
            "table": table["name"] if table else None,
            "question": needle,
            "quotes": quotes,
            "figures": figures,
            "pages": attachment.get("pages"),
            "ocr": bool(attachment.get("ocr", False)),
        },
    )
    return _finish(result)


def _finish(result: CalcResult) -> CalcResult:
    result.rationale = render(result.rationale_template, result)
    report = validate_result(result)
    if not report.ok:
        raise DomainError("DOC_VALIDATION", "; ".join(report.failures))
    result.meta["validation"] = report.to_dict()
    return result


def run_attachment_analysis(
    attachment: dict[str, Any],
    *,
    operation: str,
    table: str | None = None,
    column: str | None = None,
    compare_column: str | None = None,
    query: str | None = None,
    limit: int = 5,
    direction: str = "top",
) -> CalcResult:
    """Analyse one extracted attachment. Raises DomainError with a next step."""
    operation = str(operation or "").strip().lower()
    if operation not in OPERATIONS:
        raise DomainError(
            "E_ATTACHMENT_OPERATION",
            f"{operation or 'This'} is not an attachment operation.",
            "Use profile, sum, mean, min, max, count, difference, top, search or ask.",
        )
    builder = StepBuilder()
    table_record = None
    if operation not in {"ask"}:
        table_record = _require_table(attachment, table)
    elif attachment.get("tables"):
        table_record = choose_table(attachment, table) or (attachment.get("tables") or [None])[0]

    if operation == "profile":
        return _profile(attachment, table_record, builder, limit)
    if operation in {"sum", "mean", "min", "max", "count"}:
        return _column_operation(attachment, table_record, builder, operation, column)
    if operation == "difference":
        return _difference(attachment, table_record, builder, column, compare_column)
    if operation == "top":
        return _top(attachment, table_record, builder, column, limit, direction)
    if operation == "search":
        return _search(attachment, table_record, builder, query, column, limit)
    return _ask(attachment, table_record, builder, query)
