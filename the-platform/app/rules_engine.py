"""
Fast, deterministic "sensor" layer.

These rules run synchronously on every transaction the moment it's created,
before any LLM call. Cheap and explainable. Anything they flag gets handed
to the LLM agent (agent.py) for deeper investigation. Anything they don't
flag never touches the LLM at all -- this keeps token spend proportional to
actual risk instead of scanning every paycheck with an LLM.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from . import db


@dataclass
class RuleResult:
    triggers: list = field(default_factory=list)   # list of (rule_name, detail) tuples
    severity: str = "none"                           # none | low | medium | high

    @property
    def flagged(self) -> bool:
        return len(self.triggers) > 0


SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _bump(current: str, new: str) -> str:
    return new if SEVERITY_ORDER[new] > SEVERITY_ORDER[current] else current


def evaluate(conn, transaction_id: int) -> RuleResult:
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (tx["employee_id"],)).fetchone()
    result = RuleResult()

    # --- Rule 1: statistical outlier vs employee's own pay history ---------
    history = conn.execute(
        """SELECT gross_pay FROM transactions
           WHERE employee_id=? AND id != ? ORDER BY id DESC LIMIT 12""",
        (emp["id"], tx["id"]),
    ).fetchall()
    amounts = [r["gross_pay"] for r in history]
    if len(amounts) >= 3:
        mean = statistics.mean(amounts)
        stdev = statistics.pstdev(amounts) or 1.0
        z = (tx["gross_pay"] - mean) / stdev
        if abs(z) >= 4:
            result.triggers.append(("gross_pay_outlier", f"z-score {z:.1f} vs personal history (mean={mean:.0f})"))
            result.severity = _bump(result.severity, "high")
        elif abs(z) >= 2.5:
            result.triggers.append(("gross_pay_outlier", f"z-score {z:.1f} vs personal history (mean={mean:.0f})"))
            result.severity = _bump(result.severity, "medium")

    # --- Rule 2: terminated / inactive employee receiving pay ("ghost") ----
    if emp["status"] != "active":
        result.triggers.append(("inactive_employee_paid", f"employee status is '{emp['status']}'"))
        result.severity = _bump(result.severity, "high")

    # --- Rule 3: duplicate payment in the same pay run ----------------------
    dup = conn.execute(
        """SELECT COUNT(*) c FROM transactions
           WHERE employee_id=? AND pay_run_id=? AND id != ?""",
        (emp["id"], tx["pay_run_id"], tx["id"]),
    ).fetchone()
    if dup["c"] > 0:
        result.triggers.append(("duplicate_payment", f"{dup['c']} other payment(s) to this employee in same pay run"))
        result.severity = _bump(result.severity, "high")

    # --- Rule 4: bank account changed just before this payment -------------
    prior = conn.execute(
        """SELECT bank_account FROM transactions
           WHERE employee_id=? AND id != ? ORDER BY id DESC LIMIT 1""",
        (emp["id"], tx["id"]),
    ).fetchone()
    if prior and prior["bank_account"] != tx["bank_account"]:
        result.triggers.append(
            ("bank_account_changed", f"payout account changed from {prior['bank_account']} to {tx['bank_account']}")
        )
        result.severity = _bump(result.severity, "medium")

    # --- Rule 5: off-cycle run ------------------------------------------
    run = conn.execute("SELECT run_type FROM pay_runs WHERE id=?", (tx["pay_run_id"],)).fetchone()
    if run["run_type"] == "off_cycle":
        result.triggers.append(("off_cycle_run", "payment issued outside the scheduled payroll cadence"))
        result.severity = _bump(result.severity, "low")

    # --- Rule 6: negative or implausible net pay ----------------------------
    if tx["net_pay"] <= 0 or tx["deductions"] < 0:
        result.triggers.append(("implausible_amounts", f"net_pay={tx['net_pay']}, deductions={tx['deductions']}"))
        result.severity = _bump(result.severity, "medium")

    return result
