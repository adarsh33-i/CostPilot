"""Financial statements: parsing, the deterministic engine, planning and cards."""

from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from costpilot.data.documents import extract
from costpilot.data.statements import StatementError, parse_statements
from costpilot.engine.errors import DomainError
from costpilot.engine.statements import run_financial_statements
from costpilot.platform import llm as llm_module
from costpilot.platform.llm import LLMClient
from costpilot.platform.planner import plan_message

INCOME_ROWS = (
    ("Revenue", 100000000, 90000000),
    ("Cost of sales", 60000000, 55000000),
    ("Gross profit", 40000000, 35000000),
    ("Operating expenses", 20000000, 18000000),
    ("Depreciation", 5000000, 4500000),
    ("Finance cost", 2000000, 1800000),
    ("Profit before tax", 13000000, 10700000),
    ("Tax expense", 3000000, 2500000),
    ("Net profit", 10000000, 8200000),
)
BALANCE_ROWS = (
    ("Property, plant and equipment", 30000000, 28000000),
    ("Inventories", 10000000, 9000000),
    ("Trade receivables", 15000000, 13000000),
    ("Cash and cash equivalents", 5000000, 4000000),
    ("Total current assets", 30000000, 26000000),
    ("Share capital", 10000000, 10000000),
    ("Reserves and surplus", 20000000, 16000000),
    ("Total equity", 30000000, 26000000),
    ("Long-term borrowings", 12000000, 12000000),
    ("Other non-current liabilities", 5000000, 4000000),
    ("Trade payables", 8000000, 7000000),
    ("Short-term borrowings", 5000000, 5000000),
    ("Total current liabilities", 13000000, 12000000),
    ("Total liabilities", 30000000, 28000000),
    ("Total liabilities and equity", 60000000, 54000000),
)
CASH_ROWS = (
    ("Net cash from operating activities", 12000000, 11000000),
    ("Net cash from investing activities", -3000000, -2500000),
    ("Net cash from financing activities", -8000000, -7500000),
    ("Net increase in cash", 1000000, 1000000),
    ("Cash at beginning of the year", 4000000, 3000000),
    ("Cash at end of the year", 5000000, 4000000),
)


def statement_xlsx(
    *, total_assets: bool = False, cashflow: bool = True, lakh: bool = False
) -> bytes:
    workbook = Workbook()
    income = workbook.active
    income.title = "Income"
    income.append(["Statement of Profit and Loss"])
    income.append(["Particulars", "FY2025 (₹ lakh)" if lakh else "FY2025", "FY2024"])
    factor = 100000 if lakh else 1
    for label, current, prior in INCOME_ROWS:
        income.append([label, current / factor, prior / factor])

    balance = workbook.create_sheet("Balance")
    balance.append(["Balance Sheet"])
    balance.append(["Particulars", "FY2025", "FY2024"])
    for label, current, prior in BALANCE_ROWS:
        balance.append([label, current, prior])
    if total_assets:
        balance.append(["Total assets", 60000000, 54000000])

    if cashflow:
        cash = workbook.create_sheet("Cash Flow")
        cash.append(["Cash Flow Statement"])
        cash.append(["Particulars", "FY2025", "FY2024"])
        for label, current, prior in CASH_ROWS:
            cash.append([label, current, prior])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def budget_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Budget"
    sheet.append(["Product", "Actual", "Budget"])
    sheet.append(["PRD-A", 120000, 115000])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
def statements() -> dict:
    return extract(statement_xlsx(), "statements.xlsx")


def ratios(result) -> dict[str, dict]:
    return {row["name"]: row for row in result.meta["ratios"]}


def lines(result, group: str) -> dict[str, dict]:
    return {row["line"]: row for row in result.meta["statements"][group]}


# ---------- Parsing ------------------------------------------------------


def test_parser_reads_periods_prior_and_the_stated_scale(statements):
    parsed = parse_statements(statements)
    assert parsed.current_period == "FY2025"
    assert parsed.prior_period == "FY2024"
    assert {"FY2025", "FY2024"} <= set(parsed.periods)
    assert Decimal(parsed.values["revenue"]) == Decimal("100000000")
    assert Decimal(parsed.prior["revenue"]) == Decimal("90000000")
    assert set(parsed.available) >= {"income", "balance", "cashflow"}
    assert parsed.warnings == []


def test_parser_scales_a_lakh_header_once():
    parsed = parse_statements(extract(statement_xlsx(lakh=True), "lakh.xlsx"))
    assert Decimal(parsed.values["revenue"]) == Decimal("100000000")
    assert Decimal(parsed.values["net_income"]) == Decimal("10000000")
    assert parsed.scales


def test_parser_rejects_a_period_the_file_does_not_hold(statements):
    with pytest.raises(StatementError) as exc:
        parse_statements(statements, period="FY2023")
    assert exc.value.code == "E_STATEMENT_PERIOD"
    assert "FY2025" in exc.value.remediation


# ---------- Modelled statements -----------------------------------------


def test_model_builds_balanced_statements_from_turnover():
    result = run_financial_statements(turnover="12000000", period="FY2026")
    assert result.meta["mode"] == "model"
    assert result.value == Decimal("846000.000000")
    assert result.favorable is True

    income = lines(result, "income")
    assert Decimal(income["revenue"]["value"]) == Decimal("12000000")
    assert Decimal(income["net_income"]["value"]) == Decimal("846000.000000")

    balance = lines(result, "balance")
    assert balance["total_assets"]["rule"] == "FIN-004"
    assert result.meta["balance_check"]["ok"] is True

    cash = lines(result, "cashflow")
    assert Decimal(cash["closing_cash"]["value"]) == Decimal(
        cash["opening_cash"]["value"]
    ) + Decimal(cash["net_change_cash"]["value"])

    values = ratios(result)
    assert values["Gross margin"]["display"] == "40.00%"
    assert values["Net margin"]["value"] == "7.05"
    assert values["Current ratio"]["display"] == "1.07x"
    assert values["Debt-to-equity"]["display"] == "1.17x"
    assert values["Interest cover"]["display"] == "6.88x"
    assert values["Asset turnover"]["display"] == "2.13x"
    assert len(values) == 20
    assert result.meta["observations"]

    validation = result.meta["validation"]
    assert validation["failures"] == []
    assert validation["identities"] == "pass"
    assert all(step.rule_id.startswith("FIN-") for step in result.steps)


def test_model_uses_prior_turnover_for_growth():
    result = run_financial_statements(turnover="12000000", prior_turnover="10000000")
    growth = ratios(result)["Revenue growth"]
    assert growth["value"] == "20"
    assert growth["display"] == "20.00%"


def test_model_honours_stated_assumptions():
    result = run_financial_statements(
        turnover="12000000",
        assumptions={"gross_margin": "0.45", "inventory_days": "45"},
        assumption_sources={"gross_margin": "user_explicit", "inventory_days": "user_explicit"},
    )
    values = ratios(result)
    assert values["Gross margin"]["value"] == "45"
    assert values["Inventory days"]["value"] == "45"
    assert result.meta["balance_check"]["ok"] is True
    sources = {item.key: item.source for item in result.assumptions}
    assert sources["gross_margin"] == "user_explicit"


# ---------- Reported statements -----------------------------------------


def test_reported_statements_derive_totals_and_tie_without_conflicts():
    attachment = extract(statement_xlsx(), "statements.xlsx")
    result = run_financial_statements(attachment, mode="reported")
    assert result.meta["mode"] == "reported"
    assert result.meta["period"] == "FY2025"
    assert result.meta["prior_period"] == "FY2024"
    assert result.warnings == []
    assert result.meta["balance_check"]["ok"] is True

    balance = lines(result, "balance")
    assert balance["total_assets"]["rule"] == "FIN-004"
    assert balance["total_assets"]["derived"] is True
    assert Decimal(balance["total_assets"]["value"]) == Decimal("60000000")
    assert balance["total_assets"]["prior"] == "54000000"

    cash = lines(result, "cashflow")
    assert Decimal(cash["net_change_cash"]["value"]) == Decimal("1000000")
    assert len(ratios(result)) == 25
    assert any("prior period" in note for note in result.meta["observations"])


def test_reported_statements_without_cash_flow_are_not_invented():
    attachment = extract(statement_xlsx(cashflow=False), "no_cashflow.xlsx")
    result = run_financial_statements(attachment, mode="reported")
    assert result.meta["statements"]["cashflow"] == []
    assert "FIN_NO_CASHFLOW" in {warning.code for warning in result.warnings}


def test_auto_prefers_the_reported_file_over_turnover():
    attachment = extract(statement_xlsx(), "statements.xlsx")
    result = run_financial_statements(attachment, turnover="12000000", mode="auto")
    assert result.meta["mode"] == "reported"
    assert Decimal(lines(result, "income")["revenue"]["value"]) == Decimal("100000000")
    assert "FIN_TURNOVER_IGNORED" in {warning.code for warning in result.warnings}


def test_auto_models_when_the_file_holds_no_statements():
    attachment = extract(budget_xlsx(), "budget.xlsx")
    result = run_financial_statements(attachment, turnover="12000000", mode="auto")
    assert result.meta["mode"] == "model"
    assert Decimal(lines(result, "income")["revenue"]["value"]) == Decimal("12000000")


def test_engine_rejects_bad_statement_requests(statements):
    cases = [
        ({"turnover": "12000000", "currency": "JPY"}, "E_STATEMENT_CURRENCY"),
        ({"turnover": "12000000", "mode": "guess"}, "E_STATEMENT_MODE"),
        ({"turnover": "0"}, "E_STATEMENT_TURNOVER"),
        ({"turnover": "not a number"}, "E_STATEMENT_TURNOVER"),
        ({}, "E_STATEMENT_SOURCE"),
        ({"mode": "reported"}, "E_STATEMENT_SOURCE"),
    ]
    for kwargs, code in cases:
        with pytest.raises(DomainError) as exc:
            run_financial_statements(**kwargs)
        assert exc.value.code == code, kwargs

    with pytest.raises(DomainError) as exc:
        run_financial_statements(statements, period="FY2023", mode="reported")
    assert exc.value.code == "E_STATEMENT_PERIOD"


# ---------- Planning -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "turnover", "currency"),
    [
        (
            "Prepare the balance sheet, income statement, cash flow and ratio analysis "
            "on turnover of 12 crore",
            "120000000",
            "INR",
        ),
        ("Balance sheet and cash flow for turnover 12 lakh", "1200000", "INR"),
        ("Prepare financial statements on turnover of $2.5 million", "2500000.0", "USD"),
        ("Build statements on turnover 12,00,00,000", "120000000", "INR"),
    ],
)
def test_planner_routes_turnover_phrasing_to_the_statements_engine(
    snapshot, text, turnover, currency
):
    plan = plan_message(snapshot, text)
    assert plan["intent"] == "financial_statements"
    assert plan["tool"] == "run_financial_statements"
    assert plan["status"] == "ready"
    assert Decimal(plan["arguments"]["turnover"]) == Decimal(turnover)
    assert plan["arguments"]["currency"] == currency
    assert plan["arguments"]["mode"] == "model"


def test_planner_reads_assumptions_period_and_prior_turnover(snapshot):
    plan = plan_message(
        snapshot,
        "Prepare statements for FY2025 on turnover 12 crore with prior year turnover 10 crore, "
        "gross margin 45% and inventory days 45",
    )
    arguments = plan["arguments"]
    assert arguments["period"] == "FY2025"
    assert Decimal(arguments["prior_turnover"]) == Decimal("100000000")
    assert arguments["assumptions"] == {"gross_margin": "0.45", "inventory_days": "45"}
    assert arguments["assumption_sources"] == {
        "gross_margin": "user_explicit",
        "inventory_days": "user_explicit",
    }


def test_planner_asks_for_a_source_when_none_is_given(snapshot):
    plan = plan_message(snapshot, "Prepare the financial statements")
    assert plan["status"] == "needs_clarification"
    assert "turnover" in plan["clarification"]["question"].lower()
    options = " ".join(plan["clarification"]["options"])
    assert "Attach" in options and "Turnover 12 crore" in options


def test_planner_keeps_a_statement_attachment_and_uses_reported_mode(
    service, orchestrator, controller, session
):
    view = service.create_attachment(statement_xlsx(), "statements.xlsx", user=controller)
    payload = orchestrator.post_message(
        controller, session, "Prepare the financial statements", attachment_ids=[view["attachment_id"]]
    )
    arguments = payload["plan"]["arguments"]
    assert payload["status"] == "ready"
    assert arguments["attachment_id"] == view["attachment_id"]
    assert arguments["mode"] == "reported"

    mismatch = orchestrator.post_message(
        controller,
        session,
        "Prepare the financial statements for FY2023",
        attachment_ids=[view["attachment_id"]],
    )
    assert mismatch["status"] == "needs_clarification"
    options = " ".join(mismatch["clarification"]["options"])
    assert "FY2025" in options and "FY2024" in options


def test_planner_models_a_statementless_attachment_and_states_why(
    service, orchestrator, controller, session
):
    view = service.create_attachment(budget_xlsx(), "budget.xlsx", user=controller)
    payload = orchestrator.post_message(
        controller,
        session,
        "Prepare financial statements on turnover 3 crore",
        attachment_ids=[view["attachment_id"]],
    )
    assert payload["status"] == "ready"
    assert payload["plan"]["arguments"]["mode"] == "auto"
    mode = next(row for row in payload["plan"]["assumptions"] if row["key"] == "mode")
    assert mode["source"] == "engine_default"
    assert "no recognisable statements" in mode["text"]

    card = orchestrator.confirm_plan(controller, payload["plan_id"])["card"]
    assert card["header"]["statement_mode"] == "model"


def test_planner_accepts_a_previous_user_turnover(snapshot):
    hints = {
        "intent": "financial_statements",
        "confidence": "0.9",
        "slots": {
            "turnover": "120000000",
            "currency": "INR",
            "statement_period": "FY2025",
            "assumptions": {"gross_margin": 45, "inventory_days": 45, "unknown": 10},
        },
        "planner": "llm:fake:8b",
    }
    plan = plan_message(snapshot, "Use the same turnover and prepare the statements", hints=hints)
    assert plan["intent"] == "financial_statements"
    assert plan["arguments"]["turnover"] == "120000000"
    assert plan["arguments"]["period"] == "FY2025"
    assert plan["arguments"]["assumptions"] == {"gross_margin": "0.45", "inventory_days": "45"}


def test_planner_routes_fy_statements_without_the_word_financial(snapshot):
    plan = plan_message(snapshot, "Turnover 12 crore, FY2024 statements")
    assert plan["intent"] == "financial_statements"
    assert plan["arguments"]["period"] == "FY2024"


def test_llm_alias_and_slots_map_to_statements(monkeypatch):
    raw = {
        "intent": "balance_sheet",
        "confidence": 0.91,
        "slots": {
            "turnover": "120000000",
            "currency": "usd",
            "statement_period": "2025",
            "assumptions": {"gross_margin": 45, "inventory_days": 45, "made_up": 1},
        },
        "reason": "balance sheet request",
    }
    monkeypatch.setattr(
        llm_module, "_post", lambda *args, **kwargs: {"message": {"content": json.dumps(raw)}}
    )
    hints = LLMClient().interpret("prepare the balance sheet", {}, model="fake:8b")
    assert hints["intent"] == "financial_statements"
    assert hints["slots"]["turnover"] == "120000000"
    assert hints["slots"]["currency"] == "USD"
    assert hints["slots"]["statement_period"] == "FY2025"
    assert hints["slots"]["assumptions"] == {"gross_margin": "45", "inventory_days": "45"}


# ---------- Orchestration, cards and the API ----------------------------


def test_statements_plan_confirm_card_trace_and_reperform(
    orchestrator, controller, session
):
    payload = orchestrator.post_message(
        controller,
        session,
        "Prepare the balance sheet, income statement, cash flow and ratio analysis "
        "on turnover of 12 crore",
    )
    assert payload["status"] == "ready"
    sheet = {row["key"]: row for row in payload["parameter_sheet"]}
    assert sheet["turnover"]["value"] == "120,000,000.00"
    assert sheet["turnover"]["source"] == "user_explicit"
    assert sheet["mode"]["value"] == "Modelled from the stated turnover"
    assert sheet["period"]["value"] == "FY2026"

    result = orchestrator.confirm_plan(controller, payload["plan_id"])
    card = result["card"]
    assert result["status"] == "completed"
    assert card["header"]["statement_mode"] == "model"
    assert card["header"]["period_id"] == "FY2026"
    assert card["verdict"]["decision"] == "favorable"
    assert card["validation"]["failures"] == []
    ids = [table["id"] for table in card["tables"]]
    assert ids == [
        "statement_income",
        "statement_balance",
        "statement_cashflow",
        "ratios",
        "observations",
    ]
    assert card["rationale"]["text"]
    assert card["exports"]["xlsx"].endswith("format=xlsx")

    trace = orchestrator.trace(controller, result["run_id"])
    assert len(trace["steps"]) >= 50
    assert all(step["rule_id"].startswith("FIN-") for step in trace["steps"])
    assert orchestrator.reperform(controller, result["run_id"])["status"] == "exact"


def test_parameter_sheet_edits_accept_scaled_amounts_and_percentages(
    orchestrator, controller, session
):
    payload = orchestrator.post_message(
        controller, session, "Prepare statements on turnover 12 crore with tax rate 22%"
    )
    result = orchestrator.confirm_plan(
        controller,
        payload["plan_id"],
        edits=[
            {"key": "turnover", "value": "3 crore", "reason": "controller correction"},
            {"key": "assumptions.tax_rate", "value": "25%", "reason": "tax update"},
            {"key": "assumptions.gross_margin", "value": "0.5", "reason": "margin update"},
        ],
    )
    plan = orchestrator.store.get_plan(payload["plan_id"])
    assert plan["arguments"]["turnover"] == "30000000"
    assert plan["arguments"]["assumptions"]["tax_rate"] == "0.25"
    assert plan["arguments"]["assumptions"]["gross_margin"] == "0.5"
    assert plan["plan_version"] == 2
    assert result["card"]["validation"]["failures"] == []


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


def _plan_and_run(api, headers, text: str, attachment_ids=None) -> tuple[dict, dict]:
    session_id = _new_session(api, headers)
    payload = {"text": text}
    if attachment_ids:
        payload["attachment_ids"] = attachment_ids
    planned = api.post(f"/v1/sessions/{session_id}/messages", json=payload, headers=headers)
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    confirmed = api.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={"plan_version": 1},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    return plan, confirmed.json()


def test_api_statement_plan_trace_reperform_and_export(api, auth):
    headers = auth()
    plan, confirmed = _plan_and_run(
        api,
        headers,
        "Prepare the balance sheet, income statement, cash flow and ratio analysis "
        "on turnover of 12 crore",
    )
    assert plan["plan"]["intent"] == "financial_statements"
    assert plan["plan"]["arguments"]["turnover"] == "120000000"
    run_id = confirmed["run_id"]
    card = confirmed["card"]
    assert card["header"]["statement_mode"] == "model"
    assert [table["id"] for table in card["tables"]][-1] == "observations"

    trace = api.get(f"/v1/runs/{run_id}/trace", headers=headers).json()
    assert trace["steps"]
    assert trace["integrity"]["event_hash"]
    assert api.post(f"/v1/runs/{run_id}/reperform", headers=headers).json()["status"] == "exact"

    export = api.get(f"/v1/runs/{run_id}/export?format=xlsx", headers=headers)
    assert export.status_code == 200
    assert export.headers["x-costpilot-run"] == run_id
    workbook = load_workbook(io.BytesIO(export.content))
    analysis = workbook["Analysis"]
    values = [cell.value for row in analysis.iter_rows() for cell in row if cell.value]
    assert "Income statement" in values
    assert "Ratio analysis" in values
    summary = [cell.value for row in workbook["Summary"].iter_rows() for cell in row if cell.value]
    assert "model" in summary or "Modelled" in " ".join(str(value) for value in summary)


def test_api_reads_reported_statements_from_an_upload(api, auth):
    headers = auth()
    view = _upload(api, headers, statement_xlsx(), "statements.xlsx")
    plan, confirmed = _plan_and_run(
        api, headers, "Prepare the financial statements", [view["attachment_id"]]
    )
    assert plan["plan"]["arguments"]["mode"] == "reported"
    card = confirmed["card"]
    assert card["header"]["statement_mode"] == "reported"
    assert card["header"]["period_id"] == "FY2025"
    assert card["header"]["prior_period"] == "FY2024"
    ids = {table["id"] for table in card["tables"]}
    assert {"statement_income", "statement_balance", "statement_cashflow", "ratios"} <= ids
    balance = next(table for table in card["tables"] if table["id"] == "statement_balance")
    total_assets = next(row for row in balance["rows"] if row["line"] == "total_assets")
    assert total_assets["rule"] == "FIN-004"
    assert total_assets["value"] == "60000000"
    assert api.post(
        f"/v1/runs/{confirmed['run_id']}/reperform", headers=headers
    ).json()["status"] == "exact"
