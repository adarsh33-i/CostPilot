"""Canonical dataset snapshot: dimensions, standards, actuals, budgets, GL, inventory.

Phase 1 files are required. Phase 2 files (ABC, budgets, performance, group, connectors)
are optional: a dataset without them still loads, and the capability card marks the
dependent analyses unavailable instead of failing the whole snapshot.
"""

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
    "accounts.csv": [
        "account_id", "name", "statement", "behavior", "directness", "unit", "owner",
    ],
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

OPTIONAL_FILES = {
    "activities.csv": [
        "activity_id", "name", "driver_name", "driver_unit", "pool_group",
        "minutes_per_event", "source",
    ],
    "cost_pools.csv": [
        "period_id", "plant_id", "pool_id", "activity_id", "pool_cost",
        "driver_quantity", "capacity_events",
    ],
    "driver_events.csv": [
        "period_id", "plant_id", "product_id", "activity_id", "driver_quantity",
    ],
    "customers.csv": ["customer_id", "name", "segment", "region", "channel"],
    "customer_activity.csv": [
        "period_id", "plant_id", "customer_id", "product_id", "units", "revenue",
        "orders", "service_tickets", "deliveries",
    ],
    "customer_cost_rates.csv": ["rate_id", "name", "rate", "unit"],
    "budget_assumptions.csv": [
        "period_id", "plant_id", "product_id", "sales_units", "beginning_fg_units",
        "ending_fg_units", "beginning_rm_qty", "ending_rm_qty",
    ],
    "budget_cash.csv": [
        "period_id", "plant_id", "opening_cash", "capex", "collection_rate",
        "payment_rate", "tax_rate",
    ],
    "assets.csv": [
        "period_id", "division_id", "name", "plant_id", "beginning_assets",
        "ending_assets", "required_return", "new_investment_income",
        "new_investment_assets",
    ],
    "scorecard.csv": [
        "kpi_id", "perspective", "name", "owner", "weight", "target", "actual",
        "direction", "source", "controllability",
    ],
    "entity_financials.csv": [
        "period_id", "entity_id", "name", "revenue", "variable_income",
        "absorption_income", "inventory_change_units", "fixed_oh_rate",
    ],
    "intercompany.csv": [
        "period_id", "seller_entity", "buyer_entity", "description", "revenue",
        "cost", "status",
    ],
    "connector_mappings.csv": [
        "source_system", "source_account", "source_name", "target_account", "status",
        "approved_by", "contract_version",
    ],
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
    cost_center: str = ""
    controllability: str = ""


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


@dataclass(frozen=True)
class Activity:
    activity_id: str
    name: str
    driver_name: str
    driver_unit: str
    pool_group: str
    minutes_per_event: Decimal
    source: str


@dataclass(frozen=True)
class CostPool:
    period_id: str
    plant_id: str
    pool_id: str
    activity_id: str
    pool_cost: Decimal
    driver_quantity: Decimal
    capacity_events: Decimal

    @property
    def rate(self) -> Decimal:
        return self.pool_cost / self.driver_quantity if self.driver_quantity else Decimal("0")


@dataclass(frozen=True)
class DriverEvent:
    period_id: str
    plant_id: str
    product_id: str
    activity_id: str
    driver_quantity: Decimal


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    segment: str
    region: str
    channel: str


@dataclass(frozen=True)
class CustomerActivity:
    period_id: str
    plant_id: str
    customer_id: str
    product_id: str
    units: Decimal
    revenue: Decimal
    orders: Decimal
    service_tickets: Decimal
    deliveries: Decimal


@dataclass(frozen=True)
class CustomerCostRate:
    rate_id: str
    name: str
    rate: Decimal
    unit: str


@dataclass(frozen=True)
class BudgetAssumption:
    period_id: str
    plant_id: str
    product_id: str
    sales_units: Decimal
    beginning_fg_units: Decimal
    ending_fg_units: Decimal
    beginning_rm_qty: Decimal
    ending_rm_qty: Decimal


@dataclass(frozen=True)
class BudgetCash:
    period_id: str
    plant_id: str
    opening_cash: Decimal
    capex: Decimal
    collection_rate: Decimal
    payment_rate: Decimal
    tax_rate: Decimal


@dataclass(frozen=True)
class AssetBase:
    period_id: str
    division_id: str
    name: str
    plant_id: str
    beginning_assets: Decimal
    ending_assets: Decimal
    required_return: Decimal
    new_investment_income: Decimal
    new_investment_assets: Decimal


@dataclass(frozen=True)
class ScorecardMetric:
    kpi_id: str
    perspective: str
    name: str
    owner: str
    weight: Decimal
    target: Decimal
    actual: Decimal | None
    direction: str
    source: str
    controllability: str


@dataclass(frozen=True)
class EntityFinancial:
    period_id: str
    entity_id: str
    name: str
    revenue: Decimal
    variable_income: Decimal
    absorption_income: Decimal
    inventory_change_units: Decimal
    fixed_oh_rate: Decimal


@dataclass(frozen=True)
class IntercompanyLine:
    period_id: str
    seller_entity: str
    buyer_entity: str
    description: str
    revenue: Decimal
    cost: Decimal
    status: str


@dataclass(frozen=True)
class ConnectorMapping:
    source_system: str
    source_account: str
    source_name: str
    target_account: str
    status: str
    approved_by: str
    contract_version: str


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
    activities: dict[str, Activity] = field(default_factory=dict)
    cost_pools: dict[tuple[str, str, str], CostPool] = field(default_factory=dict)
    driver_events: dict[tuple[str, str, str, str], DriverEvent] = field(default_factory=dict)
    customers: dict[str, Customer] = field(default_factory=dict)
    customer_activity: dict[tuple[str, str, str, str], CustomerActivity] = field(default_factory=dict)
    customer_cost_rates: dict[str, CustomerCostRate] = field(default_factory=dict)
    budget_assumptions: dict[tuple[str, str, str], BudgetAssumption] = field(default_factory=dict)
    budget_cash: dict[tuple[str, str], BudgetCash] = field(default_factory=dict)
    assets: dict[tuple[str, str], AssetBase] = field(default_factory=dict)
    scorecard: dict[str, ScorecardMetric] = field(default_factory=dict)
    entity_financials: dict[tuple[str, str], EntityFinancial] = field(default_factory=dict)
    intercompany: list[IntercompanyLine] = field(default_factory=list)
    connector_mappings: list[ConnectorMapping] = field(default_factory=list)

    @property
    def currency(self) -> str:
        return self.entities[0]["currency"] if self.entities else "INR"

    def standard(self, period_id: str, plant_id: str, product_id: str) -> StandardCost:
        return self._lookup(self.standards, "standard cost", period_id, plant_id, product_id)

    def actual(self, period_id: str, plant_id: str, product_id: str) -> ProductionActual:
        return self._lookup(self.actuals, "production actual", period_id, plant_id, product_id)

    def budget(self, period_id: str, plant_id: str, product_id: str) -> BudgetLine:
        return self._lookup(self.budgets, "budget line", period_id, plant_id, product_id)

    def inventory_line(self, period_id: str, plant_id: str, product_id: str) -> InventoryLine:
        return self._lookup(self.inventory, "inventory line", period_id, plant_id, product_id)

    def _lookup(self, table: dict[Any, Any], kind: str, period_id: str, plant_id: str, product_id: str) -> Any:
        key = (period_id, plant_id, product_id)
        if key in table:
            return table[key]
        periods = sorted({item[0] for item in table}) or [row["period_id"] for row in self.periods]
        plants = sorted({item[1] for item in table}) or [row["plant_id"] for row in self.plants]
        products = sorted({item[2] for item in table}) or [row["product_id"] for row in self.products]
        details = []
        if period_id not in periods:
            details.append(f"period {period_id} is not loaded")
        if plant_id not in plants:
            details.append(f"plant {plant_id} has no rows")
        if product_id not in products:
            details.append(f"product {product_id} has no rows")
        if not details:
            details.append(f"no {kind} covers {period_id} / {plant_id} / {product_id}")
        raise DomainError(
            "E_DATA_MISSING",
            f"Cannot analyse this scope: {'; '.join(details)}.",
            (
                f"Use one of the loaded scopes: periods {', '.join(periods)}; "
                f"plants {', '.join(plants)}; products {', '.join(products)}."
            ),
        )

    def has(self, name: str) -> bool:
        return name in self.row_counts

    def plant_overhead(self, period_id: str, plant_id: str) -> Decimal:
        """Actual variable + fixed overhead recorded for one plant in one period."""
        total = Decimal("0")
        for (pid, plant, _), row in self.actuals.items():
            if pid == period_id and plant == plant_id:
                total += row.variable_oh_actual + row.fixed_oh_actual
        return total


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


def _decimal_or_none(value: str | None) -> Decimal | None:
    return dec(value) if value not in (None, "") else None


def load_snapshot(root: str | Path, dataset_name: str = "Northline Industries — Sep 2026 close") -> Snapshot:
    root = Path(root).expanduser().resolve()
    missing = [name for name in REQUIRED_FILES if not (root / name).exists()]
    if missing:
        raise FileNotFoundError(f"fixture is missing required files: {', '.join(missing)}")

    check_headers(root)

    present = list(REQUIRED_FILES) + [
        name for name in OPTIONAL_FILES if (root / name).exists()
    ]
    file_hashes, manifest_hash = _manifest(root, present)
    snapshot_id = "snap_" + manifest_hash[:12]

    rows = {name: _read_csv(root / name) for name in present}
    accounts = {
        row["account_id"]: Account(
            account_id=row["account_id"],
            name=row["name"],
            statement=row["statement"],
            behavior=row["behavior"],
            directness=row["directness"],
            unit=row["unit"],
            owner=row["owner"],
            cost_center=row.get("cost_center", ""),
            controllability=row.get("controllability", ""),
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
    _load_phase_two(snapshot, rows)
    from costpilot.data.gates import run_gates

    snapshot.gates = run_gates(snapshot)
    return snapshot


def _load_phase_two(snapshot: Snapshot, rows: dict[str, list[dict[str, str]]]) -> None:
    """Parse whichever optional Phase 2 files are present."""
    if "activities.csv" in rows:
        snapshot.activities = {
            row["activity_id"]: Activity(
                activity_id=row["activity_id"],
                name=row["name"],
                driver_name=row["driver_name"],
                driver_unit=row["driver_unit"],
                pool_group=row["pool_group"],
                minutes_per_event=dec(row["minutes_per_event"]),
                source=row["source"],
            )
            for row in rows["activities.csv"]
        }
    if "cost_pools.csv" in rows:
        snapshot.cost_pools = {
            (row["period_id"], row["plant_id"], row["pool_id"]): CostPool(
                period_id=row["period_id"],
                plant_id=row["plant_id"],
                pool_id=row["pool_id"],
                activity_id=row["activity_id"],
                pool_cost=dec(row["pool_cost"]),
                driver_quantity=dec(row["driver_quantity"]),
                capacity_events=dec(row["capacity_events"]),
            )
            for row in rows["cost_pools.csv"]
        }
    if "driver_events.csv" in rows:
        snapshot.driver_events = {
            (row["period_id"], row["plant_id"], row["product_id"], row["activity_id"]): DriverEvent(
                period_id=row["period_id"],
                plant_id=row["plant_id"],
                product_id=row["product_id"],
                activity_id=row["activity_id"],
                driver_quantity=dec(row["driver_quantity"]),
            )
            for row in rows["driver_events.csv"]
        }
    if "customers.csv" in rows:
        snapshot.customers = {
            row["customer_id"]: Customer(
                customer_id=row["customer_id"],
                name=row["name"],
                segment=row["segment"],
                region=row["region"],
                channel=row["channel"],
            )
            for row in rows["customers.csv"]
        }
    if "customer_activity.csv" in rows:
        snapshot.customer_activity = {
            (row["period_id"], row["plant_id"], row["customer_id"], row["product_id"]): CustomerActivity(
                period_id=row["period_id"],
                plant_id=row["plant_id"],
                customer_id=row["customer_id"],
                product_id=row["product_id"],
                units=dec(row["units"]),
                revenue=dec(row["revenue"]),
                orders=dec(row["orders"]),
                service_tickets=dec(row["service_tickets"]),
                deliveries=dec(row["deliveries"]),
            )
            for row in rows["customer_activity.csv"]
        }
    if "customer_cost_rates.csv" in rows:
        snapshot.customer_cost_rates = {
            row["rate_id"]: CustomerCostRate(
                rate_id=row["rate_id"],
                name=row["name"],
                rate=dec(row["rate"]),
                unit=row["unit"],
            )
            for row in rows["customer_cost_rates.csv"]
        }
    if "budget_assumptions.csv" in rows:
        snapshot.budget_assumptions = {
            (row["period_id"], row["plant_id"], row["product_id"]): BudgetAssumption(
                period_id=row["period_id"],
                plant_id=row["plant_id"],
                product_id=row["product_id"],
                sales_units=dec(row["sales_units"]),
                beginning_fg_units=dec(row["beginning_fg_units"]),
                ending_fg_units=dec(row["ending_fg_units"]),
                beginning_rm_qty=dec(row["beginning_rm_qty"]),
                ending_rm_qty=dec(row["ending_rm_qty"]),
            )
            for row in rows["budget_assumptions.csv"]
        }
    if "budget_cash.csv" in rows:
        snapshot.budget_cash = {
            (row["period_id"], row["plant_id"]): BudgetCash(
                period_id=row["period_id"],
                plant_id=row["plant_id"],
                opening_cash=dec(row["opening_cash"]),
                capex=dec(row["capex"]),
                collection_rate=dec(row["collection_rate"]),
                payment_rate=dec(row["payment_rate"]),
                tax_rate=dec(row["tax_rate"]),
            )
            for row in rows["budget_cash.csv"]
        }
    if "assets.csv" in rows:
        snapshot.assets = {
            (row["period_id"], row["division_id"]): AssetBase(
                period_id=row["period_id"],
                division_id=row["division_id"],
                name=row["name"],
                plant_id=row["plant_id"],
                beginning_assets=dec(row["beginning_assets"]),
                ending_assets=dec(row["ending_assets"]),
                required_return=dec(row["required_return"]),
                new_investment_income=dec(row["new_investment_income"]),
                new_investment_assets=dec(row["new_investment_assets"]),
            )
            for row in rows["assets.csv"]
        }
    if "scorecard.csv" in rows:
        snapshot.scorecard = {
            row["kpi_id"]: ScorecardMetric(
                kpi_id=row["kpi_id"],
                perspective=row["perspective"],
                name=row["name"],
                owner=row["owner"],
                weight=dec(row["weight"]),
                target=dec(row["target"]),
                actual=_decimal_or_none(row.get("actual")),
                direction=row["direction"],
                source=row["source"],
                controllability=row["controllability"],
            )
            for row in rows["scorecard.csv"]
        }
    if "entity_financials.csv" in rows:
        snapshot.entity_financials = {
            (row["period_id"], row["entity_id"]): EntityFinancial(
                period_id=row["period_id"],
                entity_id=row["entity_id"],
                name=row["name"],
                revenue=dec(row["revenue"]),
                variable_income=dec(row["variable_income"]),
                absorption_income=dec(row["absorption_income"]),
                inventory_change_units=dec(row["inventory_change_units"]),
                fixed_oh_rate=dec(row["fixed_oh_rate"]),
            )
            for row in rows["entity_financials.csv"]
        }
    if "intercompany.csv" in rows:
        snapshot.intercompany = [
            IntercompanyLine(
                period_id=row["period_id"],
                seller_entity=row["seller_entity"],
                buyer_entity=row["buyer_entity"],
                description=row["description"],
                revenue=dec(row["revenue"]),
                cost=dec(row["cost"]),
                status=row["status"],
            )
            for row in rows["intercompany.csv"]
        ]
    if "connector_mappings.csv" in rows:
        snapshot.connector_mappings = [
            ConnectorMapping(
                source_system=row["source_system"],
                source_account=row["source_account"],
                source_name=row["source_name"],
                target_account=row["target_account"],
                status=row["status"],
                approved_by=row["approved_by"],
                contract_version=row["contract_version"],
            )
            for row in rows["connector_mappings.csv"]
        ]
