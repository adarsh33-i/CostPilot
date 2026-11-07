"""Plan -> confirm -> sealed card, with RBAC, scope and idempotency."""

from __future__ import annotations

import pytest

from costpilot.engine.errors import DomainError


def ask(orchestrator, user, session_id, text):
    return orchestrator.post_message(user, session_id, text)


def test_variance_plan_runs_and_returns_a_card(orchestrator, controller, session):
    payload = ask(
        orchestrator, controller, session,
        "Variance for PRD-B at PLT-01 in 2026-09",
    )
    assert payload["status"] == "ready"
    assert payload["plan"]["intent"] == "variance_analysis"
    assert payload["plan_id"].startswith("plan_")
    sheet = {row["key"]: row for row in payload["parameter_sheet"]}
    assert sheet["period_id"]["value"] == "2026-09"
    assert sheet["plant_id"]["source"] == "lookup"

    result = orchestrator.confirm_plan(controller, payload["plan_id"])
    card = result["card"]
    assert result["status"] == "completed"
    assert result["run_id"].startswith("run_")
    assert card["header"]["snapshot_id"].startswith("snap_")
    assert card["verdict"]["decision"] == "adverse"
    assert card["facts"]
    assert card["tables"]
    assert card["rationale"]["text"]
    assert card["validation"]["all_components_pass"] is True
    assert card["audit"]["trace"].endswith("/trace")
    assert card["exports"]["xlsx"].endswith("format=xlsx")

    stored = orchestrator.get_run(controller, result["run_id"])
    assert stored["run_id"] == result["run_id"]
    trace = orchestrator.trace(controller, result["run_id"])
    assert trace["steps"]
    assert all(step["rule_id"] for step in trace["steps"])


def test_confirmation_is_idempotent(orchestrator, controller, session):
    payload = ask(orchestrator, controller, session, "Variance for PRD-B at PLT-01 in 2026-09")
    first = orchestrator.confirm_plan(controller, payload["plan_id"], idempotency_key="ticket-1")
    second = orchestrator.confirm_plan(controller, payload["plan_id"], idempotency_key="ticket-1")
    assert second["idempotent"] is True
    assert second["run_id"] == first["run_id"]


def test_idempotency_key_is_bound_to_one_plan(orchestrator, controller, session):
    first = ask(orchestrator, controller, session, "Variance for PRD-B at PLT-01 in 2026-09")
    second = ask(orchestrator, controller, session, "Variance for PRD-A at PLT-01 in 2026-09")
    orchestrator.confirm_plan(controller, first["plan_id"], idempotency_key="ticket-1")
    with pytest.raises(DomainError) as exc:
        orchestrator.confirm_plan(controller, second["plan_id"], idempotency_key="ticket-1")
    assert exc.value.code == "E_IDEMPOTENCY_CONFLICT"


def test_plan_edits_are_applied_and_versioned(orchestrator, controller, session):
    payload = ask(orchestrator, controller, session, "Variance for PRD-B at PLT-01 in 2026-09")
    result = orchestrator.confirm_plan(
        controller,
        payload["plan_id"],
        edits=[{"key": "product_id", "value": "PRD-A", "reason": "controller corrected the product"}],
    )
    assert result["card"]["header"]["product_id"] == "PRD-A"
    plan = orchestrator.store.get_plan(payload["plan_id"])
    assert plan["plan_version"] == 2
    assert plan["user_edits"][0]["confirmed"] == "PRD-A"


def test_clarification_cannot_be_confirmed(orchestrator, controller, session):
    payload = ask(orchestrator, controller, session, "What should I have for lunch?")
    assert payload["status"] == "needs_clarification"
    assert payload["next_actions"][0]["action"] == "answer_clarification"
    with pytest.raises(DomainError) as exc:
        orchestrator.confirm_plan(controller, payload["plan_id"])
    assert exc.value.code == "E_PLAN_AMBIGUOUS"
    assert exc.value.remediation


def test_cancelled_plan_cannot_run(orchestrator, controller, session):
    payload = ask(orchestrator, controller, session, "Variance for PRD-B at PLT-01 in 2026-09")
    orchestrator.cancel_plan(controller, payload["plan_id"], "changed my mind")
    with pytest.raises(DomainError) as exc:
        orchestrator.confirm_plan(controller, payload["plan_id"])
    assert exc.value.code == "E_PLAN_CANCELLED"


def test_analyst_cannot_confirm_a_high_impact_plan(orchestrator, login):
    analyst = login("analyst")
    own_session = orchestrator.create_session(analyst)["session"]["session_id"]
    payload = ask(
        orchestrator, analyst, own_session,
        "Special order for PRD-B at PLT-01: 1200 units at 940 each",
    )
    assert payload["status"] == "needs_confirmation"
    with pytest.raises(DomainError) as exc:
        orchestrator.confirm_plan(analyst, payload["plan_id"])
    assert exc.value.code == "E_AUTH_SCOPE"
    assert "permission" in exc.value.message


def test_controller_can_confirm_special_order(orchestrator, controller, session):
    payload = ask(
        orchestrator, controller, session,
        "Special order for PRD-B at PLT-01: 1200 units at 940 each",
    )
    card = orchestrator.confirm_plan(controller, payload["plan_id"])["card"]
    assert card["intent"] == "special_order"
    assert card["verdict"]["decision"] in {"accept", "accept, conditionally", "reject"}


def test_viewer_scope_blocks_other_plant(orchestrator, login):
    viewer = login("viewer")
    own_session = orchestrator.create_session(viewer)["session"]["session_id"]
    payload = ask(
        orchestrator, viewer, own_session,
        "Variance for PRD-B at PLT-02 in 2026-09",
    )
    with pytest.raises(DomainError) as exc:
        orchestrator.confirm_plan(viewer, payload["plan_id"])
    assert exc.value.code == "E_AUTH_SCOPE"


def test_viewer_cannot_run_any_analysis(orchestrator, login):
    viewer = login("viewer")
    own_session = orchestrator.create_session(viewer)["session"]["session_id"]
    payload = ask(
        orchestrator, viewer, own_session,
        "Variance for PRD-B at PLT-01 in 2026-09",
    )
    with pytest.raises(DomainError) as exc:
        orchestrator.confirm_plan(viewer, payload["plan_id"])
    assert exc.value.code == "E_AUTH_SCOPE"


def test_sessions_are_private(orchestrator, controller, login, session):
    analyst = login("analyst")
    with pytest.raises(DomainError) as exc:
        orchestrator.session_detail(analyst, session)
    assert exc.value.code == "E_AUTH_SCOPE"


def test_empty_question_is_rejected_with_guidance(orchestrator, controller, session):
    with pytest.raises(DomainError) as exc:
        ask(orchestrator, controller, session, "   ")
    assert exc.value.code == "E_EMPTY_MESSAGE"
    assert "variance" in exc.value.remediation


def test_customer_scope_reaches_the_engine(orchestrator, controller, session):
    payload = ask(
        orchestrator, controller, session,
        "Customer profitability for PLT-01 in 2026-09 for CUS-05",
    )
    card = orchestrator.confirm_plan(controller, payload["plan_id"])["card"]
    assert card["header"]["customer_id"] == "CUS-05"
    customers = next(table for table in card["tables"] if table["id"] == "customers")
    assert [row["customer_id"] for row in customers["rows"]] == ["CUS-05"]


def test_audit_chain_verifies_after_runs(orchestrator, controller, session):
    payload = ask(orchestrator, controller, session, "Variance for PRD-B at PLT-01 in 2026-09")
    orchestrator.confirm_plan(controller, payload["plan_id"])
    chain = orchestrator.service.verify_chain()
    assert chain["ok"] is True
    assert chain["checked"] >= 1


def test_run_events_stream_after_completion(orchestrator, controller, session):
    payload = ask(orchestrator, controller, session, "Variance for PRD-B at PLT-01 in 2026-09")
    run_id = orchestrator.confirm_plan(controller, payload["plan_id"])["run_id"]
    events = orchestrator.run_events(controller, run_id)
    kinds = {event["event"] for event in events}
    assert "run.completed" in kinds
