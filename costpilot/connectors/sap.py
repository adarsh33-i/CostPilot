"""SAP read-only GL extract connector (flat file, no RFC calls)."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

from costpilot.connectors.base import ConnectorRow, read_csv
from costpilot.engine.money import dec


class SAPConnector:
    system = "sap"
    contract_version = "1"

    def extract(self, root: Path) -> list[ConnectorRow]:
        rows = read_csv(root / "connectors" / "sap_gl_extract.csv")
        normalized = []
        for row in rows:
            month = row.get("MONAT", "").zfill(2)
            year = row.get("GJAHR", "")
            normalized.append(
                ConnectorRow(
                    period_id=f"{year}-{month}",
                    plant_id=row["WERKS"],
                    source_account=row["HKONT"],
                    source_name=row.get("TXT50", ""),
                    amount=dec(row["DMBTR"]),
                    currency=row["WAERS"],
                )
            )
        return normalized

    def control_totals(self, root: Path) -> dict[str, Decimal]:
        rows = read_csv(root / "connectors" / "sap_control_totals.csv")
        totals: dict[str, Decimal] = {}
        for row in rows:
            account = row["source_account"]
            totals[account] = totals.get(account, Decimal("0")) + dec(row["expected_amount"])
        return totals

    def source_hash(self, root: Path) -> str:
        path = root / "connectors" / "sap_gl_extract.csv"
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
