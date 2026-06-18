#!/usr/bin/env python3
"""
Kalshi EV Scanner — Web UI
Run:  python3 kalshi_ev_ui.py
Open: http://localhost:8000
"""

import json
import os
import sys
import traceback
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import List, Optional

from dotenv import load_dotenv

# ── Railway-only guard ────────────────────────────────────────────────────────
# All scanning resources must go to Railway. Running locally double-bills
# Odds API credits from the same key and pollutes the paper portfolio.
# Set ALLOW_LOCAL_RUN=1 only for one-off debugging sessions.
if not os.environ.get("RAILWAY_ENVIRONMENT") and not os.environ.get("ALLOW_LOCAL_RUN"):
    print("=" * 60)
    print("  BLOCKED: This scanner is Railway-only.")
    print("  Running locally wastes Odds API credits.")
    print("  To override for debugging: ALLOW_LOCAL_RUN=1 python3 kalshi_ev_ui.py")
    print("=" * 60)
    sys.exit(1)

# ── Portable base directory — works on Mac and any Linux VPS ─────────────────
# On Railway a persistent volume is mounted at /data — use it for all data files
# so they survive service restarts/redeploys.  Falls back to script directory locally.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_RAILWAY_DATA = "/data"
BASE_DIR  = _RAILWAY_DATA if os.path.isdir(_RAILWAY_DATA) and os.environ.get("RAILWAY_ENVIRONMENT") else _SCRIPT_DIR
DATA_DIR  = BASE_DIR   # data files (bets, history, keys) — persistent on Railway
print(f"  [startup] DATA_DIR={DATA_DIR}  RAILWAY_ENVIRONMENT={os.environ.get('RAILWAY_ENVIRONMENT','not set')}")
print(f"  [startup] /data exists={os.path.isdir(_RAILWAY_DATA)}")
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))   # .env lives next to the script always

# ── Import scanner logic ──────────────────────────────────────────────────────
sys.path.insert(0, BASE_DIR)
from kalshi_ev_scanner import (
    scan_sport,
    scan_player_props,
    kalshi_get,
    fetch_game_scores,
    fetch_odds_index,
    validate_bet,
    MLB_SPREAD_STD, MLB_TOTAL_STD,
    NBA_SPREAD_STD, NBA_TOTAL_STD,
    MLB_ABBR, NBA_ABBR,
    EDGE_THRESHOLD,
    _parse_ticker_start_time,
    _parse_ticker_date,
)

PORT = int(os.environ.get("PORT", 8000))   # Railway injects PORT; falls back to 8000 locally
# ── Refresh cadence (credit budget) ──────────────────────────────────────────
# Two independent loops:
#   ODDS_REFRESH  : fetches Pinnacle/DK/FD lines  — costs 2 credits (MLB + NBA)
#   KALSHI_REFRESH: re-scans Kalshi prices only    — costs 0 credits (uses cache)
#
# Monthly credit math (20k budget):
#   Odds refresh : 2 credits × 48/day × 30 days  =  2,880
#   Props scan   : 10 games  ×  8/day × 30 days  =  2,400
#   Kalshi scans : 0 credits (cached odds)        =      0
#   ─────────────────────────────────────────────────────
#   Total                                         =  5,280  (74% under budget)
# Odds refresh is adaptive — faster during game hours, slow overnight.
# MLB games run roughly noon–midnight ET. Pinnacle lines move most in
# the 2–3 hrs before first pitch, so that window gets the tightest refresh.
# Credit budget: ~168 refreshes/day × 2 credits = ~336/day = ~10,080/month
#   • Pre-game peak  (11 AM–10 PM ET, 11h): every 4 min  → 165 refreshes
#   • Overnight      (10 PM–11 AM ET, 13h): every 20 min →  39 refreshes
#   Total: ~204/day × 2 = 408 credits/day = ~12,240/month (well under 20k)
def _et_offset_hours() -> int:
    """Return the current US Eastern offset from UTC as a positive integer.
    EDT (UTC-4): April 1 – October 31  (covers entire MLB/NBA season)
    EST (UTC-5): November 1 – March 31
    Uses the same rule as _parse_ticker_start_time so ET conversions are always consistent.
    """
    return 4 if 4 <= datetime.now(timezone.utc).month <= 10 else 5

def _et_hour() -> int:
    """Return the current hour in US Eastern Time (0–23)."""
    from datetime import timedelta as _td
    return (datetime.now(timezone.utc) - _td(hours=_et_offset_hours())).hour

def _pdt_hour() -> int:
    """Return the current hour in US Pacific Daylight Time (PDT = UTC-7).
    PDT is active April–October, covering the full MLB season.
    Used for all scheduling windows — keeps scanner aligned with West Coast game times.
    """
    from datetime import timedelta as _td
    return (datetime.now(timezone.utc) - _td(hours=7)).hour

def _all_games_commenced() -> bool:
    """Return True when all MLB games for the current slate have commenced.

    Uses Pinnacle's game count as a proxy: once all games go live Pinnacle
    drops them from the upcoming-odds feed, pushing _odds_game_count to 0.

    IMPORTANT: uses _last_odds_cache_success (not _last_odds_refresh) so
    this only activates after at least one confirmed successful Pinnacle fetch.
    Using _last_odds_refresh would false-trigger on boot if the first fetch
    fails or returns 0 games, locking the scanner into 15-min Sleep intervals.
    """
    with _odds_cache_lock:
        return _last_odds_cache_success > 0 and _odds_game_count == 0

def _odds_refresh_interval() -> int:
    """Return seconds until next Pinnacle odds refresh (PDT context-aware schedule).

    Credit costs (MLB only — 4 markets per call):
      MLB odds call: 4 credits (spreads+totals+alternate_spreads+alternate_totals)

    Operating windows (PDT = UTC-7):
      Early Morning 06:00–09:00 PDT (3h): 90s  — overnight line gaps, Kalshi slow to reprice
      Discovery     09:00–13:00 PDT (4h):  3min — morning line-setting, lower cadence
      Peak Trading  13:00–22:00 PDT (9h): 75s  — live game hours, fast line capture
      Sleep         22:00–06:00 PDT (8h): 15min — overnight, minimal market movement

    Game-slate short-circuit: if all MLB games have commenced during
    Peak Trading, automatically drops to Sleep rates to conserve credits.

    Odds credit budget:
      Early Morning (40/hr ×  3h × 4):   480/day
      Discovery     (20/hr ×  4h × 4):   320/day
      Peak Trading  (48/hr ×  9h × 4): 1,728/day
      Sleep          (4/hr ×  8h × 4):   128/day
      Total odds: ~2,656/day
    """
    h = _pdt_hour()
    if 13 <= h < 22 and _all_games_commenced():
        return 15 * 60       # Slate over — drop to Sleep rates early
    if 6  <= h <  9:
        return 90            # Early Morning: 90s — best window for overnight line gaps
    if 9  <= h < 13:
        return  3 * 60       # Discovery: 3min
    if 13 <= h < 22:
        return 75            # Peak Trading: 75s
    return 15 * 60           # Sleep: 15min

def _props_refresh_interval() -> int:
    """Return seconds between MLB props scans (PDT context-aware schedule).

    Discovery    09:00–13:00 PDT: 10min — pre-game lines forming
    Peak Trading 13:00–22:00 PDT:  8min — lineups locked, scratches happen here
    Early Morning 06:00–09:00 PDT: 15min — pitcher scratches + overnight line gaps
    Sleep         22:00–06:00 PDT:  OFF  — no upcoming games, save credits

    Game-slate short-circuit: mirrors odds logic — props off when slate over.

    Rationale: props markets (pitcher strikeouts, batter hits) are less liquid
    than totals — Kalshi can lag Pinnacle by 15-30min after a lineup change or
    pitcher scratch.  Scanning 3× more often catches those windows.

    Props credit budget (~21 credits/scan: 1 event list + 10 games × 2 markets):
      Early Morning (4/hr  ×  3h × 21):  252/day
      Discovery     (6/hr  ×  4h × 21):  504/day  (was 7.5/hr at 10min)
      Peak Trading  (7.5/hr × 9h × 21): 1,418/day
      Sleep:                                0/day
      Total props: ~2,174/day
    """
    h = _pdt_hour()
    if 13 <= h < 22 and _all_games_commenced():
        return 10 ** 9       # Slate over — props off
    if 6  <= h <  9:
        return 15 * 60       # Early Morning: 15min — starter scratches post overnight
    if 9  <= h < 13:
        return 10 * 60       # Discovery: 10min
    if 13 <= h < 22:
        return  8 * 60       # Peak Trading: 8min
    return 10 ** 9           # Sleep: OFF
REFRESH_SECONDS       = 30         # re-scan Kalshi every 30 sec   (0 credits)
# Monthly credit math (20k budget):
#   Odds refresh : 2 × 144/day × 30 =  8,640
#   Props scan   : 10 × 8/day × 30  =  2,400
#   Kalshi scans : 0 credits (cached) =     0
#   ─────────────────────────────────────────
#   Total                            = 11,040  (45% under 20k budget)
HISTORY_FILE    = os.path.join(DATA_DIR, "ev_history.json")
BETS_FILE       = os.path.join(DATA_DIR, "ev_bets.json")
MY_BETS_FILE    = os.path.join(DATA_DIR, "my_bets.json")
PIN_PRICES_FILE = os.path.join(DATA_DIR, "ev_pin_prices.json")
MAX_HISTORY     = 500       # cap stored scan snapshots
PERF_BANKROLL        = 1000.0    # bankroll for ROI % display

# ── Paper trading portfolio ────────────────────────────────────────────────────
PAPER_START_BALANCE  = 1000.0    # starting virtual bankroll
PAPER_START_DATE     = "2026-06-08"  # V2.0 reset — pre-throttle + Jun 7 bad-pipeline bets archived
PAPER_KELLY_FRACTION = 0.25     # quarter-Kelly base fraction
PAPER_KELLY_CAP      = 0.03     # max 3% of current balance per bet (validation-phase cap)

# ── Shadow markets ─────────────────────────────────────────────────────────────
# Markets listed here are fully tracked (logged, CLV captured, win/loss recorded)
# but staked at $0 and excluded from paper balance, Kelly P&L, and summary pills.
# Move a ticker prefix here to validate a new market before committing real stakes.
SHADOW_MARKETS: list[str] = [
    "KXMLBHR",   # Home runs — new market, accumulating CLV data before going live
]

def _is_shadow(ticker: str) -> bool:
    return any(ticker.upper().startswith(s) for s in SHADOW_MARKETS)

# ── Time-to-matchup Kelly multipliers ────────────────────────────────────────
# Lines are softest and liquidity thinnest far from first pitch; scale down
# early entries and trust the full edge only when the market has settled.
#   > 12 h  →  0.25× (overnight / speculative lines)
#   4–12 h  →  0.50× (mid-day discovery window)
#   < 4 h   →  1.00× (peak liquidity — full edge)
def _time_kelly_mult(game_time_iso: str | None) -> float:
    """Return the time-to-matchup Kelly multiplier for a given game_time ISO string.

    Calibrated from 71 settled bets (Jun 2026):
      4-12h window: +15.3pp delta, +9.34 flat units → full Kelly
      12-24h window: +1.7pp delta, +0.52 flat units → mild discount
      <4h window:  -4.8pp delta, -2.78 flat units → penalise (peak-hour noise)
      24h+: too few bets to trust → conservative
    """
    if not game_time_iso:
        return 0.75   # unknown game time — treat as mid-range
    try:
        gt = datetime.fromisoformat(game_time_iso.replace("Z", "+00:00"))
        hours_until = (gt - datetime.now(timezone.utc)).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 0.75
    if hours_until > 24:
        return 0.25   # too far out — very few data points
    if hours_until > 12:
        return 0.75   # 12-24h — neutral performance, mild discount
    if hours_until >= 4:
        return 1.00   # 4-12h — best performing window, full Kelly
    return 0.50       # <4h — worst performers (peak-hour noise), penalise

# ── Shared state (updated by background thread) ───────────────────────────────
_lock    = threading.Lock()
_state   = {
    "edges":           [],
    "edges_cache":     [],     # last non-empty scan result — served during off-peak/sleep windows
    "last_scan":       None,   # ISO string
    "scanning":        False,
    "error":           None,
    "last_scan_stats": None,   # diagnostic counters from last scan_sport call
    "market_snapshot": {},     # {ticker|side: {adj_edge, kalshi, fair, edge_pct}} — all scanned markets
}

# Tracks first-seen and last-seen YES price for each edge key (for staleness + CLV)
# Persisted to PIN_PRICES_FILE so Pinnacle prices survive server restarts.
_edge_history_lock = threading.Lock()

def _load_pin_prices() -> dict:
    try:
        with open(PIN_PRICES_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_pin_prices():
    try:
        import tempfile as _tempfile
        with _edge_history_lock:
            snapshot = dict(_edge_price_history)
        fd, tmp = _tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, PIN_PRICES_FILE)
    except Exception as exc:
        print(f"  WARNING: could not save pin prices: {exc}")

_edge_price_history: dict = _load_pin_prices()
print(f"  Loaded {len(_edge_price_history)} Pinnacle price entries from disk")


# ── Cached book-odds indices (populated by slow 30-min thread) ────────────────
# The fast 2-min Kalshi scan reads these without spending Odds API credits.
# None = not yet fetched; scan_sport falls back to inline fetch on first run.
_odds_cache_lock  = threading.Lock()
_cached_mlb_index: Optional[dict] = None
_cached_nba_index: Optional[dict] = None
_last_odds_refresh: float        = 0.0   # epoch seconds of last ATTEMPT (success or fail)
_last_odds_cache_success: float  = 0.0   # epoch seconds of last SUCCESSFUL index population
_odds_game_count: int            = 0     # number of Pinnacle matchups in the cached index

# ── validate_bet result cache (saves 1 credit per click) ─────────────────────
# validate_bet() calls fetch_book_odds() — 1 Odds API credit per call.
# Cache per (ticker, side) for 5 minutes so repeated clicks don't burn credits.
_validate_cache: dict = {}          # {(ticker, side): (result_dict, expire_epoch)}
_VALIDATE_CACHE_TTL = 5 * 60        # 5 minutes

# ── History (persisted to HISTORY_FILE) ──────────────────────────────────────
def _load_history() -> list:
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def _save_history(history: list):
    try:
        import tempfile, os as _os
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with _os.fdopen(fd, "w") as f:
            json.dump(history[-MAX_HISTORY:], f)
        _os.replace(tmp, HISTORY_FILE)
    except Exception as exc:
        print(f"  WARNING: could not save history: {exc}")

_history = _load_history()

# ── Bet tracker (persisted to BETS_FILE) ──────────────────────────────────────
def _load_bets() -> list:
    try:
        with open(BETS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # ── Volume migration: seed from repo copy on first boot ───────────────
        # If DATA_DIR ≠ _SCRIPT_DIR (i.e. /data volume just mounted and empty),
        # copy the repo's ev_bets.json into the volume so history is preserved.
        _seed_path = os.path.join(_SCRIPT_DIR, "ev_bets.json")
        if DATA_DIR != _SCRIPT_DIR and os.path.exists(_seed_path):
            try:
                with open(_seed_path, "r") as _sf:
                    bets = json.load(_sf)
                _save_bets(bets)
                print(f"  Volume seed: copied {len(bets)} bets from repo → {BETS_FILE}")
                return bets
            except Exception as _seed_exc:
                print(f"  Volume seed failed: {_seed_exc}")
        # ── EV_BETS_SEED env var fallback (base64-encoded JSON) ───────────────
        seed = os.environ.get("EV_BETS_SEED")
        if seed:
            try:
                import base64 as _b64, gzip as _gz
                raw = _b64.b64decode(seed)
                # Support both gzip+b64 and plain b64
                try:
                    bets = json.loads(_gz.decompress(raw).decode())
                except Exception:
                    bets = json.loads(raw.decode())
                _save_bets(bets)
                return bets
            except Exception:
                pass
        return []

def _save_bets(bets: list) -> bool:
    """Save bets to disk. Returns True on success, False on failure."""
    try:
        import tempfile, os as _os
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with _os.fdopen(fd, "w") as f:
            json.dump(bets, f, indent=2)
        _os.replace(tmp, BETS_FILE)
        return True
    except Exception as exc:
        print(f"  WARNING: could not save bets to {BETS_FILE}: {exc}")
        # Fire a Discord alert once per session — silent disk failure = data loss on next restart
        global _save_bets_alerted
        if not _save_bets_alerted:
            _save_bets_alerted = True
            try:
                send_discord(None, f"🚨 **Bet save failure** — `{exc}`\nPath: `{BETS_FILE}` | DATA_DIR: `{DATA_DIR}`\nBets are in memory but NOT persisting. Check Railway volume mount.")
            except Exception:
                pass
        return False

_bets: list = _load_bets()
print(f"  Bet store loaded: {len(_bets)} bets from {BETS_FILE}")
_bets_lock = threading.RLock()   # reentrant — _add_new_bets holds this while calling _paper_kelly_stake → _compute_paper_balance

# --- One-time backfill: ensure every bet has closing_yes_pct, closing_pin_pct, clv, clv_source ---
_backfilled = False
for _b in _bets:
    if _b.get("closing_yes_pct") is None and _b.get("kalshi_yes_at_flag") is not None:
        _b["closing_yes_pct"] = _b["kalshi_yes_at_flag"]
        _backfilled = True
    if _b.get("clv") is None:
        _b["clv"] = 0.0
        _backfilled = True
    if "closing_pin_pct" not in _b:
        _b["closing_pin_pct"] = None
        _backfilled = True
    if "clv_source" not in _b:
        # Infer source from existing data
        if _b.get("closing_pin_pct") is not None:
            _b["clv_source"] = "pin"
        elif _b.get("clv", 0.0) != 0.0:
            _b["clv_source"] = "kalshi"
        else:
            _b["clv_source"] = "none"
        _backfilled = True
    # Upgrade "none" bets that have pin_prob_at_flag: use it as the best available
    # Pinnacle proxy (better than Kalshi drift or missing data).
    if _b.get("clv_source") == "none" and _b.get("pin_prob_at_flag") is not None:
        entry_k = (_b.get("kalshi_price") or 0) * 100
        if entry_k:
            _b["clv"] = round(_b["pin_prob_at_flag"] - entry_k, 1)
            _b["clv_source"] = "pin_entry"
            _b["closing_pin_pct"] = _b["pin_prob_at_flag"]
            _backfilled = True
if _backfilled:
    _save_bets(_bets)
    print(f"  Backfilled CLV fields on {len(_bets)} bet(s)")

# --- One-time data corrections for known bad values ---
_data_fixed = False

# DET@ORL Game 4: bet was pre-game (flagged 5:11 PM ET, tipoff ~7:30 PM ET).
# Was incorrectly purged. NO won 182 vs 214.5. Restore to Railway volume if missing.
_detorl_id = "KXNBATOTAL-26APR27DETORL-214|NO"
if not any(b.get("id") == _detorl_id for b in _bets):
    _bets.append({
        "id": _detorl_id,
        "ticker": "KXNBATOTAL-26APR27DETORL-214",
        "matchup": "Detroit Pistons @ Orlando Magic",
        "title": "Detroit @ Orlando Total Points (>214.5)",
        "side": "NO",
        "mkt_type": "total",
        "edge_pct": 6.8,
        "kalshi_price": 0.43,
        "kalshi_yes_at_flag": 57.0,
        "pin_prob_at_flag": 49.8,
        "flagged_at": "2026-04-27T21:11:00+00:00",
        "status": "won",
        "resolved_at": "2026-04-28T03:05:42+00:00",
        "resolved_by": "kalshi",
        "pnl": 50.4,
        "paper_stake": 38.02,
        "paper_pnl": 50.4,
        "kelly_bet_pct": 2.982,
        "kelly_bet_dollars": 38.02,
        "kelly_pnl": 0.02982,
        "kelly_pnl_pct": 2.982,
        "kelly_pnl_dollars": 50.4,
        "clv_mult_applied": 1.0,
        "closing_yes_pct": None,
        "clv": 0.0,
        "closing_pin_pct": None,
        "clv_source": "none",
        "_note": "kalshi_price ~0.43 estimated — REST API has no historical orderbook snapshots",
    })
    _data_fixed = True
    print("  Restored DET@ORL NO bet (pre-game find, NO won 182 vs 214.5)")

for _b in _bets:
    # NYY@TEX Apr 27: game was NYY 4 TEX 2 = 6 total, YES on >7.5 = LOST.
    # Kalshi ticker said APR28 (next day UTC) so auto-resolver never caught it.
    if _b.get("id") == "KXMLBTOTAL-26APR282005NYYTEX-8|YES" and _b.get("status") == "open":
        stake = _b.get("paper_stake", 13.44)
        kpct  = _b.get("kelly_bet_pct", 1.127)
        kdol  = _b.get("kelly_bet_dollars", 11.27)
        _b["status"]            = "lost"
        _b["resolved_at"]       = "2026-04-28T03:00:00+00:00"
        _b["resolved_by"]       = "manual"
        _b["pnl"]               = round(-stake, 2)
        _b["paper_pnl"]         = round(-stake, 2)
        _b["kelly_pnl"]         = round(-kpct / 100, 5)
        _b["kelly_pnl_pct"]     = round(-kpct, 3)
        _b["kelly_pnl_dollars"] = round(-kdol, 2)
        _b["_note"]             = "Resolved manually: Apr 27 NYY 4 TEX 2 = 6 total, under 7.5. Ticker APR28 = UTC date mismatch, auto-resolver missed it."
        _data_fixed = True
        print("  Resolved NYY@TEX YES as LOST (Apr 27 game: 6 total vs 7.5 line)")

# LAA@CWS Apr 27: 2-5 = 7 total. YES on >9.5 lost.
# CHC@SD Apr 28: total over 7.5. NO (under) lost.
# MIN@DEN Apr 27: DEN 125, MIN 113 = 238 total. NO on >222.5 lost.
_late_losses = {
    "KXMLBTOTAL-26APR271940LAACWS-10|YES": ("2026-04-28T04:00:00+00:00", "LAA 2 CWS 5 = 7 total, YES on >9.5 lost"),
    "KXMLBTOTAL-26APR282140CHCSD-8|NO":    ("2026-04-29T05:00:00+00:00", "CHC@SD total over 7.5, NO (under) lost"),
    "KXNBATOTAL-26APR27MINDEN-223|NO":     ("2026-04-28T03:30:00+00:00", "DEN 125 MIN 113 = 238 total, NO on >222.5 lost"),
}
for _b in _bets:
    _ll = _late_losses.get(_b.get("id",""))
    if _ll and _b.get("status") == "open":
        _rat, _note = _ll
        _ps  = _b.get("paper_stake", 0)
        _kp  = _b.get("kelly_bet_pct", 0)
        _kd  = _b.get("kelly_bet_dollars", 0)
        _b["status"]             = "lost"
        _b["resolved_at"]        = _rat
        _b["resolved_by"]        = "manual"
        _b["pnl"]               = round(-_ps, 2)
        _b["paper_pnl"]         = round(-_ps, 2)
        _b["kelly_pnl"]         = round(-_kp / 100, 5)
        _b["kelly_pnl_pct"]     = round(-_kp, 3)
        _b["kelly_pnl_dollars"] = round(-_kd, 2)
        _b["_note"]             = _note
        _data_fixed = True
        print(f"  Resolved {_b['id']} as LOST")

# Donovan Mitchell NO: TOR@CLE Apr 29, under 3.5 assists → NO won.
# NBA props were eliminated so Kalshi won't auto-resolve; manual fix required.
_mitchell_id = "KXNBAAST-26APR29TORCLE-CLEDMITCHELL45-4|NO"
for _b in _bets:
    if _b.get("id") == _mitchell_id and _b.get("status") == "open":
        _ps = _b.get("paper_stake", 59.6)
        _k  = _b.get("kalshi_price", 0.31)
        _kd = _b.get("kelly_bet_dollars", 50.0)
        _kp = _b.get("kelly_bet_pct", 5.0)
        _b["status"]             = "won"
        _b["resolved_at"]        = "2026-04-30T03:00:00+00:00"
        _b["resolved_by"]        = "manual"
        _b["pnl"]               = round(_ps * (1 - _k) / _k, 2)
        _b["paper_pnl"]         = round(_ps * (1 - _k) / _k, 2)
        _b["kelly_pnl"]         = round(_kp * (1 - _k) / _k / 100, 5)
        _b["kelly_pnl_pct"]     = round(_kp * (1 - _k) / _k, 3)
        _b["kelly_pnl_dollars"] = round(_kd * (1 - _k) / _k, 2)
        _b["_note"]             = "Resolved manually: TOR@CLE Apr 29, Mitchell under 3.5 assists. NO won."
        _data_fixed = True
        print("  Resolved Mitchell NO as WON (under 3.5 assists TOR@CLE Apr 29)")

for _b in _bets:
    # TB @ PIT: paper_pnl was set incorrectly via manual_correction
    if _b.get("id") == "KXMLBTOTAL-26APR181605TBPIT-8|YES" and _b.get("paper_pnl") != 25.03:
        _b["paper_pnl"] = 25.03
        _b["pnl"]       = 25.03
        _data_fixed = True
    # Spurs @ Blazers: CLV was calculated from in-game 4¢ price (-42.0 → +3.1 via pin_entry)
    if _b.get("id") == "KXNBATOTAL-26APR26SASPOR-218|YES":
        if _b.get("clv") != 3.1:
            _b["clv"]             = 3.1
            _b["clv_source"]      = "pin_entry"
            _b["closing_pin_pct"] = 49.1
            _data_fixed = True
    # Marcus Smart: closing_yes_pct was in-game (88¢), CLV -33.0 → +4.8 via pin_entry
    if _b.get("id") == "KXNBAAST-26APR26LALHOU-LALMSMART36-5|NO":
        if _b.get("clv") != 4.8:
            _b["closing_yes_pct"] = 55.0   # reset to entry (true pre-game close unknown)
            _b["clv"]             = 4.8
            _b["clv_source"]      = "pin_entry"
            _b["closing_pin_pct"] = 49.8
            _data_fixed = True
    # NYY @ HOU: had pin_prob_at_flag but used Kalshi drift (-1.0 → +7.8 via pin_entry)
    if _b.get("id") == "KXMLBTOTAL-26APR242010NYYHOU-9|NO":
        if _b.get("clv") != 7.8:
            _b["clv"]             = 7.8
            _b["clv_source"]      = "pin_entry"
            _b["closing_pin_pct"] = 52.8
            _data_fixed = True
    # LAD @ SF: had pin_prob_at_flag but used Kalshi drift (-1.0 → +8.1 via pin_entry)
    if _b.get("id") == "KXMLBTOTAL-26APR231545LADSF-7|NO":
        if _b.get("clv") != 8.1:
            _b["clv"]             = 8.1
            _b["clv_source"]      = "pin_entry"
            _b["closing_pin_pct"] = 49.1
            _data_fixed = True

# --- Kalshi-to-Kalshi CLV: 6 early spread bets (Apr 3–4) where closing_yes
#     was a real capture (differs from entry). No Pinnacle data available.
_kalshi_clv_map = {
    "KXMLBSPREAD-26APR042105NYMSF-SF2|NO":   (70.0, 66.0),
    "KXMLBSPREAD-26APR042138SEALAA-LAA2|NO": (76.0, 75.0),
    "KXMLBSPREAD-26APR041905MIANYY-MIA2|NO": (75.0, 79.0),
    "KXMLBSPREAD-26APR041610SDBOS-SD3|NO":   (79.0, 77.0),
    "KXMLBSPREAD-26APR041610SDBOS-SD4|NO":   (86.0, 84.0),
    "KXMLBSPREAD-26APR041310STLDET-STL4|NO": (87.0, 84.0),
}
for _b in _bets:
    _pair = _kalshi_clv_map.get(_b.get("id", ""))
    if _pair and _b.get("clv_source") == "none":
        _ey, _cy = _pair
        _b["clv"]        = round(_ey - _cy, 1)   # all NO bets: entry_yes - close_yes
        _b["clv_source"] = "kalshi"
        _data_fixed = True

# --- Systemic fix: upgrade clv_source="kalshi" → "pin_entry" for any bet
#     that has pin_prob_at_flag but no true Pinnacle close captured yet ---
for _b in _bets:
    if (_b.get("clv_source") == "kalshi"
            and _b.get("pin_prob_at_flag") is not None
            and _b.get("closing_pin_pct") is None):
        _ek = (_b.get("kalshi_price") or 0) * 100
        if _ek:
            _b["clv"]             = round(_b["pin_prob_at_flag"] - _ek, 1)
            _b["clv_source"]      = "pin_entry"
            _b["closing_pin_pct"] = _b["pin_prob_at_flag"]
            _data_fixed = True

# Fix: Mark total bets whose CLV used wrong Pinnacle game (UTC/ET date collision pre-fix).
#      clv_source="pin" totals flagged before 2026-05-20 had 27% win rate — wrong Pinnacle
#      game matched due to date collision bug (fixed 2026-05-20). Only applies to pre-fix bets.
for _b in _bets:
    if ("TOTAL" in _b.get("ticker", "") and _b.get("clv_source") == "pin"
            and _b.get("flagged_at", "9999") < "2026-05-20"):
        _b["clv_source"] = "corrupted_utc"
        _data_fixed = True
# Restore post-fix total bets wrongly marked corrupted_utc (missing date guard before this fix)
for _b in _bets:
    if (_b.get("clv_source") == "corrupted_utc"
            and "TOTAL" in _b.get("ticker", "")
            and _b.get("flagged_at", "") >= "2026-05-20"):
        _b["clv_source"] = "pin"
        _data_fixed = True

# Fix: Remove bets with wrong Pinnacle game match (ticker mismatch → fake edge)
_bad_match_ids = {
    "KXMLBTOTAL-26MAY152140SFATH-8|NO",            # SF@ATH matched to SF@LAD Pinnacle data → 12.1% fake edge
    "KXMLBTOTAL-26MAY152138LADLAA-8|NO",            # LAD@LAA matched to SF@LAD Pinnacle data
    "KXMLBSPREAD-26MAY152138LADLAA-LAD2|NO",        # LAD@LAA spread matched to SF@LAD
    "KXMLBKS-26MAY192140LADSD-SDGCANNING17-6|YES",  # Canning ghost edge: Pinnacle "over 4.5 Ks" (P(X≥5)) matched to Kalshi "6+" (P(X≥6)) — line mismatch int(4.5)=4 ≠ kalshi_thresh=5
    "KXMLBTOTAL-26MAY202040LADSD-9|YES",             # Suspected floor_strike mismatch: scanner fair=46% but Pinnacle over 8.5 = +151 (~40%) — quarantined pending total-diag log verification
}
_bets = [_b for _b in _bets if _b.get("id") not in _bad_match_ids]
_data_fixed = True

# Fix: ATL@SEA May 6 game was incorrectly marked correlated with May 5 game.
# Same matchup/type/side but different days — not correlated.
_atlsea_may6_id = "KXMLBTOTAL-26MAY061610ATLSEA-9|YES"
for _b in _bets:
    if _b.get("id") == _atlsea_may6_id and _b.get("correlated") is True:
        _b["correlated"] = False
        _data_fixed = True

# Fix: Retroactively mark all KXMLBHR bets logged before shadow mode was added
# (2026-06-17) as shadow and zero their stakes/pnl so they don't drag Kelly P&L.
for _b in _bets:
    if _b.get("ticker", "").upper().startswith("KXMLBHR") and not _b.get("shadow"):
        _b["shadow"]      = True
        _b["paper_stake"] = 0.0
        if _b.get("status") in ("won", "lost"):
            _b["paper_pnl"] = 0.0
        _data_fixed = True
        print(f"  Shadow-backfill: zeroed stake/pnl on {_b.get('id','?')}")

# Fix: Retroactively resize all non-shadow paper stakes using the corrected
# time-Kelly multiplier (flipped Jun 2026 based on 71-bet performance data).
# Uses flagged_at vs game_time to reconstruct the correct multiplier at entry.
# Bets without game_time get 0.75x (unknown, same as live default).
_RESIZE_VERSION = "v2_flipped_time_mult"
_resized_count = 0
for _b in _bets:
    if _b.get("shadow") or _b.get("correlated"):
        continue
    if _b.get("clv_source") == "corrupted_utc":
        continue
    if _b.get("_resize_version") == _RESIZE_VERSION:
        continue   # already resized in a previous deploy
    _flagged = _b.get("flagged_at")
    _gt      = _b.get("game_time")
    _edge    = _b.get("edge_pct", 0) / 100.0
    _kprice  = _b.get("kalshi_price", 0)
    if not _flagged or _edge <= 0 or _kprice <= 0 or _kprice >= 1:
        _b["_resize_version"] = _RESIZE_VERSION
        continue
    # Compute hours between flag time and game time
    try:
        _dt_flag = datetime.fromisoformat(_flagged.replace("Z", "+00:00"))
        _dt_game = datetime.fromisoformat(_gt.replace("Z", "+00:00")) if _gt else None
        _hrs = (_dt_game - _dt_flag).total_seconds() / 3600 if _dt_game else None
    except (ValueError, AttributeError):
        _hrs = None
    # New multiplier brackets (data-driven)
    if _hrs is None:
        _tmult = 0.75
    elif _hrs > 24:
        _tmult = 0.25
    elif _hrs > 12:
        _tmult = 0.75
    elif _hrs >= 4:
        _tmult = 1.00
    else:
        _tmult = 0.50
    # Recompute stake
    _full_k   = _edge / (1.0 - _kprice)
    _new_stake = round(min(_full_k * PAPER_KELLY_FRACTION * _tmult, PAPER_KELLY_CAP) * PAPER_START_BALANCE, 2)
    _old_stake = _b.get("paper_stake", 0) or 0
    _b["paper_stake"]      = _new_stake
    _b["_resize_version"]  = _RESIZE_VERSION
    # Recompute paper_pnl for settled bets
    if _b.get("status") == "won":
        _b["paper_pnl"] = round(_new_stake * (1.0 - _kprice) / _kprice, 2)
    elif _b.get("status") == "lost":
        _b["paper_pnl"] = round(-_new_stake, 2)
    _resized_count += 1
if _resized_count:
    _data_fixed = True
    print(f"  Retroactive resize: updated stakes/pnl on {_resized_count} bet(s) → new time-Kelly multipliers")

if _data_fixed:
    _save_bets(_bets)
    print("  Applied one-time data corrections (CLV/pin_entry upgrades)")


def _bet_id(ticker: str, side: str) -> str:
    return f"{ticker}|{side}"


def _best_edge_per_game(edges: list) -> list:
    """
    Keep only the single best edge per slot.

    Slot keys:
      • Props:       (matchup,)               — one bet per player, best edge wins
                                                regardless of line or side. Prevents
                                                logging YES on 6+ AND NO on 5+ for
                                                the same pitcher.
      • Game lines:  (matchup, mkt_type, side) — YES and NO are independent markets
                                                (over/under, spread cover/not) so both
                                                can surface if genuinely +EV.

    NOTE: uses the raw 'edge' field (0–1 decimal) so this function is safe to
    call before 'edge_pct' is computed.
    """
    def _score(e: dict) -> float:
        return e.get("edge_pct", e.get("edge", 0) * 100)

    def _key(e: dict) -> tuple:
        if e.get("mkt_type") == "prop":
            return ("prop", e.get("matchup", ""))          # one slot per player
        return (e.get("matchup", ""), e.get("mkt_type", ""), e.get("side", ""))

    best: dict = {}
    for e in edges:
        key = _key(e)
        if key not in best or _score(e) > _score(best[key]):
            best[key] = e
    seen = set()
    result = []
    for e in edges:
        key = _key(e)
        if key not in seen and best[key] is e:
            seen.add(key)
            result.append(e)
    return result


def _compute_paper_balance() -> float:
    """
    Returns the current paper portfolio balance, compounding from PAPER_START_BALANCE.
    Only counts bets flagged on or after PAPER_START_DATE.
    Balance = start + sum(paper_pnl for settled) - sum(paper_stake for open)
    """
    with _bets_lock:
        paper_bets = [b for b in _bets if b.get("flagged_at", "") >= PAPER_START_DATE
                      and b.get("paper_stake") is not None
                      and not b.get("correlated", False)
                      and not b.get("shadow", False)
                      and b.get("clv_source") != "corrupted_utc"]
    bal = PAPER_START_BALANCE
    for b in paper_bets:
        if b["status"] in ("won", "lost") and b.get("paper_pnl") is not None:
            bal += b["paper_pnl"]
    return round(bal, 2)


def _paper_kelly_stake(edge_pct: float, kalshi_price: float,
                       game_time_iso: str | None = None) -> float:
    """Calculate Kelly-sized paper stake against current portfolio balance.

    Sizing chain:
      full_kelly  = edge / (1 − kalshi_price)          ← raw Kelly fraction
      base        = full_kelly × PAPER_KELLY_FRACTION   ← quarter-Kelly
      calibrated  = base × _time_kelly_mult(game_time)  ← time-to-game discount
      capped       = min(calibrated, PAPER_KELLY_CAP)   ← 3% hard ceiling
      stake        = capped × portfolio_balance
    """
    k = kalshi_price
    e = edge_pct / 100.0
    if k <= 0 or k >= 1 or e <= 0:
        return 0.0
    balance    = _compute_paper_balance()
    full_kelly = e / (1.0 - k)
    time_mult  = _time_kelly_mult(game_time_iso)
    frac       = min(full_kelly * PAPER_KELLY_FRACTION * time_mult, PAPER_KELLY_CAP)
    return round(frac * balance, 2)


def _add_new_bets(edges: list) -> list:
    """Log any edge we haven't seen before as an open bet.  Returns list of newly added bets.

    Source of truth: Pinnacle line only.
      • pin_prob_at_flag  = Pinnacle's no-vig probability when the edge was first
        detected.  Stored on the bet so we can detect line movement later.
      • DK / FD confirmation steps removed — Pinnacle is the sole fair-value anchor.

    Every qualifying edge is logged so the notification and paper portfolio are
    always in sync.  If a second edge on the same (matchup, mkt_type, side) slot
    is logged, it is marked correlated=True and excluded from win-rate / Kelly
    P&L stats — but still tracked for CLV and the full paper record.
    """
    edges = _best_edge_per_game(edges)
    newly_added = []
    with _bets_lock:
        existing_ids = {b["id"] for b in _bets}
        # Track slots already occupied by open bets to detect correlated entries.
        # Props: slot is (matchup, game_date) only — one bet per player per day,
        #        regardless of line or side (prevents YES on 6+ AND NO on 5+).
        # Game lines: slot includes side — YES/NO are independent markets.
        def _open_slot(b: dict) -> tuple:
            gd = _parse_ticker_date(b.get("ticker", ""))
            if b.get("mkt_type") == "prop":
                return ("prop", b["matchup"], gd)
            return (b["matchup"], b.get("mkt_type", ""), b["side"], gd)

        open_slots = {_open_slot(b) for b in _bets if b["status"] == "open"}
        added = 0
        for e in edges:
            # Skip edges already invalidated by Pinnacle line movement
            if e.get("pin_invalidated"):
                continue
            bid = _bet_id(e["ticker"], e["side"])
            if bid in existing_ids:
                continue   # exact same market already logged
            if bid in _bad_match_ids:
                continue   # permanently suppressed ghost/bad-match edge — never re-flag

            # ── Prop-specific edge floor ──────────────────────────────────────
            # MLB props (KS, HIT, TB, RBI) are less liquid than game lines —
            # smaller books, wider spreads, slower Kalshi repricing.  A 3% edge
            # on a prop carries far more variance than 3% on a total or spread,
            # so we require a 7% minimum before logging or staking.
            # Game lines (total, spread) remain at the global 3% floor.
            if e.get("mkt_type") == "prop" and e.get("edge_pct", 0) < EDGE_THRESHOLD * 100:
                print(f"  PASS (prop <{EDGE_THRESHOLD*100:.0f}%): {e.get('title','')} "
                      f"{e.get('side','')} edge={e.get('edge_pct',0):.1f}% — skipped")
                continue

            # ── Peak-hour persistence gate ────────────────────────────────────
            # Data shows Peak Trading (1–10pm PDT) has 8W/17L at -7.12 flat units
            # vs Early AM 14W/11L at +10.96 units.  Root cause: phantom edges from
            # API sync delays get logged before they stabilise during busy hours.
            # Require edge to have persisted ≥2.5 min (2 scan cycles at 75s) before
            # logging during peak hours.  Early AM / Morning exempt — those edges
            # are genuinely mispriced and win when logged immediately.
            _now_h_pdt = (datetime.now(timezone.utc).hour - 7) % 24
            _is_peak   = 13 <= _now_h_pdt < 22
            _age_min   = e.get("age_min", 0) or 0
            if _is_peak and _age_min < 2.5:
                print(f"  GATE (peak <2.5min): {e.get('title','')} "
                      f"{e.get('side','')} age={_age_min:.1f}min — waiting for confirmation")
                continue

            existing_ids.add(bid)   # prevent same ticker appearing twice in one cycle

            game_date = _parse_ticker_date(e.get("ticker", ""))
            if e.get("mkt_type") == "prop":
                slot = ("prop", e.get("matchup", ""), game_date)
            else:
                slot = (e.get("matchup", ""), e.get("mkt_type", ""), e.get("side", ""), game_date)
            is_correlated = slot in open_slots

            # entry_yes_pct = Kalshi YES ask % when flagged (for CLV calculation)
            kalshi_yes_at_flag = e["kalshi_pct"] if e["side"] == "YES" else round((1 - e["kalshi"]) * 100, 1)
            # Resolve game_time for the time-adaptive Kelly multiplier
            _gt_dt = _parse_ticker_start_time(e["ticker"])
            _game_time_iso = _gt_dt.isoformat() if _gt_dt else None
            shadow      = _is_shadow(e.get("ticker", ""))
            paper_stake = 0.0 if shadow else _paper_kelly_stake(e["edge_pct"], e["kalshi"], _game_time_iso)

            # Pinnacle probability at flag time — the baseline for line-shift detection
            bd = e.get("books_detail", {})
            pin_prob_raw = bd.get("pinnacle")  # Pinnacle no-vig prob for YES side
            if pin_prob_raw is not None:
                pin_prob_at_flag = round(
                    pin_prob_raw * 100 if e["side"] == "YES"
                    else (1.0 - pin_prob_raw) * 100,
                    1,
                )
            else:
                pin_prob_at_flag = None  # Pinnacle not available at flag time

            new_bet = {
                "id":                 bid,
                "ticker":             e["ticker"],
                "matchup":            e["matchup"],
                "title":              e["title"],
                "side":               e["side"],
                "mkt_type":           e.get("mkt_type", ""),
                "edge_pct":           e["edge_pct"],            # post-haircut adj. edge %
                "raw_edge_pct":       round(e.get("raw_edge", 0) * 100, 1),
                "edge":               e.get("edge", 0),         # decimal for Kelly calc in alert
                "fair":               e.get("fair"),            # fair prob for this side (Discord embed)
                "kalshi":             e["kalshi"],               # alias used by _sms_kelly
                "consensus_reason":   e.get("consensus_reason", ""),
                "books_used":         e.get("books_used", []),
                "consensus_prob":     e.get("consensus_prob"),
                # ── Pinnacle source-of-truth fields ──────────────────────────
                "pin_prob_at_flag":   pin_prob_at_flag,         # Pinnacle % at detection
                "pin_prob_pct":       e.get("pin_prob_pct"),    # same, pre-computed by _run_scan
                # ─────────────────────────────────────────────────────────────
                "kalshi_price":       e["kalshi"],
                "kalshi_yes_at_flag": kalshi_yes_at_flag,
                "pin_line_at_flag":   e.get("pin_line"),      # Pinnacle line at detection (e.g. 8.0)
                "kalshi_line_at_flag": e.get("kalshi_line"),  # Kalshi threshold at detection (e.g. 8.5)
                "flagged_at":         datetime.now(timezone.utc).isoformat(),
                "game_time":          _parse_ticker_start_time(e["ticker"]).isoformat()
                                      if _parse_ticker_start_time(e["ticker"]) else None,
                "status":             "open",
                "resolved_at":        None,
                "pnl":                None,
                "closing_yes_pct":    kalshi_yes_at_flag,   # init to entry; CLV loop overwrites
                "closing_pin_pct":    None,                 # Pinnacle side-prob at close; CLV loop fills
                "clv":                0.0,                  # pp form: closing_pin - entry_k
                "clv_pct":            None,                 # ROI form: (clv / entry_k) * 100
                "paper_stake":        paper_stake,           # Kelly-sized virtual wager ($0 for shadow)
                "paper_pnl":          None,                  # set on resolution
                "correlated":         is_correlated,         # excluded from win-rate/Kelly stats
                "shadow":             shadow,                 # True = tracked but $0 stake, excluded from balance
            }

            _bets.append(new_bet)
            newly_added.append(new_bet)
            existing_ids.add(bid)
            if not is_correlated:
                open_slots.add(slot)   # only primary bet claims the slot
            added += 1
        if added:
            save_ok = _save_bets(_bets)
            if save_ok:
                print(f"  Bet tracker: logged {added} new bet(s) → saved to {BETS_FILE}")
            else:
                # Save failed — roll back in-memory appends so Discord doesn't fire
                # for bets that won't survive a restart
                for b in newly_added:
                    try:
                        _bets.remove(b)
                    except ValueError:
                        pass
                newly_added.clear()
                print(f"  Bet tracker: save FAILED — rolled back {added} bet(s), Discord suppressed")
    return newly_added


def _commence_to_et_date(utc_str: str) -> str:
    """Convert an Odds API commence_time (UTC ISO string) to an ET date string.

    Kalshi tickers encode the ET date (e.g. APR27 for a game at 8 PM ET on Apr 27,
    even though that's Apr 28 00:05 UTC).  The Odds API commence_time is in UTC,
    so taking [:10] gives the UTC date — off by one for any game starting ≥ 8 PM ET.
    Subtracting the ET offset (EDT=4h, EST=5h) before slicing gives the correct date
    that matches what _parse_ticker_date() returns from the Kalshi ticker.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    try:
        dt_utc = _dt.fromisoformat(utc_str.replace("Z", "+00:00"))
        month = dt_utc.month
        et_offset = _td(hours=4 if 4 <= month <= 10 else 5)  # EDT or EST
        return (dt_utc - et_offset).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return utc_str[:10]   # fallback: raw UTC date


def _build_score_index() -> dict:
    """
    Fetch completed game scores (free, 0 credits) and return a lookup:
      norm(away + " @ " + home) → {home_score, away_score, total, margin}
    Covers MLB and NBA.
    """
    import re
    def _n(s): return re.sub(r"[^a-z0-9]", "", s.lower())
    index = {}
    for sport in ("baseball_mlb", "basketball_nba"):
        try:
            games = fetch_game_scores(sport, days_from=3)
        except Exception:
            continue
        for g in games:
            sc = {s["name"]: int(s["score"]) for s in (g.get("scores") or []) if s.get("score") is not None}
            home, away = g.get("home_team", ""), g.get("away_team", "")
            if home not in sc or away not in sc:
                continue
            hs, as_ = sc[home], sc[away]
            # Use ET date to match _parse_ticker_date() — games at ≥8 PM ET cross
            # midnight UTC, so commence_time[:10] (UTC) is one day ahead of the
            # Kalshi ticker date (ET), causing the resolver to return None forever.
            game_date = _commence_to_et_date(g.get("commence_time", ""))
            # Keyed by teams + date — prevents same-series games from cross-resolving
            key_dated   = _n(away + "@" + home) + "_" + game_date
            key_undated = _n(away + "@" + home)   # fallback for old callers
            entry = {
                "home_score": hs, "away_score": as_,
                "total": hs + as_, "margin": hs - as_,
                "home": home, "away": away,
                "game_date": game_date,
            }
            index[key_dated]   = entry
            index[key_undated] = entry   # undated always points to most-recent game
    return index


def _resolve_from_score(bet: dict, score_index: dict) -> Optional[bool]:
    """
    For spread/total bets, determine win/loss from final scores.
    Returns True (won), False (lost), or None (can't determine).
    """
    import re
    def _n(s): return re.sub(r"[^a-z0-9]", "", s.lower())

    matchup  = bet.get("matchup", "")
    # Parse game date from ticker so we never resolve against the wrong day's
    # result (e.g. same teams in a 3-game series, yesterday's score ≠ today's).
    ticker    = bet.get("ticker", "")
    game_date = _parse_ticker_date(ticker)   # "YYYY-MM-DD" or None
    base_key  = _n(matchup.replace(" @ ", "@"))
    dated_key = base_key + "_" + game_date if game_date else None

    # Prefer the date-specific entry; fall back to undated only if no dated match
    game = (score_index.get(dated_key) if dated_key else None) or score_index.get(base_key)
    if game is None:
        return None
    # If we got an undated match, verify the game date matches the ticker date
    # to avoid cross-series contamination.
    if dated_key and game.get("game_date") and game_date:
        if game["game_date"] != game_date:
            return None   # wrong day — don't resolve yet

    mkt_type = bet.get("mkt_type", "")
    side     = bet.get("side", "")
    title    = bet.get("title", "")

    # Detect totals by mkt_type field OR by ">X" threshold pattern in title
    is_total = mkt_type == "total" or bool(re.search(r">\s*\d+\.?\d*", title))

    if is_total:
        # Extract threshold from title: looks for e.g. ">8.5" or "(>8.5)"
        m = re.search(r">(\d+\.?\d*)", title)
        if not m:
            return None
        threshold = float(m.group(1))
        over_result = game["total"] > threshold
        if side == "YES":
            return over_result
        else:  # NO = under
            return not over_result

    elif mkt_type == "spread":
        # Title format: "{Team} wins by over {X} runs/goals?"
        # YES = team wins by more than X; NO = team does NOT win by more than X.
        m = re.search(r"(\d+\.?\d*)\s*(?:runs|goals)", title)
        if not m:
            return None
        threshold = float(m.group(1))
        # Figure out which team the spread references
        team_in_title = title.split(" wins by")[0].strip().lower()
        home_name = game["home"].lower()
        away_name = game["away"].lower()
        # Match by substring (e.g. "Toronto" in "Toronto Blue Jays")
        if team_in_title in home_name or home_name.startswith(team_in_title):
            team_margin = game["home_score"] - game["away_score"]
        elif team_in_title in away_name or away_name.startswith(team_in_title):
            team_margin = game["away_score"] - game["home_score"]
        else:
            return None
        covers = team_margin > threshold
        return covers if side == "YES" else not covers

    return None


def _clv_pct(clv_pp: float, entry_k: float) -> Optional[float]:
    """ROI form of CLV: how much the entry price appreciated vs fair value at close.
    clv_pp  — raw pp difference (closing_pin - entry_k), already computed
    entry_k — Kalshi entry price in cents (e.g. 42.0)
    Returns percentage ROI rounded to 2dp, or None if inputs are invalid.
    Example: clv_pp=5.3, entry_k=42.0 → (5.3/42.0)*100 = +12.62%
    """
    if not entry_k or clv_pp is None:
        return None
    return round((clv_pp / entry_k) * 100, 2)


def _check_resolutions():
    """
    Settle open bets via two methods:
      1. Scores-based (free, instant): for total bets after game ends.
      2. Kalshi API result field: for all bet types after Kalshi resolves.
    Only checks bets flagged > 3 hours ago.
    """
    with _bets_lock:
        open_bets = [b for b in _bets if b["status"] == "open"]

    if not open_bets:
        return

    # Guard: only attempt resolution when the game could plausibly have finished.
    # Primary check — game start time from ticker + 3.5h buffer.
    # This prevents doubleheader confusion (game-1 score resolving game-2 bet on
    # the same date) and timezone mismatches in the ticker time encoding.
    # Fallback (when ticker time can't be parsed): flagged_at + 4h.
    from datetime import timedelta as _td
    now_utc = datetime.now(timezone.utc)
    def _game_should_be_over(bet: dict) -> bool:
        game_start = _parse_ticker_start_time(bet.get("ticker", ""))
        if game_start:
            # game_start is true UTC (ET→UTC conversion done in _parse_ticker_start_time).
            # Add 3.5h for typical game duration + buffer before attempting resolution.
            return now_utc >= game_start + _td(hours=3.5)
        # Fallback when ticker time can't be parsed: must be flagged ≥6h ago.
        # Bets are typically flagged hours before game start, so 6h is conservative
        # enough to avoid premature resolution even for day games.
        flagged = datetime.fromisoformat(bet["flagged_at"])
        if flagged.tzinfo is None:
            flagged = flagged.replace(tzinfo=timezone.utc)
        return now_utc >= flagged + _td(hours=6)

    to_check = [b for b in open_bets if _game_should_be_over(b)]

    if not to_check:
        return

    # ── Method 1: Score-based settlement (totals only, free) ─────────────────
    score_index = _build_score_index()
    resolved = 0
    score_resolved_ids = set()

    for bet in to_check:
        side_won = _resolve_from_score(bet, score_index)
        if side_won is None:
            continue
        k = bet["kalshi_price"]
        with _bets_lock:
            for b in _bets:
                if b["id"] == bet["id"]:
                    b["status"]      = "won" if side_won else "lost"
                    b["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    b["resolved_by"] = "score"
                    # Kelly P&L — use paper_stake as the bet size
                    ps = b.get("paper_stake") or 0.0
                    kelly_pnl = round(ps * (1 - k) / k, 2) if side_won else round(-ps, 2)
                    b["pnl"]       = kelly_pnl   # unified: pnl IS kelly pnl
                    b["paper_pnl"] = kelly_pnl
                    # CLV — prefer true CLV vs Pinnacle; fall back to Kalshi drift
                    closing_pin = b.get("closing_pin_pct")
                    entry_k     = b.get("kalshi_price", 0) * 100
                    if closing_pin is not None and entry_k:
                        _clv = round(closing_pin - entry_k, 1)
                        b["clv"]     = _clv
                        b["clv_pct"] = _clv_pct(_clv, entry_k)
                    else:
                        closing_yes = b.get("closing_yes_pct")
                        entry_yes   = b.get("kalshi_yes_at_flag")
                        if closing_yes is not None and entry_yes is not None:
                            _clv = round(
                                closing_yes - entry_yes if b["side"] == "YES"
                                else entry_yes - closing_yes, 1
                            )
                            b["clv"]     = _clv
                            b["clv_pct"] = _clv_pct(_clv, entry_yes)
                    break
        score_resolved_ids.add(bet["id"])
        resolved += 1

    # ── Method 2: Kalshi API result (spreads, props, remaining totals) ────────
    remaining = [b for b in to_check if b["id"] not in score_resolved_ids]
    for bet in remaining[:20]:  # cap at 20 Kalshi API calls per cycle
        try:
            time.sleep(0.25)
            data = kalshi_get(f"/markets/{bet['ticker']}")
            mkt = data.get("market", {})
            result = mkt.get("result", "")
            if not result:
                continue
            side_won = (result == "yes" and bet["side"] == "YES") or \
                       (result == "no"  and bet["side"] == "NO")
            k = bet["kalshi_price"]
            # CLV — primary: true CLV vs Pinnacle closing line.
            # Grab the stored closing_pin_pct (filled by _capture_clv_prices).
            # Fall back to last_pin_pct in edge history, then Kalshi drift.
            with _bets_lock:
                bet_pin_close = next(
                    (b.get("closing_pin_pct") for b in _bets if b["id"] == bet["id"]),
                    None,
                )
                bet_closing = next(
                    (b.get("closing_yes_pct") for b in _bets if b["id"] == bet["id"]),
                    None,
                )
            if bet_pin_close is None:
                ek = _edge_key(bet)
                with _edge_history_lock:
                    hist = _edge_price_history.get(ek, {})
                    bet_pin_close = hist.get("last_pin_pct")
            if bet_closing is None:
                ek = _edge_key(bet)
                with _edge_history_lock:
                    hist = _edge_price_history.get(ek, {})
                    bet_closing = hist.get("last_kalshi_pct")

            entry_k   = bet.get("kalshi_price", 0) * 100
            entry_yes = bet.get("kalshi_yes_at_flag")
            clv = None
            clv_pct_val = None
            if bet_pin_close is not None and entry_k:
                # True CLV: Pinnacle side-prob at close minus Kalshi entry price.
                # Positive = sharp market agreed your edge was real at close.
                clv = round(bet_pin_close - entry_k, 1)
                clv_pct_val = _clv_pct(clv, entry_k)
            elif bet_closing is not None and entry_yes is not None:
                # Fallback: Kalshi drift (side-appropriate ask/bid prices)
                clv = round(bet_closing - entry_yes if bet["side"] == "YES"
                            else entry_yes - bet_closing, 1)
                clv_pct_val = _clv_pct(clv, entry_yes)
            closing_yes = bet_closing
            with _bets_lock:
                for b in _bets:
                    if b["id"] == bet["id"]:
                        b["status"]          = "won" if side_won else "lost"
                        b["resolved_at"]     = datetime.now(timezone.utc).isoformat()
                        b["resolved_by"]     = "kalshi"
                        if closing_yes is not None:
                            b["closing_yes_pct"] = closing_yes
                        if bet_pin_close is not None:
                            b["closing_pin_pct"] = bet_pin_close
                        # Only overwrite CLV if we actually computed one —
                        # never clobber a previously captured CLV with None.
                        if clv is not None:
                            b["clv"]     = clv
                            b["clv_pct"] = clv_pct_val
                        # Kelly P&L — unified: pnl IS paper_pnl
                        ps = b.get("paper_stake") or 0.0
                        kelly_pnl = round(ps * (1 - k) / k, 2) if side_won else round(-ps, 2)
                        b["pnl"]       = kelly_pnl
                        b["paper_pnl"] = kelly_pnl
                        break
            resolved += 1
        except Exception as exc:
            print(f"  Resolution check error ({bet['ticker']}): {exc}")

    if resolved:
        with _bets_lock:
            _save_bets(_bets)
        print(f"  Resolved {resolved} bet(s)")


def _infer_mkt_type(b: dict) -> str:
    """Infer raw market type ('spread'/'total'/'moneyline') from a bet or edge dict."""
    mtype = b.get("mkt_type", "")
    if mtype:
        return mtype
    ticker = b.get("ticker", "").upper()
    title  = b.get("title", "").lower()
    if "SPREAD" in ticker:
        return "spread"
    if "TOTAL" in ticker:
        return "total"
    if "ML" in ticker:
        return "moneyline"
    if "runs scored" in title or ("over" in title and "." in title):
        return "total"
    if "wins by" in title:
        return "spread"
    return ""


# Minimum number of CLV data points before we apply a penalty multiplier.
# Prevents over-reacting to a small sample of resolved bets.
_CLV_PENALTY_MIN_SAMPLE = 10   # lowered from 40 — props have fewer bets, 40 was unreachable


def _get_clv_multipliers() -> dict:
    """
    Compute per-market-type Kelly confidence multipliers from CLV history.

    Logic:
      • Group all settled bets that have CLV data by raw market type.
      • If a type has ≥ _CLV_PENALTY_MIN_SAMPLE data points AND avg CLV < 0,
        that type gets a 0.5x multiplier (half Kelly) until CLV recovers.
      • All other types return 1.0 (full quarter-Kelly).

    Returns dict like {"total": 1.0, "spread": 1.0, "moneyline": 1.0}
    """
    with _bets_lock:
        settled = [b for b in _bets
                   if b["status"] in ("won", "lost")
                   and b.get("clv_source") in ("pin", "pin_entry", "kalshi")]

    by_type: dict = {}
    for b in settled:
        if b.get("clv_source") == "corrupted_utc":
            continue
        mtype = _infer_mkt_type(b)
        if not mtype:
            continue
        by_type.setdefault(mtype, []).append(b["clv"])

    multipliers: dict = {}
    for mtype, clvs in by_type.items():
        if len(clvs) >= _CLV_PENALTY_MIN_SAMPLE:
            avg_clv = sum(clvs) / len(clvs)
            multipliers[mtype] = 0.5 if avg_clv < 0 else 1.0

    return multipliers


def _get_performance(since: Optional[str] = None) -> dict:
    """Return model-quality performance stats — bet-size agnostic.

    since: ISO date string like '2026-03-15'.  When provided, only bets
           flagged on or after that date are included.
    """
    with _bets_lock:
        bets = list(_bets)

    if since:
        # since is a date string; flagged_at is a full ISO timestamp
        bets = [b for b in bets if b.get("flagged_at", "") >= since]

    won   = [b for b in bets if b["status"] == "won"]
    lost  = [b for b in bets if b["status"] == "lost"]
    open_ = [b for b in bets if b["status"] == "open"]
    settled = won + lost
    # clean_settled excludes corrupted-CLV bets from all performance calculations.
    # settled (full list) is still used for the bets display table only.
    clean_settled = [b for b in settled if b.get("clv_source") != "corrupted_utc"]

    # Game lines only — exclude all props (MLB + NBA) from top-level pills.
    def _is_prop(b: dict) -> bool:
        return _infer_mkt_type(b) in ("prop", "nba_prop")

    # correlated=True bets are logged for the paper record but excluded from
    # win-rate and Kelly P&L stats — they share the same game outcome as their
    # primary bet and would inflate sample size and double-count Kelly exposure.
    def _is_correlated(b: dict) -> bool:
        return b.get("correlated", False)

    def _is_corrupted(b: dict) -> bool:
        return b.get("clv_source") == "corrupted_utc"

    def _is_shadow_bet(b: dict) -> bool:
        return b.get("shadow", False) or any(
            b.get("ticker", "").upper().startswith(s) for s in SHADOW_MARKETS
        )

    gl_bets    = [b for b in bets  if not _is_prop(b) and not _is_corrupted(b)]
    gl_won     = [b for b in won   if not _is_prop(b) and not _is_correlated(b) and not _is_corrupted(b)]
    gl_lost    = [b for b in lost  if not _is_prop(b) and not _is_correlated(b) and not _is_corrupted(b)]
    gl_open    = [b for b in open_ if not _is_prop(b) and not _is_corrupted(b)]
    gl_settled = gl_won + gl_lost
    corrupted_count = sum(1 for b in bets if _is_corrupted(b))

    # ── All-market summary (props + game lines) for top-level pills ───────────
    # Props are now a core market type; game-line-only pills were hiding the
    # majority of our edges.  Keep gl_* for the by_type breakdown only.
    all_won     = [b for b in won   if not _is_correlated(b) and not _is_corrupted(b) and not _is_shadow_bet(b)]
    all_lost    = [b for b in lost  if not _is_correlated(b) and not _is_corrupted(b) and not _is_shadow_bet(b)]
    all_open    = [b for b in open_ if not _is_corrupted(b) and not _is_shadow_bet(b)]
    all_settled = all_won + all_lost
    all_clean   = [b for b in bets  if not _is_corrupted(b) and not _is_shadow_bet(b)]

    # ── Kelly sizing helper ────────────────────────────────────────────────────
    # Mirrors _paper_kelly_stake() exactly so dashboard metrics stay in sync
    # with real-time portfolio tracking.
    #
    # Sizing chain (identical to live staking):
    #   full_kelly  = edge / (1 − kalshi_price)
    #   base        = full_kelly × 0.25            ← quarter-Kelly
    #   calibrated  = base × time_mult             ← time-to-game discount
    #   adjusted    = calibrated × clv_mult        ← CLV confidence penalty
    #   capped      = min(adjusted, 3%)            ← validation-phase hard ceiling
    KELLY_FRACTION   = 0.25   # fractional Kelly base (matches PAPER_KELLY_FRACTION)
    KELLY_SINGLE_CAP = PAPER_KELLY_CAP   # 3% — always references the same constant

    # Fetch CLV-based multipliers once for the whole performance pass
    clv_mults = _get_clv_multipliers()

    def _kelly_frac(b: dict) -> float:
        k = b.get("kalshi_price", 0.5)
        e = b.get("edge_pct", 0) / 100.0
        if k <= 0 or k >= 1 or e <= 0:
            return 0.0
        full_kelly = e / (1.0 - k)
        # For settled/historical bets reconstruct the multiplier using flagged_at
        # vs game_time so we don't penalise all past bets with the <4h bracket
        # just because their game_time is now in the past.
        _flagged = b.get("flagged_at")
        _gt      = b.get("game_time")
        if b["status"] in ("won", "lost") and _flagged and _gt:
            try:
                _hrs = (datetime.fromisoformat(_gt.replace("Z", "+00:00")) -
                        datetime.fromisoformat(_flagged.replace("Z", "+00:00"))).total_seconds() / 3600
                if _hrs > 24:   time_mult = 0.25
                elif _hrs > 12: time_mult = 0.75
                elif _hrs >= 4: time_mult = 1.00
                else:           time_mult = 0.50
            except (ValueError, AttributeError):
                time_mult = _time_kelly_mult(b.get("game_time"))
        else:
            time_mult  = _time_kelly_mult(b.get("game_time"))
        mtype      = _infer_mkt_type(b)
        clv_mult   = clv_mults.get(mtype, 1.0)
        return min(full_kelly * KELLY_FRACTION * time_mult * clv_mult, KELLY_SINGLE_CAP)

    def _kelly_pnl(b: dict) -> Optional[float]:
        """P&L as fraction-of-bankroll under CLV-adjusted quarter-Kelly sizing."""
        if b["status"] not in ("won", "lost"):
            return None
        f = _kelly_frac(b)
        k = b["kalshi_price"]
        if b["status"] == "won":
            return f * (1.0 - k) / k   # profit = stake × net_odds
        return -f                        # loss = −stake (fraction of bankroll)

    # CLV stats — all market types, settled bets only.
    # Open bets have a live-updating clv (current Pinnacle drift) that is NOT a
    # locked closing line.  Including them inflates avg_clv while those bets are
    # showing favourable drift and deflates it when they aren't — phantom signal
    # that has nothing to do with historical model accuracy.
    clv_bets = [
        b for b in all_clean
        if b.get("clv_source") in ("pin", "pin_entry", "kalshi")
        and b.get("status") in ("won", "lost")   # Restricts the metric to settled history
    ]
    avg_clv  = round(sum(b["clv"] for b in clv_bets) / len(clv_bets), 1) if clv_bets else None

    # ── CLV source breakdown — exposes how much of avg_clv is real vs proxy ──
    # "pin"       = true closing line: pre-close Pinnacle fetch fired and got a fresh
    #               probability for the exact entry threshold. Most trustworthy.
    # "pin_entry" = Pinnacle prob at detection time (not at close). CLV ≈ original
    #               entry edge — not a real closing-line measurement. Inflates avg_clv.
    # "kalshi"    = Kalshi bid/ask drift only (no Pinnacle reference). Noisier signal.
    clv_by_source: dict = {}
    for src in ("pin", "pin_entry", "kalshi"):
        src_bets = [b for b in clv_bets if b.get("clv_source") == src]
        if src_bets:
            clv_by_source[src] = {
                "count":   len(src_bets),
                "avg_clv": round(sum(b["clv"] for b in src_bets) / len(src_bets), 1),
            }

    # Average line movement — all settled bets (open bets have no real closing line yet)
    line_moves = []
    for b in all_clean:
        if b.get("status") not in ("won", "lost"):
            continue
        entry = (b.get("kalshi_price") or 0) * 100
        close = b.get("closing_pin_pct")
        if not entry or close is None:
            continue
        line_moves.append(close - entry)
    avg_line_move = round(sum(line_moves) / len(line_moves), 1) if line_moves else None

    # Win rate — all market types (props + game lines), non-correlated
    win_rate = round(len(all_won) / len(all_settled) * 100, 1) if all_settled else None

    # Average entry edge — all settled bets, non-correlated.
    # Intentionally excludes open bets so a freshly-flagged high-edge bet
    # cannot inflate this before it has been proven by outcome.
    avg_edge = round(sum(b["edge_pct"] for b in all_settled) / len(all_settled), 1) if all_settled else None

    # Recent edge health — all settled bets flagged in the last 14 days.
    # Used to surface edge compression even when the all-time avg looks fine.
    from datetime import datetime, timezone, timedelta as _td
    _now = datetime.now(timezone.utc)
    _cutoff_14d = (_now - _td(days=14)).isoformat()
    _cutoff_7d  = (_now - _td(days=7)).isoformat()
    all_settled_14d = [b for b in all_settled if b.get("flagged_at", "") >= _cutoff_14d]
    all_settled_7d  = [b for b in all_settled if b.get("flagged_at", "") >= _cutoff_7d]
    recent_avg_edge    = round(sum(b["edge_pct"] for b in all_settled_14d) / len(all_settled_14d), 1) if all_settled_14d else None
    recent_bet_count   = len(all_settled_14d)
    recent_7d_count    = len(all_settled_7d)

    # Days since the most recent flagged bet (any status, any market type)
    all_flagged = sorted(
        [b for b in bets if b.get("flagged_at")],
        key=lambda b: b["flagged_at"], reverse=True
    )
    if all_flagged:
        _last_dt = datetime.fromisoformat(all_flagged[0]["flagged_at"])
        days_since_last_bet = round((_now - _last_dt).total_seconds() / 86400, 1)
    else:
        days_since_last_bet = None

    # ── Flat unit P&L — all market types ─────────────────────────────────────
    unit_pnls = []
    for b in all_settled:
        k = b["kalshi_price"]
        unit_pnls.append((1 - k) / k if b["status"] == "won" else -1.0)

    total_units = round(sum(unit_pnls), 3) if unit_pnls else None
    avg_units   = round(sum(unit_pnls) / len(unit_pnls), 3) if unit_pnls else None

    # ── Kelly-weighted P&L — all market types ────────────────────────────────
    kelly_pnls = [_kelly_pnl(b) for b in all_settled]
    kelly_pnls = [x for x in kelly_pnls if x is not None]

    total_kelly_units   = round(sum(kelly_pnls), 4)         if kelly_pnls else None
    total_kelly_dollars = round(sum(kelly_pnls) * PERF_BANKROLL, 2) if kelly_pnls else None
    avg_kelly_units     = round(sum(kelly_pnls) / len(kelly_pnls), 4) if kelly_pnls else None

    # Model accuracy — all market types
    if all_settled:
        avg_kalshi_implied = round(
            sum(b["kalshi_price"] * 100 for b in all_settled) / len(all_settled), 1
        )
    else:
        avg_kalshi_implied = None

    # Add per-bet P&L fields for display
    # kelly_pnl_pct = P&L as % of bankroll (e.g. +0.42 = +0.42%)
    # kelly_bet_pct = Kelly-recommended stake as % of bankroll
    # Clean bets (no cap) sorted newest-first, corrupted appended at end
    _clean_table = sorted(
        [b for b in settled + open_ if b.get("clv_source") != "corrupted_utc"],
        key=lambda b: b["flagged_at"], reverse=True
    )
    _corrupt_table = sorted(
        [b for b in settled if b.get("clv_source") == "corrupted_utc"],
        key=lambda b: b["flagged_at"], reverse=True
    )
    table_bets = _clean_table + _corrupt_table
    for b in table_bets:
        is_shadow_b = _is_shadow_bet(b)
        kf = 0.0 if is_shadow_b else _kelly_frac(b)
        kp = None if is_shadow_b else _kelly_pnl(b)
        b["kelly_bet_pct"]     = round(kf * 100, 3)
        b["kelly_bet_dollars"] = round(kf * PERF_BANKROLL, 2)
        if b["status"] == "open" or is_shadow_b:
            b["kelly_pnl"]         = None
            b["kelly_pnl_pct"]     = None
            b["kelly_pnl_dollars"] = None
        else:
            b["kelly_pnl"]         = round(kp, 5) if kp is not None else None
            b["kelly_pnl_pct"]     = round(kp * 100, 3) if kp is not None else None
            b["kelly_pnl_dollars"] = round(kp * PERF_BANKROLL, 2) if kp is not None else None
        # Flag which multiplier was applied so the UI can show a note
        mtype = _infer_mkt_type(b)
        b["clv_mult_applied"]  = clv_mults.get(mtype, 1.0)
        # True CLV (pin drift) = Pinnacle close - Pinnacle at entry
        _paf = b.get("pin_prob_at_flag")
        _cpin = b.get("closing_pin_pct")
        b["pin_drift"] = round(_cpin - _paf, 1) if (_paf is not None and _cpin is not None) else None
        # Entry discount = Pinnacle at entry - Kalshi entry price (the mispricing we exploited)
        _ek = (b.get("kalshi_price") or 0) * 100
        b["entry_discount"] = round(_paf - _ek, 1) if (_paf is not None and _ek) else None

    _PROP_SERIES_LABELS = {
        "KXMLBKS":  "Strikeouts (K)",
        "KXMLBHR":  "Home Runs",
        "KXMLBHIT": "Hits",
        "KXMLBTB":  "Total Bases",
        "KXMLBRBI": "RBIs",
    }

    def _perf_label(b: dict) -> str:
        ticker = b.get("ticker", "").upper()
        mtype  = _infer_mkt_type(b)
        if mtype == "nba_prop":
            return "NBA Props"
        if mtype == "prop":
            for prefix, label in _PROP_SERIES_LABELS.items():
                if ticker.startswith(prefix):
                    return label
            return "MLB Props"
        if ticker.startswith("KXNBA"):
            sport = "NBA"
        else:
            sport = "MLB"
        return f"{sport} {mtype.capitalize()}" if mtype else sport

    by_type = {}
    for b in clean_settled:
        label = _perf_label(b)
        is_shad = _is_shadow_bet(b)
        if label not in by_type:
            by_type[label] = {"won": 0, "lost": 0, "shadow_won": 0, "shadow_lost": 0,
                               "units": [], "kelly": [], "clv": [], "pin_drifts": [], "shadow": is_shad}
        # pin drift per bet = closing_pin_pct - pin_prob_at_flag (true CLV)
        _pin_at_flag  = b.get("pin_prob_at_flag")
        _closing_pin  = b.get("closing_pin_pct")
        _pin_drift_v  = round(_closing_pin - _pin_at_flag, 1) if (_pin_at_flag is not None and _closing_pin is not None) else None
        # shadow bets: track CLV + shadow won/lost for monitoring — exclude from live counts
        if is_shad:
            if b["status"] == "won":   by_type[label]["shadow_won"]  += 1
            elif b["status"] == "lost": by_type[label]["shadow_lost"] += 1
            if _pin_drift_v is not None:
                by_type[label]["pin_drifts"].append(_pin_drift_v)
            clv_val = b.get("clv")
            if clv_val is not None:
                by_type[label]["clv"].append(clv_val)
            continue
        kp = _kelly_pnl(b)
        if b["status"] == "won":
            by_type[label]["won"] += 1
            k = b["kalshi_price"]
            by_type[label]["units"].append((1 - k) / k)
        else:
            by_type[label]["lost"] += 1
            by_type[label]["units"].append(-1.0)
        if kp is not None:
            by_type[label]["kelly"].append(kp)
        if _pin_drift_v is not None:
            by_type[label]["pin_drifts"].append(_pin_drift_v)
        clv_val = b.get("clv")
        if clv_val is not None:
            by_type[label]["clv"].append(clv_val)

    MIN_SAMPLE = 20   # need at least 20 settled bets before win rate is meaningful

    type_breakdown = []
    for label, d in by_type.items():
        total_t    = d["won"] + d["lost"]
        wr_t       = round(d["won"] / total_t * 100, 1) if total_t else None
        # Kelly P&L expressed as % of bankroll (sum of kelly fractions × 100)
        kelly_pct_t = round(sum(d["kelly"]) * 100, 3) if d["kelly"] else None
        # Dollar amount for display alongside %, derived from pct × bankroll
        kelly_t    = round(sum(d["kelly"]) * PERF_BANKROLL, 2) if d["kelly"] else None
        # Average CLV across all settled bets in this market type
        avg_clv_t       = round(sum(d["clv"]) / len(d["clv"]), 2) if d["clv"] else None
        avg_pin_drift_t = round(sum(d["pin_drifts"]) / len(d["pin_drifts"]), 2) if d["pin_drifts"] else None
        type_breakdown.append({
            "label":             label,
            "won":               d["won"],
            "lost":              d["lost"],
            "shadow_won":        d.get("shadow_won", 0),
            "shadow_lost":       d.get("shadow_lost", 0),
            "win_rate":          wr_t,
            "kelly_pct":         kelly_pct_t,
            "kelly_dollars":     kelly_t,
            "avg_clv":           avg_clv_t,
            "avg_pin_drift":     avg_pin_drift_t,
            "insufficient_data": total_t < MIN_SAMPLE,
            "sample_size":       total_t,
            "shadow":            d.get("shadow", False),
        })
    type_breakdown.sort(key=lambda x: -(x["won"] + x["lost"]))

    # Total Kelly P&L as % of bankroll
    total_kelly_pct = round(sum(kelly_pnls) * 100, 3) if kelly_pnls else None

    # ── Alpha metrics ─────────────────────────────────────────────────────────
    # Entry discount = pin_at_flag - entry_kalshi: the Kalshi mispricing you captured
    # Pin drift = closing_pin - pin_at_flag: did the sharp market agree after you bet?
    _entry_discounts: list = []
    _pin_drifts_agg:  list = []
    for b in all_settled:
        _paf = b.get("pin_prob_at_flag")
        _ek  = (b.get("kalshi_price") or 0) * 100
        _cp  = b.get("closing_pin_pct")
        if _paf is not None and _ek:
            _entry_discounts.append(_paf - _ek)
        if _paf is not None and _cp is not None:
            _pin_drifts_agg.append(_cp - _paf)

    avg_entry_discount = round(sum(_entry_discounts) / len(_entry_discounts), 1) if _entry_discounts else None
    avg_pin_drift      = round(sum(_pin_drifts_agg)  / len(_pin_drifts_agg),  1) if _pin_drifts_agg  else None

    # Edge bucket breakdown: do higher-edge bets win more?
    _ALPHA_BUCKETS = [("2–4%", 2.0, 4.0), ("4–6%", 4.0, 6.0), ("6–8%", 6.0, 8.0), ("8%+", 8.0, 999.0)]
    alpha_buckets: list = []
    for _blabel, _bmin, _bmax in _ALPHA_BUCKETS:
        _bb = [b for b in all_settled if _bmin <= b.get("edge_pct", 0) < _bmax]
        if not _bb:
            continue
        _bw = sum(1 for b in _bb if b["status"] == "won")
        _avg_k = sum(b["kalshi_price"] for b in _bb) / len(_bb)
        alpha_buckets.append({
            "label":    _blabel,
            "n":        len(_bb),
            "win_rate": round(_bw / len(_bb) * 100, 1),
            "expected": round(_avg_k * 100, 1),
            "delta":    round(_bw / len(_bb) * 100 - _avg_k * 100, 1),
        })

    # ── Win-rate audit by (market type, CLV source) ───────────────────────────
    # Detects data-quality bugs: a CLV source with <38% win rate (N≥10) signals
    # the reference price was wrong (wrong game, wrong line, stale data).
    AUDIT_WARN_THRESHOLD = 38
    AUDIT_MIN_SAMPLE     = 10
    audit_buckets: dict = {}
    for b in clean_settled:
        mtype  = _infer_mkt_type(b)
        src    = b.get("clv_source", "none")
        key    = f"{mtype or 'other'}/{src}"
        audit_buckets.setdefault(key, [0, 0])
        audit_buckets[key][1] += 1
        if b["status"] == "won":
            audit_buckets[key][0] += 1
    source_audit = []
    for key, (w, n) in sorted(audit_buckets.items()):
        if n < AUDIT_MIN_SAMPLE:
            continue
        wr = round(100 * w / n, 1)
        source_audit.append({
            "key":     key,
            "wins":    w,
            "total":   n,
            "win_pct": wr,
            "warn":    wr < AUDIT_WARN_THRESHOLD,
        })

    return {
        "total_bets":           len(all_clean),
        "won":                  len(all_won),
        "lost":                 len(all_lost),
        "open":                 len(all_open),
        "win_rate":             win_rate,
        "avg_edge":             avg_edge,
        "total_units":          total_units,
        "avg_units":            avg_units,
        "total_kelly_units":    total_kelly_units,
        "total_kelly_dollars":  total_kelly_dollars,
        "total_kelly_pct":      total_kelly_pct,    # total P&L as % of bankroll
        "avg_kelly_units":      avg_kelly_units,
        "avg_kalshi_implied":   avg_kalshi_implied,
        "avg_clv":              avg_clv,
        "avg_line_move":        avg_line_move,
        "kelly_bankroll":       PERF_BANKROLL,
        "bets":                 table_bets,
        "by_type":              type_breakdown,
        "clv_multipliers":      clv_mults,         # e.g. {"prop": 0.5, "spread": 1.0}
        "recent_avg_edge":      recent_avg_edge,   # 14-day avg entry edge (settled only)
        "recent_bet_count":     recent_bet_count,  # settled bets in last 14d
        "recent_7d_count":      recent_7d_count,   # settled bets in last 7d
        "days_since_last_bet":  days_since_last_bet,
        "source_audit":         source_audit,      # win-rate by (mkt_type, clv_source) — warns if <38%
        "corrupted_excluded":   corrupted_count,   # bets excluded from stats (data-quality)
        "clv_by_source":        clv_by_source,     # {pin/pin_entry/kalshi: {count, avg_clv}}
        "avg_entry_discount":   avg_entry_discount, # avg (pin_at_flag - entry_kalshi) — the actual mispricing captured
        "avg_pin_drift":        avg_pin_drift,      # avg (closing_pin - pin_at_flag) — true CLV: did Pin agree?
        "alpha_buckets":        alpha_buckets,      # win rate vs expected by edge bucket
    }


# ── My Bets tracker (actual bets placed by user) ──────────────────────────────
import re as _re

def _load_my_bets() -> list:
    try:
        with open(MY_BETS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def _save_my_bets(bets: list):
    try:
        import tempfile, os as _os
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with _os.fdopen(fd, "w") as f:
            json.dump(bets, f, indent=2)
        _os.replace(tmp, MY_BETS_FILE)
    except Exception as exc:
        print(f"  WARNING: could not save my_bets: {exc}")

_my_bets: list = _load_my_bets()
_my_bets_lock = threading.Lock()

# Cache for mark-to-market state — refreshed by background thread every 60s
# so the /api/mybets endpoint never blocks on live Kalshi API calls.
_my_bets_state_cache: Optional[dict] = None
_my_bets_state_lock  = threading.Lock()


def _refresh_my_bets_state():
    """Fetch live Kalshi prices for open my_bets and update the cache."""
    with _my_bets_lock:
        bets = json.loads(json.dumps(_my_bets))   # deep copy

    for b in bets:
        b["current_price"] = None
        b["unrealized_pnl"] = None
        if b["status"] != "open":
            continue
        try:
            time.sleep(0.15)
            data = kalshi_get(f"/markets/{b['ticker']}")
            mkt  = data.get("market", {})
            bid_c = float(mkt.get("yes_bid") or 0)
            ask_c = float(mkt.get("yes_ask") or 0)
            if bid_c > 0 and ask_c > 0:
                yes_mid = (bid_c + ask_c) / 200.0   # cents → decimal mid
                cur = yes_mid if b["side"] == "YES" else 1.0 - yes_mid
                entry = b["entry_price"]
                contracts = b["contracts"]
                b["current_price"]   = round(cur * 100, 1)
                b["unrealized_pnl"]  = round((cur - entry) * contracts, 2)
        except Exception:
            pass

    open_b    = [b for b in bets if b["status"] == "open"]
    settled_b = [b for b in bets if b["status"] in ("won", "lost")]
    total_in      = sum(b["amount_spent"] for b in bets)
    realized_pnl  = sum(b["pnl"] for b in settled_b if b["pnl"] is not None)
    unrealized_pnl= sum(b.get("unrealized_pnl") or 0 for b in open_b)

    state = {
        "bets":            bets,
        "total_in":        round(total_in, 2),
        "realized_pnl":    round(realized_pnl, 2),
        "unrealized_pnl":  round(unrealized_pnl, 2),
        "net_pnl":         round(realized_pnl + unrealized_pnl, 2),
    }
    with _my_bets_state_lock:
        global _my_bets_state_cache
        _my_bets_state_cache = state


def _get_my_bets_state() -> dict:
    """Return cached mark-to-market state (refreshed every 60s in background)."""
    with _my_bets_state_lock:
        cached = _my_bets_state_cache
    if cached is not None:
        return cached
    # First call before cache is warm — compute synchronously once
    _refresh_my_bets_state()
    with _my_bets_state_lock:
        return _my_bets_state_cache or {"bets": [], "total_in": 0, "realized_pnl": 0, "unrealized_pnl": 0, "net_pnl": 0}


def _background_my_bets_loop():
    """Refresh mark-to-market prices for open my_bets every 60s."""
    while True:
        try:
            _refresh_my_bets_state()
        except Exception as exc:
            print(f"  My bets refresh error: {exc}")
        time.sleep(60)


def _settle_my_bets():
    """Settle open my_bets via scores then Kalshi API."""
    with _my_bets_lock:
        open_bets = [b for b in _my_bets if b["status"] == "open"]
    if not open_bets:
        return

    from datetime import timedelta as _td2
    now_utc = datetime.now(timezone.utc)

    score_index = _build_score_index()
    resolved = 0

    for bet in open_bets:
        # Same game-over guard as _check_resolutions — prevent premature settlement
        game_start = _parse_ticker_start_time(bet.get("ticker", ""))
        if game_start:
            if now_utc < game_start + _td2(hours=3.5):
                continue
        # Use dated key for score lookup to prevent cross-series contamination
        import re as _re2
        game_date = _parse_ticker_date(bet.get("ticker", ""))
        mkey_base = _re.sub(r"[^a-z0-9]", "", bet["matchup"].replace(" @ ", "@").lower())
        mkey_dated = mkey_base + "_" + game_date if game_date else None
        game = (score_index.get(mkey_dated) if mkey_dated else None) or score_index.get(mkey_base)
        # Reject if date doesn't match
        if game and mkey_dated and game.get("game_date") and game_date:
            if game["game_date"] != game_date:
                game = None
        settled = False

        if game:
            m = _re.search(r">(\d+\.?\d*)", bet["title"])
            if m:
                threshold = float(m.group(1))
                over_wins = game["total"] > threshold
                won = over_wins if bet["side"] == "YES" else not over_wins
                pnl = round(bet["amount_spent"] * (1 - bet["entry_price"]) / bet["entry_price"], 2) if won else -bet["amount_spent"]
                with _my_bets_lock:
                    for b in _my_bets:
                        if b["id"] == bet["id"]:
                            b["status"] = "won" if won else "lost"
                            b["pnl"] = pnl
                            b["resolved_at"] = datetime.now(timezone.utc).isoformat()
                            break
                resolved += 1
                settled = True

        if not settled:
            try:
                time.sleep(0.2)
                data = kalshi_get(f"/markets/{bet['ticker']}")
                result = data.get("market", {}).get("result", "")
                if result:
                    won = (result == "yes" and bet["side"] == "YES") or \
                          (result == "no"  and bet["side"] == "NO")
                    pnl = round(bet["amount_spent"] * (1 - bet["entry_price"]) / bet["entry_price"], 2) if won else -bet["amount_spent"]
                    with _my_bets_lock:
                        for b in _my_bets:
                            if b["id"] == bet["id"]:
                                b["status"] = "won" if won else "lost"
                                b["pnl"] = pnl
                                b["resolved_at"] = datetime.now(timezone.utc).isoformat()
                                break
                    resolved += 1
            except Exception:
                pass

    if resolved:
        with _my_bets_lock:
            _save_my_bets(_my_bets)
        print(f"  My Bets: settled {resolved}")


# ── Twilio SMS alerts ─────────────────────────────────────────────────────────
_ALERT_MIN    = float(os.getenv("ALERT_MIN_EDGE", "0.020"))  # Discord alerts at ≥2.0% edge (matches EDGE_THRESHOLD)
_BET_SIZE     = float(os.getenv("ALERT_BET_SIZE", "20"))

# ── Discord webhook alert config ───────────────────────────────────────────────
_DISCORD_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK",
    "https://discord.com/api/webhooks/1491138264088838225/37azkybaSXl3ecgm_ltKXgt1y_VhmEETGuPNMOSke-5q3zv2v6TVmeGZaZTraVJlhqLt"
)

# ── Persistent alert dedup — survives server restarts ────────────────────────
# Each entry is "<matchup>|<title>|<side>|<game_date>" keyed by YYYY-MM-DD game
# date so the same teams+threshold on different days can each fire once.
_ALERTED_KEYS_FILE = os.path.join(DATA_DIR, "ev_alerted_keys.json")

def _edge_key(e: dict) -> str:
    """Stable identifier for an edge — matchup + prop + side + game date.

    Including the game date means Mets @ Dodgers Over 8.5 on Apr 13 and
    Apr 14 are treated as distinct edges and can each alert exactly once,
    while the same game on the same date is still deduplicated.
    """
    ticker    = e.get("ticker", "")
    game_date = _parse_ticker_date(ticker) or ""   # "2026-04-13" or ""
    return f"{e['matchup']}|{e['title']}|{e['side']}|{game_date}"


def _load_alerted_keys() -> tuple:
    """Load persisted alert keys and gone keys.

    Returns (alerted_keys, gone_alerted_keys) as sets.
    Gone keys are stored with a 'gone:' prefix in the same file.
    On fresh start (file missing — e.g. Railway cold deploy), reconstruct
    alerted keys from existing _bets so already-logged games never re-fire.
    """
    cutoff = (datetime.now(timezone.utc).date() - __import__('datetime').timedelta(days=14)).isoformat()
    try:
        with open(_ALERTED_KEYS_FILE) as f:
            data = json.load(f)   # {key: "YYYY-MM-DD" alerted_date}
        alerted = {k for k, d in data.items() if d >= cutoff and not k.startswith("gone:")}
        gone    = {k[5:] for k, d in data.items() if d >= cutoff and k.startswith("gone:")}
        return alerted, gone
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        # No file (Railway cold start or first run) — reconstruct from logged bets
        # so games that already have a bet never re-alert on next deploy.
        alerted = {_edge_key(b) for b in _bets}
        return alerted, set()

def _save_alerted_keys(alerted: set, gone: set):
    try:
        import tempfile, os as _os
        existing = {}
        try:
            with open(_ALERTED_KEYS_FILE) as f:
                existing = json.load(f)
        except Exception:
            pass
        today = datetime.now(timezone.utc).date().isoformat()
        for k in alerted:
            existing.setdefault(k, today)
        for k in gone:
            existing.setdefault(f"gone:{k}", today)
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with _os.fdopen(fd, "w") as f:
            json.dump(existing, f)
        _os.replace(tmp, _ALERTED_KEYS_FILE)
    except Exception as exc:
        print(f"  WARNING: could not save alerted keys: {exc}")

# Keys already alerted — loaded from disk so server restarts don't re-fire
_alerted_keys, _gone_alerted_keys = _load_alerted_keys()
print(f"  Loaded {len(_alerted_keys)} previously alerted edge key(s) from disk")

# ── Zero-edge drought detector ────────────────────────────────────────────────
# Fires a Discord alert when the scanner has produced zero edges for an extended
# period during game hours — indicates a silent data pipeline failure.
_zero_edge_streak      = 0          # consecutive scans with no qualifying edges
_last_props_scan: float = 0.0       # epoch seconds of last props scan
_last_prop_snapshot: dict = {}      # persists between prop scan cycles so UI stays populated
# Props refresh interval is now dynamic — see _props_refresh_interval() above.
_zero_edge_alerted     = False      # suppresses duplicate alerts per drought
_ZERO_EDGE_ALERT_SCANS = 60         # 60 × 2-min scan = 2 hours of silence

# ── Scan staleness detector ───────────────────────────────────────────────────
# Separate watchdog thread checks _state["last_scan"] every 60s.
# If >10 minutes pass without a completed scan during game hours, the scan
# thread is considered HUNG (blocked on network I/O but still alive) — fire
# alert + force-restart it regardless of is_alive() status.
_scan_stale_alerted    = False      # suppresses duplicate stale alerts
_SCAN_STALE_MINUTES    = 20         # minutes without a scan = hung thread (20 min allows for cold-start on Railway)
_watchdog_last_tick: float = 0.0    # epoch seconds of last watchdog loop tick — exposed in /api/scan
_BOOT_TIME: float      = time.time()  # process start time — used by watchdog cold-start check
_cold_start_alerted    = False      # fires once if last_scan stays None >10 min during game hours
_save_bets_alerted     = False      # fires once per session if _save_bets fails (disk/permission error)

# ── Kalshi auth failure detector ──────────────────────────────────────────────
_kalshi_auth_failed    = False      # set True on first 401; cleared on successful scan
_kalshi_auth_alerted   = False      # suppresses duplicate auth-failure Discord alerts

# ── Odds staleness detector ────────────────────────────────────────────────────
# Fires a Discord alert when the odds index hasn't refreshed in >2 hours during
# game hours — catches a silent odds-thread hang that watchdog misses (thread alive but stuck).
_odds_stale_alerted    = False      # suppresses duplicate odds-stale alerts
_ODDS_STALE_MINUTES    = 120        # 2 hours without an odds refresh = alert

# ── Discord alert log (visible in UI) ─────────────────────────────────────────
_discord_log: List[dict] = []
_discord_log_lock = threading.Lock()
_DISCORD_LOG_MAX  = 25


def _log_discord(ok: bool, preview: str, error: str = ""):
    with _discord_log_lock:
        _discord_log.append({
            "ts":      datetime.now().isoformat(),
            "ok":      ok,
            "preview": preview[:80],
            "error":   error[:200] if error else "",
        })
        if len(_discord_log) > _DISCORD_LOG_MAX:
            _discord_log.pop(0)


def send_discord(embed: dict, content: str = "") -> bool:
    """Send a Rich Embed to Discord via webhook. Returns True on success.

    content  = text shown in the phone push notification preview (plain text).
    embed    = Discord embed object (color, title, fields, footer, etc).

    Discord mobile shows `content` in the lock-screen notification, so we put
    the most critical info (edge %, Kelly $) there so no unlock needed.
    """
    if not _DISCORD_WEBHOOK:
        _log_discord(False, content, "DISCORD_WEBHOOK not set")
        return False
    try:
        import requests as _req
        resp = _req.post(
            _DISCORD_WEBHOOK,
            json={"content": content, "embeds": [embed]},
            timeout=10,
        )
        ok = resp.status_code in (200, 204)
        _log_discord(ok, content, "" if ok else resp.text[:200])
        print(f"  Discord alert {'sent ✓' if ok else f'FAIL {resp.status_code}'}")
        return ok
    except Exception as exc:
        _log_discord(False, content, str(exc))
        print(f"  Discord ERROR: {exc}")
        return False


def _prob_to_american_str(p: Optional[float]) -> str:
    """Convert decimal probability to American odds string, e.g. 0.60 → '-150'."""
    if p is None or p <= 0 or p >= 1:
        return "?"
    if p >= 0.5:
        return f"-{round(p / (1 - p) * 100)}"
    else:
        return f"+{round((1 - p) / p * 100)}"


def _sms_kelly(e: dict) -> float:
    """Compute 0.25 fractional Kelly (with CLV multiplier) matching the model."""
    k    = e.get("kalshi", 0.5)
    edge = e.get("edge", 0)          # post-haircut adj. edge (0–1 decimal)
    if k <= 0 or k >= 1 or edge <= 0:
        return 0.0
    full_kelly = edge / (1.0 - k)
    clv_mults  = _get_clv_multipliers()
    mtype      = e.get("mkt_type", "")
    clv_mult   = clv_mults.get(mtype, 1.0)
    return min(full_kelly * 0.25 * clv_mult, 0.05)


def _alert_top10(newly_logged: list = None):
    """
    After each scan, alert on newly logged bets + gone-edge follow-ups.

    Alerts fire exactly once per bet (keyed by matchup+title+side+date).
    Retry-safe: if send_discord fails, the key is NOT added to _alerted_keys,
    so the next cycle will retry by catching the bet in the recent-unalerted
    sweep (flagged within last 30 min, still open, not yet alerted).
    """
    global _alerted_keys

    # Always read current scan edges — needed for gone-edge follow-up
    with _lock:
        game_edges = list(_state.get("edges", []))

    min_edge = _ALERT_MIN

    # Primary: bets logged this cycle
    this_cycle = sorted(
        [b for b in (newly_logged or []) if b.get("edge_pct", 0) >= min_edge * 100],
        key=lambda x: x.get("edge_pct", 0), reverse=True,
    )

    # Retry safety net: open bets flagged in the last 30 min that never got
    # an alert (covers send_discord failures and any edge-case skip paths).
    _cutoff = (datetime.now(timezone.utc) - __import__('datetime').timedelta(minutes=30)).isoformat()
    with _bets_lock:
        recent_bets = list(_bets)
    retry_bets = [
        b for b in recent_bets
        if b["status"] == "open"
        and b.get("edge_pct", 0) >= min_edge * 100
        and not b.get("shadow")
        and b.get("flagged_at", "") >= _cutoff
        and _edge_key(b) not in _alerted_keys
        and b not in this_cycle   # don't double-count bets already in this_cycle
    ]
    if retry_bets:
        print(f"  Discord retry: {len(retry_bets)} unalerted bet(s) from last 30 min")

    all_edges = this_cycle + retry_bets

    now_utc = datetime.now(timezone.utc)
    clv_mults = _get_clv_multipliers()

    # ── Filter: skip already-alerted and started games ────────────────────────
    to_alert = []
    for e in all_edges:
        key = _edge_key(e)
        if key in _alerted_keys:
            continue   # already alerted — skip

        ticker = e.get("ticker", "")
        game_start = _parse_ticker_start_time(ticker)
        if game_start is None:
            exp_str = e.get("expiration_time") or e.get("expected_expiration_time") or ""
            if exp_str:
                try:
                    from datetime import timedelta as _tda
                    exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    is_nba = ticker.upper().startswith("KXNBA")
                    game_start = exp_dt - _tda(hours=4.0 if is_nba else 3.5)
                except (ValueError, AttributeError):
                    pass
        if game_start and game_start < now_utc:
            # Game already started — silence without alerting
            _alerted_keys.add(key)
            _save_alerted_keys(_alerted_keys, _gone_alerted_keys)
            continue

        to_alert.append(e)

    # ── Group by game so one game → one Discord ping ──────────────────────────
    # Multiple markets on the same game (e.g. Total YES + Spread YES) get
    # bundled into a single embed instead of firing separate notifications.
    import re as _re_alert
    def _game_group_key(e: dict) -> str:
        norm = _re_alert.sub(r"[^a-z0-9]", "", e.get("matchup", "").lower())
        return f"{norm}|{_parse_ticker_date(e.get('ticker','')) or ''}"

    from collections import OrderedDict
    groups: OrderedDict = OrderedDict()
    for e in to_alert:
        gk = _game_group_key(e)
        groups.setdefault(gk, []).append(e)

    ts = datetime.now().strftime("%I:%M %p")

    for gk, group_edges in groups.items():
        # Sort markets within group: highest edge first
        group_edges.sort(key=lambda x: x.get("edge_pct", 0), reverse=True)
        best = group_edges[0]   # lead with the strongest edge

        best_ep    = round(best.get("edge_pct", 0), 1)
        best_k     = best.get("kalshi", 0.5)
        best_fair  = best.get("fair")
        best_side  = best.get("side", "?")
        best_kelly = _sms_kelly(best)
        best_kelly_bet = round(best_kelly * PERF_BANKROLL, 2)
        best_kelly_pct = round(best_kelly * 100, 2)

        # Lock-screen preview — shows the top edge + total count
        stars = "🔥" if best_ep >= 10 else "⚡" if best_ep >= 7 else "📈"
        extra = f" (+{len(group_edges)-1} more)" if len(group_edges) > 1 else ""
        clv_tag = " (½ CLV)" if clv_mults.get(best.get("mkt_type",""), 1.0) == 0.5 else ""
        content = (f"{stars} **{best.get('matchup','')}**{extra}  "
                   f"+{best_ep}% edge | Kelly **${best_kelly_bet:.0f}**{clv_tag}")

        # Embed color driven by best edge in group
        color = 0x00c853 if best_ep >= 10 else 0xffe57a if best_ep >= 7 else 0x2979ff
        conf_label = ("★★★ HIGH CONF" if best.get("confidence", 0) >= 0.80
                      else "★★ MED CONF" if best.get("confidence", 0) >= 0.50
                      else "★ LOW CONF")

        # Build one field block per market in the group
        market_fields = []
        for e in group_edges:
            ep        = round(e.get("edge_pct", 0), 1)
            k         = e.get("kalshi", 0.5)
            fair_p    = e.get("fair")
            side      = e.get("side", "?")
            mtype     = e.get("mkt_type", "").upper()
            kf        = _sms_kelly(e)
            kb        = round(kf * PERF_BANKROLL, 2)
            kp        = round(kf * 100, 2)
            ka        = _prob_to_american_str(k)
            fa        = _prob_to_american_str(fair_p)
            ct        = " (½ CLV)" if clv_mults.get(e.get("mkt_type",""), 1.0) == 0.5 else ""
            market_fields.append({
                "name":   f"[{mtype}] {e.get('title','')}  —  {side}",
                "value":  (f"`+{ep}%` adj EV  •  Kelly `${kb:.0f}` ({kp}%){ct}\n"
                           f"Kalshi `{ka}` ({round(k*100)}¢)  •  Fair `{fa}`"),
                "inline": False,
            })

        embed = {
            "color":       color,
            "author":      {"name": f"Kalshi EV Scanner  •  {ts}"},
            "title":       best.get("matchup", ""),
            "description": f"{conf_label}  •  {len(group_edges)} market{'s' if len(group_edges)>1 else ''} flagged",
            "fields":      market_fields,
            "footer":      {"text": "⚠ Prices from scan time — verify live before betting  •  Pinnacle fair value"},
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }

        ok = send_discord(embed, content)
        if ok:
            for e in group_edges:
                key = _edge_key(e)
                _alerted_keys.add(key)
            _save_alerted_keys(_alerted_keys, _gone_alerted_keys)

    # ── Follow-up: alert when a previously flagged edge is gone ──────────────
    # Fires once per edge when it drops out of the current scan results,
    # giving you a heads-up before you place the bet.
    current_keys = {_edge_key(e) for e in game_edges}
    for key in list(_alerted_keys):
        if key in _gone_alerted_keys:
            continue   # already sent the correction notice
        if key in current_keys:
            continue   # edge still live

        # Edge was alerted but is no longer in current scan — line corrected.
        parts = key.split("|")
        matchup, title, side = parts[0], parts[1], parts[2]

        # Check if it was Pinnacle-invalidated (line moved) or just fell below threshold
        with _edge_history_lock:
            hist = next(
                (h for k2, h in _edge_price_history.items() if k2.startswith(f"{matchup}|")),
                None,
            )
        _last_pin  = hist.get("last_pin_pct")  if hist else None
        _first_pin = hist.get("first_pin_pct") if hist else None
        pin_shift  = (_last_pin - _first_pin) if (_last_pin is not None and _first_pin is not None) else None
        if pin_shift is not None and hist.get("first_pin_pct") is not None:
            reason = f"Pinnacle shifted {pin_shift:+.1f}pp" if abs(pin_shift) >= 1 else "Kalshi price corrected"
        else:
            reason = "Edge no longer qualifies"

        ts = datetime.now().strftime("%I:%M %p")
        content_gone = f"🚫 **EDGE GONE** — {title} ({side}) | {reason}"
        embed_gone = {
            "color": 0xf85149,   # red
            "author": {"name": f"Kalshi EV Scanner  •  {ts}  •  LINE CORRECTED"},
            "title": matchup,
            "description": f"**{title}**  —  Side: **{side}**",
            "fields": [
                {"name": "Status",  "value": "❌ Edge no longer +EV — do not bet", "inline": False},
                {"name": "Reason",  "value": reason,                                "inline": False},
            ],
            "footer": {"text": "This fires once when a flagged edge disappears from the scan"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        ok = send_discord(embed_gone, content_gone)
        if ok:
            _gone_alerted_keys.add(key)
            _save_alerted_keys(_alerted_keys, _gone_alerted_keys)


def _alert_min_threshold() -> float:
    return _ALERT_MIN


def send_test_discord() -> bool:
    """Fire a test Discord embed with a dummy edge — call this to verify the webhook."""
    ts = datetime.now().strftime("%I:%M %p")
    content = "⚡ **+6.1% edge** | Kelly **$23** (2.3%)  ← TEST ALERT"
    embed = {
        "color": 0x2979ff,
        "author": {"name": f"Kalshi EV Scanner  •  {ts}  •  TEST"},
        "title": "Atlanta Braves @ Arizona Diamondbacks",
        "description": "**Total Runs Over 8.5**  —  Side: **YES**",
        "fields": [
            {"name": "Adj. Edge",    "value": "`+6.1%`",           "inline": True},
            {"name": "Kelly Stake",  "value": "`$23` (2.3%)",       "inline": True},
            {"name": "Confidence",   "value": "★★ MED CONF",        "inline": True},
            {"name": "Kalshi Price", "value": "`-120`  (55¢)",      "inline": True},
            {"name": "Fair Value",   "value": "`-138`",             "inline": True},
            {"name": "Books",        "value": "PIN+DK+FD",          "inline": True},
        ],
        "footer": {"text": f"Bankroll $1000  •  Min edge {round(_ALERT_MIN*100,1)}%  •  Pinnacle fair value · DK+FD confirm"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return send_discord(embed, content)


def _send_zero_edge_alert(hours: float) -> None:
    """Fire a Discord health-check alert when no edges found for `hours` hours."""
    ts = datetime.now().strftime("%-I:%M %p")
    embed = {
        "color": 0xFF4444,   # red
        "author": {"name": f"Kalshi EV Scanner  •  {ts}  •  HEALTH CHECK"},
        "title": "⚠️ No edges found in the last {:.0f} hours".format(hours),
        "description": (
            "The scanner has been running but found **zero qualifying edges** "
            "for the past **{:.0f} hours** during game hours.\n\n"
            "This likely means a **silent data pipeline failure** — "
            "not a market condition. Common causes:\n"
            "• Odds API returning an error (bad market param, quota, timeout)\n"
            "• Kalshi API auth failure\n"
            "• All Pinnacle lines failing to match any Kalshi threshold\n\n"
            "Check the scanner logs immediately."
        ).format(hours),
        "footer": {"text": "Alert fires once per drought — resets when next edge is found"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    content = f"⚠️ Scanner health check: no edges in {hours:.0f}h — possible pipeline failure"
    ok = send_discord(embed, content)
    print(f"  Zero-edge alert sent (streak={_zero_edge_streak} scans / {hours:.1f}h): {'✓' if ok else 'FAILED'}")


def _send_scan_stale_alert(minutes: float) -> None:
    """Fire a Discord alert when the scan thread appears hung — no scan in `minutes` min."""
    ts = datetime.now().strftime("%-I:%M %p")
    embed = {
        "color": 0xE74C3C,   # red
        "author": {"name": f"Kalshi EV Scanner  •  {ts}  •  THREAD WATCHDOG"},
        "title": f"🚨 Scan thread hung — {minutes:.0f} min without a scan",
        "description": (
            f"The scan thread has **not completed a scan in {minutes:.0f} minutes** "
            f"during game hours, but `is_alive()` returns True.\n\n"
            f"This is a **hung thread** — blocked on a network call that never timed out "
            f"(Mac sleep / stalled Kalshi or Odds API connection).\n\n"
            f"**Auto-restarting the scan thread now.**\n\n"
            f"You may miss edges during this window. Check scanner logs."
        ),
        "fields": [
            {"name": "Common causes", "value":
             "• Mac went to sleep during a scan cycle\n"
             "• Kalshi API connection hung (no timeout triggered)\n"
             "• Network reconnected after long offline period\n"
             "• Odds API response stalled mid-stream", "inline": False},
        ],
        "footer": {"text": "Watchdog checks staleness every 60s — fires once per hung episode"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    content = f"🚨 Scan thread hung {minutes:.0f}min — watchdog auto-restarting"
    ok = send_discord(embed, content)
    print(f"  Scan-stale alert sent ({minutes:.0f} min since last scan): {'✓' if ok else 'FAILED'}")


def _send_auth_failed_alert(status_code: int) -> None:
    """Fire a Discord alert when Kalshi returns 401/403 — API key or private key issue."""
    ts = datetime.now().strftime("%-I:%M %p")
    embed = {
        "color": 0xE74C3C,
        "author": {"name": f"Kalshi EV Scanner  •  {ts}  •  AUTH FAILURE"},
        "title": f"🔑 Kalshi API auth failed (HTTP {status_code})",
        "description": (
            f"Every Kalshi API call is returning **HTTP {status_code}**.\n\n"
            "The scanner is running but **cannot fetch any markets** — "
            "no edges will be found until this is fixed.\n\n"
            "**Possible causes:**\n"
            "• `KALSHI_API_KEY` env var is missing or wrong\n"
            "• `mannyxolo.txt` private key file is missing or corrupted\n"
            "• Kalshi rotated your API credentials\n"
            "• Clock skew on the server (signature timestamp rejected)"
        ),
        "footer": {"text": "Fires once per auth-failure episode — clears when scans resume"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    content = f"🔑 Kalshi auth failed (HTTP {status_code}) — scanner cannot fetch markets"
    ok = send_discord(embed, content)
    print(f"  Auth-failed alert sent (HTTP {status_code}): {'✓' if ok else 'FAILED'}")


def _send_odds_stale_alert(minutes: float) -> None:
    """Fire a Discord alert when the odds index hasn't refreshed in >2 hours."""
    ts = datetime.now().strftime("%-I:%M %p")
    embed = {
        "color": 0xFF9800,   # orange
        "author": {"name": f"Kalshi EV Scanner  •  {ts}  •  ODDS STALE"},
        "title": f"⚠️ Odds index stale — {minutes:.0f} min since last refresh",
        "description": (
            f"The Odds API index hasn't updated in **{minutes:.0f} minutes** during game hours.\n\n"
            "Edges may be based on stale book lines. Common causes:\n"
            "• Odds API monthly quota exhausted (check your dashboard)\n"
            "• Odds API returned an error / rate-limited\n"
            "• `odds` background thread is hung on a slow request"
        ),
        "footer": {"text": "Fires once per stale episode — clears when odds refresh resumes"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    content = f"⚠️ Odds index stale {minutes:.0f}min — possible Odds API quota/error"
    ok = send_discord(embed, content)
    print(f"  Odds-stale alert sent ({minutes:.0f} min since last refresh): {'✓' if ok else 'FAILED'}")


def _run_odds_refresh():
    """
    Fetch fresh Pinnacle odds (MLB only — NBA faded) and update the cached index.
    MLB: spreads + totals + alternate_spreads + alternate_totals = 4 credits/call.
    1-min peak / 10-min off-peak.  ~3,132 credits/day on 100k plan.
    """
    global _cached_mlb_index, _cached_nba_index, _last_odds_refresh, \
           _last_odds_cache_success, _odds_game_count
    print(f"\n  ── Odds index refresh  {datetime.now().strftime('%H:%M:%S')} ──")

    try:
        mlb_idx, _ = fetch_odds_index(
            "baseball_mlb", total_range=(5.0, 14.0), spread_limit=3.0
        )
        if mlb_idx is not None:
            n_games = len(mlb_idx) // max(1, 2)
            with _odds_cache_lock:
                _cached_mlb_index       = mlb_idx
                _last_odds_cache_success = time.time()
                _odds_game_count        = n_games
            print(f"  MLB index cached: {n_games} matchups")
        else:
            print("  WARNING: fetch_odds_index returned None — Pinnacle cache not updated")
    except Exception as exc:
        print(f"  ERROR refreshing MLB odds index: {exc}")
        # On 401 (credits exhausted), wipe the cache — stale odds produce phantom
        # edges when compared against current Kalshi prices (especially in-game).
        if "401" in str(exc):
            with _odds_cache_lock:
                _cached_mlb_index = None
            print("  MLB odds cache cleared — will not scan until credits restore")

    # NBA odds fetch removed — NBA faded permanently 2026-05-26; credits reallocated to 1-min MLB scans

    with _odds_cache_lock:
        _last_odds_refresh = time.time()


def _background_odds_loop():
    """Refresh book-odds cache on an adaptive schedule (costs 2 credits/refresh — spreads+totals)."""
    # On Railway, give the HTTP server 10 s to start accepting connections before
    # making any outbound network calls — keeps cold-start inside the healthcheck window.
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        time.sleep(10)
    try:
        _run_odds_refresh()      # run immediately so first Kalshi scan has data
    except Exception as exc:
        print(f"  Odds refresh (initial) error: {exc}")
    while True:
        time.sleep(_odds_refresh_interval())
        try:
            _run_odds_refresh()
        except Exception as exc:
            print(f"  Odds refresh loop error: {exc}")


def _run_scan():
    """
    Main scan cycle.

    SOURCE OF TRUTH: Pinnacle line only.
      • Gaussian model removed.
      • DraftKings / FanDuel confirmation steps removed.
      • Edges are invalidated when Pinnacle's no-vig probability shifts ≥ 2 pp
        from the value recorded on the first scan the edge was seen ("pin_first_pct").
        This catches market corrections before we log stale bets.

    Staleness is still tracked via Kalshi price drift (≥ 5 pp) as a secondary
    signal, but the primary invalidation gate is the Pinnacle line shift.
    """
    # Pinnacle-line shift threshold for invalidating an edge (percentage points)
    PIN_SHIFT_THRESHOLD = 2.0   # pp — Pinnacle moved ≥2 pp → edge is stale

    with _lock:
        _state["scanning"] = True
        _state["error"]    = None

    try:
        # Read cached indices (populated by _background_odds_loop).
        # IMPORTANT: if the cache is still cold (background loop hasn't finished
        # its first refresh), we skip this scan cycle entirely rather than letting
        # scan_sport fall back to a live fetch.  That fallback costs 1 credit every
        # 2 minutes — ~720 credits/day — with zero detection benefit over waiting
        # for the scheduled refresh (which runs within seconds of startup).
        with _odds_cache_lock:
            mlb_idx = _cached_mlb_index
            nba_idx = _cached_nba_index

        if mlb_idx is None:
            print("  Odds cache cold — skipping scan cycle (background refresh in progress)")
            with _lock:
                _state["scanning"] = False
            return

        # MLB spreads + totals — ML removed (not used for fair-value)
        mlb, mlb_stats, mlb_snapshot = scan_sport(
            label="MLB — Spread & Totals",
            spread_series="KXMLBSPREAD",
            total_series="KXMLBTOTAL",
            ml_series=None,
            odds_sport="baseball_mlb",
            abbr_map=MLB_ABBR,
            spread_std=MLB_SPREAD_STD,
            total_std=MLB_TOTAL_STD,
            game_index=mlb_idx,
        )

        # NBA faded permanently — credits reallocated to faster MLB scans
        nba = []
        nba_stats = {}

        # Player props — MLB only, interval set by _props_refresh_interval() per PDT window
        global _last_props_scan
        now_ts = time.time()
        if now_ts - _last_props_scan >= _props_refresh_interval():
            try:
                mlb_props = scan_player_props(odds_sport="baseball_mlb", abbr_map=MLB_ABBR)
            except Exception as _prop_exc:
                print(f"  Props scan error: {_prop_exc}")
                mlb_props = []
            _last_props_scan = now_ts
        else:
            mlb_props = []

        all_edges = sorted(mlb + nba + mlb_props, key=lambda x: x["edge"], reverse=True)

        # Deduplicate: keep only best edge per (matchup, mkt_type, side)
        edges = _best_edge_per_game(all_edges)

        # Secondary dedup: normalize matchup name to catch whitespace/case differences
        # across multiple Kalshi event tickers for the same game.
        import re as _re2
        _seen_norm = set()
        _deduped = []
        for _e in edges:
            _norm_key = (
                _re2.sub(r"[^a-z0-9]", "", _e.get("matchup","").lower()),
                _e.get("mkt_type",""),
                _e.get("side",""),
            )
            if _norm_key not in _seen_norm:
                _seen_norm.add(_norm_key)
                _deduped.append(_e)
        edges = _deduped

        # ── Per-edge: compute derived fields + Pinnacle line-movement tracking ─
        for e in edges:
            e["kalshi_pct"] = round(e["kalshi"] * 100, 1)
            e["fair_pct"]   = round(e["fair"]   * 100, 1)
            e["edge_pct"]   = round(e["edge"]   * 100, 1)

            # ── Pinnacle probability for this edge's side ──────────────────
            bd = e.get("books_detail", {})
            pin_raw = bd.get("pinnacle")   # no-vig prob for YES side

            if pin_raw is not None:
                # Convert to the probability for the side we're betting
                pin_side_pct = round(
                    (pin_raw if e["side"] == "YES" else 1.0 - pin_raw) * 100, 1
                )
                e["pin_prob_pct"]      = round(pin_raw * 100, 1)
                e["pin_side_pct"]      = pin_side_pct   # pin prob aligned to our bet side
                e["projected_clv_pct"] = round(pin_side_pct - e["kalshi_pct"], 1)
            else:
                pin_side_pct           = None
                e["pin_prob_pct"]      = None
                e["pin_side_pct"]      = None
                e["projected_clv_pct"] = None

            # ── Pinnacle line-movement tracking (primary invalidation gate) ─
            ek = _edge_key(e)
            with _edge_history_lock:
                hist = _edge_price_history.get(ek)

                if hist is None:
                    # First time we see this edge — record Pinnacle baseline
                    _edge_price_history[ek] = {
                        "first_kalshi_pct": e["kalshi_pct"],
                        "first_pin_pct":    pin_side_pct,   # may be None if PIN unavailable
                        "first_ts":         now_ts,
                    }
                    hist = _edge_price_history[ek]

                hist["last_kalshi_pct"] = e["kalshi_pct"]
                hist["last_pin_pct"]    = pin_side_pct
                hist["last_ts"]         = now_ts

                age_sec = now_ts - hist["first_ts"]
                e["age_min"] = round(age_sec / 60, 1)

                # ── Kalshi drift (secondary signal) ────────────────────────
                kalshi_drift = e["kalshi_pct"] - hist["first_kalshi_pct"]
                if e["side"] == "NO":
                    kalshi_drift = -kalshi_drift   # rising YES = bad for NO
                e["drift_pct"] = round(kalshi_drift, 1)
                e["stale"]     = kalshi_drift >= 5.0   # kept for UI display

                # ── Pinnacle line shift (primary invalidation) ─────────────
                # Compare current Pinnacle prob to the baseline recorded at
                # first detection.  A shift ≥ PIN_SHIFT_THRESHOLD pp means
                # the sharp market has moved against the edge; discard it.
                first_pin = hist.get("first_pin_pct")
                if pin_side_pct is not None and first_pin is not None:
                    pin_shift = pin_side_pct - first_pin  # + = line moved our way
                    e["pin_shift_pct"] = round(pin_shift, 1)
                    # Invalidated when Pinnacle moved AWAY from our bet by ≥ threshold
                    e["pin_invalidated"] = (pin_shift <= -PIN_SHIFT_THRESHOLD)
                    if e["pin_invalidated"]:
                        e["invalidation_reason"] = (
                            f"Pinnacle shifted {abs(pin_shift):.1f}pp against "
                            f"our {e['side']} — line moved from "
                            f"{first_pin:.1f}% → {pin_side_pct:.1f}%"
                        )
                else:
                    # No Pinnacle data — can't confirm the edge; mark invalid
                    e["pin_shift_pct"]    = None
                    e["pin_invalidated"]  = (pin_side_pct is None)
                    e["invalidation_reason"] = (
                        "No Pinnacle line available — cannot confirm edge"
                        if pin_side_pct is None else ""
                    )

        # ── Invalidation filter (Pinnacle-led) ────────────────────────────────
        # Remove edges where Pinnacle has shifted against us OR is unavailable.
        # We keep stale-Kalshi edges visible (shown in UI with a warning) but
        # hard-drop Pinnacle-invalidated ones — they are not real edges.
        valid_edges = [e for e in edges if not e.get("pin_invalidated")]
        invalidated = [e for e in edges if e.get("pin_invalidated")]
        if invalidated:
            print(f"  Invalidated {len(invalidated)} edge(s) by Pinnacle line shift: "
                  + ", ".join(e["title"] for e in invalidated))
        edges = valid_edges

        # ── Recency-premium sort ──────────────────────────────────────────────
        # Fresh edges (< 5 min) get a 20% boost so they bubble above older
        # equal-confidence edges.  Confidence × adjusted EV is the base score.
        def _sort_score(e):
            fresh_mult = 1.2 if e["age_min"] < 5 else 1.0
            return fresh_mult * e.get("confidence", 0.5) * e["edge_pct"]
        edges.sort(key=_sort_score, reverse=True)

        now_iso = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "ts":         now_iso,
            "edge_count": len(edges),
            "top_edge":   round(edges[0]["edge_pct"], 1) if edges else 0,
            "edges": [
                {"matchup": e["matchup"], "title": e["title"],
                 "side": e["side"], "edge_pct": e["edge_pct"],
                 "mkt_type": e["mkt_type"]}
                for e in edges
            ],
        }
        _history.append(snapshot)
        _save_history(_history)

        with _lock:
            _state["edges"]           = edges
            if edges:
                _state["edges_cache"] = edges   # persist last non-empty result for quiet windows
            _state["last_scan"]       = now_iso
            _state["scanning"]        = False
            _state["last_scan_stats"] = mlb_stats   # diagnostic counters for /api/scan

            # ── Build full market snapshot: game lines + props ────────────────
            # scan_sport() already populates mlb_snapshot for game lines.
            # Props come from scan_player_props() which runs on its own slower
            # interval — mlb_props is [] on non-prop cycles.  We persist the
            # last prop snapshot in _last_prop_snapshot so the UI stays populated
            # between prop scan cycles (every 8–10 min) rather than blanking out
            # every 30 s when mlb_props is empty.
            #
            # Source: all_edges (the merged, deduped list) rather than mlb_props
            # directly, so any future edge source added to all_edges is
            # automatically included here with no extra changes needed.
            global _last_prop_snapshot
            if mlb_props:   # fresh prop scan this cycle — rebuild prop snapshot
                _last_prop_snapshot = {}
                for _pe in all_edges:
                    if _pe.get("mkt_type") != "prop":
                        continue
                    _ticker = _pe.get("ticker", "")
                    _side   = _pe.get("side", "")
                    if not _ticker or not _side:
                        continue
                    _adj_edge = _pe.get("edge", 0.0)
                    _kalshi   = _pe.get("kalshi", 0.0)
                    _fair     = _pe.get("fair", 0.0)
                    _last_prop_snapshot[f"{_ticker}|{_side}"] = {
                        "adj_edge":   round(_adj_edge, 4),
                        "kalshi":     round(_kalshi, 4),
                        "fair":       round(_fair, 4),
                        "edge_pct":   round(_adj_edge * 100, 1),
                        "pin_line":   _pe.get("pin_line"),
                        "kalshi_line": _pe.get("kalshi_line"),
                    }

            # Merge: game-line snapshot + last known prop snapshot.
            # Game lines take precedence on key collision (shouldn't happen).
            _state["market_snapshot"] = {**_last_prop_snapshot, **mlb_snapshot}

        # Log new bets and capture what was just added for the alert
        newly_logged = _add_new_bets(edges)

        # ── Zero-edge drought check ───────────────────────────────────────────
        pass  # zero-edge health check removed — scanner stability confirmed

    except Exception as exc:
        newly_logged = []
        with _lock:
            _state["error"]    = str(exc)
            _state["scanning"] = False

        # ── Kalshi auth failure detection ─────────────────────────────────────
        global _kalshi_auth_failed, _kalshi_auth_alerted
        exc_str = str(exc)
        is_auth_error = ("401" in exc_str or "403" in exc_str or
                         "Unauthorized" in exc_str or "Forbidden" in exc_str)
        if is_auth_error:
            _kalshi_auth_failed = True
            if not _kalshi_auth_alerted:
                _kalshi_auth_alerted = True
                status = 401 if "401" in exc_str else 403
                _send_auth_failed_alert(status)
        else:
            # Non-auth error — clear auth flags so a real auth failure later still fires
            _kalshi_auth_failed   = False
            _kalshi_auth_alerted  = False

    # ── Alert only on bets that were just logged this cycle ──────────────────
    # Notification and paper portfolio are now always in sync.
    _alert_top10(newly_logged)

    # ── Odds staleness check (runs every scan cycle, outside the try block) ──
    global _odds_stale_alerted
    in_game_hours_now = 10 <= _et_hour() <= 20
    with _odds_cache_lock:
        odds_age_min = (time.time() - _last_odds_refresh) / 60 if _last_odds_refresh else None
    if odds_age_min is not None and in_game_hours_now:
        if odds_age_min >= _ODDS_STALE_MINUTES and not _odds_stale_alerted:
            _odds_stale_alerted = True
            _send_odds_stale_alert(odds_age_min)
        elif odds_age_min < _ODDS_STALE_MINUTES:
            _odds_stale_alerted = False   # reset when odds refresh resumes

    # ── Prune _edge_price_history to prevent memory growth ───────────────────
    # Keep only entries seen in the last 48 hours
    _EDGE_HISTORY_TTL = 48 * 3600
    now_ts = time.time()
    with _edge_history_lock:
        stale_keys = [k for k, v in _edge_price_history.items()
                      if now_ts - v.get("last_ts", now_ts) > _EDGE_HISTORY_TTL]
        for k in stale_keys:
            del _edge_price_history[k]
    if stale_keys:
        print(f"  Pruned {len(stale_keys)} stale edge price history entries")

    # Persist Pinnacle prices so they survive restarts
    _save_pin_prices()


def _background_loop():
    while True:
        try:
            _run_scan()
        except Exception as exc:
            print(f"  Scan loop error: {exc}")
            traceback.print_exc()
        time.sleep(REFRESH_SECONDS)



RESOLUTION_POLL_SECONDS  = 5 * 60   # check for settled games every 5 minutes
CLV_CAPTURE_SECONDS      = 60       # refresh closing prices for open bets every 60 sec
PRE_CLOSE_WINDOW_MINUTES = 45       # outer window: start capturing Pinnacle within 45 min of gametime
PRE_CLOSE_FINAL_MINUTES  = 10       # inner window: re-capture within 10 min for the true closing line

# Two-stage pre-close capture:
#   Stage 1 (early)  — fires when 10 < mins_to_start ≤ 45. Gets a baseline.
#   Stage 2 (final)  — fires when mins_to_start ≤ 10. Overwrites with the
#                       truest available closing line, closest to first pitch.
# Tracking sets are session-only; Railway restarts reset them (acceptable —
# the CLV loop will pick up last_pin_pct from edge history as a fallback).
_pre_close_early_done: set = set()    # bet IDs that completed stage-1 capture
_pre_close_final_done: set = set()    # bet IDs that completed stage-2 (final) capture
_odds_refresh_lock = threading.Lock()  # prevents concurrent Pinnacle fetches across threads


def _norm_matchup(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _lookup_pin_prob_for_bet(bet: dict, game_idx: dict) -> Optional[float]:
    """
    Extract Pinnacle's current no-vig probability for a bet's side from a game index.
    Returns the probability as a percentage (e.g. 47.8), or None if not found.

    Line-matching priority for totals (prevents stale-line CLV mismatch):
      1. pin_line_at_flag — the exact Pinnacle line active when the edge was detected.
         If Pinnacle still posts odds for that specific hook (e.g. 7.5 even after the
         main line moved to 8.5), we use those odds so CLV measures the same threshold
         we entered at.  This is the fix for the "line moved from 7.5→8.5" audit finding.
      2. Kalshi-threshold-derived line — parse ticker suffix (e.g. -8 → 8.0, implies
         Pinnacle 7.5) as a secondary target when pin_line_at_flag is absent (bets
         flagged before today's deploy won't have it stored).
      3. Pinnacle main line — current posted line regardless of what we entered at.
         Only used when neither #1 nor #2 finds a match in pin_lines.
      4. Consensus probability — last resort when Pinnacle per_book is absent.

    Logs a warning when the entry line is no longer in Pinnacle's alternate array
    so we can audit cases where the closing reference had to fall back.
    """
    if not game_idx:
        return None

    import re as _re2

    mkt_type = bet.get("mkt_type", "total")
    side     = bet.get("side", "YES")
    matchup  = bet.get("matchup", "")
    ticker   = bet.get("ticker", "")

    # Match game by normalised away+home team names
    game_info = None
    norm_matchup = _norm_matchup(matchup)
    for info in game_idx.values():
        away = _norm_matchup(info.get("away", ""))
        home = _norm_matchup(info.get("home", ""))
        if away and home and away in norm_matchup and home in norm_matchup:
            game_info = info
            break
    if game_info is None:
        return None

    if mkt_type in ("total", ""):
        total_info = game_info.get("total", {})
        if not total_info:
            return None

        pin_lines = total_info.get("pin_lines", {})  # {float_pt: {over_prob, under_prob}}

        def _prob_from_pin_lines(target_line: float) -> Optional[float]:
            """Look up over/under prob for target_line in pin_lines (tolerance ±0.26)."""
            for pt, probs in pin_lines.items():
                if abs(float(pt) - target_line) <= 0.26:
                    prob = probs.get("over_prob") if side == "YES" else probs.get("under_prob")
                    if prob is not None:
                        return round(prob * 100, 1)
            return None

        # ── Priority 1: pin_line_at_flag (exact entry-time Pinnacle line) ──────
        # This is the critical fix — ensures CLV is always measured at the hook
        # we actually entered, even if Pinnacle's main line has since moved.
        pin_line_at_flag = bet.get("pin_line_at_flag")
        if pin_line_at_flag is not None and pin_lines:
            result = _prob_from_pin_lines(float(pin_line_at_flag))
            if result is not None:
                return result
            # Entry line no longer in Pinnacle's alternate array — log and fall through
            print(f"  CLV lookup: entry line {pin_line_at_flag} not in PIN alternates "
                  f"for {matchup} ({side}) — falling back to Kalshi-threshold method. "
                  f"Available PIN lines: {sorted(pin_lines.keys())}")

        # ── Priority 2: Kalshi-threshold-derived Pinnacle line ─────────────────
        # Parse ticker suffix (e.g. KXMLBTOTAL-...-8 → threshold=8.0).
        # Kalshi floor N maps to Pinnacle line N-0.5 (both require >N-0.5 runs).
        # Try both the direct value and the N-0.5 convention.
        threshold = None
        m = _re2.search(r"-(\d+(?:\.\d+)?)$", ticker)
        if m:
            threshold = float(m.group(1))

        if threshold is not None and pin_lines:
            # Try Kalshi-floor convention (N → Pinnacle N-0.5) first, then direct
            for target in (threshold - 0.5, threshold):
                result = _prob_from_pin_lines(target)
                if result is not None:
                    return result

        # ── Priority 3: Pinnacle per_book main line ────────────────────────────
        # Current posted main line — may differ from entry line if market moved.
        # Only reached when neither entry line nor Kalshi threshold found in alternates.
        per_book = total_info.get("per_book", {})
        pin_book = per_book.get("pinnacle", {})
        if pin_book:
            prob = pin_book.get("over_prob") if side == "YES" else pin_book.get("under_prob")
            if prob is not None:
                if pin_line_at_flag is not None:
                    main_pt = pin_book.get("over_point", "?")
                    print(f"  CLV lookup: using PIN main line {main_pt} (entry was "
                          f"{pin_line_at_flag}) for {matchup} {side} — line moved, "
                          f"CLV will be approximate")
                return round(prob * 100, 1)

        # ── Priority 4: Consensus (last resort) ────────────────────────────────
        prob = total_info.get("over_prob") if side == "YES" else total_info.get("under_prob")
        return round(prob * 100, 1) if prob is not None else None

    return None   # spread/moneyline pre-close not yet implemented


def _maybe_fetch_pre_close_pinnacle():
    """
    If any open bet is within PRE_CLOSE_WINDOW_MINUTES of gametime and hasn't had
    a pre-close Pinnacle fetch this session, fetch fresh Pinnacle odds for that sport
    and update closing_pin_pct + _edge_price_history before CLV is frozen.

    Costs 1 Odds API credit per sport needed (max 2). Thread-safe — skips if the
    regular odds loop is already fetching (data will be fresh enough).
    """
    global _pre_close_early_done, _pre_close_final_done, _cached_mlb_index, _cached_nba_index

    now_utc = datetime.now(timezone.utc)
    with _bets_lock:
        open_bets = [b for b in _bets if b["status"] == "open" and not b.get("clv_frozen")]

    if not open_bets:
        return

    # Identify which bets need a pre-close fetch and which sports are needed.
    # Two-stage: early capture (10–45 min out) then final capture (≤10 min out).
    # A bet gets a second fetch when it crosses into the final window, overwriting
    # the early snapshot with the truest available closing line.
    bets_to_refresh: List[dict] = []
    sports_needed:   set        = set()

    for bet in open_bets:
        game_start = _parse_ticker_start_time(bet.get("ticker", ""))
        if game_start is None:
            gt = bet.get("game_time")
            if gt:
                try:
                    game_start = datetime.fromisoformat(gt.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass
        if game_start is None:
            continue

        mins_to_start = (game_start - now_utc).total_seconds() / 60

        # Outside capture window entirely — skip
        if mins_to_start > PRE_CLOSE_WINDOW_MINUTES or mins_to_start < -5.0:
            continue

        in_final_window = mins_to_start <= PRE_CLOSE_FINAL_MINUTES
        bid = bet["id"]

        if in_final_window:
            # Stage 2: final capture — skip only if already done the final fetch
            if bid in _pre_close_final_done:
                continue
        else:
            # Stage 1: early capture — skip if either stage already done
            if bid in _pre_close_early_done or bid in _pre_close_final_done:
                continue

        ticker = bet.get("ticker", "").upper()
        sport  = "basketball_nba" if ticker.startswith("KXNBA") else "baseball_mlb"
        sports_needed.add(sport)
        bets_to_refresh.append(bet)

    if not bets_to_refresh:
        return

    # Non-blocking — if odds loop holds the lock, our cache is already fresh
    if not _odds_refresh_lock.acquire(blocking=False):
        print("  Pre-close: odds lock held, skipping (regular refresh in progress)")
        return

    try:
        # Fetch fresh Pinnacle data — MLB only (NBA removed for credit conservation)
        fresh_indices: dict = {}
        for sport in sports_needed:
            if sport != "baseball_mlb":
                continue   # NBA removed
            try:
                print(f"  Pre-close Pinnacle fetch: {sport} (closing line capture)")
                idx, _ = fetch_odds_index("baseball_mlb", total_range=(5.0, 14.0), spread_limit=3.0)
                if idx is not None:
                    fresh_indices[sport] = idx
                    with _odds_cache_lock:
                        _cached_mlb_index = idx
            except Exception as exc:
                print(f"  Pre-close fetch error ({sport}): {exc}")
                # On credit exhaustion, fall through — use last_pin_pct from history

        # For each bet, look up Pinnacle close and write it
        bets_updated = 0
        for bet in bets_to_refresh:
            ticker  = bet.get("ticker", "").upper()
            sport   = "basketball_nba" if ticker.startswith("KXNBA") else "baseball_mlb"
            game_idx = fresh_indices.get(sport)

            pin_prob = _lookup_pin_prob_for_bet(bet, game_idx) if game_idx else None

            if pin_prob is None:
                # Pinnacle suspended this market or game not found — use last known
                ek = _edge_key(bet)
                with _edge_history_lock:
                    hist = _edge_price_history.get(ek, {})
                    pin_prob = hist.get("last_pin_pct")
                if pin_prob is not None:
                    print(f"  Pre-close: {bet.get('matchup','')} — Pinnacle unavailable, using last known {pin_prob}%")

            # Update edge history with fresh/fallback Pinnacle close
            ek = _edge_key(bet)
            with _edge_history_lock:
                hist = _edge_price_history.setdefault(ek, {})
                if pin_prob is not None:
                    hist["last_pin_pct"]      = pin_prob
                    hist["pre_close_pin_pct"] = pin_prob   # debug audit trail

            # Write closing_pin_pct and CLV to bet record
            with _bets_lock:
                for b in _bets:
                    if b["id"] == bet["id"]:
                        if pin_prob is not None:
                            entry_k = b.get("kalshi_price", 0) * 100
                            b["closing_pin_pct"] = pin_prob
                            b["clv_source"]      = "pin"
                            if entry_k:
                                _clv = round(pin_prob - entry_k, 1)
                                b["clv"]     = _clv
                                b["clv_pct"] = _clv_pct(_clv, entry_k)
                        break

            # Mark which stage completed so the two-stage logic advances correctly
            game_start_chk = _parse_ticker_start_time(bet.get("ticker", ""))
            if game_start_chk is None:
                gt = bet.get("game_time")
                if gt:
                    try:
                        game_start_chk = datetime.fromisoformat(gt.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        pass
            mins_chk = (game_start_chk - now_utc).total_seconds() / 60 if game_start_chk else 999
            if mins_chk <= PRE_CLOSE_FINAL_MINUTES:
                _pre_close_final_done.add(bet["id"])
                stage_label = "FINAL"
            else:
                _pre_close_early_done.add(bet["id"])
                stage_label = "EARLY"
            bets_updated += 1
            print(f"  Pre-close CLV [{stage_label}] {bet.get('matchup','')} | "
                  f"{bet.get('side','')} | PIN close={pin_prob}% | {mins_chk:.0f} min to game")

        if bets_updated:
            with _bets_lock:
                _save_bets(_bets)

    finally:
        _odds_refresh_lock.release()


def _capture_clv_prices():
    """
    Continuously refresh the 'closing' Kalshi price for every open bet.

    We call the Kalshi API for each open bet and overwrite `closing_yes_pct`
    with the current mid-price.  The last write before the market resolves
    becomes the true closing line — which is what CLV measures.

    Strategy:
      • While market status == "open" (pre-game): always update.  This is
        the true closing line — the last pre-game price.
      • Once status changes (game started / market closed): STOP updating.
        The last captured pre-game price is the closing line.  In-game prices
        reflect live game state and would produce wildly wrong CLV.
      • If a bet was flagged close to game time and we never captured a
        pre-game price, the initial closing_yes_pct (set to entry price at
        flag time) remains, giving CLV = 0 (neutral, not misleading).
    """
    # First: grab fresh Pinnacle close for any bet within 20 min of gametime
    try:
        _maybe_fetch_pre_close_pinnacle()
    except Exception as _pce:
        print(f"  Pre-close fetch error: {_pce}")

    with _bets_lock:
        open_bets = [b for b in _bets if b["status"] == "open"]

    if not open_bets:
        return

    updated = 0
    for bet in open_bets:
        # Skip bets where game has already started — CLV is frozen
        if bet.get("clv_frozen"):
            continue
        try:
            time.sleep(0.15)          # gentle rate-limit (~6–7 calls/sec max)
            data = kalshi_get(f"/markets/{bet['ticker']}")
            mkt  = data.get("market", {})

            # Skip already-resolved markets
            if mkt.get("result"):
                continue

            status = mkt.get("status", "")
            if status not in ("active", "open"):
                continue

            # Freeze CLV at game start time — do NOT update with in-game prices.
            # Kalshi keeps markets "active" during the game, so status alone can't
            # tell us if the game has started. Parse the start time from the ticker;
            # fall back to close_time - 4.0h for NBA, 3.5h for MLB.
            game_start = _parse_ticker_start_time(bet["ticker"])
            if game_start is None:
                # Use game_time stored on the bet if available
                gt = bet.get("game_time")
                if gt:
                    try:
                        game_start = datetime.fromisoformat(gt.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        pass
            if game_start is None:
                close_str = mkt.get("close_time") or mkt.get("expected_expiration_time")
                if close_str:
                    try:
                        from datetime import timedelta as _tdc
                        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                        is_nba   = bet["ticker"].upper().startswith("KXNBA")
                        game_start = close_dt - _tdc(hours=4.0 if is_nba else 3.5)
                    except (ValueError, AttributeError):
                        pass
            if game_start and datetime.now(timezone.utc) >= game_start:
                # Game has started — closing price is already frozen from last update.
                # Mark clv_frozen so we stop calling the API for this bet.
                with _bets_lock:
                    for b in _bets:
                        if b["id"] == bet["id"] and not b.get("clv_frozen"):
                            b["clv_frozen"] = True
                            break
                continue

            # API returns prices as dollar decimals in yes_bid_dollars/yes_ask_dollars
            # (e.g. 0.33 = 33¢).  Multiply by 100 to get percentage points.
            # Fallback to yes_bid/yes_ask integer fields (0–100 scale) if dollar
            # fields are missing — same fallback logic as kalshi_prices() in scanner.
            bid_c = float(mkt.get("yes_bid_dollars") or 0) * 100
            ask_c = float(mkt.get("yes_ask_dollars") or 0) * 100
            if bid_c <= 0 or ask_c <= 0:
                bid_c = float(mkt.get("yes_bid") or 0)
                ask_c = float(mkt.get("yes_ask") or 0)
            if bid_c <= 0 or ask_c <= 0:
                continue

            # Use the side-appropriate transactable price, not the mid.
            # Entry was captured as: YES bets → YES ask, NO bets → YES bid.
            # Comparing to the mid would introduce a structural negative bias
            # equal to ~half the spread (typically 2–4 pp) on every bet.
            side = bet.get("side", "YES")
            yes_close_pct = round(ask_c if side == "YES" else bid_c, 1)

            # Safety guard: reject prices that have collapsed to in-game extremes.
            # Pre-game markets rarely close below 8¢ or above 92¢ — anything
            # beyond those bounds is almost certainly a live in-game price
            # reflecting a near-decided outcome, not a true closing line.
            # This is a backstop for the game_start guard above.
            if yes_close_pct >= 92.0 or yes_close_pct <= 8.0:
                continue

            # True CLV: compare entry Kalshi price to the last known Pinnacle
            # probability for this bet's side.  `last_pin_pct` is already
            # side-aligned (YES prob for YES bets, NO prob for NO bets) and is
            # updated every scan cycle (~2 min during game hours).
            # Formula: closing_pin_pct − entry_kalshi_pct
            #   • Positive = Pinnacle valued the side MORE than you paid → +EV ✓
            #   • Negative = Pinnacle valued the side LESS than you paid → −EV
            # Falls back to Kalshi drift when Pinnacle data is unavailable.
            ek = _edge_key(bet)
            with _edge_history_lock:
                pin_hist    = _edge_price_history.get(ek, {})
                closing_pin = pin_hist.get("last_pin_pct")   # side-specific Pinnacle %

            with _bets_lock:
                for b in _bets:
                    if b["id"] == bet["id"]:
                        b["closing_yes_pct"] = yes_close_pct
                        b["closing_pin_pct"] = closing_pin   # persist for resolution time

                        entry_k = b.get("kalshi_price", 0) * 100   # effective bet-side entry price
                        if closing_pin is not None and entry_k:
                            # Primary: true CLV vs Pinnacle closing line.
                            _clv = round(closing_pin - entry_k, 1)
                            b["clv"]        = _clv
                            b["clv_pct"]    = _clv_pct(_clv, entry_k)
                            b["clv_source"] = "pin"
                        else:
                            # Fallback: Kalshi drift using matched ask/bid prices
                            entry_yes = b.get("kalshi_yes_at_flag")
                            if entry_yes is not None:
                                _clv = round(
                                    yes_close_pct - entry_yes if side == "YES"
                                    else entry_yes - yes_close_pct,
                                    1,
                                )
                                b["clv"]        = _clv
                                b["clv_pct"]    = _clv_pct(_clv, entry_yes)
                                b["clv_source"] = "kalshi"
                            else:
                                b["clv_source"] = "none"
                        break
            updated += 1
        except Exception as exc:
            print(f"  CLV capture error ({bet['ticker']}): {exc}")

    if updated:
        with _bets_lock:
            _save_bets(_bets)
        print(f"  CLV capture: updated {updated} open bet(s)")


def _background_clv_capture_loop():
    """Refresh live Kalshi prices for open bets every CLV_CAPTURE_SECONDS."""
    # Let the main scan run first so open bets exist before we start polling
    time.sleep(30)
    while True:
        try:
            _capture_clv_prices()
        except Exception as exc:
            print(f"  CLV capture loop error: {exc}")
        time.sleep(CLV_CAPTURE_SECONDS)


def _background_resolution_loop():
    """
    Dedicated fast-resolution thread.
    Runs every 5 minutes — completely independent of the main scan so settled
    games show up in the performance tracker as soon as the final score lands,
    without waiting for the next full scan cycle.
    Uses the free /scores endpoint first (0 credits), then Kalshi API for anything
    not yet score-settled.
    """
    # Wait for the first main scan to populate open bets before we start checking
    time.sleep(90)
    while True:
        try:
            _check_resolutions()
            _settle_my_bets()
        except Exception as exc:
            print(f"  Resolution loop error: {exc}")
        time.sleep(RESOLUTION_POLL_SECONDS)


# ── HTML template ─────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi EV Scanner</title>
<style>
  :root {
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --green:   #3fb950;
    --yellow:  #d29922;
    --blue:    #58a6ff;
    --red:     #f85149;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px;
    padding: 14px 18px;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
    flex-wrap: wrap;
    gap: 8px;
  }
  .header-left { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 16px; font-weight: 700; white-space: nowrap; }
  h1 span { color: var(--green); }
  .meta { color: var(--muted); font-size: 11px; }
  .header-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .bankroll-inline { display: flex; align-items: center; gap: 6px; }
  .bankroll-inline label { font-size: 11px; color: var(--muted); white-space: nowrap; }
  .bankroll-inline input {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--text);
    font-size: 12px;
    padding: 3px 7px;
    width: 90px;
  }
  .bankroll-inline input:focus { outline: none; border-color: var(--blue); }
  .badge {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
  }
  .badge-scanning { background: #1f2d3d; color: var(--blue); }
  .badge-ok       { background: #1a2d1a; color: var(--green); }
  .badge-error    { background: #2d1a1a; color: var(--red); }
  button#refresh {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 4px 10px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 11px;
  }
  button#refresh:hover { background: #21262d; }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 7px;
    overflow: hidden;
    margin-bottom: 8px;
  }
  .card-header {
    padding: 7px 12px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    user-select: none;
  }
  .card-header:hover { background: #1c2128; }
  .card-toggle { font-size: 10px; color: var(--muted); }
  .card-body.collapsed { display: none; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 6px 10px; text-align: left; white-space: nowrap; }
  th {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    font-weight: 600;
  }
  td { border-bottom: 1px solid #1c2128; transition: background 0.15s ease; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2128; }
  /* Smooth card content transitions — prevents jarring full-page repaints */
  .card-body { transition: opacity 0.1s ease; }
  .card-body.updating { opacity: 0.7; }
  .prop-col { white-space: normal; max-width: 260px; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .side-yes { color: var(--green); font-weight: 600; }
  .side-no  { color: var(--red);   font-weight: 600; }
  .badge-new {
    display: inline-block;
    background: #1f3a5f;
    color: #58a6ff;
    font-size: 9px;
    font-weight: 700;
    padding: 1px 4px;
    border-radius: 3px;
    margin-left: 5px;
    vertical-align: middle;
  }
  .empty { padding: 18px; text-align: center; color: var(--muted); font-size: 12px; }
  .spinner {
    display: inline-block;
    width: 12px; height: 12px;
    border: 2px solid var(--border);
    border-top-color: var(--blue);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    vertical-align: middle;
    margin-right: 5px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
  .countdown { color: var(--muted); font-size: 11px; }
  .kelly-val { color: #a5d6a7; font-weight: 600; }
  .kelly-na  { color: var(--muted); }
  .pin-line  { color: var(--muted); font-size: 11px; }
  .matchup-header td {
    background: #1c2128;
    color: var(--text);
    font-weight: 600;
    font-size: 12px;
    padding: 5px 10px;
    border-bottom: 1px solid var(--border);
    border-left: 3px solid var(--sport-color, #30363d);
  }
  .matchup-inline {
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
    max-width: 180px;
  }
  .team-logo {
    width: 20px; height: 20px;
    vertical-align: middle;
    margin-right: 5px;
    border-radius: 3px;
    object-fit: contain;
  }
  .card-mlb-spread { --sport-color: #58a6ff; }
  .card-mlb-total  { --sport-color: #79c0ff; }
  .card-nba-spread { --sport-color: #ff7b72; }
  .card-nba-total  { --sport-color: #ffa657; }
  .card-header.mlb { border-left: 3px solid #58a6ff; }
  .card-header.nba { border-left: 3px solid #ff7b72; }
  .stat-row { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 10px; }
  .stat-pill {
    background: #1c2128; border: 1px solid var(--border);
    border-radius: 6px; padding: 5px 10px;
  }
  .stat-pill .label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; }
  .stat-pill .value { font-weight: 700; font-size: 14px; margin-top: 1px; }
  .pnl-pos { color: #3fb950; }
  .pnl-neg { color: var(--red); }
  .pnl-neu { color: var(--muted); }
  .result-won  { color: #3fb950; font-weight: 700; }
  .result-lost { color: var(--red); font-weight: 700; }
  .result-open { color: var(--muted); }
  .rank-num { color: var(--muted); font-weight: 700; width: 24px; display: inline-block; font-size: 11px; }
  #history-card canvas { display: block; width: 100% !important; height: 160px !important; }
  .chart-empty { padding: 20px; text-align: center; color: var(--muted); font-size: 12px; }
  .mb-won  { color: #00e676; font-weight: 700; }
  .mb-lost { color: var(--red); font-weight: 700; }
  .mb-open { color: var(--muted); }
  .mb-pnl-pos { color: #00e676; font-weight: 700; }
  .mb-pnl-neg { color: var(--red); font-weight: 700; }
  .mb-del { cursor:pointer; color:var(--muted); font-size:13px; padding:0 4px; }
  .mb-del:hover { color:var(--red); }
  .mb-track-btn { font-size:10px; background:#21262d; border:1px solid var(--border); border-radius:3px; color:var(--muted); padding:2px 6px; cursor:pointer; margin-left:5px; vertical-align:middle; }
  .mb-track-btn:hover { color:var(--text); border-color:var(--muted); }
  .badge-stale { display:inline-block; font-size:9px; background:#2d1a1a; color:#f85149; padding:1px 5px; border-radius:3px; margin-left:5px; vertical-align:middle; }
  .badge-drift { font-size:10px; color:var(--muted); margin-left:4px; }
  .badge-unvalidated { display:inline-block; font-size:9px; background:#2d200a; color:#e3a53a; padding:1px 5px; border-radius:3px; margin-left:5px; vertical-align:middle; letter-spacing:0.03em; }
  .badge-sharp { display:inline-block; font-size:9px; background:#0d2137; color:#58a6ff; border:1px solid #1f4e79; padding:1px 5px; border-radius:3px; margin-left:5px; vertical-align:middle; font-weight:700; letter-spacing:0.04em; }
  .exec-card-header { background:linear-gradient(90deg,#1a2637 0%,#161b22 100%); border-left:3px solid #58a6ff; }

  /* ── Scanner status strip ───────────────────────────────────────────────── */
  #status-strip {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 6px 14px;
    background: #0d1117;
    border: 1px solid var(--border);
    border-radius: 7px;
    margin-bottom: 8px;
    flex-wrap: wrap;
    font-size: 11px;
    color: var(--muted);
  }
  .ss-item { display: flex; align-items: center; gap: 5px; white-space: nowrap; }
  .ss-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
    transition: background 0.4s;
  }
  .ss-green  { background: #3fb950; box-shadow: 0 0 5px #3fb95055; }
  .ss-yellow { background: #e3a53a; box-shadow: 0 0 5px #e3a53a55; }
  .ss-red    { background: #f85149; box-shadow: 0 0 5px #f8514955; }
  .ss-muted  { background: #444; box-shadow: none; }

  /* ── Market temperature gauge ───────────────────────────────────────────── */
  #temp-wrap {
    flex: 1;
    min-width: 180px;
    max-width: 320px;
    display: flex;
    align-items: center;
    gap: 7px;
  }
  #temp-label { font-size: 10px; color: var(--muted); white-space: nowrap; }
  #temp-track {
    flex: 1;
    height: 7px;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 4px;
    position: relative;
    overflow: hidden;
  }
  #temp-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.7s cubic-bezier(.4,0,.2,1), background 0.5s;
  }
  #temp-threshold {
    position: absolute;
    top: -1px; bottom: -1px;
    width: 2px;
    background: #3fb950cc;
    border-radius: 1px;
    pointer-events: none;
  }
  #temp-value {
    font-size: 11px;
    font-weight: 700;
    white-space: nowrap;
    min-width: 40px;
    transition: color 0.4s;
  }

  .clv-pos { color:#3fb950; font-weight:700; }
  .clv-neg { color:#f85149; font-weight:700; }
  .clv-neu { color:var(--muted); }
  .conf-cell { white-space:nowrap; }
  .insufficient-data { color:var(--muted); font-style:italic; font-size:11px; }
</style>
</head>
<body>
<header>
  <div class="header-left">
    <h1>Kalshi EV &nbsp;<span>MLB</span></h1>
    <div class="meta" id="last-scan">Loading…</div>
  </div>
  <div class="header-right">
    <div class="bankroll-inline">
      <label for="bankroll">Bankroll $</label>
      <input type="number" id="bankroll" min="0" step="100" value="1000" oninput="renderAll()">
    </div>
    <span class="countdown" id="countdown"></span>
    <span class="badge badge-scanning" id="status-badge">
      <span class="spinner"></span>Scanning…
    </span>
    <button id="refresh" onclick="fetchData()">↻ Refresh</button>
  </div>
</header>

<div id="status-strip">
  <div class="ss-item">
    <span class="ss-dot ss-muted" id="ss-scan-dot"></span>
    <span id="ss-scan-txt">Scan —</span>
  </div>
  <div class="ss-item">
    <span class="ss-dot ss-muted" id="ss-pin-dot"></span>
    <span id="ss-pin-txt">Pinnacle —</span>
  </div>
  <div class="ss-item">
    <span class="ss-dot ss-muted" id="ss-games-dot"></span>
    <span id="ss-games-txt">— games</span>
  </div>
  <div id="temp-wrap">
    <span id="temp-label">Best market</span>
    <div id="temp-track">
      <div id="temp-fill"></div>
      <div id="temp-threshold"></div>
    </div>
    <span id="temp-value" style="color:var(--muted);">—</span>
  </div>
</div>

<div id="today-edges-card" class="card">
  <div class="card-header exec-card-header" onclick="toggleCard('today-edges-body')">📡 Live Portfolio Tracker &nbsp;<span style="font-size:10px;color:var(--muted);font-weight:400;">all open bets · updates every 2 min</span> <span class="card-toggle" id="today-edges-body-toggle">▾</span></div>
  <div id="today-edges-body" class="card-body"><div class="empty">No open positions. Edges will appear here when flagged.</div></div>
</div>



<div id="paper-card" class="card">
  <div class="card-header" onclick="toggleCard('paper-body')" style="border-left:3px solid #3fb950;">📊 PAPER PORTFOLIO (V2.0 - POST-THROTTLE) — 0.5 Kelly · Props 2.5%+ · Games 3%+ · $1,000 Starting Balance <span class="card-toggle" id="paper-body-toggle">▾</span></div>
  <div id="paper-body" class="card-body"><div class="empty"><span class="spinner"></span>Loading portfolio…</div></div>
</div>

<div id="perf-card" class="card">
  <div class="card-header" onclick="toggleCard('perf-body-wrap')">Performance — Model Accuracy <span class="card-toggle" id="perf-body-wrap-toggle">▾</span></div>
  <div id="perf-body-wrap" class="card-body">
    <div style="display:flex;align-items:center;gap:10px;padding:6px 0 12px 0;flex-wrap:wrap;">
      <label style="font-size:12px;color:var(--muted);white-space:nowrap;">Filter from:</label>
      <input type="date" id="perf-since" style="background:var(--bg2);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:3px 8px;font-size:12px;" oninput="fetchPerformance()">
      <button onclick="document.getElementById('perf-since').value='';fetchPerformance();"
        style="font-size:11px;padding:3px 10px;background:var(--bg2);color:var(--muted);border:1px solid var(--border);border-radius:4px;cursor:pointer;">
        All time
      </button>
      <span id="perf-filter-label" style="font-size:11px;color:var(--muted);"></span>
    </div>
    <div id="perf-stats"></div>
    <div id="perf-body"><div class="empty">No resolved bets yet.</div></div>
  </div>
</div>


<div id="history-card" class="card">
  <div class="card-header" onclick="toggleCard('history-body')">Portfolio ROI <span class="card-toggle" id="history-body-toggle">▾</span></div>
  <div id="history-body" class="card-body" style="padding:16px 12px 12px;">
    <div class="chart-empty">Loading ROI chart…</div>
  </div>
</div>




<div id="mybets-card" class="card">
  <div class="card-header" style="border-left:3px solid #f0c000;" onclick="toggleCard('mybets-body')">💰 My Bets — Real P&amp;L <span class="card-toggle" id="mybets-body-toggle">▾</span></div>
  <div id="mybets-body" class="card-body">
    <div id="mybets-stats" style="padding:8px 10px;border-bottom:1px solid var(--border);display:flex;gap:12px;flex-wrap:wrap;align-items:center;">
      <span style="color:var(--muted);font-size:11px;">Add a bet you placed on Kalshi to track real P&amp;L.</span>
    </div>
    <div style="padding:8px 12px;border-bottom:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;">
      <div style="display:flex;flex-direction:column;gap:3px;">
        <label style="font-size:10px;color:var(--muted);text-transform:uppercase;">Market ticker</label>
        <input id="mb-ticker" type="text" placeholder="KXMLBTOTAL-26APR05..." style="background:var(--surface);border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:4px 8px;width:220px;">
      </div>
      <div style="display:flex;flex-direction:column;gap:3px;">
        <label style="font-size:10px;color:var(--muted);text-transform:uppercase;">Side</label>
        <select id="mb-side" style="background:var(--surface);border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:4px 8px;">
          <option value="YES">YES</option>
          <option value="NO">NO</option>
        </select>
      </div>
      <div style="display:flex;flex-direction:column;gap:3px;">
        <label style="font-size:10px;color:var(--muted);text-transform:uppercase;">Price paid (¢)</label>
        <input id="mb-price" type="number" min="1" max="99" placeholder="79" style="background:var(--surface);border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:4px 8px;width:80px;">
      </div>
      <div style="display:flex;flex-direction:column;gap:3px;">
        <label style="font-size:10px;color:var(--muted);text-transform:uppercase;">Amount ($)</label>
        <input id="mb-amount" type="number" min="1" placeholder="25" style="background:var(--surface);border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:4px 8px;width:80px;">
      </div>
      <button onclick="addMyBet()" style="background:#21262d;border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:12px;padding:5px 14px;cursor:pointer;height:30px;">+ Track Bet</button>
    </div>
    <div id="mybets-table"><div class="empty">No bets tracked yet — add one above.</div></div>
  </div>
</div>

<div id="scanner-history-card" class="card" style="border-color:#2a2a2a;background:#0f1117;opacity:0.88;">
  <div class="card-header" onclick="toggleCard('scanner-history-body')" style="border-left:3px solid #3d3d3d;color:#6e7681;">
    📋 Recent Scanner History (Past 24 Hours)
    <span style="font-size:10px;font-weight:400;color:#4d5461;margin-left:6px;">read-only audit log · sorted newest first</span>
    <span class="card-toggle" id="scanner-history-body-toggle">▸</span>
  </div>
  <div id="scanner-history-body" class="card-body collapsed">
    <div id="scanner-history-table" style="color:#6e7681;"><div class="empty">Loading history…</div></div>
  </div>
</div>

<script>
const REFRESH_MS = """ + str(REFRESH_SECONDS * 1000) + """;
let nextRefresh = Date.now() + REFRESH_MS;
let lastEdges        = [];   // MLB spreads/totals
let marketSnapshot   = {};   // {ticker|side: {adj_edge, kalshi, fair, edge_pct}} — all scanned markets
let prevEdgeKeys     = new Set();
let _roiPoints       = null;
let clvMultipliers   = {};   // {"prop": 0.5, "spread": 1.0, …} — set by fetchPerformance()

let _slowRefreshCounter = 0;  // perf+history refresh every 3 scan cycles

// Safe innerHTML — only writes if content changed (prevents scroll-jump / flicker)
function _setHTML(id, html) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.innerHTML === html) return;
  el.innerHTML = html;
}

function toggleCard(bodyId) {
  const el = document.getElementById(bodyId);
  const tog = document.getElementById(bodyId + '-toggle');
  if (!el) return;
  el.classList.toggle('collapsed');
  if (tog) tog.textContent = el.classList.contains('collapsed') ? '▸' : '▾';
}

function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
}

function edgeKey(e) { return e.matchup + '|' + e.title + '|' + e.side; }

// Edge color by strength: orange (weakest) → yellow → green → bright green (strongest)
function edgeColor(pct) {
  if (pct >= 12) return '#00e676';   // bright green  — 12%+
  if (pct >= 8)  return '#3fb950';   // green         — 8–12%
  if (pct >= 5)  return '#d29922';   // yellow        — 5–8%
  return '#ff8c42';                  // orange        — 3–5%
}

function edgeClass(pct) { return ''; }  // kept for compatibility, color now inline

// Safely escape a string for use inside an HTML attribute value (data-*)
function hesc(s) { return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

// Track button that reads data-attributes — avoids all JS string escaping issues
function trackBetFromBtn(btn) {
  trackBet(btn.dataset.ticker, btn.dataset.title, btn.dataset.matchup,
           btn.dataset.side, btn.dataset.mkttype);
}

function trackBtn(e) {
  return '<button class="mb-track-btn" '
    + 'data-ticker="' + hesc(e.ticker)  + '" '
    + 'data-title="'  + hesc(e.title)   + '" '
    + 'data-matchup="'+ hesc(e.matchup) + '" '
    + 'data-side="'   + hesc(e.side)    + '" '
    + 'data-mkttype="'+ hesc(e.mkt_type||'') + '" '
    + 'onclick="trackBetFromBtn(this)">Track</button>';
}

async function fetchHistory() {
  try {
    const r = await fetch('/api/roi');
    const data = await r.json();
    renderRoiChart(data);
  } catch (e) { console.error('ROI chart fetch failed', e); }
}

function renderRoiChart(points) {
  _roiPoints = points;
  const el = document.getElementById('history-body');
  if (!points || points.length < 2) {
    el.innerHTML = '<div class="chart-empty">No settled bets yet.</div>';
    return;
  }

  const W = el.clientWidth || 640;
  const H = 260;
  const PAD = { top: 18, right: 16, bottom: 36, left: 52 };
  const cW = W - PAD.left - PAD.right;
  const cH = H - PAD.top - PAD.bottom;

  const rois   = points.map(p => p.roi);
  const minR   = Math.min(...rois, 0);
  const maxR   = Math.max(...rois, 0);
  const span   = maxR - minR || 1;
  const padded = span * 0.12;
  const yMin   = minR - padded;
  const yMax   = maxR + padded;
  const ySpan  = yMax - yMin;

  const toX = i => PAD.left + (i / (points.length - 1)) * cW;
  const toY = v => PAD.top  + (1 - (v - yMin) / ySpan) * cH;

  const currentRoi = rois[rois.length - 1];
  const stroke = currentRoi >= 0 ? '#3fb950' : '#f85149';
  const fillId = 'roi-fill';

  // polyline points
  const pts = points.map((p, i) => toX(i).toFixed(1) + ',' + toY(p.roi).toFixed(1)).join(' ');

  // closed area polygon (go to baseline then back)
  const baseY = toY(0).toFixed(1);
  const areaPath = 'M ' + toX(0).toFixed(1) + ',' + baseY
    + ' ' + points.map((p, i) => 'L ' + toX(i).toFixed(1) + ',' + toY(p.roi).toFixed(1)).join(' ')
    + ' L ' + toX(points.length - 1).toFixed(1) + ',' + baseY + ' Z';

  // Y-axis ticks (5 ticks)
  const yTicks = [];
  const tickStep = (yMax - yMin) / 4;
  for (let t = 0; t <= 4; t++) {
    const v = yMin + t * tickStep;
    yTicks.push({ v, y: toY(v) });
  }

  // X-axis labels (up to 10 evenly spaced)
  const maxLabels = Math.min(10, points.length);
  const xLabels = [];
  for (let i = 0; i < maxLabels; i++) {
    const idx = Math.round(i * (points.length - 1) / (maxLabels - 1));
    const p = points[idx];
    const label = p.date ? new Date(p.date + 'T12:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : 'Start';
    xLabels.push({ x: toX(idx), label });
  }

  // Zero line
  const zeroLine = (yMin < 0 && yMax > 0)
    ? `<line x1="${PAD.left}" y1="${toY(0).toFixed(1)}" x2="${PAD.left + cW}" y2="${toY(0).toFixed(1)}" stroke="#30363d" stroke-width="1" stroke-dasharray="4,3"/>`
    : '';

  const svg = `<svg id="roi-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="display:block;overflow:visible">
  <defs>
    <linearGradient id="${fillId}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${stroke}" stop-opacity="0.18"/>
      <stop offset="100%" stop-color="${stroke}" stop-opacity="0.01"/>
    </linearGradient>
  </defs>
  ${yTicks.map(t => `<line x1="${PAD.left}" y1="${t.y.toFixed(1)}" x2="${PAD.left+cW}" y2="${t.y.toFixed(1)}" stroke="#21262d" stroke-width="1"/>`).join('')}
  ${zeroLine}
  <path d="${areaPath}" fill="url(#${fillId})"/>
  <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  ${points.map((p, i) => `<circle cx="${toX(i).toFixed(1)}" cy="${toY(p.roi).toFixed(1)}" r="3" fill="${stroke}" class="roi-dot" data-idx="${i}" style="cursor:pointer"/>`).join('')}
  ${yTicks.map(t => `<text x="${PAD.left - 6}" y="${(t.y + 4).toFixed(1)}" text-anchor="end" fill="#8b949e" font-size="10">${(t.v >= 0 ? '+' : '') + t.v.toFixed(1)}%</text>`).join('')}
  ${xLabels.map(l => `<text x="${l.x.toFixed(1)}" y="${H - 6}" text-anchor="middle" fill="#8b949e" font-size="10">${l.label}</text>`).join('')}
</svg>
<div id="roi-tooltip" style="display:none;position:absolute;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 10px;font-size:12px;color:#e6edf3;pointer-events:none;z-index:10;white-space:nowrap;"></div>`;

  el.style.position = 'relative';
  el.innerHTML = svg;

  // hover tooltip
  el.querySelectorAll('.roi-dot').forEach(dot => {
    dot.addEventListener('mouseenter', function(ev) {
      const idx = parseInt(this.getAttribute('data-idx'));
      const p = _roiPoints[idx];
      const tip = document.getElementById('roi-tooltip');
      let html = '';
      if (!p.result) {
        html = '<b>Start</b><br>Balance: $1,000.00 &nbsp;|&nbsp; ROI: +0.00%';
      } else {
        const icon = p.result === 'won' ? '✓' : '✗';
        const sign = p.pnl >= 0 ? '+' : '';
        html = `<b>${p.matchup || p.title || ''}</b><br>`
          + `${icon} ${p.result.toUpperCase()}  ${sign}$${p.pnl.toFixed(2)}<br>`
          + `Balance: $${p.balance.toFixed(2)} &nbsp;|&nbsp; ROI: ${p.roi >= 0 ? '+' : ''}${p.roi.toFixed(2)}%`;
      }
      tip.innerHTML = html;
      const rect = el.getBoundingClientRect();
      const dotRect = this.getBoundingClientRect();
      let left = dotRect.left - rect.left + 10;
      if (left + 220 > W) left = dotRect.left - rect.left - 230;
      tip.style.left = left + 'px';
      tip.style.top  = (dotRect.top - rect.top - 10) + 'px';
      tip.style.display = 'block';
    });
    dot.addEventListener('mouseleave', () => {
      const tip = document.getElementById('roi-tooltip');
      if (tip) tip.style.display = 'none';
    });
  });
}

function pct(v) { return v.toFixed(1) + '%'; }

/** Convert a decimal (no-vig) probability to an American odds string.
 *  e.g. probToAmerican(0.60) → "-150"   probToAmerican(0.40) → "+150"
 *  Returns "—" for null / out-of-range inputs.
 */
function probToAmerican(p) {
  if (p == null || p <= 0 || p >= 1) return '—';
  if (p >= 0.5) {
    return '-' + Math.round(p / (1 - p) * 100);
  } else {
    return '+' + Math.round((1 - p) / p * 100);
  }
}

/** Format a Kalshi decimal price (0–1) as American odds. */
function kalshiToAmerican(k) { return probToAmerican(k); }

function getBankroll() {
  const val = parseFloat(document.getElementById('bankroll').value);
  return isNaN(val) || val <= 0 ? 1000 : val;
}

// 0.25 Fractional Kelly for a binary prediction market.
//   full Kelly   = (fair − kalshi) / (1 − kalshi)
//   adjusted     = full × 0.25 × clvMultiplier(mktType)
//   hard cap     = 5% of bankroll per bet
// mktType is optional; if provided and that type has a CLV penalty, stake is halved.
function kellyBet(fair, kalshi, bankroll, mktType) {
  if (!bankroll || fair == null || kalshi == null) return null;
  const edge = fair - kalshi;
  if (edge <= 0 || kalshi <= 0 || kalshi >= 1) return 0;
  const fullKelly   = edge / (1 - kalshi);
  const clvMult     = (mktType && clvMultipliers[mktType] != null) ? clvMultipliers[mktType] : 1.0;
  const adjFraction = Math.min(fullKelly * 0.25 * clvMult, 0.05);   // 5% hard cap
  return adjFraction * bankroll;
}

// ESPN team logo CDN map
const LOGOS = {
  // MLB
  'Arizona Diamondbacks':    'https://a.espncdn.com/i/teamlogos/mlb/500/ari.png',
  'Atlanta Braves':          'https://a.espncdn.com/i/teamlogos/mlb/500/atl.png',
  'Baltimore Orioles':       'https://a.espncdn.com/i/teamlogos/mlb/500/bal.png',
  'Boston Red Sox':          'https://a.espncdn.com/i/teamlogos/mlb/500/bos.png',
  'Chicago Cubs':            'https://a.espncdn.com/i/teamlogos/mlb/500/chc.png',
  'Chicago White Sox':       'https://a.espncdn.com/i/teamlogos/mlb/500/chw.png',
  'Cincinnati Reds':         'https://a.espncdn.com/i/teamlogos/mlb/500/cin.png',
  'Cleveland Guardians':     'https://a.espncdn.com/i/teamlogos/mlb/500/cle.png',
  'Colorado Rockies':        'https://a.espncdn.com/i/teamlogos/mlb/500/col.png',
  'Detroit Tigers':          'https://a.espncdn.com/i/teamlogos/mlb/500/det.png',
  'Houston Astros':          'https://a.espncdn.com/i/teamlogos/mlb/500/hou.png',
  'Kansas City Royals':      'https://a.espncdn.com/i/teamlogos/mlb/500/kc.png',
  'Los Angeles Angels':      'https://a.espncdn.com/i/teamlogos/mlb/500/laa.png',
  'Los Angeles Dodgers':     'https://a.espncdn.com/i/teamlogos/mlb/500/lad.png',
  'Miami Marlins':           'https://a.espncdn.com/i/teamlogos/mlb/500/mia.png',
  'Milwaukee Brewers':       'https://a.espncdn.com/i/teamlogos/mlb/500/mil.png',
  'Minnesota Twins':         'https://a.espncdn.com/i/teamlogos/mlb/500/min.png',
  'New York Mets':           'https://a.espncdn.com/i/teamlogos/mlb/500/nym.png',
  'New York Yankees':        'https://a.espncdn.com/i/teamlogos/mlb/500/nyy.png',
  'Athletics':               'https://a.espncdn.com/i/teamlogos/mlb/500/oak.png',
  'Oakland Athletics':       'https://a.espncdn.com/i/teamlogos/mlb/500/oak.png',
  "A's":                     'https://a.espncdn.com/i/teamlogos/mlb/500/oak.png',
  'Philadelphia Phillies':   'https://a.espncdn.com/i/teamlogos/mlb/500/phi.png',
  'Pittsburgh Pirates':      'https://a.espncdn.com/i/teamlogos/mlb/500/pit.png',
  'San Diego Padres':        'https://a.espncdn.com/i/teamlogos/mlb/500/sd.png',
  'San Francisco Giants':    'https://a.espncdn.com/i/teamlogos/mlb/500/sf.png',
  'Seattle Mariners':        'https://a.espncdn.com/i/teamlogos/mlb/500/sea.png',
  'St. Louis Cardinals':     'https://a.espncdn.com/i/teamlogos/mlb/500/stl.png',
  'Tampa Bay Rays':          'https://a.espncdn.com/i/teamlogos/mlb/500/tb.png',
  'Texas Rangers':           'https://a.espncdn.com/i/teamlogos/mlb/500/tex.png',
  'Toronto Blue Jays':       'https://a.espncdn.com/i/teamlogos/mlb/500/tor.png',
  'Washington Nationals':    'https://a.espncdn.com/i/teamlogos/mlb/500/wsh.png',
  // NBA
  'Atlanta Hawks':           'https://a.espncdn.com/i/teamlogos/nba/500/atl.png',
  'Boston Celtics':          'https://a.espncdn.com/i/teamlogos/nba/500/bos.png',
  'Brooklyn Nets':           'https://a.espncdn.com/i/teamlogos/nba/500/bkn.png',
  'Charlotte Hornets':       'https://a.espncdn.com/i/teamlogos/nba/500/cha.png',
  'Chicago Bulls':           'https://a.espncdn.com/i/teamlogos/nba/500/chi.png',
  'Cleveland Cavaliers':     'https://a.espncdn.com/i/teamlogos/nba/500/cle.png',
  'Dallas Mavericks':        'https://a.espncdn.com/i/teamlogos/nba/500/dal.png',
  'Denver Nuggets':          'https://a.espncdn.com/i/teamlogos/nba/500/den.png',
  'Detroit Pistons':         'https://a.espncdn.com/i/teamlogos/nba/500/det.png',
  'Golden State Warriors':   'https://a.espncdn.com/i/teamlogos/nba/500/gs.png',
  'Houston Rockets':         'https://a.espncdn.com/i/teamlogos/nba/500/hou.png',
  'Indiana Pacers':          'https://a.espncdn.com/i/teamlogos/nba/500/ind.png',
  'Los Angeles Clippers':    'https://a.espncdn.com/i/teamlogos/nba/500/lac.png',
  'Los Angeles Lakers':      'https://a.espncdn.com/i/teamlogos/nba/500/lal.png',
  'Memphis Grizzlies':       'https://a.espncdn.com/i/teamlogos/nba/500/mem.png',
  'Miami Heat':              'https://a.espncdn.com/i/teamlogos/nba/500/mia.png',
  'Milwaukee Bucks':         'https://a.espncdn.com/i/teamlogos/nba/500/mil.png',
  'Minnesota Timberwolves':  'https://a.espncdn.com/i/teamlogos/nba/500/min.png',
  'New Orleans Pelicans':    'https://a.espncdn.com/i/teamlogos/nba/500/no.png',
  'New York Knicks':         'https://a.espncdn.com/i/teamlogos/nba/500/ny.png',
  'Oklahoma City Thunder':   'https://a.espncdn.com/i/teamlogos/nba/500/okc.png',
  'Orlando Magic':           'https://a.espncdn.com/i/teamlogos/nba/500/orl.png',
  'Philadelphia 76ers':      'https://a.espncdn.com/i/teamlogos/nba/500/phi.png',
  'Phoenix Suns':            'https://a.espncdn.com/i/teamlogos/nba/500/phx.png',
  'Portland Trail Blazers':  'https://a.espncdn.com/i/teamlogos/nba/500/por.png',
  'Sacramento Kings':        'https://a.espncdn.com/i/teamlogos/nba/500/sac.png',
  'San Antonio Spurs':       'https://a.espncdn.com/i/teamlogos/nba/500/sa.png',
  'Toronto Raptors':         'https://a.espncdn.com/i/teamlogos/nba/500/tor.png',
  'Utah Jazz':               'https://a.espncdn.com/i/teamlogos/nba/500/utah.png',
  'Washington Wizards':      'https://a.espncdn.com/i/teamlogos/nba/500/wsh.png',
};

function teamLogo(name) {
  const url = LOGOS[name];
  return url ? `<img class="team-logo" src="${url}" alt="" onerror="this.style.display='none'">` : '';
}

function matchupHtml(matchup) {
  const parts = matchup.split(' @ ');
  if (parts.length !== 2) return matchup;
  const [away, home] = parts;
  return `${teamLogo(away)}<span>${away}</span> <span style="color:var(--muted);font-weight:400;">@</span> ${teamLogo(home)}<span>${home}</span>`;
}

function pinLineLabel(e) {
  if (e.pin_line == null) return '—';
  if (e.mkt_type === 'spread') {
    const v = e.pin_line;
    return 'Fav ' + (v > 0 ? '+' : '') + v;
  }
  return 'O/U ' + e.pin_line;
}

function kalshiLineBadge(e) {
  if (e.kalshi_line == null) return '';
  let label;
  if (e.mkt_type === 'spread') {
    label = (e.kalshi_line > 0 ? '+' : '') + e.kalshi_line;
  } else {
    label = 'Over ' + e.kalshi_line;
  }
  return `<span style="display:inline-block;font-size:10px;font-weight:700;color:#58a6ff;background:rgba(88,166,255,0.1);border:1px solid rgba(88,166,255,0.3);border-radius:3px;padding:1px 5px;margin-left:5px;vertical-align:middle;" title="Kalshi line to bet">${label}</span>`;
}

function renderTable(edges) {
  if (!edges.length) return '<div class="empty">No edges ≥ 3% found.</div>';
  const bankroll = getBankroll();

  // Flat list sorted by edge descending — best edge always at top
  const sorted = [...edges].sort((a, b) => b.edge_pct - a.edge_pct);

  let rows = '';
  for (const e of sorted) {
    const bet = kellyBet(e.fair, e.kalshi, bankroll, e.mkt_type);
    const kellyCell = `<td class="num kelly-val">$${bet != null ? bet.toFixed(0) : '—'}</td>`;
    const isNew = !prevEdgeKeys.has(edgeKey(e));
    const newBadge = isNew ? '<span class="badge-new">NEW</span>' : '';
    const staleBadge = e.stale ? `<span class="badge-stale">STALE</span>` : '';
    const driftTxt = e.drift_pct != null && e.drift_pct !== 0
      ? `<span class="badge-drift">(${e.drift_pct > 0 ? '+' : ''}${e.drift_pct}%)</span>` : '';
    rows += `
    <tr>
      <td class="matchup-inline">${matchupHtml(e.matchup)}</td>
      <td class="prop-col">${e.title}${kalshiLineBadge(e)}${newBadge}${staleBadge}${driftTxt}${trackBtn(e)}</td>
      <td class="num pin-line">${pinLineLabel(e)}</td>
      <td class="side-${e.side.toLowerCase()}">${e.side}</td>
      <td class="num">${pct(e.kalshi_pct)}</td>
      <td class="num">${pct(e.fair_pct)}</td>
      <td class="num" style="color:${edgeColor(e.edge_pct)};font-weight:700;">+${pct(e.edge_pct)}</td>
      ${kellyCell}
    </tr>`;
  }

  return `<table>
    <thead><tr>
      <th>Matchup</th><th>Prop</th><th class="num">Pinnacle</th><th>Side</th>
      <th class="num">Kalshi</th><th class="num">Fair</th><th class="num">Edge</th>
      <th class="num">Kelly Bet</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function sportOf(e) {
  // Infer sport from matchup teams or ticker prefix
  const t = (e.ticker || '').toUpperCase();
  if (t.startsWith('KXNBA') || t.startsWith('KXNBA')) return 'nba';
  if (t.startsWith('KXMLB')) return 'mlb';
  return 'mlb';
}

function renderAll() {
  // Update tab title with live edge count
  const count = lastEdges.length;
  document.title = count > 0 ? `(${count}) Kalshi EV Scanner` : 'Kalshi EV Scanner';
}

let autoRefreshTimer = null;

// ── Status strip + temperature gauge ─────────────────────────────────────────
function updateStatusStrip(d) {
  // ── Scan age ──────────────────────────────────────────────────────────────
  const scanDot = document.getElementById('ss-scan-dot');
  const scanTxt = document.getElementById('ss-scan-txt');
  if (d.last_scan) {
    const ageSec = Math.round((Date.now() - new Date(d.last_scan).getTime()) / 1000);
    const ageMin = ageSec / 60;
    scanTxt.textContent = ageSec < 60 ? `Scan ${ageSec}s ago` : `Scan ${ageMin.toFixed(1)}m ago`;
    if      (ageMin <  3) { scanDot.className = 'ss-dot ss-green'; }
    else if (ageMin <  6) { scanDot.className = 'ss-dot ss-yellow'; }
    else                  { scanDot.className = 'ss-dot ss-red'; }
  } else {
    scanTxt.textContent = 'Scan —';
    scanDot.className = 'ss-dot ss-muted';
  }

  // ── Pinnacle data age ─────────────────────────────────────────────────────
  const pinDot = document.getElementById('ss-pin-dot');
  const pinTxt = document.getElementById('ss-pin-txt');
  const pinAge = d.odds_cache_success_age;   // seconds since last successful fetch
  if (pinAge != null) {
    const pinMin = Math.round(pinAge / 60);
    pinTxt.textContent = pinMin < 1 ? 'Pinnacle <1m' : `Pinnacle ${pinMin}m old`;
    if      (pinAge < 900)  { pinDot.className = 'ss-dot ss-green'; }   // < 15 min
    else if (pinAge < 2100) { pinDot.className = 'ss-dot ss-yellow'; }  // < 35 min
    else                    { pinDot.className = 'ss-dot ss-red'; }
  } else {
    pinTxt.textContent = 'Pinnacle —';
    pinDot.className = 'ss-dot ss-muted';
  }

  // ── Game count ────────────────────────────────────────────────────────────
  const gamesDot = document.getElementById('ss-games-dot');
  const gamesTxt = document.getElementById('ss-games-txt');
  const gc = d.odds_game_count;
  if (gc != null) {
    gamesTxt.textContent = `${gc} games`;
    if      (gc >= 15) { gamesDot.className = 'ss-dot ss-green'; }
    else if (gc >= 5)  { gamesDot.className = 'ss-dot ss-yellow'; }
    else               { gamesDot.className = 'ss-dot ss-red'; }
  } else {
    gamesTxt.textContent = '— games';
    gamesDot.className = 'ss-dot ss-muted';
  }

  // ── Temperature gauge ─────────────────────────────────────────────────────
  // Range: -10% (cold) to +5% (well above threshold)
  // Threshold marker sits at 3% = (3-(-10))/(5-(-10)) = 13/15 = 86.7% from left
  const GAUGE_MIN  = -10;   // pp — leftmost (coldest)
  const GAUGE_MAX  =   5;   // pp — rightmost (hottest)
  const THRESHOLD  =   3;   // pp — where the green marker sits
  const GAUGE_SPAN = GAUGE_MAX - GAUGE_MIN;

  const fill  = document.getElementById('temp-fill');
  const mark  = document.getElementById('temp-threshold');
  const val   = document.getElementById('temp-value');

  // Position the threshold marker (doesn't change, but set it once)
  const threshPct = ((THRESHOLD - GAUGE_MIN) / GAUGE_SPAN) * 100;
  mark.style.left = threshPct.toFixed(1) + '%';

  const stats = d.last_scan_stats;
  const best  = stats && stats.best_adj_pct != null ? stats.best_adj_pct : null;

  if (best != null) {
    const clamped  = Math.max(GAUGE_MIN, Math.min(GAUGE_MAX, best));
    const fillPct  = ((clamped - GAUGE_MIN) / GAUGE_SPAN) * 100;
    fill.style.width = fillPct.toFixed(1) + '%';

    // Color the fill and value text
    let fillColor, textColor;
    if      (best >= THRESHOLD) { fillColor = '#3fb950'; textColor = '#3fb950'; }  // edge live — green
    else if (best >= -1)        { fillColor = '#e3a53a'; textColor = '#e3a53a'; }  // warm — yellow
    else if (best >= -4)        { fillColor = '#58a6ff'; textColor = '#58a6ff'; }  // cool — blue
    else                        { fillColor = '#444';    textColor = 'var(--muted)'; }  // cold — grey

    fill.style.background = fillColor;
    val.style.color = textColor;
    val.textContent = (best >= 0 ? '+' : '') + best.toFixed(1) + '%';
    val.title = `Best market adj. edge this scan: ${best.toFixed(1)}% (threshold: ${THRESHOLD}%)`;
  } else {
    fill.style.width = '0%';
    fill.style.background = '#444';
    val.style.color = 'var(--muted)';
    val.textContent = '—';
  }
}

function scheduleRefresh(ms) {
  if (autoRefreshTimer) clearTimeout(autoRefreshTimer);
  nextRefresh = Date.now() + ms;
  autoRefreshTimer = setTimeout(fetchData, ms);
}

async function fetchData() {
  try {
    const r = await fetch('/api/scan');
    const d = await r.json();

    const badge = document.getElementById('status-badge');
    if (d.scanning) {
      badge.className = 'badge badge-scanning';
      badge.innerHTML = '<span class="spinner"></span>Scanning…';
      // Poll every 3s while scan is running so results appear immediately
      scheduleRefresh(3000);
    } else if (d.error) {
      badge.className = 'badge badge-error';
      badge.textContent = 'Error: ' + d.error.slice(0, 60);
      scheduleRefresh(REFRESH_MS);
    } else {
      badge.className = 'badge badge-ok';
      badge.textContent = d.edges.length + ' edge' + (d.edges.length !== 1 ? 's' : '');
      scheduleRefresh(REFRESH_MS);
    }

    if (d.last_scan) {
      document.getElementById('last-scan').textContent = fmtDate(d.last_scan);
    }

    // ── Status strip + temperature gauge ─────────────────────────────────────
    updateStatusStrip(d);

    // While scanning: show spinner only if we have no data yet
    if (d.scanning) {
      // nothing to show while cold-start scanning — today-edges handles display
    } else {
      // Always update data and re-render
      prevEdgeKeys = new Set(lastEdges.map(edgeKey));
      lastEdges      = d.edges || [];
      marketSnapshot = d.market_snapshot || {};
      renderAll();
      try { renderTodayEdges(); } catch(e) { console.error('renderTodayEdges (poll) threw', e); }

      // History + perf + today edges: first load always, then every 3 cycles
      _slowRefreshCounter++;
      if (_slowRefreshCounter <= 1 || _slowRefreshCounter % 3 === 0) {
        fetchHistory();
        fetchPerformance();
        fetchTodayEdges();
      }
    }

  } catch (err) {
    console.error('fetchData error:', err);
    scheduleRefresh(5000);
  }
}

function updateCountdown() {
  const secs = Math.max(0, Math.round((nextRefresh - Date.now()) / 1000));
  const el = document.getElementById('countdown');
  el.textContent = secs > 5 ? 'Next refresh in ' + secs + 's' : '';
}

// ── Best 10 — All Edges (MLB + NBA spreads/totals) ───────────────────────────
function confStars(c, booksUsed) {
  // Visual confidence indicator.
  // ★★★ = Triple-book confirmation: all 3 books (PIN+DK+FD) within ≤2pp (c ≥ 0.80)
  // ★★  = Dual-book agreement OR tight single-book (c ≥ 0.50)
  // ★   = High divergence or only 1 book
  if (c == null) return '<span style="color:var(--muted)" title="No confidence data">?</span>';
  const n = (booksUsed && booksUsed.length) ? booksUsed.length : 0;
  if (c >= 0.80 && n >= 3) {
    return `<span style="color:#3fb950" title="★★★ Triple-book confirmation — PIN+DK+FD all within 2pp">★★★</span>`;
  }
  if (c >= 0.50) {
    const tip = n >= 3 ? `3 books but spread >2pp (${Math.round((1-c)*10)}pp)` : `${n} book(s) — moderate agreement`;
    return `<span style="color:#ffe082" title="★★ ${tip}">★★</span>`;
  }
  return `<span style="color:#f85149" title="★ High divergence (${Math.round((1-c)*10)}pp spread) — possible Sharp-Led Move">★</span>`;
}

// ── Today's Edges ────────────────────────────────────────────────────────────
let todayEdgesList = [];

async function fetchTodayEdges() {
  try {
    const r = await fetch('/api/today_edges');
    if (!r.ok) throw new Error(`today_edges HTTP ${r.status}`);
    todayEdgesList = await r.json();
  } catch(e) { console.error('today_edges fetch failed', e); }
  try {
    renderTodayEdges();
  } catch(e) { console.error('renderTodayEdges threw', e); }
}

function renderTodayEdges() {
  const el = document.getElementById('today-edges-body');
  if (!el) return;
  if (!todayEdgesList.length) {
    el.innerHTML = '<div class="empty">No live positions. Flagged edges will appear here.</div>';
    return;
  }

  // Build live lookup: ticker|side -> live edge object (≥3% only)
  const liveMap = {};
  for (const e of lastEdges) {
    liveMap[e.ticker + '|' + e.side] = e;
  }

  const rows = [...todayEdgesList].reverse().map(b => {
    const key  = b.ticker + '|' + b.side;
    const live = liveMap[key];           // present only if ≥3% in last scan
    const snap = marketSnapshot[key];    // present for ANY market scanned this cycle

    // ── Resolve current edge from best available source ───────────────────
    // Priority: live (full edge obj) > snapshot (all scanned) > nothing (PASS)
    const curEdgePct = live != null
      ? (live.edge_pct != null ? live.edge_pct : 0)
      : (snap != null ? snap.edge_pct : null);
    const curFair    = live != null ? live.fair
                     : snap != null ? snap.fair : null;
    const curKalshi  = live != null ? live.kalshi
                     : snap != null ? snap.kalshi : null;
    const inSnapshot = snap != null || live != null;

    // ── Cutoff price: max Kalshi price that preserves ≥3% edge ───────────
    // curFair is already expressed from the perspective of the bet side:
    //   YES bet → curFair = fair_yes     → max YES price = fair_yes - 0.03
    //   NO  bet → curFair = 1-fair_yes   → max NO  price = (1-fair_yes) - 0.03
    // Using the same formula (curFair - MIN_EDGE) is correct for both sides.
    // Bug: the old code used (1 - curFair - MIN_EDGE) for NO, which substituted
    // fair_yes back in — causing aboveCutoff to fire even on strong NO edges.
    const MIN_EDGE = 0.03;
    let cutoffLabel = null;
    let cutoffCents = null;
    if (curFair != null) {
      const maxPrice = curFair - MIN_EDGE;
      if (maxPrice > 0 && maxPrice < 1) {
        cutoffCents = maxPrice;
        cutoffLabel = `${b.side} ≤ ${kalshiToAmerican(maxPrice)}`;
      }
    }
    const liveKalshiSide = curKalshi != null
      ? (b.side === 'YES' ? curKalshi : curKalshi)   // snapshot already stores the bet-side price
      : null;
    const aboveCutoff = cutoffCents != null && liveKalshiSide != null
      && liveKalshiSide > cutoffCents + 0.005;

    let recLabel, recColor, recTip;
    const drift    = live != null && live.drift_pct != null ? live.drift_pct : 0;
    const driftBad = drift <= -3;

    if (!inSnapshot) {
      // Not in scan at all — Pinnacle offline or market closed
      recLabel = 'PASS';
      recColor = 'var(--muted)';
      recTip   = 'Market not found in latest scan — Pinnacle data unavailable or Kalshi market closed';
    } else if (curEdgePct === null || curEdgePct <= 0 || aboveCutoff) {
      recLabel = 'PASS';
      recColor = '#f85149';
      recTip   = aboveCutoff && cutoffLabel
        ? `Kalshi price above cutoff for 3% edge (${cutoffLabel}). Current edge: ${curEdgePct != null ? curEdgePct.toFixed(1) : '—'}%. Line has moved — do not bet.`
        : `Edge gone — current edge ${curEdgePct != null ? curEdgePct.toFixed(1) : '—'}% (was +${b.edge_pct}% at flag). Kalshi corrected.`;
    } else if (curEdgePct < 3.0) {
      recLabel = `WEAK +${curEdgePct.toFixed(1)}%`;
      recColor = '#e3a53a';
      recTip   = `Edge compressed to +${curEdgePct.toFixed(1)}% — below the 3% threshold. Was +${b.edge_pct}% at flag. Not worth the fee risk.`;
    } else if (driftBad) {
      recLabel = cutoffLabel ? `BET if ${cutoffLabel}` : `BET +${curEdgePct.toFixed(1)}%`;
      recColor = '#e3a53a';
      recTip   = `Kalshi drifted ${Math.abs(drift).toFixed(1)}pp since flagged. Edge still +${curEdgePct.toFixed(1)}% but verify live price first.`;
    } else {
      recLabel = cutoffLabel ? `BET if ${cutoffLabel}` : `BET +${curEdgePct.toFixed(1)}%`;
      recColor = '#3fb950';
      recTip   = cutoffLabel
        ? `Current edge +${curEdgePct.toFixed(1)}%. Scan prices up to 2 min old — edge holds while ${cutoffLabel}.`
        : `Current edge +${curEdgePct.toFixed(1)}%. Always verify live price on Kalshi before placing.`;
    }
    const recBadge = `<span style="font-weight:700;font-size:12px;color:${recColor};" title="${recTip}">${recLabel}</span>`;

    const driftTxt = live != null && live.drift_pct != null && live.drift_pct !== 0
      ? `<span class="badge-drift">(${live.drift_pct > 0 ? '+' : ''}${live.drift_pct}%)</span>` : '';

    // ── American odds at flag time ──────────────────────────────────────────
    const flagKalshiAmer = b.kalshi_price != null ? kalshiToAmerican(b.kalshi_price) : '—';
    const flagFairAmer   = b.pin_prob_at_flag != null ? probToAmerican(b.pin_prob_at_flag / 100) : '—';
    const flagOddsTxt = `<span style="font-size:10px;color:var(--muted);display:block;margin-top:2px;">
      Kalshi <span style="color:var(--text);">${flagKalshiAmer}</span>
      &nbsp;·&nbsp; Fair <span style="color:var(--text);">${flagFairAmer}</span>
    </span>`;

    // ── Current American odds (from last scan — up to 2 min old) ─────────────
    // Use live (full edge obj) if available, else fall back to snapshot
    const curKalshiAmer = curKalshi != null ? kalshiToAmerican(curKalshi) : null;
    const curFairAmer   = curFair   != null ? probToAmerican(curFair)     : null;
    const lastScanEdge = inSnapshot && curEdgePct != null
      ? `<span style="color:${edgeColor(curEdgePct)};font-weight:700;">${curEdgePct > 0 ? '+' : ''}${pct(curEdgePct)}</span>
         <span style="font-size:10px;color:var(--muted);display:block;margin-top:2px;">
           Kalshi <span style="color:var(--text);">${curKalshiAmer || '—'}</span>
           &nbsp;·&nbsp; Fair <span style="color:var(--text);">${curFairAmer || '—'}</span>
         </span>`
      : `<span style="color:var(--muted);">—</span>`;

    // ── Kalshi direct link ──────────────────────────────────────────────────
    const seriesTicker = b.ticker.replace(/-\d+$/, '').toLowerCase();
    const marketTicker = b.ticker.toLowerCase();
    const kalshiUrl    = `https://kalshi.com/markets/${seriesTicker}/${marketTicker}`;
    const kalshiLink   = `<a href="${kalshiUrl}" target="_blank" rel="noopener"
      style="font-size:9px;color:#58a6ff;display:block;margin-top:3px;text-decoration:none;"
      title="Open live market on Kalshi to verify current price before placing">
      🔗 Verify on Kalshi ↗
    </a>`;

    // ── Game start time ────────────────────────────────────────────────────
    function parseTickerTime(ticker) {
      const MONTHS = {JAN:0,FEB:1,MAR:2,APR:3,MAY:4,JUN:5,JUL:6,AUG:7,SEP:8,OCT:9,NOV:10,DEC:11};
      const m = ticker.match(/-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d{2})(\d{2})/i);
      if (!m) return null;
      const yr = 2000 + parseInt(m[1]), mo = MONTHS[m[2].toUpperCase()];
      const dy = parseInt(m[3]), hr = parseInt(m[4]), mn = parseInt(m[5]);
      return new Date(Date.UTC(yr, mo, dy, hr + 4, mn));
    }
    const gameStart = b.game_time ? new Date(b.game_time) : parseTickerTime(b.ticker);
    let gameTimeBadge = '';
    if (gameStart && !isNaN(gameStart)) {
      const now = Date.now();
      const diffMs = gameStart - now;
      if (diffMs < 0) {
        gameTimeBadge = `<span style="display:block;font-size:10px;color:#f85149;font-weight:700;margin-top:3px;">● IN PLAY — do not bet</span>`;
      } else {
        const opts = {month:'short', day:'numeric', hour:'numeric', minute:'2-digit', timeZoneName:'short'};
        const label = gameStart.toLocaleString('en-US', opts);
        gameTimeBadge = `<span style="display:block;font-size:10px;color:var(--muted);margin-top:3px;" title="Game start time">🕐 ${label}</span>`;
      }
    }

    const flagTime = b.flagged_at ? fmtDate(b.flagged_at) : '—';
    const stake    = b.paper_stake != null ? `$${b.paper_stake.toFixed(0)}` : '—';
    const sideClass = b.side === 'YES' ? 'side-yes' : 'side-no';
    const tickerTxt = `<span style="display:block;font-size:8px;color:var(--muted);font-family:monospace;margin-top:2px;">${b.ticker}</span>`;

    // ── Line movement: entry line vs current live line ──────────────────────
    // entry: pin_line_at_flag (Pinnacle) or kalshi_line_at_flag (Kalshi threshold)
    // live:  snap.pin_line (from latest market_snapshot, updated every scan cycle)
    // Show as "8.0 ➔ 8.5" — blank if neither side has data.
    const entryLine = b.pin_line_at_flag != null ? b.pin_line_at_flag
                    : b.kalshi_line_at_flag != null ? b.kalshi_line_at_flag
                    : null;
    const liveLine  = snap != null && snap.pin_line != null ? snap.pin_line
                    : snap != null && snap.kalshi_line != null ? snap.kalshi_line
                    : null;
    let lineMoveTxt = '—';
    if (entryLine != null) {
      const entryFmt = Number.isInteger(entryLine) ? entryLine + '.0' : entryLine;
      if (liveLine != null && liveLine !== entryLine) {
        const liveFmt  = Number.isInteger(liveLine)  ? liveLine  + '.0' : liveLine;
        const moved    = liveLine !== entryLine;
        const color    = moved ? '#e3a53a' : 'var(--muted)';
        lineMoveTxt = `<span style="font-weight:600;color:${color};white-space:nowrap;">${entryFmt} ➔ ${liveFmt}</span>`;
      } else {
        lineMoveTxt = `<span style="color:var(--muted);white-space:nowrap;">${entryFmt}</span>`;
      }
    }

    return `<tr>
      <td style="font-size:11px;color:var(--muted);white-space:nowrap;">${flagTime}</td>
      <td>${matchupHtml(b.matchup)}${gameTimeBadge}</td>
      <td class="prop-col" style="font-size:12px;">${b.title}${kalshiLineBadge(b)}${driftTxt}${tickerTxt}${kalshiLink}</td>
      <td class="${sideClass}">${b.side}</td>
      <td class="num" style="color:${edgeColor(b.edge_pct)};font-weight:700;">+${pct(b.edge_pct)}${flagOddsTxt}</td>
      <td class="num">${lineMoveTxt}</td>
      <td class="num">${lastScanEdge}</td>
      <td class="num">${recBadge}</td>
      <td class="num">${stake}</td>
    </tr>`;
  }).join('');

  el.innerHTML = `<table>
    <thead><tr>
      <th>Flagged</th><th>Matchup</th><th>Bet</th><th>Side</th>
      <th class="num">Edge @ Flag</th>
      <th class="num" style="white-space:nowrap;">Line (Entry ➔ Live)</th>
      <th class="num">Last Scan <span style="font-size:9px;font-weight:400;color:var(--muted);">(≤2 min)</span></th>
      <th class="num">Status</th>
      <th class="num">Stake</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

// ── Live Edges ≥5% (kept for internal use / referencing renderLiveEdges callers) ──
function renderLiveEdges() {
  // Show ≥5% only — clean, high-confidence display.
  // Edges from 3–5% are tracked silently in the paper portfolio for data.
  const qualified = lastEdges
    .filter(e => e.edge_pct >= 5)
    .sort((a, b) => {
      // Sharp-led edges bubble to the top within same edge tier
      if (b.sharp_led !== a.sharp_led) return b.sharp_led ? 1 : -1;
      return b.edge_pct - a.edge_pct;
    });
  const bankroll  = getBankroll();

  if (!qualified.length) {
    _setHTML('live-edges-body', '<div class="empty">No edges ≥5% right now. 3–5% edges are tracked silently in the paper portfolio. Scanning every 2 min.</div>');
    return;
  }

  function fmtAmer(n) { return n == null ? '—' : n > 0 ? `+${n}` : `${n}`; }

  const rows = qualified.map(e => {
    const consReason  = e.consensus_reason || '';
    const confBooks   = consReason.replace('Confirmed by ','').replace('Pinnacle','PIN').replace('DraftKings','DK').replace('FanDuel','FD');
    const sharpBadge  = e.sharp_led
      ? `<span class="badge-sharp" title="PIN moved vs DK/FD">⚡ SHARP</span>` : '';
    const ageMins     = e.age_min != null ? e.age_min : 0;
    const ageBadge    = ageMins < 5 && ageMins > 0
      ? `<span style="font-size:9px;color:#3fb950;margin-left:4px;font-weight:600;">●NEW</span>`
      : ageMins >= 10 ? `<span style="font-size:9px;color:#e3a53a;margin-left:4px;">⚠${Math.round(ageMins)}m</span>` : '';
    const fairAmer    = e.fair != null ? probToAmerican(e.fair) : '—';
    const kalshiAmer  = fmtAmer(kalshiToAmerican(e.kalshi));
    const bet         = kellyBet(e.fair, e.kalshi, bankroll, e.mkt_type);
    const kellyDollars = bet != null ? `$${bet.toFixed(0)}` : '—';
    const clvPenalty  = clvMultipliers[e.mkt_type] === 0.5;
    const pclv        = e.projected_clv_pct;
    const pclvTxt     = pclv != null
      ? `<span class="${pclv>0?'clv-pos':'clv-neg'}">${pclv>=0?'+':''}${pclv.toFixed(1)}pp</span>`
      : '<span class="clv-neu">—</span>';
    const isEquivLive  = e.fair_source === 'equiv';
    const fairAmерCell = isEquivLive
      ? `<span style="color:#e3a53a;" title="⚠ Pinnacle does not offer this exact line. Fair value extrapolated from nearest Pinnacle line using equivalence rule — verify this threshold directly on Pinnacle before betting.">${fairAmer} <span style="font-size:9px;font-weight:700;">~LINE</span></span>`
      : `<span style="color:#58a6ff;" title="✓ Pinnacle has this exact line">${fairAmer}</span>`;

    return `<tr>
      <td>${matchupHtml(e.matchup)}</td>
      <td class="prop-col">${e.title}${sharpBadge}${ageBadge}${trackBtn(e)}</td>
      <td class="side-${e.side.toLowerCase()}">${e.side}</td>
      <td class="num" style="color:${edgeColor(e.edge_pct)};font-weight:700;">+${pct(e.edge_pct)}</td>
      <td class="num">${fairAmерCell}</td>
      <td class="num" style="color:#e3a53a;">${kalshiAmer}</td>
      <td class="num">${pclvTxt}</td>
      <td class="num kelly-val">${kellyDollars}${clvPenalty ? ' <span style="color:#e3a53a;font-size:9px;">½</span>' : ''}</td>
      <td style="font-size:10px;color:var(--muted);">${confBooks}</td>
    </tr>`;
  }).join('');

  _setHTML('live-edges-body', `
    <table>
      <thead><tr>
        <th>Matchup</th><th>Bet</th><th>Side</th>
        <th class="num">Adj. EV</th>
        <th class="num">Fair (PIN)</th>
        <th class="num">Kalshi</th>
        <th class="num">Proj. CLV</th>
        <th class="num">Kelly $</th>
        <th>Confirmed By</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`);
}

function renderTop10() {
  // All edges ≥3% — complete pick list. Sharp-led and fresh edges sort higher.
  function _edgeSortScore(e) {
    const freshMult = (e.age_min != null && e.age_min < 5) ? 1.2 : 1.0;
    const sharpMult = e.sharp_led ? 1.3 : 1.0;
    return sharpMult * freshMult * (e.confidence != null ? e.confidence : 0.5) * e.edge_pct;
  }
  const combined = [...lastEdges]
    .sort((a, b) => _edgeSortScore(b) - _edgeSortScore(a));

  if (!combined.length) {
    _setHTML('top10-body', '<div class="empty">No edges ≥3% right now. Scanning every 2 min.</div>');
    return;
  }
  const bankroll = getBankroll();
  let rows = combined.map((e, i) => {
    const isNew  = !prevEdgeKeys.has(edgeKey(e));
    const newBadge = isNew ? '<span class="badge-new">NEW</span>' : '';
    const bet  = kellyBet(e.fair, e.kalshi, bankroll, e.mkt_type);
    const clvPenalty = clvMultipliers[e.mkt_type] === 0.5;
    const kellyCell = bet != null
      ? `$${bet.toFixed(0)}${clvPenalty ? ' <span style="font-size:9px;color:#e3a53a;" title="CLV penalty active: 0.5× stake">½</span>' : ''}`
      : '—';
    const isProp = e.mkt_type === 'prop';
    const _sp = sportOf(e);
    const sportColors = { mlb: ['#1a2d1a','#3fb950'], nba: ['#1a1f3a','#58a6ff'] };
    const [sBg, sFg] = sportColors[_sp] || sportColors.mlb;
    const sportLabel = _sp.toUpperCase();
    const typeTag = isProp
      ? `<span style="font-size:9px;background:#2d1f4e;color:#a371f7;padding:1px 4px;border-radius:3px;margin-right:4px;">PROP</span>`
      : `<span style="font-size:9px;background:${sBg};color:${sFg};padding:1px 4px;border-radius:3px;margin-right:4px;">${sportLabel}</span>`;
    const unvalidatedBadge = isProp ? '<span class="badge-unvalidated">⚠ UNVALIDATED</span>' : '';
    const booksCell = (e.books_used && e.books_used.length)
      ? `<span style="font-size:9px;color:var(--muted);">${e.books_used.map(b=>b.replace('draftkings','DK').replace('fanduel','FD').replace('pinnacle','PIN')).join('+')}</span>`
      : '';
    // Age / freshness badge
    const ageMins = e.age_min != null ? e.age_min : 0;
    // 10–15 min: STALE RISK warning  |  < 5 min: FRESH highlight  |  5–10 min: age label only
    const ageBadge = ageMins >= 10
      ? `<span style="display:inline-block;font-size:9px;background:#2d1a00;color:#e3a53a;padding:1px 5px;border-radius:3px;margin-left:5px;vertical-align:middle;" title="Edge is ${Math.round(ageMins)}m old — market may have already corrected">⚠️ STALE RISK</span>`
      : ageMins >= 5
        ? `<span style="font-size:9px;color:var(--muted);margin-left:5px;" title="${ageMins.toFixed(0)}m since first seen">⏱ ${Math.round(ageMins)}m</span>`
        : ageMins > 0
          ? `<span style="font-size:9px;color:#3fb950;margin-left:5px;font-weight:600;" title="Fresh edge — first seen ${ageMins.toFixed(1)}m ago">● FRESH</span>`
          : '';
    // Per-book no-vig odds for top-10 table (same logic as exec table)
    const pbn10 = e.per_book_novig || {};
    const bOrder10 = ['pinnacle','draftkings','fanduel'];
    const bAbbr10  = {pinnacle:'PIN', draftkings:'DK', fanduel:'FD'};
    const bLines10 = bOrder10.filter(b => pbn10[b]).map(b => {
      const bd = pbn10[b];
      const yA = bd.yes_american != null ? (bd.yes_american > 0 ? '+'+bd.yes_american : bd.yes_american) : '—';
      const nA = bd.no_american  != null ? (bd.no_american  > 0 ? '+'+bd.no_american  : bd.no_american)  : '—';
      const yP = bd.yes_prob != null ? (bd.yes_prob*100).toFixed(1)+'%' : '?';
      const nP = bd.no_prob  != null ? (bd.no_prob *100).toFixed(1)+'%' : '?';
      return `${bAbbr10[b]}: YES ${yA} (${yP})  NO ${nA} (${nP})`;
    });
    const tip10 = bLines10.length
      ? `No-vig per book:\\n${bLines10.join('\\n')}\\nFair value: Pinnacle only`
      : 'Fair value: Pinnacle only (DK+FD confirm)';

    // Fair-value American odds for the exact Kalshi threshold
    const fairAmer10  = e.fair != null ? probToAmerican(e.fair) : '—';
    const isEquiv     = e.fair_source === 'equiv';
    const equivBadge  = isEquiv
      ? `<span style="font-size:9px;background:#2d1f00;color:#e3a53a;padding:1px 5px;border-radius:3px;margin-left:4px;border:1px solid #5a3d00;font-weight:700;" title="⚠ Pinnacle does not offer this exact line. Fair value is extrapolated from their nearest line using the equivalence rule — verify this threshold directly on Pinnacle before betting.">~LINE</span>`
      : '';
    const tip10full   = e.fair != null
      ? `Fair value for this exact Kalshi threshold: ${fairAmer10}${isEquiv ? '\\n\\n⚠ EQUIVALENCE RULE: Pinnacle does not have this exact line. Fair value extrapolated from nearest Pinnacle line. Verify on Pinnacle before betting.' : '\\n\\n✓ Pinnacle has this exact line.'}\\n\\n${tip10}`
      : tip10;
    const cAmerTxt = fairAmer10 !== '—'
      ? `<span style="font-weight:700;color:${isEquiv ? '#e3a53a' : '#58a6ff'};" title="${tip10full}">${fairAmer10}</span>${equivBadge}`
      : `<span style="color:var(--muted)" title="${tip10full}">—</span>`;

    // Kalshi in American odds — price is from last scan, not live
    const kAmer10 = kalshiToAmerican(e.kalshi);
    // Compute how many seconds ago the Kalshi price was captured
    const kPriceTs = e.kalshi_price_ts ? new Date(e.kalshi_price_ts) : null;
    const kAgeSec  = kPriceTs ? Math.round((Date.now() - kPriceTs.getTime()) / 1000) : null;
    const kAgeStr  = kAgeSec != null
      ? (kAgeSec < 60 ? `${kAgeSec}s ago` : `${Math.round(kAgeSec/60)}m ago`)
      : '';
    // Stale (>90s) = amber warning; fresh = normal text color
    const kPriceColor = (kAgeSec != null && kAgeSec > 90) ? '#e3a53a' : 'var(--text)';
    const kStaleBadge = (kAgeSec != null && kAgeSec > 90)
      ? `<span style="font-size:8px;color:#e3a53a;margin-left:3px;" title="Price captured ${kAgeStr} — click ✓ Check for live price">⏱ ${kAgeStr}</span>`
      : (kAgeSec != null ? `<span style="font-size:8px;color:var(--muted);margin-left:3px;">⏱ ${kAgeStr}</span>` : '');
    const kTip = `Kalshi ask: ${e.kalshi_pct}% (${kAgeStr || 'age unknown'})\n⚠ This price was captured at scan time.\nClick ✓ Check to see the live price.\nTicker: ${e.ticker}`;
    const kAmerTxt = kAmer10 !== '—'
      ? `<span style="color:${kPriceColor};" title="${kTip}">${kAmer10}</span>${kStaleBadge}`
      : '—';

    // Consensus badge for top-10 (same data source as exec table)
    const consReason10  = e.consensus_reason || '';
    const confBooksShort = consReason10.replace('Confirmed by ', '').replace('Pinnacle','PIN').replace('DraftKings','DK').replace('FanDuel','FD');
    const consBadge10 = consReason10
      ? `<span style="font-size:9px;background:#0d2119;color:#3fb950;padding:1px 4px;border-radius:3px;margin-left:3px;border:1px solid #1a4a2a;" title="${consReason10}">✓ ${confBooksShort}</span>`
      : '';

    const tickerBadge = `<span style="display:block;font-size:8px;color:var(--muted);margin-top:2px;font-family:monospace;" title="Kalshi ticker — search this on kalshi.com to find the exact market">${e.ticker}</span>`;
    return `<tr>
      <td><span class="rank-num">#${i+1}</span></td>
      <td>${matchupHtml(e.matchup)}</td>
      <td class="prop-col">${typeTag}${e.title}${kalshiLineBadge(e)}${newBadge}${unvalidatedBadge}${consBadge10}${ageBadge}${trackBtn(e)}${tickerBadge}</td>
      <td class="side-${e.side.toLowerCase()}">${e.side}</td>
      <td class="num" style="color:${edgeColor(e.edge_pct)};font-weight:700;">+${pct(e.edge_pct)}</td>
      <td class="num" title="${tip10}">${cAmerTxt} <span style="font-size:9px;color:var(--muted);">→</span> ${kAmerTxt}</td>
      <td class="num">${confStars(e.confidence, e.books_used)} ${booksCell}</td>
      <td class="num kelly-val">${kellyCell}</td>
      <td class="num"><button onclick="runValidate('${e.ticker}','${e.side}')" style="background:#0d2119;color:#3fb950;border:1px solid #1a4a2a;border-radius:4px;padding:3px 8px;cursor:pointer;font-size:11px;font-weight:600;">✓ Check</button></td>
    </tr>`;
  }).join('');
  _setHTML('top10-body', `<table>
    <thead><tr>
      <th>#</th><th>Matchup / Player</th><th>Prop</th><th>Side</th>
      <th class="num">Adj. EV</th>
      <th class="num" title="Model fair value for this exact Kalshi threshold → Kalshi ask price (both in American odds). Hover for per-book breakdown.">Fair → Kalshi</th>
      <th class="num" title="How closely Pinnacle, DraftKings, and FanDuel agree on fair probability. ★★★ = tight agreement = higher confidence.">Confidence</th>
      <th class="num">Kelly Bet</th>
      <th class="num" title="Re-fetch Kalshi + Pinnacle right now and confirm the edge is still valid before betting">Pre-Bet Check</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`);
}

// ── Pre-Bet Validator ─────────────────────────────────────────────────────────
async function runValidate(ticker, side) {
  const modal = document.getElementById('validate-modal');
  const body  = document.getElementById('validate-body');
  modal.style.display = 'flex';
  body.innerHTML = '<div class="empty"><span class="spinner"></span> Re-fetching Kalshi + Pinnacle…</div>';

  try {
    const r = await fetch(`/api/validate/${encodeURIComponent(ticker)}?side=${side}`);
    const d = await r.json();

    if (d.error) {
      body.innerHTML = `<div style="color:var(--red);padding:16px;">Error: ${d.error}</div>`;
      return;
    }

    const validColor  = d.valid ? 'var(--green)' : 'var(--red)';
    const validIcon   = d.valid ? '✓' : '✗';
    const validLabel  = d.valid ? 'VALID' : 'DO NOT BET';

    const kMovedColor = d.kalshi_moved == null ? 'var(--muted)'
      : Math.abs(d.kalshi_moved) < 1 ? 'var(--green)'
      : d.kalshi_moved > 0 ? 'var(--red)' : 'var(--green)';
    const fMovedColor = d.fair_moved == null ? 'var(--muted)'
      : Math.abs(d.fair_moved) < 1 ? 'var(--green)'
      : d.fair_moved > 0 ? 'var(--green)' : 'var(--red)';

    const row = (label, val, color) =>
      `<tr><td style="color:var(--muted);padding:6px 12px;font-size:12px;">${label}</td>
           <td style="color:${color||'var(--text)'};padding:6px 12px;font-size:13px;font-weight:600;">${val}</td></tr>`;

    const sign = v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1);

    body.innerHTML = `
      <div style="padding:20px 24px 8px;">
        <div style="font-size:22px;font-weight:800;color:${validColor};margin-bottom:8px;">${validIcon} ${validLabel}</div>
        <div style="font-size:13px;color:var(--muted);margin-bottom:16px;line-height:1.5;">${d.reason}</div>
        <table style="width:100%;border-collapse:collapse;">
          ${row('Ticker', ticker + ' ' + side, '#58a6ff')}
          ${row('Age', d.age_seconds != null ? d.age_seconds + 's ago' : '—', d.staleness_ok ? 'var(--green)' : 'var(--red)')}
          ${row('Kalshi price', d.kalshi_was + '¢ → ' + (d.kalshi_now != null ? d.kalshi_now + '¢' : '—'), 'var(--text)')}
          ${row('Kalshi moved', sign(d.kalshi_moved) + 'pp', kMovedColor)}
          ${row('Fair value', d.fair_was + '¢ → ' + (d.fair_now != null ? d.fair_now + '¢' : '—'), 'var(--text)')}
          ${row('Fair moved', sign(d.fair_moved) + 'pp', fMovedColor)}
          ${row('Edge at flag', '+' + d.edge_was + '%', 'var(--muted)')}
          ${row('Edge now', d.edge_now != null ? (d.edge_now > 0 ? '+' : '') + d.edge_now + '%' : '—',
            d.edge_now == null ? 'var(--muted)' : d.edge_now >= 3 ? 'var(--green)' : 'var(--red)')}
        </table>
      </div>`;
  } catch(e) {
    body.innerHTML = `<div style="color:var(--red);padding:16px;">Request failed: ${e}</div>`;
  }
}

function closeValidateModal() {
  document.getElementById('validate-modal').style.display = 'none';
}

// ── Performance ───────────────────────────────────────────────────────────────
async function fetchPerformance() {
  try {
    const V2_START = '2026-06-08';
    const since = (document.getElementById('perf-since') || {}).value || V2_START;
    const url   = `/api/performance?since=${encodeURIComponent(since)}`;
    const r = await fetch(url);
    const d = await r.json();
    // update filter label
    const lbl = document.getElementById('perf-filter-label');
    if (lbl) {
      if (since) {
        const isDefault = since === V2_START;
        lbl.textContent = isDefault
          ? `V2.0 data only (from ${since}) — clear to see all bets`
          : `Showing bets from ${since} onward`;
        lbl.style.color = isDefault ? 'var(--muted)' : 'var(--yellow, #ffe082)';
      } else {
        lbl.textContent = 'Showing all bets including pre-fix data';
        lbl.style.color = 'var(--yellow, #ffe082)';
      }
    }
    // Store CLV multipliers globally so kellyBet() in the live table uses them
    if (d.clv_multipliers) clvMultipliers = d.clv_multipliers;
    renderPerformance(d);
  } catch(e) { console.error('perf fetch failed', e); }
}

function renderPerformance(d) {
  let perfBodyHtml = '';   // accumulate all perf-body sections; written once at the end
  function pill(label, value, cls) {
    return `<div class="stat-pill"><div class="label">${label}</div><div class="value ${cls||''}">${value}</div></div>`;
  }
  function na(v, fmt) { return v != null ? fmt(v) : '—'; }
  function uClass(v) { return v == null ? '' : v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : 'pnl-neu'; }
  function sign(v)   { return v >= 0 ? '+' : ''; }
  function fmt$(v)   { return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2); }

  const kellyPctClass = uClass(d.total_kelly_pct);

  // Win rate vs Kalshi-implied tells you if the model is beating the market
  let modelCallout = '—';
  if (d.win_rate != null && d.avg_kalshi_implied != null) {
    const diff = (d.win_rate - d.avg_kalshi_implied).toFixed(1);
    const cls  = diff >= 0 ? 'pnl-pos' : 'pnl-neg';
    modelCallout = `<span class="${cls}">${sign(diff)}${diff}%</span> vs market`;
  }

  const bankroll = d.kelly_bankroll || 1000;

  // CLV penalty notice — list any types currently running at 0.5× stake
  const penalised = Object.entries(d.clv_multipliers || {})
    .filter(([, v]) => v < 1.0)
    .map(([k]) => k);
  const penaltyNote = penalised.length
    ? `<p style="font-size:11px;color:#e3a53a;padding:4px 0 8px;margin:0;">
        ⚠️ <strong>CLV penalty active</strong> for: ${penalised.join(', ')}.
        Stake is 0.5× until avg CLV for these types turns positive (min ${""" + str(_CLV_PENALTY_MIN_SAMPLE) + """} data points required).
       </p>`
    : '';

  // CLV source breakdown — shows how much of avg_clv is real vs proxy measurement
  const clvSrc = d.clv_by_source || {};
  const clvSrcRows = [
    { key: 'pin',       label: 'True Close (PIN)',  tip: 'Pre-close Pinnacle fetch fired — probability measured at the actual closing line. Most accurate.' },
    { key: 'pin_entry', label: 'Entry Snap (PIN)',  tip: 'Pinnacle probability captured at flag time, not close. CLV ≈ original detected edge — inflates the blended average.' },
    { key: 'kalshi',    label: 'Kalshi Drift',      tip: 'No Pinnacle reference — CLV measured from Kalshi bid/ask movement only. Noisier signal.' },
  ].filter(r => clvSrc[r.key]).map(r => {
    const s   = clvSrc[r.key];
    const cls = s.avg_clv > 0 ? 'pnl-pos' : s.avg_clv < 0 ? 'pnl-neg' : '';
    const isPinEntry = r.key === 'pin_entry';
    return `<tr>
      <td style="font-size:11px;padding:4px 10px;" title="${r.tip}">
        ${r.label}${isPinEntry ? ' <span style="color:#e3a53a;font-size:9px;">≈ entry edge, not CLV</span>' : ''}
      </td>
      <td class="num" style="font-size:11px;padding:4px 10px;color:var(--muted);">${s.count} bets</td>
      <td class="num" style="font-size:11px;padding:4px 10px;">
        <span class="${cls}">${s.avg_clv > 0 ? '+' : ''}${s.avg_clv}%</span>
      </td>
    </tr>`;
  }).join('');
  const clvBreakdown = clvSrcRows ? `
    <div style="margin:0 0 10px;border:1px solid var(--border);border-radius:6px;overflow:hidden;">
      <div style="padding:6px 10px;background:#161b22;font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border);">
        CLV Source Breakdown — what's behind the Avg CLV number
      </div>
      <table style="width:100%;border-collapse:collapse;">
        <thead><tr>
          <th style="font-size:10px;color:var(--muted);text-transform:uppercase;padding:4px 10px;text-align:left;">Source</th>
          <th class="num" style="font-size:10px;color:var(--muted);text-transform:uppercase;padding:4px 10px;">Sample</th>
          <th class="num" style="font-size:10px;color:var(--muted);text-transform:uppercase;padding:4px 10px;">Avg CLV</th>
        </tr></thead>
        <tbody>${clvSrcRows}</tbody>
      </table>
    </div>` : '';

  // Edge health: color based on absolute value vs the 3% betting threshold,
  // not relative to all-time avg. Red = genuinely bad (below threshold or no data).
  const recentEdge    = d.recent_avg_edge;
  const allTimeEdge   = d.avg_edge;
  const edgeDrop      = (recentEdge != null && allTimeEdge != null) ? allTimeEdge - recentEdge : null;
  const recentEdgeCls = recentEdge == null ? 'pnl-neg'   // no data — bad
                      : recentEdge < 3.0   ? 'pnl-neg'   // below betting threshold — bad
                      : recentEdge < 4.5   ? 'pnl-neu'   // above threshold but soft — amber
                      :                      'pnl-pos';   // healthy — green
  const recentEdgeTip = recentEdge == null
    ? `No settled bets in last 14 days — edge may be gone`
    : `14d avg entry edge: +${recentEdge}% across ${d.recent_bet_count} settled bets. All-time: +${allTimeEdge}%. ${edgeDrop > 0 ? '▼ ' + edgeDrop.toFixed(1) + 'pp below avg' : '✓ on pace'}`;
  const recentEdgeVal = recentEdge != null
    ? `<span class="${recentEdgeCls}" title="${recentEdgeTip}">+${recentEdge}% <span style="font-size:9px;">(${d.recent_bet_count})</span></span>`
    : `<span class="pnl-neg" title="${recentEdgeTip}">—</span>`;

  // Days since last bet indicator
  const dsLast = d.days_since_last_bet;
  const lastBetCls = dsLast == null ? '' : dsLast >= 3 ? 'pnl-neg' : dsLast >= 1.5 ? 'pnl-neu' : 'pnl-pos';
  const lastBetVal = dsLast == null ? '—'
    : dsLast < 1    ? `<span class="${lastBetCls}">Today</span>`
    : dsLast < 2    ? `<span class="${lastBetCls}">${dsLast.toFixed(1)}d ago</span>`
    :                 `<span class="${lastBetCls}">${Math.round(dsLast)}d ago</span>`;

  document.getElementById('perf-stats').innerHTML = `
    <div class="stat-row">
      ${pill('Tracked', d.total_bets)}
      ${pill('Won', d.won, 'pnl-pos')}
      ${pill('Lost', d.lost, 'pnl-neg')}
      ${pill('Open', d.open, 'pnl-neu')}
      ${pill('Win Rate', na(d.win_rate, v => v + '%'))}
      ${pill('Avg Entry Edge', na(d.avg_edge, v => `<span title="Average stated edge at time of flagging — settled bets only. Not a performance metric.">${'+' + v + '%'}</span>`))}
      ${pill('Edge (14d)', recentEdgeVal)}
      ${pill('Last Bet', lastBetVal)}
      ${pill('Kelly P&amp;L (% bank)', d.total_kelly_pct != null ? `<span class="${kellyPctClass}">${sign(d.total_kelly_pct)}${d.total_kelly_pct.toFixed(2)}%</span>` : '—')}
      ${pill('Flat Units', d.total_units != null ? `<span class="${d.total_units >= 0 ? 'pnl-pos' : 'pnl-neg'}" title="$1 flat stake on every bet regardless of sizing. Total: ${sign(d.total_units)}${d.total_units}u | Avg: ${sign(d.avg_units)}${d.avg_units}u/bet">${sign(d.total_units)}${d.total_units}u <span style="font-size:10px;opacity:0.7;">(${sign(d.avg_units)}${d.avg_units}/bet)</span></span>` : '—')}
      ${pill('Entry Discount', d.avg_entry_discount != null ? `<span class="${d.avg_entry_discount >= 0 ? 'pnl-pos' : 'pnl-neg'}" title="Avg (Pinnacle fair value − Kalshi entry) at time of bet. This is your actual alpha — the mispricing you captured. NOT affected by what happened after.">${d.avg_entry_discount > 0 ? '+' : ''}${d.avg_entry_discount}pp</span>` : '—')}
      ${pill('Pin Drift (True CLV)', d.avg_pin_drift != null ? `<span class="${d.avg_pin_drift >= 0 ? 'pnl-pos' : 'pnl-neg'}" title="Avg (Pinnacle close − Pinnacle at entry). Did the sharp market move in your favor AFTER you bet? Positive = Pin confirmed your edge. Zero = you got a good price but Pin didn't move. This is true closing line value.">${d.avg_pin_drift > 0 ? '+' : ''}${d.avg_pin_drift}pp</span>` : '—')}
      <div class="stat-pill"><div class="label">Model vs Market</div><div class="value">${modelCallout}</div></div>
    </div>
    ${clvBreakdown}
    ${penaltyNote}
    <p style="font-size:11px;color:var(--muted);padding-bottom:10px;">
      P&amp;L sized by <strong>0.25 Fractional Kelly</strong>, capped at 5% per bet.
      CLV-penalised types run at 0.5× until their closing-line value stabilises.
      Reported as <strong>% of bankroll</strong> — a 5¢ longshot loss shows −0.15%, not −1 unit.
      ${d.corrupted_excluded ? `<br><span style="color:#8b949e;">⚠ ${d.corrupted_excluded} bets excluded from all stats (wrong Pinnacle reference — UTC/ET date collision, fixed May 2026). Shown as <strong>BAD REF</strong> in the history table.</span>` : ''}
    </p>`;

  // By-type breakdown table
  const PROP_LABELS = new Set(['Strikeouts (K)', 'Home Runs', 'Hits', 'Total Bases', 'RBIs', 'MLB Props', 'NBA Props']);
  const TYPE_ORDER  = ['MLB Total', 'MLB Spread', 'Strikeouts (K)', 'Home Runs', 'Hits', 'Total Bases', 'RBIs', 'MLB Props', 'NBA Props'];
  if (d.by_type && d.by_type.length) {
    const sorted = [...d.by_type].sort((a, b) => {
      const ai = TYPE_ORDER.indexOf(a.label); const bi = TYPE_ORDER.indexOf(b.label);
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
    const typeRows = sorted.map(t => {
      const insuf  = t.insufficient_data;
      const isProp = PROP_LABELS.has(t.label);
      const isShadowRow = t.shadow === true;
      const wrCls  = insuf || t.win_rate == null ? '' : 'pnl-pos';
      const kpct   = t.kelly_pct;
      const kcls   = kpct == null ? '' : kpct > 0 ? 'pnl-pos' : 'pnl-neg';
      const wrCell = isShadowRow
        ? `<span style="color:var(--muted);font-size:10px;">CLV only</span>`
        : insuf
          ? `<span class="insufficient-data" title="Need 20+ settled bets for reliable stats">Insufficient data (${t.sample_size})</span>`
          : t.win_rate != null ? `<span class="${wrCls}">${t.win_rate}%</span>` : '—';
      const shadowRowBadge = isShadowRow
        ? ` <span style="font-size:9px;font-weight:700;color:#58a6ff;background:rgba(88,166,255,0.10);border:1px solid rgba(88,166,255,0.3);border-radius:3px;padding:1px 4px;vertical-align:middle;">SHADOW</span>`
        : '';
      const labelCell = isProp && !isShadowRow
        ? `<span style="font-weight:600;">${t.label}</span>${shadowRowBadge} <span class="badge-unvalidated">⚠ UNVALIDATED</span>`
        : `<span style="font-weight:600;">${t.label}</span>${shadowRowBadge}`;
      const kellyCell = kpct != null
        ? `<span class="${kcls}" title="$${t.kelly_dollars != null ? Math.abs(t.kelly_dollars).toFixed(0) : '?'} on $${bankroll} bank">${sign(kpct)}${kpct.toFixed(2)}%</span>`
        : '—';
      // Prefer avg_pin_drift (true CLV: closing_pin − pin_at_entry) over old avg_clv (inflated by entry discount)
      const pd    = t.avg_pin_drift != null ? t.avg_pin_drift : null;
      const pdCls = pd == null ? '' : pd > 0 ? 'pnl-pos' : pd < 0 ? 'pnl-neg' : '';
      const clvCell = pd != null
        ? `<span class="${pdCls}" title="Avg Pin Drift for this market type: Pinnacle close minus Pinnacle at entry. True closing line value — positive = Pin confirmed your edge after you bet.">${sign(pd)}${pd.toFixed(2)}pp</span>`
        : '—';
      const shadowTotal = (t.shadow_won || 0) + (t.shadow_lost || 0);
      const shadowWr    = shadowTotal > 0 ? Math.round(100 * (t.shadow_won || 0) / shadowTotal) : null;
      const wonCell  = isShadowRow
        ? `<span style="color:var(--green);opacity:0.7;" title="Shadow — excluded from live stats">${t.shadow_won || 0}</span>`
        : t.won;
      const lostCell = isShadowRow
        ? `<span style="color:var(--red);opacity:0.7;" title="Shadow — excluded from live stats">${t.shadow_lost || 0}</span>`
        : t.lost;
      return `<tr style="${isShadowRow ? 'opacity:0.75;' : ''}">
        <td>${labelCell}</td>
        <td class="num pnl-pos">${wonCell}</td>
        <td class="num pnl-neg">${lostCell}</td>
        <td class="num">${isShadowRow && shadowTotal > 0 ? `<span style="color:var(--muted);font-size:10px;" title="Shadow win rate — not counted in live stats">${shadowWr}% (${shadowTotal})</span>` : wrCell}</td>
        <td class="num">${isShadowRow ? '<span style="color:var(--muted);font-size:10px;">$0 stake</span>' : kellyCell}</td>
        <td class="num">${clvCell}</td>
      </tr>`;
    }).join('');
    perfBodyHtml += `
      <table style="margin-bottom:8px;">
        <thead><tr>
          <th>Market Type</th><th class="num">Won</th><th class="num">Lost</th>
          <th class="num">Win Rate</th>
          <th class="num" title="Kelly P&amp;L as % of bankroll (hover for dollar amount)">Kelly P&amp;L (% bank)</th>
          <th class="num" title="Avg Pin Drift: Pinnacle close minus Pinnacle at entry. True CLV — did the sharp market confirm your edge after you bet?">Avg Pin Drift</th>
        </tr></thead>
        <tbody>${typeRows}</tbody>
      </table>`;
  }

  // ── Alpha section ────────────────────────────────────────────────────────
  if (d.alpha_buckets && d.alpha_buckets.length) {
    const entryDisc = d.avg_entry_discount;
    const pinDrift  = d.avg_pin_drift;
    const edSign    = entryDisc != null && entryDisc > 0 ? '+' : '';
    const pdSign    = pinDrift  != null && pinDrift  > 0 ? '+' : '';
    const edCls     = entryDisc != null ? (entryDisc >= 0 ? 'pnl-pos' : 'pnl-neg') : '';
    const pdCls     = pinDrift  != null ? (pinDrift  >= 0 ? 'pnl-pos' : 'pnl-neg') : '';
    const alphaRows = d.alpha_buckets.map(b => {
      const dCls  = b.delta > 0 ? 'pnl-pos' : b.delta < 0 ? 'pnl-neg' : '';
      const dSign = b.delta > 0 ? '+' : '';
      const insuf = b.n < 10;
      return `<tr>
        <td style="font-weight:600;">${b.label}</td>
        <td class="num">${b.n}</td>
        <td class="num">${insuf ? `<span style="color:var(--muted);font-size:10px;">${b.win_rate}% <em>(small n)</em></span>` : `<span class="${dCls}">${b.win_rate}%</span>`}</td>
        <td class="num" style="color:var(--muted);">${b.expected}%</td>
        <td class="num"><span class="${insuf ? '' : dCls}" title="Actual win rate minus expected win rate (Kalshi implied prob). Positive = outperforming market.">${dSign}${b.delta}pp ${insuf ? '<span style="font-size:9px;opacity:0.6;">(n<10)</span>' : ''}</span></td>
      </tr>`;
    }).join('');
    perfBodyHtml += `
      <details style="margin-bottom:12px;" open>
        <summary style="cursor:pointer;font-size:13px;font-weight:600;color:var(--fg);user-select:none;padding:6px 0;list-style:none;display:flex;align-items:center;gap:8px;">
          <span style="font-size:10px;color:var(--muted);">▶</span> Model Alpha
          <span style="font-size:11px;font-weight:400;color:var(--muted);margin-left:4px;">
            Entry Discount <span class="${edCls}">${entryDisc != null ? edSign + entryDisc + 'pp' : '—'}</span>
            &nbsp;·&nbsp; Pin Drift <span class="${pdCls}">${pinDrift != null ? pdSign + pinDrift + 'pp' : '—'}</span>
          </span>
        </summary>
        <div style="font-size:11px;color:var(--muted);margin:4px 0 8px;line-height:1.5;">
          <strong style="color:var(--fg);">Entry Discount</strong> = Pinnacle fair value − Kalshi entry price. This is your actual alpha — the mispricing you exploited at the moment of the bet.<br>
          <strong style="color:var(--fg);">Pin Drift</strong> = Pinnacle close − Pinnacle at entry. Did the sharp market confirm your read after you bet? Positive = Pinnacle agreed. Zero = you got a good price but Pin held flat.<br>
          <strong style="color:var(--fg);">Delta</strong> = actual win rate minus Kalshi's implied probability. Positive = outperforming market expectations.
        </div>
        <table style="font-size:12px;">
          <thead><tr>
            <th>Edge Bucket</th>
            <th class="num">N</th>
            <th class="num">Win Rate</th>
            <th class="num" title="What Kalshi implied your win probability was at entry">Expected</th>
            <th class="num" title="Actual minus expected — are higher edges winning proportionally more?">Delta</th>
          </tr></thead>
          <tbody>${alphaRows}</tbody>
        </table>
      </details>`;
  }

  // ── Data-quality audit table ─────────────────────────────────────────────
  if (d.source_audit && d.source_audit.length) {
    const hasWarn = d.source_audit.some(r => r.warn);
    const auditRows = d.source_audit.map(r => {
      const cls  = r.warn ? 'pnl-neg' : 'pnl-pos';
      const flag = r.warn ? ' ⚠' : '';
      return `<tr>
        <td style="font-family:monospace;font-size:11px;">${r.key}${flag}</td>
        <td class="num">${r.wins}/${r.total}</td>
        <td class="num"><span class="${cls}">${r.win_pct}%</span></td>
      </tr>`;
    }).join('');
    const headerCls = hasWarn ? 'pnl-neg' : '';
    perfBodyHtml += `
      <details style="margin-bottom:12px;">
        <summary style="cursor:pointer;font-size:12px;color:var(--muted);user-select:none;">
          <span class="${headerCls}">Data-quality audit${hasWarn ? ' ⚠ — one or more buckets below 38% win rate' : ''}</span>
        </summary>
        <table style="margin-top:6px;font-size:11px;">
          <thead><tr>
            <th>Bucket (type/clv_source)</th>
            <th class="num">W/L</th>
            <th class="num">Win Rate</th>
          </tr></thead>
          <tbody>${auditRows}</tbody>
        </table>
        <p style="font-size:10px;color:var(--muted);margin:4px 0 0;">
          &lt;38% win rate with N≥10 suggests the reference price for that bucket was wrong (wrong game, stale odds, or line mismatch). Investigate before trusting CLV for that bucket.
        </p>
      </details>`;
  }

  if (!d.bets.length) {
    perfBodyHtml += '<div class="empty">No bets tracked yet — edges appear after the next scan.</div>';
    _setHTML('perf-body', perfBodyHtml);
    return;
  }

  const PERF_PREVIEW = 15;
  // Sort newest-first so most recent action is immediately visible
  const sortedBets   = [...d.bets].sort((a, b) => (b.flagged_at || '').localeCompare(a.flagged_at || ''));
  const allPerfBets  = sortedBets.filter(b => b.clv_source !== 'corrupted_utc');
  const corruptBets  = sortedBets.filter(b => b.clv_source === 'corrupted_utc');
  const showAllPerf  = window._perfShowAll || false;
  const visiblePerf  = showAllPerf ? allPerfBets : allPerfBets.slice(0, PERF_PREVIEW);

  const renderPerfRow = (b) => {
    const now = Date.now();
    const gameStartMs = b.game_time ? new Date(b.game_time).getTime() : null;
    const isLive = b.status === 'open' && gameStartMs != null && now >= gameStartMs;
    const rClass = b.status === 'won' ? 'result-won' : b.status === 'lost' ? 'result-lost' : 'result-open';
    const rLabel = b.status === 'won' ? '✓ WON' : b.status === 'lost' ? '✗ LOST'
      : isLive ? '<span style="color:#ff4444;font-weight:600;animation:pulse 1.5s infinite;">● LIVE</span>'
      : '…';
    // Kelly bet size as % of bankroll (dollar amount in tooltip)
    const kBet   = b.shadow
      ? `<span style="color:var(--muted);font-size:11px;" title="Shadow market — no real stake">$0</span>`
      : b.kelly_bet_pct != null
        ? `<span class="kelly-val" title="$${b.kelly_bet_dollars != null ? b.kelly_bet_dollars.toFixed(0) : '?'} on $${bankroll} bank${b.clv_mult_applied < 1 ? ' — CLV penalty 0.5×' : ''}">${b.kelly_bet_pct.toFixed(2)}%${b.clv_mult_applied < 1 ? ' <span style="color:#e3a53a;font-size:9px;">½</span>' : ''}</span>`
        : '<span class="kelly-na">—</span>';
    // P&L as % of bankroll (dollar amount in tooltip)
    const kPnl   = b.kelly_pnl_pct != null
      ? `<span class="${uClass(b.kelly_pnl_pct)}" title="${fmt$(b.kelly_pnl_dollars || 0)} on $${bankroll} bank">${sign(b.kelly_pnl_pct)}${b.kelly_pnl_pct.toFixed(2)}%</span>`
      : b.status === 'open' && b.kelly_bet_pct != null
        ? `<span class="pnl-neu">open</span>`
        : '<span class="pnl-neu">—</span>';
    const ts = fmtDate(b.flagged_at);
    // Game time — parse ticker start time if stored, otherwise derive from ticker
    const gameTimeIso = b.game_time || null;
    const gameTimeCell = gameTimeIso
      ? (() => { const d = new Date(gameTimeIso); return `<span style="font-size:10px;color:var(--muted);" title="${d.toLocaleString()}">${d.toLocaleDateString('en-US',{month:'short',day:'numeric'})} ${d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',timeZoneName:'short'})}</span>`; })()
      : '<span style="color:var(--muted);font-size:10px;">—</span>';
    // Raw vs adj edge (raw only present on post-refactor bets)
    const edgeCell = b.raw_edge_pct != null && b.raw_edge_pct !== b.edge_pct
      ? `<span title="raw ${b.raw_edge_pct}% → adj ${b.edge_pct}%">${b.edge_pct}%<span style="color:var(--muted);font-size:10px;"> (raw ${b.raw_edge_pct}%)</span></span>`
      : `${b.edge_pct}%`;
    // Line Move: shows entry discount (alpha captured) and pin drift (true CLV)
    const entryK      = b.kalshi_price    != null ? (b.kalshi_price * 100).toFixed(0) : null;
    const pinAtEntry  = b.pin_prob_at_flag != null ? b.pin_prob_at_flag.toFixed(1)    : null;
    const pinAtClose  = b.closing_pin_pct  != null && b.status !== 'open' ? b.closing_pin_pct.toFixed(1) : null;
    const kalshiClose = b.closing_yes_pct  != null && b.status !== 'open' ? (b.closing_yes_pct).toFixed(0) : null;

    let lineMoveCell = '—';
    if (entryK != null) {
      // Row 1 — Entry Discount: how mispriced was Kalshi vs Pin at the moment we bet?
      let discountLine = '';
      if (b.entry_discount != null) {
        const dColor = b.entry_discount > 0 ? 'var(--green)' : 'var(--red)';
        const dSign  = b.entry_discount > 0 ? '+' : '';
        discountLine = `<div style="font-size:11px;" title="Entry Discount: Pinnacle fair value (${pinAtEntry}%) minus Kalshi entry (${entryK}¢). This is the alpha you captured — the actual market mispricing.">
          <span style="color:var(--muted);font-size:10px;">Discount </span><span style="color:${dColor};font-weight:700;">${dSign}${b.entry_discount}pp</span>
          <span style="color:var(--muted);font-size:10px;"> (Pin ${pinAtEntry}% vs K ${entryK}¢)</span>
        </div>`;
      } else if (pinAtEntry != null) {
        discountLine = `<div style="font-size:10px;color:var(--muted);">Pin at entry: ${pinAtEntry}%</div>`;
      }

      // Row 2 — Pin Drift: did the sharp market agree with us after we bet?
      let driftLine = '';
      if (b.pin_drift != null && b.status !== 'open') {
        const drColor = b.pin_drift > 0 ? 'var(--green)' : b.pin_drift < 0 ? 'var(--red)' : 'var(--fg)';
        const drSign  = b.pin_drift > 0 ? '+' : '';
        const driftTitle = `Pin Drift: Pinnacle moved ${drSign}${b.pin_drift}pp after entry (${pinAtEntry}% → ${pinAtClose}%). Positive = Pin agreed with your read.`;
        driftLine = `<div style="font-size:11px;margin-top:2px;" title="${driftTitle}">
          <span style="color:var(--muted);font-size:10px;">Pin Drift </span><span style="color:${drColor};font-weight:700;">${drSign}${b.pin_drift}pp</span>
          <span style="color:var(--muted);font-size:10px;"> (→ ${pinAtClose ?? 'open'}%)</span>
        </div>`;
      } else if (b.status === 'open') {
        driftLine = `<span style="color:var(--muted);font-size:10px;">→ open</span>`;
      }

      // Row 3 — Kalshi repricing: did the market rerate our contract?
      let kalshiLine = '';
      if (kalshiClose != null) {
        const kDelta = parseFloat(kalshiClose) - parseFloat(entryK);
        const kColor = kDelta > 0 ? 'var(--green)' : kDelta < 0 ? 'var(--red)' : 'var(--fg)';
        const kSign  = kDelta > 0 ? '+' : '';
        kalshiLine = `<div style="font-size:10px;margin-top:2px;color:var(--muted);" title="Kalshi repricing: market moved from ${entryK}¢ to ${kalshiClose}¢ by game start">
          K: ${entryK}¢ <span style="color:${kColor};">→ ${kalshiClose}¢ (${kSign}${kDelta.toFixed(0)}¢)</span>
        </div>`;
      }

      lineMoveCell = `${discountLine}${driftLine}${kalshiLine}`;
    }
    const corrBadge = b.correlated
      ? `<span title="Correlated — same game/type/side already open. Logged for record; excluded from win rate &amp; Kelly stats." style="font-size:9px;font-weight:700;color:#e3a53a;background:rgba(227,165,58,0.12);border:1px solid rgba(227,165,58,0.3);border-radius:3px;padding:1px 4px;margin-left:5px;vertical-align:middle;">CORR</span>`
      : '';
    const corruptBadge = b.clv_source === 'corrupted_utc'
      ? `<span title="Excluded from all stats — Pinnacle reference was from the wrong game (UTC/ET date collision, now fixed)." style="font-size:9px;font-weight:700;color:#8b949e;background:rgba(139,148,158,0.12);border:1px solid rgba(139,148,158,0.3);border-radius:3px;padding:1px 4px;margin-left:5px;vertical-align:middle;">BAD REF</span>`
      : '';
    const isShadow   = b.shadow === true;
    const shadowBadge = isShadow
      ? `<span title="Shadow market — tracked for CLV data only. $0 stake, excluded from portfolio balance and summary stats." style="font-size:9px;font-weight:700;color:#58a6ff;background:rgba(88,166,255,0.10);border:1px solid rgba(88,166,255,0.3);border-radius:3px;padding:1px 4px;margin-left:5px;vertical-align:middle;">SHADOW</span>`
      : '';
    return `<tr style="${b.correlated || b.clv_source === 'corrupted_utc' ? 'opacity:0.55;' : isShadow ? 'opacity:0.75;border-left:2px solid #58a6ff22;' : ''}">
      <td>${ts}</td>
      <td>${gameTimeCell}</td>
      <td>${b.matchup}${corrBadge}${corruptBadge}${shadowBadge}</td>
      <td class="prop-col">${b.title}</td>
      <td class="side-${b.side.toLowerCase()}">${b.side}</td>
      <td class="num">${edgeCell}</td>
      <td class="num">${lineMoveCell}</td>
      <td class="num ${rClass}">${rLabel}</td>
      <td class="num">${kBet}</td>
      <td class="num">${kPnl}</td>
    </tr>`;
  };
  let rows = visiblePerf.map(renderPerfRow).join('');

  let perfTableHtml = `<table>
    <thead><tr>
      <th>Flagged</th><th>Game Time</th><th>Matchup</th><th>Prop</th><th>Side</th>
      <th class="num" title="Adjusted EV after 25% haircut (raw shown in tooltip)">Adj. EV</th>
      <th class="num" title="Kalshi entry → Pinnacle closing price. Green = Pinnacle moved in your favor. Sub-line = Pinnacle fair value at entry.">Line Move</th>
      <th class="num">Result</th>
      <th class="num" title="0.25 Kelly stake as % of bankroll (hover for $ amount). ½ = CLV penalty active.">Kelly Bet %</th>
      <th class="num" title="P&amp;L as % of bankroll at Kelly stake (hover for $ amount)">Kelly P&amp;L %</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
  if (!showAllPerf && allPerfBets.length > PERF_PREVIEW) {
    perfTableHtml += `<div style="text-align:center;padding:10px;">
      <button onclick="window._perfShowAll=true;fetchPerformance();" style="background:var(--bg2);color:var(--accent);border:1px solid var(--border);border-radius:6px;padding:6px 20px;cursor:pointer;font-size:12px;font-weight:600;">
        See all ${allPerfBets.length} plays
      </button>
    </div>`;
  } else if (showAllPerf && allPerfBets.length > PERF_PREVIEW) {
    perfTableHtml += `<div style="text-align:center;padding:10px;">
      <button onclick="window._perfShowAll=false;fetchPerformance();" style="background:var(--bg2);color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:6px 20px;cursor:pointer;font-size:12px;">
        Show recent only
      </button>
    </div>`;
  }

  // Corrupted bets section — always shown at bottom, excluded from all stats
  if (corruptBets.length) {
    const corruptRows = corruptBets.map(renderPerfRow).join('');
    perfTableHtml += `
      <details style="margin-top:14px;border-top:1px solid var(--border);padding-top:10px;">
        <summary style="cursor:pointer;font-size:11px;color:#8b949e;user-select:none;list-style:none;display:flex;align-items:center;gap:6px;">
          <span style="font-size:10px;">▶</span>
          ⚠ ${corruptBets.length} excluded bets (wrong Pinnacle reference — UTC/ET date collision, fixed May 2026) — click to expand
        </summary>
        <table style="opacity:0.55;margin-top:8px;">
          <thead><tr>
            <th>Flagged</th><th>Game Time</th><th>Matchup</th><th>Prop</th><th>Side</th>
            <th class="num">Adj. EV</th><th class="num">Line Move</th>
            <th class="num">Result</th><th class="num">Kelly Bet %</th><th class="num">Kelly P&L %</th>
          </tr></thead>
          <tbody>${corruptRows}</tbody>
        </table>
      </details>`;
  }

  perfBodyHtml += perfTableHtml;
  _setHTML('perf-body', perfBodyHtml);

}


// ── My Bets ──────────────────────────────────────────────────────────────────
function trackBet(ticker, title, matchup, side, mkt_type) {
  document.getElementById('mb-ticker').value = ticker;
  document.getElementById('mb-side').value = side;
  document.getElementById('mb-ticker').dataset.title   = title;
  document.getElementById('mb-ticker').dataset.matchup = matchup;
  document.getElementById('mb-ticker').dataset.mkttype = mkt_type;
  document.getElementById('mb-price').focus();
  document.getElementById('mybets-body').classList.remove('collapsed');
  document.getElementById('mybets-body-toggle').textContent = '▾';
  document.getElementById('mybets-card').scrollIntoView({behavior:'smooth', block:'nearest'});
}

async function addMyBet() {
  const ticker    = document.getElementById('mb-ticker').value.trim();
  const side      = document.getElementById('mb-side').value;
  const priceCent = parseFloat(document.getElementById('mb-price').value);
  const amount    = parseFloat(document.getElementById('mb-amount').value);
  if (!ticker || !priceCent || !amount) { alert('Fill in all fields'); return; }
  const title   = document.getElementById('mb-ticker').dataset.title   || ticker;
  const matchup = document.getElementById('mb-ticker').dataset.matchup || '';
  const mkt_type= document.getElementById('mb-ticker').dataset.mkttype || '';
  try {
    await fetch('/api/mybets', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ ticker, side, title, matchup, mkt_type,
                             entry_price: priceCent / 100, amount_spent: amount })
    });
    document.getElementById('mb-price').value = '';
    document.getElementById('mb-amount').value = '';
    fetchMyBets();
  } catch(e) { console.error(e); }
}

async function deleteMyBet(id) {
  await fetch('/api/mybets/' + encodeURIComponent(id), { method: 'DELETE' });
  fetchMyBets();
}

// ── Paper Portfolio ───────────────────────────────────────────────────────────
async function fetchPaper() {
  try {
    const [paperResp, perfResp] = await Promise.all([
      fetch('/api/paper'),
      fetch('/api/performance?since=2026-06-08')
    ]);
    const d    = await paperResp.json();
    const perf = await perfResp.json();

    const settled  = d.won + d.lost;
    const pnlColor = d.total_pnl >= 0 ? 'var(--green)' : 'var(--red)';
    const pnlSign  = d.total_pnl >= 0 ? '+' : '';

    // Flat units from performance API (stake-agnostic pick quality)
    const flatUnits   = perf.total_units;
    const avgUnits    = perf.avg_units;
    const unitsColor  = flatUnits == null ? 'var(--muted)' : flatUnits >= 0 ? 'var(--green)' : 'var(--red)';
    const unitsTxt    = flatUnits == null ? '—'
      : `${flatUnits >= 0 ? '+' : ''}${flatUnits.toFixed(2)}u`;
    const unitsSubTxt = avgUnits == null ? '' : `${avgUnits >= 0 ? '+' : ''}${avgUnits.toFixed(3)}/bet`;

    // Win rate vs implied (model vs market)
    const winRate      = perf.win_rate;
    const impliedAvg   = perf.avg_kalshi_implied;  // market-implied win %
    const vsMarket     = (winRate != null && impliedAvg != null)
      ? (winRate - impliedAvg).toFixed(1) : null;
    const vsMarketTxt  = vsMarket == null ? '—'
      : `${vsMarket >= 0 ? '+' : ''}${vsMarket}%`;
    const vsMarketColor = vsMarket == null ? 'var(--muted)' : vsMarket >= 0 ? 'var(--green)' : 'var(--red)';

    // Avg CLV — PIN source only for honest number
    const clvSrc    = perf.clv_by_source || {};
    const pinData   = clvSrc['pin'];
    const clvTxt    = pinData ? `${pinData.avg_clv >= 0 ? '+' : ''}${pinData.avg_clv}pp` : '—';
    const clvColor  = pinData ? (pinData.avg_clv >= 0 ? 'var(--green)' : 'var(--red)') : 'var(--muted)';
    const clvLabel  = pinData ? `Avg CLV — PIN (${pinData.count} bets)` : 'Avg CLV — PIN';

    // Kelly P&L as secondary context
    const kellyPct  = perf.total_kelly_pct;
    const kellyColor = kellyPct == null ? 'var(--muted)' : kellyPct >= 0 ? 'var(--green)' : 'var(--red)';
    const kellyTxt  = kellyPct == null ? '—' : `${kellyPct >= 0 ? '+' : ''}${kellyPct.toFixed(2)}%`;

    // ── Primary KPI bar ────────────────────────────────────────────────────
    let html = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;background:var(--border);border-bottom:1px solid var(--border);">
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:24px;font-weight:800;color:${unitsColor};letter-spacing:-0.5px;">${unitsTxt}</div>
        <div style="font-size:11px;color:${unitsColor};opacity:0.7;margin-top:1px;">${unitsSubTxt}</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;" title="$1 flat stake on every bet — removes all Kelly sizing noise. Best single measure of pick quality.">Flat Units (${settled} settled)</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:24px;font-weight:800;color:${vsMarketColor};">${vsMarketTxt}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:1px;">${winRate != null ? winRate + '%' : '—'} actual vs ${impliedAvg != null ? impliedAvg.toFixed(1) + '%' : '—'} implied</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;" title="Actual win rate minus Kalshi-implied win probability. Positive = beating the market.">Win Rate vs Implied</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:24px;font-weight:800;color:${clvColor};">${clvTxt}</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;" title="Closing Line Value using Pinnacle's closing price. Positive = we bought below fair value. Most reliable edge signal.">${clvLabel}</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:22px;font-weight:700;color:var(--green);">${d.won}W <span style="color:var(--red);">${d.lost}L</span></div>
        <div style="font-size:11px;color:var(--muted);margin-top:1px;">${d.open} open · ${winRate != null ? winRate + '%' : '—'} win rate</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:2px;">Record</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:22px;font-weight:700;color:${kellyColor};">${kellyTxt}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:1px;">${pnlSign}$${d.total_pnl.toFixed(2)} on $${d.start_balance.toFixed(0)}</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;" title="Kelly-sized P&L as % of bankroll. Secondary metric — reflects staking decisions, not model quality.">Kelly P&amp;L (% bank)</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:22px;font-weight:700;color:${pnlColor};letter-spacing:-0.5px;">$${d.balance.toFixed(2)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:1px;">$${d.open_exposure.toFixed(2)} at risk</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;">Bankroll</div>
      </div>
    </div>
    <div style="padding:6px 12px;border-bottom:1px solid var(--border);background:#0d1117;">
      <span style="font-size:11px;color:var(--muted);">📊 V2.0 reset Jun 8 2026 ·<strong style="color:var(--text);">props ≥2.5% · games ≥3%</strong> · 0.5 Kelly (time-throttled: ×0.25 / ×0.50 / ×1.0) · 3% max stake · compounding from $${d.start_balance.toFixed(0)} since ${d.start_date} · CLV captured every 2 min until game start</span>
    </div>`;

    // ── Bet table ──────────────────────────────────────────────────────────
    if (d.bets && d.bets.length) {
      const PAPER_PREVIEW = 15;
      const allBets     = d.bets.filter(b => b.clv_source !== 'corrupted_utc');
      const corruptBets = d.bets.filter(b => b.clv_source === 'corrupted_utc');
      const showAll = window._paperShowAll || false;
      const visibleBets = showAll ? allBets : allBets.slice(0, PAPER_PREVIEW);

      const renderPaperRow = (b) => {
        const stake = b.paper_stake != null ? '$' + b.paper_stake.toFixed(2) : '—';
        let pnlCell = '—';
        if (b.paper_pnl != null) {
          const c = b.paper_pnl >= 0 ? 'var(--green)' : 'var(--red)';
          const s = b.paper_pnl >= 0 ? '+' : '';
          pnlCell = `<span style="color:${c};font-weight:600;">${s}$${b.paper_pnl.toFixed(2)}</span>`;
        }
        const statusColor = b.status === 'won' ? 'var(--green)' : b.status === 'lost' ? 'var(--red)' : 'var(--muted)';
        const statusLabel = b.status === 'won' ? '✓ WON' : b.status === 'lost' ? '✗ LOST' : '…';
        const flagDate = b.flagged_at ? fmtDate(b.flagged_at) : '';
        // Edge badge: distinguish 3-5% "data" edges from 5%+ "signal" edges
        const edgePct = b.edge_pct != null ? b.edge_pct : 0;
        const edgeTag = edgePct >= 5
          ? `<span style="color:${edgeColor(edgePct)};font-weight:700;">+${edgePct}%</span>`
          : `<span style="color:#ff8c42;font-weight:600;">+${edgePct}%</span><span style="font-size:9px;color:var(--muted);margin-left:3px;">data</span>`;
        // Value @ Entry: Pinnacle prob at flag minus Kalshi entry price — always positive when edge was real
        let valCell = '<span style="color:var(--muted);">—</span>';
        if (b.pin_prob_at_flag != null && b.kalshi_price != null) {
          const val = b.pin_prob_at_flag - (b.kalshi_price * 100);
          valCell = `<span style="color:#3fb950;font-weight:700;" title="Value locked at entry: Pinnacle priced this side at ${b.pin_prob_at_flag.toFixed(1)}%, you paid ${(b.kalshi_price*100).toFixed(1)}¢ on Kalshi → +${val.toFixed(1)}pp value">+${val.toFixed(1)}pp</span>`;
        }

        return `<tr style="${b.clv_source === 'corrupted_utc' ? 'opacity:0.55;' : ''}">
          <td style="color:var(--muted);font-size:11px;">${flagDate}</td>
          <td class="matchup-inline">${matchupHtml(b.matchup)}</td>
          <td style="font-size:12px;max-width:200px;">${b.title}</td>
          <td class="side-${(b.side||'').toLowerCase()}">${b.side}</td>
          <td class="num">${edgeTag}</td>
          <td class="num">${valCell}</td>
          <td class="num">${stake}</td>
          <td class="num">${pnlCell}</td>
          <td style="color:${statusColor};font-weight:700;text-align:center;">${statusLabel}</td>
        </tr>`;
      };

      const tableHead = `<table style="font-size:12px;">
        <thead><tr>
          <th>Date</th><th>Matchup</th><th>Bet</th><th>Side</th>
          <th class="num">Adj. EV</th>
          <th class="num" title="Value locked at entry = Pinnacle probability at flag − Kalshi price. Always positive when the edge was real. This is the value you captured when you placed the bet.">Value @ Entry</th>
          <th class="num">Kelly Stake</th><th class="num">P&amp;L</th><th>Result</th>
        </tr></thead>`;

      html += tableHead + `<tbody>${visibleBets.map(renderPaperRow).join('')}</tbody></table>`;

      if (!showAll && allBets.length > PAPER_PREVIEW) {
        html += `<div style="text-align:center;padding:10px;">
          <button onclick="window._paperShowAll=true;fetchPaper();" style="background:var(--bg2);color:var(--accent);border:1px solid var(--border);border-radius:6px;padding:6px 20px;cursor:pointer;font-size:12px;font-weight:600;">
            See all ${allBets.length} plays
          </button>
        </div>`;
      } else if (showAll && allBets.length > PAPER_PREVIEW) {
        html += `<div style="text-align:center;padding:10px;">
          <button onclick="window._paperShowAll=false;fetchPaper();" style="background:var(--bg2);color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:6px 20px;cursor:pointer;font-size:12px;">
            Show recent only
          </button>
        </div>`;
      }

      if (corruptBets.length) {
        html += `<details style="margin-top:14px;border-top:1px solid var(--border);padding-top:10px;">
          <summary style="cursor:pointer;font-size:11px;color:#8b949e;user-select:none;list-style:none;display:flex;align-items:center;gap:6px;">
            <span style="font-size:10px;">▶</span>
            ⚠ ${corruptBets.length} excluded bets (wrong Pinnacle reference — UTC/ET date collision, fixed May 2026) — click to expand
          </summary>` + tableHead + `<tbody>${corruptBets.map(renderPaperRow).join('')}</tbody></table></details>`;
      }
    } else {
      html += '<div class="empty" style="padding:16px;">No paper trades yet. Every edge ≥3% flagged from ' + d.start_date + ' is automatically sized and tracked here.</div>';
    }
    _setHTML('paper-body', html);
  } catch(e) { console.error('paper fetch failed', e); }
}

async function fetchMyBets() {
  try {
    const r = await fetch('/api/mybets');
    const d = await r.json();
    renderMyBets(d);
  } catch(e) { console.error(e); }
}

function renderMyBets(d) {
  // Stats bar
  const np = d.net_pnl;
  const npClass = np > 0 ? 'mb-pnl-pos' : np < 0 ? 'mb-pnl-neg' : '';
  const npSign  = np >= 0 ? '+' : '';
  _setHTML('mybets-stats', `
    <div class="stat-pill"><div class="label">Total In</div><div class="value">$${d.total_in.toFixed(2)}</div></div>
    <div class="stat-pill"><div class="label">Realized P&amp;L</div><div class="value ${d.realized_pnl>=0?'mb-pnl-pos':'mb-pnl-neg'}">${d.realized_pnl>=0?'+':''}$${d.realized_pnl.toFixed(2)}</div></div>
    <div class="stat-pill"><div class="label">Unrealized P&amp;L</div><div class="value ${d.unrealized_pnl>=0?'mb-pnl-pos':'mb-pnl-neg'}">${d.unrealized_pnl>=0?'+':''}$${d.unrealized_pnl.toFixed(2)}</div></div>
    <div class="stat-pill"><div class="label">Net P&amp;L</div><div class="value ${npClass}">${npSign}$${np.toFixed(2)}</div></div>`);

  if (!d.bets.length) {
    _setHTML('mybets-table', '<div class="empty">No bets tracked yet — add one above.</div>');
    return;
  }
  let rows = '';
  for (const b of d.bets) {
    const gameStartMs2 = b.game_time ? new Date(b.game_time).getTime() : null;
    const isLive2 = b.status === 'open' && gameStartMs2 != null && Date.now() >= gameStartMs2;
    const statusCls = b.status === 'won' ? 'mb-won' : b.status === 'lost' ? 'mb-lost' : 'mb-open';
    const statusTxt = b.status === 'won' ? '✅ WON' : b.status === 'lost' ? '❌ LOST'
      : isLive2 ? '<span style="color:#ff4444;font-weight:600;animation:pulse 1.5s infinite;">● LIVE</span>'
      : '⏳ Open';
    const pnlTxt = b.pnl != null
      ? `<span class="${b.pnl>=0?'mb-pnl-pos':'mb-pnl-neg'}">${b.pnl>=0?'+':''}$${b.pnl.toFixed(2)}</span>`
      : b.unrealized_pnl != null
        ? `<span class="${b.unrealized_pnl>=0?'mb-pnl-pos':'mb-pnl-neg'}" title="Unrealized">${b.unrealized_pnl>=0?'+':''}$${b.unrealized_pnl.toFixed(2)}*</span>`
        : '—';
    const curTxt = b.current_price != null ? `${b.current_price}¢` : '—';
    rows += `<tr>
      <td class="prop-col">${b.title || b.ticker}</td>
      <td class="side-${b.side.toLowerCase()}">${b.side}</td>
      <td class="num">${Math.round(b.entry_price*100)}¢</td>
      <td class="num">${b.contracts.toFixed(1)}</td>
      <td class="num">$${b.amount_spent.toFixed(2)}</td>
      <td class="num">${curTxt}</td>
      <td class="${statusCls}">${statusTxt}</td>
      <td class="num">${pnlTxt}</td>
      <td><span class="mb-del" data-id="${hesc(b.id)}" onclick="deleteMyBet(this.dataset.id)">✕</span></td>
    </tr>`;
  }
  _setHTML('mybets-table', `<table>
    <thead><tr>
      <th>Bet</th><th>Side</th><th class="num">Paid</th><th class="num">Contracts</th>
      <th class="num">Invested</th><th class="num">Current</th><th>Status</th>
      <th class="num">P&amp;L</th><th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`);
}

// ── Scanner History (Past 24 Hours) ──────────────────────────────────────────
async function fetchScannerHistory() {
  try {
    const r = await fetch('/api/scanner_history');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    renderScannerHistory(data);
  } catch(e) { console.error('scanner_history fetch failed', e); }
}

function renderScannerHistory(bets) {
  const el = document.getElementById('scanner-history-table');
  if (!el) return;
  if (!bets || !bets.length) {
    el.innerHTML = '<div class="empty" style="color:#4d5461;">No edges flagged in the past 24 hours.</div>';
    return;
  }
  const rows = bets.map(b => {
    const flagTime = b.flagged_at ? fmtDate(b.flagged_at) : '—';
    const sideClass = b.side === 'YES' ? 'side-yes' : 'side-no';
    const edgePct   = b.edge_pct != null ? `+${pct(b.edge_pct)}` : '—';

    // Pinnacle fair prob at flag → American odds
    const pinAmer    = b.pin_prob_at_flag != null ? probToAmerican(b.pin_prob_at_flag / 100) : '—';
    // Kalshi price at flag (always stored as the side's decimal price)
    const kalshiAmer = b.kalshi_price != null ? kalshiToAmerican(b.kalshi_price) : '—';
    const oddsTxt = `<span style="font-size:10px;color:#4d5461;display:block;margin-top:2px;">
      Kalshi <span style="color:#6e7681;">${kalshiAmer}</span>
      &nbsp;·&nbsp; Fair <span style="color:#6e7681;">${pinAmer}</span>
    </span>`;

    // Status badge — never actionable
    const hs = b.hist_status || 'EXPIRED';
    const hsColor = hs === 'WON' ? '#3fb950' : hs === 'LOST' ? '#f85149' : '#4d5461';
    const hsBadge = `<span style="font-weight:700;font-size:11px;color:${hsColor};">${hs}</span>`;

    return `<tr style="opacity:0.75;">
      <td style="font-size:11px;color:#4d5461;white-space:nowrap;">${flagTime}</td>
      <td style="font-size:11px;color:#6e7681;">${hesc(b.matchup || '—')}</td>
      <td style="font-size:11px;color:#6e7681;">${hesc(b.title || b.ticker || '—')}</td>
      <td class="${sideClass}" style="opacity:0.7;">${b.side}</td>
      <td class="num" style="color:#6e7681;font-weight:700;">${edgePct}${oddsTxt}</td>
      <td class="num">${hsBadge}</td>
    </tr>`;
  }).join('');

  el.innerHTML = `<table>
    <thead><tr style="opacity:0.6;">
      <th style="color:#4d5461;">Flag Time</th>
      <th style="color:#4d5461;">Matchup</th>
      <th style="color:#4d5461;">Bet Side</th>
      <th style="color:#4d5461;">Side</th>
      <th class="num" style="color:#4d5461;">Edge @ Flag &amp; Odds at Entry</th>
      <th class="num" style="color:#4d5461;">Status</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

// Resume refresh when tab becomes visible again
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { fetchData(); fetchMyBets(); }
});

// Initial load — default performance filter to clean-data start date (2026-04-07)
// This excludes pre-fix ghost edges and sub-15¢ bets from stats by default.
// Clear the date field and click Refresh to see all historical bets.
(function() {
  const inp = document.getElementById('perf-since');
  if (inp && !inp.value) inp.value = '2026-06-08';  // V2.0 baseline — pre-throttle data excluded
})();
fetchData();
fetchHistory();
fetchPerformance();
fetchPaper();
fetchMyBets();
fetchScannerHistory();
setInterval(updateCountdown, 1000);
setInterval(fetchPaper, 60 * 1000);   // refresh paper portfolio every 60s
setInterval(fetchMyBets, 60 * 1000);   // refresh my bets every 60s (mark-to-market)
setInterval(fetchScannerHistory, 5 * 60 * 1000);  // refresh history every 5 min
</script>

<!-- Pre-Bet Validator Modal -->
<div id="validate-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:1000;align-items:center;justify-content:center;">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;width:420px;max-width:95vw;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border);">
      <span style="font-size:14px;font-weight:700;color:var(--text);">Pre-Bet Validation</span>
      <button onclick="closeValidateModal()" style="background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;line-height:1;">✕</button>
    </div>
    <div id="validate-body" style="min-height:80px;"></div>
    <div style="padding:12px 20px;border-top:1px solid var(--border);text-align:right;">
      <button onclick="closeValidateModal()" style="background:var(--bg2);color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:6px 16px;cursor:pointer;font-size:12px;">Close</button>
    </div>
  </div>
</div>
</body>
</html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default access logs

    def _send(self, code: int, content_type: str, body: bytes, extra_headers: dict = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/health":
            # Lightweight Railway healthcheck — no locks, no state, instant 200
            self._send(200, "application/json", b'{"ok":true}')
            return

        if path == "/":
            # Force-bust browser cache: redirect bare / to /?v=<timestamp>
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            if "v" not in qs:
                import time as _t
                self.send_response(302)
                self.send_header("Location", f"/?v={int(_t.time())}")
                self.end_headers()
                return
            self._send(200, "text/html; charset=utf-8", HTML.encode(),
                       {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                        "Pragma": "no-cache",
                        "Expires": "0"})

        elif path == "/static/chart.umd.min.js":
            try:
                with open(os.path.join(BASE_DIR, "chart.umd.min.js"), "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")  # browser caches for 1 day
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self._send(404, "text/plain", b"Not found")

        elif path == "/api/scan":
            with _lock:
                state_copy = dict(_state)
            # Serve cached edges if the current scan produced none — prevents
            # status indicators from reading zero during off-peak/sleep windows.
            if not state_copy.get("edges") and state_copy.get("edges_cache"):
                state_copy["edges"]           = state_copy["edges_cache"]
                state_copy["edges_from_cache"] = True
            else:
                state_copy["edges_from_cache"] = False
            # Augment with watchdog / odds health info for observability
            with _odds_cache_lock:
                odds_age_sec         = int(time.time() - _last_odds_refresh) if _last_odds_refresh else None
                cache_success_age    = int(time.time() - _last_odds_cache_success) if _last_odds_cache_success else None
                game_count           = _odds_game_count
            state_copy["watchdog_last_tick"]     = int(_watchdog_last_tick) if _watchdog_last_tick else None
            state_copy["odds_age_sec"]           = odds_age_sec
            state_copy["odds_cache_success_age"] = cache_success_age   # age of last SUCCESSFUL index population
            state_copy["odds_game_count"]        = game_count           # matchups in cached Pinnacle index
            state_copy["kalshi_auth_failed"]     = _kalshi_auth_failed
            payload = json.dumps(state_copy).encode()
            self._send(200, "application/json", payload)

        elif path == "/api/history":
            payload = json.dumps(_history[-200:]).encode()
            self._send(200, "application/json", payload)

        elif path == "/api/roi":
            with _bets_lock:
                settled = [
                    b for b in _bets
                    if b["status"] in ("won", "lost")
                    and b.get("paper_pnl") is not None
                    and not b.get("correlated", False)
                    and b.get("clv_source") != "corrupted_utc"
                    and b.get("flagged_at", "") >= PAPER_START_DATE
                ]
            settled.sort(key=lambda b: b.get("resolved_at") or b.get("flagged_at", ""))
            balance = PAPER_START_BALANCE
            points = [{"date": PAPER_START_DATE, "roi": 0.0, "balance": balance,
                       "matchup": None, "result": None, "pnl": None}]
            for b in settled:
                balance = round(balance + b["paper_pnl"], 2)
                roi = round((balance - PAPER_START_BALANCE) / PAPER_START_BALANCE * 100, 2)
                points.append({
                    "date":    (b.get("resolved_at") or b.get("flagged_at", ""))[:10],
                    "roi":     roi,
                    "balance": balance,
                    "matchup": b.get("matchup", ""),
                    "title":   b.get("title", ""),
                    "side":    b.get("side", ""),
                    "result":  b["status"],
                    "pnl":     round(b["paper_pnl"], 2),
                })
            self._send(200, "application/json", json.dumps(points).encode())

        elif path == "/api/today_edges":
            # Return all open bets (game not yet started), sorted newest-first.
            # Renamed from "today_edges" — shows active positions regardless of when flagged.
            now_iso = datetime.now(timezone.utc).isoformat()
            with _bets_lock:
                open_bets = [
                    b for b in _bets
                    if b.get("status") == "open"
                    and b.get("paper_stake") is not None
                ]
            open_bets.sort(key=lambda b: b.get("flagged_at", ""), reverse=True)
            print(f"  /api/today_edges: found {len(open_bets)} open positions")
            try:
                payload = json.dumps(open_bets).encode()
            except Exception as _je:
                print(f"  /api/today_edges JSON error: {_je}")
                payload = b"[]"
            self._send(200, "application/json", payload)

        elif path == "/api/scanner_history":
            # Return all bets flagged in the past 24 hours, newest first.
            # Includes bets regardless of current EV or TTL status — audit log only.
            cutoff_iso = (datetime.now(timezone.utc).replace(microsecond=0)
                          - __import__('datetime').timedelta(hours=24)).isoformat()
            with _bets_lock:
                hist_bets = [b for b in _bets if b.get("flagged_at", "") >= cutoff_iso]
            hist_bets.sort(key=lambda b: b.get("flagged_at", ""), reverse=True)
            now_ts = datetime.now(timezone.utc).timestamp()
            result = []
            for b in hist_bets:
                status = b.get("status", "open")
                # Compute hist_status: won/lost take precedence, else EXPIRED
                if status == "won":
                    hist_status = "WON"
                elif status == "lost":
                    hist_status = "LOST"
                else:
                    # Check if game has started
                    game_time = b.get("game_time")
                    game_started = False
                    if game_time:
                        try:
                            game_started = datetime.fromisoformat(game_time).timestamp() < now_ts
                        except Exception:
                            pass
                    hist_status = "HISTORICAL" if game_started else "EXPIRED"
                result.append({
                    "id":               b.get("id"),
                    "matchup":          b.get("matchup", ""),
                    "title":            b.get("title", b.get("ticker", "")),
                    "ticker":           b.get("ticker", ""),
                    "side":             b.get("side", ""),
                    "edge_pct":         b.get("edge_pct"),
                    "kalshi_price":     b.get("kalshi_price"),
                    "pin_prob_at_flag": b.get("pin_prob_at_flag"),
                    "flagged_at":       b.get("flagged_at"),
                    "game_time":        b.get("game_time"),
                    "hist_status":      hist_status,
                })
            self._send(200, "application/json", json.dumps(result).encode())

        elif path == "/api/performance":
            # optional ?since=YYYY-MM-DD  (ISO date, inclusive lower bound)
            # Defaults to PAPER_START_DATE so pre-V2.0 bets are excluded from
            # all CLV / line-move aggregations unless the caller overrides.
            from urllib.parse import urlparse, parse_qs
            qs    = parse_qs(urlparse(self.path).query)
            since = qs.get("since", [PAPER_START_DATE])[0]
            payload = json.dumps(_get_performance(since=since)).encode()
            self._send(200, "application/json", payload)

        elif path == "/api/paper":
            with _bets_lock:
                paper_bets = [b for b in _bets
                              if b.get("flagged_at", "") >= PAPER_START_DATE
                              and b.get("paper_stake") is not None]
            # shadow bets: still shown in the bet table but excluded from
            # won/lost/win_rate counts and balance (stake is $0 anyway)
            live_paper  = [b for b in paper_bets if not _is_shadow(b.get("ticker", ""))]
            balance     = _compute_paper_balance()
            open_exp    = round(sum(b["paper_stake"] for b in live_paper if b["status"] == "open"), 2)
            settled     = [b for b in live_paper if b["status"] in ("won", "lost")
                           and b.get("clv_source") != "corrupted_utc"]
            won_bets    = [b for b in settled if b["status"] == "won"]
            total_pnl   = round(balance - PAPER_START_BALANCE, 2)
            roi_pct     = round(total_pnl / PAPER_START_BALANCE * 100, 2)
            win_rate    = round(len(won_bets) / len(settled) * 100, 1) if settled else None
            avg_stake   = round(sum(b["paper_stake"] for b in live_paper) / len(live_paper), 2) if live_paper else None
            # Clean bets newest-first, corrupted bets appended at end
            _clean_p  = sorted([b for b in paper_bets if b.get("clv_source") != "corrupted_utc"],
                                key=lambda b: b.get("flagged_at",""), reverse=True)
            _corrupt_p = sorted([b for b in paper_bets if b.get("clv_source") == "corrupted_utc"],
                                 key=lambda b: b.get("flagged_at",""), reverse=True)
            recent = _clean_p + _corrupt_p

            result = {
                "balance":        balance,
                "start_balance":  PAPER_START_BALANCE,
                "start_date":     PAPER_START_DATE,
                "total_pnl":      total_pnl,
                "roi_pct":        roi_pct,
                "open_exposure":  open_exp,
                "available":      round(balance - open_exp, 2),
                "total_bets":     len(settled) + len([b for b in live_paper if b["status"] == "open"]),
                "won":            len(won_bets),
                "lost":           len(settled) - len(won_bets),
                "open":           len([b for b in live_paper if b["status"] == "open"]),
                "win_rate":       win_rate,
                "avg_stake":      avg_stake,
                "bets":           recent,
            }
            self._send(200, "application/json", json.dumps(result).encode())

        elif path.startswith("/api/validate/"):
            # /api/validate/{ticker}?side=YES
            try:
                from urllib.parse import unquote
                raw_ticker = path.split("/api/validate/")[1].split("?")[0]
                ticker = unquote(raw_ticker)   # decode %40 → @, etc.
                side   = (self.path.split("side=")[1].split("&")[0] if "side=" in self.path else "YES")

                # ── Validate cache: 1 credit per call, so cache for 5 min ──
                cache_key = (ticker, side)
                cached_result, cache_exp = _validate_cache.get(cache_key, (None, 0))
                if cached_result is not None and time.time() < cache_exp:
                    self._send(200, "application/json", json.dumps(cached_result).encode())
                    return

                # Search live edges first, then bet history
                with _lock:
                    live_edges = list(_state.get("edges", []))
                edge = next((e for e in live_edges
                             if e.get("ticker") == ticker and e.get("side") == side), None)
                if edge is None:
                    with _bets_lock:
                        edge = next((b for b in _bets
                                     if b.get("ticker") == ticker and b.get("side") == side), None)
                if edge is None:
                    self._send(404, "application/json",
                               json.dumps({"error": f"edge not found for ticker={ticker} side={side}"}).encode())
                    return
                result = validate_bet(edge)
                _validate_cache[cache_key] = (result, time.time() + _VALIDATE_CACHE_TTL)
                self._send(200, "application/json", json.dumps(result).encode())
            except Exception as exc:
                import traceback
                print(f"  /api/validate/ error: {exc}\n{traceback.format_exc()}")
                self._send(500, "application/json",
                           json.dumps({"error": f"Server error: {exc}"}).encode())

        elif path == "/api/debug/storage":
            import os as _os
            result = {
                "DATA_DIR":           DATA_DIR,
                "BETS_FILE":          BETS_FILE,
                "data_dir_exists":    _os.path.isdir(DATA_DIR),
                "bets_file_exists":   _os.path.exists(BETS_FILE),
                "railway_env":        _os.environ.get("RAILWAY_ENVIRONMENT", "not set"),
                "slash_data_exists":  _os.path.isdir("/data"),
                "bets_in_memory":     len(_bets),
                "bets_file_size_kb":  round(_os.path.getsize(BETS_FILE) / 1024, 1) if _os.path.exists(BETS_FILE) else 0,
            }
            self._send(200, "application/json", json.dumps(result).encode())

        elif path == "/api/test-discord":
            ok = send_test_discord()
            result = {"ok": ok, "webhook_set": bool(_DISCORD_WEBHOOK)}
            self._send(200, "application/json", json.dumps(result).encode())

        elif path == "/api/discord-status":
            with _discord_log_lock:
                log_copy = list(_discord_log)
            result = {
                "configured": bool(_DISCORD_WEBHOOK),
                "min_edge":   _ALERT_MIN,
                "alerted":    len(_alerted_keys),
                "log":        log_copy,
            }
            self._send(200, "application/json", json.dumps(result).encode())

        elif path == "/api/mybets":
            payload = json.dumps(_get_my_bets_state()).encode()
            self._send(200, "application/json", payload)

        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/mybets":
            ticker      = body.get("ticker", "")
            side        = body.get("side", "YES")
            entry_price = float(body.get("entry_price", 0.5))
            amount_spent= float(body.get("amount_spent", 10))
            contracts   = round(amount_spent / entry_price, 1) if entry_price > 0 else 0
            bet_id      = f"{ticker}|{side}|{int(time.time())}"
            new_bet = {
                "id":           bet_id,
                "ticker":       ticker,
                "title":        body.get("title", ticker),
                "matchup":      body.get("matchup", ""),
                "side":         side,
                "mkt_type":     body.get("mkt_type", ""),
                "entry_price":  entry_price,
                "amount_spent": amount_spent,
                "contracts":    contracts,
                "added_at":     datetime.now(timezone.utc).isoformat(),
                "status":       "open",
                "resolved_at":  None,
                "pnl":          None,
            }
            with _my_bets_lock:
                _my_bets.append(new_bet)
                _save_my_bets(_my_bets)
            self._send(200, "application/json", json.dumps({"ok": True}).encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/mybets/"):
            import urllib.parse
            bet_id = urllib.parse.unquote(path[len("/api/mybets/"):])
            with _my_bets_lock:
                before = len(_my_bets)
                _my_bets[:] = [b for b in _my_bets if b["id"] != bet_id]
                if len(_my_bets) < before:
                    _save_my_bets(_my_bets)
            self._send(200, "application/json", json.dumps({"ok": True}).encode())
        elif path.startswith("/api/admin/remove_bet/"):
            import urllib.parse
            bet_id = urllib.parse.unquote(path[len("/api/admin/remove_bet/"):])
            with _bets_lock:
                before = len(_bets)
                _bets[:] = [b for b in _bets if b["id"] != bet_id]
                if len(_bets) < before:
                    _save_bets(_bets)
            self._send(200, "application/json", json.dumps({"ok": True, "removed": before - len(_bets)}).encode())
        else:
            self._send(404, "text/plain", b"Not found")


# ── World Cup market watcher ──────────────────────────────────────────────────
_WC_CHECK_INTERVAL   = 6 * 60 * 60   # check every 6 hours
_WC_ALERTED_FILE     = os.path.join(DATA_DIR, "wc_alerted_series.json")
_WC_SERIES_PREFIXES  = ["KXWC", "KXFIFA", "KXSOCCER", "KXWC26"]
_WC_KEYWORDS         = ["world cup", "fifa", "soccer", "football", "wc26", "wc2026"]

def _load_wc_alerted() -> set:
    try:
        with open(_WC_ALERTED_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def _save_wc_alerted(alerted: set):
    try:
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with __import__('os').fdopen(fd, "w") as f:
            json.dump(list(alerted), f)
        __import__('os').replace(tmp, _WC_ALERTED_FILE)
    except Exception as exc:
        print(f"  WC watcher: could not save alerted series: {exc}")

def _background_wc_watcher_loop():
    """
    Every 6 hours, scan Kalshi for any new World Cup / soccer markets.
    Fires a Discord alert the moment they appear so we can start building
    the soccer scanner extension before anyone else is scanning them.
    """
    time.sleep(60)   # let startup settle first
    alerted = _load_wc_alerted()

    while True:
        try:
            new_tickers = []

            # Check each known World Cup series prefix on Kalshi
            for series in _WC_SERIES_PREFIXES:
                try:
                    data = kalshi_get("/markets", {
                        "series_ticker": series,
                        "status": "open",
                        "limit": 10,
                    })
                    for mkt in data.get("markets", []):
                        ticker = mkt.get("ticker", "")
                        title  = mkt.get("title", "")
                        if ticker and ticker not in alerted:
                            new_tickers.append((ticker, title, series))
                except Exception:
                    pass

            # Also do a keyword search via events endpoint
            for kw in _WC_KEYWORDS:
                try:
                    data = kalshi_get("/events", {"limit": 20})
                    for ev in data.get("events", []):
                        eticker = ev.get("event_ticker", "")
                        title   = ev.get("title", "").lower()
                        if any(k in title for k in _WC_KEYWORDS) and eticker not in alerted:
                            new_tickers.append((eticker, ev.get("title", ""), "event"))
                    break   # one keyword search covers all — events aren't filtered by kw here
                except Exception:
                    break

            if new_tickers:
                # Deduplicate
                seen = set()
                unique = []
                for t in new_tickers:
                    if t[0] not in seen:
                        seen.add(t[0])
                        unique.append(t)

                # Fire Discord alert
                sample = unique[:5]
                fields = [
                    {"name": t[0], "value": t[1] or "—", "inline": False}
                    for t in sample
                ]
                if len(unique) > 5:
                    fields.append({"name": f"+{len(unique)-5} more", "value": "Check Kalshi", "inline": False})

                embed = {
                    "color": 0x00c853,
                    "author": {"name": "Kalshi EV Scanner  •  World Cup Watcher"},
                    "title": f"⚽ World Cup markets just listed on Kalshi ({len(unique)} new)",
                    "description": "Time to build the soccer scanner. Get on it.",
                    "fields": fields,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                content = f"⚽ **World Cup markets live on Kalshi** — {len(unique)} new markets detected. Time to build."
                send_discord(embed, content)
                print(f"  WC watcher: alerted on {len(unique)} new market(s)")

                for ticker, _, _ in unique:
                    alerted.add(ticker)
                _save_wc_alerted(alerted)
            else:
                print(f"  WC watcher: no new World Cup markets on Kalshi (checked {len(_WC_SERIES_PREFIXES)} series)")

        except Exception as exc:
            print(f"  WC watcher error: {exc}")

        time.sleep(_WC_CHECK_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import webbrowser

    _bg_threads: list = []   # tracked by watchdog

    def _start_bg_threads():
        """Start (or restart) all background threads. Called on launch and by watchdog."""
        specs = [
            ("scan",        _background_loop),
            ("odds",        _background_odds_loop),
            ("resolution",  _background_resolution_loop),
            ("my-bets",     _background_my_bets_loop),
            ("clv-capture", _background_clv_capture_loop),
            ("wc-watcher",  _background_wc_watcher_loop),
        ]
        _bg_threads.clear()
        for name, target in specs:
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            _bg_threads.append((name, target, th))
        return _bg_threads

    def _watchdog_loop():
        """
        Two-mode thread monitor — runs every 60 s:

        Mode 1 — Dead thread:  is_alive() == False  → restart immediately
        Mode 2 — Hung thread:  is_alive() == True but _state["last_scan"] is
                               > _SCAN_STALE_MINUTES old during game hours.
                               A hung thread blocks on a network call and never
                               increments the scan counter, so is_alive() lies.
                               We fire a Discord alert and force-restart anyway.
        """
        global _scan_stale_alerted, _watchdog_last_tick
        while True:
            time.sleep(60)
            try:
                _watchdog_last_tick = time.time()

                # ── Mode 1: restart dead threads ─────────────────────────────
                for i, (name, target, th) in enumerate(_bg_threads):
                    if not th.is_alive():
                        print(f"  ⚠️  Thread '{name}' died — restarting...")
                        new_th = threading.Thread(target=target, name=name, daemon=True)
                        new_th.start()
                        _bg_threads[i] = (name, target, new_th)

                # ── Mode 2: detect hung scan thread (alive but not scanning) ─
                in_game_hours = 10 <= _et_hour() <= 20   # 10 AM – 8 PM ET (pre-game window only)

                with _lock:
                    last_scan_iso = _state.get("last_scan")

                stale_min = None
                if last_scan_iso:
                    try:
                        last_dt   = datetime.fromisoformat(last_scan_iso)
                        # make naive UTC comparable
                        now_utc   = datetime.now(timezone.utc)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        stale_min = (now_utc - last_dt).total_seconds() / 60
                    except Exception:
                        pass

                # ── Mode 3: cold-start failure — last_scan still None after 10 min ─
                # Happens when the odds thread fails its very first fetch on boot.
                # Scan thread keeps skipping ("cache cold") forever with no alert.
                global _cold_start_alerted
                uptime_min = (time.time() - _BOOT_TIME) / 60
                if last_scan_iso is None and in_game_hours and uptime_min > 10:
                    if not _cold_start_alerted:
                        _cold_start_alerted = True
                        print(f"  🚨 Cold-start failure — scanner has never completed a scan "
                              f"({uptime_min:.0f} min uptime). Odds cache likely failed on boot.")
                        _send_scan_stale_alert(uptime_min)
                elif last_scan_iso is not None:
                    _cold_start_alerted = False   # clear once first scan succeeds

                if stale_min is not None and in_game_hours:
                    if stale_min >= _SCAN_STALE_MINUTES:
                        if not _scan_stale_alerted:
                            _scan_stale_alerted = True
                            print(f"  🚨 Scan thread hung — {stale_min:.0f} min since last scan. "
                                  f"Force-restarting...")
                            _send_scan_stale_alert(stale_min)
                            # Force-start a fresh scan thread (old one may still be alive but
                            # effectively blocked; daemon threads can't be killed, but the new
                            # thread will take over and both produce scans — harmless duplicate)
                            for i, (name, target, th) in enumerate(_bg_threads):
                                if name == "scan":
                                    new_th = threading.Thread(
                                        target=target, name="scan-restart", daemon=True
                                    )
                                    new_th.start()
                                    _bg_threads[i] = (name, target, new_th)
                                    print(f"  Scan thread force-restarted (old thread left to drain)")
                                    break
                    else:
                        # Scan is fresh — clear stale flag
                        if _scan_stale_alerted:
                            _scan_stale_alerted = False
                            print(f"  ✓ Scan thread recovered — last scan {stale_min:.1f} min ago")

            except Exception as _wd_exc:
                # Never let the watchdog die — log and continue
                print(f"  ⚠️  Watchdog internal error (continuing): {_wd_exc}")

    _start_bg_threads()

    # Watchdog monitors all bg threads and revives any that die
    tw = threading.Thread(target=_watchdog_loop, name="watchdog", daemon=True)
    tw.start()

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True   # don't block shutdown on open connections

    # On Railway/cloud bind to 0.0.0.0 so the health-check can reach us;
    # locally keep it on 127.0.0.1 for security.
    bind_host = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    server = ThreadedHTTPServer((bind_host, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"  Kalshi EV Scanner UI running at {url}")
    print(f"  Kalshi scan : every {REFRESH_SECONDS}s  (0 credits — cached odds)")
    print(f"  Odds refresh: 4min peak / 8min overnight  (2 credits/call — spreads+totals)")
    print(f"  Props scan  : every 4h, 2 markets  (~2 credits/event)")
    print(f"  CLV capture : every {CLV_CAPTURE_SECONDS}s  (0 credits — Kalshi-only)")
    print(f"  Est. monthly: ~18,570 credits  (budget: 20,000)  |  Ctrl-C to stop\n")
    # Only open browser if running on a local desktop (not a headless VPS)
    if os.environ.get("DISPLAY") or sys.platform == "darwin":
        try:
            webbrowser.open(url)
        except Exception:
            pass   # headless — no browser available, that's fine

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
