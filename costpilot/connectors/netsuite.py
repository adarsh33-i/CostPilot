"""NetSuite read-only GL extract connector."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

from costpilot.connectors.base import ConnectorRow, read_csv
from costpilot.engine.money import dec


class NetSuiteConnector:
    system = "netsuite"
    contract_version = "1"

    def extract(self, root: Path) -> list[ConnectorRow]:
        rows = read_csv(root / "connectors" / "netsuite_gl_extract.csv")
        return [
            ConnectorRow(
                period_id=row["period_id"],
                plant_id=row["plant_id"],
                source_account=row["source_account"],
                source_name=row["source_name"],
                amount=dec(row["amount"]),
                currency=row["currency"],
            )
            for row in rows
        ]

    def control_totals(self, root: Path) -> dict[str, Decimal]:
        rows = read_csv(root / "connectors" / "netsuite_control_totals.csv")
        return {row["source_account"]: dec(row["expected_amount"]) for row in rows}

    def source_hash(self, root: Path) -> str:
        path = root / "connectors" / "netsuite_gl_extract.csv"
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
