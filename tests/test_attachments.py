"""Attachments: extraction, deterministic analysis, planning and the sealed run."""

from __future__ import annotations

import io
import zipfile
from decimal import Decimal

import pytest

from costpilot.data.documents import AttachmentError, extract, public_view
from costpilot.engine.documents import run_attachment_analysis
from costpilot.engine.errors import DomainError

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def budget_xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Budget"
    sheet.append(["Product", "Actual", "Budget", "Margin %"])
    sheet.append(["PRD-A", 120000, 115000, "12.5%"])
    sheet.append(["PRD-B", 127500, 130000, "15.0%"])
    sheet.append(["PRD-C", 17400, 10000, "12.25%"])
    notes = workbook.create_sheet("Notes")
    notes.append(["Note"])
    notes.append(["Overhead absorbed at the plant rate."])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def csv_bytes() -> bytes:
    return (
        "Product;Actual;Budget\n"
        "PRD-A;₹1,20,000;115000\n"
        "PRD-B;127500;130000\n"
        "PRD-C;17400;10000\n"
    ).encode()


def docx_bytes() -> bytes:
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Overhead was absorbed at 18.5% of direct labour.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Unused capacity cost Rs. 1,26,000 in September.</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Product</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Margin</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>PRD-A</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>1200</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def pdf_bytes() -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Plant overhead was 126000 and unused capacity 18000.")
    pdf.drawString(72, 700, "The plant ran at 88% of practical capacity.")
    pdf.save()
    return buffer.getvalue()


@pytest.fixture()
def workbook() -> dict:
    return extract(budget_xlsx(), "budget.xlsx")


@pytest.fixture()
def note() -> dict:
    return extract(docx_bytes(), "note.docx")


# ---------- extraction -------------------------------------------------


def test_xlsx_becomes_one_table_per_sheet(workbook):
    assert workbook["kind"] == "xlsx"
    assert workbook["sha256"]
    assert [table["name"] for table in workbook["tables"]] == ["Budget", "Notes"]
    budget = workbook["tables"][0]
    assert budget["headers"] == ["Product", "Actual", "Budget", "Margin %"]
    assert budget["row_count"] == 3
    view = public_view(workbook)
    assert view["attachment_id"] is None
    assert view["characters"] == len(workbook["text"])
    assert view["tables"][0]["rows"][0] == ["PRD-A", "120000", "115000", "12.5%"]


def test_csv_sniffs_semicolons_and_currency(monkeypatch):
    record = extract(csv_bytes(), "budget.csv")
    table = record["tables"][0]
    assert table["headers"] == ["Product", "Actual", "Budget"]
    assert table["rows"][0] == ["PRD-A", "₹1,20,000", "115000"]
    column = [row[1] for row in table["rows"]]
    assert column[0].startswith("₹")


def test_docx_reads_paragraphs_and_tables(note):
    assert note["kind"] == "docx"
    assert "18.5% of direct labour" in note["text"]
    assert note["tables"][0]["headers"] == ["Product", "Margin"]
    assert note["tables"][0]["rows"] == [["PRD-A", "1200"]]


def test_pdf_reads_the_text_layer():
    record = extract(pdf_bytes(), "overhead.pdf")
    assert record["kind"] == "pdf"
    assert record["pages"] == 1
    assert "unused capacity 18000" in record["text"]


def test_unsupported_empty_and_oversized_files_give_a_next_step():
    with pytest.raises(AttachmentError) as rejected:
        extract(b"PK\x03\x04", "notes.zip")
    assert rejected.value.code == "E_ATTACHMENT_FORMAT"
    assert rejected.value.remediation

    with pytest.raises(AttachmentError) as empty:
        extract(b"", "blank.xlsx")
    assert empty.value.code == "E_ATTACHMENT_EMPTY"

    with pytest.raises(AttachmentError) as huge:
        extract(b"x" * (25 * 1024 * 1024 + 1), "big.csv")
    assert huge.value.code == "E_ATTACHMENT_TOO_LARGE"


# ---------- deterministic engine ---------------------------------------


def _fact(result, fact_id: str) -> Decimal:
    return result.fact_map()[fact_id].value


def test_profile_reports_rows_and_numeric_columns(workbook):
    result = run_attachment_analysis(workbook, operation="profile", table="Budget")
    assert result.meta["row_count"] == 3
    assert result.meta["numeric_columns"] == ["Actual", "Budget", "Margin %"]
    assert result.meta["validation"]["failures"] == []


def test_column_operations(workbook):
    total = run_attachment_analysis(workbook, operation="sum", table="Budget", column="Actual")
    assert total.value == Decimal("264900")
    assert total.steps[0].rule_id == "DOC-002"

    mean = run_attachment_analysis(workbook, operation="mean", table="Budget", column="Margin %")
    assert mean.value == Decimal("13.25")

    low = run_attachment_analysis(workbook, operation="min", table="Budget", column="Budget")
    assert low.value == Decimal("10000")
    high = run_attachment_analysis(workbook, operation="max", table="Budget", column="Budget")
    assert high.value == Decimal("130000")

    counted = run_attachment_analysis(
        workbook, operation="count", table="Budget", column="Actual"
    )
    assert counted.value == Decimal("3")


def test_difference_subtracts_two_column_totals(workbook):
    result = run_attachment_analysis(
        workbook,
        operation="difference",
        table="Budget",
        column="Actual",
        compare_column="Budget",
    )
    assert result.value == Decimal("9900")
    assert _fact(result, "f_doc_left") == Decimal("264900")
    assert _fact(result, "f_doc_right") == Decimal("255000")


def test_top_and_bottom_rank_the_rows(workbook):
    top = run_attachment_analysis(
        workbook, operation="top", table="Budget", column="Actual", limit=2
    )
    assert top.meta["rows"][0]["Product"] == "PRD-B"
    assert len(top.meta["rows"]) == 2

    bottom = run_attachment_analysis(
        workbook, operation="top", table="Budget", column="Actual", limit=1, direction="bottom"
    )
    assert bottom.meta["rows"][0]["Product"] == "PRD-C"


def test_search_and_ask_ground_every_figure(note):
    found = run_attachment_analysis(note, operation="search", query="capacity")
    assert found.value >= 1
    assert "capacity" in " ".join(
        str(row.get("passage", "")) for row in found.meta["rows"]
    ).lower()

    asked = run_attachment_analysis(note, operation="ask", query="What was the unused capacity cost?")
    figures = asked.meta["figures"]
    printed = {figure["printed"] for figure in figures}
    assert "18.5%" in printed
    assert "Rs. 1,26,000" in printed
    assert any(figure["unit"] == "INR" and figure["value"] == "126000" for figure in figures)
    # Table markers and row labels are not reported as printed figures.
    assert "1" not in printed


def test_engine_errors_name_the_fix(workbook):
    with pytest.raises(DomainError) as table:
        run_attachment_analysis(workbook, operation="sum", table="Missing", column="Actual")
    assert table.value.code == "E_ATTACHMENT_TABLE"
    assert "Budget" in str(table.value.remediation)

    with pytest.raises(DomainError) as column:
        run_attachment_analysis(workbook, operation="sum", table="Budget", column="Profit")
    assert column.value.code == "E_ATTACHMENT_COLUMN"

    with pytest.raises(DomainError) as operation:
        run_attachment_analysis(workbook, operation="forecast")
    assert operation.value.code == "E_ATTACHMENT_OPERATION"

    with pytest.raises(DomainError) as query:
        run_attachment_analysis(workbook, operation="search", query="")
    assert query.value.code == "E_ATTACHMENT_QUERY"

    with pytest.raises(DomainError) as miss:
        run_attachment_analysis(workbook, operation="search", query="qqqqzz")
    assert miss.value.code == "E_ATTACHMENT_NO_MATCH"


# ---------- planning ---------------------------------------------------


def _store(service, user, raw: bytes, name: str) -> dict:
    return service.create_attachment(raw, name, user=user)


def test_planner_routes_a_document_question_to_the_engine(
    service, orchestrator, controller, session
):
    view = _store(service, controller, budget_xlsx(), "budget.xlsx")
    payload = orchestrator.post_message(
        controller,
        session,
        "What is the difference between total Actual and total Budget in the attached spreadsheet?",
        attachment_ids=[view["attachment_id"]],
    )
    plan = payload["plan"]
    assert plan["intent"] == "attachment_analysis"
    assert plan["tool"] == "run_attachment_analysis"
    assert payload["status"] == "ready"
    assert plan["arguments"]["table"] == "Budget"
    assert plan["arguments"]["column"] == "Actual"
    assert plan["arguments"]["compare_column"] == "Budget"
    sheet = {row["key"]: row for row in payload["parameter_sheet"]}
    assert sheet["attachment_id"]["value"] == "budget.xlsx"


def test_top_plan_does_not_carry_an_unused_compare_column(
    service, orchestrator, controller, session
):
    view = _store(service, controller, budget_xlsx(), "budget.xlsx")
    payload = orchestrator.post_message(
        controller,
        session,
        "Show the top 2 rows by Actual in the Budget sheet of the attached file.",
        attachment_ids=[view["attachment_id"]],
    )
    arguments = payload["plan"]["arguments"]
    assert arguments["operation"] == "top"
    assert arguments["limit"] == 2
    assert "compare_column" not in arguments


def test_two_attachments_without_a_name_asks_which_one(
    service, orchestrator, controller, session
):
    first = _store(service, controller, budget_xlsx(), "budget.xlsx")
    second = _store(service, controller, csv_bytes(), "costs.csv")
    payload = orchestrator.post_message(
        controller,
        session,
        "Summarise the attached file.",
        attachment_ids=[first["attachment_id"], second["attachment_id"]],
    )
    assert payload["status"] == "needs_clarification"
    options = " ".join(payload["clarification"]["options"])
    assert "budget.xlsx" in options and "costs.csv" in options


def test_a_document_reference_without_a_file_asks_for_one(
    orchestrator, controller, session
):
    payload = orchestrator.post_message(controller, session, "Summarise the attached file.")
    assert payload["status"] == "needs_clarification"
    assert "attach" in payload["clarification"]["question"].lower()


def test_dataset_questions_still_use_the_dataset(
    service, orchestrator, controller, session
):
    view = _store(service, controller, budget_xlsx(), "budget.xlsx")
    payload = orchestrator.post_message(
        controller,
        session,
        "Variance for PRD-B at PLT-01 in 2026-09",
        attachment_ids=[view["attachment_id"]],
    )
    assert payload["plan"]["intent"] == "variance_analysis"
    assert payload["plan"]["arguments"]["product_id"] == "PRD-B"


# ---------- API and sealed run -----------------------------------------


def _new_session(api, headers) -> str:
    response = api.post("/v1/sessions", json={}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["session"]["session_id"]


def _upload(api, headers, raw: bytes, name: str) -> dict:
    response = api.post(
        "/v1/attachments",
        files={"file": (name, raw, "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_plan_confirm_trace_and_reperform(api, auth):
    headers = auth()
    session_id = _new_session(api, headers)
    view = _upload(api, headers, budget_xlsx(), "budget.xlsx")
    assert view["attachment_id"].startswith("att_")
    assert view["kind"] == "xlsx"
    assert view["tables"][0]["row_count"] == 3

    planned = api.post(
        f"/v1/sessions/{session_id}/messages",
        json={
            "text": "Difference between Actual and Budget totals in the attached spreadsheet.",
            "attachment_ids": [view["attachment_id"]],
        },
        headers=headers,
    )
    assert planned.status_code == 200, planned.text
    body = planned.json()
    assert body["plan"]["intent"] == "attachment_analysis"
    assert body["attachments"][0]["attachment_id"] == view["attachment_id"]

    confirmed = api.post(
        f"/v1/plans/{body['plan_id']}/confirm",
        json={"plan_version": 1},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    run_id = confirmed.json()["run_id"]

    card = api.get(f"/v1/runs/{run_id}", headers=headers).json()
    facts = {fact["label"]: fact["value"] for fact in card["facts"]}
    assert facts["Actual minus Budget"] == "9900"
    assert card["header"]["attachment_name"] == "budget.xlsx"
    assert card["audit"]["trace"] == f"/v1/runs/{run_id}/trace"

    trace = api.get(f"/v1/runs/{run_id}/trace", headers=headers).json()
    assert [step["rule_id"] for step in trace["steps"]][-1] == "DOC-005"

    reperform = api.post(f"/v1/runs/{run_id}/reperform", headers=headers).json()
    assert reperform["status"] == "exact"

    thread = api.get(f"/v1/sessions/{session_id}", headers=headers).json()
    user_message = thread["messages"][0]
    assert user_message["attachments"][0]["name"] == "budget.xlsx"


def test_attachment_errors_are_typed_and_actionable(api, auth):
    headers = auth()
    bad = api.post(
        "/v1/attachments",
        files={"file": ("notes.zip", b"PK\x03\x04", "application/zip")},
        headers=headers,
    )
    assert bad.status_code == 422
    body = bad.json()
    assert body["error"]["code"] == "E_ATTACHMENT_FORMAT"
    assert body["remediation"]
    assert body["actions"]

    missing = api.post(
        f"/v1/sessions/{_new_session(api, headers)}/messages",
        json={"text": "Summarise the attached file.", "attachment_ids": ["att_missing"]},
        headers=headers,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "E_ATTACHMENT_NOT_FOUND"


def test_attachments_are_private_to_the_uploader(api, auth):
    owner = _upload(api, auth("analyst"), budget_xlsx(), "budget.xlsx")
    other = api.get(f"/v1/attachments/{owner['attachment_id']}", headers=auth("viewer"))
    assert other.status_code == 403
    assert other.json()["error"]["code"] == "E_AUTH_SCOPE"

    admin = api.get(f"/v1/attachments/{owner['attachment_id']}", headers=auth("admin"))
    assert admin.status_code == 200
    assert admin.json()["name"] == "budget.xlsx"
