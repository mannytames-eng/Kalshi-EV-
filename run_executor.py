#!/usr/bin/env python3
"""
run_executor.py — standalone runner for the execution module.

Runs as its OWN process, SEPARATE from the scanner (a second Railway worker, or
locally). Every EXECUTOR_INTERVAL_SEC it reads the scanner's flagged bets, feeds
them to kalshi_execution, and places any approved pending orders. Never import
this into the scanner — that isolation is the whole point.

Self-gates on the module's switches: this loop is INERT until the module is armed
(KALSHI_EXECUTION_ENABLED=1 and KALSHI_EXECUTION_DRY_RUN=0). With APPROVAL_REQUIRED
on (default), it only QUEUES orders + alerts; you approve via the CLI, and the
next tick places them.

Where it gets the bets:
  • BETS_FILE (default /data/ev_bets.json) — if the runner shares the scanner's
    volume (same Railway service/volume, or local).
  • BETS_URL  (e.g. https://<app>/api/today_edges) — set this if the runner is a
    SEPARATE service without the shared volume; it fetches open bets over HTTP.

Run:  python run_executor.py
"""

import json
import os
import time

from kalshi_execution import (
    execute_flagged_bets, process_approved_orders,
    EXECUTION_ENABLED, DRY_RUN, APPROVAL_REQUIRED, MARKET_ALLOWLIST,
)

BETS_FILE            = os.environ.get("BETS_FILE", "/data/ev_bets.json")
BETS_URL             = os.environ.get("BETS_URL", "")   # set for cross-service HTTP fetch
EXECUTOR_INTERVAL_SEC = int(os.environ.get("EXECUTOR_INTERVAL_SEC", "60"))


def _load_bets() -> list:
    """Read the scanner's bets (file or HTTP). Returns [] on any failure so the
    loop never dies on a transient read error."""
    try:
        if BETS_URL:
            import requests
            data = requests.get(BETS_URL, timeout=15).json()
        else:
            with open(BETS_FILE) as f:
                data = json.load(f)
        if isinstance(data, dict):                       # tolerate {"bets":[...]} shapes
            data = data.get("bets") or data.get("rows") or []
        return data if isinstance(data, list) else []
    except Exception as exc:
        print(f"[runner] could not read bets ({BETS_URL or BETS_FILE}): {exc}")
        return []


def main() -> None:
    src = BETS_URL or BETS_FILE
    print(f"[runner] start — ENABLED={EXECUTION_ENABLED} DRY_RUN={DRY_RUN} "
          f"APPROVAL_REQUIRED={APPROVAL_REQUIRED} allow={MARKET_ALLOWLIST} "
          f"bets={src} every {EXECUTOR_INTERVAL_SEC}s")
    if not EXECUTION_ENABLED or DRY_RUN:
        print("[runner] NOTE: inert config — no real orders will be placed "
              "(need EXECUTION_ENABLED=1 and DRY_RUN=0).")
    while True:
        try:
            bets = _load_bets()
            execute_flagged_bets(bets)      # approval mode → queue+alert; autonomous → place
            process_approved_orders()        # place APPROVED, unexpired; expire stale
        except Exception as exc:
            print(f"[runner] loop error: {exc}")
        time.sleep(EXECUTOR_INTERVAL_SEC)


if __name__ == "__main__":
    main()
