# Agentic Payroll Platform (local sample)

A minimal, runnable sample of a **natively agentic payroll platform**: payroll
transactions stream in, a fast rule-based sensor layer screens every one of
them instantly, and anything suspicious is handed to a **Claude-based
investigative agent** that pulls extra context (employee history, department
pay norms, pay-run context) via tool calls and returns a verdict — `clear`,
`monitor`, `hold`, or `escalate` — with a human-readable explanation.

No frontend yet (by design, per your request) — this is the backend/agent
core: a SQLite-backed pipeline + a FastAPI layer over it. A UI can subscribe
to `/stream` (Server-Sent Events) later without any backend changes.

## Why this architecture

Running every paycheck through an LLM doesn't scale and isn't necessary —
most payroll is boring. So the pipeline is two-tier:

1. **Rule engine** (`app/rules_engine.py`) — deterministic, instant, free.
   Flags statistical pay outliers, terminated/"ghost" employees being paid,
   duplicate payments, bank-account changes right before a payout, off-cycle
   runs, and implausible amounts.
2. **LLM agent** (`app/llm_agent.py`) — only runs on transactions the rule
   engine already flagged. It's given the transaction + which rules fired,
   and a small toolbox (`get_employee_profile`, `get_employee_pay_history`,
   `get_department_peer_pay`, `get_pay_run_context`). It decides which tools
   it needs, calls them, and returns a structured verdict — this is the
   "agentic" part: it reasons and gathers evidence rather than just
   classifying from a fixed feature vector.

`app/orchestrator.py` is the glue that wires sensor → agent → database →
live event stream. `app/simulator.py` generates a believable real-time
payroll event feed (normal payments + periodically injected anomalies) so
you can watch the whole thing work without hooking up a real payroll feed.

## Quickstart (terminal only, no API needed)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt        # only needed for the API server; the
                                        # terminal demo below only needs stdlib

python3 -m app.seed                    # creates payroll.db, 18 employees, 6 periods of history
python3 run_demo.py                    # watch the agent react to a live event stream
```

You'll see output like:

```
=== new pay run #7 ===
  ✓ paid Maria Chen              $  5,692.31
  ⚠  injecting anomaly: bank_account_change (employee #1)
  🔎 rules flagged tx #109 (Maria Chen) severity=medium triggers=['bank_account_changed']
  🤖 agent verdict: HOLD (confidence=0.70) -- Maria Chen
     "Maria Chen's payout account changed immediately before this payment..."
```

By default this runs with **no `ANTHROPIC_API_KEY` set**, so `MockAgent`
(a deterministic stand-in in `app/llm_agent.py`) handles verdicts. The whole
pipeline — simulator, rules, agent, database — is fully exercised this way.

## Running with a real Claude agent

```bash
cp .env.example .env
# put your key in .env, then:
export ANTHROPIC_API_KEY=sk-ant-...
python3 run_demo.py
```

Now flagged transactions go through `ClaudeAgent`, which actually calls the
tools (you'll see real tool-use reasoning if you inspect `agent_tool_calls`
in the DB / API) before returning its verdict.

## Running the API

```bash
uvicorn app.main:app --reload --port 8000
```

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Service + simulation status |
| `/simulation/start` | POST | Start the live event generator |
| `/simulation/stop` | POST | Stop it |
| `/employees` | GET | List employees |
| `/transactions?limit=50` | GET | Recent transactions |
| `/anomalies?status=investigating&limit=50` | GET | Flagged transactions + agent verdicts |
| `/anomalies/{id}/resolve` | POST | Mark a flag resolved (human-in-the-loop) |
| `/stream` | GET | SSE feed of live transaction/flag/verdict events — point a future frontend's `EventSource` at this |

Example:

```bash
curl -X POST localhost:8000/simulation/start
curl localhost:8000/anomalies | python3 -m json.tool
curl -N localhost:8000/stream          # live feed, Ctrl-C to stop
```

## Project layout

```
app/
  db.py             SQLite schema + connection helpers (stdlib only)
  models.py         Dataclasses for Employee / Transaction / AnomalyFlag
  seed.py           Creates employees + 6 periods of clean baseline pay history
  rules_engine.py   Fast deterministic anomaly "sensors"
  llm_agent.py       Claude tool-use investigative agent (+ offline mock)
  orchestrator.py   Wires rules -> agent -> DB -> event callbacks
  simulator.py      Generates a live payroll event stream with injected anomalies
  main.py           FastAPI app (REST + SSE)
run_demo.py         Terminal demo runner, no API server needed
requirements.txt
.env.example
```

## Anomaly types the simulator injects

| Kind | What it simulates | Rule(s) it should trigger |
|---|---|---|
| `ghost_employee` | Paying a terminated employee | `inactive_employee_paid` |
| `duplicate_payment` | Same employee paid twice in one pay run | `duplicate_payment` |
| `bank_account_change` | Payout account changed right before a payment | `bank_account_changed` |
| `pay_spike` | Gross pay 3.5–6x an employee's normal amount | `gross_pay_outlier` (+ often `off_cycle_run`) |

## Extending this

- **Frontend**: build against `/stream` (SSE) + `/anomalies` + `/transactions`;
  no backend changes needed.
- **Real payroll data**: replace `simulator.py`'s generator with a real
  ingestion source (webhook, message queue, HRIS export) that calls
  `orchestrator.process_transaction(tx_id)` per new transaction.
- **More rules**: add functions to `rules_engine.evaluate()` — keep them
  fast and deterministic; that's what keeps LLM spend proportional to risk.
- **More agent tools**: add entries to `TOOLS` + `TOOL_IMPL` in
  `llm_agent.py` (e.g. a tool to check for open HR tickets, or compare
  against last year's W-2).
- **Persistence at scale**: swap `db.py`'s sqlite3 calls for Postgres/SQLAlchemy
  when you outgrow a single local file — the rest of the app only depends on
  the row-shaped query results, not on sqlite specifically.
