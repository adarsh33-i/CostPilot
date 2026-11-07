"""Accept a user workbook and turn it into the canonical CSV snapshot.

A user drops one Excel workbook or a ZIP of CSVs. Sheet and file names are
normalised onto the canonical templates, missing required tables are reported as a
list with a remediation, and the resulting snapshot is sealed by the same loader
and gates as the bundled sample. Nothing is written back to the source system.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from costpilot.data.loader import OPTIONAL_FILES, REQUIRED_FILES

ROOT = Path(__file__).resolve().parents[2]


def default_datasets_root() -> Path:
    return Path(os.environ.get("COSTPILOT_DATASETS", ROOT / "data" / "datasets")).expanduser().resolve()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 120 * 1024 * 1024

CANONICAL = {**REQUIRED_FILES, **OPTIONAL_FILES}


def canonical_name(raw: str) -> str | None:
    """Map 'Standard Costs', 'standard-costs.csv' or 'standard_costs' to the template."""
    name = Path(str(raw).strip().replace("\\", "/")).name.lower().strip()
    if not name:
        return None
    stem = name[:-4] if name.endswith(".csv") else name
    slug = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    candidate = f"{slug}.csv"
    if candidate in CANONICAL:
        return candidate
    return None


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value).strip()


def _write_csv(path: Path, rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow([_cell(value) for value in row])


def _extract_zip(raw: bytes, workdir: Path) -> list[str]:
    ignored: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("The uploaded file is not a readable ZIP archive.") from exc
    total = 0
    with archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            total += member.file_size
            if member.file_size > MAX_ZIP_MEMBER_BYTES or total > MAX_ZIP_TOTAL_BYTES:
                raise ValueError("The ZIP expands beyond the allowed size.")
            target = canonical_name(member.filename)
            if target is None:
                ignored.append(Path(member.filename).name)
                continue
            text = archive.read(member).decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            _write_csv(workdir / target, rows)
    return ignored


def _extract_xlsx(raw: bytes, workdir: Path) -> tuple[list[str], list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ValueError("Excel support is not installed; use a ZIP of CSVs.") from exc
    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("The uploaded file is not a readable XLSX workbook.") from exc
    ignored: list[str] = []
    found: list[str] = []
    try:
        for sheet in workbook.worksheets:
            target = canonical_name(sheet.title)
            if target is None:
                ignored.append(sheet.title)
                continue
            rows = []
            for row in sheet.iter_rows(values_only=True):
                rows.append(list(row))
            _write_csv(workdir / target, rows)
            found.append(target)
    finally:
        workbook.close()
    return ignored, found


def summarize(workdir: Path) -> dict[str, Any]:
    present = sorted(path.name for path in workdir.glob("*.csv"))
    missing_required = [name for name in REQUIRED_FILES if name not in present]
    empty_required = []
    row_counts: dict[str, int] = {}
    for name in present:
        with (workdir / name).open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            rows = list(reader)
        header = rows[0] if rows else []
        row_counts[name] = max(0, len(rows) - 1)
        if name in REQUIRED_FILES and not header:
            empty_required.append(name)
    missing_columns = {}
    for name in present:
        if name not in REQUIRED_FILES:
            continue
        with (workdir / name).open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle), [])
        absent = [column for column in REQUIRED_FILES[name] if column not in header]
        if absent:
            missing_columns[name] = absent
    return {
        "files": present,
        "row_counts": row_counts,
        "missing_required": missing_required,
        "empty_required": empty_required,
        "missing_columns": missing_columns,
    }


def extract(raw: bytes, filename: str, workdir: Path) -> dict[str, Any]:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("The upload is larger than the 25 MB limit.")
    suffix = Path(filename or "").suffix.lower()
    ignored: list[str] = []
    if suffix == ".zip":
        ignored = _extract_zip(raw, workdir)
    elif suffix in {".xlsx", ".xlsm"}:
        ignored, _ = _extract_xlsx(raw, workdir)
    else:
        raise ValueError("Upload an .xlsx workbook or a .zip of canonical CSVs.")
    summary = summarize(workdir)
    summary["ignored_sheets"] = ignored
    return summary


def remediation(summary: dict[str, Any]) -> list[str]:
    steps: list[str] = []
    if summary.get("missing_required"):
        steps.append(
            "Add the missing tables: " + ", ".join(summary["missing_required"]) + "."
        )
    for name, columns in (summary.get("missing_columns") or {}).items():
        steps.append(f"{name} is missing columns: {', '.join(columns)}.")
    for name in summary.get("empty_required") or []:
        steps.append(f"{name} has no header row.")
    if summary.get("ignored_sheets"):
        steps.append(
            "These sheets were ignored because they match no template: "
            + ", ".join(summary["ignored_sheets"]) + "."
        )
    if not steps:
        steps.append("The workbook matches the canonical templates.")
    steps.append("Download the workbook template at GET /v1/datasets/workbook-template.")
    return steps


# ---------- datastore ----------


def _read_index(root: Path) -> list[dict[str, Any]]:
    path = root / "index.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return payload if isinstance(payload, list) else []


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def save_dataset(csv_dir: Path, root: Path, dataset_id: str) -> Path:
    target = root / dataset_id
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for source in sorted(csv_dir.glob("*.csv")):
        shutil.copy2(source, target / source.name)
    return target


def record_dataset(root: Path, entry: dict[str, Any]) -> list[dict[str, Any]]:
    index = [row for row in _read_index(root) if row.get("dataset_id") != entry.get("dataset_id")]
    index.insert(0, entry)
    _write_json(root / "index.json", index)
    return index


def list_datasets(root: Path) -> list[dict[str, Any]]:
    return _read_index(root)


def set_active(root: Path, path: Path, name: str) -> None:
    _write_json(root / "active.json", {"path": str(path), "name": name})


def active_dataset(root: Path) -> dict[str, Any] | None:
    path = root / "active.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not payload.get("path") or not Path(payload["path"]).is_dir():
        return None
    return payload


def workbook_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    readme = workbook.active
    readme.title = "README"
    readme.append(["CostPilot dataset workbook"])
    readme.append(["One sheet per canonical table. Sheet names are case-insensitive."])
    readme.append(["Required tables: " + ", ".join(sorted(REQUIRED_FILES))])
    readme.append(["Optional tables: " + ", ".join(sorted(OPTIONAL_FILES))])
    readme.append(["Amounts in the reporting currency of entities.csv; periods as YYYY-MM."])
    readme.append(["Close periods with status=closed; leave the current period open."])
    for name, columns in sorted(CANONICAL.items()):
        sheet = workbook.create_sheet(title=name[:-4][:31])
        sheet.append(list(columns))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
