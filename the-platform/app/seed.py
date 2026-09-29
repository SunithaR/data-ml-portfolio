from __future__ import annotations

import datetime as dt
import random

from . import db

EMPLOYEES = [
    ("Maria Chen", "Engineering", "Senior Engineer", 148_000, "biweekly"),
    ("James Okafor", "Engineering", "Engineer II", 118_000, "biweekly"),
    ("Priya Natarajan", "Engineering", "Staff Engineer", 172_000, "biweekly"),
    ("Tom Reilly", "Sales", "Account Executive", 95_000, "biweekly"),
    ("Sofia Marquez", "Sales", "Sales Manager", 132_000, "biweekly"),
    ("David Kim", "Finance", "Financial Analyst", 88_000, "biweekly"),
    ("Elena Petrova", "Finance", "Controller", 145_000, "monthly"),
    ("Ahmed Hassan", "Operations", "Ops Lead", 102_000, "biweekly"),
    ("Grace Liu", "Operations", "Ops Associate", 68_000, "biweekly"),
    ("Marcus Webb", "HR", "HR Business Partner", 91_000, "biweekly"),
    ("Nina Kowalski", "HR", "Recruiter", 78_000, "biweekly"),
    ("Ravi Patel", "Engineering", "Engineer I", 102_000, "biweekly"),
    ("Chloe Dubois", "Marketing", "Marketing Manager", 110_000, "biweekly"),
    ("Oscar Nilsson", "Marketing", "Content Strategist", 82_000, "biweekly"),
    ("Hannah Osei", "Engineering", "Engineering Manager", 168_000, "biweekly"),
    ("Leo Fischer", "Sales", "SDR", 62_000, "biweekly"),
    ("Aiyana Begay", "Finance", "AP Specialist", 71_000, "biweekly"),
    ("Ben Sutton", "Operations", "Warehouse Supervisor", 76_000, "biweekly"),
]


def _bank_account(seed: int) -> str:
    rnd = random.Random(seed)
    return f"****{rnd.randint(1000, 9999)}"


def seed_employees(conn) -> list[int]:
    ids = []
    today = dt.date.today()
    for i, (name, dept, role, salary, cadence) in enumerate(EMPLOYEES, start=1):
        hire_date = today - dt.timedelta(days=random.randint(90, 2500))
        conn.execute(
            """INSERT INTO employees
               (id, name, department, role, status, base_salary, pay_cadence, bank_account, hire_date)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (i, name, dept, role, "active", salary, cadence, _bank_account(i), hire_date.isoformat()),
        )
        ids.append(i)
    return ids


def seed_baseline_history(conn, employee_ids: list[int], periods: int = 6) -> None:
    """Create `periods` past pay runs with clean (non-anomalous) transactions
    so the rules engine has a real baseline to compute deviation against."""
    today = dt.date.today()
    for p in range(periods, 0, -1):
        period_end = today - dt.timedelta(days=14 * p)
        period_start = period_end - dt.timedelta(days=13)
        cur = conn.execute(
            "INSERT INTO pay_runs (period_start, period_end, run_type, created_at) VALUES (?,?,?,?)",
            (period_start.isoformat(), period_end.isoformat(), "scheduled", period_end.isoformat()),
        )
        pay_run_id = cur.lastrowid

        for emp_id in employee_ids:
            row = conn.execute("SELECT base_salary, bank_account FROM employees WHERE id=?", (emp_id,)).fetchone()
            base_salary, bank_account = row["base_salary"], row["bank_account"]
            gross = round((base_salary / 26) * random.uniform(0.98, 1.02), 2)  # biweekly-ish
            deductions = round(gross * random.uniform(0.22, 0.30), 2)
            net = round(gross - deductions, 2)
            conn.execute(
                """INSERT INTO transactions
                   (pay_run_id, employee_id, gross_pay, deductions, net_pay, bank_account, created_at, is_synthetic_anomaly)
                   VALUES (?,?,?,?,?,?,?,NULL)""",
                (pay_run_id, emp_id, gross, deductions, net, bank_account, period_end.isoformat()),
            )


def run_seed(reset: bool = True) -> None:
    db.init_db(reset=reset)
    with db.write_conn() as conn:
        ids = seed_employees(conn)
        seed_baseline_history(conn, ids)
    print(f"Seeded {len(EMPLOYEES)} employees and 6 periods of baseline history at {db.DB_PATH}")


if __name__ == "__main__":
    run_seed()
