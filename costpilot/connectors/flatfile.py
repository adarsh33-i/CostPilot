"""Generic flat-file GL extract connector."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

from costpilot.connectors.base import ConnectorRow, read_csv
from costpilot.engine.money import dec


class FlatFileConnector:
    system = "flatfile"
    contract_version = "1"

    def extract(self, root: Path) -> list[ConnectorRow]:
        rows = read_csv(root / "connectors" / "flat_gl_extract.csv")
        return [
            ConnectorRow(
                period_id=row["period_id"],
                plant_id=row["plant_id"],
                source_account=row["account_id"],
                source_name=row.get("account_id", ""),
                amount=dec(row["amount"]),
                currency=row["currency"],
            )
            for row in rows
        ]

    def control_totals(self, root: Path) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for row in self.extract(root):
            totals[row.source_account] = totals.get(row.source_account, Decimal("0")) + row.amount
        return totals

    def source_hash(self, root: Path) -> str:
        path = root / "connectors" / "flat_gl_extract.csv"
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
