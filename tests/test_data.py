"""Snapshots, manifests, and ingestion gates on the synthetic fixture."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from costpilot.data.loader import load_snapshot, sha256_text
from costpilot.engine.errors import DomainError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def copy_fixtures(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, target)
    return target


def rewrite(path: Path, transform) -> None:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows = transform(rows)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def gate(snapshot, gate_id: str):
    return next(g for g in snapshot.gates if g.id == gate_id)


def test_snapshot_id_is_reproducible_from_the_manifest(snapshot):
    assert snapshot.snapshot_id == "snap_" + snapshot.manifest_hash[:12]
    again = load_snapshot(FIXTURES)
    assert again.snapshot_id == snapshot.snapshot_id
    assert again.file_hashes == snapshot.file_hashes
    assert again.row_counts == snapshot.row_counts


def test_manifest_hash_matches_canonical_rehash(snapshot):
    from costpilot import SCHEMA_VERSION
    from costpilot.data.loader import canonical_json

    recomputed = sha256_text(
        canonical_json({"schema_version": SCHEMA_VERSION, "files": snapshot.file_hashes})
    )
    assert recomputed == snapshot.manifest_hash


def test_all_twelve_gates_pass_on_the_committed_fixture(snapshot):
    assert [g.id for g in snapshot.gates] == [f"G{i}" for i in range(1, 13)]
    assert all(g.status == "pass" for g in snapshot.gates), [
        (g.id, g.detail) for g in snapshot.gates if g.status != "pass"
    ]


def test_fixture_has_three_products_two_plants_three_periods(snapshot):
    assert len(snapshot.products) == 3
    assert len(snapshot.plants) == 2
    assert len(snapshot.periods) == 3
    assert len(snapshot.standards) == 18
    assert len(snapshot.actuals) == 18
    assert snapshot.currency == "INR"
    assert len(snapshot.activities) == 5
    assert len(snapshot.cost_pools) == 30
    assert len(snapshot.customers) == 6
    assert len(snapshot.entity_financials) == 3


def test_capability_defaults_resolve_from_the_snapshot(snapshot):
    from costpilot.data.capability import build_capability

    capability = build_capability(snapshot)
    defaults = capability["demo_defaults"]
    assert defaults["period_id"] == "2026-09"
    assert defaults["plant_id"] == "PLT-01"
    assert defaults["product_id"] == "PRD-B"
    assert defaults["variable_manufacturing_cost"] == "300"
    assert defaults["regular_price"] == "500"
    assert defaults["idle_capacity_units"] == "2200"
    assert capability["analysis_ready"]["abc"] is True


def test_schema_mismatch_raises_a_remediable_error(tmp_path):
    root = copy_fixtures(tmp_path)
    rewrite(root / "budgets.csv", lambda rows: [{k: v for k, v in r.items() if k != "volume"} for r in rows])
    with pytest.raises(DomainError) as caught:
        load_snapshot(root)
    assert caught.value.code == "SCHEMA_MISMATCH"
    assert "volume" in caught.value.message
    assert caught.value.remediation


def test_orphan_actual_blocks_and_names_the_mapping_queue(tmp_path):
    root = copy_fixtures(tmp_path)

    def mutate(rows):
        rows[0]["plant_id"] = "PLT-99"
        return rows

    rewrite(root / "production_actuals.csv", mutate)
    snapshot = load_snapshot(root)
    result = gate(snapshot, "G2")
    assert result.status == "block"
    assert "mapping queue" in result.remediation


def test_duplicate_actual_blocks_snapshot(tmp_path):
    root = copy_fixtures(tmp_path)

    def mutate(rows):
        return rows + [dict(rows[0])]

    rewrite(root / "production_actuals.csv", mutate)
    snapshot = load_snapshot(root)
    assert gate(snapshot, "G4").status == "block"


def test_control_total_break_blocks(tmp_path):
    root = copy_fixtures(tmp_path)

    def mutate(rows):
        rows[0]["expected_amount"] = "1"
        return rows

    rewrite(root / "control_totals.csv", mutate)
    snapshot = load_snapshot(root)
    assert gate(snapshot, "G5").status == "block"


def test_missing_period_blocks_completeness(tmp_path):
    root = copy_fixtures(tmp_path)
    rewrite(root / "production_actuals.csv", lambda rows: [r for r in rows if r["period_id"] != "2026-07"])
    snapshot = load_snapshot(root)
    assert gate(snapshot, "G3").status == "block"


def test_mixed_currency_blocks(tmp_path):
    root = copy_fixtures(tmp_path)

    def mutate(rows):
        second = dict(rows[0], entity_id="ENT-02", currency="USD")
        return rows + [second]

    rewrite(root / "entities.csv", mutate)
    snapshot = load_snapshot(root)
    assert gate(snapshot, "G8").status == "block"


def test_untagged_account_blocks_coverage(tmp_path):
    root = copy_fixtures(tmp_path)

    def mutate(rows):
        for row in rows:
            row["behavior"] = ""
        return rows

    rewrite(root / "accounts.csv", mutate)
    snapshot = load_snapshot(root)
    assert gate(snapshot, "G6").status == "block"


def test_price_outlier_warns_but_does_not_block(tmp_path):
    root = copy_fixtures(tmp_path)

    def mutate(rows):
        rows[0]["material_price"] = "110"
        return rows

    rewrite(root / "production_actuals.csv", mutate)
    snapshot = load_snapshot(root)
    assert gate(snapshot, "G10").status == "warn"
    assert not any(g.status == "block" for g in snapshot.gates)


def test_missing_standard_card_blocks(tmp_path):
    root = copy_fixtures(tmp_path)
    rewrite(root / "standard_costs.csv", lambda rows: rows[1:])
    snapshot = load_snapshot(root)
    assert gate(snapshot, "G11").status == "block"
    assert gate(snapshot, "G4").status == "pass"


def test_corrected_file_changes_the_snapshot_id(tmp_path):
    root = copy_fixtures(tmp_path)
    original = load_snapshot(root)
    rewrite(root / "periods.csv", lambda rows: [dict(r, label=r["label"] + " ") for r in rows])
    corrected = load_snapshot(root)
    assert corrected.snapshot_id != original.snapshot_id
    assert corrected.manifest_hash != original.manifest_hash
