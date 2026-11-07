"""Local authentication, role permissions and row-level scope for the demo platform.

Production deployments replace the local account file with OIDC; the permission and
scope checks below are the same either way.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from costpilot.engine.errors import DomainError

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"read", "export"},
    "analyst": {"read", "export", "run"},
    "controller": {"read", "export", "run", "approve", "confirm"},
    "admin": {"read", "export", "run", "approve", "confirm", "configure", "admin"},
}

SESSION_TTL = timedelta(hours=8)
_ITERATIONS = 240_000


@dataclass(frozen=True)
class Scope:
    entity_ids: frozenset[str] | None = None
    plant_ids: frozenset[str] | None = None
    product_ids: frozenset[str] | None = None

    def allows(self, *, plant_id: str | None = None, product_id: str | None = None) -> bool:
        if plant_id and self.plant_ids is not None and plant_id not in self.plant_ids:
            return False
        if product_id and self.product_ids is not None and product_id not in self.product_ids:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_ids": sorted(self.entity_ids) if self.entity_ids is not None else None,
            "plant_ids": sorted(self.plant_ids) if self.plant_ids is not None else None,
            "product_ids": sorted(self.product_ids) if self.product_ids is not None else None,
        }


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: str
    password_hash: str
    scope: Scope = field(default_factory=Scope)

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "scope": self.scope.to_dict(),
        }


@dataclass
class Session:
    token: str
    user: User
    created_at: datetime
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations))
    return hmac.compare_digest(digest.hex(), expected)


def _default_users() -> list[User]:
    password = os.environ.get("COSTPILOT_DEMO_PASSWORD", "costpilot")
    password_hash = hash_password(password, salt=f"costpilot-{password}")
    all_plants = frozenset({"PLT-01", "PLT-02"})
    return [
        User("viewer", "Demo Viewer", "viewer", password_hash,
             Scope(entity_ids=frozenset({"ENT-01"}), plant_ids=frozenset({"PLT-01"}))),
        User("analyst", "Demo Analyst", "analyst", password_hash,
             Scope(entity_ids=frozenset({"ENT-01"}), plant_ids=all_plants)),
        User("controller", "Demo Controller", "controller", password_hash,
             Scope(entity_ids=frozenset({"ENT-01", "ENT-02"}), plant_ids=all_plants)),
        User("admin", "Demo Admin", "admin", password_hash, Scope()),
    ]


def load_users() -> dict[str, User]:
    """Load users from COSTPILOT_USERS (JSON) or the documented demo accounts."""
    raw = os.environ.get("COSTPILOT_USERS")
    if not raw:
        return {user.user_id: user for user in _default_users()}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainError(
            "E_AUTH_CONFIG",
            "COSTPILOT_USERS is not valid JSON.",
            "Set COSTPILOT_USERS to a JSON array of user objects.",
        ) from exc
    users: dict[str, User] = {}
    for row in payload:
        if not {"user_id", "name", "role", "password"}.issubset(row):
            raise DomainError(
                "E_AUTH_CONFIG",
                "Each user needs user_id, name, role and password.",
                "Fix COSTPILOT_USERS before starting the service.",
            )
        scope = Scope(
            entity_ids=frozenset(row["entity_ids"]) if row.get("entity_ids") is not None else None,
            plant_ids=frozenset(row["plant_ids"]) if row.get("plant_ids") is not None else None,
            product_ids=frozenset(row["product_ids"]) if row.get("product_ids") is not None else None,
        )
        users[row["user_id"]] = User(
            user_id=row["user_id"],
            name=row["name"],
            role=row["role"],
            password_hash=hash_password(str(row["password"]), salt=f"costpilot-{row['user_id']}"),
            scope=scope,
        )
    return users


class AuthService:
    def __init__(self, users: dict[str, User] | None = None) -> None:
        self.users = users or load_users()
        self.sessions: dict[str, Session] = {}

    def login(self, user_id: str, password: str) -> Session:
        user = self.users.get(user_id)
        if user is None or not verify_password(password, user.password_hash):
            raise DomainError(
                "E_AUTH_INVALID",
                "The user id or password is incorrect.",
                "Retry with the demo credentials, or ask an administrator to reset the account.",
            )
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        session = Session(token=token, user=user, created_at=now, expires_at=now + SESSION_TTL)
        self.sessions[token] = session
        return session

    def logout(self, token: str) -> None:
        self.sessions.pop(token, None)

    def resolve(self, token: str | None) -> User:
        if not token:
            raise DomainError(
                "E_AUTH_REQUIRED",
                "An access token is required.",
                "Log in at POST /v1/auth/login and send Authorization: Bearer <token>.",
            )
        session = self.sessions.get(token)
        if session is None:
            raise DomainError(
                "E_AUTH_REQUIRED",
                "The access token is unknown.",
                "Log in again to obtain a new token.",
            )
        if session.expired:
            self.sessions.pop(token, None)
            raise DomainError(
                "E_AUTH_EXPIRED",
                "The session has expired.",
                "Log in again to obtain a new token.",
            )
        return session.user

    def require(self, user: User, permission: str) -> None:
        if not user.can(permission):
            raise DomainError(
                "E_AUTH_SCOPE",
                f"Role {user.role!r} does not include the {permission!r} permission.",
                "Ask a controller or administrator for access, or use an account with the required role.",
            )

    def check_scope(self, user: User, *, plant_id: str | None = None, product_id: str | None = None) -> None:
        if not user.scope.allows(plant_id=plant_id, product_id=product_id):
            raise DomainError(
                "E_AUTH_SCOPE",
                "The requested row is outside this user's data scope.",
                "Choose a plant or product inside your scope, or ask an administrator to widen it.",
            )
