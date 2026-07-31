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

Hard limits always on: **strikeouts only** (`KXMLBKS`), the scanner's stake
placed **as-is** (no %-of-funds cap — user call 2026-07-31), an absolute per-bet
**$ ceiling** (`KALSHI_MAX_POSITION_DOLLARS`, default $50) + an absolute **daily
$ circuit breaker** (`KALSHI_MAX_DAILY_DOLLARS`, default **$200/day**) + an
insufficient-balance guard, fill verification, full logging, Discord alert on
every fill/failure.

> ⚠ Fixed-dollar sizing on a sub-$1000 account is **not** Kelly-of-your-bankroll
> — a $12–28 bet is a large fraction of a small balance, so variance and ruin
> risk are higher than the paper track implies. The **$200/day** ceiling is your
> circuit breaker; lower it if you fund light.

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

## 3. The runner (`run_executor.py` — the piece that makes it act "by itself")

`run_executor.py` is committed. It reads the flagged bets, calls
`execute_flagged_bets` + `process_approved_orders` every `EXECUTOR_INTERVAL_SEC`
(default 60), and self-gates on the switches — inert until armed. Run it as its
**own process, separate from the scanner** (a bug here still can't touch flagging).

Locally: `python run_executor.py` (set `BETS_FILE=./ev_bets.json`).

### Deploy as a separate Railway worker

1. **New service** in the same Railway project, same GitHub repo (`main`).
2. **Point it at the worker config:** Settings → Config → set the Railway config
   file to **`railway.worker.toml`** (committed). That sets the start command to
   `python3 run_executor.py` and its own `/data` volume. ⚠ If you skip this, the
   service inherits `railway.toml` and boots a **second scanner**, not the worker.
3. **Attach a Volume** to this service and set `DATA_DIR` to its mount path (e.g.
   `/data`). The worker is a *separate* service, so it does **not** share the
   scanner's volume — it needs its own for `execution_log.jsonl` (audit +
   daily-total tracking) and `pending_orders.json` (approval queue) to survive
   restarts. Without a volume those are ephemeral: the daily-$ tracker resets on
   restart and pending approvals are lost.
4. **Bets source — use HTTP, not the file.** Since the worker can't see the
   scanner's `/data/ev_bets.json`, set:
   ```
   BETS_URL = https://evscanner-production.up.railway.app/api/today_edges
   ```
   (That endpoint returns the open positions with `paper_stake`, `shadow`,
   `ticker`, `side`, `id` — everything the executor needs.)
5. **Env vars on the worker service:**
   ```
   KALSHI_API_KEY        = <trading key ID>          # same key as the scanner
   KALSHI_PRIVATE_KEY    = <raw PEM>                 # (or KALSHI_PRIVKEY_B64)
   DISCORD_WEBHOOK       = <your webhook>            # fill/approval alerts
   BETS_URL              = https://evscanner-production.up.railway.app/api/today_edges
   DATA_DIR              = /data                     # the attached volume mount

   # switches — START INERT, arm later per the go-live sequence
   KALSHI_EXECUTION_ENABLED           = 0            # kill switch (0 = off)
   KALSHI_EXECUTION_DRY_RUN           = 1            # 1 = simulate only
   KALSHI_EXECUTION_APPROVAL_REQUIRED = 1            # 1 = approve each order

   # risk (absolute $, not % of funds)
   KALSHI_MAX_POSITION_DOLLARS = 50                  # per-bet ceiling (bug guard)
   KALSHI_MAX_DAILY_DOLLARS    = 200                 # daily circuit breaker
   EXECUTOR_INTERVAL_SEC       = 60
   ```
6. Deploy. Logs should print `[runner] start … NOTE: inert config …` until armed.

> **Approving orders on a Railway worker** (approval-gated phase): the approve
> CLI must run in the worker's environment (same volume/pending file):
> `railway run --service <worker> python kalshi_execution.py --approve <bet_id>`
> (or use the service's shell). Fully autonomous mode (`APPROVAL_REQUIRED=0`)
> needs no approvals.

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
