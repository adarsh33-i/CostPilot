"""Deterministic orchestrator: session -> plan -> confirmation -> run -> card.

The orchestrator is the only component allowed to decide whether a plan may
execute. It never computes a business number; it assembles engine outputs,
checks permissions and scope, seals the audit run, validates narration, and
records the events the UI streams.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from costpilot.data.documents import AttachmentError, public_view
from costpilot.engine.errors import DomainError
from costpilot.engine.money import display_amount
from costpilot.engine.narration import fallback_template, render_validated
from costpilot.engine.trace import Fact
from costpilot.platform.auth import AuthService, User
from costpilot.platform.llm import LLMClient, dataset_catalog
from costpilot.platform.planner import STATEMENT_SHARE_KEYS, parse_amount, plan_message
from costpilot.platform.store import PlatformStore
from costpilot.service import ToolService

AUTO_INTENTS = {"describe_dataset", "audit_verify", "explain_run"}
READ_ONLY_INTENTS = {"describe_dataset", "audit_verify", "explain_run", "connector_preview"}
INTENT_LABELS = {
    "describe_dataset": "Dataset capability",
    "attachment_analysis": "Attached document",
    "financial_statements": "Financial statements and ratios",
    "variance_analysis": "Variance analysis",
    "costing_bridge": "Variable-to-absorption bridge",
    "special_order": "Special order",
    "abc_profitability": "ABC product profitability",
    "customer_profitability": "Customer profitability",
    "tdabc": "Time-driven ABC",
    "budget_chain": "Master budget",
    "budget_scenario": "Budget scenario",
    "rolling_forecast": "Rolling forecast",
    "forecast_quality": "Forecast quality",
    "performance_eval": "Divisional performance",
    "monitoring": "Alert digest",
    "group_report": "Group consolidation",
    "connector_preview": "Connector reconciliation",
    "explain_run": "Explain run",
    "audit_verify": "Audit verification",
    "clarify": "Clarification",
}
LABELS = {
    "period_id": "Period",
    "plant_id": "Plant",
    "product_id": "Product",
    "division_id": "Division",
    "customer_id": "Customer",
    "basis": "Allocation basis",
    "decision_type": "Decision type",
    "order_units": "Order units",
    "order_price": "Order price per unit",
    "incremental_fixed_cost": "Incremental fixed cost",
    "capacity_state": "Capacity state",
    "variable_selling_cost_avoided": "Variable selling cost avoided",
    "overlay": "Scenario overlay",
    "label": "Scenario label",
    "rules": "Alert rules",
    "materiality": "Materiality threshold",
    "connector": "Connector",
    "run_id": "Run id",
    "attachment_id": "Attachment",
    "operation": "Document operation",
    "table": "Table or sheet",
    "column": "Column",
    "compare_column": "Compare column",
    "query": "Search term",
    "limit": "Row limit",
    "direction": "Sort direction",
    "turnover": "Annual turnover",
    "prior_turnover": "Prior-year turnover",
    "mode": "Statement source",
    "currency": "Currency",
    "period": "Statement period",
    "assumptions": "Modelling assumptions",
    "assumption_sources": "Assumption sources",
}
SUGGESTIONS = {
    "special_order": [
        "Run the sensitivity if the order fills the plant.",
        "What is the minimum acceptable price per unit?",
        "Show the full audit trace for this run.",
    ],
    "variance_analysis": [
        "Which accounts drive the largest flux?",
        "Run ABC to see whether overhead allocation distorts product margin.",
        "Show the variable-to-absorption bridge.",
    ],
    "abc_profitability": [
        "Run TDABC to separate unused capacity from product cost.",
        "Which customers are loss-making after cost to serve?",
        "Switch the traditional basis to machine hours.",
    ],
    "customer_profitability": [
        "Which customer is the anchor account?",
        "Run TDABC for unused capacity.",
        "Explain the largest cost-to-serve line.",
    ],
    "tdabc": [
        "Which pool has the lowest utilisation?",
        "Compare ABC and traditional product cost.",
        "Show the full audit trace.",
    ],
    "budget_chain": [
        "What if material price rises 3%?",
        "Run the rolling forecast.",
        "Check forecast quality for sandbagging.",
    ],
    "budget_scenario": [
        "Show the baseline master budget.",
        "Change the volume assumption and rebuild.",
        "Run the rolling forecast for the same period.",
    ],
    "rolling_forecast": [
        "Check forecast bias and sandbagging.",
        "Show the master budget chain.",
        "What if volume falls 2%?",
    ],
    "forecast_quality": [
        "Show the rolling forecast.",
        "Which products are flagged?",
        "Show the trace for this run.",
    ],
    "performance_eval": [
        "Why does ROI reject the new investment?",
        "Show controllable KPI misses.",
        "Compare residual income after the investment.",
    ],
    "monitoring": [
        "Which alert has the largest material impact?",
        "Explain the top alert as a variance pack.",
        "Save this rule for a weekly digest.",
    ],
    "group_report": [
        "Show the intercompany pairs flagged for review.",
        "Explain the inventory profit elimination.",
        "Show the entity-level roll-up.",
    ],
    "connector_preview": [
        "Which mapping is still unapproved?",
        "Show the reconciliation gates.",
        "Preview the SAP extract.",
    ],
    "attachment_analysis": [
        "Show the rows behind this figure.",
        "Compare two columns in the file.",
        "Which rows are highest for this column?",
    ],
    "financial_statements": [
        "Run the ratio analysis on the prior year too.",
        "What happens if gross margin rises 2 percentage points?",
        "Show the full audit trace for this run.",
    ],
    "variance": [
        "Show the audit trace.",
        "Export this working paper to Excel.",
    ],
}
DECISION_ORDER = ("reject", "accept", "investigate", "make", "buy")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _decimal_text(value: Any) -> str:
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, list | tuple):
        return [_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scalar(item) for key, item in value.items()}
    return value


def _verdict_class(verdict: str | None, favorable: bool | None) -> str:
    if verdict:
        lowered = verdict.lower()
        for word in DECISION_ORDER:
            if re.search(rf"\b{word}\b", lowered):
                return "accept" if word == "accept" else word
        if "below" in lowered or "loss" in lowered or "negative" in lowered:
            return "adverse"
        if "positive" in lowered or "above" in lowered:
            return "favorable"
        return "investigate"
    if favorable is True:
        return "favorable"
    if favorable is False:
        return "adverse"
    return "investigate"


def _verdict_sentence(intent: str, result: dict[str, Any], facts: list[dict[str, Any]] | None = None) -> str:
    verdict = result.get("verdict")
    if verdict:
        return str(verdict)
    if intent == "variance_analysis":
        rows = {row.get("fact_id"): _to_decimal(row.get("value")) for row in (facts or [])}
        material = rows.get("f_mcv")
        labour = rows.get("f_lcv")
        if material is not None and labour is not None:
            if material < 0 and labour < 0:
                return "Material and labour cost variances are both adverse; review price, rate and usage drivers before closing."
            if material > 0 and labour > 0:
                return "Material and labour cost variances are both favourable; confirm the drivers before closing."
            return "Material and labour variances move in opposite directions; review the component tables before closing."
    favorable = result.get("favorable")
    if favorable is True:
        return f"The engine reports a favourable outcome for this {INTENT_LABELS.get(intent, 'analysis').lower()}."
    if favorable is False:
        return f"The engine reports an adverse outcome for this {INTENT_LABELS.get(intent, 'analysis').lower()}."
    return f"The engine completed this {INTENT_LABELS.get(intent, 'analysis').lower()} with no single verdict."


@dataclass
class _View:
    """Adapter that lets the narration validator read a serialised result."""

    facts: list[Fact]
    steps: list[Any]
    meta: dict[str, Any]

    def fact_map(self) -> dict[str, Fact]:
        return {fact.fact_id: fact for fact in self.facts}


def _view(result: dict[str, Any]) -> _View:
    facts = [
        Fact(
            fact_id=str(row.get("fact_id")),
            step_id=str(row.get("step_id", "")),
            label=str(row.get("label", row.get("fact_id"))),
            value=_to_decimal(row.get("value")),
            unit=str(row.get("unit", "")),
        )
        for row in result.get("facts", [])
        if row.get("fact_id")
    ]
    steps = [
        SimpleNamespace(
            step_id=str(row.get("step_id", "")),
            rule_id=str(row.get("rule_id", "")),
            label=str(row.get("label", "")),
        )
        for row in result.get("steps", [])
    ]
    return _View(facts=facts, steps=steps, meta=result.get("meta", {}) or {})


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _normalize_statement_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Accept scaled amounts and percentages from parameter-sheet edits."""
    normalized = dict(arguments)
    for key in ("turnover", "prior_turnover"):
        value = normalized.get(key)
        if value in (None, ""):
            continue
        parsed = parse_amount(str(value))
        if parsed is None:
            raise DomainError(
                "E_PLAN_EDIT",
                f"{key.replace('_', ' ').title()} is not a valid amount.",
                "Send a positive number, for example 12000000 or 12 crore.",
            )
        normalized[key] = parsed
    assumptions = normalized.get("assumptions")
    if isinstance(assumptions, dict):
        cleaned: dict[str, str] = {}
        sources = dict(normalized.get("assumption_sources") or {})
        for key, raw in assumptions.items():
            text = str(raw).strip()
            percent = text.endswith("%")
            if percent:
                text = text[:-1].strip()
            try:
                number = Decimal(text.replace(",", ""))
            except (InvalidOperation, ValueError) as exc:
                raise DomainError(
                    "E_PLAN_EDIT",
                    f"The assumption {key} is not a number.",
                    "Send shares as a percentage (45%) and day counts as a number (45).",
                ) from exc
            if percent or (key in STATEMENT_SHARE_KEYS and number > 1):
                number = number / Decimal("100")
            cleaned[str(key)] = format(number, "f")
            sources.setdefault(str(key), "user_explicit")
        normalized["assumptions"] = cleaned
        normalized["assumption_sources"] = sources
    return normalized


def _composite_template(intent: str, result: dict[str, Any]) -> str:
    facts = {row.get("fact_id") for row in result.get("facts", [])}
    if intent == "variance_analysis" and {"f_mcv", "f_lcv", "f_flexible"} <= facts:
        return (
            "The material cost variance is {{f_mcv}} (price {{f_mpv}}, usage {{f_muv}}). "
            "Labour is {{f_lcv}} (rate {{f_lrv}}, efficiency {{f_lev}}). Variable overhead is "
            "{{f_voh_total}} and fixed overhead is {{f_foh_total}}, giving a flexible-budget variance "
            "of {{f_flexible}}. Absorption operating income is {{f_absorption}}."
        )
    template = result.get("rationale_template")
    if template:
        return str(template)
    return fallback_template(_view(result), intent)


class Orchestrator:
    def __init__(
        self,
        service: ToolService,
        auth: AuthService | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self.service = service
        self.auth = auth or AuthService()
        self.llm = llm or LLMClient()
        self.store: PlatformStore = service.store

    # ---------- sessions ----------

    def _session(self, user: User, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise DomainError(
                "E_SESSION_NOT_FOUND",
                f"Session {session_id} was not found.",
                "Create a session at POST /v1/sessions.",
            )
        if session["user_id"] != user.user_id and not user.can("admin"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "This session belongs to another user.",
                "Open your own session, or ask an administrator to review this one.",
            )
        if session["status"] != "open":
            raise DomainError(
                "E_SESSION_CLOSED",
                "This session is closed.",
                "Create a new session to continue.",
            )
        return session

    def create_session(self, user: User, dataset_id: str | None = None) -> dict[str, Any]:
        self.auth.require(user, "read")
        dataset_id = dataset_id or self.service.snapshot.snapshot_id
        session = self.store.create_session(user.user_id, dataset_id)
        self._event("session.created", {"session_id": session["session_id"], "user_id": user.user_id})
        return {
            "session": session,
            "capability": self.service.capability(),
            "snapshot_id": self.service.snapshot.snapshot_id,
        }

    def list_sessions(self, user: User, limit: int = 20) -> list[dict[str, Any]]:
        self.auth.require(user, "read")
        rows = self.store.list_sessions(None if user.can("admin") else user.user_id, limit)
        return rows

    def session_detail(self, user: User, session_id: str) -> dict[str, Any]:
        session = self._session(user, session_id)
        return {
            "session": session,
            "messages": self.store.messages(session_id),
            "attachments": self.service.session_attachments(session_id),
            "capability": self.service.capability(),
        }

    def close_session(self, user: User, session_id: str) -> dict[str, Any]:
        self._session(user, session_id)
        self.store.close_session(session_id)
        return {"session_id": session_id, "status": "closed"}

    # ---------- attachments ----------

    def upload_attachment(
        self,
        user: User,
        filename: str,
        raw: bytes,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.auth.require(user, "read")
        if session_id:
            self._session(user, session_id)
        try:
            view = self.service.create_attachment(
                raw, filename, user=user, session_id=session_id
            )
        except AttachmentError as exc:
            raise DomainError(exc.code, exc.message, exc.remediation) from exc
        self._event(
            "attachment.created",
            {
                "attachment_id": view.get("attachment_id"),
                "name": view.get("name"),
                "kind": view.get("kind"),
                "session_id": session_id,
                "user_id": user.user_id,
            },
        )
        return view

    def attachment(self, user: User, attachment_id: str) -> dict[str, Any]:
        self.auth.require(user, "read")
        record = self.service.attachment(attachment_id)
        if record is None:
            raise DomainError(
                "E_ATTACHMENT_NOT_FOUND",
                f"Attachment {attachment_id} was not found.",
                "Upload the document again.",
            )
        if record.get("user_id") not in {None, user.user_id} and not user.can("admin"):
            raise DomainError(
                "E_AUTH_SCOPE",
                "This attachment belongs to another user.",
                "Upload your own copy of the document.",
            )
        return public_view(record, rows=20)

    def _load_attachments(
        self, user: User, attachment_ids: list[str]
    ) -> list[dict[str, Any]]:
        views = []
        for attachment_id in attachment_ids:
            record = self.service.attachment(str(attachment_id))
            if record is None:
                raise DomainError(
                    "E_ATTACHMENT_NOT_FOUND",
                    f"Attachment {attachment_id} was not found.",
                    "Upload the document again and ask the question in the same message.",
                )
            if record.get("user_id") not in {None, user.user_id} and not user.can("admin"):
                raise DomainError(
                    "E_AUTH_SCOPE",
                    "An attachment in this message belongs to another user.",
                    "Upload your own copy of the document.",
                )
            views.append(public_view(record, rows=8))
        return views

    # ---------- planning ----------

    def post_message(
        self,
        user: User,
        session_id: str,
        text: str,
        model: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self._session(user, session_id)
        self.auth.require(user, "read")
        text = (text or "").strip()
        attachment_views = self._load_attachments(user, attachment_ids or [])
        if not text and attachment_views:
            text = "Summarise the attached file."
        if not text:
            raise DomainError(
                "E_EMPTY_MESSAGE",
                "The question is empty.",
                "Ask about a variance, a special order, ABC, the budget, performance, alerts, "
                "group reporting, or an attached document.",
            )
        self.store.add_message(
            session_id,
            "user",
            text,
            attachments=[
                {
                    key: view.get(key)
                    for key in ("attachment_id", "name", "kind", "size", "pages", "ocr")
                }
                for view in attachment_views
            ],
        )
        hints: dict[str, Any] | None = None
        model_error: str | None = None
        if model and model != "deterministic":
            hints = self.llm.interpret(
                text,
                dataset_catalog(self.service.snapshot, attachment_views),
                model=model,
            )
            if hints is None:
                model_error = self.llm.last_error or "The local model did not return a usable plan."
        plan = plan_message(
            self.service.snapshot,
            text,
            session_id=session_id,
            hints=hints,
            attachments=attachment_views,
        )
        plan["question"] = text
        if model_error:
            plan["planner_note"] = (
                f"{model_error} The deterministic planner handled this question instead."
            )

        confidence = Decimal(str(plan.get("confidence", "0")))
        if confidence < Decimal("0.60") and plan.get("status") != "needs_clarification":
            plan["status"] = "needs_clarification"
            plan["clarification"] = plan.get("clarification") or {
                "question": "I am not confident this maps to a registered analysis. Which one should I run?",
                "options": [
                    "Variance analysis",
                    "ABC or customer profitability",
                    "Budget or rolling forecast",
                    "Performance, alerts, or group reporting",
                ],
            }
            plan["missing"] = plan.get("missing") or ["intent"]

        plan = self.store.save_plan(plan, user.user_id)
        self._event(
            "plan.created",
            {
                "plan_id": plan["plan_id"],
                "session_id": session_id,
                "intent": plan.get("intent"),
                "tool": plan.get("tool"),
                "confidence": plan.get("confidence"),
                "missing": plan.get("missing", []),
            },
        )

        parameter_sheet = self.parameter_sheet(plan)
        status = plan.get("status", "ready")
        assistant_text = self._plan_message(plan, parameter_sheet, status)
        stored_assistant = self.store.add_message(
            session_id, "assistant", assistant_text, plan_id=plan["plan_id"]
        )

        response: dict[str, Any] = {
            "session_id": session_id,
            "message": stored_assistant,
            "attachments": attachment_views,
            "plan_id": plan["plan_id"],
            "status": status,
            "plan": {
                "intent": plan.get("intent"),
                "tool": plan.get("tool"),
                "tool_version": plan.get("tool_version"),
                "arguments": plan.get("arguments", {}),
                "assumptions": plan.get("assumptions", []),
                "confidence": plan.get("confidence"),
                "requires_confirmation": plan.get("requires_confirmation", False),
                "rationale": plan.get("rationale"),
                "planner": plan.get("planner") or "deterministic",
                "planner_reason": plan.get("planner_reason"),
                "planner_note": plan.get("planner_note"),
                "model": plan.get("model"),
            },
            "planner": plan.get("planner") or "deterministic",
            "model": plan.get("model"),
            "planner_note": plan.get("planner_note"),
            "parameter_sheet": parameter_sheet,
            "missing": plan.get("missing", []),
            "clarification": plan.get("clarification"),
            "requires_confirmation": plan.get("requires_confirmation", False),
            "next_actions": self._next_actions(plan, status),
        }
        if status == "ready" and plan.get("intent") in AUTO_INTENTS:
            response["result"] = self._auto_answer(user, plan)
        self._event("plan.status", {"plan_id": plan["plan_id"], "status": status})
        return response

    def _plan_message(self, plan: dict[str, Any], sheet: list[dict[str, Any]], status: str) -> str:
        intent = plan.get("intent", "clarify")
        label = INTENT_LABELS.get(intent, intent.replace("_", " ").title())
        if status == "needs_clarification":
            clarification = plan.get("clarification") or {}
            return clarification.get("question", "I need one clarification before running this analysis.")
        if status == "needs_confirmation" or plan.get("requires_confirmation"):
            return (
                f"I will run {label} using the pinned snapshot. Review the parameter sheet, "
                "then confirm. Nothing has been calculated yet."
            )
        return f"I am ready to run {label}. Review the resolved parameters and start the run."

    def _next_actions(self, plan: dict[str, Any], status: str) -> list[dict[str, str]]:
        if status == "needs_clarification":
            return [{"action": "answer_clarification", "label": "Choose an interpretation"}]
        if status == "needs_confirmation":
            return [
                {"action": "confirm_plan", "label": "Confirm and run"},
                {"action": "cancel_plan", "label": "Cancel plan"},
            ]
        return [
            {"action": "confirm_plan", "label": "Run analysis"},
            {"action": "cancel_plan", "label": "Cancel plan"},
        ]

    def parameter_sheet(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        arguments = plan.get("arguments", {}) or {}
        assumptions = {item.get("key"): item for item in plan.get("assumptions", [])}
        rows: list[dict[str, Any]] = []
        label_map = dict(LABELS)

        if plan.get("intent") == "special_order":
            resolved, resolved_sheet = self.service.resolve_special_order(arguments)
            allowed = {
                "period_id", "plant_id", "product_id", "order_units", "order_price",
                "incremental_fixed_cost", "capacity_state", "variable_selling_cost_avoided",
            }
            scope_sources = {key: item.get("source") for key, item in assumptions.items()}
            for key in ("period_id", "plant_id", "product_id"):
                rows.append(
                    {
                        "key": key,
                        "label": label_map[key],
                        "value": resolved.get(key),
                        "source": scope_sources.get(key, "lookup"),
                        "confirm": False,
                        "editable": True,
                    }
                )
            for row in resolved_sheet:
                if row["key"] not in allowed or row["key"] in {"period_id", "plant_id", "product_id"}:
                    continue
                rows.append(
                    {
                        "key": row["key"],
                        "label": label_map.get(row["key"], row["label"]),
                        "value": row["value"],
                        "source": row["source"],
                        "confirm": row["source"] in {"user_implied", "policy_default", "engine_default", "demo_default"},
                        "editable": True,
                    }
                )
            if "capacity_state" not in {row["key"] for row in rows}:
                capacity_default = str(resolved.get("capacity_state", "idle"))
                rows.append(
                    {
                        "key": "capacity_state",
                        "label": label_map["capacity_state"],
                        "value": capacity_default,
                        "source": "policy_default" if capacity_default == "idle" else "engine_default",
                        "confirm": True,
                        "editable": True,
                    }
                )
            utilization = _to_decimal(resolved.get("utilization", "0"))
            capacity = str(resolved.get("capacity_state", "idle"))
            if capacity == "constrained":
                idle = _to_decimal(resolved.get("idle_capacity_units", "0"))
                order_units = _to_decimal(resolved.get("order_units", "0"))
                for row in rows:
                    if row["key"] == "capacity_state":
                        row["warning"] = (
                            f"Spare capacity ({idle:,.0f} units) is below the order "
                            f"({order_units:,.0f} units), so displaced sales carry an "
                            "opportunity cost. Set idle capacity only if the plant can absorb the order."
                        )
                        row["confirm"] = True
            elif utilization > Decimal("0.95"):
                for row in rows:
                    if row["key"] == "capacity_state":
                        row["warning"] = (
                            f"Reported utilization is {utilization * 100:.1f}% for this plant. "
                            "Confirm the spare-capacity claim or switch to constrained."
                        )
                        row["confirm"] = True
            return rows

        if plan.get("intent") == "financial_statements":
            sources = arguments.get("assumption_sources") or {}
            currency = str(arguments.get("currency", "INR"))
            if arguments.get("attachment_id"):
                record = self.service.attachment(str(arguments["attachment_id"]))
                rows.append(
                    {
                        "key": "attachment_id",
                        "label": label_map["attachment_id"],
                        "value": record.get("name") if record else arguments["attachment_id"],
                        "source": assumptions.get("attachment_id", {}).get("source", "lookup"),
                        "confirm": False,
                        "editable": False,
                    }
                )
            for key in ("turnover", "prior_turnover"):
                value = arguments.get(key)
                if value in (None, ""):
                    continue
                rows.append(
                    {
                        "key": key,
                        "label": label_map[key],
                        "value": display_amount(_to_decimal(value), currency),
                        "source": assumptions.get(key, {}).get("source", "user_explicit"),
                        "confirm": False,
                        "editable": True,
                    }
                )
            mode = str(arguments.get("mode", "auto"))
            rows.append(
                {
                    "key": "mode",
                    "label": label_map["mode"],
                    "value": {
                        "auto": "Reported file if present, else modelled from turnover",
                        "reported": "Reported statements from the file",
                        "model": "Modelled from the stated turnover",
                    }.get(mode, mode),
                    "source": "engine_default",
                    "confirm": mode == "auto",
                    "editable": True,
                }
            )
            rows.append(
                {
                    "key": "currency",
                    "label": label_map["currency"],
                    "value": currency,
                    "source": assumptions.get("currency", {}).get("source", "engine_default"),
                    "confirm": False,
                    "editable": True,
                }
            )
            if arguments.get("period"):
                rows.append(
                    {
                        "key": "period",
                        "label": label_map["period"],
                        "value": arguments["period"],
                        "source": assumptions.get("period", {}).get("source", "user_explicit"),
                        "confirm": False,
                        "editable": True,
                    }
                )
            for key, value in (arguments.get("assumptions") or {}).items():
                number = _to_decimal(value)
                shown = (
                    f"{number * 100:.1f}%"
                    if key in STATEMENT_SHARE_KEYS
                    else f"{number:.0f} days"
                )
                rows.append(
                    {
                        "key": f"assumptions.{key}",
                        "label": key.replace("_", " ").title(),
                        "value": shown,
                        "source": sources.get(key, "user_explicit"),
                        "confirm": False,
                        "editable": True,
                    }
                )
            return rows

        for key, value in arguments.items():
            if key in {"confirmed", "decision_type"}:
                continue
            assumption = assumptions.get(key)
            source = assumption.get("source") if assumption else (
                "lookup" if key.endswith("_id") else "user_explicit"
            )
            if plan.get("intent") == "attachment_analysis" and key == "attachment_id":
                record = self.service.attachment(str(value))
                rows.append(
                    {
                        "key": key,
                        "label": label_map[key],
                        "value": record.get("name") if record else value,
                        "source": source or "user_explicit",
                        "confirm": False,
                        "editable": False,
                    }
                )
                continue
            if plan.get("intent") == "attachment_analysis" and key in {
                "table", "column", "compare_column", "limit", "direction"
            }:
                source = "lookup"
            if key == "overlay" and isinstance(value, dict):
                for driver, change in value.items():
                    rows.append(
                        {
                            "key": f"overlay.{driver}",
                            "label": f"{driver.replace('_', ' ').title()} change",
                            "value": f"{_to_decimal(change) * 100}%",
                            "source": "user_explicit",
                            "confirm": True,
                            "editable": True,
                        }
                    )
                continue
            rows.append(
                {
                    "key": key,
                    "label": label_map.get(key, key.replace("_", " ").title()),
                    "value": value if not isinstance(value, list) else "; ".join(str(item) for item in value),
                    "source": source or "engine_default",
                    "confirm": bool(assumption.get("confirm")) if assumption else source not in {"lookup", "registry"},
                    "editable": True,
                }
            )
        for field in plan.get("missing", []) or []:
            if field in {row["key"] for row in rows}:
                continue
            rows.append(
                {
                    "key": field,
                    "label": label_map.get(field, field.replace("_", " ").title()),
                    "value": "Not provided",
                    "source": "missing",
                    "confirm": True,
                    "editable": True,
                }
            )
        return rows

    # ---------- confirmation and execution ----------

    def confirm_plan(
        self,
        user: User,
        plan_id: str,
        *,
        edits: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
        plan_version: int | None = None,
    ) -> dict[str, Any]:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise DomainError(
                "E_PLAN_NOT_FOUND",
                f"Plan {plan_id} was not found.",
                "Ask the question again to create a new plan.",
            )
        if plan.get("user_id") and plan["user_id"] != user.user_id and not user.can("admin"):
            raise DomainError("E_AUTH_SCOPE", "This plan belongs to another user.", "Create your own plan.")
        if plan.get("status") == "cancelled":
            raise DomainError("E_PLAN_CANCELLED", "This plan was cancelled.", "Ask the question again to create a new plan.")
        if plan.get("status") == "completed":
            existing = self.store.get_object("run_card", str(plan.get("run_id")))
            if existing:
                return {"run_id": plan.get("run_id"), "status": "completed", "card": existing, "idempotent": True}
            raise DomainError("E_PLAN_COMPLETED", "This plan has already run.", "Open the existing run or ask a new question.")
        if plan.get("status") == "needs_clarification":
            clarification = plan.get("clarification") or {}
            options = clarification.get("options") or []
            raise DomainError(
                "E_PLAN_AMBIGUOUS",
                clarification.get("question") or "This plan still needs a clarification before it can run.",
                (
                    "Answer with one of: " + ", ".join(str(item) for item in options)
                    if options
                    else "Ask the question again with an explicit intent, period, plant and product."
                ),
            )

        self.auth.require(user, "run")
        if plan.get("requires_confirmation"):
            self.auth.require(user, "confirm")

        key = idempotency_key or f"{plan_id}:v{plan_version or 1}"
        cached = self.store.get_object("idempotency", key)
        if cached:
            if cached.get("plan_id") != plan_id:
                raise DomainError(
                    "E_IDEMPOTENCY_CONFLICT",
                    "This idempotency key belongs to a different plan.",
                    "Use a new idempotency key for this confirmation.",
                )
            card = self.store.get_object("run_card", str(cached["run_id"]))
            return {"run_id": cached["run_id"], "status": "completed", "card": card, "idempotent": True}

        current_version = int(plan.get("plan_version", 1))
        edits = edits or []
        proposed_arguments = dict(plan.get("arguments", {}) or {})
        applied: list[dict[str, Any]] = []
        for edit in edits:
            field = str(edit.get("key", ""))
            if not field:
                raise DomainError("E_PLAN_EDIT", "An edit has no key.", "Send edits as [{key, value, reason}].")
            proposed = _lookup(proposed_arguments, field)
            _apply_edit(proposed_arguments, field, edit.get("value"))
            applied.append(
                {
                    "key": field,
                    "proposed": _scalar(proposed),
                    "confirmed": _scalar(edit.get("value")),
                    "reason": edit.get("reason"),
                }
            )
        if plan.get("intent") == "special_order":
            proposed_arguments["confirmed"] = True
        if plan.get("intent") == "financial_statements":
            proposed_arguments = _normalize_statement_arguments(proposed_arguments)
        if plan.get("intent") == "budget_scenario" and "overlay" not in proposed_arguments:
            raise DomainError(
                "E_ASSUMPTION_UNCONFIRMED",
                "The scenario has no overlay to confirm.",
                "Add at least one driver change, for example volume -2% or material price 3%.",
            )

        version = current_version + (1 if applied else 0)
        plan = self.store.update_plan(
            plan_id,
            status="running",
            plan_version=version,
            arguments=proposed_arguments,
            user_edits=applied,
            confirmed_by=user.user_id,
            confirmed_at=_now(),
        )
        self._event("plan.confirmed", {"plan_id": plan_id, "plan_version": version, "user_id": user.user_id})

        confirmation = {
            "required": bool(plan.get("requires_confirmation")),
            "confirmed": True,
            "confirmed_by": user.user_id,
            "confirmed_at": _now(),
            "plan_version": version,
            "edits": applied,
        }
        envelope = self._execute(user, plan, confirmation)
        run_id = (envelope.get("trace_ref") or {}).get("run_id")
        narration = self._narration(plan, envelope)
        card = self._card(plan, envelope, narration, user)
        assistant_text = self._assistant_text(plan, card, narration)
        self.store.put_object("run_card", run_id, card)
        self.store.put_object(
            "idempotency",
            key,
            {"plan_id": plan_id, "run_id": run_id, "created_at": _now()},
        )
        self.store.update_plan(plan_id, status="completed", run_id=run_id, card_id=run_id)
        self.store.add_message(
            plan["session_id"],
            "assistant",
            assistant_text,
            plan_id=plan_id,
            run_id=run_id,
        )
        self._record_run_events(run_id, plan, envelope)
        return {
            "run_id": run_id,
            "status": "completed",
            "card": card,
            "assistant_text": assistant_text,
            "card_url": f"/v1/runs/{run_id}",
            "events": f"/v1/runs/{run_id}/events",
            "idempotent": False,
        }

    def _assistant_text(
        self,
        plan: dict[str, Any],
        card: dict[str, Any],
        narration: dict[str, Any],
    ) -> str:
        fallback = f"{card['verdict']['sentence']} {narration['text']}"
        model = plan.get("model")
        if not model or model == "deterministic":
            return fallback
        text = self.llm.narrate(card, model=model)
        return text or fallback

    def cancel_plan(self, user: User, plan_id: str, reason: str | None = None) -> dict[str, Any]:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise DomainError("E_PLAN_NOT_FOUND", f"Plan {plan_id} was not found.", "Ask the question again.")
        if plan.get("user_id") and plan["user_id"] != user.user_id and not user.can("admin"):
            raise DomainError("E_AUTH_SCOPE", "This plan belongs to another user.", "Create your own plan.")
        if plan.get("status") == "completed":
            raise DomainError(
                "E_PLAN_COMPLETED",
                "This plan has already run and cannot be cancelled.",
                "Open the run instead.",
            )
        plan = self.store.update_plan(plan_id, status="cancelled", cancel_reason=reason, cancelled_by=user.user_id)
        self._event("plan.cancelled", {"plan_id": plan_id, "reason": reason})
        return {"plan_id": plan_id, "status": "cancelled"}

    def _execute(self, user: User, plan: dict[str, Any], confirmation: dict[str, Any]) -> dict[str, Any]:
        intent = plan.get("intent")
        args = plan.get("arguments", {}) or {}
        svc = self.service
        common = {"question": plan.get("question"), "user": user, "plan_id": plan.get("plan_id")}
        record = intent not in READ_ONLY_INTENTS

        if intent == "attachment_analysis":
            return svc.run_attachment_analysis(
                attachment_id=str(args.get("attachment_id", "")),
                operation=str(args.get("operation", "profile")),
                table=args.get("table"),
                column=args.get("column"),
                compare_column=args.get("compare_column"),
                query=args.get("query"),
                limit=int(args.get("limit", 5)),
                direction=str(args.get("direction", "top")),
                **common,
            )

        if intent == "financial_statements":
            return svc.run_financial_statements(
                attachment_id=args.get("attachment_id"),
                turnover=args.get("turnover"),
                prior_turnover=args.get("prior_turnover"),
                currency=str(args.get("currency", "INR")),
                period=args.get("period"),
                mode=str(args.get("mode", "auto")),
                assumptions=args.get("assumptions") or {},
                assumption_sources=args.get("assumption_sources") or {},
                **common,
            )

        if intent == "special_order":
            decision = str(args.get("decision_type", "special_order"))
            payload = dict(args)
            payload.pop("decision_type", None)
            payload.pop("confirmed", None)
            payload.pop("connector", None)
            return svc.run_differential(
                decision_type=decision,
                arguments=payload,
                confirmation=confirmation,
                **common,
            )
        if intent == "variance_analysis":
            return svc.run_variance(
                product_id=args.get("product_id"), plant_id=args.get("plant_id"),
                period_id=args.get("period_id"), **common,
            )
        if intent == "costing_bridge":
            return svc.run_bridge(
                product_id=args.get("product_id"), plant_id=args.get("plant_id"),
                period_id=args.get("period_id"),
                variable_operating_income=args.get("variable_operating_income"), **common,
            )
        if intent == "abc_profitability":
            return svc.run_abc_profitability(
                period_id=args.get("period_id"), plant_id=args.get("plant_id"),
                basis=args.get("basis", "labor_hours"), **common,
            )
        if intent == "customer_profitability":
            return svc.run_customer_profitability(
                period_id=args.get("period_id"), plant_id=args.get("plant_id"),
                customer_id=args.get("customer_id"), **common,
            )
        if intent == "tdabc":
            return svc.run_tdabc(
                period_id=args.get("period_id"), plant_id=args.get("plant_id"), **common,
            )
        if intent == "budget_chain":
            return svc.run_budget(
                period_id=args.get("period_id"), plant_id=args.get("plant_id"), **common,
            )
        if intent == "budget_scenario":
            return svc.run_scenario(
                period_id=args.get("period_id"), plant_id=args.get("plant_id"),
                overlay=args.get("overlay") or {}, label=args.get("label", "Scenario"), **common,
            )
        if intent == "rolling_forecast":
            return svc.run_rolling_forecast(plant_id=args.get("plant_id"), **common)
        if intent == "forecast_quality":
            return svc.run_forecast_quality(plant_id=args.get("plant_id"), **common)
        if intent == "performance_eval":
            return svc.run_performance(
                period_id=args.get("period_id"), division_id=args.get("division_id"), **common,
            )
        if intent == "monitoring":
            return svc.run_monitoring(
                period_id=args.get("period_id"), rules=args.get("rules") or [],
                materiality=str(args.get("materiality", "50000")), **common,
            )
        if intent == "group_report":
            return svc.run_group_report(period_id=args.get("period_id"), **common)
        if intent == "connector_preview":
            return svc.run_connector_tool(name=str(args.get("connector", "netsuite")), record=record, **common)
        if intent == "explain_run":
            return svc.trace(str(args.get("run_id", "")))  # type: ignore[arg-type]
        if intent == "audit_verify":
            return svc.verify_chain()
        if intent == "describe_dataset":
            return svc.capability()
        raise DomainError(
            "E_PLAN_UNSUPPORTED",
            f"Intent {intent!r} has no registered tool.",
            "Try a supported analysis: variance, special order, ABC, budget, performance, alerts, or group.",
        )

    def _auto_answer(self, user: User, plan: dict[str, Any]) -> dict[str, Any]:
        intent = plan.get("intent")
        if intent == "describe_dataset":
            capability = self.service.capability()
            return {
                "type": "capability",
                "capability": capability,
                "text": (
                    f"{capability['dataset']} is loaded at snapshot {capability['snapshot_id']}. "
                    f"Analysis readiness: {', '.join(capability['analysis_ready'])}."
                ),
            }
        if intent == "audit_verify":
            return {"type": "audit", "verification": self.service.verify_chain()}
        if intent == "explain_run":
            run_id = str(plan["arguments"].get("run_id", ""))
            try:
                run = self.service.trace(run_id)
            except DomainError as exc:
                return {
                    "type": "error",
                    "error": {"code": exc.code, "message": exc.message},
                    "remediation": [exc.remediation] if exc.remediation else ["Open the audit drawer and copy a run id."],
                }
            return {"type": "trace", "trace": run}
        return {"type": "none"}

    # ---------- card assembly ----------

    def _narration(self, plan: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        result = dict(envelope.get("result", {}) or {})
        result["facts"] = list(result.get("facts") or envelope.get("facts") or [])
        if not result.get("steps"):
            result["steps"] = [
                {"step_id": step.get("step_id"), "rule_id": step.get("rule_id"), "label": step.get("label")}
                for step in (envelope.get("facts") or [])[:1]
            ]
        template = _composite_template(str(plan.get("intent")), result)
        return render_validated(template, _view(result), intent=str(plan.get("intent")))

    def _card(
        self,
        plan: dict[str, Any],
        envelope: dict[str, Any],
        narration: dict[str, Any],
        user: User,
    ) -> dict[str, Any]:
        result = envelope.get("result", {}) or {}
        run_id = (envelope.get("trace_ref") or {}).get("run_id")
        intent = str(plan.get("intent"))
        args = plan.get("arguments", {}) or {}
        meta = result.get("meta", {}) or {}
        verdict_text = _verdict_sentence(intent, result, envelope.get("facts"))
        decision = _verdict_class(result.get("verdict"), result.get("favorable"))
        if intent == "variance_analysis" and result.get("verdict") is None:
            if "both adverse" in verdict_text:
                decision = "adverse"
            elif "both favourable" in verdict_text:
                decision = "favorable"
        if intent == "financial_statements":
            headline = meta.get("net_income") or result.get("value")
            if headline is not None:
                decision = "favorable" if _to_decimal(headline) > 0 else "adverse"
        tables = self._tables(intent, result)
        facts = [
            {
                **fact,
                "display": self._display_fact(fact),
            }
            for fact in envelope.get("facts", [])[:8]
        ]
        return {
            "run_id": run_id,
            "plan_id": plan.get("plan_id"),
            "session_id": plan.get("session_id"),
            "status": "completed",
            "intent": intent,
            "intent_label": INTENT_LABELS.get(intent, intent),
            "question": plan.get("question"),
            "header": {
                "question": plan.get("question"),
                "period_id": (
                    meta.get("period_id")
                    or meta.get("period")
                    or args.get("period_id")
                    or args.get("period")
                ),
                "plant_id": meta.get("plant_id") or args.get("plant_id") or "ALL",
                "product_id": meta.get("product_id") or args.get("product_id") or "ALL",
                "customer_id": meta.get("customer_id") or args.get("customer_id"),
                "division_id": meta.get("division_id") or args.get("division_id"),
                "basis": meta.get("basis") or args.get("basis"),
                "attachment_id": meta.get("attachment_id") or args.get("attachment_id"),
                "attachment_name": meta.get("attachment_name"),
                "prior_period": meta.get("prior_period"),
                "statement_periods": meta.get("statement_periods") or [],
                "statement_mode": meta.get("mode"),
                "snapshot_id": self.service.snapshot.snapshot_id,
                "dataset": self.service.snapshot.dataset_name,
                "currency": meta.get("currency") or self.service.snapshot.currency,
                "status": "completed",
            },
            "verdict": {
                "decision": decision,
                "sentence": verdict_text,
                "favorable": result.get("favorable"),
            },
            "facts": facts,
            "tables": tables,
            "assumptions": envelope.get("assumptions", []),
            "warnings": envelope.get("warnings", []),
            "conditions": result.get("conditions", []),
            "sensitivity": result.get("sensitivity", []),
            "rationale": narration,
            "remediation": envelope.get("remediation", []),
            "audit": {
                "trace": f"/v1/runs/{run_id}/trace" if run_id else None,
                "reperform": f"/v1/runs/{run_id}/reperform" if run_id else None,
            },
            "exports": {
                "xlsx": f"/v1/runs/{run_id}/export?format=xlsx" if run_id else None,
                "pdf": f"/v1/runs/{run_id}/export?format=pdf" if run_id else None,
                "json": f"/v1/runs/{run_id}/export?format=json" if run_id else None,
            },
            "pins": envelope.get("pins", {}),
            "user": user.public(),
            "created_at": _now(),
            "suggested_questions": SUGGESTIONS.get(intent, SUGGESTIONS.get("variance", [])),
            "validation": envelope.get("validation", {}),
        }

    @staticmethod
    def _display_fact(fact: dict[str, Any]) -> str:
        try:
            return display_amount(Decimal(str(fact.get("value", "0"))), str(fact.get("unit", "")))
        except (InvalidOperation, ValueError):
            return str(fact.get("display", fact.get("value", "")))

    def _tables(self, intent: str, result: dict[str, Any]) -> list[dict[str, Any]]:
        meta = result.get("meta", {}) or {}
        tables: list[dict[str, Any]] = []

        def add(table_id: str, title: str, rows: list[dict[str, Any]], columns: list[dict[str, str]]) -> None:
            if rows:
                tables.append(
                    {
                        "id": table_id,
                        "title": title,
                        "columns": columns,
                        "rows": [_scalar(row) for row in rows],
                    }
                )

        if intent == "financial_statements":
            statements = meta.get("statements") or {}
            for key, title in (
                ("income", "Income statement"),
                ("balance", "Balance sheet"),
                ("cashflow", "Cash flow statement"),
            ):
                add(
                    f"statement_{key}",
                    title,
                    statements.get(key) or [],
                    [
                        {"key": "label", "label": "Line", "type": "text"},
                        {"key": "display", "label": "Current", "type": "text"},
                        {"key": "prior_display", "label": "Prior", "type": "text"},
                        {"key": "rule", "label": "Rule", "type": "text"},
                    ],
                )
            add(
                "ratios",
                "Ratio analysis",
                meta.get("ratios") or [],
                [
                    {"key": "name", "label": "Ratio", "type": "text"},
                    {"key": "display", "label": "Current", "type": "text"},
                    {"key": "prior_display", "label": "Prior", "type": "text"},
                    {"key": "rule", "label": "Rule", "type": "text"},
                    {"key": "formula", "label": "Formula", "type": "text"},
                ],
            )
            add(
                "observations",
                "What the numbers show",
                [{"note": note} for note in (meta.get("observations") or [])],
                [{"key": "note", "label": "Observation", "type": "text"}],
            )

        if intent != "financial_statements" and result.get("steps"):
            add(
                    "steps",
                    "Calculation chain",
                    [
                        {
                            "step": row.get("step_id"),
                            "label": row.get("label"),
                            "rule": row.get("rule_id"),
                            "formula": row.get("formula"),
                            "value": row.get("value"),
                            "unit": row.get("unit"),
                        }
                        for row in result.get("steps", [])
                    ],
                    [
                        {"key": "step", "label": "Step", "type": "text"},
                        {"key": "label", "label": "Line", "type": "text"},
                        {"key": "rule", "label": "Rule", "type": "text"},
                        {"key": "formula", "label": "Formula", "type": "text"},
                        {"key": "value", "label": "Value", "type": "number"},
                        {"key": "unit", "label": "Unit", "type": "text"},
                    ],
                )

        if intent == "attachment_analysis":
            if meta.get("profile"):
                add(
                    "doc_profile",
                    "Column profile",
                    meta["profile"],
                    [
                        {"key": "column", "label": "Column", "type": "text"},
                        {"key": "count", "label": "Values", "type": "qty"},
                        {"key": "sum", "label": "Total", "type": "number"},
                        {"key": "mean", "label": "Average", "type": "number"},
                        {"key": "min", "label": "Lowest", "type": "number"},
                        {"key": "max", "label": "Highest", "type": "number"},
                        {"key": "unit", "label": "Unit", "type": "text"},
                    ],
                )
            headers = [header for header in (meta.get("headers") or []) if header]
            rows = meta.get("rows") or []
            if rows and headers:
                columns = [{"key": "row", "label": "Row", "type": "text"}]
                columns += [{"key": header, "label": header, "type": "text"} for header in headers]
                add("doc_rows", "Rows read from the file", rows, columns)
            elif rows:
                first = rows[0]
                columns = [
                    {"key": key, "label": str(key).title(), "type": "text"}
                    for key in first
                ]
                add("doc_rows", "Rows read from the file", rows, columns)
            if meta.get("figures"):
                add(
                    "doc_figures",
                    "Figures printed in the file",
                    meta["figures"],
                    [
                        {"key": "printed", "label": "Printed as", "type": "text"},
                        {"key": "display", "label": "Read as", "type": "text"},
                        {"key": "unit", "label": "Unit", "type": "text"},
                        {"key": "context", "label": "Context", "type": "text"},
                    ],
                )
            if meta.get("quotes"):
                add(
                    "doc_quotes",
                    "Passages used",
                    [{"passage": quote} for quote in meta["quotes"]],
                    [{"key": "passage", "label": "Passage", "type": "text"}],
                )

        if intent == "variance_analysis":
            components = result.get("components", {}) or {}
            for name, component in components.items():
                add(
                    f"variance_{name}",
                    name.replace("_", " ").title(),
                    [
                        {
                            "step": row.get("step_id"),
                            "label": row.get("label"),
                            "rule": row.get("rule_id"),
                            "value": row.get("value"),
                            "unit": row.get("unit"),
                        }
                        for row in component.get("steps", [])
                    ],
                    [
                        {"key": "step", "label": "Step", "type": "text"},
                        {"key": "label", "label": "Line", "type": "text"},
                        {"key": "rule", "label": "Rule", "type": "text"},
                        {"key": "value", "label": "Value", "type": "number"},
                        {"key": "unit", "label": "Unit", "type": "text"},
                    ],
                )

        if meta.get("products"):
            rows = meta["products"]
            columns = _columns_for(rows, {
                "product_id": ("Product", "text"),
                "plant_id": ("Plant", "text"),
                "output_units": ("Output", "qty"),
                "sales_units": ("Sales units", "qty"),
                "production_units": ("Production", "qty"),
                "price": ("Price", "money"),
                "abc_overhead": ("ABC overhead", "money"),
                "traditional_overhead": ("Traditional overhead", "money"),
                "abc_unit_cost": ("ABC unit cost", "money"),
                "traditional_unit_cost": ("Traditional unit cost", "money"),
                "abc_margin": ("ABC margin", "money"),
                "distortion": ("Distortion", "money"),
                "distortion_ratio": ("Distortion %", "ratio"),
                "cross_subsidised": ("Cross-subsidised", "bool"),
                "operating_income": ("Operating income", "money"),
                "material_cost": ("Material", "money"),
                "labour": ("Labour", "money"),
                "variable_oh": ("Variable OH", "money"),
                "fixed_oh": ("Fixed OH", "money"),
                "selling": ("Selling", "money"),
            })
            add("products", "Product profitability", rows, columns)

        if meta.get("pools"):
            add(
                "pools",
                "Cost pools and driver rates",
                meta["pools"],
                _columns_for(meta["pools"], {
                    "pool_id": ("Pool", "text"),
                    "activity_id": ("Activity", "text"),
                    "pool_cost": ("Pool cost", "money"),
                    "driver_quantity": ("Driver quantity", "qty"),
                    "rate": ("Rate", "money"),
                    "capacity_rate": ("Capacity rate", "money"),
                    "assigned": ("Assigned", "money"),
                    "unused": ("Unused", "money"),
                    "utilisation": ("Utilisation", "ratio"),
                }),
            )

        if meta.get("customers"):
            add(
                "customers",
                "Customer profitability",
                meta["customers"],
                _columns_for(meta["customers"], {
                    "customer_id": ("Customer", "text"),
                    "name": ("Name", "text"),
                    "units": ("Units", "qty"),
                    "revenue": ("Revenue", "money"),
                    "product_cost": ("Product cost", "money"),
                    "cost_to_serve": ("Cost to serve", "money"),
                    "net_margin": ("Net margin", "money"),
                    "margin_ratio": ("Margin %", "ratio"),
                    "orders": ("Orders", "qty"),
                    "service_tickets": ("Service tickets", "qty"),
                    "deliveries": ("Deliveries", "qty"),
                    "loss_making": ("Loss-making", "bool"),
                }),
            )

        if meta.get("kpis"):
            add(
                "kpis",
                "Weighted scorecard",
                meta["kpis"],
                _columns_for(meta["kpis"], {
                    "kpi_id": ("KPI", "text"),
                    "name": ("Name", "text"),
                    "perspective": ("Perspective", "text"),
                    "owner": ("Owner", "text"),
                    "weight": ("Weight", "ratio"),
                    "target": ("Target", "number"),
                    "actual": ("Actual", "number"),
                    "direction": ("Direction", "text"),
                    "controllability": ("Controllability", "text"),
                    "score": ("Score", "number"),
                    "missed": ("Missed", "bool"),
                }),
            )

        if meta.get("alerts"):
            add(
                "alerts",
                "Alert digest",
                meta["alerts"],
                _columns_for(meta["alerts"], {
                    "rule": ("Rule", "text"),
                    "metric": ("Metric", "text"),
                    "plant_id": ("Plant", "text"),
                    "value": ("Value", "number"),
                    "threshold": ("Threshold", "number"),
                    "comparator": ("Comparator", "text"),
                    "severity": ("Severity", "text"),
                    "triggered": ("Triggered", "bool"),
                    "material": ("Material", "bool"),
                    "alert": ("Alert", "text"),
                    "impact": ("Impact", "money"),
                }),
            )

        if meta.get("intercompany"):
            add(
                "intercompany",
                "Intercompany pairs",
                meta["intercompany"],
                _columns_for(meta["intercompany"], {
                    "seller_entity": ("Seller", "text"),
                    "buyer_entity": ("Buyer", "text"),
                    "description": ("Description", "text"),
                    "revenue": ("Revenue", "money"),
                    "cost": ("Cost", "money"),
                    "status": ("Status", "text"),
                }),
            )

        if meta.get("entities"):
            add(
                "entities",
                "Entity roll-up",
                meta["entities"],
                _columns_for(meta["entities"], {
                    "entity_id": ("Entity", "text"),
                    "revenue": ("Revenue", "money"),
                    "income": ("Operating income", "money"),
                }),
            )

        if meta.get("samples"):
            add(
                "samples",
                "Forecast quality samples",
                meta["samples"],
                _columns_for(meta["samples"], {
                    "period_id": ("Period", "text"),
                    "plant_id": ("Plant", "text"),
                    "product_id": ("Product", "text"),
                    "budget_revenue": ("Budget revenue", "money"),
                    "actual_revenue": ("Actual revenue", "money"),
                    "bias": ("Bias", "ratio"),
                    "sandbagging": ("Sandbagging", "bool"),
                }),
            )

        if intent == "budget_chain" and meta.get("products"):
            add(
                "budget_products",
                "Budget by product",
                meta["products"],
                _columns_for(meta["products"], {
                    "product_id": ("Product", "text"),
                    "plant_id": ("Plant", "text"),
                    "sales_units": ("Sales units", "qty"),
                    "price": ("Price", "money"),
                    "production_units": ("Production", "qty"),
                    "material_cost": ("Material", "money"),
                    "labour": ("Labour", "money"),
                    "variable_oh": ("Variable OH", "money"),
                    "fixed_oh": ("Fixed OH", "money"),
                    "selling": ("Selling", "money"),
                    "operating_income": ("Operating income", "money"),
                }),
            )

        return tables

    # ---------- run access ----------

    def get_run(self, user: User, run_id: str) -> dict[str, Any]:
        card = self.store.get_object("run_card", run_id)
        if card is not None:
            self._check_run_access(user, card)
            return card
        run = self.service.audit.get(run_id)
        if run is None:
            raise DomainError(
                "RUN_NOT_FOUND",
                f"Run {run_id} was not found.",
                "Open a run from the audit drawer or list recent runs.",
            )
        self._check_run_access(user, run)
        return {
            "run_id": run_id,
            "status": "completed",
            "question": run.get("question"),
            "intent": run.get("intent"),
            "result": run.get("result"),
            "validation": run.get("validation"),
            "pins": run.get("pins"),
            "integrity": run.get("integrity"),
        }

    def _check_run_access(self, user: User, run: dict[str, Any]) -> None:
        owner = (run.get("user") or {}).get("user_id")
        if owner not in {None, user.user_id, "system"} and not user.can("admin") and user.role != "controller":
            raise DomainError(
                "E_AUTH_SCOPE",
                "This run belongs to another user.",
                "Ask an administrator for access, or open your own run.",
            )

    def trace(self, user: User, run_id: str) -> dict[str, Any]:
        self.auth.require(user, "read")
        trace = self.service.trace(run_id)
        card = self.store.get_object("run_card", run_id)
        if card:
            self._check_run_access(user, card)
        return trace

    def reperform(self, user: User, run_id: str) -> dict[str, Any]:
        self.auth.require(user, "run")
        card = self.store.get_object("run_card", run_id)
        if card:
            self._check_run_access(user, card)
        return self.service.reperform(run_id)

    def list_runs(self, user: User, limit: int = 30) -> list[dict[str, Any]]:
        self.auth.require(user, "read")
        return self.service.audit.recent_runs(limit, None if user.role in {"admin", "controller"} else user.user_id)

    def export_payload(self, user: User, run_id: str) -> dict[str, Any]:
        self.auth.require(user, "export")
        card = self.store.get_object("run_card", run_id)
        run = self.service.audit.get(run_id)
        if card is None or run is None:
            raise DomainError(
                "RUN_NOT_FOUND",
                f"Run {run_id} was not found.",
                "Open a run from the audit drawer and retry the export.",
            )
        self._check_run_access(user, card)
        return {"card": card, "run": run, "pins": run.get("pins"), "validation": run.get("validation")}

    # ---------- events ----------

    def _event(self, kind: str, payload: dict[str, Any]) -> None:
        event_id = "evt_" + secrets.token_hex(6)
        self.store.put_object(
            "event",
            event_id,
            {"event_id": event_id, "kind": kind, "payload": payload, "created_at": _now()},
        )
        sequence = self.store.get_object("event_seq", "global") or {"value": 0}
        sequence["value"] = int(sequence.get("value", 0)) + 1
        self.store.put_object("event_seq", "global", sequence)

    def _record_run_events(self, run_id: str | None, plan: dict[str, Any], envelope: dict[str, Any]) -> None:
        if not run_id:
            return
        events = [
            {
                "event": "run.started",
                "data": {
                    "run_id": run_id,
                    "plan_id": plan.get("plan_id"),
                    "snapshot_id": self.service.snapshot.snapshot_id,
                    "pins": envelope.get("pins", {}),
                },
            }
        ]
        result = envelope.get("result", {}) or {}
        for step in result.get("steps", [])[:60]:
            events.append(
                {
                    "event": "step.completed",
                    "data": {
                        "run_id": run_id,
                        "step_id": step.get("step_id"),
                        "label": step.get("label"),
                        "duration_ms": 0,
                    },
                }
            )
        events.append(
            {
                "event": "run.validated",
                "data": {
                    "run_id": run_id,
                    "validation": envelope.get("validation", {}),
                    "warnings": len(envelope.get("warnings", [])),
                },
            }
        )
        events.append(
            {
                "event": "run.completed",
                "data": {"run_id": run_id, "card_url": f"/v1/runs/{run_id}"},
            }
        )
        for index, event in enumerate(events, start=1):
            self.store.put_object(
                "run_event",
                f"{run_id}:{index:04d}",
                {"sequence": index, "run_id": run_id, **event, "created_at": _now()},
            )

    def run_events(self, user: User, run_id: str, last_event_id: int = 0) -> list[dict[str, Any]]:
        self.auth.require(user, "read")
        card = self.store.get_object("run_card", run_id)
        if card:
            self._check_run_access(user, card)
        events = [
            row for row in self.store.list_objects("run_event")
            if row.get("run_id") == run_id and int(row.get("sequence", 0)) > last_event_id
        ]
        return sorted(events, key=lambda row: int(row.get("sequence", 0)))


def _columns_for(rows: list[dict[str, Any]], spec: dict[str, tuple[str, str]]) -> list[dict[str, str]]:
    columns = []
    for key in rows[0].keys() if rows else []:
        label, kind = spec.get(key, (key.replace("_", " ").title(), "text"))
        columns.append({"key": key, "label": label, "type": kind})
    return columns


def _lookup(arguments: dict[str, Any], field: str) -> Any:
    current: Any = arguments
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _apply_edit(arguments: dict[str, Any], field: str, value: Any) -> None:
    parts = field.split(".")
    if not field or any(not part for part in parts):
        raise DomainError("E_PLAN_EDIT", "An edit key is empty.", "Send edits as [{key, value, reason}].")
    current = arguments
    for part in parts[:-1]:
        node = current.get(part)
        if node is None:
            node = {}
            current[part] = node
        if not isinstance(node, dict):
            raise DomainError(
                "E_PLAN_EDIT",
                f"Cannot edit {field!r} because {part!r} is not a parameter group.",
                "Edit the group as a whole, or change the field name.",
            )
        current = node
    if parts[-1] == "overlay" and isinstance(value, dict):
        current[parts[-1]] = {str(key): str(item) for key, item in value.items()}
    else:
        current[parts[-1]] = value
