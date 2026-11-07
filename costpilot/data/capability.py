"""Capability card generated from the snapshot, never written by a model."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from costpilot.engine.decisions import SUPPORTED

if TYPE_CHECKING:
    from costpilot.data.loader import Snapshot


def _demo_defaults(snapshot: Snapshot) -> dict:
    periods = [row for row in snapshot.periods if row.get("status") == "open"] or snapshot.periods
    period_id = periods[-1]["period_id"]
    plant_id = "PLT-01"
    product_id = "PRD-B"
    standard = snapshot.standard(period_id, plant_id, product_id)
    actual = snapshot.actual(period_id, plant_id, product_id)
    idle = standard.capacity_units - actual.output_units
    return {
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

    return {
        "dataset": snapshot.dataset_name,
        "snapshot_id": snapshot.snapshot_id,
        "manifest_hash": snapshot.manifest_hash,
        "periods": [row["period_id"] for row in snapshot.periods],
        "entities": snapshot.entities,
        "plants": snapshot.plants,
        "products": snapshot.products,
        "currency": snapshot.currency,
        "analysis_ready": {
            "variance": ready,
            "decisions": ready,
            "costing_bridge": ready,
            "abc": False,
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
        "limits": {"max_tool_calls": 3, "max_clarifications": 1},
        "milestones": "M0 foundations, M1 data + formulas, M2 variance + audit, M3 decision engine",
    }
