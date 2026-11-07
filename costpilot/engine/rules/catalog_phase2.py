"""Phase 2 rulebook additions: ABC, budgets, performance, monitoring, group reporting."""

from __future__ import annotations

from costpilot.engine.rules.registry import Rule, register_many

V = "2.0.0"


def _r(rule_id: str, title: str, module: str, formula: str, citation: str, cases: tuple[str, ...] = ()) -> Rule:
    return Rule(rule_id, V, title, module, formula, citation, cases)


RULES: tuple[Rule, ...] = (
    # Activity-based costing
    _r("ABC-001", "Activity pool rate", "abc", "pool_cost / driver_quantity", "Blueprint 7.2", ("GOLDEN-ABC-001",)),
    _r("ABC-002", "Product activity cost", "abc", "sum(driver_quantity x pool_rate)", "Blueprint 7.2", ("GOLDEN-ABC-001",)),
    _r("ABC-003", "Traditional overhead rate", "abc", "total_overhead / total_allocation_base", "Blueprint 7.2", ("GOLDEN-ABC-001",)),
    _r("ABC-004", "Traditional product overhead", "abc", "rate x product_base_quantity", "Blueprint 7.2", ("GOLDEN-ABC-001",)),
    _r("ABC-005", "Cost distortion", "abc", "traditional_overhead - abc_overhead", "Blueprint 7.2", ("GOLDEN-ABC-001",)),
    _r("ABC-006", "ABC unit cost and margin", "abc", "direct_material + direct_labour + activity_overhead", "Blueprint 7.2", ("GOLDEN-ABC-001",)),
    _r("ABC-007", "Cross-subsidy flag", "abc", "abs(distortion) / abc_unit_cost >= 0.10", "Blueprint 7.2", ("GOLDEN-ABC-001",)),
    # TDABC
    _r("TDABC-001", "Capacity cost rate", "abc", "pool_cost / capacity_events", "Blueprint 7.3", ("GOLDEN-TDABC-001",)),
    _r("TDABC-002", "Assigned activity cost", "abc", "capacity_rate x actual_driver_quantity", "Blueprint 7.3", ("GOLDEN-TDABC-001",)),
    _r("TDABC-003", "Unused capacity cost", "abc", "capacity_rate x (capacity_events - actual_quantity)", "Blueprint 7.3", ("GOLDEN-TDABC-001",)),
    _r("TDABC-004", "Capacity utilisation", "abc", "actual_quantity / capacity_events", "Blueprint 7.3", ("GOLDEN-TDABC-001",)),
    # Customer profitability
    _r("CUS-001", "Customer revenue", "abc", "sum(customer_activity.revenue)", "Blueprint 7.4", ("GOLDEN-CUS-001",)),
    _r("CUS-002", "Cost to serve", "abc", "orders x order_rate + tickets x service_rate + deliveries x delivery_rate", "Blueprint 7.4", ("GOLDEN-CUS-001",)),
    _r("CUS-003", "Customer net margin", "abc", "revenue - abc_product_cost - cost_to_serve", "Blueprint 7.4", ("GOLDEN-CUS-001",)),
    _r("CUS-004", "Loss-making customer verdict", "abc", "net_margin < 0", "Blueprint 7.4", ("GOLDEN-CUS-001",)),
    # Budget chain
    _r("BUD-001", "Sales budget", "budgets", "sales_units x standard_price", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-002", "Production budget", "budgets", "sales_units + ending_fg_units - beginning_fg_units", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-003", "Material usage budget", "budgets", "production_units x std_material_qty", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-004", "Material purchases budget", "budgets", "usage + ending_rm_qty - beginning_rm_qty", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-005", "Direct labour budget", "budgets", "production_units x std_labor_hours x std_labor_rate", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-006", "Manufacturing overhead budget", "budgets", "production_units x (variable_oh + fixed_oh_rate)", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-007", "Cash collections", "budgets", "sales x collection_rate", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-008", "Cash payments", "budgets", "(material + labour + overhead) x payment_rate + capex", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-009", "Closing cash", "budgets", "opening_cash + collections - payments - tax", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-010", "Master budget operating income", "budgets", "sales - material - labour - overhead - selling", "Blueprint 8.2", ("GOLDEN-BUD-001",)),
    _r("BUD-011", "Rolling forecast", "budgets", "actual_to_date + remaining_budget", "Blueprint 8.3", ("GOLDEN-BUD-002",)),
    _r("BUD-012", "Scenario overlay", "budgets", "base_value x (1 + overlay_factor)", "Blueprint 8.4", ("GOLDEN-BUD-002",)),
    _r("BUD-013", "Budget version variance", "budgets", "current_version - prior_version", "Blueprint 8.4", ("GOLDEN-BUD-002",)),
    _r("BUD-014", "Forecast bias flag", "budgets", "abs(forecast - actual) / actual > threshold", "Blueprint 8.5", ("GOLDEN-BUD-002",)),
    # Performance
    _r("PERF-001", "Return on investment", "performance", "operating_income / average_operating_assets", "Blueprint 9.2", ("GOLDEN-PERF-001",)),
    _r("PERF-002", "Residual income", "performance", "operating_income - required_return x average_assets", "Blueprint 9.2", ("GOLDEN-PERF-001",)),
    _r("PERF-003", "Investment conflict verdict", "performance", "new_ri > 0 and new_roi < current_roi", "Blueprint 9.2", ("GOLDEN-PERF-001",)),
    _r("PERF-004", "KPI normalisation", "performance", "(actual - target) / target with direction", "Blueprint 9.3", ("GOLDEN-PERF-002",)),
    _r("PERF-005", "Weighted scorecard", "performance", "sum(weight x normalised_score)", "Blueprint 9.3", ("GOLDEN-PERF-002",)),
    _r("PERF-006", "Controllability split", "performance", "sum(controllable) vs sum(uncontrollable)", "Blueprint 9.4", ("GOLDEN-PERF-002",)),
    # Monitoring
    _r("MON-001", "Alert rule parse", "monitoring", "metric comparator threshold", "Blueprint 10.2", ("GOLDEN-MON-001",)),
    _r("MON-002", "Alert evaluation", "monitoring", "compare(metric_value, threshold)", "Blueprint 10.2", ("GOLDEN-MON-001",)),
    _r("MON-003", "Materiality", "monitoring", "abs(variance) >= materiality_amount", "Blueprint 10.3", ("GOLDEN-MON-001",)),
    _r("MON-004", "Digest ordering", "monitoring", "sort by abs(variance) descending", "Blueprint 10.3", ("GOLDEN-MON-001",)),
    # Group reporting
    _r("GRP-001", "Group revenue", "group", "sum(entity_revenue)", "Blueprint 11.2", ("GOLDEN-GRP-001",)),
    _r("GRP-002", "Intercompany elimination", "group", "sum(intercompany_revenue) and sum(intercompany_cost)", "Blueprint 11.2", ("GOLDEN-GRP-001",)),
    _r("GRP-003", "Inventory profit elimination", "group", "inventory_change_units x fixed_oh_rate", "Blueprint 11.2", ("GOLDEN-GRP-001",)),
    _r("GRP-004", "Consolidated operating income", "group", "sum(entity_income) - intercompany_margin - inventory_profit", "Blueprint 11.2", ("GOLDEN-GRP-001",)),
    _r("GRP-005", "Flagged intercompany pairs", "group", "count(status = flagged_for_review)", "Blueprint 11.3", ("GOLDEN-GRP-001",)),
)

register_many(RULES)
