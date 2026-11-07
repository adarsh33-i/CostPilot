"""The local model plans and explains; the rule engine still owns every number."""

from __future__ import annotations

import json

from costpilot.platform import llm as llm_module
from costpilot.platform.llm import LLMClient, model_catalog
from costpilot.platform.planner import plan_message


class FakeLLM:
    def __init__(self, hints=None, narration=None):
        self.hints = hints
        self.narration = narration
        self.last_error = None
        self.interpreted: list[str] = []
        self.narrated: list[str] = []

    def interpret(self, question, catalog, *, model=None):
        self.interpreted.append(model or "")
        return self.hints

    def narrate(self, card, *, model=None):
        self.narrated.append(model or "")
        return self.narration


def test_model_hint_selects_the_registered_analysis(snapshot):
    hints = {
        "intent": "tdabc",
        "confidence": "0.91",
        "slots": {"period_id": "2026-09", "plant_id": "PLT-02"},
        "planner": "llm:fake:8b",
        "model": "fake:8b",
        "reason": "capacity questions map to time-driven ABC",
    }
    plan = plan_message(snapshot, "Where is capacity being wasted?", hints=hints)
    assert plan["intent"] == "tdabc"
    assert plan["arguments"]["plant_id"] == "PLT-02"
    assert plan["planner"] == "llm:fake:8b"
    assert plan["model"] == "fake:8b"
    assert plan["planner_reason"] == hints["reason"]
    sources = {row["key"]: row["source"] for row in plan["assumptions"]}
    assert sources["plant_id"] == "user_implied"


def test_unknown_hinted_intent_falls_back_to_the_rule_planner(snapshot):
    hints = {"intent": "invent_a_new_report", "confidence": "0.99", "slots": {}}
    plan = plan_message(snapshot, "Variance for PRD-B at PLT-01 in 2026-09", hints=hints)
    assert plan["intent"] == "variance_analysis"
    assert plan["planner"] == "deterministic"


def test_hinted_overlay_is_converted_to_decimal_shares(snapshot):
    hints = {
        "intent": "budget_scenario",
        "confidence": "0.9",
        "slots": {"overlay": {"volume": -2, "price": 3, "nonsense": 5}},
        "planner": "llm:fake:8b",
        "model": "fake:8b",
    }
    plan = plan_message(snapshot, "Rebuild the budget for PLT-01 in 2026-09", hints=hints)
    assert plan["arguments"]["overlay"] == {"volume": "-0.02", "price": "0.03"}


def test_orchestrator_records_planner_and_returns_model_answer(
    snapshot, orchestrator, controller, session
):
    hints = {
        "intent": "variance_analysis",
        "confidence": "0.9",
        "slots": {"period_id": "2026-09", "plant_id": "PLT-01", "product_id": "PRD-B"},
        "planner": "llm:fake:8b",
        "model": "fake:8b",
        "reason": "variance question",
    }
    orchestrator.llm = FakeLLM(hints=hints, narration="Material cost variance is unfavourable.")
    payload = orchestrator.post_message(
        controller, session, "How did material cost land this month?", model="fake:8b"
    )
    assert payload["planner"] == "llm:fake:8b"
    assert payload["plan"]["intent"] == "variance_analysis"
    assert payload["status"] == "ready"

    result = orchestrator.confirm_plan(controller, payload["plan_id"])
    assert result["assistant_text"] == "Material cost variance is unfavourable."
    assert orchestrator.llm.narrated == ["fake:8b"]
    messages = orchestrator.store.messages(session)
    assert messages[-1]["text"] == "Material cost variance is unfavourable."


def test_model_failure_falls_back_without_blocking(orchestrator, controller, session):
    fake = FakeLLM(hints=None)
    fake.last_error = "connection refused"
    orchestrator.llm = fake
    payload = orchestrator.post_message(
        controller, session, "Variance for PRD-B at PLT-01 in 2026-09", model="missing:8b"
    )
    assert payload["status"] == "ready"
    assert payload["planner"] == "deterministic"
    assert "det" in (payload["planner_note"] or "").lower()
    assert fake.interpreted == ["missing:8b"]


def test_deterministic_model_never_calls_ollama(orchestrator, controller, session):
    fake = FakeLLM(hints={"intent": "tdabc", "confidence": "0.9", "slots": {}})
    orchestrator.llm = fake
    payload = orchestrator.post_message(
        controller, session, "Variance for PRD-B at PLT-01 in 2026-09", model="deterministic"
    )
    assert payload["planner"] == "deterministic"
    assert fake.interpreted == []


def _chat_reply(content: str) -> dict:
    return {"message": {"content": content}}


def test_interpret_rejects_identifiers_outside_the_catalog(snapshot, monkeypatch):
    raw = {
        "intent": "variance_analysis",
        "confidence": 0.88,
        "slots": {
            "period_id": "2026-09",
            "product_id": "PRD-A",
            "plant_id": "PLT-99",
            "order_units": "-4",
            "overlay": {"volume": 2},
        },
        "reason": "extracted",
    }
    monkeypatch.setattr(llm_module, "_post", lambda *args, **kwargs: _chat_reply(json.dumps(raw)))
    client = LLMClient()
    hints = client.interpret("variance", {"periods": ["2026-09"], "products": ["PRD-A"], "plants": ["PLT-01"]}, model="fake:8b")
    assert hints["intent"] == "variance_analysis"
    assert hints["slots"] == {"period_id": "2026-09", "product_id": "PRD-A", "overlay": {"volume": "2"}}
    assert hints["planner"] == "llm:fake:8b"


def test_interpret_rejects_an_unknown_intent(monkeypatch):
    monkeypatch.setattr(
        llm_module, "_post", lambda *args, **kwargs: _chat_reply('{"intent": "make_it_up"}')
    )
    assert LLMClient().interpret("hello", {}) is None


def test_narration_must_reuse_calculated_numbers(monkeypatch, orchestrator, controller, session):
    payload = orchestrator.post_message(
        controller, session, "Variance for PRD-B at PLT-01 in 2026-09"
    )
    card = orchestrator.confirm_plan(controller, payload["plan_id"])["card"]
    fact = card["facts"][0]["display"]

    monkeypatch.setattr(
        llm_module,
        "_post",
        lambda *args, **kwargs: _chat_reply(f"The engine reports {fact}, so act now."),
    )
    assert LLMClient().narrate(card, model="fake:8b")

    monkeypatch.setattr(
        llm_module,
        "_post",
        lambda *args, **kwargs: _chat_reply("The saving is 987654321, so act now."),
    )
    assert LLMClient().narrate(card, model="fake:8b") is None

    monkeypatch.setattr(
        llm_module,
        "_post",
        lambda *args, **kwargs: _chat_reply(f"The engine reports {card['facts'][0]['display']}."),
    )
    assert "$" not in (LLMClient().narrate(card, model="fake:8b") or "")

    monkeypatch.setattr(
        llm_module,
        "_post",
        lambda *args, **kwargs: _chat_reply(f"The engine reports ${card['facts'][0]['display']}."),
    )
    assert LLMClient().narrate(card, model="fake:8b") is None


def test_model_catalog_always_offers_the_deterministic_planner(monkeypatch):
    monkeypatch.setattr(LLMClient, "models", lambda self: [])
    catalog = model_catalog(LLMClient())
    assert catalog["available"] is False
    assert catalog["default"] == "deterministic"
    assert [row["id"] for row in catalog["models"]] == ["deterministic"]


def test_models_endpoint_lists_free_local_models(api, auth, monkeypatch):
    monkeypatch.setattr(
        "costpilot.api.main.model_catalog",
        lambda client: {
            "models": [{"id": "qwen3:8b", "label": "qwen3", "provider": "ollama", "free": True, "local": True, "tools": True}],
            "default": "qwen3:8b",
            "ollama": "http://127.0.0.1:11434",
            "available": True,
            "note": "test",
        },
    )
    response = api.get("/v1/models", headers=auth())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["models"][0]["free"] is True
    assert body["planner"].startswith("AI selects")
