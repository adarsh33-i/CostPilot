"""Capability card generated from the snapshot, never written by a model."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from costpilot import SCHEMA_VERSION
from costpilot.engine.decisions import SUPPORTED

if TYPE_CHECKING:
    from costpilot.data.loader import Snapshot


def _demo_defaults(snapshot: Snapshot) -> dict:
    periods = [row for row in snapshot.periods if row.get("status") == "open"] or snapshot.periods
    period_id = periods[-1]["period_id"]
    plant_id = next((p for p in snapshot.plants if p["plant_id"] == "PLT-01"), snapshot.plants[0])["plant_id"]
    product_id = "PRD-B" if any(p["product_id"] == "PRD-B" for p in snapshot.products) else snapshot.products[0]["product_id"]
    standard = snapshot.standard(period_id, plant_id, product_id)
    actual = snapshot.actual(period_id, plant_id, product_id)
    idle = standard.capacity_units - actual.output_units
    defaults = {
        "period_id": period_id,
        "plant_id": plant_id,
        "product_id": product_id,
        "order_units": "2000",
        "order_price": "380.00",
        "incremental_fixed_cost": "40000.00",
        "variable_manufacturing_cost": str(standard.variable_manufacturing_cost),
        "regular_price": str(standard.price),
        "regular_variable_selling_cost": str(standard.variable_selling),
        "idle_capacity_units": str(idle),
        "utilization": str((actual.output_units / standard.capacity_units).quantize(Decimal("0.0001"))),
        "capacity_units": str(standard.capacity_units),
        "current_output_units": str(actual.output_units),
    }
    customers = sorted(snapshot.customers)
    if customers:
        defaults["customer_id"] = "CUS-05" if "CUS-05" in snapshot.customers else customers[0]
    divisions = sorted({key[1] for key in snapshot.assets})
    if divisions:
        defaults["division_id"] = divisions[0]
    return defaults


def build_capability(snapshot: Snapshot) -> dict:
    blocking = [gate for gate in snapshot.gates if gate.status == "block"]
    warnings = [gate for gate in snapshot.gates if gate.status == "warn"]
    total_amount = sum((abs(row.actual) for row in snapshot.gl.values()), Decimal("0"))
    tagged_amount = sum(
        (
            abs(row.actual)
            for row in snapshot.gl.values()
            if snapshot.accounts.get(row.account_id)
            and snapshot.accounts[row.account_id].behavior
            and snapshot.accounts[row.account_id].directness
        ),
        Decimal("0"),
    )
    coverage = tagged_amount / total_amount if total_amount else Decimal("0")
    reconciled = not any(gate.id == "G5" and gate.status == "block" for gate in snapshot.gates)
    ready = not blocking

    abc = ready and bool(snapshot.cost_pools) and bool(snapshot.driver_events)
    customer_abc = abc and bool(snapshot.customer_activity) and bool(snapshot.customer_cost_rates)
    budgets = ready and bool(snapshot.budget_assumptions)
    performance = ready and bool(snapshot.scorecard) and bool(snapshot.assets)
    group = ready and bool(snapshot.entity_financials)
    connectors = bool(snapshot.connector_mappings) and (snapshot.root / "connectors").is_dir()

    return {
        "dataset": snapshot.dataset_name,
        "snapshot_id": snapshot.snapshot_id,
        "manifest_hash": snapshot.manifest_hash,
        "schema_version": SCHEMA_VERSION,
        "periods": [row["period_id"] for row in snapshot.periods],
        "entities": snapshot.entities,
        "plants": snapshot.plants,
        "products": snapshot.products,
        "customers": [
            {"customer_id": key, "name": value.name, "segment": value.segment}
            for key, value in sorted(snapshot.customers.items())
        ],
        "divisions": [
            {"division_id": division_id, "name": row.name, "plant_id": row.plant_id}
            for (period_id, division_id), row in sorted(snapshot.assets.items())
            if period_id == snapshot.periods[-1]["period_id"]
        ] if snapshot.periods else [],
        "currency": snapshot.currency,
        "analysis_ready": {
            "variance": ready,
            "decisions": ready,
            "costing_bridge": ready,
            "abc": abc,
            "customer_profitability": customer_abc,
            "tdabc": abc,
            "budgets": budgets,
            "performance": performance,
            "group_reporting": group,
            "monitoring": ready,
            "connectors": connectors,
        },
        "data_quality": {
            "tag_coverage_weighted": str(coverage.quantize(Decimal("0.0001"))),
            "reconciled": reconciled,
            "blocking_gates": len(blocking),
            "warnings": len(warnings),
            "gates": [gate.to_dict() for gate in snapshot.gates],
        },
        "supported_decisions": sorted(SUPPORTED),
        "demo_defaults": _demo_defaults(snapshot),
        "limits": {"max_tool_calls": 6, "max_clarifications": 1},
        "milestones": (
            "M0 foundations; M1 data + formulas; M2 variance + audit; M3 decision engine; "
            "M4 chat, RBAC, exports; M5 ERP connectors; M6 ABC/TDABC; M7 budgets and forecast; "
            "M8 performance measurement; M9 continuous monitoring; M10 group reporting; M11 hardening"
        ),
        "phases": {
            "phase_1": ["M0", "M1", "M2", "M3", "M4", "M5"],
            "phase_2": ["M6", "M7", "M8", "M9", "M10", "M11"],
        },
    }
