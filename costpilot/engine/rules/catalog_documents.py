"""Rulebook additions for analysing an attached document.

These rules never infer a figure. They read a value the document already shows,
or they copy a passage verbatim. Every step cites the attachment and the row or
passage it came from.
"""

from __future__ import annotations

from costpilot.engine.rules.registry import Rule, register_many

V = "1.0.0"


def _r(rule_id: str, title: str, formula: str, citation: str) -> Rule:
    return Rule(rule_id, V, title, "documents", formula, citation)


RULES: tuple[Rule, ...] = (
    _r(
        "DOC-001",
        "Document row count",
        "count(non-empty rows in the selected table)",
        "Attachment analysis policy 1.1",
    ),
    _r(
        "DOC-002",
        "Document column sum",
        "sum(parsed values in the selected column)",
        "Attachment analysis policy 1.2",
    ),
    _r(
        "DOC-003",
        "Document column average",
        "sum(parsed values) / count(parsed values)",
        "Attachment analysis policy 1.2",
    ),
    _r(
        "DOC-004",
        "Document column extreme",
        "min(parsed values) or max(parsed values)",
        "Attachment analysis policy 1.2",
    ),
    _r(
        "DOC-005",
        "Document total difference",
        "sum(left column) - sum(right column)",
        "Attachment analysis policy 1.3",
    ),
    _r(
        "DOC-006",
        "Document row selection",
        "sort rows by column, or filter rows that contain the query",
        "Attachment analysis policy 1.4",
    ),
    _r(
        "DOC-007",
        "Document quote extraction",
        "copy matching passages and read the figures printed in them; scale lakh or crore once in the engine",
        "Attachment analysis policy 1.5",
    ),
)

register_many(RULES)
