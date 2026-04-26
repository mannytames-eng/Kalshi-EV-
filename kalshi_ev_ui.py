#!/usr/bin/env python3
"""
Kalshi EV Scanner — Web UI
Run:  python3 kalshi_ev_ui.py
Open: http://localhost:8000
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import List, Optional

from dotenv import load_dotenv

# ── Portable base directory — works on Mac and any Linux VPS ─────────────────
# On Railway a persistent volume is mounted at /data — use it for all data files
# so they survive service restarts/redeploys.  Falls back to script directory locally.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_RAILWAY_DATA = "/data"
BASE_DIR  = _RAILWAY_DATA if os.path.isdir(_RAILWAY_DATA) and os.environ.get("RAILWAY_ENVIRONMENT") else _SCRIPT_DIR
DATA_DIR  = BASE_DIR   # data files (bets, history, keys) — persistent on Railway
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))   # .env lives next to the script always

# ── Import scanner logic ──────────────────────────────────────────────────────
sys.path.insert(0, BASE_DIR)
from kalshi_ev_scanner import (
    scan_sport,
    scan_player_props,
    scan_nba_player_props,
    kalshi_get,
    fetch_game_scores,
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

def _odds_refresh_interval() -> int:
    """Return seconds until next odds refresh based on current ET hour."""
    et_hour = _et_hour()
    if 11 <= et_hour < 22:   # 11 AM – 10 PM ET: game window
        return 4 * 60
    return 20 * 60           # overnight: slow down
REFRESH_SECONDS       = 2 * 60     # re-scan Kalshi every 2 min    (0 credits)
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
PAPER_START_DATE     = "2026-04-07"  # bets before this date excluded from portfolio
PAPER_KELLY_FRACTION = 0.25     # quarter-Kelly sizing
PAPER_KELLY_CAP      = 0.05     # max 5% of current balance per bet

# ── Shared state (updated by background thread) ───────────────────────────────
_lock    = threading.Lock()
_state   = {
    "edges":       [],
    "last_scan":   None,   # ISO string
    "scanning":    False,
    "error":       None,
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
        with _edge_history_lock:
            snapshot = dict(_edge_price_history)
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
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
_last_odds_refresh: float = 0.0   # epoch seconds of last successful refresh

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

def _save_bets(bets: list):
    try:
        import tempfile, os as _os
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with _os.fdopen(fd, "w") as f:
            json.dump(bets, f, indent=2)
        _os.replace(tmp, BETS_FILE)
    except Exception as exc:
        print(f"  WARNING: could not save bets: {exc}")

_bets: list = _load_bets()
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


def _bet_id(ticker: str, side: str) -> str:
    return f"{ticker}|{side}"


def _best_edge_per_game(edges: list) -> list:
    """
    Keep only the single best edge per (matchup, mkt_type, side) group.
    This eliminates correlated bets on the same game — e.g. if a game has
    edges at >7.5, >8.5, and >9.5 total runs YES, only the highest-edge one
    is kept.  YES and NO on the same market are treated as distinct slots so
    both can surface if both are genuinely +EV.

    NOTE: uses the raw 'edge' field (0–1 decimal) so this function is safe to
    call before 'edge_pct' is computed.
    """
    def _score(e: dict) -> float:
        return e.get("edge_pct", e.get("edge", 0) * 100)

    best: dict = {}
    for e in edges:
        key = (e.get("matchup", ""), e.get("mkt_type", ""), e.get("side", ""))
        if key not in best or _score(e) > _score(best[key]):
            best[key] = e
    seen = set()
    result = []
    for e in edges:
        key = (e.get("matchup", ""), e.get("mkt_type", ""), e.get("side", ""))
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
                      and b.get("paper_stake") is not None]
    bal = PAPER_START_BALANCE
    for b in paper_bets:
        if b["status"] in ("won", "lost") and b.get("paper_pnl") is not None:
            bal += b["paper_pnl"]
    return round(bal, 2)


def _paper_kelly_stake(edge_pct: float, kalshi_price: float) -> float:
    """Calculate Kelly-sized paper stake against current portfolio balance."""
    k = kalshi_price
    e = edge_pct / 100.0
    if k <= 0 or k >= 1 or e <= 0:
        return 0.0
    balance = _compute_paper_balance()
    full_kelly = e / (1.0 - k)
    frac = min(full_kelly * PAPER_KELLY_FRACTION, PAPER_KELLY_CAP)
    return round(frac * balance, 2)


def _add_new_bets(edges: list):
    """Log any edge we haven't seen before as an open bet.

    Source of truth: Pinnacle line only.
      • pin_prob_at_flag  = Pinnacle's no-vig probability when the edge was first
        detected.  Stored on the bet so we can detect line movement later.
      • DK / FD confirmation steps removed — Pinnacle is the sole fair-value anchor.

    Deduplication has two layers:
      1. Within-cycle: _best_edge_per_game keeps only the highest-edge market
         per (matchup, mkt_type, side) from the current scan batch.
      2. Cross-cycle: before logging, check if an OPEN bet on the same
         (matchup, mkt_type, side) already exists from a prior scan cycle.
         If so, skip — the two bets are correlated (same game outcome) and
         logging both would overstate win rate and violate Kelly independence.
    """
    edges = _best_edge_per_game(edges)
    with _bets_lock:
        existing_ids = {b["id"] for b in _bets}
        # Build a set of (matchup, mkt_type, side) slots already occupied by open bets
        open_slots = {
            (b["matchup"], b.get("mkt_type", ""), b["side"])
            for b in _bets if b["status"] == "open"
        }
        added = 0
        for e in edges:
            # Skip edges already invalidated by Pinnacle line movement
            if e.get("pin_invalidated"):
                continue
            bid = _bet_id(e["ticker"], e["side"])
            if bid in existing_ids:
                continue   # exact same market already logged
            slot = (e.get("matchup", ""), e.get("mkt_type", ""), e.get("side", ""))
            if slot in open_slots:
                continue   # correlated bet — same game/type/side already open, skip

            # entry_yes_pct = Kalshi YES ask % when flagged (for CLV calculation)
            kalshi_yes_at_flag = e["kalshi_pct"] if e["side"] == "YES" else round((1 - e["kalshi"]) * 100, 1)
            paper_stake = _paper_kelly_stake(e["edge_pct"], e["kalshi"])

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

            _bets.append({
                "id":                 bid,
                "ticker":             e["ticker"],
                "matchup":            e["matchup"],
                "title":              e["title"],
                "side":               e["side"],
                "mkt_type":           e.get("mkt_type", ""),
                "edge_pct":           e["edge_pct"],            # post-haircut adj. edge %
                "raw_edge_pct":       round(e.get("raw_edge", 0) * 100, 1),
                "books_used":         e.get("books_used", []),
                "consensus_prob":     e.get("consensus_prob"),
                # ── Pinnacle source-of-truth fields ──────────────────────────
                "pin_prob_at_flag":   pin_prob_at_flag,         # Pinnacle % at detection
                "pin_prob_pct":       e.get("pin_prob_pct"),    # same, pre-computed by _run_scan
                # ─────────────────────────────────────────────────────────────
                "kalshi_price":       e["kalshi"],
                "kalshi_yes_at_flag": kalshi_yes_at_flag,
                "flagged_at":         datetime.now(timezone.utc).isoformat(),
                "game_time":          _parse_ticker_start_time(e["ticker"]).isoformat()
                                      if _parse_ticker_start_time(e["ticker"]) else None,
                "status":             "open",
                "resolved_at":        None,
                "pnl":                None,
                "closing_yes_pct":    kalshi_yes_at_flag,   # init to entry; CLV loop overwrites
                "closing_pin_pct":    None,                 # Pinnacle side-prob at close; CLV loop fills
                "clv":                0.0,                  # init to 0; CLV loop overwrites
                "paper_stake":        paper_stake,           # Kelly-sized virtual wager
                "paper_pnl":          None,                  # set on resolution
            })
            existing_ids.add(bid)
            open_slots.add(slot)
            added += 1
        if added:
            _save_bets(_bets)
            print(f"  Bet tracker: logged {added} new bet(s)")


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
            game_date = g.get("commence_time", "")[:10]   # "YYYY-MM-DD"
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
                        b["clv"] = round(closing_pin - entry_k, 1)
                    else:
                        closing_yes = b.get("closing_yes_pct")
                        entry_yes   = b.get("kalshi_yes_at_flag")
                        if closing_yes is not None and entry_yes is not None:
                            b["clv"] = round(
                                closing_yes - entry_yes if b["side"] == "YES"
                                else entry_yes - closing_yes, 1
                            )
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
            if bet_pin_close is not None and entry_k:
                # True CLV: Pinnacle side-prob at close minus Kalshi entry price.
                # Positive = sharp market agreed your edge was real at close.
                clv = round(bet_pin_close - entry_k, 1)
            elif bet_closing is not None and entry_yes is not None:
                # Fallback: Kalshi drift (side-appropriate ask/bid prices)
                clv = round(bet_closing - entry_yes if bet["side"] == "YES"
                            else entry_yes - bet_closing, 1)
            closing_yes = bet_closing
            with _bets_lock:
                for b in _bets:
                    if b["id"] == bet["id"]:
                        b["status"]          = "won" if side_won else "lost"
                        b["resolved_at"]     = datetime.now(timezone.utc).isoformat()
                        b["resolved_by"]     = "kalshi"
                        b["closing_yes_pct"] = closing_yes
                        b["closing_pin_pct"] = bet_pin_close
                        b["clv"]             = clv
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
_CLV_PENALTY_MIN_SAMPLE = 40


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

    # Game lines only — exclude all props (MLB + NBA) from top-level pills.
    def _is_prop(b: dict) -> bool:
        return _infer_mkt_type(b) in ("prop", "nba_prop")

    gl_bets    = [b for b in bets    if not _is_prop(b)]
    gl_won     = [b for b in won     if not _is_prop(b)]
    gl_lost    = [b for b in lost    if not _is_prop(b)]
    gl_open    = [b for b in open_   if not _is_prop(b)]
    gl_settled = gl_won + gl_lost

    # ── Kelly sizing helper ────────────────────────────────────────────────────
    # 0.25 Fractional Kelly for a binary prediction-market bet, with:
    #   • Hard 5% bankroll cap per bet
    #   • Dynamic CLV confidence multiplier — types with negative avg CLV get
    #     0.5× applied on top of the quarter-Kelly fraction
    #
    # Full Kelly  = edge / (1 − kalshi_price)
    # Adjusted    = full × 0.25 × clv_multiplier,  capped at 5% of bankroll
    KELLY_FRACTION   = 0.25   # fractional Kelly scaling
    KELLY_SINGLE_CAP = 0.05   # max 5% of bankroll per bet

    # Fetch CLV-based multipliers once for the whole performance pass
    clv_mults = _get_clv_multipliers()

    def _kelly_frac(b: dict) -> float:
        k = b.get("kalshi_price", 0.5)
        e = b.get("edge_pct", 0) / 100.0
        if k <= 0 or k >= 1 or e <= 0:
            return 0.0
        full_kelly  = e / (1.0 - k)
        mtype       = _infer_mkt_type(b)
        clv_mult    = clv_mults.get(mtype, 1.0)
        return min(full_kelly * KELLY_FRACTION * clv_mult, KELLY_SINGLE_CAP)

    def _kelly_pnl(b: dict) -> Optional[float]:
        """P&L as fraction-of-bankroll under CLV-adjusted quarter-Kelly sizing."""
        if b["status"] not in ("won", "lost"):
            return None
        f = _kelly_frac(b)
        k = b["kalshi_price"]
        if b["status"] == "won":
            return f * (1.0 - k) / k   # profit = stake × net_odds
        return -f                        # loss = −stake (fraction of bankroll)

    # CLV stats — game lines only, all bets with Pinnacle or Kalshi data
    clv_bets = [b for b in gl_bets if b.get("clv_source") in ("pin", "pin_entry", "kalshi")]
    avg_clv  = round(sum(b["clv"] for b in clv_bets) / len(clv_bets), 1) if clv_bets else None

    # Average line movement — game lines only, Pinnacle-tracked bets only
    line_moves = []
    for b in gl_bets:
        entry = (b.get("kalshi_price") or 0) * 100
        close = b.get("closing_pin_pct")
        if not entry or close is None:
            continue
        line_moves.append(close - entry)
    avg_line_move = round(sum(line_moves) / len(line_moves), 1) if line_moves else None

    # Win rate — game lines only
    win_rate = round(len(gl_won) / len(gl_settled) * 100, 1) if gl_settled else None

    # Average edge — game lines only
    avg_edge = round(sum(b["edge_pct"] for b in gl_bets) / len(gl_bets), 1) if gl_bets else None

    # ── Flat unit P&L — game lines only ──────────────────────────────────────
    unit_pnls = []
    for b in gl_settled:
        k = b["kalshi_price"]
        unit_pnls.append((1 - k) / k if b["status"] == "won" else -1.0)

    total_units = round(sum(unit_pnls), 3) if unit_pnls else None
    avg_units   = round(sum(unit_pnls) / len(unit_pnls), 3) if unit_pnls else None

    # ── Kelly-weighted P&L — game lines only ─────────────────────────────────
    kelly_pnls = [_kelly_pnl(b) for b in gl_settled]
    kelly_pnls = [x for x in kelly_pnls if x is not None]

    total_kelly_units   = round(sum(kelly_pnls), 4)         if kelly_pnls else None
    total_kelly_dollars = round(sum(kelly_pnls) * PERF_BANKROLL, 2) if kelly_pnls else None
    avg_kelly_units     = round(sum(kelly_pnls) / len(kelly_pnls), 4) if kelly_pnls else None

    # Model accuracy — game lines only
    if gl_settled:
        avg_kalshi_implied = round(
            sum(b["kalshi_price"] * 100 for b in gl_settled) / len(gl_settled), 1
        )
    else:
        avg_kalshi_implied = None

    # Add per-bet P&L fields for display
    # kelly_pnl_pct = P&L as % of bankroll (e.g. +0.42 = +0.42%)
    # kelly_bet_pct = Kelly-recommended stake as % of bankroll
    table_bets = sorted(settled + open_, key=lambda b: b["flagged_at"], reverse=True)[:30]
    for b in table_bets:
        kf = _kelly_frac(b)
        kp = _kelly_pnl(b)
        b["kelly_bet_pct"]     = round(kf * 100, 3)           # e.g. 1.5 = 1.5% of bankroll
        b["kelly_bet_dollars"] = round(kf * PERF_BANKROLL, 2)
        if b["status"] == "open":
            b["kelly_pnl"]        = None
            b["kelly_pnl_pct"]    = None
            b["kelly_pnl_dollars"] = None
        else:
            b["kelly_pnl"]         = round(kp, 5) if kp is not None else None
            b["kelly_pnl_pct"]     = round(kp * 100, 3) if kp is not None else None  # % of bankroll
            b["kelly_pnl_dollars"] = round(kp * PERF_BANKROLL, 2) if kp is not None else None
        # Flag which multiplier was applied so the UI can show a note
        mtype = _infer_mkt_type(b)
        b["clv_mult_applied"]  = clv_mults.get(mtype, 1.0)

    def _perf_label(b: dict) -> str:
        ticker = b.get("ticker", "").upper()
        mtype  = _infer_mkt_type(b)
        if mtype == "nba_prop":
            return "NBA Props"
        if mtype == "prop":
            return "MLB Props"
        if ticker.startswith("KXNBA"):
            sport = "NBA"
        else:
            sport = "MLB"
        return f"{sport} {mtype.capitalize()}" if mtype else sport

    by_type = {}
    for b in settled:
        label = _perf_label(b)
        if label not in by_type:
            by_type[label] = {"won": 0, "lost": 0, "units": [], "kelly": []}
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

    MIN_SAMPLE = 20   # need at least 20 settled bets before win rate is meaningful

    type_breakdown = []
    for label, d in by_type.items():
        total_t    = d["won"] + d["lost"]
        wr_t       = round(d["won"] / total_t * 100, 1) if total_t else None
        # Kelly P&L expressed as % of bankroll (sum of kelly fractions × 100)
        kelly_pct_t = round(sum(d["kelly"]) * 100, 3) if d["kelly"] else None
        # Dollar amount for display alongside %, derived from pct × bankroll
        kelly_t    = round(sum(d["kelly"]) * PERF_BANKROLL, 2) if d["kelly"] else None
        type_breakdown.append({
            "label":             label,
            "won":               d["won"],
            "lost":              d["lost"],
            "win_rate":          wr_t,
            "kelly_pct":         kelly_pct_t,    # % of bankroll P&L
            "kelly_dollars":     kelly_t,
            "insufficient_data": total_t < MIN_SAMPLE,
            "sample_size":       total_t,
        })
    type_breakdown.sort(key=lambda x: -(x["won"] + x["lost"]))

    # Total Kelly P&L as % of bankroll
    total_kelly_pct = round(sum(kelly_pnls) * 100, 3) if kelly_pnls else None

    return {
        "total_bets":           len(gl_bets),
        "won":                  len(gl_won),
        "lost":                 len(gl_lost),
        "open":                 len(gl_open),
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
_ALERT_MIN    = float(os.getenv("ALERT_MIN_EDGE", "0.03"))  # Discord alerts at ≥3% edge (matches .env default)
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
PROPS_REFRESH_SECONDS  = 2 * 60 * 60  # props scan every 2 hours (~45 credits/scan)
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


def _alert_top10():
    """
    After each scan, alert on any qualifying edge that hasn't been seen before.
    Qualifying = edge_pct >= ALERT_MIN_EDGE (default 4%) + passed book-consensus
    validation (is_valid_consensus is always True for edges that reach here).

    Each unique edge fires exactly once per server session (_alerted_keys never
    clears), so restarting the server will re-alert on edges that are still live.
    """
    global _alerted_keys

    with _lock:
        game_edges = list(_state.get("edges", []))
    min_edge = _ALERT_MIN  # from env: ALERT_MIN_EDGE (default 0.04 = 4%)

    # Apply minimum edge threshold, sort best-first
    all_edges = sorted(
        [e for e in game_edges
         if e.get("edge_pct", e.get("edge", 0) * 100) >= min_edge * 100],
        key=lambda x: x.get("edge_pct", x.get("edge", 0) * 100),
        reverse=True,
    )

    now_utc = datetime.now(timezone.utc)

    for e in all_edges:
        key = _edge_key(e)
        if key in _alerted_keys:
            continue   # already alerted — skip

        # Skip (and permanently mark) edges for games that have already started.
        # Prevents stale alerts after Railway restarts or slow scan cycles.
        ticker = e.get("ticker", "")
        game_start = _parse_ticker_start_time(ticker)
        if game_start is None:
            # Fallback: estimate start from expiration_time.
            # NBA games run ~2.5h + Kalshi buffer → use 4h offset.
            # MLB games run ~3h + buffer → use 3.5h offset.
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
            _alerted_keys.add(key)
            _save_alerted_keys(_alerted_keys, _gone_alerted_keys)
            continue   # game already started — no alert, just dedup

        ts   = datetime.now().strftime("%I:%M %p")
        k    = e.get("kalshi", 0.5)
        ep   = round(e.get("edge_pct", e.get("edge", 0) * 100), 1)
        side = e.get("side", "?")

        # American odds
        # e["fair"] = model probability for the bet side at the exact Kalshi threshold.
        # This is the correct "consensus" price — it's calibrated to the specific bet line,
        # not the book's main line (which can be at a different threshold for totals).
        kalshi_amer = _prob_to_american_str(k)
        fair_p      = e.get("fair", None)
        cons_amer   = _prob_to_american_str(fair_p)   # fair value for this exact bet

        # Confirming books (from consensus_reason: "Confirmed by Pinnacle + DraftKings")
        conf_reason = e.get("consensus_reason", "")
        conf_books  = conf_reason.replace("Confirmed by ", "").replace("Pinnacle", "PIN").replace("DraftKings", "DK").replace("FanDuel", "FD") if conf_reason else "PIN+?"

        # Kelly bet size
        kelly_frac = _sms_kelly(e)
        kelly_bet  = round(kelly_frac * PERF_BANKROLL, 2)
        kelly_pct  = round(kelly_frac * 100, 2)
        clv_mults  = _get_clv_multipliers()
        clv_tag    = " (½ CLV penalty)" if clv_mults.get(e.get("mkt_type", ""), 1.0) == 0.5 else ""

        # Embed color: green ≥10%, yellow ≥7%, blue otherwise
        color = 0x00c853 if ep >= 10 else 0xffe57a if ep >= 7 else 0x2979ff
        stars = "🔥" if ep >= 10 else "⚡" if ep >= 7 else "📈"
        conf_label = "★★★ HIGH CONF" if e.get("confidence", 0) >= 0.80 else "★★ MED CONF" if e.get("confidence", 0) >= 0.50 else "★ LOW CONF"

        # content = what shows on the phone lock screen without unlocking
        # Keep it tight: the two numbers the user needs at a glance
        content = f"{stars} **+{ep}% edge** | Kelly **${kelly_bet:.0f}** ({kelly_pct}%){clv_tag}"

        # Rich embed — full detail visible after opening Discord
        ticker_str = e.get("ticker", "")
        mkt_type_str = e.get("mkt_type", "").upper()
        embed = {
            "color": color,
            "author": {"name": f"Kalshi EV Scanner  •  {ts}"},
            "title": f"{e.get('matchup', '')}",
            "description": f"**{e.get('title', '')}**  —  Side: **{side}**",
            "fields": [
                {"name": "Adj. Edge",      "value": f"`+{ep}%`",                        "inline": True},
                {"name": "Kelly Stake",    "value": f"`${kelly_bet:.0f}` ({kelly_pct}%){clv_tag}", "inline": True},
                {"name": "Confidence",     "value": conf_label,                          "inline": True},
                # ⚠ Kalshi price is captured at scan time — can be up to 2 min stale.
                # Always use ✓ Check (or validate_bet) before placing.
                {"name": "Kalshi Price",   "value": f"`{kalshi_amer}`  ({round(k*100)}¢)  *(scan price)*","inline": True},
                {"name": "Fair Value",     "value": f"`{cons_amer}`",                    "inline": True},
                {"name": "Books",          "value": conf_books,                          "inline": True},
                {"name": "Market Ticker",  "value": f"`{ticker_str}`  [{mkt_type_str}]", "inline": False},
            ],
            "footer": {"text": f"⚠ Kalshi price shown is from scan time — verify live price before betting  •  Bankroll ${PERF_BANKROLL:.0f}  •  Pinnacle fair value"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        ok = send_discord(embed, content)
        if ok:
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
        pin_shift = hist.get("last_pin_pct", 0) - hist.get("first_pin_pct", 0) if hist else None
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
    Fetch fresh book odds (Pinnacle + DK + FanDuel) and update the cached indices.
    Costs exactly 2 Odds API credits (1 per sport: MLB + NBA).
    Runs every ODDS_REFRESH_SECONDS in a dedicated background thread.
    """
    global _cached_mlb_index, _cached_nba_index, _last_odds_refresh
    from kalshi_ev_scanner import fetch_odds_index
    print(f"\n  ── Odds index refresh  {datetime.now().strftime('%H:%M:%S')} ──")

    try:
        mlb_idx, _ = fetch_odds_index(
            "baseball_mlb", total_range=(5.0, 14.0), spread_limit=3.0
        )
        if mlb_idx is not None:
            with _odds_cache_lock:
                _cached_mlb_index = mlb_idx
    except Exception as exc:
        print(f"  ERROR refreshing MLB odds index: {exc}")

    try:
        nba_idx, _ = fetch_odds_index(
            "basketball_nba", total_range=(170.0, 280.0), spread_limit=40.0
        )
        if nba_idx is not None:
            with _odds_cache_lock:
                _cached_nba_index = nba_idx
    except Exception as exc:
        print(f"  ERROR refreshing NBA odds index: {exc}")

    with _odds_cache_lock:
        _last_odds_refresh = time.time()


def _background_odds_loop():
    """Refresh book-odds cache on an adaptive schedule (costs 3 credits/refresh)."""
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
        # If cache is still None (very first startup before odds loop finishes),
        # scan_sport falls back to fetching inline automatically.
        with _odds_cache_lock:
            mlb_idx = _cached_mlb_index
            nba_idx = _cached_nba_index

        mlb = scan_sport(
            label="MLB — Run Line, Totals & Moneyline",
            spread_series="KXMLBSPREAD",
            total_series="KXMLBTOTAL",
            ml_series="KXMLBML",
            odds_sport="baseball_mlb",
            abbr_map=MLB_ABBR,
            spread_std=MLB_SPREAD_STD,
            total_std=MLB_TOTAL_STD,
            game_index=mlb_idx,
        )

        nba = scan_sport(
            label="NBA — Spread, Totals & Moneyline",
            spread_series="KXNBASPREAD",
            total_series="KXNBATOTAL",
            ml_series="KXNBAML",
            odds_sport="basketball_nba",
            abbr_map=NBA_ABBR,
            spread_std=NBA_SPREAD_STD,
            total_std=NBA_TOTAL_STD,
            game_index=nba_idx,
        )

        global _last_props_scan
        now_ts = time.time()
        if now_ts - _last_props_scan >= PROPS_REFRESH_SECONDS:
            mlb_props = scan_player_props(odds_sport="baseball_mlb", abbr_map=MLB_ABBR)
            nba_props = scan_nba_player_props()
            _last_props_scan = now_ts
        else:
            mlb_props, nba_props = [], []

        all_edges = sorted(mlb + nba + mlb_props + nba_props, key=lambda x: x["edge"], reverse=True)

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
            _state["edges"]     = edges
            _state["last_scan"] = now_iso
            _state["scanning"]  = False

        # Log new bets (skips pin_invalidated edges)
        _add_new_bets(edges)

        # ── Zero-edge drought check ───────────────────────────────────────────
        pass  # zero-edge health check removed — scanner stability confirmed

    except Exception as exc:
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

    # ── Alert on new edges (runs every cycle, even if scan threw an error) ──────
    # Decoupled from scan so a failed scan doesn't delay Discord notifications.
    # Reads from _state["edges"] (last successful scan's edges).
    _alert_top10()

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
        time.sleep(REFRESH_SECONDS)



RESOLUTION_POLL_SECONDS = 5 * 60   # check for settled games every 5 minutes
CLV_CAPTURE_SECONDS     = 60       # refresh closing prices for open bets every 60 sec


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
            # fall back to close_time - 2.5h for NBA tickers that embed no time.
            game_start = _parse_ticker_start_time(bet["ticker"])
            if game_start is None:
                close_str = mkt.get("close_time") or mkt.get("expected_expiration_time")
                if close_str:
                    try:
                        from datetime import timedelta as _tdc
                        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                        game_start = close_dt - _tdc(hours=2.5)
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
            bid_c = float(mkt.get("yes_bid_dollars") or 0) * 100
            ask_c = float(mkt.get("yes_ask_dollars") or 0) * 100
            if bid_c <= 0 or ask_c <= 0:
                continue

            # Use the side-appropriate transactable price, not the mid.
            # Entry was captured as: YES bets → YES ask, NO bets → YES bid.
            # Comparing to the mid would introduce a structural negative bias
            # equal to ~half the spread (typically 2–4 pp) on every bet.
            side = bet.get("side", "YES")
            yes_close_pct = round(ask_c if side == "YES" else bid_c, 1)

            # Safety guard: reject prices that have collapsed to in-game extremes.
            # A market at 97¢+ or 3¢- means the outcome is effectively decided —
            # that's a live game price, not a pre-game closing line.
            # This prevents contamination if game_start parsing ever fails.
            if yes_close_pct >= 97.0 or yes_close_pct <= 3.0:
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
                            b["clv"]        = round(closing_pin - entry_k, 1)
                            b["clv_source"] = "pin"
                        else:
                            # Fallback: Kalshi drift using matched ask/bid prices
                            entry_yes = b.get("kalshi_yes_at_flag")
                            if entry_yes is not None:
                                b["clv"] = round(
                                    yes_close_pct - entry_yes if side == "YES"
                                    else entry_yes - yes_close_pct,
                                    1,
                                )
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

<div id="today-edges-card" class="card">
  <div class="card-header exec-card-header" onclick="toggleCard('today-edges-body')">📅 Today's Edges &nbsp;<span style="font-size:10px;color:var(--muted);font-weight:400;">all edges flagged today · live tracker updates every 2 min</span> <span class="card-toggle" id="today-edges-body-toggle">▾</span></div>
  <div id="today-edges-body" class="card-body"><div class="empty">No edges found today yet.</div></div>
</div>

<div id="mlb-card" class="card">
  <div class="card-header mlb" onclick="toggleCard('mlb-body')">⚾ MLB &nbsp;<span style="font-size:10px;color:var(--muted);font-weight:400;">spreads · totals · moneylines · sorted by edge strength</span> <span class="card-toggle" id="mlb-body-toggle">▾</span></div>
  <div id="mlb-body" class="card-body"><div class="empty">No MLB edges ≥3% right now.</div></div>
</div>

<div id="nba-card" class="card">
  <div class="card-header" style="border-left:3px solid #58a6ff;" onclick="toggleCard('nba-body')">🏀 NBA &nbsp;<span style="font-size:10px;color:var(--muted);font-weight:400;">spreads · totals · moneylines · sorted by edge strength</span> <span class="card-toggle" id="nba-body-toggle">▾</span></div>
  <div id="nba-body" class="card-body"><div class="empty">No NBA edges ≥3% right now.</div></div>
</div>


<div id="paper-card" class="card">
  <div class="card-header" onclick="toggleCard('paper-body')" style="border-left:3px solid #3fb950;">📊 Paper Portfolio — 0.25 Kelly · All 3%+ Edges · $1,000 Starting Balance <span class="card-toggle" id="paper-body-toggle">▾</span></div>
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
  <div class="card-header" onclick="toggleCard('history-body')">Edge History <span class="card-toggle" id="history-body-toggle">▾</span></div>
  <div id="history-body" class="card-body" style="padding:12px;">
    <div class="chart-empty">Waiting for first scan…</div>
  </div>
</div>

<div id="mlb-spread-card" class="card">
  <div class="card-header mlb" onclick="toggleCard('mlb-spread-body')">⚾ MLB — Run Line <span class="card-toggle" id="mlb-spread-body-toggle">▾</span></div>
  <div id="mlb-spread-body" class="card-body card-mlb-spread"><div class="empty">No run line edges found. Scanning every 2 min.</div></div>
</div>

<div id="mlb-total-card" class="card">
  <div class="card-header mlb" onclick="toggleCard('mlb-total-body')">⚾ MLB — Totals <span class="card-toggle" id="mlb-total-body-toggle">▾</span></div>
  <div id="mlb-total-body" class="card-body card-mlb-total"><div class="empty">No totals edges found. Scanning every 2 min.</div></div>
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

<script src="/static/chart.umd.min.js"></script>
<script>
const REFRESH_MS = """ + str(REFRESH_SECONDS * 1000) + """;
let nextRefresh = Date.now() + REFRESH_MS;
let lastEdges      = [];   // MLB spreads/totals
let prevEdgeKeys = new Set();
let historyChart = null;
let clvMultipliers = {};   // {"prop": 0.5, "spread": 1.0, …} — set by fetchPerformance()

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
    const r = await fetch('/api/history');
    const data = await r.json();
    renderHistoryChart(data);
  } catch (e) { console.error('history fetch failed', e); }
}

function renderHistoryChart(data) {
  const el = document.getElementById('history-body');
  if (!data.length) {
    el.innerHTML = '<div class="chart-empty">No history yet — data appears after first scan.</div>';
    return;
  }

  // Ensure canvas exists
  if (!document.getElementById('history-canvas')) {
    el.innerHTML = '<canvas id="history-canvas"></canvas>';
  }

  const labels   = data.map(d => new Date(d.ts).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}));
  const counts   = data.map(d => d.edge_count);
  const topEdges = data.map(d => d.top_edge);

  const ctx = document.getElementById('history-canvas').getContext('2d');

  if (historyChart) {
    historyChart.data.labels         = labels;
    historyChart.data.datasets[0].data = counts;
    historyChart.data.datasets[1].data = topEdges;
    historyChart.update('none');
    return;
  }

  historyChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Edge count',
          data: counts,
          borderColor: '#3fb950',
          backgroundColor: 'rgba(63,185,80,0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 3,
          yAxisID: 'yCount',
        },
        {
          label: 'Top edge (%)',
          data: topEdges,
          borderColor: '#58a6ff',
          backgroundColor: 'rgba(88,166,255,0.06)',
          fill: false,
          tension: 0.3,
          pointRadius: 3,
          yAxisID: 'yEdge',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#8b949e', font: { size: 12 } } },
        tooltip: { backgroundColor: '#161b22', borderColor: '#30363d', borderWidth: 1,
                   titleColor: '#e6edf3', bodyColor: '#8b949e' },
      },
      scales: {
        x: {
          ticks: { color: '#8b949e', maxTicksLimit: 10, maxRotation: 0 },
          grid:  { color: '#21262d' },
        },
        yCount: {
          type: 'linear', position: 'left',
          title: { display: true, text: 'Edges', color: '#3fb950', font: { size: 11 } },
          ticks: { color: '#3fb950', stepSize: 1 },
          grid:  { color: '#21262d' },
          min: 0,
        },
        yEdge: {
          type: 'linear', position: 'right',
          title: { display: true, text: 'Top edge %', color: '#58a6ff', font: { size: 11 } },
          ticks: { color: '#58a6ff', callback: v => v + '%' },
          grid:  { drawOnChartArea: false },
          min: 0,
        },
      },
    },
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
      <td class="prop-col">${e.title}${newBadge}${staleBadge}${driftTxt}${trackBtn(e)}</td>
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
  const mlbEdges = lastEdges.filter(e => sportOf(e) === 'mlb');
  const nbaEdges = lastEdges.filter(e => sportOf(e) === 'nba');
  _setHTML('mlb-body', renderTable(mlbEdges));
  _setHTML('nba-body', renderTable(nbaEdges));

  // Update tab title
  const count = lastEdges.length;
  document.title = count > 0 ? `(${count}) Kalshi EV Scanner` : 'Kalshi EV Scanner';
}

let autoRefreshTimer = null;

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

    // While scanning: show spinner only if we have no data yet
    if (d.scanning) {
      if (!lastEdges.length) {
        const scanning = '<div class="empty">Scanning...</div>';
        ['mlb-body','nba-body']
          .forEach(id => _setHTML(id, scanning));
      }
    } else {
      // Always update data and re-render
      prevEdgeKeys = new Set(lastEdges.map(edgeKey));
      lastEdges = d.edges || [];
      renderAll();
      renderTodayEdges();

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
    todayEdgesList = await r.json();
  } catch(e) { console.error('today_edges fetch failed', e); }
  renderTodayEdges();
}

function renderTodayEdges() {
  const el = document.getElementById('today-edges-body');
  if (!el) return;
  if (!todayEdgesList.length) {
    el.innerHTML = '<div class="empty">No edges found today yet. Scanning every 2 min.</div>';
    return;
  }

  // Build live lookup: ticker|side -> live edge object
  const liveMap = {};
  for (const e of lastEdges) {
    liveMap[e.ticker + '|' + e.side] = e;
  }

  const rows = [...todayEdgesList].reverse().map(b => {
    const key  = b.ticker + '|' + b.side;
    const live = liveMap[key];
    const isLive = !!live;
    const statusBadge = isLive
      ? `<span style="color:#3fb950;font-weight:700;font-size:11px;">● LIVE</span>`
      : `<span style="color:var(--muted);font-size:11px;">○ GONE</span>`;
    const currentEdge = isLive
      ? `<span style="color:${edgeColor(live.edge_pct)};font-weight:700;">+${pct(live.edge_pct)}</span>`
      : `<span style="color:var(--muted);">—</span>`;
    const flagTime = b.flagged_at ? fmtDate(b.flagged_at) : '—';
    const stake    = b.paper_stake != null ? `$${b.paper_stake.toFixed(0)}` : '—';
    const sideClass = b.side === 'YES' ? 'side-yes' : 'side-no';
    const tickerTxt = `<span style="display:block;font-size:8px;color:var(--muted);font-family:monospace;margin-top:2px;">${b.ticker}</span>`;
    return `<tr>
      <td style="font-size:11px;color:var(--muted);white-space:nowrap;">${flagTime}</td>
      <td>${matchupHtml(b.matchup)}</td>
      <td class="prop-col" style="font-size:12px;">${b.title}${tickerTxt}</td>
      <td class="${sideClass}">${b.side}</td>
      <td class="num" style="color:${edgeColor(b.edge_pct)};font-weight:700;">+${pct(b.edge_pct)}</td>
      <td class="num">${currentEdge}</td>
      <td class="num">${statusBadge}</td>
      <td class="num">${stake}</td>
    </tr>`;
  }).join('');

  el.innerHTML = `<table>
    <thead><tr>
      <th>Flagged</th><th>Matchup</th><th>Bet</th><th>Side</th>
      <th class="num">Edge @ Flag</th>
      <th class="num">Current Edge</th>
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
      <td class="prop-col">${typeTag}${e.title}${newBadge}${unvalidatedBadge}${consBadge10}${ageBadge}${trackBtn(e)}${tickerBadge}</td>
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
    const since = (document.getElementById('perf-since') || {}).value || '';
    const url   = since ? `/api/performance?since=${encodeURIComponent(since)}` : '/api/performance';
    const r = await fetch(url);
    const d = await r.json();
    // update filter label
    const lbl = document.getElementById('perf-filter-label');
    if (lbl) {
      if (since) {
        const isDefault = since === '2026-04-07';
        lbl.textContent = isDefault
          ? `Post-fix only (from ${since}) — clear to see all bets`
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

  document.getElementById('perf-stats').innerHTML = `
    <div class="stat-row">
      ${pill('Tracked', d.total_bets)}
      ${pill('Won', d.won, 'pnl-pos')}
      ${pill('Lost', d.lost, 'pnl-neg')}
      ${pill('Open', d.open, 'pnl-neu')}
      ${pill('Win Rate', na(d.win_rate, v => v + '%'))}
      ${pill('Avg Edge Flagged', na(d.avg_edge, v => '+' + v + '%'))}
      ${pill('Kelly P&amp;L (% bank)', d.total_kelly_pct != null ? `<span class="${kellyPctClass}">${sign(d.total_kelly_pct)}${d.total_kelly_pct.toFixed(2)}%</span>` : '—')}
      ${pill('Avg CLV', d.avg_clv != null ? `<span class="${d.avg_clv >= 0 ? 'pnl-pos' : 'pnl-neg'}">${d.avg_clv > 0 ? '+' : ''}${d.avg_clv}%</span>` : '—')}
      ${pill('Avg Line Move', d.avg_line_move != null ? `<span class="${d.avg_line_move >= 0 ? 'pnl-pos' : 'pnl-neg'}">${d.avg_line_move > 0 ? '+' : ''}${d.avg_line_move}¢</span>` : '—')}
      <div class="stat-pill"><div class="label">Model vs Market</div><div class="value">${modelCallout}</div></div>
    </div>
    ${penaltyNote}
    <p style="font-size:11px;color:var(--muted);padding-bottom:10px;">
      P&amp;L sized by <strong>0.25 Fractional Kelly</strong>, capped at 5% per bet.
      CLV-penalised types run at 0.5× until their closing-line value stabilises.
      Reported as <strong>% of bankroll</strong> — a 5¢ longshot loss shows −0.15%, not −1 unit.
    </p>`;

  // By-type breakdown table
  if (d.by_type && d.by_type.length) {
    const typeRows = d.by_type.map(t => {
      const insuf  = t.insufficient_data;
      const isProp = t.label === 'Props';
      const wrCls  = insuf || t.win_rate == null ? '' : t.win_rate >= 55 ? 'pnl-pos' : t.win_rate < 45 ? 'pnl-neg' : '';
      const kpct   = t.kelly_pct;
      const kcls   = kpct == null ? '' : kpct > 0 ? 'pnl-pos' : 'pnl-neg';
      const wrCell = insuf
        ? `<span class="insufficient-data" title="Need 20+ settled bets for reliable stats">Insufficient data (${t.sample_size})</span>`
        : t.win_rate != null ? `<span class="${wrCls}">${t.win_rate}%</span>` : '—';
      const labelCell = isProp
        ? `<span style="font-weight:600;">${t.label}</span> <span class="badge-unvalidated">⚠ UNVALIDATED</span>`
        : `<span style="font-weight:600;">${t.label}</span>`;
      const kellyCell = kpct != null
        ? `<span class="${kcls}" title="$${t.kelly_dollars != null ? Math.abs(t.kelly_dollars).toFixed(0) : '?'} on $${bankroll} bank">${sign(kpct)}${kpct.toFixed(2)}%</span>`
        : '—';
      return `<tr>
        <td>${labelCell}</td>
        <td class="num pnl-pos">${t.won}</td>
        <td class="num pnl-neg">${t.lost}</td>
        <td class="num">${wrCell}</td>
        <td class="num">${kellyCell}</td>
      </tr>`;
    }).join('');
    _setHTML('perf-body', `
      <table style="margin-bottom:8px;">
        <thead><tr>
          <th>Market Type</th><th class="num">Won</th><th class="num">Lost</th>
          <th class="num">Win Rate</th>
          <th class="num" title="Kelly P&amp;L as % of bankroll (hover for dollar amount)">Kelly P&amp;L (% bank)</th>
        </tr></thead>
        <tbody>${typeRows}</tbody>
      </table>`);
  }

  if (!d.bets.length) {
    document.getElementById('perf-body').innerHTML +=
      '<div class="empty">No bets tracked yet — edges appear after the next scan.</div>';
    return;
  }

  const PERF_PREVIEW = 15;
  const allPerfBets = d.bets;
  const showAllPerf = window._perfShowAll || false;
  const visiblePerf = showAllPerf ? allPerfBets : allPerfBets.slice(0, PERF_PREVIEW);

  let rows = visiblePerf.map(b => {
    const now = Date.now();
    const gameStartMs = b.game_time ? new Date(b.game_time).getTime() : null;
    const isLive = b.status === 'open' && gameStartMs != null && now >= gameStartMs;
    const rClass = b.status === 'won' ? 'result-won' : b.status === 'lost' ? 'result-lost' : 'result-open';
    const rLabel = b.status === 'won' ? '✓ WON' : b.status === 'lost' ? '✗ LOST'
      : isLive ? '<span style="color:#ff4444;font-weight:600;animation:pulse 1.5s infinite;">● LIVE</span>'
      : '…';
    // Kelly bet size as % of bankroll (dollar amount in tooltip)
    const kBet   = b.kelly_bet_pct != null
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
    // Line Move: entry Kalshi price → closing price (side-adjusted throughout)
    const entryK = b.kalshi_price != null ? (b.kalshi_price * 100).toFixed(0) : null;
    const pinEntry = b.pin_prob_at_flag != null ? b.pin_prob_at_flag.toFixed(1) : null;
    // Closing price — closing_pin_pct is already side-adjusted; closing_yes_pct needs flip for NO
    let closePrice = null, closeSrc = null;
    if (b.closing_pin_pct != null) {
      closePrice = b.closing_pin_pct.toFixed(0);
      closeSrc = 'Pin';
    } else if (b.closing_yes_pct != null) {
      const raw = b.side === 'YES' ? b.closing_yes_pct : 100 - b.closing_yes_pct;
      closePrice = raw.toFixed(0);
      closeSrc = 'K';
    }
    let lineMoveCell = '—';
    if (entryK != null) {
      const delta = closePrice != null ? parseFloat(closePrice) - parseFloat(entryK) : null;
      const closeColor = delta == null ? 'var(--muted)' : delta > 0 ? 'var(--green)' : delta < 0 ? 'var(--red)' : 'var(--fg)';
      const closeTxt = closePrice != null
        ? `<span style="color:${closeColor};font-weight:600;">→ ${closePrice}¢</span><span style="font-size:9px;color:var(--muted);"> ${closeSrc}</span>`
        : `<span style="color:var(--muted);">→ open</span>`;
      const pinLine = pinEntry != null
        ? `<div style="font-size:10px;color:var(--muted);margin-top:1px;">Pin at entry: ${pinEntry}¢</div>`
        : '';
      lineMoveCell = `<div style="white-space:nowrap;"><span style="color:var(--fg);">${entryK}¢</span> ${closeTxt}</div>${pinLine}`;
    }
    return `<tr>
      <td>${ts}</td>
      <td>${gameTimeCell}</td>
      <td>${b.matchup}</td>
      <td class="prop-col">${b.title}</td>
      <td class="side-${b.side.toLowerCase()}">${b.side}</td>
      <td class="num">${edgeCell}</td>
      <td class="num">${lineMoveCell}</td>
      <td class="num ${rClass}">${rLabel}</td>
      <td class="num">${kBet}</td>
      <td class="num">${kPnl}</td>
    </tr>`;
  }).join('');

  let perfTableHtml = `<table>
    <thead><tr>
      <th>Flagged</th><th>Game Time</th><th>Matchup</th><th>Prop</th><th>Side</th>
      <th class="num" title="Adjusted EV after 25% haircut (raw shown in tooltip)">Adj. EV</th>
      <th class="num" title="Kalshi entry price → closing price (Pin = Pinnacle close, K = Kalshi drift). Sub-line shows Pinnacle's read at entry.">Line Move</th>
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
  document.getElementById('perf-body').innerHTML += perfTableHtml;
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
    const r = await fetch('/api/paper');
    const d = await r.json();
    const pnlPos    = d.total_pnl >= 0;
    const pnlColor  = pnlPos ? 'var(--green)' : 'var(--red)';
    const pnlSign   = pnlPos ? '+' : '';
    const roiSign   = d.roi_pct >= 0 ? '+' : '';
    const roiColor  = d.roi_pct >= 0 ? 'var(--green)' : 'var(--red)';
    const settled   = d.won + d.lost;

    // Avg Value @ Entry — Pinnacle prob at flag minus Kalshi entry price, averaged over bets with PIN data
    const valBets = (d.bets || []).filter(b => b.pin_prob_at_flag != null && b.kalshi_price != null);
    const avgVal  = valBets.length
      ? valBets.reduce((s, b) => s + (b.pin_prob_at_flag - b.kalshi_price * 100), 0) / valBets.length
      : null;
    const valColor = avgVal == null ? 'var(--muted)' : 'var(--green)';  // always positive when bets had edge
    const valTxt   = avgVal == null ? '—' : `+${avgVal.toFixed(1)}pp`;
    const valLabel = valBets.length ? `Avg Value @ Entry (${valBets.length})` : 'Value @ Entry (pending)';

    // ── Primary KPI bar ────────────────────────────────────────────────────
    let html = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:1px;background:var(--border);border-bottom:1px solid var(--border);">
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:26px;font-weight:800;color:${pnlColor};letter-spacing:-0.5px;">$${d.balance.toFixed(2)}</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;">Current Bankroll</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:22px;font-weight:700;color:${pnlColor};">${pnlSign}$${d.total_pnl.toFixed(2)}</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;">Total P&amp;L</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:22px;font-weight:700;color:${roiColor};">${roiSign}${d.roi_pct.toFixed(2)}%</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;">ROI on $${d.start_balance.toFixed(0)}</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:22px;font-weight:700;color:${d.win_rate != null ? (d.win_rate >= 55 ? 'var(--green)' : d.win_rate < 45 ? 'var(--red)' : 'var(--text)') : 'var(--muted)'};">${d.win_rate != null ? d.win_rate + '%' : '—'}</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;">Win Rate (${settled} settled)</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:20px;font-weight:700;color:${valColor};" title="Average value locked at entry = Pinnacle prob − Kalshi price. Always positive when bets had real edge. This is what you captured, regardless of how the line moved after.">${valTxt}</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;">${valLabel}</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:18px;font-weight:600;color:var(--green);">${d.won}W</div>
        <div style="font-size:11px;color:var(--red);">${d.lost}L &nbsp; <span style="color:var(--muted);">${d.open} open</span></div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:2px;">Record</div>
      </div>
      <div style="background:var(--surface);padding:14px 16px;text-align:center;">
        <div style="font-size:18px;font-weight:600;color:var(--text);">$${d.open_exposure.toFixed(2)}</div>
        <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-top:3px;">Exposure (open)</div>
      </div>
    </div>
    <div style="padding:6px 12px;border-bottom:1px solid var(--border);background:#0d1117;">
      <span style="font-size:11px;color:var(--muted);">📊 Tracks <strong style="color:var(--text);">all edges ≥3%</strong> · 0.25 fractional Kelly · 5% max stake · compounding from $${d.start_balance.toFixed(0)} since ${d.start_date} · CLV captured every 2 min until game start</span>
    </div>`;

    // ── Bet table ──────────────────────────────────────────────────────────
    if (d.bets && d.bets.length) {
      const PAPER_PREVIEW = 15;
      const allBets = d.bets;
      const showAll = window._paperShowAll || false;
      const visibleBets = showAll ? allBets : allBets.slice(0, PAPER_PREVIEW);
      let rows = '';
      for (const b of visibleBets) {
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

        rows += `<tr>
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
      }
      html += `<table style="font-size:12px;">
        <thead><tr>
          <th>Date</th><th>Matchup</th><th>Bet</th><th>Side</th>
          <th class="num">Adj. EV</th>
          <th class="num" title="Value locked at entry = Pinnacle probability at flag − Kalshi price. Always positive when the edge was real. This is the value you captured when you placed the bet.">Value @ Entry</th>
          <th class="num">Kelly Stake</th><th class="num">P&amp;L</th><th>Result</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
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

// Resume refresh when tab becomes visible again
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { fetchData(); fetchMyBets(); }
});

// Initial load — default performance filter to clean-data start date (2026-04-07)
// This excludes pre-fix ghost edges and sub-15¢ bets from stats by default.
// Clear the date field and click Refresh to see all historical bets.
(function() {
  const inp = document.getElementById('perf-since');
  if (inp && !inp.value) inp.value = '2026-04-07';
})();
fetchData();
fetchHistory();
fetchPerformance();
fetchPaper();
fetchMyBets();
setInterval(updateCountdown, 1000);
setInterval(fetchPaper, 60 * 1000);   // refresh paper portfolio every 60s
setInterval(fetchMyBets, 60 * 1000);   // refresh my bets every 60s (mark-to-market)
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
            # Augment with watchdog / odds health info for observability
            with _odds_cache_lock:
                odds_age_sec = int(time.time() - _last_odds_refresh) if _last_odds_refresh else None
            state_copy["watchdog_last_tick"] = int(_watchdog_last_tick) if _watchdog_last_tick else None
            state_copy["odds_age_sec"]       = odds_age_sec
            state_copy["kalshi_auth_failed"] = _kalshi_auth_failed
            payload = json.dumps(state_copy).encode()
            self._send(200, "application/json", payload)

        elif path == "/api/history":
            payload = json.dumps(_history[-200:]).encode()
            self._send(200, "application/json", payload)

        elif path == "/api/today_edges":
            today = datetime.now(timezone.utc).date().isoformat()
            with _bets_lock:
                today_bets = [b for b in _bets if b.get("flagged_at", "")[:10] == today]
            payload = json.dumps(today_bets).encode()
            self._send(200, "application/json", payload)

        elif path == "/api/performance":
            # optional ?since=YYYY-MM-DD  (ISO date, inclusive lower bound)
            from urllib.parse import urlparse, parse_qs
            qs    = parse_qs(urlparse(self.path).query)
            since = qs.get("since", [None])[0]
            payload = json.dumps(_get_performance(since=since)).encode()
            self._send(200, "application/json", payload)

        elif path == "/api/paper":
            with _bets_lock:
                paper_bets = [b for b in _bets
                              if b.get("flagged_at", "") >= PAPER_START_DATE
                              and b.get("paper_stake") is not None]
            balance     = _compute_paper_balance()
            open_exp    = round(sum(b["paper_stake"] for b in paper_bets if b["status"] == "open"), 2)
            settled     = [b for b in paper_bets if b["status"] in ("won", "lost")]
            won_bets    = [b for b in settled if b["status"] == "won"]
            total_pnl   = round(balance - PAPER_START_BALANCE, 2)
            roi_pct     = round(total_pnl / PAPER_START_BALANCE * 100, 2)
            win_rate    = round(len(won_bets) / len(settled) * 100, 1) if settled else None
            avg_stake   = round(sum(b["paper_stake"] for b in paper_bets) / len(paper_bets), 2) if paper_bets else None
            # Show all bets newest-first so the running table is complete (not capped at 20)
            recent = sorted(paper_bets, key=lambda b: b.get("flagged_at",""), reverse=True)
            result = {
                "balance":        balance,
                "start_balance":  PAPER_START_BALANCE,
                "start_date":     PAPER_START_DATE,
                "total_pnl":      total_pnl,
                "roi_pct":        roi_pct,
                "open_exposure":  open_exp,
                "available":      round(balance - open_exp, 2),
                "total_bets":     len(paper_bets),
                "won":            len(won_bets),
                "lost":           len(settled) - len(won_bets),
                "open":           len(paper_bets) - len(settled),
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
                self._send(200, "application/json", json.dumps(result).encode())
            except Exception as exc:
                import traceback
                print(f"  /api/validate/ error: {exc}\n{traceback.format_exc()}")
                self._send(500, "application/json",
                           json.dumps({"error": f"Server error: {exc}"}).encode())

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
        elif path == "/api/admin/patch_bet":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            bet_id = body.get("id")
            fields = {k: v for k, v in body.items() if k != "id"}
            patched = False
            with _bets_lock:
                for b in _bets:
                    if b["id"] == bet_id:
                        b.update(fields)
                        patched = True
                        break
                if patched:
                    _save_bets(_bets)
            self._send(200, "application/json", json.dumps({"ok": True, "patched": patched}).encode())
        else:
            self._send(404, "text/plain", b"Not found")


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
    print(f"  Odds refresh: 4min game-hours / 20min overnight  (2 credits — Pinnacle+DK+FD)")
    print(f"  CLV capture : every {CLV_CAPTURE_SECONDS}s  (0 credits — Kalshi-only)")
    print(f"  Est. monthly: ~6,000 credits  (budget: 20,000)  |  Ctrl-C to stop\n")
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
