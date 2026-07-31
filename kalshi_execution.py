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
  (2) pre-trade checks: 3% per-position cap, 15% daily cap, global kill switch;
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

# Hard pre-trade risk limits (independent safety net on TOP of the scanner's
# Kelly sizing — a circuit breaker, not a resizer; an order that breaches these
# is REJECTED, not shrunk, because a breach means something upstream is wrong).
MAX_POSITION_FRAC = 0.03    # ≤3% of bankroll per single position
MAX_DAILY_FRAC    = 0.15    # ≤15% of bankroll committed across a calendar day (PT)

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

EXECUTION_LOG_FILE = os.path.join(
    os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__))),
    "execution_log.jsonl",
)
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


def _alert(title: str, detail: str, ok: bool) -> None:
    """Fire a Discord alert on every fill and every failure (spec #6)."""
    if not _DISCORD_WEBHOOK:
        return
    emoji = "✅" if ok else "🛑"
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
    if bankroll is None or bankroll <= 0:
        return False, "bankroll unavailable — refusing to size against unknown balance"
    if cost > MAX_POSITION_FRAC * bankroll + 1e-9:
        return False, (f"per-position cap: ${cost:.2f} > "
                       f"{MAX_POSITION_FRAC:.0%} of ${bankroll:.2f}")
    if daily_committed + cost > MAX_DAILY_FRAC * bankroll + 1e-9:
        return False, (f"daily cap: ${daily_committed:.2f}+${cost:.2f} > "
                       f"{MAX_DAILY_FRAC:.0%} of ${bankroll:.2f}")
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

    # ── LIVE submit ──────────────────────────────────────────────────────────
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

    # ── Verify the ACTUAL fill (spec #4) — never assume the 200 filled it ─────
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
        rec = place_order_for_bet(b, bankroll, daily)
        results.append(rec)
        if rec.get("outcome") == "FILLED":
            daily += float(rec.get("cost_dollars") or 0.0)
    return results


if __name__ == "__main__":
    # Self-check only — prints config, never trades. Real use: import
    # execute_flagged_bets and feed it the scanner's flagged bets.
    print("kalshi_execution config:")
    print(f"  EXECUTION_ENABLED = {EXECUTION_ENABLED}  (kill switch; must be True to trade)")
    print(f"  DRY_RUN           = {DRY_RUN}  (must be False to submit real orders)")
    print(f"  MARKET_ALLOWLIST  = {MARKET_ALLOWLIST}   LIVE_SIZE_FRACTION = {LIVE_SIZE_FRACTION}")
    print(f"  MAX_POSITION_FRAC = {MAX_POSITION_FRAC:.0%}   MAX_DAILY_FRAC = {MAX_DAILY_FRAC:.0%}")
    print(f"  log → {EXECUTION_LOG_FILE}")
    if EXECUTION_ENABLED and not DRY_RUN:
        print("  ⚠ LIVE MODE ARMED — this will place REAL orders when fed flagged bets.")
    else:
        print("  ✓ inert (no real orders will be placed in this configuration).")
