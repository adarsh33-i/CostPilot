"""Canonical dataset snapshot: dimensions, standards, actuals, budgets, GL, inventory."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from costpilot import SCHEMA_VERSION
from costpilot.engine.errors import DomainError
from costpilot.engine.money import dec

REQUIRED_FILES = {
    "entities.csv": ["entity_id", "name", "currency"],
    "plants.csv": ["plant_id", "name", "entity_id"],
    "products.csv": ["product_id", "name", "family"],
    "periods.csv": ["period_id", "label", "status"],
    "accounts.csv": ["account_id", "name", "statement", "behavior", "directness", "unit", "owner"],
    "standard_costs.csv": [
        "period_id", "plant_id", "product_id", "price", "variable_material",
        "variable_labor", "variable_oh", "fixed_oh_rate", "variable_selling",
        "std_material_qty", "std_material_price", "std_labor_hours", "std_labor_rate",
        "std_machine_hours", "budget_volume", "capacity_units", "denominator_hours",
    ],
    "production_actuals.csv": [
        "period_id", "plant_id", "product_id", "output_units", "material_qty_used",
        "material_price", "labor_hours", "labor_rate", "variable_oh_actual",
        "fixed_oh_actual", "actual_machine_hours",
    ],
    "budgets.csv": [
        "period_id", "plant_id", "product_id", "volume", "revenue", "material", "labor",
        "variable_oh", "fixed_oh", "variable_selling",
    ],
    "gl_balances.csv": [
        "period_id", "plant_id", "account_id", "actual", "budget", "prior",
    ],
    "inventory.csv": [
        "period_id", "plant_id", "product_id", "beginning_units", "ending_units",
        "fixed_oh_rate",
    ],
    "control_totals.csv": ["period_id", "account_id", "expected_amount"],
}


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    statement: str
    behavior: str
    directness: str
    unit: str
    owner: str


@dataclass(frozen=True)
class StandardCost:
    period_id: str
    plant_id: str
    product_id: str
    price: Decimal
    variable_material: Decimal
    variable_labor: Decimal
    variable_oh: Decimal
    fixed_oh_rate: Decimal
    variable_selling: Decimal
    std_material_qty: Decimal
    std_material_price: Decimal
    std_labor_hours: Decimal
    std_labor_rate: Decimal
    std_machine_hours: Decimal
    budget_volume: Decimal
    capacity_units: Decimal
    denominator_hours: Decimal

    @property
    def variable_manufacturing_cost(self) -> Decimal:
        return self.variable_material + self.variable_labor + self.variable_oh

    @property
    def contribution_per_unit(self) -> Decimal:
        return self.price - self.variable_manufacturing_cost - self.variable_selling


@dataclass(frozen=True)
class ProductionActual:
    period_id: str
    plant_id: str
    product_id: str
    output_units: Decimal
    material_qty_used: Decimal
    material_price: Decimal
    labor_hours: Decimal
    labor_rate: Decimal
    variable_oh_actual: Decimal
    fixed_oh_actual: Decimal
    actual_machine_hours: Decimal


@dataclass(frozen=True)
class BudgetLine:
    period_id: str
    plant_id: str
    product_id: str
    volume: Decimal
    revenue: Decimal
    material: Decimal
    labor: Decimal
    variable_oh: Decimal
    fixed_oh: Decimal
    variable_selling: Decimal


@dataclass(frozen=True)
class InventoryLine:
    period_id: str
    plant_id: str
    product_id: str
    beginning_units: Decimal
    ending_units: Decimal
    fixed_oh_rate: Decimal


@dataclass(frozen=True)
class GlBalance:
    period_id: str
    plant_id: str
    account_id: str
    actual: Decimal
    budget: Decimal
    prior: Decimal


@dataclass
class Snapshot:
    root: Path
    dataset_name: str
    file_hashes: dict[str, str]
    manifest_hash: str
    snapshot_id: str
    row_counts: dict[str, int]
    entities: list[dict[str, str]]
    plants: list[dict[str, str]]
    products: list[dict[str, str]]
    periods: list[dict[str, str]]
    accounts: dict[str, Account]
    standards: dict[tuple[str, str, str], StandardCost]
    actuals: dict[tuple[str, str, str], ProductionActual]
    budgets: dict[tuple[str, str, str], BudgetLine]
    gl: dict[tuple[str, str, str], GlBalance]
    inventory: dict[tuple[str, str, str], InventoryLine]
    control_totals: dict[tuple[str, str], Decimal]
    gates: list[Any] = field(default_factory=list)

    @property
    def currency(self) -> str:
        return self.entities[0]["currency"] if self.entities else "INR"

    def standard(self, period_id: str, plant_id: str, product_id: str) -> StandardCost:
        return self.standards[(period_id, plant_id, product_id)]

    def actual(self, period_id: str, plant_id: str, product_id: str) -> ProductionActual:
        return self.actuals[(period_id, plant_id, product_id)]

    def budget(self, period_id: str, plant_id: str, product_id: str) -> BudgetLine:
        return self.budgets[(period_id, plant_id, product_id)]

    def inventory_line(self, period_id: str, plant_id: str, product_id: str) -> InventoryLine:
        return self.inventory[(period_id, plant_id, product_id)]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def check_headers(root: Path) -> None:
    """Fail with a remediation, not a KeyError, when a template column is missing."""
    for name, columns in REQUIRED_FILES.items():
        with (root / name).open(newline="") as handle:
            header = csv.DictReader(handle).fieldnames or []
        missing = [column for column in columns if column not in header]
        if missing:
            raise DomainError(
                "SCHEMA_MISMATCH",
                f"{name} is missing required columns: {', '.join(missing)}.",
                f"Download the template for {name}, add the missing columns, and re-ingest.",
            )


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest(root: Path, names: list[str]) -> tuple[dict[str, str], str]:
    hashes = {name: _hash_file(root / name) for name in sorted(names)}
    manifest_hash = sha256_text(canonical_json({"schema_version": SCHEMA_VERSION, "files": hashes}))
    return hashes, manifest_hash


def load_snapshot(root: str | Path, dataset_name: str = "Northline Industries — Sep 2026 close") -> Snapshot:
    root = Path(root).expanduser().resolve()
    missing = [name for name in REQUIRED_FILES if not (root / name).exists()]
    if missing:
        raise FileNotFoundError(f"fixture is missing required files: {', '.join(missing)}")

    check_headers(root)

    file_hashes, manifest_hash = _manifest(root, list(REQUIRED_FILES))
    snapshot_id = "snap_" + manifest_hash[:12]

    rows = {name: _read_csv(root / name) for name in REQUIRED_FILES}
    accounts = {
        row["account_id"]: Account(
            account_id=row["account_id"],
            name=row["name"],
            statement=row["statement"],
            behavior=row["behavior"],
            directness=row["directness"],
            unit=row["unit"],
            owner=row["owner"],
        )
        for row in rows["accounts.csv"]
    }
    standards = {
        (row["period_id"], row["plant_id"], row["product_id"]): StandardCost(
            period_id=row["period_id"],
            plant_id=row["plant_id"],
            product_id=row["product_id"],
            price=dec(row["price"]),
            variable_material=dec(row["variable_material"]),
            variable_labor=dec(row["variable_labor"]),
            variable_oh=dec(row["variable_oh"]),
            fixed_oh_rate=dec(row["fixed_oh_rate"]),
            variable_selling=dec(row["variable_selling"]),
            std_material_qty=dec(row["std_material_qty"]),
            std_material_price=dec(row["std_material_price"]),
            std_labor_hours=dec(row["std_labor_hours"]),
            std_labor_rate=dec(row["std_labor_rate"]),
            std_machine_hours=dec(row["std_machine_hours"]),
            budget_volume=dec(row["budget_volume"]),
            capacity_units=dec(row["capacity_units"]),
            denominator_hours=dec(row["denominator_hours"]),
        )
        for row in rows["standard_costs.csv"]
    }
    actuals = {
        (row["period_id"], row["plant_id"], row["product_id"]): ProductionActual(
            period_id=row["period_id"],
            plant_id=row["plant_id"],
            product_id=row["product_id"],
            output_units=dec(row["output_units"]),
            material_qty_used=dec(row["material_qty_used"]),
            material_price=dec(row["material_price"]),
            labor_hours=dec(row["labor_hours"]),
            labor_rate=dec(row["labor_rate"]),
            variable_oh_actual=dec(row["variable_oh_actual"]),
            fixed_oh_actual=dec(row["fixed_oh_actual"]),
            actual_machine_hours=dec(row["actual_machine_hours"]),
        )
        for row in rows["production_actuals.csv"]
    }
    budgets = {
        (row["period_id"], row["plant_id"], row["product_id"]): BudgetLine(
            period_id=row["period_id"],
            plant_id=row["plant_id"],
            product_id=row["product_id"],
            volume=dec(row["volume"]),
            revenue=dec(row["revenue"]),
            material=dec(row["material"]),
            labor=dec(row["labor"]),
            variable_oh=dec(row["variable_oh"]),
            fixed_oh=dec(row["fixed_oh"]),
            variable_selling=dec(row["variable_selling"]),
        )
        for row in rows["budgets.csv"]
    }
    gl = {
        (row["period_id"], row["plant_id"], row["account_id"]): GlBalance(
            period_id=row["period_id"],
            plant_id=row["plant_id"],
            account_id=row["account_id"],
            actual=dec(row["actual"]),
            budget=dec(row["budget"]),
            prior=dec(row["prior"]),
        )
        for row in rows["gl_balances.csv"]
    }
    inventory = {
        (row["period_id"], row["plant_id"], row["product_id"]): InventoryLine(
            period_id=row["period_id"],
            plant_id=row["plant_id"],
            product_id=row["product_id"],
            beginning_units=dec(row["beginning_units"]),
            ending_units=dec(row["ending_units"]),
            fixed_oh_rate=dec(row["fixed_oh_rate"]),
        )
        for row in rows["inventory.csv"]
    }
    control_totals = {
        (row["period_id"], row["account_id"]): dec(row["expected_amount"])
        for row in rows["control_totals.csv"]
    }

    snapshot = Snapshot(
        root=root,
        dataset_name=dataset_name,
        file_hashes=file_hashes,
        manifest_hash=manifest_hash,
        snapshot_id=snapshot_id,
        row_counts={name: len(rows[name]) for name in rows},
        entities=rows["entities.csv"],
        plants=rows["plants.csv"],
        products=rows["products.csv"],
        periods=rows["periods.csv"],
        accounts=accounts,
        standards=standards,
        actuals=actuals,
        budgets=budgets,
        gl=gl,
        inventory=inventory,
        control_totals=control_totals,
    )
    from costpilot.data.gates import run_gates

    snapshot.gates = run_gates(snapshot)
    return snapshot
