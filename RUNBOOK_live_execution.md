# Live Execution Runbook — `kalshi_execution.py`

How to take the execution module from **inert** to **placing real Kalshi orders**,
safely. Strikeouts-only, half-Kelly, approval-gated first. Read the whole thing
once before arming anything.

> **Roles:** Claude writes the code. **You** run it on your infrastructure with
> **your** credentials. Claude never sees your keys and never places a trade.

---

## 0. Safety model — the switches that gate real money

| Switch (env var) | Default | Effect |
|---|---|---|
| `KALSHI_EXECUTION_ENABLED` | `0` (off) | Global **kill switch**. `0` = nothing is ever submitted. |
| `KALSHI_EXECUTION_DRY_RUN` | `1` (on) | `1` = log the intended order, submit nothing. |
| `KALSHI_EXECUTION_APPROVAL_REQUIRED` | `1` (on) | `1` = each order waits for your explicit `--approve`. `0` = fully autonomous. |

Hard limits always on: **strikeouts only** (`KXMLBKS`), **half-Kelly** sizing,
**3%** per-position + **15%** daily caps (vs your **real** balance), fill
verification, full logging, Discord alert on every fill/failure.

Nothing trades until: `ENABLED=1` **and** `DRY_RUN=0` **and** a runner calls the
module **and** valid trading credentials are set.

---

## 1. Credentials (Railway → your service → **Variables**)

Set as environment variables — **never in code, never shared with anyone/any AI:**

```
KALSHI_API_KEY       = <your key ID>
KALSHI_PRIVKEY_B64   = <base64 of your private-key PEM>     # base64 -i key.pem
#   (or)  KALSHI_PRIVKEY_PATH = /path/to/key.pem
DISCORD_WEBHOOK      = <your webhook>                        # for fill/approval alerts
```

The key must have **trading (order) permission** — a read-only key can fetch
market data but cannot place orders.

---

## 2. Verify the order API (one-time, before any real order)

Kalshi's order-endpoint field names can change. Open `kalshi_execution.py` and
sanity-check `_build_order_body()` and `verify_fill()` against the **current**
Kalshi API docs:
- create order: `POST /portfolio/orders` — `ticker, action, side, count, type, yes_price/no_price, client_order_id`
- order status: `GET /portfolio/orders/{order_id}` — `status`, `filled_count`

---

## 3. The runner (the piece that makes it act "by itself")

The module is a library — something has to call it on a loop. Run this as a
**separate process** from the scanner (a second Railway worker, or locally).
A bug here still can't touch flagging.

```python
# run_executor.py — standalone runner (separate process from the scanner)
import json, time
from kalshi_execution import execute_flagged_bets, process_approved_orders

BETS_FILE = "/data/ev_bets.json"   # Railway; locally: ./ev_bets.json

while True:
    try:
        bets = json.load(open(BETS_FILE))
        execute_flagged_bets(bets)      # approval mode: queues + alerts. autonomous: places.
        process_approved_orders()        # places APPROVED, unexpired orders; expires stale
    except Exception as e:
        print(f"[runner] error: {e}")
    time.sleep(60)
```

Run: `python run_executor.py`. It self-gates on the switches above, so it's
harmless until you arm it.

---

## 4. Go-live sequence (do these in order)

**Step A — dry run.** Keep `ENABLED=1`, `DRY_RUN=1`. Start the runner. Confirm the
log (`execution_log.jsonl`) shows only `KXMLBKS` orders at the sizes you expect
(~your $25 unit). No orders are placed. Let it run a full evening.

**Step B — arm, approval-gated, ONE order.** Set `ENABLED=1`, `DRY_RUN=0`,
`APPROVAL_REQUIRED=1`. When a K edge fires you'll get a Discord "approve?" alert.
**Approve exactly one:**
```
python kalshi_execution.py --list-pending
python kalshi_execution.py --approve <bet_id>
```
The runner's next `process_approved_orders()` places it. Watch: the FILLED alert,
`execution_log.jsonl`, and the position on Kalshi. Confirm size + price are right.

**Step C — run approval-gated for a few weeks.** Approve edges you like; ignore
ones you don't (they expire in 20 min or when the game starts). Compare **realized**
fills/P&L against the paper track.

**Step D — scale / go autonomous (only if C confirms the edge).** To let it place
without you: `APPROVAL_REQUIRED=0`. To size up toward true-Kelly-on-real-bankroll:
raise `LIVE_SIZE_FRACTION` (in the module) gradually. Do these one at a time.

---

## 5. Stop / panic

- **Stop all future orders instantly:** set `KALSHI_EXECUTION_ENABLED=0` (redeploy/restart). Resting orders already on Kalshi are unaffected — cancel those in the Kalshi UI.
- **Pause without disarming:** set `DRY_RUN=1`.
- **Reject a queued order:** `python kalshi_execution.py --reject <bet_id>`.

---

## 6. Monitoring

- **Every fill and every failure** → Discord (`⏳` approval, `✅` fill, `🛑` failure/reject).
- **`execution_log.jsonl`** — full audit trail: intended size, checks, response, fill.
- **`pending_orders.json`** — current approval queue (`--list-pending`).
- Reconcile `execution_log.jsonl` fills against your Kalshi positions periodically.

---

## 7. Standing judgment (why the on-ramp is this careful)

The edge is **confirmed (~95.8% confidence), not proven** — statistical
significance is not proof (Total Bases looked significant and was a phantom). So:
start strikeouts-only, half-Kelly, approval-gated; scale on **realized** P&L, not
on CLV/confidence. Graduated costs a little upside if the edge is real and saves
the account if it isn't.
