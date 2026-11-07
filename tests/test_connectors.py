"""Read-only connectors and human-approved account mappings."""

from __future__ import annotations

import pytest

from costpilot.engine.errors import DomainError


def test_catalog_is_read_only(service):
    catalog = service.connector_catalog()
    assert {row["name"] for row in catalog["connectors"]} == {"netsuite", "sap", "flatfile"}
    assert catalog["write_back"] is False
    assert all(row["mode"] == "read_only_extract" for row in catalog["connectors"])


def test_preview_reconciles_and_never_writes_back(service):
    envelope = service.run_connector_tool(name="netsuite", record=False)
    result = envelope["result"]
    assert result["connector"] == "netsuite"
    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["write_back"] is False
    assert result["row_count"] > 0
    assert result["source_hash"]
    assert all(gate["status"] == "pass" for gate in result["gates"])
    assert all(row["difference"] == "0.00" for row in result["control"])
    assert envelope["read_only"] is True
    assert envelope["trace_ref"]["run_id"] is None  # record=False proves no write-back run


def test_preview_records_a_sealed_run_when_asked(service, controller):
    envelope = service.run_connector_tool(name="sap", user=controller, record=True)
    assert envelope["trace_ref"]["run_id"].startswith("run_")
    stored = service.audit.get(envelope["trace_ref"]["run_id"])
    assert stored["tool"]["name"] == "connector:sap"


def test_unknown_connector_lists_supported_systems(service, controller):
    with pytest.raises(DomainError) as exc:
        service.run_connector_tool(name="oracle", user=controller, record=False)
    assert exc.value.code == "CONNECTOR_UNKNOWN"
    assert "netsuite" in exc.value.remediation


def test_mapping_requires_a_canonical_target(service, controller):
    with pytest.raises(DomainError) as exc:
        service.propose_mapping(
            source_system="netsuite",
            source_account="40010",
            source_name="Revenue",
            target_account="NOT-AN-ACCOUNT",
            user=controller,
        )
    assert exc.value.code == "MAPPING_TARGET_UNKNOWN"


def test_mapping_proposal_waits_for_human_approval(service, controller):
    account_id = sorted(service.snapshot.accounts)[0]
    proposal = service.propose_mapping(
        source_system="netsuite",
        source_account="99001",
        source_name="Suspense",
        target_account=account_id,
        user=controller,
    )
    assert proposal["status"] == "pending"
    assert proposal["approved_by"] is None
    approved = service.approve_mapping(proposal["proposal_id"], controller)
    assert approved["status"] == "approved"
    assert approved["approved_by"] == controller.user_id
    rows = service.mappings.approved("netsuite")
    assert rows["99001"]["target_account"] == account_id


def test_mapping_approval_unknown_id_has_a_path_forward(service, controller):
    with pytest.raises(DomainError) as exc:
        service.approve_mapping("map_missing", controller)
    assert exc.value.code == "MAPPING_NOT_FOUND"
    assert "proposal" in exc.value.remediation.lower()
