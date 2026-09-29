from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Employee:
    id: int
    name: str
    department: str
    role: str
    status: str
    base_salary: float
    pay_cadence: str
    bank_account: str
    hire_date: str


@dataclass
class Transaction:
    id: int
    pay_run_id: int
    employee_id: int
    gross_pay: float
    deductions: float
    net_pay: float
    bank_account: str
    created_at: str
    is_synthetic_anomaly: Optional[str] = None


@dataclass
class AnomalyFlag:
    id: int
    transaction_id: int
    employee_id: int
    rule_triggers: list
    rule_severity: str
    status: str
    created_at: str
    agent_verdict: Optional[str] = None
    agent_explanation: Optional[str] = None
    agent_confidence: Optional[float] = None
    agent_tool_calls: Optional[list] = None
    resolved_at: Optional[str] = None
