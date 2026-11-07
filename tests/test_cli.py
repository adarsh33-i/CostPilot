"""The command line is a second door onto the same engine and audit chain."""

from __future__ import annotations

import json
from pathlib import Path

from costpilot import cli

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("COSTPILOT_FIXTURES", str(FIXTURES))
    monkeypatch.setenv("COSTPILOT_AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("COSTPILOT_PLATFORM_DB", str(tmp_path / "platform.db"))


def test_ask_run_verify_and_runs(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    question = "Variance for PRD-B at PLT-01 in 2026-09"

    assert cli.main(["ask", question]) == 0
    assert "Status: ready" in capsys.readouterr().out

    assert cli.main(["run", question, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["card"]["intent"] == "variance_analysis"
    run_id = payload["run_id"]

    assert cli.main(["runs", "--limit", "5"]) == 0
    assert run_id in capsys.readouterr().out

    assert cli.main(["verify"]) == 0
    assert "Audit chain: ok" in capsys.readouterr().out

    assert cli.main(["connectors"]) == 0
    assert "write_back=False" in capsys.readouterr().out


def test_run_stops_on_clarification(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    assert cli.main(["run", "What should I have for lunch?"]) == 2
    out = capsys.readouterr().out
    assert "Nothing was run" in out
    assert cli.main(["verify"]) == 0
    assert "0 events checked" in capsys.readouterr().out


def test_edit_changes_the_confirmed_plan(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    assert (
        cli.main(
            [
                "run",
                "Variance for PRD-B at PLT-01 in 2026-09",
                "--edit",
                "product_id=PRD-A",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["card"]["header"]["product_id"] == "PRD-A"


def test_role_without_confirm_gets_a_remediation(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    rc = cli.main(
        [
            "run",
            "Special order for PRD-B at PLT-01: 1200 units at 940 each",
            "--user",
            "analyst",
        ]
    )
    assert rc == 3
    out = capsys.readouterr().out
    assert "E_AUTH_SCOPE" in out
    assert "nothing was written" in out
