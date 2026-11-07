"""Tool service: resolves dataset inputs, runs the engine, and seals the audit run."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from costpilot import ENGINE_VERSION, RULEBOOK_VERSION, SCHEMA_VERSION
from costpilot.audit import AuditStore, compare_results
from costpilot.data.capability import build_capability
from costpilot.data.loader import Snapshot, load_snapshot
from costpilot.engine.costing import bridge
from costpilot.engine.decisions import SUPPORTED, run_decision
from costpilot.engine.errors import DomainError
from costpilot.engine.money import dec, trace_str
from costpilot.engine.rules import all_rules, registry_version
from costpilot.engine.variance import flexible_budget, flux, labor, material, overhead

ROOT = Path(__file__).resolve().parents[1]


def default_fixtures_path() -> Path:
    return Path(os.environ.get("COSTPILOT_FIXTURES", ROOT / "fixtures")).expanduser().resolve()


class ToolService:
    def __init__(self, snapshot: Snapshot, audit: AuditStore) -> None:
        self.snapshot = snapshot
        self.audit = audit

    @classmethod
    def from_env(cls) -> ToolService:
        snapshot = load_snapshot(default_fixtures_path())
        db_path = os.environ.get("COSTPILOT_AUDIT_DB", str(ROOT / "data" / "audit.db"))
        return cls(snapshot, AuditStore(db_path))

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
            "registry_version": registry_version(),
            "engine_version": ENGINE_VERSION,
            "rulebook_version": RULEBOOK_VERSION,
            "schema_version": SCHEMA_VERSION,
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
        fill(
            "idle_capacity_units",
            max(Decimal("0"), standard.capacity_units - actual.output_units),
            "snapshot",
            "Idle capacity units",
        )
        fill(
            "utilization",
            (actual.output_units / standard.capacity_units).quantize(Decimal("0.0001")),
            "snapshot",
            "Current utilization",
        )
        resolved.setdefault("capacity_state", "idle")
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
    ) -> dict[str, Any]:
        defaults = build_capability(self.snapshot)["demo_defaults"]
        product_id = product_id or defaults["product_id"]
        plant_id = plant_id or defaults["plant_id"]
        period_id = period_id or defaults["period_id"]
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
    ) -> dict[str, Any]:
        defaults = build_capability(self.snapshot)["demo_defaults"]
        product_id = product_id or defaults["product_id"]
        plant_id = plant_id or defaults["plant_id"]
        period_id = period_id or defaults["period_id"]
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
        raise DomainError("REPERFORM_UNSUPPORTED", f"Re-performance is not implemented for {tool}.")
