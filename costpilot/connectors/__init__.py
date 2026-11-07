"""Read-only ERP connectors. No connector writes to a source system."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from costpilot.connectors.base import ConnectorResult, validate_connector
from costpilot.connectors.flatfile import FlatFileConnector
from costpilot.connectors.netsuite import NetSuiteConnector
from costpilot.connectors.registry import MappingRegistry
from costpilot.connectors.sap import SAPConnector
from costpilot.data.loader import Snapshot
from costpilot.engine.errors import DomainError

CONNECTORS = {
    "netsuite": NetSuiteConnector,
    "sap": SAPConnector,
    "flatfile": FlatFileConnector,
}


def run_connector(
    snapshot: Snapshot,
    registry: MappingRegistry,
    name: str,
    *,
    extract_root: Path | None = None,
) -> dict[str, Any]:
    connector_class = CONNECTORS.get(name)
    if connector_class is None:
        raise DomainError(
            "CONNECTOR_UNKNOWN",
            f"Connector {name!r} is not registered.",
            "Use one of: " + ", ".join(sorted(CONNECTORS)) + ".",
        )
    connector = connector_class()
    # Connector extracts ship with the product, not with each uploaded dataset.
    root = extract_root or snapshot.root
    rows = connector.extract(root)
    result = validate_connector(
        snapshot,
        connector=name,
        system=connector.system,
        contract_version=connector.contract_version,
        rows=rows,
        mappings=registry.approved(connector.system),
        expected=connector.control_totals(root),
        source_hash=connector.source_hash(root),
    )
    payload = result.to_dict()
    payload["read_only"] = True
    payload["source_system"] = connector.system
    return payload


__all__ = ["CONNECTORS", "ConnectorResult", "MappingRegistry", "run_connector"]
