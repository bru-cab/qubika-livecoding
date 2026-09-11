"""Regenerate the seed exercises (metadata + synthetic CSVs).

Deterministic (fixed seed). Safe to re-run: overwrites exercise files in
place. Edit the EXERCISES dict / name lists below and run:

    python3 gen_exercises.py

Authoring rules:
- Exercise titles and statements must NOT hint at the solution technique
  (no "JOIN + HAVING" in anything the candidate sees). Folders and titles
  are just "exercise_NN" / "Exercise N"; the technique lives only in the
  interviewer-facing "focus" field (shown by --list) and in solution.sql.
- Statements must be explicit about the expected output shape.
- Metric names must be consistent between statement, schema and solution.
- "check" says how the interviewer's result column compares a candidate's
  output with solution.sql: set ordered/order_by whenever the statement asks
  for a specific row order (see exercises.DEFAULT_CHECK).
"""
import csv
import io
import json
import os
import random
from collections import Counter
from datetime import date, timedelta

EX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exercises")

rng = random.Random(42)

# ---------------------------------------------------------------- dataset ---
FIRST = ["Alice", "Michael", "Sophie", "Martin", "Valerie", "Jack", "Emily",
         "Brandon", "Ashley", "Philip", "Julia", "Thomas", "Rachel", "Ian",
         "Paula", "James", "Caroline", "Andrew", "Michelle", "Frank"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis",
        "Wilson", "Moore", "Taylor", "Anderson", "Baker", "Jackson", "White",
        "Harris", "Cooper", "Thompson", "Lewis", "Clark", "Walker"]
COUNTRIES = ["Uruguay", "Argentina", "Chile", "Brazil", "Mexico", "USA"]

customers = []
for i in range(1, 21):
    country = "" if i == 7 else COUNTRIES[(i - 1) % len(COUNTRIES)]  # 7 -> NULL
    signup = date(2025, 1, 1) + timedelta(days=rng.randint(0, 540))
    customers.append([i, f"{FIRST[i-1]} {LAST[i-1]}", country, signup.isoformat()])

# orders per customer: a few heavy customers so "more than 3 orders" has clear
# answers; customer 20 has none (edge case for LEFT JOIN discussions).
orders_per_customer = {1: 4, 3: 6, 5: 5, 12: 4, 20: 0}
orders = []
oid = 0
for c in customers:
    cid = c[0]
    n = orders_per_customer.get(cid, rng.randint(1, 3))
    for _ in range(n):
        oid += 1
        odate = date(2026, 1, 5) + timedelta(days=rng.randint(0, 200))
        status = rng.choices(["completed", "pending", "cancelled"],
                             weights=[65, 20, 15])[0]
        amount = round(rng.uniform(15, 1400), 2)
        orders.append([oid, cid, odate.isoformat(), status, amount])
rng.shuffle(orders)  # so the CSV isn't sorted by customer
orders = [[i + 1, *row[1:]] for i, row in enumerate(orders)]  # renumber ids 1..N

CUSTOMERS_HEADER = ["id", "name", "country", "signup_date"]
ORDERS_HEADER = ["id", "customer_id", "order_date", "status", "order_usd_amount"]

CUSTOMERS_DDL = """CREATE TABLE customers (
    id INTEGER,
    name VARCHAR,
    country VARCHAR,
    signup_date DATE
);"""
ORDERS_DDL = """CREATE TABLE orders (
    id INTEGER,
    customer_id INTEGER,
    order_date DATE,
    status VARCHAR,
    order_usd_amount DECIMAL(10,2)
);"""

# -------------------------------------------------------------- exercises ---
EXERCISES = {
    "exercise_01": {
        "title": "Exercise 1",
        "difficulty": "junior",
        "focus": "warmup: COUNT + WHERE",
        "tables": ["orders"],
        "check": {"ordered": False},
        "statement": """# Exercise 1

You have the `orders` table with the orders of an online store.

**Task:** Write the query that returns the number of orders in `completed`
status.

Return a single number.
""",
        "solution": "SELECT COUNT(*) AS completed_orders\nFROM orders\nWHERE status = 'completed';\n",
    },
    "exercise_02": {
        "title": "Exercise 2",
        "difficulty": "junior",
        "focus": "JOIN + numeric filter",
        "tables": ["customers", "orders"],
        "check": {"ordered": True, "order_by": ["order_usd_amount"]},
        "statement": """# Exercise 2

You have the `customers` and `orders` tables.

**Task:** List the customer name (`name`), the order date (`order_date`) and
the order amount (`order_usd_amount`) of every order whose amount is
**greater than 500 USD**.

Sort the result by amount, highest first.
""",
        "solution": ("SELECT c.name, o.order_date, o.order_usd_amount\n"
                     "FROM orders o\nJOIN customers c ON c.id = o.customer_id\n"
                     "WHERE o.order_usd_amount > 500\n"
                     "ORDER BY o.order_usd_amount DESC;\n"),
    },
    "exercise_03": {
        "title": "Exercise 3",
        "difficulty": "junior",
        "focus": "JOIN + GROUP BY per country",
        "tables": ["customers", "orders"],
        "check": {"ordered": False},
        "statement": """# Exercise 3

Same tables: `customers` and `orders`.

**Task:** For customers from `Brazil` and `Argentina`, calculate the total
amount (sum of `order_usd_amount`) of their `completed` orders, **per
country**.

Return one row per country, with the country and its total completed amount.
""",
        "solution": ("SELECT c.country, SUM(o.order_usd_amount) AS total_completed_usd\n"
                     "FROM orders o\nJOIN customers c ON c.id = o.customer_id\n"
                     "WHERE c.country IN ('Brazil', 'Argentina')\n"
                     "  AND o.status = 'completed'\n"
                     "GROUP BY c.country;\n"),
    },
    "exercise_04": {
        "title": "Exercise 4",
        "difficulty": "junior/mid",
        "focus": "JOIN + GROUP BY + HAVING",
        "tables": ["customers", "orders"],
        "check": {"ordered": True, "order_by": ["order_count"]},
        "statement": """# Exercise 4

Same tables: `customers` and `orders`.

**Task:** Which customers have **more than 3 orders**?

The query must return **only** those customers — one row per customer, with
the customer name and their order count, sorted by count, highest first.
""",
        "solution": ("SELECT c.name, COUNT(*) AS order_count\n"
                     "FROM orders o\nJOIN customers c ON c.id = o.customer_id\n"
                     "GROUP BY c.name\nHAVING COUNT(*) > 3\n"
                     "ORDER BY order_count DESC;\n"),
    },
    "exercise_05": {
        "title": "Exercise 5",
        "difficulty": "junior/mid",
        "focus": "JOIN + GROUP BY + ORDER BY + LIMIT (top-N)",
        "tables": ["customers", "orders"],
        "check": {"ordered": True, "order_by": ["total_completed_usd"]},
        "statement": """# Exercise 5

Same tables: `customers` and `orders`.

**Task:** Show the **top 5 customers by total completed order amount** (sum
of `order_usd_amount` over their `completed` orders).

Return the customer name and that total amount, highest first.
""",
        "solution": ("SELECT c.name, SUM(o.order_usd_amount) AS total_completed_usd\n"
                     "FROM orders o\nJOIN customers c ON c.id = o.customer_id\n"
                     "WHERE o.status = 'completed'\n"
                     "GROUP BY c.name\nORDER BY total_completed_usd DESC\nLIMIT 5;\n"),
    },
}


def _csv_bytes(header, rows):
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def render_exercise(name, spec):
    """Every file of one exercise as {relative path: bytes}.

    main() writes exactly what this returns, so a test can assert the
    committed files still match the generator byte for byte.
    """
    meta = {
        "title": spec["title"],
        "difficulty": spec["difficulty"],
        "focus": spec["focus"],  # interviewer-facing only (--list)
        "tables": [{"name": t, "csv": f"data/{t}.csv"} for t in spec["tables"]],
        "sample_rows": 5,
        "check": spec["check"],  # interviewer-only result comparison options
    }
    ddl = []
    if "customers" in spec["tables"]:
        ddl.append(CUSTOMERS_DDL)
    if "orders" in spec["tables"]:
        ddl.append(ORDERS_DDL)

    files = {
        "exercise.json": (json.dumps(meta, ensure_ascii=False, indent=2)
                          + "\n").encode("utf-8"),
        "statement.md": spec["statement"].encode("utf-8"),
        "schema.sql": ("\n\n".join(ddl) + "\n").encode("utf-8"),
        "solution.sql": spec["solution"].encode("utf-8"),
        "data/orders.csv": _csv_bytes(ORDERS_HEADER, orders),
    }
    if "customers" in spec["tables"]:
        files["data/customers.csv"] = _csv_bytes(CUSTOMERS_HEADER, customers)
    return files


def main():
    for name, spec in EXERCISES.items():
        folder = os.path.join(EX_DIR, name)
        os.makedirs(os.path.join(folder, "data"), exist_ok=True)
        for relpath, content in render_exercise(name, spec).items():
            with open(os.path.join(folder, relpath), "wb") as f:
                f.write(content)

    print(f"customers: {len(customers)} rows, orders: {len(orders)} rows")
    print("completed:", sum(1 for o in orders if o[3] == "completed"))
    print(">3 orders:", {cid: n for cid, n in
                         Counter(o[1] for o in orders).items() if n > 3})
    print("amounts > 500:", sum(1 for o in orders if o[4] > 500))


if __name__ == "__main__":
    main()
