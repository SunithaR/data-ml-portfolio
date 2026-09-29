"""
Simulated real-time payroll event stream.

Runs on a background thread, ticking every `interval` seconds. Each tick
either issues a normal payment or (at `anomaly_rate` probability) injects
one of several known anomaly patterns. Every transaction it creates is
immediately fed through orchestrator.process_transaction, so the agent
reacts as events arrive -- this is what makes the demo feel "real-time"
without needing a message broker.
"""
from __future__ import annotations

import datetime as dt
import random
import threading
import time
from typing import Callable, Optional

from . import db, orchestrator

ANOMALY_KINDS = ["ghost_employee", "duplicate_payment", "bank_account_change", "pay_spike"]


class PayrollSimulator:
    def __init__(self, interval: float = 2.0, anomaly_rate: float = 0.25,
                 on_event: Optional[Callable[[dict], None]] = None):
        self.interval = interval
        self.anomaly_rate = anomaly_rate
        self.on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._current_pay_run_id: Optional[int] = None
        self._paid_in_run: set[int] = set()
        self._employee_ids: list[int] = []
        self._tick_count = 0

    # -- lifecycle ---------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        with db.write_conn() as conn:
            self._employee_ids = [r["id"] for r in conn.execute("SELECT id FROM employees").fetchall()]
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    # -- main loop -----------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # pragma: no cover - keep the demo alive
                if self.on_event:
                    self.on_event({"type": "error", "detail": str(e)})
            time.sleep(self.interval)

    def _emit(self, event: dict):
        if self.on_event:
            self.on_event(event)

    def _ensure_pay_run(self, conn, off_cycle: bool = False) -> int:
        if off_cycle:
            today = dt.date.today()
            cur = conn.execute(
                "INSERT INTO pay_runs (period_start, period_end, run_type, created_at) VALUES (?,?,?,?)",
                (today.isoformat(), today.isoformat(), "off_cycle", dt.datetime.utcnow().isoformat()),
            )
            return cur.lastrowid

        if self._current_pay_run_id is None or len(self._paid_in_run) >= len(self._employee_ids):
            today = dt.date.today()
            period_start = today - dt.timedelta(days=13)
            cur = conn.execute(
                "INSERT INTO pay_runs (period_start, period_end, run_type, created_at) VALUES (?,?,?,?)",
                (period_start.isoformat(), today.isoformat(), "scheduled", dt.datetime.utcnow().isoformat()),
            )
            self._current_pay_run_id = cur.lastrowid
            self._paid_in_run = set()
            self._emit({"type": "pay_run_started", "pay_run_id": self._current_pay_run_id})
        return self._current_pay_run_id

    def _normal_gross(self, conn, employee_id: int) -> float:
        row = conn.execute("SELECT base_salary FROM employees WHERE id=?", (employee_id,)).fetchone()
        return round((row["base_salary"] / 26) * random.uniform(0.98, 1.02), 2)

    def _insert_tx(self, conn, pay_run_id: int, employee_id: int, gross: float,
                   bank_account: Optional[str] = None, label: Optional[str] = None) -> int:
        emp = conn.execute("SELECT bank_account FROM employees WHERE id=?", (employee_id,)).fetchone()
        bank_account = bank_account or emp["bank_account"]
        deductions = round(gross * random.uniform(0.22, 0.30), 2)
        net = round(gross - deductions, 2)
        cur = conn.execute(
            """INSERT INTO transactions
               (pay_run_id, employee_id, gross_pay, deductions, net_pay, bank_account, created_at, is_synthetic_anomaly)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pay_run_id, employee_id, gross, deductions, net, bank_account,
             dt.datetime.utcnow().isoformat(), label),
        )
        return cur.lastrowid

    # -- one tick: normal payment OR injected anomaly -------------------
    def _tick(self):
        self._tick_count += 1
        inject_anomaly = random.random() < self.anomaly_rate

        with db.write_conn() as conn:
            if inject_anomaly:
                kind = random.choice(ANOMALY_KINDS)
                tx_id = self._inject(conn, kind)
            else:
                candidates = [e for e in self._employee_ids if e not in self._paid_in_run] or self._employee_ids
                employee_id = random.choice(candidates)
                pay_run_id = self._ensure_pay_run(conn)
                gross = self._normal_gross(conn, employee_id)
                tx_id = self._insert_tx(conn, pay_run_id, employee_id, gross)
                self._paid_in_run.add(employee_id)

        # process outside the write lock scope of insertion, orchestrator opens its own conn
        orchestrator.process_transaction(tx_id, on_event=self._emit)

    def _inject(self, conn, kind: str) -> int:
        employee_id = random.choice(self._employee_ids)

        if kind == "ghost_employee":
            conn.execute("UPDATE employees SET status='terminated' WHERE id=?", (employee_id,))
            pay_run_id = self._ensure_pay_run(conn)
            gross = self._normal_gross(conn, employee_id)
            self._emit({"type": "injected", "kind": kind, "employee_id": employee_id})
            return self._insert_tx(conn, pay_run_id, employee_id, gross, label=kind)

        if kind == "duplicate_payment":
            pay_run_id = self._ensure_pay_run(conn)
            already_paid = list(self._paid_in_run) or self._employee_ids
            employee_id = random.choice(already_paid)
            gross = self._normal_gross(conn, employee_id)
            self._emit({"type": "injected", "kind": kind, "employee_id": employee_id})
            return self._insert_tx(conn, pay_run_id, employee_id, gross, label=kind)

        if kind == "bank_account_change":
            new_account = f"****{random.randint(1000, 9999)}"
            pay_run_id = self._ensure_pay_run(conn)
            gross = self._normal_gross(conn, employee_id)
            self._emit({"type": "injected", "kind": kind, "employee_id": employee_id})
            tx_id = self._insert_tx(conn, pay_run_id, employee_id, gross, bank_account=new_account, label=kind)
            conn.execute("UPDATE employees SET bank_account=? WHERE id=?", (new_account, employee_id))
            return tx_id

        # pay_spike
        pay_run_id = self._ensure_pay_run(conn, off_cycle=random.random() < 0.5)
        gross = self._normal_gross(conn, employee_id) * random.uniform(3.5, 6.0)
        self._emit({"type": "injected", "kind": kind, "employee_id": employee_id})
        return self._insert_tx(conn, pay_run_id, employee_id, round(gross, 2), label=kind)
