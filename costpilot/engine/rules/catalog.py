"""The M0-M3 rulebook. Metadata lives here so every step can cite a version."""

from __future__ import annotations

from costpilot.engine.rules.registry import Rule, register_many

V = "1.0.0"

RULES: tuple[Rule, ...] = (
    # Special order
    Rule(
        "DA-SO-001", V, "Incremental revenue", "decisions.special_order",
        "order_units x order_price", "Blueprint 6.7", ("GOLDEN-DA-SO-001",),
    ),
    Rule(
        "DA-SO-002", V, "Incremental variable cost", "decisions.special_order",
        "order_units x (variable_manufacturing + variable_selling_on_order)",
        "Blueprint 6.7", ("GOLDEN-DA-SO-001",),
    ),
    Rule(
        "DA-SO-003", V, "Incremental contribution", "decisions.special_order",
        "s1 + s2", "Blueprint 6.7", ("GOLDEN-DA-SO-001",),
    ),
    Rule(
        "DA-SO-004", V, "Incremental fixed cost", "decisions.special_order",
        "incremental_fixed_cost", "Blueprint 6.7", ("GOLDEN-DA-SO-001",),
    ),
    Rule(
        "DA-SO-005", V, "Opportunity cost of displaced sales", "decisions.special_order",
        "displaced_units x regular_contribution_per_unit", "Blueprint 6.7",
        ("GOLDEN-DA-SO-002",),
    ),
    Rule(
        "DA-SO-006", V, "Incremental profit", "decisions.special_order",
        "s3 - s4 - s5", "Blueprint 6.7", ("GOLDEN-DA-SO-001", "GOLDEN-DA-SO-002"),
    ),
    Rule(
        "DA-SO-007", V, "Minimum acceptable price", "decisions.special_order",
        "(variable cost + incremental fixed + opportunity cost) / order_units",
        "Blueprint 6.7", ("GOLDEN-DA-SO-001", "PROP-DA-SO-FLOOR"),
    ),
    Rule(
        "DA-SO-008", V, "Idle-capacity cross-check", "decisions.special_order",
        "order_units - idle_capacity_units", "Blueprint 6.9",
    ),
    # Make or buy
    Rule(
        "DA-MB-001", V, "Relevant cost to make", "decisions.make_or_buy",
        "units x variable_cost + avoidable_fixed + opportunity_cost", "Blueprint 6.5",
    ),
    Rule(
        "DA-MB-002", V, "Relevant cost to buy", "decisions.make_or_buy",
        "units x purchase_price + incremental_fixed", "Blueprint 6.5",
    ),
    Rule(
        "DA-MB-003", V, "Advantage of making", "decisions.make_or_buy",
        "s2 - s1", "Blueprint 6.5",
    ),
    Rule(
        "DA-MB-004", V, "Make-or-buy verdict threshold", "decisions.make_or_buy",
        "compare s3 with materiality", "Blueprint 6.5",
    ),
    # Add or drop
    Rule(
        "DA-AD-001", V, "Segment contribution", "decisions.add_or_drop",
        "revenue - variable_costs", "Blueprint 6.5",
    ),
    Rule(
        "DA-AD-002", V, "Avoidable fixed costs", "decisions.add_or_drop",
        "avoidable_fixed", "Blueprint 6.5",
    ),
    Rule(
        "DA-AD-003", V, "Lost contribution from related sales", "decisions.add_or_drop",
        "lost_units x related_contribution_per_unit", "Blueprint 6.5",
    ),
    Rule(
        "DA-AD-004", V, "Segment margin", "decisions.add_or_drop",
        "s1 - s3 - s2", "Blueprint 6.5",
    ),
    Rule(
        "DA-AD-005", V, "Drop verdict", "decisions.add_or_drop",
        "s4 < 0 => drop", "Blueprint 6.5",
    ),
    # Sell or process further
    Rule(
        "DA-SP-001", V, "Incremental revenue from further processing", "decisions.sell_or_process",
        "units x (processed_price - split_off_price)", "Blueprint 6.5",
    ),
    Rule(
        "DA-SP-002", V, "Incremental processing cost", "decisions.sell_or_process",
        "units x processing_cost_per_unit + incremental_fixed", "Blueprint 6.5",
    ),
    Rule(
        "DA-SP-003", V, "Incremental advantage of processing", "decisions.sell_or_process",
        "s1 - s2", "Blueprint 6.5",
    ),
    Rule(
        "DA-SP-004", V, "Joint-cost memo", "decisions.sell_or_process",
        "joint_cost (excluded from decision)", "Blueprint 6.5",
    ),
    # Shutdown
    Rule(
        "DA-SD-001", V, "Contribution lost on shutdown", "decisions.shutdown",
        "units x contribution_per_unit", "Blueprint 6.5",
    ),
    Rule(
        "DA-SD-002", V, "Avoidable fixed costs saved", "decisions.shutdown",
        "avoidable_fixed - restart_cost", "Blueprint 6.5",
    ),
    Rule(
        "DA-SD-003", V, "Shutdown advantage", "decisions.shutdown",
        "s2 - s1", "Blueprint 6.5",
    ),
    # Product mix
    Rule(
        "DA-PM-001", V, "Contribution per unit", "decisions.product_mix",
        "price - variable_cost", "Blueprint 6.5",
    ),
    Rule(
        "DA-PM-002", V, "Contribution per scarce resource unit", "decisions.product_mix",
        "s1 / resource_units_per_unit", "Blueprint 6.5",
    ),
    Rule(
        "DA-PM-003", V, "Ranking by contribution per constraint", "decisions.product_mix",
        "sort descending by s2", "Blueprint 6.5",
    ),
    Rule(
        "DA-PM-004", V, "Allocated mix and contribution", "decisions.product_mix",
        "min(demand, remaining_capacity / resource_units_per_unit)", "Blueprint 6.5",
    ),
    # CVP
    Rule(
        "DA-CVP-001", V, "Contribution margin per unit", "decisions.cvp",
        "price - variable_cost", "Blueprint 6.5",
    ),
    Rule(
        "DA-CVP-002", V, "Break-even units", "decisions.cvp",
        "fixed_costs / contribution_per_unit", "Blueprint 6.5",
    ),
    Rule(
        "DA-CVP-003", V, "Break-even revenue", "decisions.cvp",
        "break_even_units x price", "Blueprint 6.5",
    ),
    Rule(
        "DA-CVP-004", V, "Margin of safety", "decisions.cvp",
        "actual_units - break_even_units", "Blueprint 6.5",
    ),
    Rule(
        "DA-CVP-005", V, "Degree of operating leverage", "decisions.cvp",
        "contribution / operating_income", "Blueprint 6.5",
    ),
    # Material
    Rule(
        "VAR-MAT-001", V, "Material price variance", "variance.material",
        "AQ_used x (SP - AP)", "Blueprint 6.4", ("GOLDEN-VAR-MAT-001",),
    ),
    Rule(
        "VAR-MAT-002", V, "Material usage variance", "variance.material",
        "SP x (SQ - AQ_used)", "Blueprint 6.4", ("GOLDEN-VAR-MAT-001",),
    ),
    Rule(
        "VAR-MAT-003", V, "Material cost variance", "variance.material",
        "s1 + s2", "Blueprint 6.4", ("GOLDEN-VAR-MAT-001",),
    ),
    Rule(
        "VAR-MAT-004", V, "Purchase-basis price variance", "variance.material",
        "purchased_qty x (SP - AP)", "Blueprint 6.4",
    ),
    # Labour
    Rule(
        "VAR-LAB-001", V, "Labour rate variance", "variance.labor",
        "AH x (SR - AR)", "Blueprint 6.4", ("GOLDEN-VAR-LAB-001",),
    ),
    Rule(
        "VAR-LAB-002", V, "Labour efficiency variance", "variance.labor",
        "SR x (SH - AH)", "Blueprint 6.4", ("GOLDEN-VAR-LAB-001",),
    ),
    Rule(
        "VAR-LAB-003", V, "Labour cost variance", "variance.labor",
        "s1 + s2", "Blueprint 6.4", ("GOLDEN-VAR-LAB-001",),
    ),
    # Overhead
    Rule(
        "VAR-OH-001", V, "Variable overhead spending variance", "variance.overhead",
        "AH x SR_voh - actual_variable_oh", "Blueprint 6.4", ("GOLDEN-VAR-OH-001",),
    ),
    Rule(
        "VAR-OH-002", V, "Variable overhead efficiency variance", "variance.overhead",
        "(SH - AH) x SR_voh", "Blueprint 6.4", ("GOLDEN-VAR-OH-001",),
    ),
    Rule(
        "VAR-OH-003", V, "Variable overhead total variance", "variance.overhead",
        "s1 + s2", "Blueprint 6.4", ("GOLDEN-VAR-OH-001",),
    ),
    Rule(
        "VAR-OH-004", V, "Fixed overhead spending variance", "variance.overhead",
        "budgeted_fixed - actual_fixed", "Blueprint 6.4", ("GOLDEN-VAR-OH-002",),
    ),
    Rule(
        "VAR-OH-005", V, "Fixed overhead volume variance", "variance.overhead",
        "(SH - denominator_hours) x fixed_rate", "Blueprint 6.4", ("GOLDEN-VAR-OH-002",),
    ),
    Rule(
        "VAR-OH-006", V, "Fixed overhead total variance", "variance.overhead",
        "s4 + s5", "Blueprint 6.4", ("GOLDEN-VAR-OH-002",),
    ),
    # Flexible budget and flux
    Rule(
        "FLX-001", V, "Flexed revenue", "variance.flexible_budget",
        "static_revenue / budget_volume x actual_volume", "Blueprint 6.4",
    ),
    Rule(
        "FLX-002", V, "Flexed variable cost", "variance.flexible_budget",
        "variable_rate_per_unit x actual_volume", "Blueprint 6.4",
    ),
    Rule(
        "FLX-003", V, "Flexible budget variance", "variance.flexible_budget",
        "flexed - actual for cost; actual - flexed for revenue", "Blueprint 6.4",
    ),
    Rule(
        "FLX-004", V, "Volume variance", "variance.flexible_budget",
        "flexed - static for revenue; static - flexed for cost", "Blueprint 6.4",
    ),
    Rule(
        "FLX-ACCT-001", V, "Account flux", "variance.flux",
        "actual - comparison", "Blueprint 5.8",
    ),
    Rule(
        "FLX-ACCT-002", V, "Unexplained residual", "variance.flux",
        "budget_variance - explained_components", "Blueprint 5.8",
    ),
    # Bridge
    Rule(
        "BRG-000", V, "Variable-costing operating income", "costing.bridge",
        "variable_operating_income", "Blueprint 6.6",
    ),
    Rule(
        "BRG-001", V, "Fixed overhead in inventory change", "costing.bridge",
        "inventory_change_units x fixed_overhead_rate", "Blueprint 6.6",
    ),
    Rule(
        "BRG-002", V, "Absorption operating income", "costing.bridge",
        "variable_operating_income + s1", "Blueprint 6.6",
    ),
)

register_many(RULES)
