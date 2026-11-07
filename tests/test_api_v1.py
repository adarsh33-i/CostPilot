"""Authenticated v1 API: sessions, plans, runs, exports, governance and RBAC."""

from __future__ import annotations

import io
import json

from openpyxl import load_workbook


def new_session(api, headers):
    response = api.post("/v1/sessions", json={}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["session"]["session_id"]


def plan_and_run(api, headers, text, session_id=None):
    session_id = session_id or new_session(api, headers)
    response = api.post(
        f"/v1/sessions/{session_id}/messages",
        json={"text": text},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    confirmed = api.post(
        f"/v1/plans/{body['plan_id']}/confirm",
        json={"plan_version": 1},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    return body, confirmed.json()


def test_health_and_auth(api):
    health = api.get("/api/health").json()
    assert health["ok"] is True

    required = api.get("/v1/capabilities")
    assert required.status_code == 401
    assert required.json()["error"]["code"] == "E_AUTH_REQUIRED"
    assert required.json()["remediation"]

    bad = api.post("/v1/auth/login", json={"user_id": "controller", "password": "wrong"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "E_AUTH_INVALID"


def test_login_returns_role_permissions_and_scope(api):
    body = api.post(
        "/v1/auth/login", json={"user_id": "controller", "password": "costpilot"}
    ).json()
    assert body["user"]["role"] == "controller"
    assert {"read", "run", "confirm", "approve", "export"} <= set(body["user"]["permissions"])
    assert body["user"]["scope"]["plant_ids"] == ["PLT-01", "PLT-02"]
    assert body["token"]


def test_capability_contract(api, auth):
    body = api.get("/v1/capabilities", headers=auth()).json()
    assert body["dataset"].startswith("Northline")
    assert body["currency"] == "INR"
    assert {row["plant_id"] for row in body["plants"]} == {"PLT-01", "PLT-02"}
    assert len(body["periods"]) == 3
    assert body["limits"]["max_tool_calls"] == 3
    assert set(body["roles"]) == {"viewer", "analyst", "controller", "admin"}
    assert body["data_quality"]["blocking_gates"] == 0
    assert body["demo_defaults"]["period_id"] == "2026-09"


def test_plan_confirm_trace_reperform(api, auth):
    headers = auth()
    body, confirmed = plan_and_run(api, headers, "Variance for PRD-B at PLT-01 in 2026-09")
    assert body["status"] == "ready"
    assert body["plan"]["intent"] == "variance_analysis"
    run_id = confirmed["run_id"]

    card = api.get(f"/v1/runs/{run_id}", headers=headers).json()
    assert card["verdict"]["decision"] == "adverse"
    assert card["header"]["snapshot_id"].startswith("snap_")

    trace = api.get(f"/v1/runs/{run_id}/trace", headers=headers).json()
    assert trace["steps"]
    assert trace["integrity"]["event_hash"]

    reperform = api.post(f"/v1/runs/{run_id}/reperform", headers=headers).json()
    assert reperform["status"] == "exact"

    events = api.get(f"/v1/runs/{run_id}/events", headers=headers).text
    assert "event: run.completed" in events
    assert "event: done" in events

    runs = api.get("/v1/runs", headers=headers).json()["runs"]
    assert any(row["run_id"] == run_id for row in runs)


def test_exports_are_complete_and_safe(api, auth):
    headers = auth()
    _, confirmed = plan_and_run(api, headers, "Variance for PRD-B at PLT-01 in 2026-09")
    run_id = confirmed["run_id"]

    xlsx = api.get(f"/v1/runs/{run_id}/export?format=xlsx", headers=headers)
    assert xlsx.status_code == 200
    assert "spreadsheetml" in xlsx.headers["content-type"]
    assert xlsx.headers["x-costpilot-run"] == run_id
    workbook = load_workbook(io.BytesIO(xlsx.content))
    assert len(workbook.sheetnames) >= 6
    summary = workbook[workbook.sheetnames[0]]
    values = [cell.value for row in summary.iter_rows() for cell in row if cell.value]
    assert any("snap_" in str(value) for value in values)

    pdf = api.get(f"/v1/runs/{run_id}/export?format=pdf", headers=headers)
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")

    payload = api.get(f"/v1/runs/{run_id}/export?format=json", headers=headers)
    body = json.loads(payload.content)
    assert body["run"]["run_id"] == run_id
    assert body["run"]["result"]["components"]
    assert body["card"]["header"]["snapshot_id"].startswith("snap_")

    bad = api.get(f"/v1/runs/{run_id}/export?format=docx", headers=headers)
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "E_EXPORT_FORMAT"
    assert bad.json()["remediation"]


def test_clarification_and_edit_path(api, auth):
    headers = auth()
    session_id = new_session(api, headers)
    unclear = api.post(
        f"/v1/sessions/{session_id}/messages",
        json={"text": "What should I have for lunch?"},
        headers=headers,
    ).json()
    assert unclear["status"] == "needs_clarification"
    blocked = api.post(
        f"/v1/plans/{unclear['plan_id']}/confirm",
        json={"plan_version": 1},
        headers=headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "E_PLAN_AMBIGUOUS"

    plan = api.post(
        f"/v1/sessions/{session_id}/messages",
        json={"text": "Variance for PRD-B at PLT-01 in 2026-09"},
        headers=headers,
    ).json()
    corrected = api.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={
            "plan_version": 1,
            "edits": [{"key": "product_id", "value": "PRD-A", "reason": "corrected product"}],
        },
        headers=headers,
    )
    assert corrected.status_code == 200
    assert corrected.json()["card"]["header"]["product_id"] == "PRD-A"


def test_rbac_and_scope_errors(api, auth):
    viewer = auth("viewer")
    analyst = auth("analyst")

    session_id = new_session(api, viewer)
    plan = api.post(
        f"/v1/sessions/{session_id}/messages",
        json={"text": "Variance for PRD-B at PLT-01 in 2026-09"},
        headers=viewer,
    ).json()
    forbidden = api.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={"plan_version": 1},
        headers=viewer,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "E_AUTH_SCOPE"
    assert forbidden.json()["remediation"]

    special = api.post(
        f"/v1/sessions/{new_session(api, analyst)}/messages",
        json={"text": "Special order for PRD-B at PLT-01: 1200 units at 940 each"},
        headers=analyst,
    ).json()
    approval = api.post(
        f"/v1/plans/{special['plan_id']}/confirm",
        json={"plan_version": 1},
        headers=analyst,
    )
    assert approval.status_code == 403

    scoped = api.post(
        f"/v1/sessions/{new_session(api, viewer)}/messages",
        json={"text": "Variance for PRD-B at PLT-02 in 2026-09"},
        headers=viewer,
    ).json()
    out_of_scope = api.post(
        f"/v1/plans/{scoped['plan_id']}/confirm",
        json={"plan_version": 1},
        headers=viewer,
    )
    assert out_of_scope.status_code == 403


def test_connector_mapping_flow(api, auth):
    controller = auth()
    analyst = auth("analyst")
    viewer = auth("viewer")

    catalog = api.get("/v1/connectors", headers=controller).json()
    assert catalog["write_back"] is False
    preview = api.post("/v1/connectors/netsuite/preview", headers=controller).json()
    assert preview["result"]["ok"] is True
    assert preview["read_only"] is True

    denied = api.post("/v1/connectors/netsuite/preview", headers=viewer)
    assert denied.status_code == 403

    account = api.get("/v1/registry/accounts", headers=controller).json()["accounts"][0]["account_id"]
    proposal = api.post(
        "/v1/mappings",
        json={
            "source_system": "netsuite",
            "source_account": "99001",
            "source_name": "Suspense",
            "target_account": account,
        },
        headers=analyst,
    ).json()
    assert proposal["status"] == "pending"
    approved = api.post(f"/v1/mappings/{proposal['proposal_id']}/approve", headers=controller)
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    denied_approve = api.post(f"/v1/mappings/{proposal['proposal_id']}/approve", headers=analyst)
    assert denied_approve.status_code == 403


def test_registry_governance_requires_approval(api, auth):
    analyst = auth("analyst")
    admin = auth("admin")
    accounts = api.get("/v1/registry/accounts", headers=admin).json()
    account_id = accounts["accounts"][0]["account_id"]
    version_before = accounts["registry_version"]

    proposal = api.patch(
        f"/v1/registry/accounts/{account_id}",
        json={"changes": {"owner": "plant controller"}, "reason": "ownership correction"},
        headers=analyst,
    ).json()
    assert proposal["status"] == "proposed"
    not_yet = api.get("/v1/registry/accounts", headers=admin).json()
    assert not_yet["registry_version"] == version_before

    approved = api.post(
        f"/v1/registry/proposals/{proposal['proposal_id']}/approve", headers=admin
    )
    assert approved.status_code == 200
    after = api.get("/v1/registry/accounts", headers=admin).json()
    assert after["registry_version"] == version_before + 1
    row = next(item for item in after["accounts"] if item["account_id"] == account_id)
    assert row["owner"] == "plant controller"

    denied = api.post(
        f"/v1/registry/proposals/{proposal['proposal_id']}/approve", headers=analyst
    )
    assert denied.status_code == 403
    repeat = api.post(
        f"/v1/registry/proposals/{proposal['proposal_id']}/approve", headers=admin
    )
    assert repeat.status_code == 409
    assert repeat.json()["error"]["code"] == "REGISTRY_PROPOSAL_STATE"


def test_alerts_rules_persist_and_delete(api, auth):
    analyst = auth("analyst")
    admin = auth("admin")
    created = api.post(
        "/v1/alerts/rules",
        json={"text": "material variance above 50,000", "materiality": "50000"},
        headers=analyst,
    ).json()
    rules = api.get("/v1/alerts/rules", headers=analyst).json()["rules"]
    assert any(rule["rule_id"] == created["rule_id"] for rule in rules)

    denied = api.delete(f"/v1/alerts/rules/{created['rule_id']}", headers=analyst)
    assert denied.status_code == 403
    deleted = api.delete(f"/v1/alerts/rules/{created['rule_id']}", headers=admin)
    assert deleted.json()["deleted"] is True


def test_dataset_templates_and_validation(api, auth):
    headers = auth()
    templates = api.get("/v1/datasets/templates", headers=headers).json()["templates"]
    assert templates
    name = templates[0]["file"]
    download = api.get(f"/v1/datasets/templates/{name}", headers=headers)
    assert download.status_code == 200
    assert download.text.splitlines()[0].count(",") >= 1

    valid = api.post(
        "/v1/datasets/validate",
        files={"file": (name, download.content, "text/csv")},
        headers=headers,
    ).json()
    assert valid["ok"] is True
    assert valid["applies_to_runs"] is False

    invalid = api.post(
        "/v1/datasets/validate",
        files={"file": (name, b"wrong,header\n1,2\n", "text/csv")},
        headers=headers,
    ).json()
    assert invalid["ok"] is False
    assert invalid["missing"]

    unknown = api.post(
        "/v1/datasets/validate",
        files={"file": ("notes.csv", b"a,b\n", "text/csv")},
        headers=headers,
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "SCHEMA_MISMATCH"


def test_customer_scope_flows_through_http(api, auth):
    headers = auth()
    _, confirmed = plan_and_run(
        api, headers,
        "Customer profitability for PLT-01 in 2026-09 for CUS-05",
    )
    card = confirmed["card"]
    assert card["header"]["customer_id"] == "CUS-05"
    customers = next(table for table in card["tables"] if table["id"] == "customers")
    assert [row["customer_id"] for row in customers["rows"]] == ["CUS-05"]
