"""A user workbook becomes the live snapshot; the sample is only a starting point."""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from costpilot.data.loader import OPTIONAL_FILES, REQUIRED_FILES

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _rows(name: str, skip: set[str] | None = None) -> list[list[str]]:
    if name in (skip or set()):
        return []
    with (FIXTURES / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def workbook_bytes(skip=None, only=None, edit=None) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for name in list(REQUIRED_FILES) + list(OPTIONAL_FILES):
        if only is not None and name not in only:
            continue
        rows = _rows(name, skip)
        if edit is not None:
            rows = edit(name, rows)
        if not rows:
            continue
        sheet = workbook.create_sheet(title=name[:-4])
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def zip_bytes(names: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, (FIXTURES / name).read_bytes())
    return buffer.getvalue()


def prepare(api, tmp_path, monkeypatch):
    service = api.app.state.service
    service.datasets_root = tmp_path / "datasets"
    monkeypatch.setenv("COSTPILOT_DATASETS", str(tmp_path / "datasets"))
    monkeypatch.setenv("COSTPILOT_AUDIT_DB", str(tmp_path / "restored-audit.db"))
    monkeypatch.setenv("COSTPILOT_PLATFORM_DB", str(tmp_path / "restored-platform.db"))
    monkeypatch.delenv("COSTPILOT_FIXTURES", raising=False)
    return service


def test_workbook_upload_activates_and_analyses(api, auth, tmp_path, monkeypatch):
    service = prepare(api, tmp_path, monkeypatch)
    before = service.snapshot.snapshot_id

    def raise_material_price(name, rows):
        if name != "standard_costs.csv":
            return rows
        column = rows[0].index("std_material_price")
        for row in rows[1:]:
            if row[0] == "2026-09" and row[2] == "PRD-B":
                row[column] = str(float(row[column]) + 10)
        return rows

    response = api.post(
        "/v1/datasets/upload",
        files={"file": ("close.xlsx", workbook_bytes(edit=raise_material_price), XLSX)},
        data={"name": "Acme Components — Sep 2026"},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["activated"] is True
    assert body["dataset"]["name"] == "Acme Components — Sep 2026"
    assert body["dataset"]["source"] == "upload"
    assert body["dataset"]["dataset_id"] != before
    assert body["capability"]["analysis_ready"]
    assert body["dataset"]["currency"] == "INR"

    listing = api.get("/v1/datasets", headers=auth()).json()
    current = listing["current"]
    assert current["dataset_id"] == body["dataset"]["dataset_id"]
    assert listing["datasets"][0]["current"] is True

    orch = api.app.state.orchestrator
    user = orch.auth.login("controller", "costpilot").user
    session_id = orch.create_session(user)["session"]["session_id"]
    plan = orch.post_message(user, session_id, "Variance for PRD-B at PLT-01 in 2026-09")
    assert plan["status"] == "ready"
    result = orch.confirm_plan(user, plan["plan_id"])
    assert result["status"] == "completed"
    assert result["card"]["header"]["snapshot_id"] == body["dataset"]["dataset_id"]
    assert result["card"]["facts"]

    restored = type(service).from_env()
    assert restored.snapshot.snapshot_id == body["dataset"]["dataset_id"]


def test_zip_of_csvs_is_accepted(api, auth, tmp_path, monkeypatch):
    prepare(api, tmp_path, monkeypatch)
    response = api.post(
        "/v1/datasets/upload",
        files={
            "file": (
                "bundle.zip",
                zip_bytes(list(REQUIRED_FILES) + list(OPTIONAL_FILES)),
                "application/zip",
            )
        },
        data={"name": "Zipped close"},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["dataset"]["name"] == "Zipped close"


def test_missing_table_gets_a_remediation_not_a_crash(api, auth, tmp_path, monkeypatch):
    prepare(api, tmp_path, monkeypatch)
    response = api.post(
        "/v1/datasets/upload",
        files={"file": ("close.xlsx", workbook_bytes(skip={"standard_costs.csv"}), XLSX)},
        data={"name": "Incomplete"},
        headers=auth(),
    )
    assert response.status_code == 422, response.text
    error = response.json()
    assert error["error"]["code"] == "SCHEMA_MISMATCH"
    assert "standard_costs.csv" in error["error"]["message"]
    assert any("workbook template" in item.lower() for item in error["remediation"])


def test_missing_column_is_named(api, auth, tmp_path, monkeypatch):
    prepare(api, tmp_path, monkeypatch)
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for name in REQUIRED_FILES:
        rows = _rows(name)
        if name == "inventory.csv":
            rows = [row[:-1] for row in rows]
        sheet = workbook.create_sheet(title=name[:-4])
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)

    response = api.post(
        "/v1/datasets/upload",
        files={"file": ("close.xlsx", buffer.getvalue(), XLSX)},
        data={"name": "Broken"},
        headers=auth(),
    )
    assert response.status_code == 422, response.text
    remediation = " ".join(response.json()["remediation"])
    assert "fixed_oh_rate" in remediation


def test_blocking_gate_keeps_the_current_snapshot(api, auth, tmp_path, monkeypatch):
    service = prepare(api, tmp_path, monkeypatch)
    before = service.snapshot.snapshot_id

    def orphan_standard(name, rows):
        if name != "standard_costs.csv":
            return rows
        column = rows[0].index("product_id")
        rows[1][column] = "PRD-Z"
        return rows

    response = api.post(
        "/v1/datasets/upload",
        files={"file": ("orphan.xlsx", workbook_bytes(edit=orphan_standard), XLSX)},
        data={"name": "Broken close"},
        headers=auth(),
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "E_DATA_QUALITY"
    assert "G2" in body["error"]["message"]
    assert body["remediation"]
    assert service.snapshot.snapshot_id == before


def test_unsupported_file_type_is_rejected_with_the_template(api, auth, tmp_path, monkeypatch):
    prepare(api, tmp_path, monkeypatch)
    response = api.post(
        "/v1/datasets/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=auth(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "E_DATASET_FORMAT"
    assert "workbook-template" in " ".join(response.json()["remediation"])


def test_workbook_template_downloads_all_sheets(api, auth):
    response = api.get("/v1/datasets/workbook-template", headers=auth())
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(response.content), read_only=True)
    titles = set(workbook.sheetnames)
    assert "README" in titles
    assert {name[:-4] for name in REQUIRED_FILES} <= titles
    workbook.close()
