"""Read-only connector contract and shared ingestion quality checks."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from costpilot.data.loader import Snapshot
from costpilot.engine.errors import DomainError


@dataclass(frozen=True)
class ConnectorRow:
    period_id: str
    plant_id: str
    source_account: str
    source_name: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class ConnectorGate:
    id: str
    name: str
    status: str
    detail: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"id": self.id, "name": self.name, "status": self.status, "detail": self.detail}
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


@dataclass
class ConnectorResult:
    connector: str
    system: str
    contract_version: str
    rows: list[dict[str, Any]]
    gates: list[ConnectorGate]
    control: list[dict[str, Any]] = field(default_factory=list)
    source_hash: str = ""

    @property
    def ok(self) -> bool:
        return not any(gate.status == "block" for gate in self.gates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "system": self.system,
            "contract_version": self.contract_version,
            "ok": self.ok,
            "rows": self.rows,
            "row_count": len(self.rows),
            "gates": [gate.to_dict() for gate in self.gates],
            "control": self.control,
            "source_hash": self.source_hash,
            "write_back": False,
        }


class Extractor(Protocol):
    system: str
    contract_version: str

    def extract(self, root: Path) -> list[ConnectorRow]: ...

    def control_totals(self, root: Path) -> dict[str, Decimal]: ...


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise DomainError(
            "CONNECTOR_EXTRACT_MISSING",
            f"Connector extract not found at {path.name}.",
            "Place the read-only extract in fixtures/connectors or fix the connector path.",
        )
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def check_control_totals(
    snapshot: Snapshot,
    rows: list[ConnectorRow],
    expected: dict[str, Decimal],
    system: str,
) -> tuple[ConnectorGate, list[dict[str, Any]]]:
    actual: dict[str, Decimal] = {}
    for row in rows:
        actual[row.source_account] = actual.get(row.source_account, Decimal("0")) + row.amount
    comparisons: list[dict[str, Any]] = []
    breaks: list[str] = []
    for account, amount in sorted(expected.items()):
        observed = actual.get(account, Decimal("0"))
        difference = observed - amount
        comparisons.append(
            {
                "source_account": account,
                "expected": str(amount),
                "observed": str(observed),
                "difference": str(difference),
                "reconciled": abs(difference) <= Decimal("0.01"),
            }
        )
        if abs(difference) > Decimal("0.01"):
            breaks.append(f"{account}: {observed} vs {amount}")
    if breaks:
        return (
            ConnectorGate(
                "C3", "Control totals", "block",
                f"{len(breaks)} control-total breaks: {'; '.join(breaks[:5])}",
                "Re-extract or reconcile the source system to its control total before loading.",
            ),
            comparisons,
        )
    return (
        ConnectorGate("C3", "Control totals", "pass", f"{len(expected)} source accounts reconcile"),
        comparisons,
    )


def validate_connector(
    snapshot: Snapshot,
    *,
    connector: str,
    system: str,
    contract_version: str,
    rows: list[ConnectorRow],
    mappings: dict[str, dict[str, Any]],
    expected: dict[str, Decimal],
    source_hash: str,
) -> ConnectorResult:
    gates: list[ConnectorGate] = []
    gates.append(
        ConnectorGate("C1", "Extract schema", "pass", f"{len(rows)} rows parsed read-only")
        if rows else ConnectorGate(
            "C1", "Extract schema", "block", "The extract contains no rows",
            "Check the extract date range and source filters.",
        )
    )
    dim_periods = {row["period_id"] for row in snapshot.periods}
    dim_plants = {row["plant_id"] for row in snapshot.plants}
    missing_mappings = sorted({
        row.source_account for row in rows
        if row.source_account not in mappings or mappings[row.source_account].get("status") != "approved"
    })
    gates.append(
        ConnectorGate(
            "C2", "Mapping coverage", "block",
            f"{len(missing_mappings)} source accounts lack an approved mapping: {', '.join(missing_mappings[:5])}",
            "Create a mapping proposal and have a controller approve it; unapproved rows never reach canonical facts.",
        )
        if missing_mappings else ConnectorGate(
            "C2", "Mapping coverage", "pass", f"{len(mappings)} approved mappings cover every source account"
        )
    )
    control_gate, control = check_control_totals(snapshot, rows, expected, system)
    gates.append(control_gate)
    currencies = {row.currency for row in rows}
    gates.append(
        ConnectorGate(
            "C4", "Currency scope", "block", f"mixed currencies {sorted(currencies)}",
            "Filter the extract to the reporting currency; Phase 2 has no FX translation.",
        )
        if len(currencies) != 1 or (currencies and next(iter(currencies)) != snapshot.currency)
        else ConnectorGate("C4", "Currency scope", "pass", f"single currency {snapshot.currency}")
    )
    bad_periods = sorted({row.period_id for row in rows if row.period_id not in dim_periods})
    bad_plants = sorted({row.plant_id for row in rows if row.plant_id not in dim_plants})
    gates.append(
        ConnectorGate(
            "C5", "Scope resolution", "block",
            f"unknown periods {bad_periods}, unknown plants {bad_plants}",
            "Map the source period and plant to canonical dimensions before loading.",
        )
        if bad_periods or bad_plants else ConnectorGate(
            "C5", "Scope resolution", "pass", "Every period and plant resolves"
        )
    )
    negative = [row for row in rows if row.amount < 0]
    if negative:
        gates.append(
            ConnectorGate(
                "C6", "Sign anomalies", "warn",
                f"{len(negative)} negative source amounts preserved",
                "Confirm credits or reversals; do not flip the sign silently.",
            )
        )
    else:
        gates.append(ConnectorGate("C6", "Sign anomalies", "pass", "No negative amounts"))

    normalized = [
        {
            "period_id": row.period_id,
            "plant_id": row.plant_id,
            "account_id": mappings[row.source_account]["target_account"],
            "source_system": system,
            "source_account": row.source_account,
            "source_name": row.source_name,
            "amount": str(row.amount),
            "currency": row.currency,
            "mapping_version": str(mappings[row.source_account].get("contract_version", contract_version)),
        }
        for row in rows if row.source_account in mappings
    ]
    return ConnectorResult(
        connector=connector,
        system=system,
        contract_version=contract_version,
        rows=normalized,
        gates=gates,
        control=control,
        source_hash=source_hash,
    )
