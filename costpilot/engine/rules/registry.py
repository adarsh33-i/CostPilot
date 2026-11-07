"""Rule registry. A rule is registered once with a version and a citation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


class RuleError(KeyError):
    """Raised when a calculation cites an unregistered rule."""


@dataclass(frozen=True)
class Rule:
    id: str
    version: str
    title: str
    module: str
    formula: str
    citation: str
    golden_cases: tuple[str, ...] = field(default_factory=tuple)
    notes: str | None = None


_RULES: dict[str, Rule] = {}


def register(rule: Rule) -> Rule:
    existing = _RULES.get(rule.id)
    if existing is not None:
        if existing != rule:
            raise ValueError(f"rule {rule.id} already registered with different metadata")
        return existing
    _RULES[rule.id] = rule
    return rule


def register_many(rules: Iterable[Rule]) -> None:
    for rule in rules:
        register(rule)


def get_rule(rule_id: str) -> Rule:
    try:
        return _RULES[rule_id]
    except KeyError as exc:
        raise RuleError(f"unregistered rule {rule_id}") from exc


def all_rules() -> list[Rule]:
    return [_RULES[key] for key in sorted(_RULES)]


def registry_version() -> int:
    # M0 freezes the catalogue at version 1. Additions bump this value.
    return 1
