"""Account-registry governance: weighted grid, proposals, approval, versioning.

Registry changes are separate from the data snapshot. A run pins both the
snapshot and the registry version. Changes apply only after approval and are
reported in the audit event stream; they are never inferred silently.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from costpilot.data.gates import run_gates
from costpilot.data.loader import Account, Snapshot
from costpilot.engine.errors import DomainError
from costpilot.platform.auth import User
from costpilot.platform.store import PlatformStore

BEHAVIORS = {"fixed", "variable", "semi_variable"}
DIRECTNESS = {"direct", "indirect"}
CONTROLLABILITY = {"controllable", "uncontrollable"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class RegistryService:
    def __init__(self, snapshot: Snapshot, store: PlatformStore) -> None:
        self.snapshot = snapshot
        self.store = store
        self.apply_approved()

    def version(self) -> int:
        record = self.store.get_object("registry_version", "current")
        return int(record.get("version", 1)) if record else 1

    def apply_approved(self) -> int:
        proposals = [
            row for row in self.store.list_objects("registry_proposal")
            if row.get("status") == "approved"
        ]
        if not proposals:
            return self.version()
        accounts = dict(self.snapshot.accounts)
        for proposal in sorted(proposals, key=lambda row: int(row.get("version", 1))):
            account = accounts.get(str(proposal["account_id"]))
            if account is None:
                continue
            changes = proposal.get("changes") or {}
            accounts[account.account_id] = Account(
                account_id=account.account_id,
                name=account.name,
                statement=changes.get("statement", account.statement),
                behavior=changes.get("behavior", account.behavior),
                directness=changes.get("directness", account.directness),
                unit=account.unit,
                owner=changes.get("owner", account.owner),
                cost_center=changes.get("cost_center", account.cost_center),
                controllability=changes.get("controllability", account.controllability),
            )
        self.snapshot.accounts = accounts
        self.snapshot.gates = run_gates(self.snapshot)
        return self.version()

    def accounts(self, *, period_id: str | None = None, plant_id: str | None = None,
                 behavior: str | None = None, statement: str | None = None,
                 query: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        totals: dict[str, Decimal] = {}
        budgets: dict[str, Decimal] = {}
        for (period, plant, account_id), row in self.snapshot.gl.items():
            if period_id and period != period_id:
                continue
            if plant_id and plant != plant_id:
                continue
            totals[account_id] = totals.get(account_id, Decimal("0")) + row.actual
            budgets[account_id] = budgets.get(account_id, Decimal("0")) + row.budget
        rows = []
        proposed = {
            row["account_id"]: row for row in self.store.list_objects("registry_proposal")
            if row.get("status") == "proposed"
        }
        for account in self.snapshot.accounts.values():
            if behavior and account.behavior != behavior:
                continue
            if statement and account.statement != statement:
                continue
            if query and query.lower() not in f"{account.account_id} {account.name}".lower():
                continue
            amount = totals.get(account.account_id, Decimal("0"))
            proportion = abs(amount) / sum((abs(value) for value in totals.values()), Decimal("1"))
            rows.append(
                {
                    "account_id": account.account_id,
                    "name": account.name,
                    "statement": account.statement,
                    "behavior": account.behavior,
                    "directness": account.directness,
                    "unit": account.unit,
                    "owner": account.owner,
                    "cost_center": account.cost_center,
                    "controllability": account.controllability,
                    "actual": str(amount),
                    "budget": str(budgets.get(account.account_id, Decimal("0"))),
                    "share": str(proportion.quantize(Decimal("0.0001"))),
                    "tagged": account.statement == "revenue"
                    or (account.behavior in BEHAVIORS and account.directness in DIRECTNESS),
                    "pending_proposal": proposed.get(account.account_id),
                }
            )
        rows.sort(key=lambda row: abs(Decimal(row["actual"])), reverse=True)
        return rows[:limit]

    def propose(self, user: User, account_id: str, changes: dict[str, Any], reason: str) -> dict[str, Any]:
        account = self.snapshot.accounts.get(account_id)
        if account is None:
            raise DomainError(
                "REGISTRY_ACCOUNT_UNKNOWN",
                f"Account {account_id} was not found.",
                "Search the registry grid for the account id and retry.",
            )
        if account.statement == "revenue" and "behavior" in (changes or {}):
            raise DomainError(
                "REGISTRY_REVENUE_BEHAVIOR",
                "Revenue accounts do not take a fixed or variable cost behaviour.",
                "Change the owner or cost center instead.",
            )
        if not reason or not str(reason).strip():
            raise DomainError(
                "REGISTRY_REASON_REQUIRED",
                "A registry change needs a reason.",
                "Add a reason such as 'fixed per maintenance contract' and resubmit.",
            )
        clean: dict[str, Any] = {}
        for key, value in (changes or {}).items():
            if key == "behavior":
                if value not in BEHAVIORS:
                    raise DomainError("REGISTRY_VALUE", "behavior must be fixed, variable, or semi_variable.", "Choose a supported behaviour.")
                clean[key] = value
            elif key == "directness":
                if value not in DIRECTNESS:
                    raise DomainError("REGISTRY_VALUE", "directness must be direct or indirect.", "Choose direct or indirect.")
                clean[key] = value
            elif key == "controllability":
                if value not in CONTROLLABILITY:
                    raise DomainError("REGISTRY_VALUE", "controllability must be controllable or uncontrollable.", "Choose a supported value.")
                clean[key] = value
            elif key in {"owner", "cost_center", "statement"}:
                clean[key] = str(value)
            else:
                raise DomainError(
                    "REGISTRY_FIELD",
                    f"{key!r} is not an editable registry field.",
                    "Edit behavior, directness, controllability, owner, cost_center, or statement.",
                )
        if not clean:
            raise DomainError("REGISTRY_EMPTY", "No registry field was changed.", "Send at least one changed field.")
        proposal_id = "reg_" + secrets.token_hex(6)
        version = self.version() + 1
        payload = {
            "proposal_id": proposal_id,
            "account_id": account_id,
            "changes": clean,
            "before": {
                "behavior": account.behavior,
                "directness": account.directness,
                "controllability": account.controllability,
                "owner": account.owner,
                "cost_center": account.cost_center,
            },
            "reason": reason,
            "status": "proposed",
            "version": version,
            "proposed_by": user.user_id,
            "proposed_at": _now(),
        }
        self.store.put_object("registry_proposal", proposal_id, payload)
        self.store.put_object(
            "event",
            "evt_" + secrets.token_hex(6),
            {"kind": "registry.proposed", "payload": {"proposal_id": proposal_id, "account_id": account_id}, "created_at": _now()},
        )
        return payload

    def approve(self, user: User, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.get_object("registry_proposal", proposal_id)
        if proposal is None:
            raise DomainError(
                "REGISTRY_PROPOSAL_UNKNOWN",
                f"Registry proposal {proposal_id} was not found.",
                "Refresh the registry grid and retry.",
            )
        if proposal.get("status") != "proposed":
            raise DomainError(
                "REGISTRY_PROPOSAL_STATE",
                f"Proposal {proposal_id} is already {proposal.get('status')}.",
                "Open the approved registry version instead.",
            )
        proposal = self.store.put_object(
            "registry_proposal",
            proposal_id,
            {**proposal, "status": "approved", "approved_by": user.user_id, "approved_at": _now()},
        )
        version = int(proposal.get("version", self.version() + 1))
        self.store.put_object(
            "registry_version",
            "current",
            {"version": version, "approved_by": user.user_id, "approved_at": _now()},
        )
        self.apply_approved()
        self.store.put_object(
            "event",
            "evt_" + secrets.token_hex(6),
            {"kind": "registry.approved", "payload": {"proposal_id": proposal_id, "version": version}, "created_at": _now()},
        )
        return proposal

    def proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.list_objects("registry_proposal")
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows
