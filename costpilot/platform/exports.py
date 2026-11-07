"""Working-paper exports: XLSX, PDF and JSON copies of a sealed run.

The export never recalculates. It formats the validated card and trace, protects
every imported string against spreadsheet formula injection, and stamps the run
id, snapshot id, engine version and rulebook version on every sheet or page.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

MONEY_FORMAT = '#,##0.00;[Red](#,##0.00)'
QTY_FORMAT = "#,##0.00"
RATIO_FORMAT = "0.00%"
INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14, color="1F3A5F")


class ExportError(Exception):
    """Raised when an export cannot be produced."""


def safe_text(value: Any) -> Any:
    """Neutralise spreadsheet formula injection on every imported string."""
    if not isinstance(value, str):
        return value
    if value[:1] in INJECTION_PREFIXES:
        return "'" + value
    return value


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float | Decimal):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _cell_value(value: Any, kind: str) -> tuple[Any, str | None]:
    if kind == "bool":
        return (str(value).lower() in {"true", "1", "yes"}, None)
    if kind in {"money", "qty", "number", "ratio"}:
        number = _number(value)
        if number is not None:
            fmt = {
                "money": MONEY_FORMAT,
                "qty": QTY_FORMAT,
                "number": QTY_FORMAT,
                "ratio": RATIO_FORMAT,
            }[kind]
            return (float(number), fmt)
    return (safe_text(value), None)


def _pdf_text(value: Any) -> str:
    text = str(value) if value is not None else ""
    return (
        text.replace("\u20b9", "INR ")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def _decorate_header(sheet: Any, row: int, width: int) -> None:
    for column in range(1, width + 1):
        cell = sheet.cell(row=row, column=column)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    sheet.freeze_panes = sheet.cell(row=row + 1, column=1)


def _widths(sheet: Any, widths: dict[int, int]) -> None:
    for column, width in widths.items():
        sheet.column_dimensions[get_column_letter(column)].width = width


def _flatten_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    result = run.get("result", {}) or {}
    steps = list(result.get("steps") or [])
    for name, component in (result.get("components") or {}).items():
        for step in component.get("steps", []) or []:
            steps.append({**step, "component": name})
    return steps


def _all_facts(run: dict[str, Any]) -> list[dict[str, Any]]:
    result = run.get("result", {}) or {}
    facts = list(result.get("facts") or [])
    for component in (result.get("components") or {}).values():
        facts.extend(component.get("facts", []) or [])
    return facts


def export_json(payload: dict[str, Any]) -> bytes:
    document = {
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "format": "costpilot-working-paper-json-1",
        "run": payload.get("run"),
        "card": payload.get("card"),
        "pins": payload.get("pins"),
        "validation": payload.get("validation"),
    }
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def export_xlsx(payload: dict[str, Any]) -> bytes:
    card = payload.get("card") or {}
    run = payload.get("run") or {}
    pins = payload.get("pins") or {}
    quality = payload.get("quality") or {}
    workbook = Workbook()

    summary = workbook.active
    summary.title = "Summary"
    summary["A1"] = "CostPilot working paper"
    summary["A1"].font = TITLE_FONT
    summary["A2"] = _pdf_text(card.get("header", {}).get("question") or run.get("question") or "")
    summary["A2"].font = Font(italic=True, size=11)
    rows = [
        ("Run id", run.get("run_id") or card.get("run_id")),
        ("Snapshot id", pins.get("snapshot_id")),
        ("Manifest hash", pins.get("manifest_hash")),
        ("Engine version", pins.get("engine_version")),
        ("Rulebook version", pins.get("rulebook_version")),
        ("Registry version", pins.get("registry_version")),
        ("Generated", datetime.now(UTC).isoformat(timespec="seconds")),
        ("Status", "Sealed"),
        ("Verdict", card.get("verdict", {}).get("sentence")),
    ]
    header = card.get("header", {}) or {}
    for key in ("period_id", "plant_id", "product_id", "division_id", "basis"):
        if header.get(key):
            rows.append((key.replace("_id", "").replace("_", " ").title(), header[key]))
    for index, (label, value) in enumerate(rows, start=4):
        summary.cell(row=index, column=1, value=safe_text(label)).font = Font(bold=True)
        summary.cell(row=index, column=2, value=safe_text(value))
    fact_start = 4 + len(rows) + 2
    summary.cell(row=fact_start, column=1, value="Headline facts").font = Font(bold=True, size=12)
    summary.cell(row=fact_start + 1, column=1, value="Fact")
    summary.cell(row=fact_start + 1, column=2, value="Value")
    summary.cell(row=fact_start + 1, column=3, value="Unit")
    _decorate_header(summary, fact_start + 1, 3)
    for offset, fact in enumerate(card.get("facts", []), start=fact_start + 2):
        summary.cell(row=offset, column=1, value=safe_text(fact.get("label")))
        value, fmt = _cell_value(fact.get("value"), "money" if fact.get("unit") == "INR" else "number")
        summary.cell(row=offset, column=2, value=value)
        if fmt:
            summary.cell(row=offset, column=2).number_format = fmt
        summary.cell(row=offset, column=3, value=safe_text(fact.get("unit")))
    _widths(summary, {1: 42, 2: 30, 3: 16})

    analysis = workbook.create_sheet("Analysis")
    analysis["A1"] = "Analysis tables"
    analysis["A1"].font = TITLE_FONT
    row = 3
    for table in card.get("tables", []):
        analysis.cell(row=row, column=1, value=safe_text(table.get("title"))).font = Font(bold=True, size=12)
        row += 1
        columns = table.get("columns", [])
        for column_index, column in enumerate(columns, start=1):
            analysis.cell(row=row, column=column_index, value=safe_text(column.get("label")))
        _decorate_header(analysis, row, max(1, len(columns)))
        row += 1
        for item in table.get("rows", []):
            for column_index, column in enumerate(columns, start=1):
                value, fmt = _cell_value(item.get(column["key"]), column.get("type", "text"))
                analysis.cell(row=row, column=column_index, value=value)
                if fmt:
                    analysis.cell(row=row, column=column_index).number_format = fmt
            row += 1
        row += 1
    if row == 3:
        analysis["A3"] = "No analysis table was returned."
    _widths(analysis, {index: 20 for index in range(1, 10)})

    trace = workbook.create_sheet("Trace")
    trace["A1"] = "Calculation trace"
    trace["A1"].font = TITLE_FONT
    headers = ["Step", "Component", "Rule", "Rule version", "Label", "Formula", "Value", "Unit", "Source refs"]
    for column_index, label in enumerate(headers, start=1):
        trace.cell(row=3, column=column_index, value=label)
    _decorate_header(trace, 3, len(headers))
    for row_index, step in enumerate(_flatten_steps(run), start=4):
        values = [
            step.get("step_id"),
            step.get("component", ""),
            step.get("rule_id"),
            step.get("rule_version"),
            step.get("label"),
            step.get("formula"),
            step.get("value"),
            step.get("unit"),
            ", ".join(step.get("source_refs") or []),
        ]
        for column_index, value in enumerate(values, start=1):
            cell_value = safe_text(value)
            if column_index == 7:
                number = _number(value)
                if number is not None:
                    cell_value = float(number)
                    trace.cell(row=row_index, column=column_index).number_format = MONEY_FORMAT
            trace.cell(row=row_index, column=column_index, value=cell_value)
    _widths(trace, {1: 8, 2: 16, 3: 14, 4: 12, 5: 34, 6: 42, 7: 16, 8: 10, 9: 44})

    assumptions = workbook.create_sheet("Assumptions")
    assumptions["A1"] = "Assumptions"
    assumptions["A1"].font = TITLE_FONT
    headers = ["Id", "Key", "Text", "Source", "Confirmed", "Evidence", "Confidence"]
    for column_index, label in enumerate(headers, start=1):
        assumptions.cell(row=3, column=column_index, value=label)
    _decorate_header(assumptions, 3, len(headers))
    items = card.get("assumptions") or run.get("result", {}).get("assumptions") or []
    for row_index, item in enumerate(items, start=4):
        values = [
            item.get("id"), item.get("key"), item.get("text"), item.get("source"),
            item.get("confirmed"), item.get("evidence"), item.get("confidence"),
        ]
        for column_index, value in enumerate(values, start=1):
            assumptions.cell(row=row_index, column=column_index, value=safe_text(value) if not isinstance(value, bool) else value)
    _widths(assumptions, {1: 10, 2: 20, 3: 70, 4: 18, 5: 12, 6: 36, 7: 12})

    sources = workbook.create_sheet("Sources")
    sources["A1"] = "Sources cited by the calculation"
    sources["A1"].font = TITLE_FONT
    headers = ["Step", "Rule", "Source reference"]
    for column_index, label in enumerate(headers, start=1):
        sources.cell(row=3, column=column_index, value=label)
    _decorate_header(sources, 3, len(headers))
    seen: set[tuple[str, str, str]] = set()
    row_index = 4
    for step in _flatten_steps(run):
        for ref in step.get("source_refs") or []:
            key = (str(step.get("step_id")), str(step.get("rule_id")), str(ref))
            if key in seen:
                continue
            seen.add(key)
            for column_index, value in enumerate(key, start=1):
                sources.cell(row=row_index, column=column_index, value=safe_text(value))
            row_index += 1
    _widths(sources, {1: 10, 2: 16, 3: 64})

    quality_sheet = workbook.create_sheet("Data Quality")
    quality_sheet["A1"] = "Data quality at run time"
    quality_sheet["A1"].font = TITLE_FONT
    headers = ["Gate", "Title", "Severity", "Status", "Detail", "Remediation"]
    for column_index, label in enumerate(headers, start=1):
        quality_sheet.cell(row=3, column=column_index, value=label)
    _decorate_header(quality_sheet, 3, len(headers))
    gates = quality.get("gates") or pins.get("gates") or []
    for row_index, gate in enumerate(gates, start=4):
        values = [
            gate.get("id"), gate.get("title"), gate.get("severity"), gate.get("status"),
            gate.get("detail"), gate.get("remediation"),
        ]
        for column_index, value in enumerate(values, start=1):
            quality_sheet.cell(row=row_index, column=column_index, value=safe_text(value))
    _widths(quality_sheet, {1: 8, 2: 40, 3: 12, 4: 12, 5: 60, 6: 56})

    footer = (
        f"Run {run.get('run_id')} | Snapshot {pins.get('snapshot_id')} | "
        f"Engine {pins.get('engine_version')} | Rulebook {pins.get('rulebook_version')} | Sealed"
    )
    for sheet in workbook.worksheets:
        last = sheet.max_row + 2
        sheet.cell(row=last, column=1, value=footer).font = Font(size=8, color="666666")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_pdf(payload: dict[str, Any]) -> bytes:
    card = payload.get("card") or {}
    run = payload.get("run") or {}
    pins = payload.get("pins") or {}
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title="CostPilot working paper",
        author="CostPilot",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("CPTitle", parent=styles["Title"], fontSize=17, textColor=colors.HexColor("#1F3A5F"))
    heading = ParagraphStyle("CPHeading", parent=styles["Heading2"], fontSize=12, textColor=colors.HexColor("#1F3A5F"), spaceBefore=10)
    body = ParagraphStyle("CPBody", parent=styles["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("CPSmall", parent=body, fontSize=7.5, textColor=colors.HexColor("#555555"))
    story: list[Any] = [Paragraph(_pdf_text(card.get("header", {}).get("question") or run.get("question") or "Working paper"), title)]
    meta = [
        ["Run", run.get("run_id") or card.get("run_id"), "Snapshot", pins.get("snapshot_id")],
        ["Engine", pins.get("engine_version"), "Rulebook", pins.get("rulebook_version")],
        ["Period", card.get("header", {}).get("period_id"), "Plant", card.get("header", {}).get("plant_id")],
        ["Product", card.get("header", {}).get("product_id"), "Basis", card.get("header", {}).get("basis") or "-"],
    ]
    table = Table(meta, colWidths=[22 * mm, 60 * mm, 22 * mm, 60 * mm])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#555555")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(table)
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("Verdict", heading))
    story.append(Paragraph(_pdf_text(card.get("verdict", {}).get("sentence")), body))
    rationale = (card.get("rationale") or {}).get("text")
    if rationale:
        story.append(Paragraph("Rationale", heading))
        story.append(Paragraph(_pdf_text(rationale), body))

    facts = card.get("facts") or []
    if facts:
        story.append(Paragraph("Headline facts", heading))
        data = [["Fact", "Value", "Unit"]]
        for fact in facts[:8]:
            data.append([_pdf_text(fact.get("label")), _pdf_text(fact.get("value")), _pdf_text(fact.get("unit"))])
        fact_table = Table(data, colWidths=[100 * mm, 45 * mm, 25 * mm], repeatRows=1)
        fact_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ALIGN", (1, 1), (1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6F9")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(fact_table)

    for table_payload in (card.get("tables") or [])[:4]:
        rows = table_payload.get("rows") or []
        columns = table_payload.get("columns") or []
        if not rows or not columns:
            continue
        story.append(Paragraph(_pdf_text(table_payload.get("title")), heading))
        data = [[_pdf_text(column.get("label")) for column in columns]]
        for item in rows[:18]:
            data.append([_pdf_text(item.get(column.get("key"))) for column in columns])
        width = 178 * mm / max(1, len(columns))
        grid = Table(data, colWidths=[width] * len(columns), repeatRows=1)
        grid.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7.2),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(grid)

    assumptions = card.get("assumptions") or []
    if assumptions:
        story.append(Paragraph("Assumptions", heading))
        for item in assumptions[:12]:
            flag = "confirmed" if item.get("confirmed") else "needs confirmation"
            story.append(Paragraph(f"- {_pdf_text(item.get('text'))} ({_pdf_text(item.get('source'))}, {flag})", small))

    conditions = card.get("conditions") or []
    if conditions:
        story.append(Paragraph("Conditions", heading))
        for item in conditions[:8]:
            story.append(Paragraph(f"- {_pdf_text(item)}", small))

    validation = payload.get("validation") or {}
    story.append(Paragraph("Validation", heading))
    story.append(Paragraph(_pdf_text(json.dumps(validation, default=str)[:1200]), small))
    story.append(Spacer(1, 4 * mm))
    story.append(
        Paragraph(
            _pdf_text(
                f"Run {run.get('run_id')} | Snapshot {pins.get('snapshot_id')} | "
                f"Engine {pins.get('engine_version')} | Rulebook {pins.get('rulebook_version')} | Sealed"
            ),
            small,
        )
    )

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(16 * mm, 10 * mm, f"CostPilot | {run.get('run_id')} | Sealed")
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def export_working_paper(payload: dict[str, Any], fmt: str) -> tuple[bytes, str, str]:
    fmt = (fmt or "json").lower()
    run_id = (payload.get("run") or {}).get("run_id") or "run"
    try:
        if fmt == "xlsx":
            return export_xlsx(payload), f"costpilot_{run_id}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if fmt == "pdf":
            return export_pdf(payload), f"costpilot_{run_id}.pdf", "application/pdf"
        if fmt == "json":
            return export_json(payload), f"costpilot_{run_id}.json", "application/json"
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise ExportError(f"Export generation failed: {exc}") from exc
    raise ExportError("Unsupported export format. Use xlsx, pdf, or json.")
