"""SQLite metadata store for sessions, plans, messages, datasets and governance records."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from costpilot.audit import canonical_json


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class PlatformStore:
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
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    plan_id TEXT,
                    run_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (kind, object_id)
                );
                """
            )

    def create_session(self, user_id: str, dataset_id: str) -> dict[str, Any]:
        session_id = "ses_" + secrets.token_hex(6)
        now = _now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO sessions (session_id, user_id, dataset_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, user_id, dataset_id, "open", now, now),
            )
        return {
            "session_id": session_id,
            "user_id": user_id,
            "dataset_id": dataset_id,
            "status": "open",
            "created_at": now,
        }

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, user_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM sessions"
        params: tuple[Any, ...] = ()
        if user_id:
            query += " WHERE user_id = ?"
            params = (user_id,)
        query += " ORDER BY created_at DESC LIMIT ?"
        params += (limit,)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def close_session(self, session_id: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE sessions SET status = 'closed', updated_at = ? WHERE session_id = ?",
                (_now(), session_id),
            )
        return cursor.rowcount > 0

    def add_message(
        self, session_id: str, role: str, text: str, plan_id: str | None = None, run_id: str | None = None
    ) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO messages (session_id, role, text, plan_id, run_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, text, plan_id, run_id, now),
            )
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id)
            )
        return {
            "message_id": cursor.lastrowid,
            "session_id": session_id,
            "role": role,
            "text": text,
            "plan_id": plan_id,
            "run_id": run_id,
            "created_at": now,
        }

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY message_id", (session_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_plan(self, plan: dict[str, Any], user_id: str) -> dict[str, Any]:
        plan_id = plan.get("plan_id") or "plan_" + secrets.token_hex(6)
        plan = dict(plan, plan_id=plan_id)
        now = _now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO plans (plan_id, session_id, user_id, status, payload_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (plan_id, plan["session_id"], user_id, plan.get("status", "proposed"),
                 canonical_json(plan), now, now),
            )
        return plan

    def update_plan(self, plan_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise KeyError(plan_id)
            payload = json.loads(row["payload_json"])
            payload.update(changes)
            self._connection.execute(
                "UPDATE plans SET status = ?, payload_json = ?, updated_at = ? WHERE plan_id = ?",
                (payload.get("status", row["status"]), canonical_json(payload), _now(), plan_id),
            )
        return payload

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"]
        return payload

    def put_object(self, kind: str, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO objects (kind, object_id, payload_json, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(kind, object_id) DO UPDATE SET payload_json = excluded.payload_json",
                (kind, object_id, canonical_json(payload), now),
            )
        return {"kind": kind, "object_id": object_id, **payload}

    def get_object(self, kind: str, object_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM objects WHERE kind = ? AND object_id = ?", (kind, object_id)
            ).fetchone()
        if row is None:
            return None
        return {"object_id": row["object_id"], **json.loads(row["payload_json"])}

    def delete_object(self, kind: str, object_id: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM objects WHERE kind = ? AND object_id = ?", (kind, object_id)
            )
        return cursor.rowcount > 0

    def list_objects(self, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM objects WHERE kind = ? ORDER BY created_at", (kind,)
            ).fetchall()
        return [{"object_id": row["object_id"], **json.loads(row["payload_json"])} for row in rows]

    def close(self) -> None:
        self._connection.close()
