"""Decision dispatch. Every path returns a CalcResult or raises a DomainError."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from costpilot.engine.decisions import (
    add_or_drop,
    cvp,
    make_or_buy,
    product_mix,
    sell_or_process,
    shutdown,
    special_order,
)
from costpilot.engine.errors import DomainError
from costpilot.engine.money import ZERO, dec
from costpilot.engine.trace import CalcResult

SUPPORTED = {
    "special_order": special_order.run,
    "make_or_buy": make_or_buy.run,
    "add_or_drop": add_or_drop.run,
    "sell_or_process": sell_or_process.run,
    "operate_or_shutdown": shutdown.run,
    "product_mix": product_mix.run,
    "cvp": cvp.run,
}

ALLOWED_KEYS = {
    "special_order": {
        "order_units", "order_price", "variable_manufacturing_cost", "regular_price",
        "regular_variable_selling_cost", "incremental_fixed_cost", "capacity_state",
        "idle_capacity_units", "displaced_units", "variable_selling_cost_avoided",
        "utilization", "confirmed", "product_id", "plant_id", "period_id", "currency",
    },
    "make_or_buy": {
        "units", "variable_cost_per_unit", "avoidable_fixed", "purchase_price",
        "incremental_fixed_to_buy", "opportunity_cost", "materiality", "product_id", "currency",
    },
    "add_or_drop": {
        "revenue", "variable_costs", "avoidable_fixed", "allocated_fixed", "lost_units",
        "related_contribution_per_unit", "materiality", "segment_id", "currency",
    },
    "sell_or_process": {
        "units", "split_off_price", "processed_price", "processing_cost_per_unit",
        "incremental_fixed", "joint_cost", "product_id", "currency",
    },
    "operate_or_shutdown": {
        "units", "price", "variable_cost", "avoidable_fixed", "restart_cost",
        "materiality", "segment_id", "currency",
    },
    "product_mix": {"capacity_units", "products", "resource_name", "currency"},
    "cvp": {
        "price", "variable_cost", "fixed_costs", "actual_units", "target_profit",
        "product_id", "currency",
    },
}


def _d(args: dict[str, Any], key: str, default: Any = None) -> Decimal:
    if key not in args or args[key] is None:
        if default is None:
            raise DomainError(
                "MISSING_ARGUMENT",
                f"Missing required argument {key}.",
                f"Supply {key} from the confirmation sheet.",
            )
        return dec(default, key)
    return dec(args[key], key)


def _opt_d(args: dict[str, Any], key: str) -> Decimal | None:
    value = args.get(key)
    return None if value in (None, "") else dec(value, key)


def _b(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    raise DomainError("INVALID_BOOLEAN", f"{key} must be a boolean.")


def parse_arguments(decision_type: str, args: dict[str, Any], *, confirmed: bool = True) -> Any:
    if decision_type not in SUPPORTED:
        raise DomainError(
            "UNSUPPORTED_DECISION",
            f"Decision type {decision_type!r} is not registered.",
            f"Use one of: {', '.join(sorted(SUPPORTED))}.",
        )
    unknown = sorted(set(args) - ALLOWED_KEYS[decision_type])
    if unknown:
        raise DomainError(
            "UNKNOWN_ARGUMENT",
            f"Unknown argument(s): {', '.join(unknown)}.",
            "Remove the extra field; arithmetic fields are forbidden in plans.",
        )
    if decision_type == "special_order":
        return special_order.SpecialOrderInput(
            order_units=_d(args, "order_units"),
            order_price=_d(args, "order_price"),
            variable_manufacturing_cost=_d(args, "variable_manufacturing_cost"),
            regular_price=_d(args, "regular_price"),
            regular_variable_selling_cost=_d(args, "regular_variable_selling_cost", ZERO),
            incremental_fixed_cost=_d(args, "incremental_fixed_cost", ZERO),
            capacity_state=str(args.get("capacity_state", "idle")),
            idle_capacity_units=_opt_d(args, "idle_capacity_units"),
            displaced_units=_opt_d(args, "displaced_units"),
            variable_selling_cost_avoided=_b(args, "variable_selling_cost_avoided", True),
            utilization=_opt_d(args, "utilization"),
            confirmed=confirmed and _b(args, "confirmed", True),
            product_id=str(args.get("product_id", "")),
            plant_id=str(args.get("plant_id", "")),
            period_id=str(args.get("period_id", "")),
            currency=str(args.get("currency", "INR")),
        )
    if decision_type == "make_or_buy":
        return make_or_buy.MakeOrBuyInput(
            units=_d(args, "units"),
            variable_cost_per_unit=_d(args, "variable_cost_per_unit"),
            avoidable_fixed=_d(args, "avoidable_fixed", ZERO),
            purchase_price=_d(args, "purchase_price"),
            incremental_fixed_to_buy=_d(args, "incremental_fixed_to_buy", ZERO),
            opportunity_cost=_d(args, "opportunity_cost", ZERO),
            materiality=_d(args, "materiality", ZERO),
            product_id=str(args.get("product_id", "")),
            currency=str(args.get("currency", "INR")),
        )
    if decision_type == "add_or_drop":
        return add_or_drop.AddOrDropInput(
            revenue=_d(args, "revenue"),
            variable_costs=_d(args, "variable_costs"),
            avoidable_fixed=_d(args, "avoidable_fixed", ZERO),
            allocated_fixed=_d(args, "allocated_fixed", ZERO),
            lost_units=_d(args, "lost_units", ZERO),
            related_contribution_per_unit=_d(args, "related_contribution_per_unit", ZERO),
            materiality=_d(args, "materiality", ZERO),
            segment_id=str(args.get("segment_id", "")),
            currency=str(args.get("currency", "INR")),
        )
    if decision_type == "sell_or_process":
        return sell_or_process.SellOrProcessInput(
            units=_d(args, "units"),
            split_off_price=_d(args, "split_off_price"),
            processed_price=_d(args, "processed_price"),
            processing_cost_per_unit=_d(args, "processing_cost_per_unit"),
            incremental_fixed=_d(args, "incremental_fixed", ZERO),
            joint_cost=_d(args, "joint_cost", ZERO),
            product_id=str(args.get("product_id", "")),
            currency=str(args.get("currency", "INR")),
        )
    if decision_type == "operate_or_shutdown":
        return shutdown.ShutdownInput(
            units=_d(args, "units", ZERO),
            price=_d(args, "price"),
            variable_cost=_d(args, "variable_cost"),
            avoidable_fixed=_d(args, "avoidable_fixed"),
            restart_cost=_d(args, "restart_cost", ZERO),
            materiality=_d(args, "materiality", ZERO),
            segment_id=str(args.get("segment_id", "")),
            currency=str(args.get("currency", "INR")),
        )
    if decision_type == "product_mix":
        products = args.get("products")
        if not isinstance(products, list) or not products:
            raise DomainError("MISSING_ARGUMENT", "products must be a non-empty list.")
        parsed = []
        for item in products:
            if not isinstance(item, dict):
                raise DomainError("INVALID_PRODUCT", "Each product must be an object.")
            parsed.append(
                product_mix.MixProduct(
                    product_id=str(item.get("product_id", "")),
                    price=dec(item.get("price"), "price"),
                    variable_cost=dec(item.get("variable_cost"), "variable_cost"),
                    demand_units=dec(item.get("demand_units"), "demand_units"),
                    resource_units_per_unit=dec(
                        item.get("resource_units_per_unit", "1"), "resource_units_per_unit"
                    ),
                    name=str(item.get("name", "")),
                )
            )
        return product_mix.ProductMixInput(
            capacity_units=_d(args, "capacity_units"),
            products=parsed,
            resource_name=str(args.get("resource_name", "machine hours")),
            currency=str(args.get("currency", "INR")),
        )
    return cvp.CvpInput(
        price=_d(args, "price"),
        variable_cost=_d(args, "variable_cost"),
        fixed_costs=_d(args, "fixed_costs", ZERO),
        actual_units=_d(args, "actual_units", ZERO),
        target_profit=_d(args, "target_profit", ZERO),
        product_id=str(args.get("product_id", "")),
        currency=str(args.get("currency", "INR")),
    )


def run_decision(decision_type: str, args: dict[str, Any], *, confirmed: bool = True) -> CalcResult:
    parsed = parse_arguments(decision_type, args, confirmed=confirmed)
    return SUPPORTED[decision_type](parsed)
