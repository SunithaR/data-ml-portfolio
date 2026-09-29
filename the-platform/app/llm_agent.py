"""
The investigative agent.

When ANTHROPIC_API_KEY is set, this drives a real Claude tool-use loop:
Claude is given the flagged transaction + the rule triggers that caused it
to be flagged, and a small toolbox to pull more context (employee profile,
pay history, department peer pay, pay-run context). Claude decides which
tools it needs, calls them, and keeps reasoning until it's ready to return
a structured verdict.

When no API key is present, `MockAgent` runs a deterministic stand-in so
the whole pipeline (simulator -> rules -> agent -> DB -> API) still works
end-to-end for local testing without any external calls or cost.
"""
from __future__ import annotations

import json
import os
import statistics
from typing import Any

from . import db

MODEL = "claude-sonnet-4-6"
MAX_TOOL_ITERATIONS = 6

SYSTEM_PROMPT = """You are a payroll anomaly investigator agent for a payroll platform.

You are handed a single transaction that a fast rule-based sensor layer has already
flagged, along with which rules fired. Your job is NOT to re-run those rules -- it's to
investigate context the rules can't see: employee tenure and role, department pay norms,
recent changes, and patterns across the pay run. Use the tools available to gather that
context before deciding.

Return your final decision as a single JSON object (no markdown, no prose outside it)
with exactly these fields:
{
  "verdict": "clear" | "monitor" | "hold" | "escalate",
  "confidence": <float 0.0-1.0>,
  "explanation": "<2-4 sentences a payroll ops reviewer can act on>"
}

Guidance on verdicts:
- "clear": context explains the anomaly (e.g. legitimate bonus, backdated raise, role change). Safe to pay.
- "monitor": nothing alarming now, but worth a human glance / watch next cycle.
- "hold": risk is high enough that this payment should be held pending human review before it's released.
- "escalate": strong signal of fraud, error, or policy violation (e.g. terminated employee paid, duplicate payment, ghost employee) -- needs immediate human attention.
"""

TOOLS = [
    {
        "name": "get_employee_profile",
        "description": "Get an employee's profile: department, role, status, tenure, base salary, bank account.",
        "input_schema": {
            "type": "object",
            "properties": {"employee_id": {"type": "integer"}},
            "required": ["employee_id"],
        },
    },
    {
        "name": "get_employee_pay_history",
        "description": "Get an employee's recent past transactions (gross pay, deductions, net pay, dates).",
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "max records, default 10"},
            },
            "required": ["employee_id"],
        },
    },
    {
        "name": "get_department_peer_pay",
        "description": "Get average and range of gross pay for other active employees in the same department and role, for comparison.",
        "input_schema": {
            "type": "object",
            "properties": {"employee_id": {"type": "integer"}},
            "required": ["employee_id"],
        },
    },
    {
        "name": "get_pay_run_context",
        "description": "Get info about the pay run this transaction belongs to: type, dates, and how many other transactions are in it.",
        "input_schema": {
            "type": "object",
            "properties": {"pay_run_id": {"type": "integer"}},
            "required": ["pay_run_id"],
        },
    },
]


def _tool_get_employee_profile(conn, employee_id: int) -> dict:
    row = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    return dict(row) if row else {"error": "not found"}


def _tool_get_employee_pay_history(conn, employee_id: int, limit: int = 10) -> dict:
    rows = conn.execute(
        "SELECT gross_pay, deductions, net_pay, created_at FROM transactions WHERE employee_id=? ORDER BY id DESC LIMIT ?",
        (employee_id, limit),
    ).fetchall()
    return {"history": [dict(r) for r in rows]}


def _tool_get_department_peer_pay(conn, employee_id: int) -> dict:
    emp = conn.execute("SELECT department, role FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not emp:
        return {"error": "not found"}
    rows = conn.execute(
        """SELECT t.gross_pay FROM transactions t
           JOIN employees e ON e.id = t.employee_id
           WHERE e.department=? AND e.role=? AND e.status='active' AND e.id != ?""",
        (emp["department"], emp["role"], employee_id),
    ).fetchall()
    amounts = [r["gross_pay"] for r in rows]
    if not amounts:
        return {"peer_count": 0}
    return {
        "peer_count": len(amounts),
        "avg_gross_pay": round(statistics.mean(amounts), 2),
        "min_gross_pay": round(min(amounts), 2),
        "max_gross_pay": round(max(amounts), 2),
    }


def _tool_get_pay_run_context(conn, pay_run_id: int) -> dict:
    run = conn.execute("SELECT * FROM pay_runs WHERE id=?", (pay_run_id,)).fetchone()
    if not run:
        return {"error": "not found"}
    count = conn.execute("SELECT COUNT(*) c FROM transactions WHERE pay_run_id=?", (pay_run_id,)).fetchone()["c"]
    return {**dict(run), "transaction_count": count}


TOOL_IMPL = {
    "get_employee_profile": _tool_get_employee_profile,
    "get_employee_pay_history": _tool_get_employee_pay_history,
    "get_department_peer_pay": _tool_get_department_peer_pay,
    "get_pay_run_context": _tool_get_pay_run_context,
}


def _build_task_prompt(tx: dict, emp: dict, rule_result) -> str:
    triggers = [{"rule": name, "detail": detail} for name, detail in rule_result.triggers]
    return json.dumps(
        {
            "transaction": {
                "id": tx["id"],
                "employee_id": tx["employee_id"],
                "pay_run_id": tx["pay_run_id"],
                "gross_pay": tx["gross_pay"],
                "deductions": tx["deductions"],
                "net_pay": tx["net_pay"],
                "bank_account": tx["bank_account"],
            },
            "employee_snapshot": {
                "name": emp["name"],
                "department": emp["department"],
                "role": emp["role"],
                "status": emp["status"],
            },
            "rule_triggers": triggers,
            "rule_severity": rule_result.severity,
        },
        indent=2,
    )


class ClaudeAgent:
    def __init__(self):
        import anthropic  # imported lazily so the mock path never needs it installed

        self.client = anthropic.Anthropic()

    def investigate(self, conn, tx: dict, emp: dict, rule_result) -> dict:
        task_prompt = _build_task_prompt(tx, emp, rule_result)
        messages = [{"role": "user", "content": task_prompt}]
        tool_trace = []

        for _ in range(MAX_TOOL_ITERATIONS):
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                final_text = "".join(b.text for b in response.content if b.type == "text")
                verdict = _parse_verdict(final_text)
                verdict["tool_calls"] = tool_trace
                return verdict

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                impl = TOOL_IMPL.get(block.name)
                result = impl(conn, **block.input) if impl else {"error": "unknown tool"}
                tool_trace.append({"tool": block.name, "input": block.input, "output": result})
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)}
                )
            messages.append({"role": "user", "content": tool_results})

        return {
            "verdict": "monitor",
            "confidence": 0.3,
            "explanation": "Agent did not converge within the tool-call budget; defaulting to monitor for human review.",
            "tool_calls": tool_trace,
        }


class MockAgent:
    """Deterministic offline stand-in -- no API key required. Mirrors the
    verdict policy a real investigation would apply, using only rule output
    plus one context lookup, so the pipeline is fully runnable locally."""

    def investigate(self, conn, tx: dict, emp: dict, rule_result) -> dict:
        rule_names = {name for name, _ in rule_result.triggers}
        tool_trace = [{"tool": "get_employee_profile", "input": {"employee_id": emp["id"]}, "output": dict(emp)}]

        if "inactive_employee_paid" in rule_names or "duplicate_payment" in rule_names:
            verdict, conf, why = "escalate", 0.9, (
                f"{emp['name']} triggered a high-severity rule "
                f"({', '.join(sorted(rule_names))}) with no legitimate mitigating context found. "
                "This pattern matches known payroll fraud signatures and should not be released without review."
            )
        elif "bank_account_changed" in rule_names:
            verdict, conf, why = "hold", 0.7, (
                f"{emp['name']}'s payout account changed immediately before this payment. "
                "This is a common indicator of account-takeover fraud; hold until the change is verified with the employee."
            )
        elif "gross_pay_outlier" in rule_names:
            peer = _tool_get_department_peer_pay(conn, emp["id"])
            tool_trace.append({"tool": "get_department_peer_pay", "input": {"employee_id": emp["id"]}, "output": peer})
            if peer.get("peer_count", 0) and peer["max_gross_pay"] * 1.15 >= tx["gross_pay"]:
                verdict, conf, why = "monitor", 0.5, (
                    f"Gross pay is a statistical outlier for {emp['name']}'s own history, but still within range of "
                    "department peers, suggesting a plausible raise, bonus, or role change rather than an error."
                )
            else:
                verdict, conf, why = "hold", 0.65, (
                    f"Gross pay is a statistical outlier for {emp['name']} and also well above department peers. "
                    "No supporting context found; hold for manual confirmation before release."
                )
        else:
            verdict, conf, why = "monitor", 0.4, (
                "Low-severity rule trigger with no corroborating high-risk signal. Flagging for a routine glance, not blocking."
            )

        return {"verdict": verdict, "confidence": conf, "explanation": why, "tool_calls": tool_trace}


def _parse_verdict(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
        return {
            "verdict": data.get("verdict", "monitor"),
            "confidence": float(data.get("confidence", 0.5)),
            "explanation": data.get("explanation", ""),
        }
    except (json.JSONDecodeError, ValueError):
        return {"verdict": "monitor", "confidence": 0.3, "explanation": f"Could not parse agent output: {text[:200]}"}


def get_agent():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeAgent()
    return MockAgent()
