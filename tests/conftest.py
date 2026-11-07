"""Shared fixtures. Every test runs on the committed synthetic snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from costpilot.audit import AuditStore
from costpilot.data.loader import load_snapshot
from costpilot.platform.auth import AuthService
from costpilot.platform.orchestrator import Orchestrator
from costpilot.platform.store import PlatformStore
from costpilot.service import ToolService

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="session")
def snapshot():
    return load_snapshot(FIXTURES)


@pytest.fixture()
def service(snapshot, tmp_path):
    return ToolService(
        snapshot,
        AuditStore(tmp_path / "audit.db"),
        PlatformStore(tmp_path / "platform.db"),
    )


@pytest.fixture()
def orchestrator(service):
    return Orchestrator(service, AuthService())


@pytest.fixture()
def login(orchestrator):
    def _login(user_id: str = "controller", password: str = "costpilot"):
        return orchestrator.auth.login(user_id, password).user

    return _login


@pytest.fixture()
def controller(login):
    return login("controller")


@pytest.fixture()
def session(orchestrator, controller):
    return orchestrator.create_session(controller)["session"]["session_id"]


@pytest.fixture()
def api(snapshot, tmp_path):
    from fastapi.testclient import TestClient

    from costpilot.api.main import create_app

    service = ToolService(
        snapshot,
        AuditStore(tmp_path / "audit.db"),
        PlatformStore(tmp_path / "platform.db"),
    )
    app = create_app(service)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def auth(api):
    def _auth(user_id: str = "controller", password: str = "costpilot"):
        response = api.post(
            "/v1/auth/login",
            json={"user_id": user_id, "password": password},
        )
        assert response.status_code == 200, response.text
        return {"Authorization": "Bearer " + response.json()["token"]}

    return _auth
