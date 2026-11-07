"""Tool service: resolves dataset inputs, runs the engine, and seals the audit run."""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from costpilot import ENGINE_VERSION, RULEBOOK_VERSION, SCHEMA_VERSION
from costpilot.audit import AuditStore, compare_results
from costpilot.connectors import CONNECTORS, MappingRegistry, run_connector
from costpilot.data.capability import build_capability
from costpilot.data.documents import extract, public_view
from costpilot.data.loader import Snapshot, load_snapshot
from costpilot.data.uploads import (
    active_dataset,
    default_datasets_root,
    list_datasets,
    record_dataset,
    set_active,
)
from costpilot.engine import abc, budgets, group, monitoring, performance
from costpilot.engine import documents as document_engine
from costpilot.engine.costing import bridge
from costpilot.engine.decisions import SUPPORTED, run_decision
from costpilot.engine.errors import DomainError
from costpilot.engine.money import dec, trace_str
from costpilot.engine.rules import all_rules, registry_version
from costpilot.engine.variance import flexible_budget, flux, labor, material, overhead
from costpilot.platform.registry import RegistryService
from costpilot.platform.store import PlatformStore

ROOT = Path(__file__).resolve().parents[1]


def default_fixtures_path() -> Path:
    return Path(os.environ.get("COSTPILOT_FIXTURES", ROOT / "fixtures")).expanduser().resolve()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ToolService:
    def __init__(
        self,
        snapshot: Snapshot,
        audit: AuditStore,
        store: PlatformStore | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.audit = audit
        self.store = store or PlatformStore(
            os.environ.get("COSTPILOT_PLATFORM_DB", str(ROOT / "data" / "platform.db"))
        )
        self.mappings = MappingRegistry(snapshot, self.store)
        self.registry = RegistryService(snapshot, self.store)
        self.datasets_root = default_datasets_root()
        self.dataset_source = "bundled"

    @classmethod
    def from_env(cls) -> ToolService:
        datasets_root = default_datasets_root()
        explicit_fixtures = os.environ.get("COSTPILOT_FIXTURES")
        active = None if explicit_fixtures else active_dataset(datasets_root)
        snapshot = None
        source = "bundled"
        if active:
            try:
                snapshot = load_snapshot(active["path"], dataset_name=active["name"])
                source = "upload"
            except Exception:
                snapshot = None
        if snapshot is None:
            snapshot = load_snapshot(default_fixtures_path())
        db_path = os.environ.get("COSTPILOT_AUDIT_DB", str(ROOT / "data" / "audit.db"))
        service = cls(snapshot, AuditStore(db_path))
        service.dataset_source = source
        return service

    # ---------- datasets ----------

    def dataset_info(self) -> dict[str, Any]:
        snapshot = self.snapshot
        return {
            "dataset_id": snapshot.snapshot_id,
            "name": snapshot.dataset_name,
            "manifest_hash": snapshot.manifest_hash,
            "row_counts": snapshot.row_counts,
            "currency": snapshot.currency,
            "source": self.dataset_source,
            "current": True,
        }

    def datasets(self) -> list[dict[str, Any]]:
        rows = list_datasets(self.datasets_root)
        current = self.snapshot.snapshot_id
        if not any(row.get("dataset_id") == current for row in rows):
            rows.insert(0, self.dataset_info())
        return [dict(row, current=row.get("dataset_id") == current) for row in rows]

    def activate_snapshot(
        self,
        snapshot: Snapshot,
        *,
        path: Path | None = None,
        source: str = "upload",
        uploaded_by: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Switch the live snapshot, then persist the active pointer and index entry."""
        from costpilot.data.uploads import save_dataset

        previous = self.snapshot.snapshot_id
        stored_path = path
        if path is not None:
            stored_path = save_dataset(path, self.datasets_root, snapshot.snapshot_id)
            snapshot = load_snapshot(stored_path, dataset_name=snapshot.dataset_name)
            set_active(self.datasets_root, stored_path, snapshot.dataset_name)

        self.snapshot = snapshot
        self.dataset_source = source
        self.mappings = MappingRegistry(snapshot, self.store)
        self.registry = RegistryService(snapshot, self.store)

        entry = dict(self.dataset_info(), uploaded_by=uploaded_by, uploaded_at=_now_iso())
        if summary:
            entry["upload"] = summary
        record_dataset(self.datasets_root, entry)
        return {"previous": previous, **entry}

    # ---------- attachments ----------

    def create_attachment(
        self,
        raw: bytes,
        filename: str,
        *,
        user: Any | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Extract an uploaded document and keep it for later analysis."""
        payload = extract(raw, filename)
        attachment_id = "att_" + secrets.token_hex(8)
        record = {
            **payload,
            "attachment_id": attachment_id,
            "user_id": getattr(user, "user_id", None),
            "session_id": session_id,
            "created_at": _now_iso(),
        }
        self.store.put_object("attachment", attachment_id, record)
        return public_view(record)

    def attachment(self, attachment_id: str) -> dict[str, Any] | None:
        return self.store.get_object("attachment", attachment_id)

    def session_attachments(self, session_id: str) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.store.list_objects("attachment")
            if row.get("session_id") == session_id
        ]
        return [public_view(row) for row in rows]

    def run_attachment_analysis(
        self,
        *,
        attachment_id: str,
        operation: str,
        table: str | None = None,
        column: str | None = None,
        compare_column: str | None = None,
        query: str | None = None,
        limit: int = 5,
        direction: str = "top",
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        attachment = self.store.get_object("attachment", attachment_id)
        if attachment is None:
            raise DomainError(
                "E_ATTACHMENT_NOT_FOUND",
                f"Attachment {attachment_id} was not found.",
                "Attach the document again and ask the question in the same message.",
            )
        result = document_engine.run_attachment_analysis(
            attachment,
            operation=operation,
            table=table,
            column=column,
            compare_column=compare_column,
            query=query,
            limit=limit,
            direction=direction,
        )
        arguments = {
            "attachment_id": attachment_id,
            "attachment_name": attachment.get("name"),
            "attachment_sha256": attachment.get("sha256"),
            "operation": operation,
            "table": table,
            "column": column,
            "compare_column": compare_column,
            "query": query,
            "limit": limit,
            "direction": direction,
        }
        return self._seal(
            tool="run_attachment_analysis",
            intent="attachment_analysis",
            arguments={key: value for key, value in arguments.items() if value is not None},
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    # ---------- metadata ----------

    def capability(self) -> dict[str, Any]:
        return build_capability(self.snapshot)

    def rules(self) -> dict[str, Any]:
        return {
            "rulebook_version": RULEBOOK_VERSION,
            "registry_version": registry_version(),
            "rules": [
                {
                    "id": rule.id,
                    "version": rule.version,
                    "title": rule.title,
                    "module": rule.module,
                    "formula": rule.formula,
                    "citation": rule.citation,
                    "golden_cases": list(rule.golden_cases),
                }
                for rule in all_rules()
            ],
        }

    def pins(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot.snapshot_id,
            "manifest_hash": self.snapshot.manifest_hash,
            "rule_registry_version": registry_version(),
            "engine_version": ENGINE_VERSION,
            "rulebook_version": RULEBOOK_VERSION,
            "schema_version": SCHEMA_VERSION,
            "registry_version": self.registry.version(),
        }

    # ---------- resolution and confirmation ----------

    def resolve_special_order(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
        defaults = build_capability(self.snapshot)["demo_defaults"]
        resolved = dict(arguments)
        period_id = str(resolved.get("period_id", defaults["period_id"]))
        plant_id = str(resolved.get("plant_id", defaults["plant_id"]))
        product_id = str(resolved.get("product_id", defaults["product_id"]))
        standard = self.snapshot.standard(period_id, plant_id, product_id)
        actual = self.snapshot.actual(period_id, plant_id, product_id)

        sheet: list[dict[str, Any]] = []

        def fill(key: str, value: Any, source: str, label: str) -> None:
            if key not in resolved or resolved[key] in (None, ""):
                resolved[key] = str(value)
                sheet.append({"key": key, "value": str(value), "source": source, "label": label})
            else:
                sheet.append({"key": key, "value": str(resolved[key]), "source": "user_explicit", "label": label})

        resolved["period_id"] = period_id
        resolved["plant_id"] = plant_id
        resolved["product_id"] = product_id
        resolved["currency"] = self.snapshot.currency
        fill("order_units", defaults["order_units"], "demo_default", "Order units")
        fill("order_price", defaults["order_price"], "demo_default", "Order price per unit")
        fill(
            "incremental_fixed_cost",
            defaults["incremental_fixed_cost"],
            "demo_default",
            "Incremental fixed cost",
        )
        fill("variable_manufacturing_cost", standard.variable_manufacturing_cost, "standard_cost", "Variable manufacturing cost per unit")
        fill("regular_price", standard.price, "standard_cost", "Regular selling price")
        fill("regular_variable_selling_cost", standard.variable_selling, "standard_cost", "Regular variable selling cost")
        idle_units = max(Decimal("0"), standard.capacity_units - actual.output_units)
        fill(
            "idle_capacity_units",
            idle_units,
            "snapshot",
            "Idle capacity units",
        )
        fill(
            "utilization",
            (actual.output_units / standard.capacity_units).quantize(Decimal("0.0001")),
            "snapshot",
            "Current utilization",
        )
        order_units = Decimal(str(resolved.get("order_units") or "0"))
        resolved.setdefault(
            "capacity_state",
            "idle" if idle_units >= order_units else "constrained",
        )
        resolved.setdefault("variable_selling_cost_avoided", True)
        resolved.setdefault("incremental_fixed_cost", "0")
        resolved.setdefault("displaced_units", None)
        return resolved, sheet

    # ---------- tool: differential analysis ----------

    def preview_differential(
        self, *, decision_type: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if decision_type != "special_order":
            raise DomainError(
                "PREVIEW_UNSUPPORTED",
                "The confirmation preview is implemented for the special-order demo.",
                "Run the tool directly with confirmation.confirmed = true.",
            )
        resolved, sheet = self.resolve_special_order(arguments)
        return {
            "decision_type": decision_type,
            "arguments": resolved,
            "confirmation_sheet": sheet,
            "message": "Calculation has not run yet. Confirm the resolved parameters to execute.",
        }

    def run_differential(
        self,
        *,
        decision_type: str,
        arguments: dict[str, Any],
        confirmation: dict[str, Any] | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        if decision_type not in SUPPORTED:
            raise DomainError(
                "UNSUPPORTED_DECISION",
                f"Decision type {decision_type!r} is not registered.",
                f"Use one of: {', '.join(sorted(SUPPORTED))}.",
            )
        confirmed = bool((confirmation or {}).get("confirmed", False))
        if not confirmed:
            raise DomainError(
                "CONFIRMATION_REQUIRED",
                "The resolved parameters and assumptions must be confirmed before calculation.",
                "Review the confirmation sheet, then resend with confirmation.confirmed = true.",
            )

        sheet: list[dict] = []
        resolved_arguments = dict(arguments)
        if decision_type == "special_order":
            resolved_arguments, sheet = self.resolve_special_order(resolved_arguments)
        self._scope(
            user,
            resolved_arguments.get("plant_id"),
            resolved_arguments.get("product_id"),
        )

        result = run_decision(decision_type, resolved_arguments, confirmed=True)
        validation = result.meta.get("validation", {})
        result_payload = result.to_dict()
        intent = decision_type
        tool = "run_differential_analysis"
        run_question = question or f"Run {decision_type.replace('_', ' ')} analysis"

        run_ref = None
        if record:
            run_ref = self.audit.append(
                tool=tool,
                question=run_question,
                intent=intent,
                arguments={"decision_type": decision_type, **resolved_arguments},
                result=result_payload,
                validation=validation,
                pins=self.pins(),
                confirmation=confirmation or {},
                user=self._audit_user(user),
                plan_id=plan_id,
            )

        envelope = {
            "result": result_payload,
            "facts": result_payload["facts"],
            "assumptions": result_payload["assumptions"],
            "warnings": result_payload["warnings"],
            "remediation": result_payload["remediation"],
            "validation": validation,
            "snapshot_id": self.snapshot.snapshot_id,
            "trace_ref": {
                "run_id": run_ref.run_id if run_ref else None,
                "event_hash": run_ref.event_hash if run_ref else None,
            },
            "pins": self.pins(),
        }
        if sheet:
            envelope["confirmation_sheet"] = sheet
        return envelope

    # ---------- tool: variance pack ----------

    def _variance_components(self, product_id: str, plant_id: str, period_id: str) -> dict[str, Any]:
        standard = self.snapshot.standard(period_id, plant_id, product_id)
        actual = self.snapshot.actual(period_id, plant_id, product_id)
        budget = self.snapshot.budget(period_id, plant_id, product_id)
        inventory = self.snapshot.inventory_line(period_id, plant_id, product_id)

        voh_rate_per_hour = standard.variable_oh / standard.std_labor_hours
        fixed_rate_per_hour = standard.fixed_oh_rate / standard.std_labor_hours
        standard_hours_allowed = actual.output_units * standard.std_labor_hours

        mat = material.run(
            material.MaterialVarianceInput(
                actual_output=actual.output_units,
                standard_qty_per_unit=standard.std_material_qty,
                standard_price=standard.std_material_price,
                actual_qty_used=actual.material_qty_used,
                actual_price=actual.material_price,
                product_id=product_id,
                currency=self.snapshot.currency,
            )
        )
        lab = labor.run(
            labor.LabourVarianceInput(
                actual_output=actual.output_units,
                standard_hours_per_unit=standard.std_labor_hours,
                standard_rate=standard.std_labor_rate,
                actual_hours=actual.labor_hours,
                actual_rate=actual.labor_rate,
                product_id=product_id,
                currency=self.snapshot.currency,
            )
        )
        voh = overhead.variable_overhead(
            overhead.VariableOverheadInput(
                actual_hours=actual.labor_hours,
                standard_hours_allowed=standard_hours_allowed,
                standard_rate=voh_rate_per_hour,
                actual_variable_overhead=actual.variable_oh_actual,
                product_id=product_id,
                currency=self.snapshot.currency,
            )
        )
        foh = overhead.fixed_overhead(
            overhead.FixedOverheadInput(
                budgeted_fixed=standard.budget_volume * standard.fixed_oh_rate,
                actual_fixed=actual.fixed_oh_actual,
                standard_hours_allowed=standard_hours_allowed,
                denominator_hours=standard.denominator_hours,
                fixed_rate=fixed_rate_per_hour,
                product_id=product_id,
                currency=self.snapshot.currency,
            )
        )

        revenue_actual = actual.output_units * standard.price
        material_actual = actual.material_qty_used * actual.material_price
        labour_actual = actual.labor_hours * actual.labor_rate
        selling_actual = actual.output_units * standard.variable_selling
        flex = flexible_budget.run(
            flexible_budget.FlexibleBudgetInput(
                budget_volume=standard.budget_volume,
                actual_volume=actual.output_units,
                period_id=period_id,
                currency=self.snapshot.currency,
                lines=[
                    flexible_budget.FlexibleBudgetLine(
                        "Revenue", "revenue", budget.revenue, revenue_actual, None, "4010"
                    ),
                    flexible_budget.FlexibleBudgetLine(
                        "Direct materials", "variable_cost", budget.material, material_actual,
                        standard.variable_material, "5010",
                    ),
                    flexible_budget.FlexibleBudgetLine(
                        "Direct labour", "variable_cost", budget.labor, labour_actual,
                        standard.variable_labor, "5020",
                    ),
                    flexible_budget.FlexibleBudgetLine(
                        "Variable overhead", "variable_cost", budget.variable_oh,
                        actual.variable_oh_actual, standard.variable_oh, "5030",
                    ),
                    flexible_budget.FlexibleBudgetLine(
                        "Fixed overhead", "fixed_cost", budget.fixed_oh, actual.fixed_oh_actual,
                        None, "5040",
                    ),
                    flexible_budget.FlexibleBudgetLine(
                        "Selling and distribution", "variable_cost", budget.variable_selling,
                        selling_actual, standard.variable_selling, "6010",
                    ),
                ],
            )
        )

        gl_lines = [
            flux.FluxLine(
                account_id=account.account_id,
                label=account.name,
                kind="revenue" if account.behavior == "revenue" else "cost",
                actual=row.actual,
                budget=row.budget,
                prior=row.prior,
            )
            for (p, pl, account_id), row in sorted(self.snapshot.gl.items())
            if p == period_id and pl == plant_id
            for account in [self.snapshot.accounts[account_id]]
        ]
        account_flux = flux.run(
            flux.FluxInput(
                period_id=period_id,
                lines=gl_lines,
                materiality=Decimal("50000"),
                currency=self.snapshot.currency,
            )
        )

        variable_oi = (
            revenue_actual - material_actual - labour_actual - actual.variable_oh_actual
            - selling_actual - actual.fixed_oh_actual
        )
        cost_bridge = bridge.run(
            bridge.BridgeInput(
                variable_operating_income=variable_oi,
                beginning_inventory_units=inventory.beginning_units,
                ending_inventory_units=inventory.ending_units,
                fixed_overhead_rate=standard.fixed_oh_rate,
                period_id=period_id,
                currency=self.snapshot.currency,
            )
        )

        return {
            "material": mat,
            "labor": lab,
            "variable_overhead": voh,
            "fixed_overhead": foh,
            "flexible_budget": flex,
            "account_flux": account_flux,
            "costing_bridge": cost_bridge,
        }

    def run_variance(
        self,
        *,
        product_id: str | None = None,
        plant_id: str | None = None,
        period_id: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = build_capability(self.snapshot)["demo_defaults"]
        product_id = product_id or defaults["product_id"]
        plant_id = plant_id or defaults["plant_id"]
        period_id = period_id or defaults["period_id"]
        self._scope(user, plant_id, product_id)
        standard = self.snapshot.standard(period_id, plant_id, product_id)
        actual = self.snapshot.actual(period_id, plant_id, product_id)
        components = self._variance_components(product_id, plant_id, period_id)
        payload = {name: result.to_dict() for name, result in components.items()}

        assumptions = [item for result in components.values() for item in result.assumptions]
        warnings = [item for result in components.values() for item in result.warnings]
        validation = all(
            result.meta.get("validation", {}).get("failures") == [] for result in components.values()
        )
        summary = {
            "product_id": product_id,
            "plant_id": plant_id,
            "period_id": period_id,
            "material_cost_variance": payload["material"]["value"],
            "labour_cost_variance": payload["labor"]["value"],
            "variable_overhead_total": payload["variable_overhead"]["value"],
            "fixed_overhead_total": payload["fixed_overhead"]["value"],
            "flexible_budget_variance": payload["flexible_budget"]["value"],
            "net_account_flux": payload["account_flux"]["value"],
            "absorption_operating_income": payload["costing_bridge"]["value"],
            "variable_operating_income": trace_str(
                components["costing_bridge"].fact_map()["f_variable"].value
            ),
            "standard_hours_allowed": str(actual.output_units * standard.std_labor_hours),
            "decomposition_status": payload["account_flux"]["meta"]["decomposition_status"],
        }

        run_ref = None
        if record:
            run_ref = self.audit.append(
                tool="run_variance_analysis",
                question=question or "Variance and flux pack",
                intent="variance_analysis",
                arguments={
                    "product_id": product_id,
                    "plant_id": plant_id,
                    "period_id": period_id,
                },
                result={"components": payload, "summary": summary},
                validation={"all_components_pass": validation},
                pins=self.pins(),
                user=self._audit_user(user),
                plan_id=plan_id,
            )

        return {
            "result": {
                "summary": summary,
                "components": payload,
            },
            "facts": [fact.to_dict() for result in components.values() for fact in result.facts],
            "assumptions": [item.to_dict() for item in assumptions],
            "warnings": [item.to_dict() for item in warnings],
            "validation": {"all_components_pass": validation},
            "snapshot_id": self.snapshot.snapshot_id,
            "trace_ref": {
                "run_id": run_ref.run_id if run_ref else None,
                "event_hash": run_ref.event_hash if run_ref else None,
            },
            "pins": self.pins(),
        }

    # ---------- tool: costing bridge ----------

    def run_bridge(
        self,
        *,
        product_id: str | None = None,
        plant_id: str | None = None,
        period_id: str | None = None,
        variable_operating_income: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = build_capability(self.snapshot)["demo_defaults"]
        product_id = product_id or defaults["product_id"]
        plant_id = plant_id or defaults["plant_id"]
        period_id = period_id or defaults["period_id"]
        self._scope(user, plant_id, product_id)
        standard = self.snapshot.standard(period_id, plant_id, product_id)
        inventory = self.snapshot.inventory_line(period_id, plant_id, product_id)
        actual = self.snapshot.actual(period_id, plant_id, product_id)
        if variable_operating_income is None:
            variable_oi = (
                actual.output_units * standard.price
                - actual.material_qty_used * actual.material_price
                - actual.labor_hours * actual.labor_rate
                - actual.variable_oh_actual
                - actual.output_units * standard.variable_selling
                - actual.fixed_oh_actual
            )
            source = "snapshot"
        else:
            variable_oi = dec(variable_operating_income)
            source = "user_explicit"
        result = bridge.run(
            bridge.BridgeInput(
                variable_operating_income=variable_oi,
                beginning_inventory_units=inventory.beginning_units,
                ending_inventory_units=inventory.ending_units,
                fixed_overhead_rate=standard.fixed_oh_rate,
                period_id=period_id,
                currency=self.snapshot.currency,
            )
        )
        result_payload = result.to_dict()
        run_ref = None
        if record:
            run_ref = self.audit.append(
                tool="run_costing_bridge",
                question=question or "Variable-to-absorption bridge",
                intent="costing_bridge",
                arguments={
                    "product_id": product_id,
                    "plant_id": plant_id,
                    "period_id": period_id,
                    "variable_operating_income": str(variable_oi),
                    "source": source,
                },
                result=result_payload,
                validation=result.meta.get("validation", {}),
                pins=self.pins(),
                user=self._audit_user(user),
                plan_id=plan_id,
            )
        return {
            "result": result_payload,
            "facts": result_payload["facts"],
            "assumptions": result_payload["assumptions"],
            "warnings": result_payload["warnings"],
            "validation": result.meta.get("validation", {}),
            "snapshot_id": self.snapshot.snapshot_id,
            "trace_ref": {
                "run_id": run_ref.run_id if run_ref else None,
                "event_hash": run_ref.event_hash if run_ref else None,
            },
            "pins": self.pins(),
        }

    # ---------- Phase 2 tools ----------

    def _defaults(self) -> dict[str, Any]:
        return build_capability(self.snapshot)["demo_defaults"]

    def _scope(self, user: Any | None, plant_id: str | None, product_id: str | None = None) -> None:
        if user is None:
            return
        scope = getattr(user, "scope", None)
        if scope is not None and not scope.allows(plant_id=plant_id, product_id=product_id):
            raise DomainError(
                "E_AUTH_SCOPE",
                "The requested plant or product is outside this user's data scope.",
                "Choose a row inside your scope, or ask an administrator to widen it.",
            )

    @staticmethod
    def _audit_user(user: Any | None) -> dict[str, Any] | None:
        if user is None:
            return None
        if hasattr(user, "public"):
            return user.public()
        if isinstance(user, dict):
            return user
        return {"user_id": str(user)}

    def _seal(
        self,
        *,
        tool: str,
        intent: str,
        arguments: dict[str, Any],
        result: Any,
        question: str | None,
        user: Any | None,
        plan_id: str | None = None,
        confirmation: dict[str, Any] | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        result_payload = result.to_dict()
        validation = result.meta.get("validation", {})
        run_ref = None
        if record:
            run_ref = self.audit.append(
                tool=tool,
                question=question or intent.replace("_", " ").title(),
                intent=intent,
                arguments=arguments,
                result=result_payload,
                validation=validation,
                pins=self.pins(),
                confirmation=confirmation or {},
                user=self._audit_user(user),
                plan_id=plan_id,
            )
        return {
            "result": result_payload,
            "facts": result_payload["facts"],
            "assumptions": result_payload["assumptions"],
            "warnings": result_payload["warnings"],
            "remediation": result_payload["remediation"],
            "validation": validation,
            "snapshot_id": self.snapshot.snapshot_id,
            "trace_ref": {
                "run_id": run_ref.run_id if run_ref else None,
                "event_hash": run_ref.event_hash if run_ref else None,
            },
            "pins": self.pins(),
        }

    def run_abc_profitability(
        self,
        *,
        period_id: str | None = None,
        plant_id: str | None = None,
        basis: str = "labor_hours",
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = self._defaults()
        period_id = period_id or defaults["period_id"]
        plant_id = plant_id or defaults["plant_id"]
        self._scope(user, plant_id)
        result = abc.run_abc_profitability(
            self.snapshot, period_id=period_id, plant_id=plant_id, basis=basis
        )
        return self._seal(
            tool="run_abc_profitability",
            intent="abc_profitability",
            arguments={"period_id": period_id, "plant_id": plant_id, "basis": basis},
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_tdabc(
        self,
        *,
        period_id: str | None = None,
        plant_id: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = self._defaults()
        period_id = period_id or defaults["period_id"]
        plant_id = plant_id or defaults["plant_id"]
        self._scope(user, plant_id)
        result = abc.run_tdabc(self.snapshot, period_id=period_id, plant_id=plant_id)
        return self._seal(
            tool="run_tdabc",
            intent="tdabc",
            arguments={"period_id": period_id, "plant_id": plant_id},
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_customer_profitability(
        self,
        *,
        period_id: str | None = None,
        plant_id: str | None = None,
        customer_id: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = self._defaults()
        period_id = period_id or defaults["period_id"]
        plant_id = plant_id or defaults["plant_id"]
        self._scope(user, plant_id)
        result = abc.run_customer_profitability(
            self.snapshot,
            period_id=period_id,
            plant_id=plant_id,
            selected_customer=customer_id,
        )
        return self._seal(
            tool="run_customer_profitability",
            intent="customer_profitability",
            arguments={
                "period_id": period_id,
                "plant_id": plant_id,
                "customer_id": customer_id or "ALL",
            },
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_budget(
        self,
        *,
        period_id: str | None = None,
        plant_id: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = self._defaults()
        period_id = period_id or defaults["period_id"]
        if plant_id:
            self._scope(user, plant_id)
        result = budgets.run_budget_chain(self.snapshot, period_id=period_id, plant_id=plant_id)
        return self._seal(
            tool="run_budget_chain",
            intent="budget_chain",
            arguments={"period_id": period_id, "plant_id": plant_id or "ALL"},
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_scenario(
        self,
        *,
        period_id: str | None = None,
        plant_id: str | None = None,
        overlay: dict[str, Any] | None = None,
        label: str = "Scenario",
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = self._defaults()
        period_id = period_id or defaults["period_id"]
        if plant_id:
            self._scope(user, plant_id)
        result = budgets.run_budget_scenario(
            self.snapshot,
            period_id=period_id,
            plant_id=plant_id,
            overlay=overlay or {},
            label=label,
        )
        return self._seal(
            tool="run_budget_scenario",
            intent="budget_scenario",
            arguments={
                "period_id": period_id,
                "plant_id": plant_id or "ALL",
                "overlay": overlay or {},
                "label": label,
            },
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_rolling_forecast(
        self,
        *,
        plant_id: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        if plant_id:
            self._scope(user, plant_id)
        result = budgets.run_rolling_forecast(self.snapshot, plant_id=plant_id)
        return self._seal(
            tool="run_rolling_forecast",
            intent="rolling_forecast",
            arguments={"plant_id": plant_id or "ALL"},
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_forecast_quality(
        self,
        *,
        plant_id: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        if plant_id:
            self._scope(user, plant_id)
        result = budgets.run_forecast_quality(self.snapshot, plant_id=plant_id)
        return self._seal(
            tool="run_forecast_quality",
            intent="forecast_quality",
            arguments={"plant_id": plant_id or "ALL"},
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_performance(
        self,
        *,
        period_id: str | None = None,
        division_id: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = self._defaults()
        period_id = period_id or defaults["period_id"]
        division_id = division_id or defaults.get("division_id", "DIV-01")
        asset = self.snapshot.assets.get((period_id, division_id))
        self._scope(user, asset.plant_id if asset else None)
        result = performance.run_performance_eval(
            self.snapshot, period_id=period_id, division_id=division_id
        )
        return self._seal(
            tool="run_performance_eval",
            intent="performance_eval",
            arguments={"period_id": period_id, "division_id": division_id},
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_monitoring(
        self,
        *,
        period_id: str | None = None,
        rules: list[Any] | None = None,
        materiality: str = "50000",
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = self._defaults()
        period_id = period_id or defaults["period_id"]
        result = monitoring.run_monitoring(
            self.snapshot, period_id=period_id, rules=rules, materiality=materiality
        )
        for alert in result.meta.get("alerts", []):
            plant = alert.get("plant_id")
            if plant not in (None, "ALL"):
                self._scope(user, str(plant))
        return self._seal(
            tool="run_monitoring",
            intent="monitoring",
            arguments={
                "period_id": period_id,
                "rules": rules or [],
                "materiality": materiality,
            },
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    def run_group_report(
        self,
        *,
        period_id: str | None = None,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        defaults = self._defaults()
        period_id = period_id or defaults["period_id"]
        result = group.run_group_report(self.snapshot, period_id=period_id)
        return self._seal(
            tool="run_group_report",
            intent="group_report",
            arguments={"period_id": period_id},
            result=result,
            question=question,
            user=user,
            plan_id=plan_id,
            record=record,
        )

    # ---------- connectors and governance ----------

    def connector_catalog(self) -> dict[str, Any]:
        return {
            "connectors": [
                {
                    "name": "netsuite",
                    "system": "NetSuite",
                    "mode": "read_only_extract",
                    "contract_version": "1",
                    "extract": "fixtures/connectors/netsuite_gl_extract.csv",
                },
                {
                    "name": "sap",
                    "system": "SAP",
                    "mode": "read_only_extract",
                    "contract_version": "1",
                    "extract": "fixtures/connectors/sap_gl_extract.csv",
                },
                {
                    "name": "flatfile",
                    "system": "Flat file",
                    "mode": "read_only_extract",
                    "contract_version": "1",
                    "extract": "fixtures/connectors/flat_gl_extract.csv",
                },
            ],
            "write_back": False,
            "mappings": self.mappings.all(),
        }

    def run_connector_tool(
        self,
        *,
        name: str,
        question: str | None = None,
        record: bool = True,
        user: Any | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        payload = run_connector(
            self.snapshot,
            self.mappings,
            name,
            extract_root=default_fixtures_path(),
        )
        run_ref = None
        if record:
            run_ref = self.audit.append(
                tool=f"connector:{name}",
                question=question or f"Preview {name} connector",
                intent="connector_preview",
                arguments={"connector": name},
                result=payload,
                validation={"ok": payload["ok"], "gates": payload["gates"]},
                pins=self.pins(),
                user=self._audit_user(user),
                plan_id=plan_id,
            )
        return {
            "result": payload,
            "facts": [],
            "assumptions": [],
            "warnings": [gate for gate in payload["gates"] if gate["status"] != "pass"],
            "validation": {"ok": payload["ok"]},
            "snapshot_id": self.snapshot.snapshot_id,
            "trace_ref": {
                "run_id": run_ref.run_id if run_ref else None,
                "event_hash": run_ref.event_hash if run_ref else None,
            },
            "pins": self.pins(),
            "read_only": True,
        }

    def propose_mapping(
        self,
        *,
        source_system: str,
        source_account: str,
        source_name: str,
        target_account: str,
        user: Any | None,
    ) -> dict[str, Any]:
        if source_system not in CONNECTORS:
            raise DomainError(
                "CONNECTOR_UNKNOWN",
                f"Unknown connector {source_system!r}.",
                "Use one of: " + ", ".join(sorted(CONNECTORS)) + ".",
            )
        return self.mappings.propose(
            source_system=source_system,
            source_account=source_account,
            source_name=source_name,
            target_account=target_account,
            proposed_by=getattr(user, "user_id", "system"),
        )

    def approve_mapping(self, proposal_id: str, user: Any | None) -> dict[str, Any]:
        return self.mappings.approve(proposal_id, getattr(user, "user_id", "system"))

    # ---------- alert rules ----------

    def save_alert_rule(
        self,
        *,
        text: str,
        created_by: str,
        period_id: str | None = None,
        materiality: str = "50000",
    ) -> dict[str, Any]:
        parsed = monitoring.parse_alert_rule(text)
        import secrets as _secrets

        rule_id = "alert_" + _secrets.token_hex(6)
        payload = {
            "rule_id": rule_id,
            "text": text,
            "parsed": {
                **{key: (str(value) if isinstance(value, Decimal) else value) for key, value in parsed.items()}
            },
            "period_id": period_id or self._defaults()["period_id"],
            "materiality": materiality,
            "created_by": created_by,
            "enabled": True,
        }
        return self.store.put_object("alert_rule", rule_id, payload)

    def list_alert_rules(self) -> list[dict[str, Any]]:
        return self.store.list_objects("alert_rule")

    def delete_alert_rule(self, rule_id: str) -> bool:
        return self.store.delete_object("alert_rule", rule_id)

    def run_saved_alerts(self, period_id: str | None = None, user: Any | None = None) -> dict[str, Any]:
        rules = self.list_alert_rules()
        enabled = [rule for rule in rules if rule.get("enabled")]
        if enabled:
            texts = [rule["text"] for rule in enabled]
            materialities = [Decimal(str(rule.get("materiality", "50000"))) for rule in enabled]
            payload = self.run_monitoring(
                period_id=period_id or enabled[0].get("period_id"),
                rules=texts,
                materiality=str(min(materialities)),
                question="Scheduled alert digest",
                user=user,
            )
        else:
            payload = self.run_monitoring(
                period_id=period_id,
                question="Scheduled alert digest",
                user=user,
            )
        digest = {
            "created_at": payload["result"]["meta"].get("period_id"),
            "period_id": payload["result"]["meta"]["period_id"],
            "active_count": payload["result"]["meta"]["active_count"],
            "alerts": payload["result"]["meta"]["digest"],
            "run_id": payload["trace_ref"]["run_id"],
        }
        self.store.put_object("alert_digest", str(digest["run_id"]), digest)
        return payload

    def alert_digests(self, limit: int = 10) -> list[dict[str, Any]]:
        digests = self.store.list_objects("alert_digest")
        return list(reversed(digests))[:limit]

    # ---------- trace ----------

    def trace(self, run_id: str) -> dict[str, Any]:
        run = self.audit.get(run_id)
        if run is None:
            raise DomainError(
                "RUN_NOT_FOUND",
                f"Run {run_id} was not found.",
                "Open a run from the audit drawer or list recent runs.",
            )
        result = run.get("result", {})
        steps: list[dict[str, Any]] = []
        if isinstance(result.get("steps"), list):
            steps = result["steps"]
        elif isinstance(result.get("components"), dict):
            for name, component in result["components"].items():
                for step in component.get("steps", []):
                    steps.append({**step, "component": name})
        return {
            "run_id": run_id,
            "tool": run["tool"]["name"],
            "question": run.get("question"),
            "pins": run.get("pins"),
            "user": run.get("user"),
            "steps": steps,
            "facts": result.get("facts", []),
            "warnings": result.get("warnings", []),
            "validation": run.get("validation", {}),
            "integrity": run.get("integrity"),
            "snapshot_id": self.snapshot.snapshot_id,
        }

    # ---------- audit ----------

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.audit.get(run_id)

    def verify_chain(self) -> dict[str, Any]:
        return self.audit.verify_chain()

    def reperform(self, run_id: str) -> dict[str, Any]:
        run = self.audit.get(run_id)
        if run is None:
            raise DomainError("RUN_NOT_FOUND", f"Run {run_id} was not found.")
        tool = run["tool"]["name"]
        arguments = run["arguments"]
        if tool == "run_differential_analysis":
            fresh = run_decision(
                arguments["decision_type"],
                {key: value for key, value in arguments.items() if key != "decision_type"},
                confirmed=True,
            )
            comparison = compare_results(run["result"], fresh.to_dict())
            return {"run_id": run_id, "tool": tool, **comparison}
        if tool == "run_variance_analysis":
            components = self._variance_components(
                arguments["product_id"], arguments["plant_id"], arguments["period_id"]
            )
            comparisons = {}
            worst = "exact"
            for name, result in components.items():
                stored = run["result"]["components"][name]
                comparison = compare_results(stored, result.to_dict())
                comparisons[name] = comparison
                if comparison["status"] == "mismatch":
                    worst = "mismatch"
                elif comparison["status"] == "display_rounding" and worst == "exact":
                    worst = "display_rounding"
            return {"run_id": run_id, "tool": tool, "status": worst, "components": comparisons}
        if tool == "run_costing_bridge":
            result = bridge.run(
                bridge.BridgeInput(
                    variable_operating_income=dec(arguments["variable_operating_income"]),
                    beginning_inventory_units=self.snapshot.inventory_line(
                        arguments["period_id"], arguments["plant_id"], arguments["product_id"]
                    ).beginning_units,
                    ending_inventory_units=self.snapshot.inventory_line(
                        arguments["period_id"], arguments["plant_id"], arguments["product_id"]
                    ).ending_units,
                    fixed_overhead_rate=self.snapshot.standard(
                        arguments["period_id"], arguments["plant_id"], arguments["product_id"]
                    ).fixed_oh_rate,
                    period_id=arguments["period_id"],
                    currency=self.snapshot.currency,
                )
            )
            comparison = compare_results(run["result"], result.to_dict())
            return {"run_id": run_id, "tool": tool, **comparison}

        fresh = self._reperform_payload(tool, arguments)
        stored_payload = run["result"]
        fresh_payload = fresh["result"]
        comparison = compare_results(stored_payload, fresh_payload)
        if comparison["status"] == "exact" and stored_payload != fresh_payload:
            comparison = {
                "status": "mismatch",
                "mismatches": [{"step_id": "payload", "stored": "recorded", "recomputed": "different"}],
                "display_rounding": [],
                "missing_steps": [],
                "extra_steps": [],
            }
        return {
            "run_id": run_id,
            "tool": tool,
            "status": comparison["status"],
            "comparison": comparison,
            "pins": run.get("pins"),
        }

    def _reperform_payload(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Re-execute a pinned tool without writing a second audit run."""
        period = arguments.get("period_id")
        plant = arguments.get("plant_id")
        if plant == "ALL":
            plant = None
        if tool == "run_abc_profitability":
            return self.run_abc_profitability(
                period_id=period, plant_id=plant, basis=arguments.get("basis", "labor_hours"), record=False
            )
        if tool == "run_attachment_analysis":
            return self.run_attachment_analysis(
                attachment_id=str(arguments.get("attachment_id", "")),
                operation=str(arguments.get("operation", "profile")),
                table=arguments.get("table"),
                column=arguments.get("column"),
                compare_column=arguments.get("compare_column"),
                query=arguments.get("query"),
                limit=int(arguments.get("limit", 5)),
                direction=str(arguments.get("direction", "top")),
                record=False,
            )
        if tool == "run_tdabc":
            return self.run_tdabc(period_id=period, plant_id=plant, record=False)
        if tool == "run_customer_profitability":
            return self.run_customer_profitability(period_id=period, plant_id=plant, record=False)
        if tool == "run_budget_chain":
            return self.run_budget(period_id=period, plant_id=plant, record=False)
        if tool == "run_budget_scenario":
            return self.run_scenario(
                period_id=period,
                plant_id=plant,
                overlay=arguments.get("overlay") or {},
                label=arguments.get("label", "Scenario"),
                record=False,
            )
        if tool == "run_rolling_forecast":
            return self.run_rolling_forecast(plant_id=plant, record=False)
        if tool == "run_forecast_quality":
            return self.run_forecast_quality(plant_id=plant, record=False)
        if tool == "run_performance_eval":
            return self.run_performance(
                period_id=period, division_id=arguments.get("division_id"), record=False
            )
        if tool == "run_monitoring":
            return self.run_monitoring(
                period_id=period,
                rules=arguments.get("rules") or [],
                materiality=str(arguments.get("materiality", "50000")),
                record=False,
            )
        if tool == "run_group_report":
            return self.run_group_report(period_id=period, record=False)
        if tool.startswith("connector:"):
            return self.run_connector_tool(name=tool.split(":", 1)[1], record=False)
        raise DomainError(
            "REPERFORM_UNSUPPORTED",
            f"Re-performance is not implemented for {tool}.",
            "Use the trace to review the steps, or ask an administrator to add this tool.",
        )
