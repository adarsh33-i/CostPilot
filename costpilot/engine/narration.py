"""Deterministic rationale rendering. The model, when added in M4, fills these slots."""

from __future__ import annotations

import re

from costpilot.engine.trace import CalcResult

_PLACEHOLDER = re.compile(r"\{\{f_[a-z0-9_]+\}\}")


def render(template: str, result: CalcResult) -> str:
    facts = result.fact_map()

    def replace(match: re.Match[str]) -> str:
        key = match.group(0)[2:-2]
        if key not in facts:
            raise ValueError(f"rationale cites unknown fact {key}")
        return facts[key].to_dict()["display"]

    return _PLACEHOLDER.sub(replace, template)


def is_slot_only(template: str) -> bool:
    stripped = _PLACEHOLDER.sub("", template)
    return not any(char.isdigit() for char in stripped) and "%" not in stripped and "₹" not in stripped
