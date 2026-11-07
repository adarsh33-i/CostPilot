"""HTTP surface: confirmation gate, analysis payloads, audit, and errors."""

from __future__ import annotations


def test_workspace_and_metadata(api):
    page = api.get("/")
    assert page.status_code == 200
    assert "CostPilot" in page.text
    assert "confirm" in page.text

    health = api.get("/api/health").json()
    assert health["ok"] is True
    assert health["snapshot_id"].startswith("snap_")
    assert health["audit_chain"]["ok"] is True

    capability = api.get("/api/capability").json()
    assert capability["dataset"].startswith("Northline")
    assert len(capability["data_quality"]["gates"]) == 10
    assert capability["data_quality"]["blocking_gates"] == 0
    assert capability["supported_decisions"] == sorted(
        ["add_or_drop", "cvp", "make_or_buy", "operate_or_shutdown", "product_mix",
         "sell_or_process", "special_order"]
    )

    rules = api.get("/api/rules").json()
    assert rules["registry_version"] == 1
    assert len(rules["rules"]) >= 40
    assert all(rule["citation"] for rule in rules["rules"])

    tools = api.get("/api/tools").json()
    assert {tool["name"] for tool in tools["tools"]} >= {
        "describe_dataset", "run_differential_analysis", "run_variance_analysis",
        "run_costing_bridge",
    }


def test_preview_resolves_without_calculating(api):
    preview = api.post(
        "/api/tools/preview_differential_analysis",
        json={"decision_type": "special_order", "arguments": {}},
    ).json()
    assert "result" not in preview
    sheet = {item["key"]: item for item in preview["confirmation_sheet"]}
    assert sheet["order_units"]["value"] == "2000"
    assert sheet["order_units"]["source"] == "demo_default"
    assert sheet["variable_manufacturing_cost"]["source"] == "standard_cost"
    assert sheet["idle_capacity_units"]["value"] == "2200"
    assert sheet["idle_capacity_units"]["source"] == "snapshot"


def test_run_requires_confirmation_and_returns_a_path_forward(api):
    response = api.post(
        "/api/tools/run_differential_analysis",
        json={"decision_type": "special_order", "arguments": {}},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert body["remediation"]
    assert "confirmation.confirmed" in body["remediation"][0]


def test_confirmed_run_matches_the_canonical_case(api):
    response = api.post(
        "/api/tools/run_differential_analysis",
        json={
            "decision_type": "special_order",
            "arguments": {},
            "confirmation": {"confirmed": True},
            "question": "Should we accept the order?",
        },
    )
    assert response.status_code == 200
    body = response.json()
    result = body["result"]
    assert result["value"] == "120000"
    assert result["verdict"] == "accept, conditionally"
    assert body["validation"]["failures"] == []
    run_id = body["trace_ref"]["run_id"]
    assert run_id.startswith("run_")
    assert len(body["trace_ref"]["event_hash"]) == 64

    stored = api.get(f"/api/runs/{run_id}")
    assert stored.status_code == 200
    assert stored.json()["question"] == "Should we accept the order?"
    assert stored.json()["pins"]["snapshot_id"] == body["snapshot_id"]

    reperformed = api.post(f"/api/runs/{run_id}/reperform")
    assert reperformed.json()["status"] == "exact"


def test_variance_and_bridge_endpoints(api):
    variance = api.post("/api/tools/run_variance_analysis", json={}).json()
    summary = variance["result"]["summary"]
    assert summary["material_cost_variance"] == "-6509.484"
    assert summary["labour_cost_variance"] == "-2893.824"
    assert summary["flexible_budget_variance"] == "-10846.768"
    assert summary["decomposition_status"] == "unavailable"
    assert variance["validation"]["all_components_pass"] is True

    bridge = api.post("/api/tools/run_costing_bridge", json={}).json()
    assert bridge["result"]["value"] == "206053.232"
    assert bridge["validation"]["failures"] == []


def test_errors_are_structured_and_actionable(api):
    unsupported = api.post(
        "/api/tools/run_differential_analysis",
        json={"decision_type": "close_the_books", "arguments": {}, "confirmation": {"confirmed": True}},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "UNSUPPORTED_DECISION"
    assert unsupported.json()["remediation"]

    unknown = api.post(
        "/api/tools/run_differential_analysis",
        json={
            "decision_type": "special_order",
            "arguments": {"order_units": "10", "order_price": "5", "profit": "1"},
            "confirmation": {"confirmed": True},
        },
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] in {"UNKNOWN_ARGUMENT", "MISSING_ARGUMENT"}

    missing_run = api.get("/api/runs/run_nonexistent")
    assert missing_run.status_code == 404
    assert missing_run.json()["remediation"]


def test_audit_chain_endpoint(api):
    api.post(
        "/api/tools/run_differential_analysis",
        json={"decision_type": "special_order", "arguments": {}, "confirmation": {"confirmed": True}},
    )
    chain = api.get("/api/audit/verify").json()
    assert chain["ok"] is True
    assert chain["checked"] >= 1
