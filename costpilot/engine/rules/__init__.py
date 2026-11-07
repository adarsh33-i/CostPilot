"""Importing this module freezes the rulebook into the registry."""

from costpilot.engine.rules import (  # noqa: F401
    catalog,
    catalog_documents,
    catalog_phase2,
    catalog_statements,
)
from costpilot.engine.rules.registry import all_rules, get_rule, registry_version

__all__ = ["all_rules", "get_rule", "registry_version"]
