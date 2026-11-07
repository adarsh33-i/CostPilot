"""Shared fixtures. Every test runs on the committed synthetic snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from costpilot.audit import AuditStore
from costpilot.data.loader import load_snapshot
from costpilot.service import ToolService

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="session")
def snapshot():
    return load_snapshot(FIXTURES)


@pytest.fixture()
def service(snapshot, tmp_path):
    return ToolService(snapshot, AuditStore(tmp_path / "audit.db"))


@pytest.fixture()
def api(snapshot, tmp_path):
    from fastapi.testclient import TestClient

    from costpilot.api.main import create_app

    app = create_app(ToolService(snapshot, AuditStore(tmp_path / "audit.db")))
    with TestClient(app) as client:
        yield client
