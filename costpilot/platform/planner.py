"""Deterministic planner. It maps language to a typed plan; it never does arithmetic."""

from __future__ import annotations

import re
import secrets
from decimal import Decimal
from typing import Any

from costpilot.data.loader import Snapshot

PLANNER_VERSION = "1.0.0"
FORBIDDEN_ARGUMENTS = {
    "amount", "total", "variance", "result", "profit", "cost_variance",
    "value", "sum", "difference", "income",
}

PERIOD = re.compile(r"\b(20\d{2}-\d{2})\b")
PLANT = re.compile(r"\b(PLT-\d{2})\b", re.IGNORECASE)
PRODUCT = re.compile(r"\b(PRD-[A-Z])\b", re.IGNORECASE)
CUSTOMER = re.compile(r"\b(CUS-\d{2})\b", re.IGNORECASE)
DIVISION = re.compile(r"\b(DIV-\d{2})\b", re.IGNORECASE)
RUN = re.compile(r"\b(run_[A-Za-z0-9_]+)\b")
PERCENT = re.compile(
    r"(volume|price|material(?: price)?|material cost|labour rate|labor rate|labour cost|overhead)"
    r"[^%\n.;]{0,30}?([+-]?\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s*cent\b)",
    re.IGNORECASE,
)
MATERIALITY = re.compile(r"materiality(?:\s+of)?\s+([\d,]+(?:\.\d+)?)", re.IGNORECASE)
UNITS = re.compile(r"([\d,]+)\s*(?:units|pieces|motors|drives)", re.IGNORECASE)
PRICE = re.compile(r"(?:at|@|price of|for)\s*(?:INR|Rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)

CITY_PLANTS = {"pune": "PLT-01", "coimbatore": "PLT-02"}
PRODUCT_NAMES = {
    "standard motor": "PRD-A",
    "custom motor": "PRD-B",
    "servo drive": "PRD-C",
}

INTENT_LABELS = {
    "variance_analysis": "Variance analysis",
    "abc_profitability": "ABC product profitability",
    "customer_profitability": "Customer profitability",
    "budget_chain": "Master budget",
    "budget_scenario": "Budget scenario",
    "monitoring": "Alert monitoring",
    "group_report": "Group consolidation",
}


def _has(text: str, *phrases: str) -> bool:
    return any(phrase in text for phrase in phrases)


def _word(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _tier_one(text: str) -> list[str]:
    intents: list[str] = []
    if _has(text, "explain run", "explain the run", "why did", "show the trace", "trace of"):
        intents.append("explain_run")
    if _has(text, "audit chain", "verify audit", "verify the audit"):
        intents.append("audit_verify")
    if _word(text, "alert", "alerting", "monitor", "monitoring") or _has(
        text, "tell me if", "notify me", "weekly digest"
    ):
        intents.append("monitoring")
    if _word(text, "connector", "connectors", "netsuite", "mapping", "mappings", "erp") or "sap " in text:
        intents.append("connector_preview")
    if _word(text, "group", "consolidated", "consolidation", "intercompany"):
        intents.append("group_report")
    if _word(text, "roi", "performance") or _has(text, "residual income", "scorecard", "performance evaluation", "divisional performance"):
        intents.append("performance_eval")
    if _has(text, "sandbag", "forecast bias", "forecast quality"):
        intents.append("forecast_quality")
    if _has(text, "rolling forecast", "rolling plan"):
        intents.append("rolling_forecast")
    if _has(text, "scenario", "what if", "what-if", "sensitivity on the budget"):
        intents.append("budget_scenario")
    if _has(text, "master budget", "budget chain", "cash budget", "purchases budget", "production budget"):
        intents.append("budget_chain")
    if _has(text, "tdabc", "time-driven", "time driven", "unused capacity"):
        intents.append("tdabc")
    if _has(text, "customer profitability", "customer profit", "loss-making customer", "loss making customer", "cost to serve"):
        intents.append("customer_profitability")
    if _word(text, "abc") or _has(text, "activity-based", "activity based", "cross-subsid", "cross subsid"):
        intents.append("abc_profitability")
    if _has(text, "bridge", "absorption costing", "absorption income", "variable costing"):
        intents.append("costing_bridge")
    if _has(
        text,
        "special order",
        "one-off order",
        "one off order",
        "incremental order",
        "idle capacity",
        "spare capacity",
    ) or re.search(r"\border\b[^\n]{0,40}\bunits?\b", text):
        intents.append("special_order")
    return intents


def _tier_two(text: str) -> list[str]:
    intents = []
    if _has(text, "variance", "price variance", "usage variance", "labour", "labor", "overhead", "flux"):
        intents.append("variance_analysis")
    if _has(text, "what data", "capability", "describe the dataset", "dataset", "what can you do"):
        intents.append("describe_dataset")
    return intents


def _serialize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


def plan_message(
    snapshot: Snapshot,
    text: str,
    *,
    session_id: str | None = None,
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lowered = text.lower().strip()
    hints = hints or {}
    slots = hints.get("slots") if isinstance(hints.get("slots"), dict) else {}
    hint_confidence = str(hints.get("confidence") or "0.8")
    hint_evidence = f"model extraction ({hints.get('model') or 'local model'})"
    defaults_period = next(
        (row["period_id"] for row in reversed(snapshot.periods) if row.get("status") != "closed"),
        snapshot.periods[-1]["period_id"] if snapshot.periods else "",
    )
    arguments: dict[str, Any] = {}
    assumptions: list[dict[str, Any]] = []
    missing: list[str] = []
    clarification: dict[str, Any] | None = None
    confidence = Decimal("0.9")

    period_match = PERIOD.search(text)
    period_id = period_match.group(1) if period_match else defaults_period
    hinted_period = slots.get("period_id")
    if hinted_period and (
        not snapshot.periods or hinted_period in {row["period_id"] for row in snapshot.periods}
    ):
        period_id = hinted_period
        assumptions.append(
            {
                "key": "period_id",
                "text": f"Period {period_id} selected by the model.",
                "source": "user_implied",
                "evidence": hint_evidence,
                "confidence": hint_confidence,
                "confirm": False,
            }
        )
    elif not period_match:
        assumptions.append(
            {
                "key": "period_id",
                "text": f"Period defaulted to the latest open period, {period_id}.",
                "source": "engine_default",
                "evidence": "periods.csv status = open",
                "confidence": "0.8",
                "confirm": False,
            }
        )
    if period_id and snapshot.periods and period_id not in {row["period_id"] for row in snapshot.periods}:
        missing.append("period_id")
        options = [row["period_id"] for row in snapshot.periods]
        clarification = {
            "question": f"I could not find period {period_id}. Which period should I use?",
            "options": options,
        }
        confidence = Decimal("0.35")

    plant_id: str | None = None
    plant_match = PLANT.search(text)
    hinted_plant = slots.get("plant_id")
    if hinted_plant and (
        not snapshot.plants or hinted_plant in {row["plant_id"] for row in snapshot.plants}
    ):
        plant_id = hinted_plant
        assumptions.append(
            {
                "key": "plant_id",
                "text": f"Plant {plant_id} selected by the model.",
                "source": "user_implied",
                "evidence": hint_evidence,
                "confidence": hint_confidence,
                "confirm": False,
            }
        )
    elif plant_match:
        plant_id = plant_match.group(1).upper()
    else:
        for city, code in CITY_PLANTS.items():
            if city in lowered:
                plant_id = code
                break
    if plant_id is None:
        plant_id = next(
            (row["plant_id"] for row in snapshot.plants if row["plant_id"] == "PLT-01"),
            snapshot.plants[0]["plant_id"] if snapshot.plants else None,
        )
        assumptions.append(
            {
                "key": "plant_id",
                "text": f"Plant defaulted to {plant_id}.",
                "source": "engine_default",
                "evidence": "capability demo defaults",
                "confidence": "0.75",
                "confirm": False,
            }
        )
    elif snapshot.plants and plant_id not in {row["plant_id"] for row in snapshot.plants}:
        missing.append("plant_id")
        clarification = {
            "question": f"I could not find plant {plant_id}. Which plant should I use?",
            "options": [row["plant_id"] for row in snapshot.plants],
        }
        confidence = Decimal("0.35")

    product_id: str | None = None
    product_match = PRODUCT.search(text)
    hinted_product = slots.get("product_id")
    if hinted_product and (
        not snapshot.products or hinted_product in {row["product_id"] for row in snapshot.products}
    ):
        product_id = hinted_product
        assumptions.append(
            {
                "key": "product_id",
                "text": f"Product {product_id} selected by the model.",
                "source": "user_implied",
                "evidence": hint_evidence,
                "confidence": hint_confidence,
                "confirm": False,
            }
        )
    elif product_match:
        product_id = product_match.group(1).upper()
    else:
        for name, code in PRODUCT_NAMES.items():
            if name in lowered:
                product_id = code
                break
    if product_id is None and snapshot.products:
        product_id = (
            "PRD-B"
            if any(row["product_id"] == "PRD-B" for row in snapshot.products)
            else snapshot.products[0]["product_id"]
        )
        assumptions.append(
            {
                "key": "product_id",
                "text": f"Product defaulted to {product_id}.",
                "source": "engine_default",
                "evidence": "capability demo defaults",
                "confidence": "0.7",
                "confirm": False,
            }
        )
    customer_match = CUSTOMER.search(text)
    customer_id = customer_match.group(1).upper() if customer_match else None
    hinted_customer = slots.get("customer_id")
    if hinted_customer and (
        not snapshot.customers or hinted_customer in snapshot.customers
    ):
        customer_id = hinted_customer
        assumptions.append(
            {
                "key": "customer_id",
                "text": f"Customer scope is {customer_id}, selected by the model.",
                "source": "user_implied",
                "evidence": hint_evidence,
                "confidence": hint_confidence,
                "confirm": False,
            }
        )
    elif customer_id:
        assumptions.append(
            {
                "key": "customer_id",
                "text": f"Customer scope is {customer_id} from the question.",
                "source": "user_explicit",
                "evidence": "question text",
                "confidence": "0.9",
                "confirm": False,
            }
        )
    division_match = DIVISION.search(text)
    run_match = RUN.search(text)

    overlay: dict[str, str] = {}
    overlay_percent: dict[str, str] = {}
    for match in PERCENT.finditer(text):
        driver = match.group(1).lower()
        if driver.startswith("material"):
            key = "material_price"
        elif driver.startswith("labour") or driver.startswith("labor"):
            key = "labour_rate"
        elif driver in {"price", "volume", "overhead"}:
            key = driver
        else:
            continue
        value = match.group(2).lstrip("+")
        context = match.group(0).lower()
        if not value.startswith("-") and _has(
            context, "falls", "fall ", "decline", "decrease", "drop", "down", "reduce", "lower"
        ):
            value = "-" + value
        overlay[key] = format(Decimal(value) / Decimal("100"), "f")
        overlay_percent[key] = value
    hinted_overlay = slots.get("overlay")
    if isinstance(hinted_overlay, dict):
        for driver, percent in hinted_overlay.items():
            if driver not in {"volume", "price", "material_price", "labour_rate", "overhead"}:
                continue
            try:
                number = Decimal(str(percent))
            except Exception:
                continue
            if abs(number) > 100:
                continue
            overlay[driver] = format(number / Decimal("100"), "f")
            overlay_percent[driver] = format(number, "f")
    if overlay:
        assumptions.append(
            {
                "key": "overlay",
                "text": "Scenario drivers come from the question: "
                + ", ".join(
                    f"{key} {overlay_percent[key]}%" for key in sorted(overlay_percent)
                ) + ".",
                "source": "user_explicit",
                "evidence": "question text",
                "confidence": "0.9",
                "confirm": True,
            }
        )

    materiality_match = MATERIALITY.search(text)
    materiality = materiality_match.group(1).replace(",", "") if materiality_match else "50000"
    if slots.get("materiality"):
        materiality = str(slots["materiality"])

    tier_one = _tier_one(lowered)
    tier_two = _tier_two(lowered)
    candidates = tier_one or tier_two
    intent: str
    if not candidates:
        intent = "describe_dataset" if not lowered else "clarify"
        if not lowered:
            clarification = None
        else:
            clarification = {
                "question": "I could not map that question to an analysis. What would you like to examine?",
                "options": [
                    "Variance analysis",
                    "ABC or customer profitability",
                    "Budget, performance or group reporting",
                    "Alerts, connectors or audit",
                ],
            }
    elif len(candidates) > 1:
        intent = "clarify"
        labels = [INTENT_LABELS.get(item, item.replace("_", " ").title()) for item in candidates[:3]]
        clarification = {
            "question": "I found more than one possible analysis. Which one should I run?",
            "options": labels,
        }
        confidence = Decimal("0.4")
    else:
        intent = candidates[0]

    hinted_intent = hints.get("intent")
    known_intents = {
        "variance_analysis", "costing_bridge", "special_order", "abc_profitability",
        "tdabc", "customer_profitability", "budget_chain", "budget_scenario",
        "rolling_forecast", "forecast_quality", "performance_eval", "monitoring",
        "group_report", "connector_preview", "describe_dataset", "clarify",
    }
    if (
        hinted_intent in known_intents
        and Decimal(hint_confidence) >= Decimal("0.55")
    ):
        intent = hinted_intent
        if intent == "clarify":
            clarification = clarification or {
                "question": (
                    "I could not choose one analysis confidently. Which result do you want: "
                    "variance, profitability, budget, performance, alerts, group or a connector preview?"
                ),
                "options": [
                    "Variance analysis",
                    "ABC or customer profitability",
                    "Budget or rolling forecast",
                    "Performance, alerts or group",
                ],
            }
        else:
            clarification = None

    tool_by_intent = {
        "describe_dataset": "describe_dataset",
        "variance_analysis": "run_variance_analysis",
        "costing_bridge": "run_costing_bridge",
        "special_order": "run_differential_analysis",
        "abc_profitability": "run_abc_profitability",
        "customer_profitability": "run_customer_profitability",
        "tdabc": "run_tdabc",
        "budget_chain": "run_budget_chain",
        "budget_scenario": "run_budget_scenario",
        "rolling_forecast": "run_rolling_forecast",
        "forecast_quality": "run_forecast_quality",
        "performance_eval": "run_performance_eval",
        "monitoring": "run_monitoring",
        "group_report": "run_group_report",
        "connector_preview": "run_connector",
        "explain_run": "explain_run",
        "audit_verify": "audit_verify",
        "clarify": "clarify",
    }
    tool = tool_by_intent[intent]

    if intent in {
        "variance_analysis", "costing_bridge", "special_order", "performance_eval",
        "customer_profitability", "tdabc", "abc_profitability",
    }:
        arguments.update({"period_id": period_id, "plant_id": plant_id})
    if intent in {"variance_analysis", "costing_bridge", "special_order"} and product_id:
        arguments["product_id"] = product_id
    if intent == "special_order":
        arguments["decision_type"] = "special_order"
        units = UNITS.search(text)
        price = PRICE.search(text)
        if units:
            arguments["order_units"] = units.group(1).replace(",", "")
        if price:
            arguments["order_price"] = price.group(1).replace(",", "")
        fixed = re.search(r"fixed cost[^0-9]{0,20}([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
        if fixed:
            arguments["incremental_fixed_cost"] = fixed.group(1).replace(",", "")
        for key in ("order_units", "order_price", "incremental_fixed_cost"):
            if slots.get(key):
                arguments[key] = str(slots[key])
        if "constrained" in lowered or "full capacity" in lowered:
            arguments["capacity_state"] = "constrained"
        arguments["confirmed"] = False
    if intent == "abc_profitability" and ("machine hour" in lowered):
        arguments["basis"] = "machine_hours"
    if intent in {"abc_profitability", "customer_profitability", "tdabc"}:
        arguments["period_id"] = period_id
        arguments["plant_id"] = plant_id
    if intent == "customer_profitability" and customer_match:
        arguments["customer_id"] = customer_match.group(1).upper()
    if intent in {"budget_chain", "budget_scenario"}:
        arguments.update({"period_id": period_id, "plant_id": plant_id})
        if overlay:
            arguments["overlay"] = overlay
            arguments["label"] = "What-if"
    if intent in {"rolling_forecast", "forecast_quality"}:
        arguments["plant_id"] = plant_id
    if intent == "performance_eval":
        arguments.update(
            {
                "period_id": period_id,
                "division_id": slots.get("division_id")
                or (division_match.group(1).upper() if division_match else "DIV-01"),
            }
        )
    if intent == "monitoring":
        arguments["period_id"] = period_id
        from costpilot.engine.monitoring import METRIC_ALIASES

        hinted_rules = slots.get("rules")
        if hinted_rules:
            arguments["rules"] = [str(rule) for rule in hinted_rules]
        else:
            rule_text = text.strip()
            if ":" in rule_text:
                rule_text = rule_text.rsplit(":", 1)[1].strip()
            names_metric = any(phrase in rule_text.lower() for phrase in METRIC_ALIASES)
            if not names_metric:
                names_metric = any(phrase in lowered for phrase in METRIC_ALIASES)
            arguments["rules"] = [rule_text] if names_metric else []
        arguments["materiality"] = materiality
    if intent == "group_report":
        arguments["period_id"] = period_id
    if intent == "connector_preview":
        chosen_connector = slots.get("connector")
        for name in ("netsuite", "sap", "flatfile"):
            if chosen_connector == name or (not chosen_connector and name in lowered):
                arguments["connector"] = name
                break
        else:
            arguments["connector"] = chosen_connector or "netsuite"
    if intent == "explain_run":
        if run_match:
            arguments["run_id"] = run_match.group(1)
        else:
            missing.append("run_id")
            clarification = {
                "question": "Which run should I explain? Give me a run id, for example run_20261107T120000_ab12cd34.",
                "options": ["Open the audit drawer and copy a run id."],
            }
            confidence = Decimal("0.35")

    requires_confirmation = intent in {"special_order", "budget_scenario"}
    if requires_confirmation and intent == "special_order":
        assumptions.append(
            {
                "key": "order_parameters",
                "text": "The order price, quantity and incremental fixed cost must be confirmed before the calculation.",
                "source": "user_explicit" if overlay or units or price else "engine_default",
                "evidence": "question text",
                "confidence": "0.85",
                "confirm": True,
            }
        )
    if requires_confirmation and intent == "budget_scenario" and overlay:
        assumptions.append(
            {
                "key": "scenario_overlay",
                "text": "The scenario overlay must be confirmed before the budget is rebuilt.",
                "source": "user_explicit",
                "evidence": "question text",
                "confidence": "0.85",
                "confirm": True,
            }
        )
    if intent == "budget_scenario" and not overlay:
        clarification = {
            "question": (
                "Which budget drivers should move, and by how much? "
                "For example material price up 3% or volume down 2%."
            ),
            "options": [
                "Material price up 3%",
                "Volume down 2%",
                "Labour rate up 5%",
            ],
        }

    if intent not in {"clarify"} and not missing:
        confidence = min(Decimal("0.98"), confidence + Decimal("0.03"))
    if intent == "clarify":
        confidence = min(confidence, Decimal("0.45"))
    if missing:
        confidence = min(confidence, Decimal("0.4"))

    forbidden = sorted(FORBIDDEN_ARGUMENTS & set(arguments))
    if forbidden:
        raise ValueError(f"planner emitted arithmetic argument names: {forbidden}")
    if any(isinstance(value, float) for value in arguments.values()):
        raise ValueError("planner arguments must be decimal strings, not floats")

    status = "needs_clarification" if clarification else (
        "needs_confirmation" if requires_confirmation else "ready"
    )
    rationale = (
        f"Matched {intent.replace('_', ' ')} from the question and resolved the period, plant and product "
        "from the dataset. All numbers will come from the engine."
        if intent != "clarify"
        else "The question did not map to a single analysis."
    )
    return {
        "plan_id": "plan_" + secrets.token_hex(6),
        "session_id": session_id,
        "status": status,
        "intent": intent,
        "tool": tool,
        "tool_version": "1.0.0",
        "planner_version": PLANNER_VERSION,
        "planner": hints.get("planner") or "deterministic",
        "planner_reason": str(hints.get("reason") or "")[:400],
        "model": hints.get("model"),
        "arguments": _serialize(arguments),
        "assumptions": assumptions,
        "missing": missing,
        "clarification": clarification,
        "confidence": str(confidence.quantize(Decimal("0.01"))),
        "requires_confirmation": requires_confirmation,
        "rationale": rationale,
    }
