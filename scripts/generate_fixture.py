"""Generate the deterministic three-product / two-plant fixture.

Run: python scripts/generate_fixture.py
All values are derived from standards and fixed factors; no randomness.

Phase 1 files (entities … control totals) drive the M0-M5 copilot.
Phase 2 files (activities … intercompany) add ABC/TDABC, budgeting,
performance measurement, monitoring and group reporting. Connector
extracts live in fixtures/connectors and are generated from the same GL.
"""

from __future__ import annotations

import csv
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"

CURRENCY = "INR"
PERIODS = [
    ("2026-07", "Jul 2026", "closed"),
    ("2026-08", "Aug 2026", "closed"),
    ("2026-09", "Sep 2026", "open"),
]
PLANTS = [("PLT-01", "Pune"), ("PLT-02", "Coimbatore")]
PRODUCTS = [
    # id, name, price, mat, labour, voh, fixed/unit, selling, std_mat_qty, std_mat_price,
    # std_labour_hours, std_labour_rate, std_machine_hours
    ("PRD-A", "Standard motor", "420", "120", "60", "30", "40", "15", "2.0", "60", "1.2", "50", "0.5"),
    ("PRD-B", "Custom motor", "500", "180", "80", "40", "60", "20", "3.0", "60", "1.6", "50", "1.0"),
    ("PRD-C", "Servo drive", "780", "320", "140", "60", "80", "30", "4.0", "80", "2.8", "50", "1.4"),
]
BUDGET_VOLUME = {
    ("PLT-01", "PRD-A"): 4000, ("PLT-01", "PRD-B"): 1800, ("PLT-01", "PRD-C"): 900,
    ("PLT-02", "PRD-A"): 2500, ("PLT-02", "PRD-B"): 3000, ("PLT-02", "PRD-C"): 600,
}
CAPACITY = {
    ("PLT-01", "PRD-A"): 6000, ("PLT-01", "PRD-B"): 4000, ("PLT-01", "PRD-C"): 1500,
    ("PLT-02", "PRD-A"): 3500, ("PLT-02", "PRD-B"): 4500, ("PLT-02", "PRD-C"): 1000,
}
OUTPUT_FACTOR = {
    "2026-07": {"PRD-A": "0.97", "PRD-B": "0.98", "PRD-C": "1.00"},
    "2026-08": {"PRD-A": "1.01", "PRD-B": "1.02", "PRD-C": "0.95"},
    "2026-09": {"PRD-A": "1.03", "PRD-B": "1.00", "PRD-C": "1.05"},
}
ACCOUNTS = [
    ("4010", "Revenue", "income_statement", "revenue", "direct", "INR", "finance",
     "CC-SALES", "uncontrollable"),
    ("5010", "Direct materials", "income_statement", "variable", "direct", "INR", "cost_accounting",
     "CC-PLANT", "controllable"),
    ("5020", "Direct labour", "income_statement", "variable", "direct", "INR", "cost_accounting",
     "CC-PLANT", "controllable"),
    ("5030", "Variable overhead", "income_statement", "variable", "indirect", "INR", "cost_accounting",
     "CC-PLANT", "controllable"),
    ("5040", "Fixed overhead", "income_statement", "fixed", "indirect", "INR", "cost_accounting",
     "CC-PLANT", "uncontrollable"),
    ("6010", "Selling and distribution", "income_statement", "variable", "indirect", "INR", "commercial",
     "CC-SALES", "controllable"),
]

ACTIVITIES = [
    # id, name, driver_name, driver_unit, pool_group, minutes_per_event, source
    ("ACT-SETUP", "Machine setups", "Setups", "setups", "manufacturing", "180", "Setup log"),
    ("ACT-INSPECT", "Quality inspections", "Inspections", "inspections", "manufacturing", "60", "Inspection log"),
    ("ACT-PO", "Purchase ordering", "Purchase orders", "purchase orders", "manufacturing", "20", "PO log"),
    ("ACT-ORDER", "Customer order processing", "Orders", "orders", "manufacturing", "30", "Order log"),
    ("ACT-ENG", "Engineering support", "Engineering hours", "engineering hours", "manufacturing", "120", "Engineering log"),
]
POOL_SHARE = {
    "ACT-SETUP": "0.30", "ACT-INSPECT": "0.15", "ACT-PO": "0.15",
    "ACT-ORDER": "0.15", "ACT-ENG": "0.25",
}
CUSTOMER_WEIGHTS = {
    "PRD-A": [("CUS-01", "0.40"), ("CUS-04", "0.25"), ("CUS-05", "0.15"), ("CUS-06", "0.20")],
    "PRD-B": [("CUS-02", "0.55"), ("CUS-04", "0.25"), ("CUS-05", "0.12"), ("CUS-06", "0.08")],
    "PRD-C": [("CUS-03", "0.60"), ("CUS-05", "0.20"), ("CUS-06", "0.20")],
}
CUSTOMERS = [
    ("CUS-01", "Arcline OEM", "enterprise", "west", "direct"),
    ("CUS-02", "Vector Robotics", "enterprise", "south", "direct"),
    ("CUS-03", "Meridian Pumps", "mid-market", "west", "distributor"),
    ("CUS-04", "Kestrel Automation", "mid-market", "north", "direct"),
    ("CUS-05", "Orbit Drives", "small", "east", "online"),
    ("CUS-06", "Terra Motion", "small", "south", "distributor"),
]
CUSTOMER_COST_RATES = [
    ("RATE-ORDER", "Order processing", "2500", "INR/order"),
    ("RATE-SERVICE", "Service support", "6000", "INR/ticket"),
    ("RATE-DELIVERY", "Delivery", "4000", "INR/delivery"),
]

budget_cash_rows: list[list[str]] = []


def d(value: str | int) -> Decimal:
    return Decimal(str(value))


def q2(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def q4(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def ceil_div(value: int, divisor: int) -> int:
    return max(1, -(-value // divisor))


def driver_counts(product_id: str, output: int) -> dict[str, int]:
    """Low-volume custom products consume disproportionate activity drivers."""
    if product_id == "PRD-A":
        return {
            "ACT-SETUP": ceil_div(output, 700), "ACT-INSPECT": ceil_div(output, 1200),
            "ACT-PO": ceil_div(output, 1500), "ACT-ORDER": ceil_div(output, 1000),
            "ACT-ENG": ceil_div(output, 2500),
        }
    if product_id == "PRD-B":
        return {
            "ACT-SETUP": ceil_div(output, 150), "ACT-INSPECT": ceil_div(output, 300),
            "ACT-PO": ceil_div(output, 300), "ACT-ORDER": ceil_div(output, 200),
            "ACT-ENG": ceil_div(output, 300),
        }
    return {
        "ACT-SETUP": ceil_div(output, 400), "ACT-INSPECT": ceil_div(output, 700),
        "ACT-PO": ceil_div(output, 800), "ACT-ORDER": ceil_div(output, 500),
        "ACT-ENG": ceil_div(output, 900),
    }


def customer_driver_count(customer_id: str, activity: str, units: int) -> int:
    if activity == "orders":
        divisor = {"CUS-01": 800, "CUS-02": 300, "CUS-03": 400, "CUS-04": 250,
                   "CUS-05": 100, "CUS-06": 500}[customer_id]
        return ceil_div(units, divisor)
    if activity == "service_tickets":
        base = {"CUS-01": 1, "CUS-02": 3, "CUS-03": 2, "CUS-04": 2, "CUS-05": 10, "CUS-06": 2}
        return base[customer_id] + (1 if units > 400 else 0)
    divisor = {"CUS-01": 900, "CUS-02": 350, "CUS-03": 500, "CUS-04": 300,
               "CUS-05": 120, "CUS-06": 600}[customer_id]
    return ceil_div(units, divisor)


def write(name: str, header: list[str], rows: list[list[str]]) -> None:
    path = FIXTURES / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    write(
        "entities.csv",
        ["entity_id", "name", "currency"],
        [["ENT-01", "Northline Industries", CURRENCY], ["ENT-02", "Northline Components", CURRENCY]],
    )
    write("plants.csv", ["plant_id", "name", "entity_id"], [[p, n, "ENT-01"] for p, n in PLANTS])
    write(
        "products.csv",
        ["product_id", "name", "family"],
        [["PRD-A", "Standard motor", "motors"], ["PRD-B", "Custom motor", "motors"],
         ["PRD-C", "Servo drive", "drives"]],
    )
    write("periods.csv", ["period_id", "label", "status"], [list(r) for r in PERIODS])
    write(
        "accounts.csv",
        ["account_id", "name", "statement", "behavior", "directness", "unit", "owner",
         "cost_center", "controllability"],
        [list(row) for row in ACCOUNTS],
    )
    write(
        "activities.csv",
        ["activity_id", "name", "driver_name", "driver_unit", "pool_group", "minutes_per_event", "source"],
        [list(row) for row in ACTIVITIES],
    )
    write(
        "customers.csv",
        ["customer_id", "name", "segment", "region", "channel"],
        [list(row) for row in CUSTOMERS],
    )
    write(
        "customer_cost_rates.csv",
        ["rate_id", "name", "rate", "unit"],
        [list(row) for row in CUSTOMER_COST_RATES],
    )
    write(
        "connector_mappings.csv",
        ["source_system", "source_account", "source_name", "target_account", "status",
         "approved_by", "contract_version"],
        [
            ["netsuite", "40010", "Product revenue", "4010", "approved", "controller.demo", "1"],
            ["netsuite", "50010", "Direct materials", "5010", "approved", "controller.demo", "1"],
            ["netsuite", "50020", "Direct labour", "5020", "approved", "controller.demo", "1"],
            ["netsuite", "50030", "Variable overhead", "5030", "approved", "controller.demo", "1"],
            ["netsuite", "50040", "Fixed overhead", "5040", "approved", "controller.demo", "1"],
            ["netsuite", "60010", "Selling and distribution", "6010", "approved", "controller.demo", "1"],
            ["sap", "400000", "Erloese", "4010", "approved", "controller.demo", "1"],
            ["sap", "500000", "Materialkosten", "5010", "approved", "controller.demo", "1"],
            ["sap", "500100", "Fertigungslohn", "5020", "approved", "controller.demo", "1"],
            ["sap", "500200", "Variable Gemeinkosten", "5030", "approved", "controller.demo", "1"],
            ["sap", "500300", "Fixe Gemeinkosten", "5040", "approved", "controller.demo", "1"],
            ["sap", "600000", "Vertriebskosten", "6010", "approved", "controller.demo", "1"],
            ["flatfile", "4010", "Revenue", "4010", "approved", "controller.demo", "1"],
            ["flatfile", "5010", "Direct materials", "5010", "approved", "controller.demo", "1"],
            ["flatfile", "5020", "Direct labour", "5020", "approved", "controller.demo", "1"],
            ["flatfile", "5030", "Variable overhead", "5030", "approved", "controller.demo", "1"],
            ["flatfile", "5040", "Fixed overhead", "5040", "approved", "controller.demo", "1"],
            ["flatfile", "6010", "Selling and distribution", "6010", "approved", "controller.demo", "1"],
        ],
    )

    standards: list[list[str]] = []
    production: list[list[str]] = []
    budgets: list[list[str]] = []
    inventory: list[list[str]] = []
    driver_events: list[list[str]] = []
    customer_rows: list[list[str]] = []
    budget_assumptions: list[list[str]] = []
    assets: list[list[str]] = []
    gl_actual: dict[tuple[str, str, str], Decimal] = {}
    gl_budget: dict[tuple[str, str, str], Decimal] = {}
    plant_overhead: dict[tuple[str, str], Decimal] = {}
    product_index = {row[0]: index for index, row in enumerate(PRODUCTS)}
    budget_cash_rows.clear()

    for period_index, (period_id, _, _) in enumerate(PERIODS):
        for plant_id, _ in PLANTS:
            for row in PRODUCTS:
                (
                    product_id, name, price, mat, labour, voh, fixed_unit, selling,
                    std_mat_qty, std_mat_price, std_labour_hours, std_labour_rate, std_machine,
                ) = row
                volume = BUDGET_VOLUME[(plant_id, product_id)]
                capacity = CAPACITY[(plant_id, product_id)]
                factor = d(OUTPUT_FACTOR[period_id][product_id])
                output = int((d(volume) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

                plant_offset = d("0.000") if plant_id == "PLT-01" else d("0.002")
                prod_offset = {"PRD-A": d("0"), "PRD-B": d("0.003"), "PRD-C": d("-0.004")}[product_id]
                price_factor = d("1") + d(period_index) * d("0.005") + plant_offset + prod_offset
                usage_factor = (
                    d("1") + d(period_index) * d("0.004") + plant_offset
                    + {"PRD-A": d("0.002"), "PRD-B": d("-0.001"), "PRD-C": d("0.003")}[product_id]
                )
                hours_factor = (
                    d("1") + d(period_index) * d("0.003") + plant_offset
                    + {"PRD-A": d("-0.002"), "PRD-B": d("0.002"), "PRD-C": d("0.001")}[product_id]
                )
                rate_factor = (
                    d("1") + d(period_index) * d("0.004") + d("0.001")
                    + {"PRD-A": d("0.002"), "PRD-B": d("0.003"), "PRD-C": d("0.001")}[product_id]
                )
                voh_factor = d("1") + d(period_index) * d("0.003") + plant_offset
                fixed_factor = d("1") + d(period_index) * d("0.002") + plant_offset

                actual_price = d(std_mat_price) * price_factor
                actual_qty = d(output) * d(std_mat_qty) * usage_factor
                actual_hours = d(output) * d(std_labour_hours) * hours_factor
                actual_rate = d(std_labour_rate) * rate_factor
                voh_rate_per_hour = d(voh) / d(std_labour_hours)
                actual_voh = voh_rate_per_hour * actual_hours * voh_factor
                budget_fixed = d(fixed_unit) * d(volume)
                actual_fixed = budget_fixed * fixed_factor

                standards.append([
                    period_id, plant_id, product_id, price, mat, labour, voh, fixed_unit, selling,
                    std_mat_qty, std_mat_price, std_labour_hours, std_labour_rate, std_machine,
                    str(volume), str(capacity), q2(d(volume) * d(std_labour_hours)),
                ])
                production.append([
                    period_id, plant_id, product_id, str(output), q4(actual_qty), q2(actual_price),
                    q4(actual_hours), q2(actual_rate), q2(actual_voh), q2(actual_fixed),
                    q4(d(output) * d(std_machine)),
                ])
                budgets.append([
                    period_id, plant_id, product_id, str(volume), q2(d(volume) * d(price)),
                    q2(d(volume) * d(mat)), q2(d(volume) * d(labour)), q2(d(volume) * d(voh)),
                    q2(budget_fixed), q2(d(volume) * d(selling)),
                ])

                counts = driver_counts(product_id, output)
                for activity_id, count in counts.items():
                    driver_events.append([period_id, plant_id, product_id, activity_id, str(count)])

                revenue_actual = d(output) * d(price)
                revenue_budget = d(volume) * d(price)
                material_actual = actual_qty * actual_price
                material_budget = d(volume) * d(mat)
                labour_actual = actual_hours * actual_rate
                labour_budget = d(volume) * d(labour)
                selling_actual = d(output) * d(selling)
                selling_budget = d(volume) * d(selling)

                key = (period_id, plant_id, "4010")
                gl_actual[key] = gl_actual.get(key, d("0")) + revenue_actual
                gl_budget[key] = gl_budget.get(key, d("0")) + revenue_budget
                for account, actual_value, budget_value in (
                    ("5010", material_actual, material_budget),
                    ("5020", labour_actual, labour_budget),
                    ("5030", actual_voh, d(volume) * d(voh)),
                    ("5040", actual_fixed, budget_fixed),
                    ("6010", selling_actual, selling_budget),
                ):
                    key = (period_id, plant_id, account)
                    gl_actual[key] = gl_actual.get(key, d("0")) + actual_value
                    gl_budget[key] = gl_budget.get(key, d("0")) + budget_value

                overhead_key = (period_id, plant_id)
                plant_overhead[overhead_key] = (
                    plant_overhead.get(overhead_key, d("0")) + actual_voh + actual_fixed
                )

                beginning = d(output) * d("0.06")
                ending = beginning + d((period_index - 1) * 10 + product_index[product_id] * 5)
                inventory.append([
                    period_id, plant_id, product_id, str(int(beginning)), str(int(ending)),
                    q2(d(fixed_unit)),
                ])

                desired_fg = int(ending) + {"PRD-A": 100, "PRD-B": 50, "PRD-C": 20}[product_id]
                usage = d(volume) * d(std_mat_qty)
                budget_assumptions.append([
                    period_id, plant_id, product_id, str(volume), str(int(beginning)), str(desired_fg),
                    q4(usage * d("0.02")), q4(usage * d("0.03")),
                ])

                for customer_id, weight in CUSTOMER_WEIGHTS[product_id]:
                    units = int((d(output) * d(weight)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    if units <= 0:
                        continue
                    customer_rows.append([
                        period_id, plant_id, customer_id, product_id, str(units),
                        q2(d(units) * d(price)),
                        str(customer_driver_count(customer_id, "orders", units)),
                        str(customer_driver_count(customer_id, "service_tickets", units)),
                        str(customer_driver_count(customer_id, "deliveries", units)),
                    ])

        for plant_id, plant_name in PLANTS:
            assets.append([
                period_id, f"DIV-{plant_id[-2:]}", f"{plant_name} division", plant_id,
                "2400000" if plant_id == "PLT-01" else "1800000",
                "2600000" if plant_id == "PLT-01" else "2000000",
                "0.12",
                "180000" if plant_id == "PLT-01" else "110000",
                "1200000" if plant_id == "PLT-01" else "900000",
            ])
            budget_cash_rows.append([
                period_id, plant_id, "1800000" if plant_id == "PLT-01" else "1200000",
                "350000", "0.85", "0.80", "0.25",
            ])

    write(
        "standard_costs.csv",
        [
            "period_id", "plant_id", "product_id", "price", "variable_material", "variable_labor",
            "variable_oh", "fixed_oh_rate", "variable_selling", "std_material_qty",
            "std_material_price", "std_labor_hours", "std_labor_rate", "std_machine_hours",
            "budget_volume", "capacity_units", "denominator_hours",
        ],
        standards,
    )
    write(
        "production_actuals.csv",
        [
            "period_id", "plant_id", "product_id", "output_units", "material_qty_used",
            "material_price", "labor_hours", "labor_rate", "variable_oh_actual",
            "fixed_oh_actual", "actual_machine_hours",
        ],
        production,
    )
    write(
        "budgets.csv",
        [
            "period_id", "plant_id", "product_id", "volume", "revenue", "material", "labor",
            "variable_oh", "fixed_oh", "variable_selling",
        ],
        budgets,
    )
    write(
        "inventory.csv",
        [
            "period_id", "plant_id", "product_id", "beginning_units", "ending_units",
            "fixed_oh_rate",
        ],
        inventory,
    )
    write(
        "driver_events.csv",
        ["period_id", "plant_id", "product_id", "activity_id", "driver_quantity"],
        driver_events,
    )
    write(
        "customer_activity.csv",
        ["period_id", "plant_id", "customer_id", "product_id", "units", "revenue",
         "orders", "service_tickets", "deliveries"],
        customer_rows,
    )
    write(
        "budget_assumptions.csv",
        ["period_id", "plant_id", "product_id", "sales_units", "beginning_fg_units",
         "ending_fg_units", "beginning_rm_qty", "ending_rm_qty"],
        budget_assumptions,
    )
    write(
        "budget_cash.csv",
        ["period_id", "plant_id", "opening_cash", "capex", "collection_rate",
         "payment_rate", "tax_rate"],
        budget_cash_rows,
    )
    write(
        "assets.csv",
        ["period_id", "division_id", "name", "plant_id", "beginning_assets",
         "ending_assets", "required_return", "new_investment_income", "new_investment_assets"],
        assets,
    )

    # Cost pools are a transparent re-slicing of actual plant overhead into activities.
    pool_rows: list[list[str]] = []
    events_by_key: dict[tuple[str, str, str], int] = {}
    for period_id, plant_id, _product_id, activity_id, count in driver_events:
        key = (period_id, plant_id, activity_id)
        events_by_key[key] = events_by_key.get(key, 0) + int(count)
    for (period_id, plant_id), overhead in sorted(plant_overhead.items()):
        allocated = d("0")
        for index, (activity_id, _name, _driver, _unit, _group, _minutes, _source) in enumerate(ACTIVITIES):
            if index == len(ACTIVITIES) - 1:
                pool_cost = (overhead - allocated).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                pool_cost = (overhead * d(POOL_SHARE[activity_id])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                allocated += pool_cost
            quantity = events_by_key[(period_id, plant_id, activity_id)]
            capacity = (d(quantity) / d("0.80")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            pool_rows.append([
                period_id, plant_id, f"POOL-{activity_id[4:]}", activity_id, q2(pool_cost),
                str(quantity), str(capacity),
            ])
    write(
        "cost_pools.csv",
        ["period_id", "plant_id", "pool_id", "activity_id", "pool_cost",
         "driver_quantity", "capacity_events"],
        pool_rows,
    )

    # Scorecard. Financial metrics are recalculated from the snapshot at run time.
    scorecard_rows = [
        ["FIN-01", "financial", "Contribution margin ratio", "plant_controller", "25", "0.32",
         "", "higher", "snapshot:contribution_margin", "controllable"],
        ["FIN-02", "financial", "Material variance to material budget", "plant_controller", "15",
         "0.01", "", "lower", "snapshot:material_variance_ratio", "controllable"],
        ["CUS-01", "customer", "On-time delivery", "plant_manager", "20", "0.95", "0.93",
         "higher", "manual", "controllable"],
        ["CUS-02", "customer", "Customer satisfaction (1-5)", "commercial_lead", "15", "4.50",
         "4.30", "higher", "manual", "controllable"],
        ["OPS-01", "internal", "First-pass yield", "quality_lead", "15", "0.97", "0.955",
         "higher", "manual", "controllable"],
        ["OPS-02", "internal", "Safety incidents", "plant_manager", "10", "0", "1",
         "lower", "manual", "controllable"],
    ]
    write(
        "scorecard.csv",
        ["kpi_id", "perspective", "name", "owner", "weight", "target", "actual",
         "direction", "source", "controllability"],
        scorecard_rows,
    )

    # Group-level reporting: ENT-01 is computed from the snapshot; ENT-02 is a
    # distribution entity supplied by a separate extract.
    entity_rows: list[list[str]] = []
    for index, (period_id, _, _) in enumerate(PERIODS):
        entity_rows.append([
            period_id, "ENT-02", "Northline Components",
            str(1_700_000 + index * 20_000),
            str(850000 + index * 10000), str(910000 + index * 12000), "200", "300",
        ])
    write(
        "entity_financials.csv",
        ["period_id", "entity_id", "name", "revenue", "variable_income", "absorption_income",
         "inventory_change_units", "fixed_oh_rate"],
        entity_rows,
    )
    intercompany_rows = []
    for period_id, _, _ in PERIODS:
        intercompany_rows.append([
            period_id, "ENT-01", "ENT-02", "Finished motors sold to distribution",
            "420000", "300000", "flagged_for_review",
        ])
        intercompany_rows.append([
            period_id, "ENT-02", "ENT-01", "Components sold to manufacturing",
            "600000", "560000", "flagged_for_review",
        ])
    write(
        "intercompany.csv",
        ["period_id", "seller_entity", "buyer_entity", "description", "revenue", "cost", "status"],
        intercompany_rows,
    )

    gl_rows: list[list[str]] = []
    control_rows: list[list[str]] = []
    source_codes = {"4010": "40010", "5010": "50010", "5020": "50020",
                    "5030": "50030", "5040": "50040", "6010": "60010"}
    for period_index, (period_id, _, _) in enumerate(PERIODS):
        for account_id, *_ in ACCOUNTS:
            total_actual = d("0")
            for plant_id, _ in PLANTS:
                key = (period_id, plant_id, account_id)
                actual = gl_actual.get(key, d("0"))
                budget = gl_budget.get(key, d("0"))
                prior = budget * (d("0.98") if period_index == 2 else d("0.99"))
                gl_rows.append([period_id, plant_id, account_id, q2(actual), q2(budget), q2(prior)])
                total_actual += actual
            control_rows.append([period_id, account_id, q2(total_actual)])
    write(
        "gl_balances.csv",
        ["period_id", "plant_id", "account_id", "actual", "budget", "prior"],
        gl_rows,
    )
    write("control_totals.csv", ["period_id", "account_id", "expected_amount"], control_rows)

    # Read-only NetSuite-style extract used by the connector demo.
    connectors = FIXTURES / "connectors"
    connectors.mkdir(exist_ok=True)
    extract_rows = []
    connector_control_rows = []
    account_names = {row[0]: row[1] for row in ACCOUNTS}
    for period_id, plant_id, account_id, actual, _budget, _prior in gl_rows:
        if period_id != "2026-09":
            continue
        extract_rows.append([
            period_id, plant_id, source_codes[account_id], account_names[account_id],
            actual, CURRENCY,
        ])
    for account_id, *_ in ACCOUNTS:
        total = sum(
            d(row[4]) for row in extract_rows if row[2] == source_codes[account_id]
        )
        connector_control_rows.append(["2026-09", source_codes[account_id], q2(total)])
    with (connectors / "netsuite_gl_extract.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["period_id", "plant_id", "source_account", "source_name", "amount", "currency"])
        writer.writerows(extract_rows)
    with (connectors / "netsuite_control_totals.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["period_id", "source_account", "expected_amount"])
        writer.writerows(connector_control_rows)

    sap_codes = {
        "4010": "400000", "5010": "500000", "5020": "500100",
        "5030": "500200", "5040": "500300", "6010": "600000",
    }
    sap_rows = []
    for row in extract_rows:
        period_id, plant_id, source_account, source_name, amount, currency = row
        if period_id != "2026-09":
            continue
        target = next(key for key, value in source_codes.items() if value == source_account)
        sap_rows.append([
            "1000", plant_id, sap_codes[target], source_name, amount, currency, "2026", "09",
        ])
    with (connectors / "sap_gl_extract.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["BUKRS", "WERKS", "HKONT", "TXT50", "DMBTR", "WAERS", "GJAHR", "MONAT"])
        writer.writerows(sap_rows)
    sap_control = []
    for row in extract_rows:
        period_id, _, source_account, _, amount, _ = row
        if period_id != "2026-09":
            continue
        target = next(key for key, value in source_codes.items() if value == source_account)
        sap_control.append(["2026-09", sap_codes[target], amount])
    with (connectors / "sap_control_totals.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["period_id", "source_account", "expected_amount"])
        writer.writerows(sap_control)

    flat_rows = [
        [row[0], row[1], next(key for key, value in source_codes.items() if value == row[2]), row[4], row[5]]
        for row in extract_rows
    ]
    with (connectors / "flat_gl_extract.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["period_id", "plant_id", "account_id", "amount", "currency"])
        writer.writerows(flat_rows)


if __name__ == "__main__":
    main()
    print(f"fixture written to {FIXTURES}")
