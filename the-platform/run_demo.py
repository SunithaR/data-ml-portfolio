#!/usr/bin/env python3
"""
Terminal demo: watch the agentic pipeline work in real time without
standing up the API. Good for a first smoke test right after cloning.

Usage:
    python3 -m app.seed          # once, to create + seed payroll.db
    python3 run_demo.py          # watch it run
    python3 run_demo.py --seconds 60 --interval 1.0 --anomaly-rate 0.3
"""
from __future__ import annotations

import argparse
import time

from app.simulator import PayrollSimulator

ICONS = {
    "transaction": "  ",
    "pay_run_started": "\n=== new pay run ===",
    "injected": "⚠ ",
    "rule_flag": "🔎",
    "agent_verdict": "🤖",
    "error": "‼ ",
}

VERDICT_COLOR = {
    "clear": "\033[92m",     # green
    "monitor": "\033[93m",   # yellow
    "hold": "\033[91m",      # red
    "escalate": "\033[95m",  # magenta
}
RESET = "\033[0m"


def on_event(e: dict):
    t = e["type"]
    if t == "transaction":
        if not e["flagged"]:
            print(f"  ✓ paid {e['employee']:<20} ${e['gross_pay']:>10,.2f}")
    elif t == "pay_run_started":
        print(f"\n=== new pay run #{e['pay_run_id']} ===")
    elif t == "injected":
        print(f"  ⚠  injecting anomaly: {e['kind']} (employee #{e['employee_id']})")
    elif t == "rule_flag":
        print(f"  🔎 rules flagged tx #{e['transaction_id']} ({e['employee']}) "
              f"severity={e['severity']} triggers={e['triggers']}")
    elif t == "agent_verdict":
        color = VERDICT_COLOR.get(e["agent_verdict"], "")
        print(f"  🤖 agent verdict: {color}{e['agent_verdict'].upper()}{RESET} "
              f"(confidence={e['agent_confidence']:.2f}) -- {e['employee']}")
        print(f"     \"{e['agent_explanation']}\"")
    elif t == "error":
        print(f"  ‼  error: {e['detail']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30, help="how long to run the simulation")
    parser.add_argument("--interval", type=float, default=1.5, help="seconds between payroll events")
    parser.add_argument("--anomaly-rate", type=float, default=0.25, help="probability each event is an injected anomaly")
    args = parser.parse_args()

    print("Agentic Payroll Platform -- local demo")
    print(f"(running for {args.seconds:.0f}s, ~1 event / {args.interval}s, "
          f"{args.anomaly_rate:.0%} anomaly rate)\n")

    sim = PayrollSimulator(interval=args.interval, anomaly_rate=args.anomaly_rate, on_event=on_event)
    sim.start()
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
        print("\nSimulation stopped. Query results any time with:")
        print("  sqlite3 payroll.db 'select * from anomaly_flags;'")


if __name__ == "__main__":
    main()
