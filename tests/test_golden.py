"""Run the committed golden corpus through the engine."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from costpilot.engine.decisions import run_decision

GOLDEN = Path(__file__).parent / "golden" / "cases.json"
CASES = json.loads(GOLDEN.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_golden_case(case):
    result = run_decision(case["decision_type"], case["args"])
    assert result.value == Decimal(case["value"]), case["id"]
    assert result.verdict == case["verdict"], case["id"]
    for fact_id, expected in case["facts"].items():
        assert result.fact_map()[fact_id].value == Decimal(expected), f"{case['id']}:{fact_id}"
    assert result.meta["validation"]["failures"] == [], case["id"]
