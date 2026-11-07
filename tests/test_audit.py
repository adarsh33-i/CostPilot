"""Append-only audit chain, tamper resistance, and re-performance."""

from __future__ import annotations

import sqlite3

import pytest

from costpilot.audit import AuditStore, compare_results


def append(store, value="1"):
    return store.append(
        tool="run_differential_analysis",
        question="test",
        intent="special_order",
        arguments={"x": value},
        result={"value": value, "steps": []},
        validation={"failures": []},
        pins={"engine_version": "0.3.0"},
    )


def test_chain_links_and_verifies(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    first = append(store, "1")
    second = append(store, "2")
    assert first.prev_hash == "GENESIS"
    assert second.prev_hash == first.event_hash
    verified = store.verify_chain()
    assert verified == {"ok": True, "checked": 2, "first_break": None, "reason": None}
    payload = store.get(second.run_id)
    assert payload["integrity"]["event_hash"] == second.event_hash
    assert payload["arguments"] == {"x": "2"}


def test_audit_rows_are_append_only(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    run = append(store)
    store.close()
    connection = sqlite3.connect(tmp_path / "audit.db")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE events SET question = 'rewritten' WHERE run_id = ?", (run.run_id,))
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM events WHERE run_id = ?", (run.run_id,))
    connection.close()


def test_verify_detects_a_broken_link(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    append(store)
    append(store)
    store.close()
    connection = sqlite3.connect(tmp_path / "audit.db")
    connection.execute("DROP TRIGGER events_no_update")
    connection.execute("UPDATE events SET prev_hash = 'tampered' WHERE seq = 2")
    connection.commit()
    connection.close()
    reopened = AuditStore(tmp_path / "audit.db")
    verified = reopened.verify_chain()
    assert verified["ok"] is False
    assert verified["reason"] == "previous hash mismatch"


def test_compare_results_statuses():
    stored = {
        "steps": [
            {"step_id": "s1", "value": "100"},
            {"step_id": "s2", "value": "200"},
        ]
    }
    exact = compare_results(stored, stored)
    assert exact["status"] == "exact"
    rounding = compare_results(
        stored, {"steps": [{"step_id": "s1", "value": "100.004"}, {"step_id": "s2", "value": "200"}]}
    )
    assert rounding["status"] == "display_rounding"
    mismatch = compare_results(
        stored, {"steps": [{"step_id": "s1", "value": "101"}, {"step_id": "s2", "value": "200"}]}
    )
    assert mismatch["status"] == "mismatch"
    missing = compare_results(stored, {"steps": [{"step_id": "s1", "value": "100"}]})
    assert missing["status"] == "mismatch"
    assert missing["missing_steps"] == ["s2"]


def test_special_order_run_reperforms_exactly(service):
    envelope = service.run_differential(
        decision_type="special_order", arguments={}, confirmation={"confirmed": True}
    )
    run_id = envelope["trace_ref"]["run_id"]
    assert run_id
    assert service.reperform(run_id)["status"] == "exact"
    assert service.verify_chain()["ok"] is True


def test_variance_pack_reperforms_exactly(service):
    envelope = service.run_variance()
    run_id = envelope["trace_ref"]["run_id"]
    result = service.reperform(run_id)
    assert result["status"] == "exact"
    assert set(result["components"]) == {
        "material", "labor", "variable_overhead", "fixed_overhead",
        "flexible_budget", "account_flux", "costing_bridge",
    }


def test_bridge_reperforms_exactly(service):
    envelope = service.run_bridge()
    assert service.reperform(envelope["trace_ref"]["run_id"])["status"] == "exact"
