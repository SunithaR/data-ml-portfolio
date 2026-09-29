"""
Database layer for the agentic payroll platform.

Uses plain sqlite3 (stdlib) so the "sensor" side of the system has zero
external dependencies. Only the API layer (FastAPI) and the LLM agent
(anthropic SDK) need third-party packages.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "payroll.db"

# sqlite3 connections aren't thread-safe by default; we use one lock to
# serialize writes coming from the simulator thread, the orchestrator
# thread, and API request threads.
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    department      TEXT NOT NULL,
    role            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | terminated
    base_salary     REAL NOT NULL,                    -- annual, USD
    pay_cadence     TEXT NOT NULL DEFAULT 'biweekly',  -- biweekly | monthly
    bank_account    TEXT NOT NULL,
    hire_date       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pay_runs (
    id              INTEGER PRIMARY KEY,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    run_type        TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | off_cycle
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    pay_run_id      INTEGER NOT NULL,
    employee_id     INTEGER NOT NULL,
    gross_pay       REAL NOT NULL,
    deductions      REAL NOT NULL,
    net_pay         REAL NOT NULL,
    bank_account    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    is_synthetic_anomaly TEXT,   -- label injected by simulator, for demo scoring only
    FOREIGN KEY (pay_run_id) REFERENCES pay_runs(id),
    FOREIGN KEY (employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS anomaly_flags (
    id                  INTEGER PRIMARY KEY,
    transaction_id      INTEGER NOT NULL,
    employee_id         INTEGER NOT NULL,
    rule_triggers       TEXT NOT NULL,   -- JSON list of rule names that fired
    rule_severity       TEXT NOT NULL,   -- low | medium | high
    agent_verdict       TEXT,            -- clear | monitor | hold | escalate
    agent_explanation   TEXT,
    agent_confidence    REAL,
    agent_tool_calls    TEXT,            -- JSON trace of tool calls the agent made
    status              TEXT NOT NULL DEFAULT 'investigating', -- investigating | resolved
    created_at          TEXT NOT NULL,
    resolved_at         TEXT,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
    FOREIGN KEY (employee_id) REFERENCES employees(id)
);

CREATE INDEX IF NOT EXISTS idx_tx_employee ON transactions(employee_id);
CREATE INDEX IF NOT EXISTS idx_flags_tx ON anomaly_flags(transaction_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset: bool = False) -> None:
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_conn():
    """Serialized connection for writes coming from any thread."""
    with _write_lock:
        conn = get_conn()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
