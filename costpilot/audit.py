"""Append-only run audit with a SHA-256 hash chain and re-performance."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

GENESIS = "GENESIS"


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunRef:
    run_id: str
    event_hash: str
    prev_hash: str
    seq: int


class AuditStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    question TEXT,
                    core_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
                CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events
                BEGIN
                    SELECT RAISE(ABORT, 'audit events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events
                BEGIN
                    SELECT RAISE(ABORT, 'audit events are append-only');
                END;
                """
            )

    def append(
        self,
        *,
        tool: str,
        question: str,
        intent: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        validation: dict[str, Any],
        pins: dict[str, Any],
        confirmation: dict[str, Any] | None = None,
        user: dict[str, Any] | None = None,
        plan_id: str | None = None,
        origin: str = "user",
    ) -> RunRef:
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        run_id = "run_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(4)
        input_hash = "sha256:" + sha256_text(canonical_json(arguments))
        result_hash = "sha256:" + sha256_text(canonical_json(result))
        core = {
            "run_id": run_id,
            "created_at": now,
            "tool": tool,
            "intent": intent,
            "input_hash": input_hash,
            "result_hash": result_hash,
            "pins": pins,
        }
        core_json = canonical_json(core)
        payload = {
            "run_id": run_id,
            "created_at": now,
            "question": question,
            "intent": intent,
            "tool": {"name": tool, "version": "1.0.0"},
            "confirmation": confirmation or {},
            "pins": pins,
            "origin": origin,
            "user": user or {"user_id": "system", "role": "system"},
            "plan_id": plan_id,
            "arguments": arguments,
            "result": result,
            "validation": validation,
            "input_hash": input_hash,
            "response_hash": result_hash,
        }
        payload_json = canonical_json(payload)

        with self._lock, self._connection:
            previous = self._connection.execute(
                "SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = previous["event_hash"] if previous else GENESIS
            event_hash = sha256_text(prev_hash + core_json)
            cursor = self._connection.execute(
                """
                INSERT INTO events
                    (run_id, created_at, tool, question, core_json, payload_json, prev_hash, event_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, now, tool, question, core_json, payload_json, prev_hash, event_hash),
            )
            return RunRef(run_id, event_hash, prev_hash, int(cursor.lastrowid))

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["integrity"] = {
            "seq": row["seq"],
            "prev_hash": row["prev_hash"],
            "event_hash": row["event_hash"],
        }
        return payload

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id, created_at, tool, question, event_hash FROM events "
                "ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_runs(self, limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
        """Recent run summaries with attribution for the audit drawer."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id, created_at, tool, question, payload_json, event_hash "
                "FROM events ORDER BY seq DESC LIMIT ?",
                (limit * 4 if user_id else limit,),
            ).fetchall()
        runs: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            owner = (payload.get("user") or {}).get("user_id")
            if user_id and owner not in {user_id, "system"}:
                continue
            runs.append(
                {
                    "run_id": row["run_id"],
                    "created_at": row["created_at"],
                    "tool": row["tool"],
                    "question": row["question"],
                    "intent": payload.get("intent"),
                    "user_id": owner,
                    "plan_id": payload.get("plan_id"),
                    "event_hash": row["event_hash"],
                }
            )
            if len(runs) >= limit:
                break
        return runs

    def verify_chain(self) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM events ORDER BY seq").fetchall()
        previous = GENESIS
        for row in rows:
            if row["prev_hash"] != previous:
                return {
                    "ok": False,
                    "checked": row["seq"] - 1,
                    "first_break": row["run_id"],
                    "reason": "previous hash mismatch",
                }
            expected = sha256_text(previous + row["core_json"])
            if expected != row["event_hash"]:
                return {
                    "ok": False,
                    "checked": row["seq"] - 1,
                    "first_break": row["run_id"],
                    "reason": "event hash mismatch",
                }
            previous = row["event_hash"]
        return {"ok": True, "checked": len(rows), "first_break": None, "reason": None}

    def close(self) -> None:
        self._connection.close()


def compare_results(stored: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """Compare two result payloads step by step. Display rounding is documented."""
    stored_steps = {step["step_id"]: step for step in stored.get("steps", [])}
    fresh_steps = {step["step_id"]: step for step in fresh.get("steps", [])}
    missing = sorted(set(stored_steps) - set(fresh_steps))
    extra = sorted(set(fresh_steps) - set(stored_steps))
    mismatches: list[dict[str, str]] = []
    rounding: list[dict[str, str]] = []
    for step_id, stored_step in stored_steps.items():
        fresh_step = fresh_steps.get(step_id)
        if fresh_step is None:
            continue
        old = Decimal(stored_step["value"])
        new = Decimal(fresh_step["value"])
        if old == new:
            continue
        if abs(old - new) <= Decimal("0.01"):
            rounding.append(
                {"step_id": step_id, "stored": str(old), "recomputed": str(new)}
            )
        else:
            mismatches.append(
                {"step_id": step_id, "stored": str(old), "recomputed": str(new)}
            )
    if mismatches or missing or extra:
        status = "mismatch"
    elif rounding:
        status = "display_rounding"
    else:
        status = "exact"
    return {
        "status": status,
        "mismatches": mismatches,
        "display_rounding": rounding,
        "missing_steps": missing,
        "extra_steps": extra,
    }
