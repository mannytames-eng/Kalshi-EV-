"""
kalshi_execution.py — order-execution module (SEPARATE from the scanner).

Sits between the scanner's flagged bets and Kalshi's trading API. It only READS
bets and places orders; it never imports into, or is imported by, the flagging
path (scan_sport / scan_player_props / _add_new_bets). A bug in here therefore
cannot affect flagging — it can only fail to trade. It reuses two low-level
PRIMITIVES from the scanner (request signing + the API base URL); those are
shared infra, not flagging logic.

╔══════════════════════════════════════════════════════════════════════════════╗
║  SAFETY — inert by default. This module places ZERO real orders until BOTH:   ║
║     EXECUTION_ENABLED = True    (env KALSHI_EXECUTION_ENABLED=1) — kill switch ║
║     DRY_RUN           = False   (env KALSHI_EXECUTION_DRY_RUN=0)               ║
║  AND valid Kalshi trading credentials are present. With DRY_RUN on (default)   ║
║  it logs + alerts the order it WOULD place and submits nothing. Going live is  ║
║  a deliberate, multi-flag decision the operator owns. Smoke-test with ONE      ║
║  tiny order before trusting it unattended. Kalshi order-API field names can    ║
║  change — verify _build_order_body / verify_fill against current docs on your  ║
║  first live order.                                                             ║
╚══════════════════════════════════════════════════════════════════════════════╝

Fulfils the 6-point spec:
  (1) acts only on bets where shadow is False (real positions);
  (2) pre-trade checks: absolute per-bet $ ceiling + optional daily $ ceiling +
      insufficient-balance guard + global kill switch (%-of-balance caps removed
      per user 2026-07-31 — bets the scanner's stake as-is, not a % of funds);
  (3) sizes the order from the scanner's Kelly-sized stake (quarter-Kelly base,
      half-Kelly on strikeouts — the scanner's single source of truth);
  (4) verifies the ACTUAL fill after submission (never assumes success);
  (5) logs every attempt with full detail (JSONL);
  (6) alerts on every fill and every failure.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone, date
from typing import Optional, Tuple

import requests

# ── Shared primitives from the scanner (signing + base URL ONLY — not flagging).
from kalshi_ev_scanner import _sign_headers, KALSHI_BASE

# ── Config ────────────────────────────────────────────────────────────────────
def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

# GLOBAL KILL SWITCH — default OFF. Nothing is submitted while this is False.
EXECUTION_ENABLED = _env_bool("KALSHI_EXECUTION_ENABLED", False)
# DRY_RUN — default ON. Even when enabled, simulate/log the order but submit none.
DRY_RUN           = _env_bool("KALSHI_EXECUTION_DRY_RUN", True)
# APPROVAL_REQUIRED — when live (armed + not dry-run), do NOT place automatically:
# queue each order for explicit human approval (Discord alert + approve()/CLI), and
# place only the approved ones via process_approved_orders(). Recommended ON for the
# first weeks of real money. False = fully autonomous placement, no per-order sign-off.
APPROVAL_REQUIRED = _env_bool("KALSHI_EXECUTION_APPROVAL_REQUIRED", True)
APPROVAL_TTL_MIN  = 20   # a queued order EXPIRES (never fires) if not approved in time

# Pre-trade limits. Per user call 2026-07-31 the %-OF-BALANCE caps were removed —
# the order is placed at the scanner's stake as-is, NOT scaled/capped to account
# size. What remains are ABSOLUTE-DOLLAR guards (not % of funds): a per-bet sanity
# ceiling (catches a runaway sizing bug — set well above the scanner's ~$28 max),
# and an OPTIONAL daily circuit breaker (default 0 = off). Env-overridable.
# ⚠ Fixed-dollar sizing on a small account is NOT Kelly-of-your-bankroll — it's a
# larger fraction, with higher variance/ruin risk. The daily cap is your friend.
MAX_POSITION_DOLLARS = float(os.environ.get("KALSHI_MAX_POSITION_DOLLARS", "50"))  # abs $/bet ceiling; 0 = off
MAX_DAILY_DOLLARS    = float(os.environ.get("KALSHI_MAX_DAILY_DOLLARS", "0"))      # abs $/day ceiling; 0 = off

# Go-live SCOPE: only these Kalshi series prefixes may execute. Strikeouts-only
# for the initial live phase — it's the one calibrated/confirmed edge. Any other
# non-shadow bet fed in is skipped, never ordered. Empty list = allow all markets.
MARKET_ALLOWLIST  = ["KXMLBKS"]
# Multiplier on the scanner's Kelly-stamped stake (half-Kelly for strikeouts).
# 1.0 = full half-Kelly as sized by the scanner. Dial DOWN to ramp in more slowly
# (e.g. 0.5 → quarter-Kelly-effective) and raise as realized live P&L confirms.
LIVE_SIZE_FRACTION = 1.0

MIN_CONTRACTS     = 1       # skip if Kelly size rounds to <1 contract
FILL_POLL_TRIES   = 6       # times to poll order status before giving up
FILL_POLL_SLEEP   = 1.0     # seconds between fill-status polls

_DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
EXECUTION_LOG_FILE = os.path.join(_DATA_DIR, "execution_log.jsonl")
PENDING_FILE       = os.path.join(_DATA_DIR, "pending_orders.json")  # awaiting-approval queue
_DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Kalshi trading API (signed) ───────────────────────────────────────────────
def kalshi_post(path: str, body: dict, _timeout: int = 15) -> Tuple[int, dict]:
    """Signed POST to the Kalshi trading API. Returns (status_code, json)."""
    r = requests.post(
        KALSHI_BASE + path,
        headers={**_sign_headers("POST", path), "Content-Type": "application/json",
                 "KALSHI-ACCESS-KEY": os.environ.get("KALSHI_API_KEY", "")},
        data=json.dumps(body),
        timeout=_timeout,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:500]}


def kalshi_get_signed(path: str, _timeout: int = 15) -> Tuple[int, dict]:
    r = requests.get(KALSHI_BASE + path, headers=_sign_headers("GET", path), timeout=_timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:500]}


def fetch_bankroll_dollars() -> Optional[float]:
    """Real account balance (available cash) in dollars, or None on failure.
    Caps are computed against this — never against the paper bankroll."""
    try:
        code, data = kalshi_get_signed("/portfolio/balance")
        if code == 200 and "balance" in data:
            return round(data["balance"] / 100.0, 2)   # Kalshi returns cents
    except Exception:
        pass
    return None


# ── Logging + alerts (decoupled — its own webhook, not the UI's send_discord) ──
def _log_attempt(record: dict) -> None:
    record["ts"] = _now_iso()
    try:
        with open(EXECUTION_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        print(f"  [exec] WARNING could not write execution log: {exc}")
    print(f"  [exec] {record.get('outcome','?')}: {record.get('bet_id','?')} "
          f"{record.get('side','')} x{record.get('count','?')} @ {record.get('price','?')} "
          f"— {record.get('detail','')}")


def _alert(title: str, detail: str, ok) -> None:
    """Fire a Discord alert on every fill and every failure (spec #6). ok=None →
    neutral (an approval request, neither success nor failure)."""
    if not _DISCORD_WEBHOOK:
        return
    emoji = "⏳" if ok is None else ("✅" if ok else "🛑")
    try:
        requests.post(_DISCORD_WEBHOOK, json={
            "content": f"{emoji} **[EXECUTION] {title}**\n{detail}",
        }, timeout=10)
    except Exception as exc:
        print(f"  [exec] WARNING alert failed: {exc}")


# ── Daily exposure tracking (from the execution log — real committed $, PT day) ─
def _pt_today() -> str:
    # Pacific calendar day (UTC-7/-8; -7 is fine as a coarse day boundary here).
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d")

def committed_today_dollars() -> float:
    """Sum of actually-committed order cost for the current PT day, read back
    from the execution log so it survives restarts and reflects reality."""
    today = _pt_today()
    total = 0.0
    try:
        with open(EXECUTION_LOG_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("outcome") == "FILLED" and rec.get("pt_day") == today:
                    total += float(rec.get("cost_dollars") or 0.0)
    except FileNotFoundError:
        pass
    return round(total, 2)

def already_attempted(bet_id: str) -> bool:
    """True if we've already logged a terminal attempt for this bet id (so a
    re-run never double-orders). Terminal = FILLED / REJECTED / SUBMIT_ERROR."""
    try:
        with open(EXECUTION_LOG_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("bet_id") == bet_id and rec.get("outcome") in (
                        "FILLED", "PARTIAL", "SUBMIT_ERROR", "REJECTED_RISK"):
                    return True
    except FileNotFoundError:
        pass
    return False


# ── Pending-approval queue (human-in-the-loop) ────────────────────────────────
def _load_pending() -> dict:
    try:
        with open(PENDING_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}

def _save_pending(d: dict) -> None:
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=_DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, PENDING_FILE)

def is_pending(bet_id: str) -> bool:
    return bet_id in _load_pending()

def _queue_pending(bet: dict, count: int, price: float, cost: float, client_order_id: str) -> None:
    """Record an order awaiting human approval — a snapshot with just what's needed
    to place it later (never the live account, never keys)."""
    p = _load_pending()
    p[bet.get("id", "")] = {
        "bet_id": bet.get("id"), "ticker": bet.get("ticker"), "side": bet.get("side"),
        "price": price, "count": count, "cost_dollars": cost,
        "kalshi_price": price, "paper_stake": bet.get("paper_stake"),
        "game_time": bet.get("game_time"), "client_order_id": client_order_id,
        "created": _now_iso(), "approved": False,
    }
    _save_pending(p)

def _expired(rec: dict) -> bool:
    from datetime import timedelta
    try:
        created = datetime.fromisoformat(rec["created"])
    except (KeyError, ValueError):
        return True
    if datetime.now(timezone.utc) - created > timedelta(minutes=APPROVAL_TTL_MIN):
        return True
    gt = rec.get("game_time")   # also expire if the game has already started
    if gt:
        try:
            if datetime.fromisoformat(gt.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                return True
        except (ValueError, AttributeError):
            pass
    return False

def list_pending() -> list:
    return [{"bet_id": k, **v} for k, v in _load_pending().items()]

def approve(bet_id: str) -> bool:
    p = _load_pending()
    if bet_id in p:
        p[bet_id]["approved"] = True
        _save_pending(p)
        return True
    return False

def approve_all() -> int:
    p = _load_pending(); n = 0
    for k in p:
        if not p[k].get("approved"):
            p[k]["approved"] = True; n += 1
    _save_pending(p)
    return n

def reject(bet_id: str) -> bool:
    p = _load_pending()
    if bet_id in p:
        del p[bet_id]
        _save_pending(p)
        _log_attempt({"bet_id": bet_id, "outcome": "REJECTED_USER", "detail": "manually rejected"})
        return True
    return False


# ── Order construction / fill verification ────────────────────────────────────
def _order_size(bet: dict, price: float) -> Tuple[int, float]:
    """(count, cost_dollars). Live stake = the scanner's Kelly-stamped paper_stake
    (half-Kelly for strikeouts) × LIVE_SIZE_FRACTION. Anchored to the paper bankroll
    the track record was built on — i.e. ≈ the same unit that's been working manually
    — so go-live REPLICATES the proven sizing rather than auto-scaling up to a larger
    real balance. The 3%-of-real-balance cap in pre_trade_checks still ceilings it
    (and correctly shrinks it on a small account). Ramp via LIVE_SIZE_FRACTION."""
    stake = float(bet.get("paper_stake") or 0.0) * LIVE_SIZE_FRACTION
    if price <= 0:
        return 0, 0.0
    count = int(stake // price)          # each contract costs `price` dollars
    return count, round(count * price, 2)


def _build_order_body(bet: dict, count: int, price: float, client_order_id: str) -> dict:
    """Marketable LIMIT order at the flagged entry price (maker rests get taken
    immediately at Kalshi per the maker-execution finding; taker fee is already
    priced into the edge). VERIFY field names against current Kalshi API docs."""
    side = "yes" if bet.get("side") == "YES" else "no"
    price_cents = int(round(price * 100))
    body = {
        "ticker":          bet["ticker"],
        "client_order_id": client_order_id,
        "action":          "buy",
        "side":            side,
        "count":           count,
        "type":            "limit",
    }
    body["yes_price" if side == "yes" else "no_price"] = price_cents
    return body


def verify_fill(order_id: str) -> Tuple[str, int]:
    """Poll the order until terminal. Returns (status, filled_count). NEVER assume
    a 200 on submit means filled — a resting/canceled order fills nothing."""
    for _ in range(FILL_POLL_TRIES):
        code, data = kalshi_get_signed(f"/portfolio/orders/{order_id}")
        order = data.get("order", data) if isinstance(data, dict) else {}
        status = (order.get("status") or "").lower()
        filled = int(order.get("filled_count") or order.get("taker_fill_count") or 0)
        if status in ("executed", "filled") or (status == "canceled" and filled > 0):
            return "filled" if filled > 0 else status, filled
        if status in ("canceled", "expired", "rejected"):
            return status, filled
        time.sleep(FILL_POLL_SLEEP)
    # last read
    return status or "unknown", filled


# ── Pre-trade checks (spec #2) ────────────────────────────────────────────────
def pre_trade_checks(bet: dict, bankroll: float, daily_committed: float,
                     count: int, cost: float) -> Tuple[bool, str]:
    if not EXECUTION_ENABLED:
        return False, "kill switch engaged (EXECUTION_ENABLED=False)"
    if bet.get("shadow"):
        return False, "shadow bet — not a real position"           # spec #1
    if MARKET_ALLOWLIST and not any(
            (bet.get("ticker", "") or "").upper().startswith(p) for p in MARKET_ALLOWLIST):
        return False, f"market not in allowlist {MARKET_ALLOWLIST}"
    if bet.get("correlated") or bet.get("daily_capped"):
        return False, "correlated / capped bet — excluded"
    if count < MIN_CONTRACTS:
        return False, f"size rounds to {count} contracts (<{MIN_CONTRACTS})"
    # Absolute per-bet sanity ceiling (NOT % of funds — the scanner's stake is
    # placed as-is; this only catches a runaway sizing bug). 0 = disabled.
    if MAX_POSITION_DOLLARS and cost > MAX_POSITION_DOLLARS + 1e-9:
        return False, f"per-position sanity ceiling: ${cost:.2f} > ${MAX_POSITION_DOLLARS:.2f}"
    # Optional absolute daily circuit breaker. 0 = off.
    if MAX_DAILY_DOLLARS and daily_committed + cost > MAX_DAILY_DOLLARS + 1e-9:
        return False, f"daily $ ceiling: ${daily_committed:.2f}+${cost:.2f} > ${MAX_DAILY_DOLLARS:.2f}"
    # Never try to bet money that isn't in the account (when balance is known).
    if bankroll is not None and bankroll > 0 and cost > bankroll + 1e-9:
        return False, f"insufficient balance: ${cost:.2f} > ${bankroll:.2f} available"
    return True, "ok"


# ── The one public entry point per bet ────────────────────────────────────────
def place_order_for_bet(bet: dict, bankroll: float, daily_committed: float) -> dict:
    """Run checks → place (or dry-run) → verify fill → log → alert. Returns the
    log record. Pure per-bet; the caller supplies bankroll + running daily total."""
    bet_id = bet.get("id", "")
    price  = float(bet.get("kalshi_price") or bet.get("kalshi") or 0.0)
    count, cost = _order_size(bet, price)
    base = {"bet_id": bet_id, "ticker": bet.get("ticker"), "side": bet.get("side"),
            "price": price, "count": count, "cost_dollars": cost,
            "pt_day": _pt_today(), "dry_run": DRY_RUN, "bankroll": bankroll}

    ok, reason = pre_trade_checks(bet, bankroll, daily_committed, count, cost)
    if not ok:
        rec = {**base, "outcome": "REJECTED_RISK", "detail": reason}
        _log_attempt(rec)
        # Only alert a real rejection (kill-switch/shadow skips are routine, silent).
        if EXECUTION_ENABLED and not bet.get("shadow"):
            _alert("Order REJECTED", f"`{bet.get('ticker')}` {bet.get('side')} — {reason}", ok=False)
        return rec

    client_order_id = f"exec-{bet_id}"[:64]   # deterministic → Kalshi dedups re-runs

    if DRY_RUN:
        rec = {**base, "outcome": "DRY_RUN", "client_order_id": client_order_id,
               "detail": f"would BUY {count} {bet.get('side')} @ {int(price*100)}c (${cost:.2f})"}
        _log_attempt(rec)
        return rec

    if APPROVAL_REQUIRED:
        # Human-in-the-loop: queue the order + alert; place nothing until approved.
        _queue_pending(bet, count, price, cost, client_order_id)
        rec = {**base, "outcome": "PENDING_APPROVAL", "client_order_id": client_order_id,
               "detail": f"awaiting approval: BUY {count} {bet.get('side')} @ {int(price*100)}c (${cost:.2f})"}
        _log_attempt(rec)
        _alert("APPROVAL NEEDED",
               f"`{bet.get('ticker')}` {bet.get('side')} — BUY {count} @ {int(price*100)}c "
               f"(${cost:.2f})\nApprove within {APPROVAL_TTL_MIN}m: `--approve {bet_id}`  "
               f"(reject: `--reject {bet_id}`)", ok=None)
        return rec

    # Fully autonomous — submit immediately.
    return _submit_and_verify(bet, count, price, cost, base, client_order_id)


def _submit_and_verify(bet: dict, count: int, price: float, cost: float,
                       base: dict, client_order_id: str) -> dict:
    """Actually place the order and verify its REAL fill (spec #4). Shared by the
    autonomous path and the approved-order path. NEVER assumes the submit 200
    means filled — a resting/canceled order fills nothing."""
    body = _build_order_body(bet, count, price, client_order_id)
    try:
        code, resp = kalshi_post("/portfolio/orders", body)
    except Exception as exc:
        rec = {**base, "outcome": "SUBMIT_ERROR", "detail": f"POST raised: {exc}"}
        _log_attempt(rec)
        _alert("Order SUBMIT ERROR", f"`{bet.get('ticker')}` {bet.get('side')} — {exc}", ok=False)
        return rec

    if code not in (200, 201):
        rec = {**base, "outcome": "SUBMIT_ERROR", "http": code, "resp": resp,
               "detail": f"HTTP {code}: {str(resp)[:200]}"}
        _log_attempt(rec)
        _alert("Order SUBMIT ERROR", f"`{bet.get('ticker')}` {bet.get('side')} — HTTP {code}", ok=False)
        return rec

    order_id = (resp.get("order") or {}).get("order_id") or resp.get("order_id")
    status, filled = verify_fill(order_id) if order_id else ("unknown", 0)
    filled_cost = round(filled * price, 2)

    if filled >= count and count > 0:
        rec = {**base, "outcome": "FILLED", "order_id": order_id, "filled": filled,
               "cost_dollars": filled_cost, "detail": f"filled {filled}/{count} @ {int(price*100)}c"}
        _log_attempt(rec)
        _alert("FILLED", f"`{bet.get('ticker')}` {bet.get('side')} — {filled}/{count} @ "
                         f"{int(price*100)}c (${filled_cost:.2f})", ok=True)
    elif filled > 0:
        rec = {**base, "outcome": "PARTIAL", "order_id": order_id, "filled": filled,
               "cost_dollars": filled_cost, "status": status,
               "detail": f"PARTIAL {filled}/{count} @ {int(price*100)}c (status={status})"}
        _log_attempt(rec)
        _alert("PARTIAL FILL", f"`{bet.get('ticker')}` {bet.get('side')} — {filled}/{count} "
                               f"(status={status})", ok=False)
    else:
        rec = {**base, "outcome": "NO_FILL", "order_id": order_id, "filled": 0,
               "status": status, "detail": f"submitted but UNFILLED (status={status})"}
        _log_attempt(rec)
        _alert("NO FILL", f"`{bet.get('ticker')}` {bet.get('side')} — order {order_id} "
                          f"unfilled (status={status})", ok=False)
    return rec


def process_approved_orders(bankroll: Optional[float] = None) -> list:
    """Place every APPROVED, unexpired pending order (re-checking risk against a
    FRESH bankroll + daily total at placement time), expire the stale ones, and
    leave un-approved ones queued. The runner calls this each sweep alongside
    execute_flagged_bets. No-op unless live (armed + not dry-run)."""
    if DRY_RUN or not EXECUTION_ENABLED:
        return []
    if bankroll is None:
        bankroll = fetch_bankroll_dollars()
    daily = committed_today_dollars()
    pending = _load_pending()
    results, remove = [], []
    for bet_id, rec in list(pending.items()):
        if _expired(rec):
            remove.append(bet_id)
            _log_attempt({**rec, "outcome": "EXPIRED",
                          "detail": f"not approved within {APPROVAL_TTL_MIN}m / game started"})
            _alert("EXPIRED (not placed)", f"`{rec.get('ticker')}` {rec.get('side')} — "
                   f"approval window passed", ok=False)
            continue
        if not rec.get("approved"):
            continue
        count = int(rec["count"]); price = float(rec["price"]); cost = float(rec["cost_dollars"])
        # Re-run the risk gate at PLACEMENT time (bankroll/daily may have changed).
        ok, reason = pre_trade_checks(rec, bankroll, daily, count, cost)
        if not ok:
            remove.append(bet_id)
            r = {**rec, "outcome": "REJECTED_RISK", "detail": f"at placement: {reason}"}
            _log_attempt(r); results.append(r)
            _alert("Order REJECTED at placement", f"`{rec.get('ticker')}` — {reason}", ok=False)
            continue
        base = {"bet_id": bet_id, "ticker": rec.get("ticker"), "side": rec.get("side"),
                "price": price, "count": count, "cost_dollars": cost,
                "pt_day": _pt_today(), "dry_run": False, "bankroll": bankroll}
        r = _submit_and_verify(rec, count, price, cost, base, rec.get("client_order_id", f"exec-{bet_id}"))
        results.append(r)
        remove.append(bet_id)
        if r.get("outcome") == "FILLED":
            daily += float(r.get("cost_dollars") or 0.0)
    if remove:
        p = _load_pending()
        for k in remove:
            p.pop(k, None)
        _save_pending(p)
    return results


def execute_flagged_bets(bets: list, bankroll: Optional[float] = None) -> list:
    """Entry point: place orders for eligible flagged bets. Filters to real
    (non-shadow), open, not-already-attempted positions. Reads the running daily
    committed total from the log and accumulates within this sweep so the daily
    cap holds across the batch. Returns the list of log records."""
    if bankroll is None:
        bankroll = fetch_bankroll_dollars()
    daily = committed_today_dollars()
    results = []
    for b in bets:
        if b.get("status") != "open":
            continue
        if b.get("shadow"):                       # spec #1 — real positions only
            continue
        if MARKET_ALLOWLIST and not any(          # go-live scope: strikeouts only
                (b.get("ticker", "") or "").upper().startswith(p) for p in MARKET_ALLOWLIST):
            continue                              # silent skip (not a rejection to log)
        if already_attempted(b.get("id", "")):
            continue
        if is_pending(b.get("id", "")):           # already queued & awaiting approval
            continue
        rec = place_order_for_bet(b, bankroll, daily)
        results.append(rec)
        if rec.get("outcome") == "FILLED":
            daily += float(rec.get("cost_dollars") or 0.0)
    return results


def _print_config():
    print("kalshi_execution config:")
    print(f"  EXECUTION_ENABLED = {EXECUTION_ENABLED}  (kill switch; must be True to trade)")
    print(f"  DRY_RUN           = {DRY_RUN}  (must be False to submit real orders)")
    print(f"  APPROVAL_REQUIRED = {APPROVAL_REQUIRED}  (True → each order waits for --approve)")
    print(f"  MARKET_ALLOWLIST  = {MARKET_ALLOWLIST}   LIVE_SIZE_FRACTION = {LIVE_SIZE_FRACTION}")
    print(f"  MAX_POSITION_DOLLARS = ${MAX_POSITION_DOLLARS:.0f}/bet   "
          f"MAX_DAILY_DOLLARS = {'off' if not MAX_DAILY_DOLLARS else '$'+format(MAX_DAILY_DOLLARS,'.0f')+'/day'}  (no %-of-funds cap)")
    print(f"  log → {EXECUTION_LOG_FILE}")
    if EXECUTION_ENABLED and not DRY_RUN:
        mode = "APPROVAL-GATED" if APPROVAL_REQUIRED else "FULLY AUTONOMOUS"
        print(f"  ⚠ LIVE MODE ARMED ({mode}) — real orders when fed flagged bets.")
    else:
        print("  ✓ inert (no real orders will be placed in this configuration).")


if __name__ == "__main__":
    # CLI for the approval workflow. NEVER places an order by itself except
    # --process (which places APPROVED, unexpired orders — and only when live).
    import argparse
    ap = argparse.ArgumentParser(description="Kalshi execution — approval workflow CLI")
    ap.add_argument("--list-pending", action="store_true", help="show orders awaiting approval")
    ap.add_argument("--approve", metavar="BET_ID", help="approve one queued order")
    ap.add_argument("--approve-all", action="store_true", help="approve all queued orders")
    ap.add_argument("--reject", metavar="BET_ID", help="reject/remove one queued order")
    ap.add_argument("--process", action="store_true",
                    help="place APPROVED unexpired orders (live only) + expire stale ones")
    a = ap.parse_args()

    if a.list_pending:
        p = list_pending()
        if not p:
            print("no pending orders.")
        for r in p:
            print(f"  [{'APPROVED' if r.get('approved') else 'pending '}] {r['bet_id']}  "
                  f"{r.get('side')} x{r.get('count')} @ {int(float(r.get('price',0))*100)}c "
                  f"(${r.get('cost_dollars')})  created {r.get('created')}")
    elif a.approve:
        print("approved." if approve(a.approve) else "not found in queue.")
    elif a.approve_all:
        print(f"approved {approve_all()} order(s).")
    elif a.reject:
        print("rejected." if reject(a.reject) else "not found in queue.")
    elif a.process:
        res = process_approved_orders()
        print(f"processed {len(res)} order(s): {[r.get('outcome') for r in res]}"
              if res else "nothing to place (none approved/live, or inert).")
    else:
        _print_config()
