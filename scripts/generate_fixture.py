"""Generate the deterministic three-product / two-plant fixture.

Run: python scripts/generate_fixture.py
All values are derived from standards and fixed factors; no randomness.
"""

from __future__ import annotations

import csv
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"

CURRENCY = "INR"
PERIODS = [("2026-07", "Jul 2026", "closed"), ("2026-08", "Aug 2026", "closed"), ("2026-09", "Sep 2026", "open")]
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
    ("4010", "Revenue", "income_statement", "revenue", "direct", "INR", "finance"),
    ("5010", "Direct materials", "income_statement", "variable", "direct", "INR", "cost_accounting"),
    ("5020", "Direct labour", "income_statement", "variable", "direct", "INR", "cost_accounting"),
    ("5030", "Variable overhead", "income_statement", "variable", "indirect", "INR", "cost_accounting"),
    ("5040", "Fixed overhead", "income_statement", "fixed", "indirect", "INR", "cost_accounting"),
    ("6010", "Selling and distribution", "income_statement", "variable", "indirect", "INR", "commercial"),
]


def d(value: str | int) -> Decimal:
    return Decimal(str(value))


def q2(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def q4(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def write(name: str, header: list[str], rows: list[list[str]]) -> None:
    path = FIXTURES / name
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    write("entities.csv", ["entity_id", "name", "currency"], [["ENT-01", "Northline Industries", CURRENCY]])
    write("plants.csv", ["plant_id", "name", "entity_id"], [[p, n, "ENT-01"] for p, n in PLANTS])
    write(
        "products.csv",
        ["product_id", "name", "family"],
        [["PRD-A", "Standard motor", "motors"], ["PRD-B", "Custom motor", "motors"], ["PRD-C", "Servo drive", "drives"]],
    )
    write("periods.csv", ["period_id", "label", "status"], [list(r) for r in PERIODS])
    write(
        "accounts.csv",
        ["account_id", "name", "statement", "behavior", "directness", "unit", "owner"],
        [list(row) for row in ACCOUNTS],
    )

    standards: list[list[str]] = []
    production: list[list[str]] = []
    budgets: list[list[str]] = []
    inventory: list[list[str]] = []
    gl_actual: dict[tuple[str, str, str], Decimal] = {}
    gl_budget: dict[tuple[str, str, str], Decimal] = {}
    product_index = {row[0]: index for index, row in enumerate(PRODUCTS)}
    standard_map: dict[tuple[str, str, str], dict] = {}

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

                standard_map[(period_id, plant_id, product_id)] = {
                    "price": d(price), "mat": d(mat), "labour": d(labour), "voh": d(voh),
                    "fixed_unit": d(fixed_unit), "selling": d(selling), "volume": d(volume),
                    "capacity": d(capacity), "std_mat_qty": d(std_mat_qty),
                    "std_mat_price": d(std_mat_price), "std_labour_hours": d(std_labour_hours),
                    "std_labour_rate": d(std_labour_rate), "std_machine": d(std_machine),
                }
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

                revenue_actual = d(output) * d(price)
                revenue_budget = d(volume) * d(price)
                material_actual = actual_qty * actual_price
                material_budget = d(volume) * d(mat)
                labour_actual = actual_hours * actual_rate
                labour_budget = d(volume) * d(labour)
                voh_actual = actual_voh
                voh_budget = d(volume) * d(voh)
                selling_actual = d(output) * d(selling)
                selling_budget = d(volume) * d(selling)

                key = (period_id, plant_id, "4010")
                gl_actual[key] = gl_actual.get(key, d("0")) + revenue_actual
                gl_budget[key] = gl_budget.get(key, d("0")) + revenue_budget
                for account, actual_value, budget_value in (
                    ("5010", material_actual, material_budget),
                    ("5020", labour_actual, labour_budget),
                    ("5030", voh_actual, voh_budget),
                    ("5040", actual_fixed, budget_fixed),
                    ("6010", selling_actual, selling_budget),
                ):
                    key = (period_id, plant_id, account)
                    gl_actual[key] = gl_actual.get(key, d("0")) + actual_value
                    gl_budget[key] = gl_budget.get(key, d("0")) + budget_value

                beginning = d(output) * d("0.06")
                ending = beginning + d((period_index - 1) * 10 + product_index[product_id] * 5)
                inventory.append([
                    period_id, plant_id, product_id, str(int(beginning)), str(int(ending)), q2(d(fixed_unit)),
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

    gl_rows: list[list[str]] = []
    control_rows: list[list[str]] = []
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


if __name__ == "__main__":
    main()
    print(f"fixture written to {FIXTURES}")
