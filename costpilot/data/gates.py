"""Ingestion quality gates G1-G12. Blocking gates prevent a snapshot from being trusted."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from costpilot.data.loader import REQUIRED_FILES

if TYPE_CHECKING:
    from costpilot.data.loader import Snapshot

ALLOWED_UNITS = {"INR"}


@dataclass(frozen=True)
class GateResult:
    id: str
    name: str
    status: str  # pass | warn | block
    detail: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"id": self.id, "name": self.name, "status": self.status, "detail": self.detail}
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


def _block(gate_id: str, name: str, detail: str, remediation: str) -> GateResult:
    return GateResult(gate_id, name, "block", detail, remediation)


def _warn(gate_id: str, name: str, detail: str, remediation: str | None = None) -> GateResult:
    return GateResult(gate_id, name, "warn", detail, remediation)


def _pass(gate_id: str, name: str, detail: str) -> GateResult:
    return GateResult(gate_id, name, "pass", detail)


def run_gates(snapshot: Snapshot) -> list[GateResult]:
    gates: list[GateResult] = []

    # G1 schema and non-empty required files
    empty = [name for name, count in snapshot.row_counts.items() if count == 0]
    gates.append(
        _block("G1", "Schema", f"Empty files: {', '.join(empty)}", "Repopulate the missing template.")
        if empty
        else _pass("G1", "Schema", f"{len(REQUIRED_FILES)} required files parsed")
    )

    # G2 referential integrity across required and optional Phase 2 facts
    dim_plants = {row["plant_id"] for row in snapshot.plants}
    dim_products = {row["product_id"] for row in snapshot.products}
    dim_periods = {row["period_id"] for row in snapshot.periods}
    dim_entities = {row["entity_id"] for row in snapshot.entities}
    orphan_standards = [
        key for key in snapshot.standards
        if key[0] not in dim_periods or key[1] not in dim_plants or key[2] not in dim_products
    ]
    orphan_gl = [
        key for key in snapshot.gl
        if key[0] not in dim_periods or key[1] not in dim_plants or key[2] not in snapshot.accounts
    ]

    def _orphans(rows: dict) -> list:
        return [
            key for key in rows
            if key[0] not in dim_periods or key[1] not in dim_plants or key[2] not in dim_products
        ]

    orphan_actuals = _orphans(snapshot.actuals)
    orphan_budgets = _orphans(snapshot.budgets)
    orphan_inventory = _orphans(snapshot.inventory)
    orphan_pools = [
        key for key in snapshot.cost_pools
        if key[0] not in dim_periods or key[1] not in dim_plants
        or snapshot.cost_pools[key].activity_id not in snapshot.activities
    ]
    orphan_drivers = [
        key for key in snapshot.driver_events
        if key[0] not in dim_periods or key[1] not in dim_plants or key[2] not in dim_products
        or key[3] not in snapshot.activities
    ]
    orphan_customer_activity = [
        key for key in snapshot.customer_activity
        if key[0] not in dim_periods or key[1] not in dim_plants
        or key[2] not in snapshot.customers or key[3] not in dim_products
    ]
    orphan_entity_financials = [
        key for key in snapshot.entity_financials if key[1] not in dim_entities
    ]
    orphan_assets = [
        key for key in snapshot.assets
        if key[0] not in dim_periods or snapshot.assets[key].plant_id not in dim_plants
    ]
    orphan_total = (
        len(orphan_standards) + len(orphan_gl) + len(orphan_actuals)
        + len(orphan_budgets) + len(orphan_inventory)
        + len(orphan_pools) + len(orphan_drivers) + len(orphan_customer_activity)
        + len(orphan_entity_financials) + len(orphan_assets)
    )
    gates.append(
        _block(
            "G2", "Referential integrity",
            f"{orphan_total} orphan rows: standards {len(orphan_standards)}, GL {len(orphan_gl)}, "
            f"actuals {len(orphan_actuals)}, budgets {len(orphan_budgets)}, "
            f"inventory {len(orphan_inventory)}, pools {len(orphan_pools)}, "
            f"drivers {len(orphan_drivers)}, customer activity {len(orphan_customer_activity)}, "
            f"entity financials {len(orphan_entity_financials)}, assets {len(orphan_assets)}",
            "Send unknown period/plant/product members to the mapping queue; do not load them "
            "into canonical facts.",
        )
        if orphan_total
        else _pass("G2", "Referential integrity", "All dimension references resolve")
    )

    # G3 completeness
    expected = {
        (period["period_id"], plant["plant_id"], product["product_id"])
        for period in snapshot.periods
        for plant in snapshot.plants
        for product in snapshot.products
    }
    missing_actual = expected - set(snapshot.actuals)
    missing_budget = expected - set(snapshot.budgets)
    missing_inventory = expected - set(snapshot.inventory)
    if missing_actual or missing_budget or missing_inventory:
        gates.append(
            _block(
                "G3", "Period completeness",
                f"{len(missing_actual)} missing actuals, {len(missing_budget)} missing budgets, "
                f"{len(missing_inventory)} missing inventory rows",
                "Load the missing period/product rows before analysis.",
            )
        )
    else:
        gates.append(
            _pass("G3", "Period completeness", f"{len(expected)} product-plant-period combinations complete")
        )

    # G4 duplicates (canonical keys must be unique)
    duplicate_standards = max(
        0, snapshot.row_counts["standard_costs.csv"] - len(snapshot.standards)
    )
    duplicate_actuals = max(
        0, snapshot.row_counts["production_actuals.csv"] - len(snapshot.actuals)
    )
    duplicate_budget = max(0, snapshot.row_counts["budgets.csv"] - len(snapshot.budgets))
    duplicate_pools = max(0, snapshot.row_counts.get("cost_pools.csv", 0) - len(snapshot.cost_pools))
    duplicate_drivers = max(
        0, snapshot.row_counts.get("driver_events.csv", 0) - len(snapshot.driver_events)
    )
    if duplicate_standards or duplicate_actuals or duplicate_budget or duplicate_pools or duplicate_drivers:
        gates.append(
            _block("G4", "Duplicate keys",
                   f"duplicate rows: standards {duplicate_standards}, actuals {duplicate_actuals}, "
                   f"budgets {duplicate_budget}, pools {duplicate_pools}, drivers {duplicate_drivers}",
                   "Deduplicate on period × plant × product (and activity for pools/drivers).")
        )
    else:
        gates.append(_pass("G4", "Duplicate keys", "Composite keys are unique"))

    # G5 control totals
    breaks: list[str] = []
    for (period_id, account_id), expected_amount in snapshot.control_totals.items():
        actual = sum(
            (row.actual for key, row in snapshot.gl.items()
             if key[0] == period_id and key[2] == account_id),
            Decimal("0"),
        )
        tolerance = max(Decimal("1"), abs(expected_amount) * Decimal("0.0005"))
        if abs(actual - expected_amount) > tolerance:
            breaks.append(f"{period_id}/{account_id}: {actual} vs {expected_amount}")
    gates.append(
        _block("G5", "Control totals", f"{len(breaks)} breaks: {'; '.join(breaks[:5])}",
               "Reconcile the extract to the GL control total.")
        if breaks
        else _pass("G5", "Control totals", "All period/account control totals reconcile")
    )

    # G6 tag coverage, weighted by absolute GL amount, plus the single-account cap
    total_amount = sum((abs(row.actual) for row in snapshot.gl.values()), Decimal("0"))
    tagged_amount = Decimal("0")
    untagged_amount: dict[str, Decimal] = {}
    for row in snapshot.gl.values():
        account = snapshot.accounts.get(row.account_id)
        if account and account.behavior and account.directness and account.unit:
            tagged_amount += abs(row.actual)
        else:
            untagged_amount[row.account_id] = untagged_amount.get(row.account_id, Decimal("0")) + abs(row.actual)
    coverage = (tagged_amount / total_amount) if total_amount else Decimal("0")
    largest_untagged = max(untagged_amount.values(), default=Decimal("0"))
    untagged_share = (largest_untagged / total_amount) if total_amount else Decimal("0")
    if coverage < Decimal("0.99") or untagged_share > Decimal("0.02"):
        gates.append(
            _block("G6", "Classification coverage",
                   f"weighted coverage {coverage:.4f}; largest untagged account "
                   f"{untagged_share:.4f}; untagged accounts: {sorted(untagged_amount)}",
                   "Tag every account as fixed/variable and direct/indirect.")
        )
    else:
        gates.append(
            _pass("G6", "Classification coverage",
                  f"weighted coverage {coverage:.4f}, no untagged account above 2%")
        )

    # G7 unit consistency
    unit_issues: list[str] = []
    for account in snapshot.accounts.values():
        if account.unit not in ALLOWED_UNITS:
            unit_issues.append(f"{account.account_id}={account.unit or 'blank'}")
    for activity in snapshot.activities.values():
        if not activity.driver_unit:
            unit_issues.append(f"{activity.activity_id}=blank driver unit")
    if unit_issues:
        gates.append(
            _block("G7", "Unit consistency", f"{len(unit_issues)} unit issues: {', '.join(unit_issues[:5])}",
                   "Set every account to a supported unit (INR) and give every activity a driver unit.")
        )
    else:
        gates.append(_pass("G7", "Unit consistency", "Account and driver units are consistent"))

    # G8 currency
    currencies = {row["currency"] for row in snapshot.entities}
    if len(currencies) != 1:
        gates.append(
            _block("G8", "Currency scope", f"mixed currencies {sorted(currencies)}",
                   "Filter to one entity/currency; Phase 1 has no FX translation.")
        )
    else:
        gates.append(_pass("G8", "Currency scope", f"single currency {next(iter(currencies))}"))

    # G9 sign anomalies
    sign_issues: list[str] = []
    for key, row in snapshot.gl.items():
        account = snapshot.accounts.get(key[2])
        if account and account.behavior == "revenue" and row.actual < 0:
            sign_issues.append(f"{key[0]}/{key[1]}/{key[2]}")
    if sign_issues:
        gates.append(
            _warn("G9", "Sign anomalies", f"{len(sign_issues)} negative revenue rows",
                  "Confirm credits or reversals; preserve the sign in drill-down.")
        )
    else:
        gates.append(_pass("G9", "Sign anomalies", "No negative revenue balances"))

    # G10 outlier check on material price
    outliers: list[str] = []
    for key, actual in snapshot.actuals.items():
        standard = snapshot.standards.get(key)
        if standard and standard.std_material_price > 0:
            ratio = actual.material_price / standard.std_material_price
            if ratio < Decimal("0.75") or ratio > Decimal("1.25"):
                outliers.append(f"{key[0]}/{key[1]}/{key[2]} ({ratio:.2f}x)")
    if outliers:
        gates.append(
            _warn("G10", "Outliers", f"{len(outliers)} material prices outside 75-125% of standard",
                  "Review the price rows; do not silently adjust them.")
        )
    else:
        gates.append(_pass("G10", "Outliers", "Material prices within 75-125% of standard"))

    # G11 missing standard cards
    missing_cards = expected - set(snapshot.standards)
    if missing_cards:
        gates.append(
            _block("G11", "Standard cards",
                   f"{len(missing_cards)} missing standard cost cards",
                   "Upload the standard cost card for every period/plant/product.")
        )
    else:
        gates.append(_pass("G11", "Standard cards", "Every product has a standard cost card"))

    # G12 usage completeness
    missing_usage = [
        key for key, actual in snapshot.actuals.items()
        if actual.material_qty_used is None or actual.labor_hours is None
    ]
    if missing_usage:
        gates.append(
            _block("G12", "Usage completeness",
                   f"{len(missing_usage)} actual rows without material or labour usage",
                   "Upload actual material quantity and labour hours for every product.")
        )
    else:
        gates.append(_pass("G12", "Usage completeness", "Every actual has material and labour usage"))

    return gates
