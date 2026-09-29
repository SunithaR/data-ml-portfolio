from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import db
from .simulator import PayrollSimulator

app = FastAPI(title="Agentic Payroll Platform", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory event queue for the live SSE stream. Fine for a local single-process demo.
_subscribers: List[asyncio.Queue] = []
_loop: Optional[asyncio.AbstractEventLoop] = None


def _broadcast(event: dict):
    if _loop is None:
        return
    for q in list(_subscribers):
        _loop.call_soon_threadsafe(q.put_nowait, event)


sim = PayrollSimulator(interval=2.0, anomaly_rate=0.25, on_event=_broadcast)


@app.on_event("startup")
async def startup():
    global _loop
    _loop = asyncio.get_event_loop()
    db.init_db(reset=False)  # keep existing data if present; run `python -m app.seed` to reset


@app.get("/health")
def health():
    return {"status": "ok", "simulation_running": sim.running}


@app.post("/simulation/start")
def start_simulation():
    sim.start()
    return {"status": "started"}


@app.post("/simulation/stop")
def stop_simulation():
    sim.stop()
    return {"status": "stopped"}


@app.get("/employees")
def list_employees():
    with db.write_conn() as conn:
        rows = conn.execute("SELECT * FROM employees ORDER BY id").fetchall()
        return [dict(r) for r in rows]


@app.get("/transactions")
def list_transactions(limit: int = 50):
    with db.write_conn() as conn:
        rows = conn.execute(
            """SELECT t.*, e.name as employee_name FROM transactions t
               JOIN employees e ON e.id = t.employee_id
               ORDER BY t.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/anomalies")
def list_anomalies(status: Optional[str] = None, limit: int = 50):
    with db.write_conn() as conn:
        query = """SELECT f.*, e.name as employee_name, t.gross_pay, t.net_pay
                    FROM anomaly_flags f
                    JOIN employees e ON e.id = f.employee_id
                    JOIN transactions t ON t.id = f.transaction_id"""
        params: list = []
        if status:
            query += " WHERE f.status = ?"
            params.append(status)
        query += " ORDER BY f.id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["rule_triggers"] = json.loads(d["rule_triggers"])
            d["agent_tool_calls"] = json.loads(d["agent_tool_calls"]) if d["agent_tool_calls"] else []
            out.append(d)
        return out


@app.post("/anomalies/{flag_id}/resolve")
def resolve_anomaly(flag_id: int):
    import datetime as dt
    with db.write_conn() as conn:
        conn.execute(
            "UPDATE anomaly_flags SET status='resolved', resolved_at=? WHERE id=?",
            (dt.datetime.utcnow().isoformat(), flag_id),
        )
    return {"status": "resolved", "flag_id": flag_id}


@app.get("/stream")
async def stream():
    """Server-Sent Events feed of live simulation activity: transactions,
    rule flags, and agent verdicts as they happen. Point a frontend
    EventSource at this endpoint for a live dashboard."""
    async def event_gen():
        q: asyncio.Queue = asyncio.Queue()
        _subscribers.append(q)
        try:
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            _subscribers.remove(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")
