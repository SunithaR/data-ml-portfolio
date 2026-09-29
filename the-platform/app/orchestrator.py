"""
The orchestrator is the glue of the "agentic" pipeline:

  new transaction --> rules_engine.evaluate() --> [if flagged] --> llm_agent.investigate()
                                                                          |
                                                                          v
                                                              anomaly_flags row + event

Nothing here calls an LLM unless the fast rule layer already found something
worth looking at, so the system stays cheap at scale while still giving every
suspicious transaction real investigative reasoning, not just a rule label.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Callable, Optional

from . import db, rules_engine
from .llm_agent import get_agent

_agent = None


def _get_agent_singleton():
    global _agent
    if _agent is None:
        _agent = get_agent()
    return _agent


def process_transaction(transaction_id: int, on_event: Optional[Callable[[dict], None]] = None) -> Optional[dict]:
    """Run the full sensor -> agent pipeline for one transaction.

    Returns the anomaly record (dict) if flagged, else None.
    Fires on_event(event_dict) at each stage for live streaming (SSE / logging).
    """
    with db.write_conn() as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        emp = conn.execute("SELECT * FROM employees WHERE id=?", (tx["employee_id"],)).fetchone()

        rule_result = rules_engine.evaluate(conn, transaction_id)

        if on_event:
            on_event({
                "type": "transaction",
                "transaction_id": transaction_id,
                "employee": emp["name"],
                "gross_pay": tx["gross_pay"],
                "flagged": rule_result.flagged,
            })

        if not rule_result.flagged:
            return None

        if on_event:
            on_event({
                "type": "rule_flag",
                "transaction_id": transaction_id,
                "employee": emp["name"],
                "severity": rule_result.severity,
                "triggers": [t[0] for t in rule_result.triggers],
            })

        agent = _get_agent_singleton()
        verdict = agent.investigate(conn, dict(tx), dict(emp), rule_result)

        now = dt.datetime.utcnow().isoformat()
        cur = conn.execute(
            """INSERT INTO anomaly_flags
               (transaction_id, employee_id, rule_triggers, rule_severity,
                agent_verdict, agent_explanation, agent_confidence, agent_tool_calls,
                status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                transaction_id,
                emp["id"],
                json.dumps([{"rule": n, "detail": d} for n, d in rule_result.triggers]),
                rule_result.severity,
                verdict["verdict"],
                verdict["explanation"],
                verdict["confidence"],
                json.dumps(verdict.get("tool_calls", [])),
                "investigating",
                now,
            ),
        )
        flag_id = cur.lastrowid

        result = {
            "flag_id": flag_id,
            "transaction_id": transaction_id,
            "employee": emp["name"],
            "rule_severity": rule_result.severity,
            "rule_triggers": [t[0] for t in rule_result.triggers],
            "agent_verdict": verdict["verdict"],
            "agent_confidence": verdict["confidence"],
            "agent_explanation": verdict["explanation"],
        }

        if on_event:
            on_event({"type": "agent_verdict", **result})

        return result
