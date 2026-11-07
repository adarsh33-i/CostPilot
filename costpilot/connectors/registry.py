"""Governed account mappings: proposals, approval and version pinning."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from costpilot.data.loader import Snapshot
from costpilot.engine.errors import DomainError
from costpilot.platform.store import PlatformStore


class MappingRegistry:
    def __init__(self, snapshot: Snapshot, store: PlatformStore) -> None:
        self.snapshot = snapshot
        self.store = store

    def _fixture(self, system: str) -> list[dict[str, Any]]:
        return [
            {
                "source_system": mapping.source_system,
                "source_account": mapping.source_account,
                "source_name": mapping.source_name,
                "target_account": mapping.target_account,
                "status": mapping.status,
                "approved_by": mapping.approved_by,
                "contract_version": mapping.contract_version,
            }
            for mapping in self.snapshot.connector_mappings
            if mapping.source_system == system
        ]

    def all(self, system: str | None = None) -> list[dict[str, Any]]:
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for mapping in self.snapshot.connector_mappings:
            if system and mapping.source_system != system:
                continue
            rows[(mapping.source_system, mapping.source_account)] = {
                "source_system": mapping.source_system,
                "source_account": mapping.source_account,
                "source_name": mapping.source_name,
                "target_account": mapping.target_account,
                "status": mapping.status,
                "approved_by": mapping.approved_by,
                "contract_version": mapping.contract_version,
            }
        for proposal in self.store.list_objects("mapping_proposal"):
            if system and proposal.get("source_system") != system:
                continue
            rows[(proposal["source_system"], proposal["source_account"])] = proposal
        return sorted(rows.values(), key=lambda row: (row["source_system"], row["source_account"]))

    def approved(self, system: str) -> dict[str, dict[str, Any]]:
        return {
            row["source_account"]: row
            for row in self.all(system)
            if row.get("status") == "approved"
        }

    def propose(
        self,
        *,
        source_system: str,
        source_account: str,
        source_name: str,
        target_account: str,
        proposed_by: str,
    ) -> dict[str, Any]:
        if target_account not in self.snapshot.accounts:
            raise DomainError(
                "MAPPING_TARGET_UNKNOWN",
                f"Target account {target_account} is not in the chart of accounts.",
                "Choose an existing canonical account.",
            )
        if not source_account.strip():
            raise DomainError("MAPPING_SOURCE_EMPTY", "Source account cannot be blank.", "Use the ERP account code.")
        proposal_id = "map_" + secrets.token_hex(6)
        payload = {
            "proposal_id": proposal_id,
            "source_system": source_system,
            "source_account": source_account.strip(),
            "source_name": source_name,
            "target_account": target_account,
            "status": "pending",
            "approved_by": None,
            "proposed_by": proposed_by,
            "contract_version": str(len(self.all(source_system)) + 1),
            "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        self.store.put_object("mapping_proposal", proposal_id, payload)
        return payload

    def approve(self, proposal_id: str, approver: str) -> dict[str, Any]:
        proposal = self.store.get_object("mapping_proposal", proposal_id)
        if proposal is None:
            raise DomainError(
                "MAPPING_NOT_FOUND",
                f"Mapping proposal {proposal_id} was not found.",
                "List mapping proposals and retry with a valid ID.",
            )
        proposal.update({"status": "approved", "approved_by": approver})
        self.store.put_object("mapping_proposal", proposal_id, proposal)
        return proposal

    def reject(self, proposal_id: str, reviewer: str, reason: str) -> dict[str, Any]:
        proposal = self.store.get_object("mapping_proposal", proposal_id)
        if proposal is None:
            raise DomainError(
                "MAPPING_NOT_FOUND",
                f"Mapping proposal {proposal_id} was not found.",
                "List mapping proposals and retry with a valid ID.",
            )
        proposal.update({"status": "rejected", "approved_by": reviewer, "reason": reason})
        self.store.put_object("mapping_proposal", proposal_id, proposal)
        return proposal
