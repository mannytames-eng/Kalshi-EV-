#!/usr/bin/env python3
"""
Kalshi EV Scanner — MLB & NBA
Compares Kalshi prices against a weighted consensus of Pinnacle, DraftKings,
and FanDuel no-vig probabilities to find +EV bets.

Key design decisions:
  • Pinnacle-only fair value (DK/FD used for confirmation only)
  • 25% EV haircut applied to raw edge (accounts for model uncertainty)
  • Minimum adjusted EV ≥ 3% to flag a bet (logged to paper portfolio)
  • Live display (UI cards) shows only ≥5% edges for clean, high-confidence view
  • Hard ceiling of 20% — larger edges are almost certainly data mismatches
  • Top 25 bets per scan cycle (all qualifying edges logged to paper portfolio)
  • Max 2 bets per (matchup × market-type) group to control correlation
  • Supports spreads, totals, and moneylines for all three sports
  • recheck_ev() available for pre-execution validation

Usage:
    python kalshi_ev_scanner.py              # single scan
    python kalshi_ev_scanner.py --loop 300  # repeat every 300 seconds
"""

import argparse
import base64
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Portable base directory ──────────────────────────────────────────────────
_SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Config ─────────────────────────────────────────────────────────────────
KALSHI_API_KEY      = os.environ.get("KALSHI_API_KEY", "d09478eb-4f1d-4d1a-b12a-02893bd02738")
KALSHI_PRIVKEY_PATH = os.path.join(_SCANNER_DIR, "mannyxolo.txt")
ODDS_API_KEY        = os.environ.get("ODDS_API_KEY", "85de0453dbc95b70936e6c1b5aeba6ca")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_BASE   = "https://api.the-odds-api.com/v4"

# ── EV / filtering constants ────────────────────────────────────────────────
# All thresholds apply to the POST-fee, POST-haircut adjusted edge.
#
# Edge pipeline (applied in order):
#   1. raw_edge   = fair_prob − kalshi_ask          (gross probability gap)
#   2. fee_adj    = raw_edge − KALSHI_FEE_RATE × fair_prob × (1 − kalshi_ask)
#                                                   (subtract Kalshi profit fee)
#   3. adj_edge   = fee_adj × (1 − EV_HAIRCUT)     (model-uncertainty discount)
#
# Kalshi charges ~7% of profits on winning trades (verify at kalshi.com/fees).
# For a typical 52% fair / 45¢ entry, this costs ~2pp of the apparent edge.
# A raw gap of 3% becomes ~0.9% true EV — still positive but thin.
KALSHI_FEE_RATE    = 0.07    # Kalshi profit fee (7% of winnings) — update if tier changes
EDGE_THRESHOLD     = 0.020   # ≥2.0% fee+haircut-adjusted EV to flag
# TB NO-side experiment (2026-07-20): Total Bases is fully shadowed ($0 stake),
# so flag the UNDER (NO) side at this lower threshold to build a risk-free sample
# and test whether NO/under bets beat their price. Data-only; global threshold
# and all other markets are unchanged. Tune up if it flags too much noise.
TB_NO_EXPERIMENT_THRESHOLD = 0.005
MAX_EDGE           = 0.20    # reject edges >20% — almost certainly a stale line
EV_HAIRCUT         = 0.05    # model-uncertainty discount (5%)
TOP_BETS_PER_CYCLE = 50      # surface up to 50 qualifying bets per scan
MAX_BETS_PER_GROUP = 2       # max bets per (matchup, mkt_type) group

# Minimum Kalshi price for any side we'll consider betting.
# Data shows 0/18 wins on markets priced below 15¢ — these are almost always
# either threshold-mismatch ghost edges (Kalshi ">2.5 runs" matched to Pinnacle
# ">8.5 runs") or rare-event props where model error is amplified.
MIN_KALSHI_PRICE   = 0.15

MAX_PROP_EVENTS = 15         # prop scan credit budget — MLB only (15 events × 1 credit each)

# ── Prop lambda sanity check ─────────────────────────────────────────────────
# After computing fair_over from Pinnacle's lambda, also compute what retail
# books (DK/FanDuel) imply at the *same* Kalshi threshold using their own
# Poisson lambdas. If Pinnacle diverges from retail by more than this gap,
# the Pinnacle line is likely stale (e.g. pre-lineup early morning price).
# The bet is shadowed (tracked, not flagged) until the lines converge.
PROP_LAMBDA_SANITY_GAP = 0.12  # 12pp divergence at Kalshi threshold → shadow

# When NO retail book posted this player yet (Pinnacle-only), a large edge is
# indistinguishable from a stale/soft early Pinnacle line (4 of 5 such edges
# historically lost). Rather than fund or shadow, DEFER edges at/above this size
# and wait for DK/FanDuel to post — a real early Pinnacle edge survives and funds
# once retail confirms; a stale one never does. Pinnacle still drives everything
# below this threshold on its own.
PROP_PIN_ONLY_MAX_EDGE = 0.05  # 5% adj edge — defer above this until retail posts

# ── Book weights for consensus probability ───────────────────────────────────
# Fair value is Pinnacle ONLY — the sharpest closing-line book.
# DK and FD are still fetched and used as CONFIRMATION signals in
# _validate_book_consensus() but do NOT influence the fair-value price.
# Including soft books in fair value adds public-money noise to the model.
BOOK_WEIGHTS: Dict[str, float] = {
    "pinnacle":   1.00,   # sharp book — sole fair-value anchor
    "draftkings": 0.00,   # confirmation only — excluded from fair-value calc
    "fanduel":    0.00,   # confirmation only — excluded from fair-value calc
}

# ── Normal-distribution standard deviations (empirical) ─────────────────────
# Used for Gaussian extrapolation: given Pinnacle's spread cover prob at their
# line, compute P(margin > X) at a different threshold (the Kalshi line).
NBA_SPREAD_STD = 12.0   # points
NBA_TOTAL_STD  = 15.0   # points
MLB_SPREAD_STD =  3.2   # runs (margin)
MLB_TOTAL_STD  =  4.5   # runs (total)
# WNBA: rough estimate scaled from NBA by average score ratio (~80/115 team
# points) — not yet calibrated against settled bets. Revisit once WNBA has
# enough resolved bets to check against, same caveat as MLB_SPREAD_STD being
# static (see project memory on known gaps).
WNBA_SPREAD_STD =  8.5   # points
WNBA_TOTAL_STD  = 10.5   # points


# ── Ticker date parser ───────────────────────────────────────────────────────
_MONTH_MAP = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def _parse_ticker_date(ticker: str) -> Optional[str]:
    """Extract YYYY-MM-DD from a Kalshi ticker like KXMLBTOTAL-26APR121410CWSKC-9.
    Returns None if the date can't be parsed."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})\d{4}", ticker)
    if not m:
        # Try without time component
        m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker)
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2), m.group(3)
    month_num = _MONTH_MAP.get(mon)
    if not month_num:
        return None
    return f"20{yy}-{month_num:02d}-{dd}"


def _parse_ticker_start_time(ticker: str) -> Optional[datetime]:
    """Extract the game start datetime (UTC) from a Kalshi ticker.
    e.g. KXMLBTOTAL-26APR131840LAANYY-10 → 2026-04-13 22:40 UTC (18:40 ET → UTC)

    Kalshi encodes all times in US Eastern Time (ET).
    MLB season (Apr–Oct) and NBA playoffs (Apr–Jun) are always EDT = UTC−4.
    Nov–Mar uses EST = UTC−5; we detect this from the month.
    Returns None if no time component found."""
    from datetime import timedelta as _td2
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})", ticker)
    if not m:
        return None
    yy, mon, dd, hh, mm = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    month_num = _MONTH_MAP.get(mon)
    if not month_num:
        return None
    try:
        # Kalshi times are Eastern Time.  EDT (UTC−4) covers Apr 1 – Oct 31;
        # EST (UTC−5) covers Nov 1 – Mar 31.  Convert to UTC by adding the offset.
        et_offset_hours = 4 if 4 <= month_num <= 10 else 5
        dt_et = datetime(2000 + int(yy), month_num, int(dd), int(hh), int(mm))
        return (dt_et + _td2(hours=et_offset_hours)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Gaussian helpers (pure math, no scipy) ───────────────────────────────────
def _norm_cdf(x: float) -> float:
    """Standard normal CDF — P(Z ≤ x)."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (percent-point function).
    Uses rational approximation (Abramowitz & Stegun 26.2.23), accurate to ~4.5e-4.
    """
    if p <= 0:
        return -10.0
    if p >= 1:
        return 10.0
    if p == 0.5:
        return 0.0
    if p > 0.5:
        return -_norm_ppf(1.0 - p)
    # Rational approx for 0 < p < 0.5
    t = math.sqrt(-2.0 * math.log(p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return -(t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t))


def _push_correction(no_vig_prob: float, integer_line: float, std: float) -> float:
    """
    Correction factor for Pinnacle integer lines vs Kalshi binary markets.

    When Pinnacle posts an integer line (e.g. total 8.0 or spread -2.0), their
    no-vig probability includes push redistribution:
        no_vig = P(win) / (P(win) + P(lose))     [push excluded from denominator]

    Kalshi is a binary market — what would be a push on Pinnacle is a LOSS on Kalshi.
    So the correct Kalshi fair value is:
        fair_kalshi = P(win) = no_vig × (1 − P(push))

    Uses a Gaussian to estimate P(push) ≈ P(line − 0.5 < score < line + 0.5).

    Returns the multiplier (1 − push_prob) to apply to the Pinnacle no-vig probability.
    Only call this when the matched Pinnacle line is a whole number.
    """
    # Derive implied mean: no_vig ≈ P(score > line), so mean ≈ line + std × Φ⁻¹(no_vig)
    # First-order approximation — push_prob is small so iteration is unnecessary.
    mean = integer_line + std * _norm_ppf(no_vig_prob)
    # Integrate normal density across the ±0.5 band around the integer line
    push_prob = (
        _norm_cdf((integer_line + 0.5 - mean) / std) -
        _norm_cdf((integer_line - 0.5 - mean) / std)
    )
    push_prob = max(0.0, min(push_prob, 0.15))   # safety cap at 15%
    return 1.0 - push_prob


def _gaussian_total_fair(
    pin_over_prob: float,
    pin_line: float,
    kalshi_threshold: float,
    std: float,
) -> float:
    """
    Compute P(total > kalshi_threshold) using Gaussian extrapolation.

    Pinnacle gives us P(total > pin_line) = pin_over_prob at their posted line.
    We derive the implied mean total, then compute P(total > kalshi_threshold).

    Used when Pinnacle's line doesn't exactly match the Kalshi threshold (e.g.
    Pinnacle posts O/U 8.0 but Kalshi markets are at >7.5, >8.5, >9.5).

    Args:
        pin_over_prob:     Pinnacle no-vig prob that total goes OVER their line
        pin_line:          Pinnacle's total line (e.g. 8.0, 8.5, 9.0)
        kalshi_threshold:  Kalshi market threshold (e.g. 7.5, 8.5, 9.5)
        std:               Sport total std dev (e.g. 4.5 for MLB, 15.0 for NBA)

    Returns:
        P(total > kalshi_threshold)
    """
    # Derive implied mean total from Pinnacle's over prob:
    #   pin_over_prob = P(total > pin_line) = 1 - Φ((pin_line - mean) / std)
    #   → mean = pin_line + std × Φ⁻¹(pin_over_prob)
    mean_total = pin_line + std * _norm_ppf(pin_over_prob)
    z = (kalshi_threshold - mean_total) / std
    return 1.0 - _norm_cdf(z)


def _gaussian_spread_fair(
    pin_cover_prob: float,
    pin_spread_pt: float,
    kalshi_threshold: float,
    std: float,
    team_is_fav: bool,
) -> float:
    """
    Compute P(team margin > kalshi_threshold) using Gaussian extrapolation.

    Pinnacle gives us P(fav margin > |pin_spread_pt|) = pin_cover_prob.
    From that we derive the implied mean margin, then compute P(team margin > threshold).

    Kalshi "Team wins by over X" = P(team margin > X).
    This is fundamentally different from sportsbook cover probabilities.

    Args:
        pin_cover_prob: Pinnacle no-vig prob that the FAVORITE covers their spread
        pin_spread_pt:  Pinnacle spread for the FAVORITE (negative, e.g. -1.5)
        kalshi_threshold: The Kalshi "over X" threshold (always positive, e.g. 1.5)
        std:            Sport-specific margin std dev (e.g. 3.2 for MLB)
        team_is_fav:    Whether the Kalshi market's team IS the favorite

    Returns:
        P(team margin > kalshi_threshold)
    """
    # Pinnacle spread is negative for favorites.  |pin_spread_pt| = expected margin anchor
    pin_abs = abs(pin_spread_pt)

    # Derive implied mean margin for the favorite from the cover prob:
    #   pin_cover_prob = P(fav_margin > pin_abs) = 1 - Φ((pin_abs - mean) / std)
    #   → Φ⁻¹(pin_cover_prob) = (mean - pin_abs) / std
    #   → mean = pin_abs + std * Φ⁻¹(pin_cover_prob)
    fav_mean_margin = pin_abs + std * _norm_ppf(pin_cover_prob)

    if team_is_fav:
        # P(fav margin > threshold) = 1 - Φ((threshold - fav_mean) / std)
        z = (kalshi_threshold - fav_mean_margin) / std
        return 1.0 - _norm_cdf(z)
    else:
        # Underdog margin = -fav_margin, so underdog_mean = -fav_mean_margin
        # P(underdog margin > threshold) = P(fav margin < -threshold)
        #   = Φ((-threshold - fav_mean_margin) / std)
        z = (-kalshi_threshold - fav_mean_margin) / std
        return _norm_cdf(z)

# ── Team abbreviation → Pinnacle full name ──────────────────────────────────
NBA_ABBR: Dict[str, str] = {
    "ATL": "Atlanta Hawks",          "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets",          "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls",          "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",       "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",        "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets",        "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers",   "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",      "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",        "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans",   "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder",  "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",     "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs",      "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",              "WAS": "Washington Wizards",
}

# Verified 2026-07-10 against live Kalshi tickers (KXWNBASPREAD/KXWNBATOTAL,
# open + settled events) and live Odds API basketball_wnba event list — all
# 15 abbreviations below are Kalshi's own team codes, cross-checked one by one.
WNBA_ABBR: Dict[str, str] = {
    "ATL":  "Atlanta Dream",         "CHI":  "Chicago Sky",
    "CONN": "Connecticut Sun",       "DAL":  "Dallas Wings",
    "GS":   "Golden State Valkyries","IND":  "Indiana Fever",
    "LV":   "Las Vegas Aces",        "LA":   "Los Angeles Sparks",
    "MIN":  "Minnesota Lynx",        "NY":   "New York Liberty",
    "PHX":  "Phoenix Mercury",       "PDX":  "Portland Fire",
    "SEA":  "Seattle Storm",         "TOR":  "Toronto Tempo",
    "WSH":  "Washington Mystics",
}

MLB_ABBR: Dict[str, str] = {
    "ARI": "Arizona Diamondbacks",   "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",      "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",           "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds",        "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",       "DET": "Detroit Tigers",
    "HOU": "Houston Astros",         "KC":  "Kansas City Royals",
    "LAA": "Los Angeles Angels",     "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",          "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",        "NYM": "New York Mets",
    "NYY": "New York Yankees",       "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies",  "PIT": "Pittsburgh Pirates",
    "SD":  "San Diego Padres",       "SF":  "San Francisco Giants",
    "SEA": "Seattle Mariners",       "STL": "St. Louis Cardinals",
    "TB":  "Tampa Bay Rays",         "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",      "WSH": "Washington Nationals",
}


# ── Kalshi auth ─────────────────────────────────────────────────────────────
_privkey = None

def _load_privkey():
    global _privkey
    if _privkey is None:
        b64 = os.environ.get("KALSHI_PRIVKEY_B64")
        if b64:
            pem_bytes = base64.b64decode(b64)
        else:
            with open(KALSHI_PRIVKEY_PATH, "rb") as f:
                pem_bytes = f.read()
        _privkey = serialization.load_pem_private_key(
            pem_bytes, password=None, backend=default_backend()
        )
    return _privkey


def _sign_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    path_no_qs = path.split("?")[0]
    msg = (ts + method.upper() + "/trade-api/v2" + path_no_qs).encode()
    sig = _load_privkey().sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY":       KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


def kalshi_get(path: str, params: Optional[Dict] = None, _retries: int = 3) -> dict:
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    full_path = path + qs
    for attempt in range(_retries):
        r = requests.get(
            KALSHI_BASE + full_path,
            headers=_sign_headers("GET", full_path),
            timeout=15,
        )
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


# ── Kalshi helpers ─────────────────────────────────────────────────────────
def fetch_kalshi_events(series_ticker: str) -> List[dict]:
    events, cursor = [], None
    for _page in range(20):   # hard cap: 20 pages × 100 = 2000 events max
        params: Dict = {"series_ticker": series_ticker, "status": "open", "limit": 100,
                         "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = kalshi_get("/events", params)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return []
            raise
        batch = data.get("events", [])
        events.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return events


def fetch_event_markets(event_ticker: str) -> List[dict]:
    data = kalshi_get("/markets", {"event_ticker": event_ticker, "status": "open", "limit": 100})
    return data.get("markets", [])


MIN_PRICE      = 0.12   # mid below 12% → likely in-game (pre-game markets rarely go this low)
MAX_PRICE      = 0.88   # mid above 88% → likely in-game
PROP_MIN_PRICE = 0.08
PROP_MAX_PRICE = 0.92
PROP_MAX_EDGE  = 0.15   # props above 15% adj edge are almost certainly a line mismatch — cap separately from game-line MAX_EDGE


def kalshi_prices(mkt: dict) -> Optional[Tuple[float, float]]:
    """Return (yes_bid, yes_ask) in [0,1], or None if unavailable / out of bounds.

    Requires a live two-sided order book (both bid AND ask present).
    We do NOT fall back to last_price — that's a historical trade, not a
    current executable price.  Near game time, market makers pull their
    quotes and both sides go to 0/None.  Falling back to last_price in
    that window produces stale-data ghost edges that don't exist in the
    real order book.
    """
    try:
        bid = float(mkt.get("yes_bid_dollars") or 0)
        ask = float(mkt.get("yes_ask_dollars") or 0)
    except (TypeError, ValueError):
        bid, ask = 0.0, 0.0

    if bid <= 0 or ask <= 0:
        bid_c = mkt.get("yes_bid") or 0
        ask_c = mkt.get("yes_ask") or 0
        bid, ask = bid_c / 100, ask_c / 100

    # Both sides must be live — no last_price fallback.
    if bid <= 0 or ask <= 0:
        return None
    if bid > ask:
        bid, ask = ask, bid

    mid = (bid + ask) / 2
    if mid < MIN_PRICE or mid > MAX_PRICE:
        return None

    return bid, ask


# ── The Odds API — multi-book ─────────────────────────────────────────────────
def fetch_book_odds(sport: str, include_h2h: bool = False) -> Tuple[List[dict], str]:
    """
    Fetch spreads / totals (+ h2h if requested) from Pinnacle only.
    Only sharp-book data is used for fair-value — DK/FanDuel have 0 weight.

    h2h was removed 2026-06 (not used for fair-value, saved ~1 credit/call) and
    is now opt-in via include_h2h — MLB moneyline (2026-07) needs it for h2h
    consensus; other callers (WNBA, etc.) default False so they don't pick up
    the extra ~1 credit/call.
    """
    sharp_books = ",".join(k for k, w in BOOK_WEIGHTS.items() if w > 0)
    markets = "h2h,spreads,totals" if include_h2h else "spreads,totals"
    r = requests.get(f"{ODDS_BASE}/sports/{sport}/odds", params={
        "apiKey":     ODDS_API_KEY,
        "bookmakers": sharp_books,
        "markets":    markets,
        "oddsFormat": "american",
    }, timeout=15)
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining", "?")
    return r.json(), remaining


# Keep old name as alias so UI import still works
def fetch_pinnacle_odds(sport: str) -> Tuple[List[dict], str]:
    return fetch_book_odds(sport)


def fetch_game_scores(sport: str, days_from: int = 2) -> List[dict]:
    r = requests.get(
        f"{ODDS_BASE}/sports/{sport}/scores",
        params={"apiKey": ODDS_API_KEY, "daysFrom": days_from},
        timeout=15,
    )
    r.raise_for_status()
    return [g for g in r.json() if g.get("completed")]


def fetch_odds_events_list(sport: str) -> List[dict]:
    r = requests.get(f"{ODDS_BASE}/sports/{sport}/events", params={
        "apiKey": ODDS_API_KEY,
    }, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_player_prop_odds_event(sport: str, event_id: str, markets: str = None) -> dict:
    """
    Fetch player-prop odds for one event from all books.
    Falls back through books if earlier ones have no data.
    Costs 1 Odds API credit per call.
    """
    if markets is None:
        markets = "pitcher_strikeouts,batter_hits,batter_total_bases,batter_rbis"
    # Fetch ALL configured books (incl. DraftKings/FanDuel at weight 0). They
    # don't influence fair value, but they're needed to cross-check Pinnacle and
    # catch stale Pinnacle-only lines (the sanity-shadow guard). Adding books to
    # the same market/region call costs no extra Odds API credits.
    sharp_books = ",".join(BOOK_WEIGHTS.keys())
    r = requests.get(
        f"{ODDS_BASE}/sports/{sport}/events/{event_id}/odds",
        params={
            "apiKey":     ODDS_API_KEY,
            "bookmakers": sharp_books,
            "markets":    markets,
            "oddsFormat": "american",
        },
        timeout=15,
    )
    if r.status_code not in (404, 422):
        r.raise_for_status()
        data = r.json()
        remaining = r.headers.get("x-requests-remaining", "?")
        if any(bm.get("markets") for bm in data.get("bookmakers", [])):
            books_found = [bm["key"] for bm in data.get("bookmakers", []) if bm.get("markets")]
            print(f"    Props fetched {books_found}  |  credits left: {remaining}")
            return data
    return {}


# ── No-vig probability helpers ───────────────────────────────────────────────
def american_to_implied(odds: float) -> float:
    return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)


def no_vig_prob(odds_a: float, odds_b: float) -> Tuple[float, float]:
    """Return (p_a, p_b) after removing bookmaker vig — PROPORTIONAL method.

    Divides each side's raw implied probability by their sum, implicitly
    assuming the book spreads margin evenly across both sides. This is what
    LIVE pricing uses. It carries a favorite-longshot bias (overstates the
    longshot, understates the favorite; worst on lopsided lines) — the reason
    shin_devig_prob / power_devig_prob exist below.
    """
    pa = american_to_implied(odds_a)
    pb = american_to_implied(odds_b)
    total = pa + pb
    return pa / total, pb / total


# ── Alternative de-vig methods — NOT wired into live pricing ──────────────────
# Added 2026-07-14 for the forward-capture de-vig study. Live pricing still uses
# no_vig_prob (proportional). These exist so the OFFLINE backtest (and a possible
# future go-live) can recompute fair value under Shin / power de-vig from the raw
# odds we now capture per bet (see devig_inputs). Do NOT call these from the
# scan/pricing path — the whole point of the study is to keep proportional live
# and compare methods out-of-sample before changing anything.
def shin_devig_prob(odds_a: float, odds_b: float, _iters: int = 60) -> Tuple[float, float]:
    """Shin's method: models the overround as the book pricing against informed
    money (fraction z), solved by bisection so sum(p_i) = 1:
        p_i(z) = [sqrt(z^2 + 4(1-z)*pi_i^2/S) - z] / (2(1-z)),  S = sum(pi_i).
    Shrinks the longshot side and inflates the favorite vs proportional,
    correcting the favorite-longshot bias; ~identical to proportional at a
    coin-flip line. Falls back to proportional if there's no overround (S<=1)
    or the bisection can't bracket a root (a real failure mode on 2-outcome
    markets — Shin was built for multi-runner books; flagged, not hidden)."""
    pi_a = american_to_implied(odds_a)
    pi_b = american_to_implied(odds_b)
    S = pi_a + pi_b
    if S <= 1.0:
        return no_vig_prob(odds_a, odds_b)

    def p_i(pi: float, z: float) -> float:
        if z >= 1.0 - 1e-12:
            return (pi * pi) / S
        return (math.sqrt(z * z + 4.0 * (1.0 - z) * (pi * pi) / S) - z) / (2.0 * (1.0 - z))

    def f(z: float) -> float:
        return p_i(pi_a, z) + p_i(pi_b, z) - 1.0

    lo, hi = 0.0, 1.0 - 1e-9
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        return no_vig_prob(odds_a, odds_b)   # no bracketed root — bail to proportional
    for _ in range(_iters):
        mid = (lo + hi) / 2.0
        f_mid = f(mid)
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    pa, pb = p_i(pi_a, (lo + hi) / 2.0), p_i(pi_b, (lo + hi) / 2.0)
    total = pa + pb
    if total <= 0:
        return no_vig_prob(odds_a, odds_b)
    return pa / total, pb / total


def shin_z(odds_a: float, odds_b: float, _iters: int = 60) -> Optional[float]:
    """Return the fitted Shin z (informed-money fraction) for a two-way line, or
    None if it can't be bracketed. Exposed for the instability audit — z near 0
    means ~no correction (Shin ≈ proportional); z<0 or undefined is the 2-outcome
    failure mode we want to count rather than silently swallow."""
    pi_a = american_to_implied(odds_a)
    pi_b = american_to_implied(odds_b)
    S = pi_a + pi_b
    if S <= 1.0:
        return 0.0

    def p_i(pi: float, z: float) -> float:
        if z >= 1.0 - 1e-12:
            return (pi * pi) / S
        return (math.sqrt(z * z + 4.0 * (1.0 - z) * (pi * pi) / S) - z) / (2.0 * (1.0 - z))

    def f(z: float) -> float:
        return p_i(pi_a, z) + p_i(pi_b, z) - 1.0

    lo, hi = 0.0, 1.0 - 1e-9
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(_iters):
        mid = (lo + hi) / 2.0
        f_mid = f(mid)
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def power_devig_prob(odds_a: float, odds_b: float, _iters: int = 80) -> Tuple[float, float]:
    """Power (logarithmic) method: find exponent k with pi_a^k + pi_b^k = 1, then
    p_i = pi_i^k. Also de-biases lopsided lines (favorite-longshot) but has no
    'z' parameter and no sqrt/near-singular term, so it can't blow up the way
    Shin can on 2-outcome markets — the lower-numerical-risk comparison. Since
    each pi<1, the sum is monotone decreasing in k, so a unique k>=1 exists
    whenever S>1; solved by bisection."""
    pi_a = american_to_implied(odds_a)
    pi_b = american_to_implied(odds_b)
    S = pi_a + pi_b
    if S <= 1.0 or pi_a <= 0 or pi_b <= 0:
        return no_vig_prob(odds_a, odds_b)
    lo, hi = 1.0, 50.0
    for _ in range(_iters):
        k = (lo + hi) / 2.0
        if pi_a ** k + pi_b ** k > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2.0
    pa, pb = pi_a ** k, pi_b ** k
    total = pa + pb
    if total <= 0:
        return no_vig_prob(odds_a, odds_b)
    return pa / total, pb / total


def shin_devig_multi(implied: List[float], _iters: int = 90) -> List[float]:
    """N-way Shin de-vig. `implied` = per-outcome raw implied probabilities
    (sum > 1 by the vig). Returns fair probabilities summing to 1.

    This is the natural home of Shin's method: it was built for multi-runner
    books, and the 2-outcome case (shin_devig_prob above) is the awkward edge
    where it can fail to bracket. For 3-way soccer (home / draw / away) the
    solve is well-conditioned — f(0)=sqrt(S)-1 > 0 and f(1)=Σpi²/S < 1 for any
    realistic slate, so a root in z∈(0,1) always exists. Shin shrinks the
    longshot (the draw, usually) and inflates the favorite relative to the
    proportional de-vig, correcting the favorite-longshot bias that matters
    more on 3-way markets than on near-coin-flip 2-way lines.

    Falls back to proportional if there's no overround (S≤1), fewer than 2
    outcomes, a non-positive input, or z can't be bracketed (should not happen
    on a valid 3-way, but never fabricate a probability from a failed solve)."""
    S = sum(implied)
    n = len(implied)
    if S <= 1.0 or n < 2 or any(pi <= 0 for pi in implied):
        return [pi / S for pi in implied] if S > 0 else list(implied)

    def p_i(pi: float, z: float) -> float:
        if z >= 1.0 - 1e-12:
            return (pi * pi) / S
        return (math.sqrt(z * z + 4.0 * (1.0 - z) * (pi * pi) / S) - z) / (2.0 * (1.0 - z))

    def f(z: float) -> float:
        return sum(p_i(pi, z) for pi in implied) - 1.0

    lo, hi = 0.0, 1.0 - 1e-9
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        return [pi / S for pi in implied]   # no bracketed root — proportional fallback
    for _ in range(_iters):
        mid = (lo + hi) / 2.0
        f_mid = f(mid)
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    z = (lo + hi) / 2.0
    ps = [p_i(pi, z) for pi in implied]
    tot = sum(ps)
    return [p / tot for p in ps] if tot > 0 else [pi / S for pi in implied]


# ── Poisson total-goals helpers (soccer) ─────────────────────────────────────
# Soccer total goals are low-count and well-modelled by a Poisson on the match
# total (the sum of two independent team Poissons is itself Poisson). Pinnacle
# posts ONE total line per game; we de-vig it to an over-probability, fit the
# single λ that reproduces it, then price EVERY Kalshi over-x.5 rung off that λ.
# This mirrors the WNBA single-line extrapolation but with the discrete Poisson
# tail (goals are lumpy and low, so a Normal would misprice the tails). Caveat:
# real scorelines are mildly overdispersed/correlated vs pure Poisson, so far-
# out rungs are guarded (MLS_MAX_TOTAL_RUNGS) and MLS launches shadow-first.
def _poisson_cdf(k: int, lam: float) -> float:
    """P(X ≤ k) for X ~ Poisson(lam), computed by stable term recurrence."""
    if k < 0:
        return 0.0
    term = math.exp(-lam)
    cum = term
    for i in range(1, k + 1):
        term *= lam / i
        cum += term
    return cum


def poisson_over_prob(line: float, lam: float) -> float:
    """P(total goals > line) for a half-integer Kalshi line k.5 → P(X ≥ k+1)."""
    return 1.0 - _poisson_cdf(int(math.floor(line)), lam)


def fit_poisson_lambda(pin_line: float, pin_over_prob: float) -> Optional[float]:
    """Solve for the Poisson mean λ that reproduces Pinnacle's de-vigged over
    probability at its posted line. Handles both half-integer lines (no push)
    and integer lines (Pinnacle voids the exact-total push, so its over is the
    push-excluded conditional). Bisection on the monotonic-in-λ over function."""
    if not (0.0 < pin_over_prob < 1.0) or pin_line <= 0:
        return None
    is_integer = abs(pin_line - round(pin_line)) < 1e-6

    def model_over(lam: float) -> float:
        if is_integer:
            L = int(round(pin_line))
            pmf_L = math.exp(-lam) * lam ** L / math.factorial(L)
            return (1.0 - _poisson_cdf(L, lam)) / (1.0 - pmf_L) if pmf_L < 1.0 else 0.0
        return poisson_over_prob(pin_line, lam)

    lo, hi = 0.05, 8.0
    if model_over(lo) > pin_over_prob or model_over(hi) < pin_over_prob:
        return None   # target outside plausible λ range — don't extrapolate a bad fit
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if model_over(mid) < pin_over_prob:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def prob_to_american(p: float) -> Optional[int]:
    """Convert a decimal (no-vig) probability to American odds.
    e.g. 0.60 → -150,  0.40 → +150,  0.50 → +100
    Returns None for invalid inputs.
    """
    if p is None or p <= 0.0 or p >= 1.0:
        return None
    if p >= 0.5:
        return round(-p / (1.0 - p) * 100)
    else:
        return round((1.0 - p) / p * 100)


def _build_per_book_novig(books_detail: Dict[str, float], side: str) -> Dict[str, dict]:
    """
    Given books_detail {"pinnacle": 0.58, "draftkings": 0.57, ...} where each value
    is the no-vig probability for the YES/over side, return a per-book breakdown:
      {
        "pinnacle":   {"yes_prob": 0.58, "no_prob": 0.42,
                       "yes_american": -138, "no_american": +138},
        "draftkings": {...},
        ...
      }
    The `side` parameter ("YES"/"NO") is included for the caller's reference but
    all four fields are always populated.
    """
    result: Dict[str, dict] = {}
    for book, yes_p in books_detail.items():
        if not isinstance(yes_p, float):
            continue
        no_p = 1.0 - yes_p
        result[book] = {
            "yes_prob":     round(yes_p, 4),
            "no_prob":      round(no_p, 4),
            "yes_american": prob_to_american(yes_p),
            "no_american":  prob_to_american(no_p),
        }
    return result


def _validate_book_consensus(
    books_detail: Dict[str, float],
    side: str,
    k_side: float,
) -> Tuple[bool, str]:
    """
    Validate that Pinnacle AND at least one of DraftKings / FanDuel both
    indicate value on the same side before we count a bet as +EV.

    books_detail: {book_key: yes_side_no_vig_prob}  (canonical YES probability)
    side:   "YES" or "NO" — the proposed bet direction
    k_side: Kalshi ask price for the bet side (tradeable cost)

    A book "indicates value on a side" when its no-vig probability for that
    side exceeds the Kalshi price you would pay to buy it.

    Returns (is_valid: bool, reason: str).
      is_valid=True  → reason is ""
      is_valid=False → reason explains why the bet was rejected
    """
    def _side_prob(book: str) -> Optional[float]:
        """Return this book's no-vig prob for the bet side."""
        p_yes = books_detail.get(book)
        if p_yes is None:
            return None
        return p_yes if side == "YES" else 1.0 - p_yes

    pin_p = _side_prob("pinnacle")
    dk_p  = _side_prob("draftkings")
    fd_p  = _side_prob("fanduel")

    opp_side = "NO" if side == "YES" else "YES"

    # ── 1. Pinnacle must be present and must confirm the bet side ─────────
    if pin_p is None:
        return False, "Pinnacle odds not available — cannot validate direction"

    if pin_p <= k_side:
        return (
            False,
            f"Pinnacle does not confirm {side} "
            f"(PIN {pin_p:.1%} ≤ Kalshi {k_side:.1%}; "
            f"Pinnacle implies {opp_side} has value instead)",
        )

    # ── 2. Classify each retail book as confirming, neutral, or disagreeing ─
    #   confirm  : book's no-vig prob for bet side > Kalshi price  → sees +EV on our side
    #   disagree : book's no-vig prob for bet side < Kalshi price  → sees +EV on opposite side
    #   neutral  : probability ≈ Kalshi price (within 0.5pp rounding)
    confirm_books:  List[str] = []
    disagree_books: List[str] = []

    retail = [("draftkings", dk_p, "DraftKings"), ("fanduel", fd_p, "FanDuel")]
    for bkey, bp, bname in retail:
        if bp is None:
            continue
        if bp > k_side:
            confirm_books.append(bname)
        elif bp < k_side:
            disagree_books.append(bname)
        # bp == k_side → neutral (skip)

    # ── 3. Reject if retail books actively disagree and none confirm ──────
    if disagree_books and not confirm_books:
        parts = []
        for bkey, bp, bname in retail:
            if bp is not None:
                parts.append(f"{bname}={bp:.1%}")
        return (
            False,
            f"Books disagree on direction: Pinnacle confirms {side} "
            f"but {' and '.join(disagree_books)} indicate {opp_side} "
            f"({', '.join(parts)}; Kalshi={k_side:.1%})",
        )

    # ── 4. Retail confirmation (DK/FD) — optional when not fetched ──────────
    # If retail books are available, at least one must confirm.
    # If none are available (Pinnacle-only mode), Pinnacle alone is sufficient.
    available_retail = [(bname, bp) for _, bp, bname in retail if bp is not None]
    if available_retail and not confirm_books:
        detail = ", ".join(f"{n}={p:.1%}" for n, p in available_retail)
        return (
            False,
            f"Neither DraftKings nor FanDuel confirms value on {side} "
            f"({detail} ≤ Kalshi {k_side:.1%})",
        )

    # ── 5. Valid — build a short confirmation string for the edge payload ─
    confirmed_by = ["Pinnacle"] + confirm_books
    return True, f"Confirmed by {' + '.join(confirmed_by)}"


def _weighted_consensus(probs_by_book: Dict[str, float]) -> Tuple[float, List[str]]:
    """
    Compute a weighted-average probability from available books.
    Renormalises weights if some books are missing.
    Returns (consensus_prob, list_of_books_used).
    """
    total_w = sum(BOOK_WEIGHTS[b] for b in probs_by_book if b in BOOK_WEIGHTS)
    if total_w == 0:
        return 0.0, []
    consensus = sum(BOOK_WEIGHTS[b] * p for b, p in probs_by_book.items()
                    if b in BOOK_WEIGHTS) / total_w
    return consensus, sorted(probs_by_book.keys())


# ── Consensus game index ─────────────────────────────────────────────────────
def build_consensus_game_index(
    games: List[dict],
    total_range: Tuple[float, float] = (0, 9999),
    spread_limit: float = 99,
) -> Dict[str, dict]:
    """
    Build { normalised_team_name: game_info } with weighted-consensus probabilities.

    game_info["spread"][team] = (consensus_prob, anchor_point, per_book_detail)
      anchor_point: Pinnacle's spread point if available, else first available book's
      per_book_detail: {"pinnacle": 0.58, "draftkings": 0.57, ...}

    game_info["total"] = {
        "over_point":  float,          # Pinnacle line (or best fallback)
        "over_prob":   float,          # weighted consensus
        "under_prob":  float,
        "books_used":  [str, ...],
        "per_book":    {"pinnacle": {"over_prob": ..., "under_prob": ..., "over_point": ...}, ...}
    }
    """
    index: Dict[str, dict] = {}

    for game in games:
        home, away = game["home_team"], game["away_team"]
        info: dict = {
            "home": home, "away": away,
            "h2h": {}, "spread": {}, "total": {},
        }

        # ── Collect per-book data ────────────────────────────────────────────
        bk_spread: Dict[str, Dict[str, Tuple[float, float]]] = {}
        # bk_spread[book][team] = (no_vig_prob, spread_point)

        bk_total: Dict[str, dict] = {}
        # bk_total[book] = {over_point, over_prob, under_prob}  (most-central line)

        pin_total_lines: Dict[float, dict] = {}
        # pin_total_lines[line] = {over_prob, under_prob}  — ALL Pinnacle lines stored

        pin_spread_lines: Dict[str, Dict[float, float]] = {}
        # pin_spread_lines[team][abs_spread] = no_vig_cover_prob
        # Populated from alternate_spreads — every Pinnacle line where that
        # team is listed at a negative spread (i.e. favored to cover by that margin).
        # Used for direct-match spread fair value — no Gaussian inference.

        pin_spread_points: Dict[str, float] = {}  # team → Pinnacle main spread point
        pin_total_point: Optional[float] = None

        for bm in game.get("bookmakers", []):
            bkey = bm["key"]
            if bkey not in BOOK_WEIGHTS:
                continue

            for mkt in bm.get("markets", []):
                mtype = mkt.get("key")
                outs  = mkt.get("outcomes", [])

                # ── h2h (moneyline) ────────────────────────────────────────
                if mtype == "h2h" and len(outs) == 2:
                    pa, pb = no_vig_prob(outs[0]["price"], outs[1]["price"])
                    # store per-book h2h probs but don't build consensus here
                    # (h2h isn't used directly for fair-value calc in scan_sport)
                    info["h2h"].setdefault(outs[0]["name"], {})[bkey] = pa
                    info["h2h"].setdefault(outs[1]["name"], {})[bkey] = pb

                # ── spreads + alternate_spreads ────────────────────────────
                # alternate_spreads returns every Pinnacle line (-0.5, -1.5,
                # -2.5 …) so we can match the Kalshi threshold directly.
                # For each outcome where point < 0 (team favored to cover by
                # that margin), store the no-vig cover probability directly —
                # no Gaussian inference needed.
                elif mtype in ("spreads", "alternate_spreads") and len(outs) == 2:
                    pt0 = float(outs[0].get("point") or 0)
                    if abs(pt0) > spread_limit:
                        continue
                    pa, pb = no_vig_prob(outs[0]["price"], outs[1]["price"])
                    probs = [(outs[0]["name"], pa, float(outs[0].get("point", 0))),
                             (outs[1]["name"], pb, float(outs[1].get("point", 0)))]
                    # Main spread: update consensus anchor
                    if mtype == "spreads":
                        if bkey not in bk_spread:
                            bk_spread[bkey] = {}
                        bk_spread[bkey][outs[0]["name"]] = (pa, float(outs[0].get("point", 0)))
                        bk_spread[bkey][outs[1]["name"]] = (pb, float(outs[1].get("point", 0)))
                        if bkey == "pinnacle":
                            for o in outs:
                                pin_spread_points[o["name"]] = float(o.get("point", 0))
                    # All spread lines (main + alternate): store Pinnacle cover probs
                    # Only store entries where point < 0, meaning this team is
                    # posted as the "favorite" to cover by that margin.
                    if bkey == "pinnacle":
                        for tname, cov_prob, pt in probs:
                            if pt < 0:
                                abs_pt = abs(pt)
                                if tname not in pin_spread_lines:
                                    pin_spread_lines[tname] = {}
                                pin_spread_lines[tname][abs_pt] = cov_prob

                # ── totals + alternate_totals ──────────────────────────────
                # alternate_totals returns every Pinnacle line (7.5, 8.5, 9.5,
                # 10.5 …) so we can match the Kalshi threshold directly.
                # Pinnacle's alternate prices already embed all venue/weather/
                # matchup effects — no Gaussian inference needed.
                elif mtype in ("totals", "alternate_totals") and len(outs) >= 2:
                    over  = next((o for o in outs if o["name"] == "Over"),  None)
                    under = next((o for o in outs if o["name"] == "Under"), None)
                    if not (over and under):
                        continue
                    pt = float(over.get("point") or 0)
                    if not (total_range[0] <= pt <= total_range[1]):
                        continue
                    po, pu = no_vig_prob(over["price"], under["price"])
                    # For the main total line: track the most-central line per book
                    # (alternate lines don't update the consensus anchor)
                    if mtype == "totals":
                        mid_range = (total_range[0] + total_range[1]) / 2
                        existing  = bk_total.get(bkey, {}).get("over_point")
                        if existing is None or abs(pt - mid_range) < abs(existing - mid_range):
                            bk_total[bkey] = {"over_point": pt, "over_prob": po, "under_prob": pu}
                        if bkey == "pinnacle":
                            pin_total_point = pt
                    # Store ALL Pinnacle lines (main + alternate) for direct matching
                    if bkey == "pinnacle":
                        pin_total_lines[pt] = {"over_prob": po, "under_prob": pu}

        # ── Build consensus spread ───────────────────────────────────────────
        all_spread_teams: set = set()
        for bd in bk_spread.values():
            all_spread_teams.update(bd.keys())

        for team in all_spread_teams:
            per_book_probs: Dict[str, float] = {}
            anchor_point: Optional[float] = pin_spread_points.get(team)

            for bkey, bd in bk_spread.items():
                if team in bd:
                    prob, pt = bd[team]
                    per_book_probs[bkey] = prob
                    if anchor_point is None:
                        anchor_point = pt  # fallback to first available book

            if not per_book_probs or anchor_point is None:
                continue

            consensus_p, books_used = _weighted_consensus(per_book_probs)
            # Safety: require at least Pinnacle alone, or 2+ books, to trust the line
            has_pinnacle = "pinnacle" in per_book_probs
            if not has_pinnacle and len(per_book_probs) < 2:
                continue  # single non-Pinnacle book — not reliable enough

            info["spread"][team] = (consensus_p, anchor_point, per_book_probs)

        # ── Build consensus total ─────────────────────────────────────────────
        if bk_total:
            over_probs:  Dict[str, float] = {bk: td["over_prob"]  for bk, td in bk_total.items()}
            under_probs: Dict[str, float] = {bk: td["under_prob"] for bk, td in bk_total.items()}

            cons_over, books_used = _weighted_consensus(over_probs)
            cons_under, _         = _weighted_consensus(under_probs)

            # Renormalise (should already sum to ~1)
            s = cons_over + cons_under
            if s > 0:
                cons_over  /= s
                cons_under /= s

            # Anchor point: Pinnacle's line, or closest available
            if pin_total_point is not None:
                anchor_pt = pin_total_point
            else:
                anchor_pt = next(iter(bk_total.values()))["over_point"]

            has_pinnacle = "pinnacle" in bk_total
            if has_pinnacle or len(bk_total) >= 2:
                info["total"] = {
                    "over_point":  anchor_pt,
                    "over_prob":   cons_over,
                    "under_prob":  cons_under,
                    "books_used":  books_used,
                    "per_book":    bk_total,
                    # All Pinnacle lines keyed by point value — used in scan_sport
                    # to prefer exact-match over equivalence-rule when possible
                    "pin_lines":   pin_total_lines,
                }

        # Store all Pinnacle alternate spread lines for direct matching in scan_sport
        if pin_spread_lines:
            info["spread_pin_lines"] = pin_spread_lines

        # ── Build consensus moneyline (h2h) ──────────────────────────────────────
        # Restructure info["h2h"] from {team: {book: prob}} to
        # {team: (consensus_prob, per_book_probs)} — same shape as spread entries.
        h2h_raw = info.get("h2h", {})
        info["h2h"] = {}
        for team, per_book in h2h_raw.items():
            has_pinnacle = "pinnacle" in per_book
            if not has_pinnacle and len(per_book) < 2:
                continue
            consensus_p, _ = _weighted_consensus(per_book)
            info["h2h"][team] = (consensus_p, per_book)

        # Skip games that have already commenced
        ct_str = game.get("commence_time", "")
        game_date = ""
        if ct_str:
            try:
                ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                if ct < datetime.now(timezone.utc):
                    continue
                # Index by ET date, not UTC date.  Kalshi tickers encode dates in ET,
                # so the index key must also use ET to prevent consecutive-day collisions.
                # Example: LAD@SD 9:40pm ET May 19 = 1:40am UTC May 20 → without this
                # fix, it's indexed as 2026-05-20 and matches Kalshi's May 20 ticker.
                et_offset = timedelta(hours=(4 if 4 <= ct.month <= 10 else 5))
                game_date = (ct - et_offset).strftime("%Y-%m-%d")
            except ValueError:
                pass

        # Key by team+date to handle doubleheaders / same-day games.
        # Also store a bare-team fallback for any code that doesn't have the date.
        info["_game_date"] = game_date   # embed date so _find_game can cross-check
        # Raw Pinnacle commence_time (ISO, UTC) — the only reliable per-game start
        # time for sports whose Kalshi ticker omits it (NBA/WNBA; MLB's ticker
        # already encodes start time via _parse_ticker_start_time and doesn't need
        # this). Was computed above (ct_str) just to filter/date-key, then
        # discarded — now persisted so bet-creation can use it as a game_time
        # fallback instead of leaving game_time=None. See WNBA CLV fix 2026-07-17.
        info["commence_time"] = ct_str or None
        for team in [home, away]:
            nteam = _norm(team)
            if game_date:
                index[f"{nteam}_{game_date}"] = info
            # Bare-team fallback: keep the entry with more spread data (richer)
            existing = index.get(nteam, {})
            if len(info["spread"]) >= len(existing.get("spread", {})):
                index[nteam] = info

    return index


# Keep old name as alias
def build_game_index(
    games: List[dict],
    total_range: Tuple[float, float] = (0, 9999),
    spread_limit: float = 99,
) -> Dict[str, dict]:
    return build_consensus_game_index(games, total_range, spread_limit)


# ── Correlation control ───────────────────────────────────────────────────────
def _apply_correlation_control(
    edges: List[dict],
    max_per_group: int = MAX_BETS_PER_GROUP,
) -> List[dict]:
    """
    Limit to max_per_group bets per (matchup, mkt_type) group.

    Moneyline is hard-capped at 1 regardless of max_per_group. A spread/total
    group can legitimately hold 2 different Kalshi thresholds on the same
    line — but moneyline's two "different" edges (Team A YES, Team B YES) are
    mutually exclusive outcomes of the SAME game on two SEPARATE ticker
    markets (no shared-ticker relationship for the existing same-ticker
    reversal guard to catch). Funding both is a guaranteed-bad double bet, not
    a hedge. Added 2026-07-14 alongside the MLB moneyline (KXMLBGAME) wiring.

    Input should already be sorted by adj. edge descending.
    """
    group_counts: Dict[tuple, int] = {}
    result = []
    for e in edges:
        mtype = e.get("mkt_type", "")
        key = (e.get("matchup", ""), mtype)
        cap = 1 if mtype == "moneyline" else max_per_group
        cnt = group_counts.get(key, 0)
        if cnt < cap:
            group_counts[key] = cnt + 1
            result.append(e)
    return result


# ── Pre-execution EV recheck ─────────────────────────────────────────────────
def recheck_ev(ticker: str, fair_prob: float, side: str) -> Optional[dict]:
    """
    Re-fetch the latest Kalshi price for `ticker` and recompute adjusted EV.

    Returns a dict with updated metrics if the bet still qualifies, else None.
    Discard the bet if:
      - price is unavailable
      - adjusted EV fell below EDGE_THRESHOLD
    """
    try:
        mkt_data = kalshi_get(f"/markets/{ticker}")
        mkt = mkt_data.get("market", mkt_data)
        prices = kalshi_prices(mkt)
    except Exception as e:
        print(f"  recheck_ev ERROR [{ticker}]: {e}")
        return None

    if prices is None:
        print(f"  recheck_ev: no price for {ticker} — discarding")
        return None

    yes_bid, yes_ask = prices

    if side == "YES":
        raw_edge = fair_prob - yes_ask
        k_price  = yes_ask
        fee_cost = KALSHI_FEE_RATE * fair_prob * (1 - yes_ask)
    else:
        raw_edge = (1 - fair_prob) - (1 - yes_bid)
        k_price  = 1 - yes_bid
        fee_cost = KALSHI_FEE_RATE * (1 - fair_prob) * yes_bid

    adj_edge = (raw_edge - fee_cost) * (1 - EV_HAIRCUT)

    if adj_edge < EDGE_THRESHOLD or adj_edge > MAX_EDGE or k_price < MIN_KALSHI_PRICE:
        print(f"  recheck_ev: {ticker} {side} — edge {adj_edge:.1%} out of range [{EDGE_THRESHOLD:.0%}–{MAX_EDGE:.0%}] or price {k_price:.2f} below floor, discarding")
        return None

    return {
        "ticker":     ticker,
        "side":       side,
        "k_price":    round(k_price, 4),
        "raw_edge":   round(raw_edge, 4),
        "adj_edge":   round(adj_edge, 4),
        "rechecked_at": datetime.now(timezone.utc).isoformat(),
    }



def validate_bet(edge: dict, max_age_seconds: int = 600) -> dict:
    """
    Pre-execution validation — call this immediately before placing any bet.

    Re-fetches both the current Kalshi price AND Pinnacle's latest odds, then
    reports exactly what has changed since the edge was first flagged and
    whether it is still valid to bet on.

    Args:
        edge:            The edge dict produced by scan_sport() / stored in _bets
        max_age_seconds: Reject if flagged more than this many seconds ago (default 10 min)

    Returns a dict with:
        valid          bool   — True only if safe to bet right now
        reason         str    — Human-readable verdict
        kalshi_moved   float  — How much Kalshi price changed (pp), + = moved against us
        fair_moved     float  — How much fair value changed (pp), + = edge improved
        edge_now       float  — Current adjusted edge (post-haircut), None if unavailable
        edge_was       float  — Edge at flag time
        kalshi_now     float  — Current Kalshi ask/bid price for the side
        kalshi_was     float  — Kalshi price at flag time
        fair_now       float  — Current fair probability (re-fetched from Pinnacle)
        fair_was       float  — Fair probability at flag time
        age_seconds    int    — Seconds since edge was flagged
        staleness_ok   bool   — False if edge is older than max_age_seconds
    """
    result: dict = {
        "valid": False,
        "reason": "",
        "kalshi_moved": None,
        "fair_moved": None,
        "edge_now": None,
        "edge_was": round(edge.get("edge", edge.get("adj_edge", 0)) * 100, 2),
        "kalshi_now": None,
        "kalshi_was": round(edge.get("kalshi", edge.get("kalshi_price", 0)) * 100, 1),
        "fair_now": None,
        "fair_was": round(edge.get("fair", 0) * 100, 1),
        "age_seconds": None,
        "staleness_ok": True,
    }

    ticker  = edge.get("ticker", "")
    side    = edge.get("side", "YES")
    fair_was = edge.get("fair", 0)
    edge_was = edge.get("edge", edge.get("adj_edge", 0))

    # ── 1. Staleness check ────────────────────────────────────────────────────
    flagged_at = edge.get("flagged_at") or edge.get("kalshi_price_ts", "")
    if flagged_at:
        try:
            flag_dt = datetime.fromisoformat(flagged_at)
            age = (datetime.now(timezone.utc) - flag_dt).total_seconds()
            result["age_seconds"] = int(age)
            if age > max_age_seconds:
                result["staleness_ok"] = False
                result["reason"] = (
                    f"STALE — flagged {int(age)}s ago (limit {max_age_seconds}s). "
                    f"Re-scan before betting."
                )
                return result
        except ValueError:
            pass

    # ── 2. Re-fetch current Kalshi price ──────────────────────────────────────
    try:
        mkt_data = kalshi_get(f"/markets/{ticker}")
        mkt = mkt_data.get("market", mkt_data)

        # Check if market already resolved
        if mkt.get("result"):
            result["reason"] = f"MARKET RESOLVED — result: {mkt['result'].upper()}"
            return result

        # Check market is still active
        if mkt.get("status") not in ("active", "open"):
            result["reason"] = f"MARKET NOT ACTIVE — status: {mkt.get('status', '?')}"
            return result

        prices = kalshi_prices(mkt)
        if prices is None:
            result["reason"] = "NO KALSHI PRICE — market may be illiquid"
            return result

        yes_bid, yes_ask = prices
        kalshi_now = yes_ask if side == "YES" else (1 - yes_bid)
        result["kalshi_now"] = round(kalshi_now * 100, 1)

        kalshi_was_dec = edge.get("kalshi", edge.get("kalshi_price", 0))
        kalshi_moved_pp = round((kalshi_now - kalshi_was_dec) * 100, 1)
        result["kalshi_moved"] = kalshi_moved_pp  # positive = price rose (worse for YES bets)

    except Exception as exc:
        result["reason"] = f"KALSHI API ERROR — {exc}"
        return result

    # ── 3. Re-fetch Pinnacle fair value ───────────────────────────────────────
    try:
        odds_sport  = edge.get("odds_sport", "")
        mkt_type    = edge.get("mkt_type", "")
        matchup     = edge.get("matchup", "")

        # Determine sport from ticker prefix if not stored on edge
        if not odds_sport:
            t = ticker.upper()
            if "KXMLB" in t:
                odds_sport = "baseball_mlb"
            elif "KXNBA" in t:
                odds_sport = "basketball_nba"
        fair_now = None
        if odds_sport:
            games, _ = fetch_book_odds(odds_sport)
            # Find this specific game
            away_raw = matchup.split(" @ ")[0].strip() if " @ " in matchup else ""
            home_raw = matchup.split(" @ ")[1].strip() if " @ " in matchup else ""
            game_date = _parse_ticker_date(ticker)
            index = build_consensus_game_index(games)
            game_info = _find_game(away_raw, home_raw, index, game_date)

            if game_info and mkt_type == "total":
                total_info = game_info.get("total", {})
                threshold  = edge.get("threshold") or edge.get("pin_line")
                # Try to extract threshold from title if not stored
                if threshold is None:
                    import re as _re
                    m = _re.search(r">(\d+\.?\d*)", edge.get("title", ""))
                    if m:
                        threshold = float(m.group(1))
                if threshold is not None and total_info:
                    pin_lines = total_info.get("pin_lines", {})
                    for pt, probs in pin_lines.items():
                        if abs(pt - threshold) <= 0.25:
                            fair_now = probs["over_prob"] if side == "YES" else probs["under_prob"]
                            break
                    if fair_now is None:
                        fair_now = total_info.get("over_prob") if side == "YES" else total_info.get("under_prob")

            elif game_info and mkt_type == "moneyline":
                team_name = edge.get("team_name", "")
                h2h = game_info.get("h2h", {})
                for k, v in h2h.items():
                    if _norm(k) == _norm(team_name) or _norm(team_name) in _norm(k):
                        fair_now = v[0]
                        break

        # Fall back to stored fair if we couldn't re-fetch
        if fair_now is None:
            fair_now = fair_was

        result["fair_now"] = round(fair_now * 100, 1)
        fair_moved_pp = round((fair_now - fair_was) * 100, 1)
        result["fair_moved"] = fair_moved_pp

        # ── Bad-data guard: reject Pinnacle re-fetch if move > 15pp ──────────
        # A move larger than 15 percentage points between scans almost certainly
        # means the index matched the wrong game (same city, different date or
        # opponent) rather than a genuine market shift.  Example: Rockies fair
        # was 32.6% at entry; re-fetch returns 53% because a different COL game
        # was matched.  We discard the bad pull, keep the stored fair value, and
        # log every rejection so it's visible in Railway logs.
        _PIN_RESCAN_MAX_MOVE_PP = 15.0
        if fair_now != fair_was and abs(fair_moved_pp) > _PIN_RESCAN_MAX_MOVE_PP:
            print(
                f"  [pin-rescan REJECTED] {ticker} — Pinnacle line moved "
                f"{fair_moved_pp:+.1f}pp "
                f"({round(fair_was * 100, 1)}% → {round(fair_now * 100, 1)}%) "
                f"exceeds {_PIN_RESCAN_MAX_MOVE_PP:.0f}pp threshold — "
                f"likely wrong game matched in index. Keeping stored fair value."
            )
            fair_now              = fair_was
            result["fair_now"]    = round(fair_now * 100, 1)
            result["fair_moved"]  = 0.0
            fair_moved_pp         = 0.0

    except Exception as exc:
        # Non-fatal — use stored fair value if Pinnacle re-fetch fails
        fair_now = fair_was
        result["fair_now"] = round(fair_now * 100, 1)
        result["fair_moved"] = 0.0

    # ── 4. Recompute edge (fee-adjusted then haircut) ─────────────────────────
    if side == "YES":
        raw_now  = fair_now - kalshi_now
        fee_now  = KALSHI_FEE_RATE * fair_now * (1 - kalshi_now)
    else:
        raw_now  = (1 - fair_now) - (1 - kalshi_now)
        fee_now  = KALSHI_FEE_RATE * (1 - fair_now) * kalshi_now

    adj_now = (raw_now - fee_now) * (1 - EV_HAIRCUT)
    result["edge_now"]     = round(adj_now * 100, 2)
    result["raw_edge_now"] = round(raw_now * 100, 2)   # gross gap before fees

    # ── 5. Verdict ────────────────────────────────────────────────────────────
    issues = []

    if adj_now < EDGE_THRESHOLD:
        result["reason"] = (
            f"EDGE GONE — was +{edge_was*100:.1f}% adj, now +{adj_now*100:.1f}% "
            f"(Kalshi moved {result['kalshi_moved']:+.1f}pp, "
            f"fair moved {result['fair_moved']:+.1f}pp)"
        )
        return result

    if adj_now > MAX_EDGE:
        result["reason"] = (
            f"EDGE TOO LARGE ({adj_now*100:.1f}%) — possible data error, do not bet"
        )
        return result

    if kalshi_now < MIN_KALSHI_PRICE:
        result["reason"] = f"PRICE TOO LOW — {kalshi_now*100:.0f}¢ below floor"
        return result

    # Flag significant line movement even if edge survives
    if abs(result["kalshi_moved"]) >= 3:
        issues.append(f"Kalshi moved {result['kalshi_moved']:+.1f}pp")
    if abs(result["fair_moved"]) >= 2:
        issues.append(f"fair value moved {result['fair_moved']:+.1f}pp")

    edge_shrink = edge_was - adj_now
    if edge_shrink > 0.02:  # edge shrank by >2pp
        issues.append(f"edge shrank {edge_shrink*100:.1f}pp (was +{edge_was*100:.1f}%, now +{adj_now*100:.1f}%)")

    age_str = f"{result['age_seconds']}s old" if result["age_seconds"] is not None else "age unknown"

    if issues:
        result["valid"] = True
        result["reason"] = (
            f"VALID WITH CAUTION ({age_str}) — " + "; ".join(issues) +
            f". Edge still +{adj_now*100:.1f}% adj."
        )
    else:
        result["valid"] = True
        result["reason"] = (
            f"VALID ({age_str}) — edge confirmed +{adj_now*100:.1f}% adj. "
            f"Kalshi {result['kalshi_now']}¢, fair {result['fair_now']}¢. "
            f"No significant line movement."
        )

    return result


# ── Normal distribution ───────────────────────────────────────────────────────
def norm_cdf(x: float, mu: float = 0, sigma: float = 1) -> float:
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


# ── Poisson model (player props) ──────────────────────────────────────────────
def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0 or k < 0:
        return 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def poisson_cdf(n: int, lam: float) -> float:
    return sum(poisson_pmf(i, lam) for i in range(max(0, int(n)) + 1))


def poisson_lambda_from_line(line: float, over_prob: float) -> Optional[float]:
    """
    Invert: find λ such that P(X > floor(line)) = over_prob.
    Sanity guards: over_prob ∈ (0.05, 0.95), λ > 0.1.
    """
    if not (0.05 < over_prob < 0.95):
        return None
    k = int(line)
    target_cdf = 1.0 - over_prob
    if not (0.05 < target_cdf < 0.95):
        return None
    lo, hi = 0.001, max(line * 6 + 5, 20)
    for _ in range(80):
        mid = (lo + hi) / 2
        if poisson_cdf(k, mid) > target_cdf:
            lo = mid
        else:
            hi = mid
    lam = (lo + hi) / 2
    if lam <= 0.1:
        return None
    return lam


# ── Normal model for WNBA prop line-extrapolation ────────────────────────────
# Fits a Normal(mu, cv*mu) to a single Pinnacle anchor (over_prob at anchor_line)
# and evaluates P(stat > kalshi_thresh) at a nearby rung. Used ONLY for the WNBA
# self-averaging stats (see WNBA_EXTRAP_* config) — Poisson is too thin-tailed
# for points. Parameter-light on purpose (one anchor + a cv prior).
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF by bisection (no scipy; matches codebase style)."""
    lo, hi = -8.0, 8.0
    for _ in range(64):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def normal_prop_fair_over(anchor_line: float, anchor_over_prob: float,
                          kalshi_thresh: float, cv: float) -> Optional[float]:
    """P(stat > kalshi_thresh) from a Normal fit to one Pinnacle line.

    Solve mu from P(X > anchor_line) = anchor_over_prob with sigma = cv*mu:
        (anchor_line - mu) / (cv*mu) = -z,  z = Phi^-1(anchor_over_prob)
        => mu = anchor_line / (1 - z*cv)
    then return 1 - Phi((kalshi_thresh - mu) / (cv*mu)). None on degenerate input.
    """
    p = anchor_over_prob
    if not (0.02 < p < 0.98) or cv <= 0 or anchor_line <= 0:
        return None
    z = _norm_ppf(p)
    denom = 1.0 - z * cv
    if denom <= 0:
        return None
    mu = anchor_line / denom
    sigma = cv * mu
    if mu <= 0 or sigma <= 0:
        return None
    fair = 1.0 - _norm_cdf((kalshi_thresh - mu) / sigma)
    return min(max(fair, 0.0), 1.0)


# ── Negative Binomial model (diagnostic — Total Bases overdispersion check) ──
# TB is a compound stat (1B/2B/3B/HR summed across at-bats), not a single count
# process — the live Poisson model implicitly assumes variance == mean, which
# a compound-hit-type process is unlikely to satisfy. The 2026-07-14 reliability
# curve found overconfidence growing with the model's own claimed probability
# (+4.2pp / +15.6pp / +18.3pp across increasing fair bands) — exactly the
# signature of an underestimated tail from a too-thin (Poisson) distribution.
#
# Pinnacle posts multiple lines per TB player (e.g. over 0.5 and over 1.5).
# The live model independently Poisson-fits a lambda from each line; if the
# true distribution were Poisson, those lambdas would agree. The functions
# below use a SECOND observed line as an extra degree of freedom to fit a
# dispersion parameter directly from market data, instead of assuming
# Poisson's variance=mean. This is diagnostic-only (see fit_neg_binom_two_point
# callers) — it does not change live pricing, edges, or staking.
def neg_binom_pmf(k: int, mu: float, r: float) -> float:
    """PMF of the Negative Binomial parametrized by mean (mu) and dispersion
    (r). As r -> infinity this converges to Poisson(mu); smaller r means a
    fatter (overdispersed) right tail relative to Poisson at the same mean."""
    if mu <= 0 or r <= 0 or k < 0:
        return 0.0
    p = r / (r + mu)
    log_pmf = (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
               + r * math.log(p) + k * math.log(1.0 - p))
    return math.exp(log_pmf)


def neg_binom_cdf(n: int, mu: float, r: float) -> float:
    return sum(neg_binom_pmf(i, mu, r) for i in range(max(0, int(n)) + 1))


def _nb_mu_from_line(line: float, over_prob: float, r: float) -> Optional[float]:
    """For a fixed dispersion r, invert: find mu such that P(X > floor(line))
    = over_prob. Mirrors poisson_lambda_from_line's bisection with r held
    constant instead of assuming the Poisson r -> infinity limit."""
    if not (0.05 < over_prob < 0.95):
        return None
    k = int(line)
    target_cdf = 1.0 - over_prob
    lo, hi = 0.001, max(line * 8 + 10, 30)
    for _ in range(80):
        mid = (lo + hi) / 2
        if neg_binom_cdf(k, mid, r) > target_cdf:
            lo = mid
        else:
            hi = mid
    mu = (lo + hi) / 2
    return mu if mu > 0.05 else None


def fit_neg_binom_two_point(
    line1: float, over_prob1: float, line2: float, over_prob2: float,
) -> Optional[Tuple[float, float]]:
    """
    Fit Negative Binomial (mu, r) from two independently-posted lines for the
    same player/prop (e.g. Pinnacle's 0.5 and 1.5 total-base lines).

    For a candidate dispersion r, solve for the mu that exactly matches the
    lower line (inner bisection, same monotonicity as poisson_lambda_from_line
    for fixed r), then compare the resulting fit's implied probability at the
    higher line against its actual market value. The implied-hi-prob curve as
    a function of r is NOT globally monotonic (verified empirically — it can
    have an interior maximum before decaying toward the Poisson limit at large
    r, rather than decreasing monotonically all the way to the max-overdispersion
    end), so this scans a log-spaced grid of r from the Poisson-like end
    inward to find the first sign change (bracketing the least-overdispersed
    root when more than one exists), then bisects within that bracket.

    Returns None on degenerate/inconsistent inputs (out-of-range over_probs,
    a higher line whose over_prob isn't lower than the lower line's, or no
    sign change found on the grid) — callers must fall back to the live
    Poisson pricing in that case.
    """
    if line2 == line1:
        return None
    lo_line, lo_p, hi_line, hi_p = (
        (line1, over_prob1, line2, over_prob2) if line1 < line2
        else (line2, over_prob2, line1, over_prob1)
    )
    if not (0.05 < lo_p < 0.95) or not (0.05 < hi_p < 0.95):
        return None
    if hi_p >= lo_p:
        return None   # higher line must have lower over-prob for a sane distribution

    def _implied_hi_prob(r: float) -> Optional[float]:
        mu = _nb_mu_from_line(lo_line, lo_p, r)
        if mu is None:
            return None
        return 1.0 - neg_binom_cdf(int(hi_line), mu, r)

    R_POISSON_LIKE  = 500.0   # large r ~ Poisson limit
    R_MAX_OVERDISP  = 0.02    # small r ~ heaviest searched overdispersion

    n_grid = 60
    log_hi, log_lo = math.log10(R_POISSON_LIKE), math.log10(R_MAX_OVERDISP)
    r_grid = [10 ** (log_hi + t * (log_lo - log_hi) / n_grid) for t in range(n_grid + 1)]

    bracket = None
    prev_r, prev_val = None, None
    for r in r_grid:
        val = _implied_hi_prob(r)
        if val is None:
            prev_r, prev_val = r, val
            continue
        if prev_val is not None and (prev_val - hi_p) * (val - hi_p) <= 0:
            bracket = (prev_r, r, prev_val)
            break
        prev_r, prev_val = r, val
    if bracket is None:
        return None   # no sign change on the grid -- target not reachable this way

    r_lo, r_hi, val_lo = bracket
    for _ in range(50):
        r_mid = (r_lo + r_hi) / 2
        val_mid = _implied_hi_prob(r_mid)
        if val_mid is None:
            return None
        if (val_lo - hi_p) * (val_mid - hi_p) <= 0:
            r_hi = r_mid
        else:
            r_lo, val_lo = r_mid, val_mid
    r_fit  = (r_lo + r_hi) / 2
    mu_fit = _nb_mu_from_line(lo_line, lo_p, r_fit)
    if mu_fit is None:
        return None

    # Sanity gate: when the market's two lines are consistent with near-Poisson
    # data, the grid scan can still find a technically-valid but SPURIOUS root
    # far down in the heavy-overdispersion region (verified empirically: exact
    # synthetic Poisson data fit to mu=30, r=0.1 instead of correctly finding
    # no signal). The mean estimate shouldn't swing wildly just because the
    # dispersion search widened — bound mu_fit to the naive single-line
    # Poisson lambda from the same lo_line; a large deviation means we've
    # locked onto that spurious secondary crossing, not a real overdispersion
    # signal, so reject it and fall back to Poisson.
    naive_lambda = poisson_lambda_from_line(lo_line, lo_p)
    if naive_lambda is None:
        return None
    if not (0.5 * naive_lambda <= mu_fit <= 2.0 * naive_lambda):
        return None

    return (mu_fit, r_fit)


def fair_spread_prob(threshold: float, mean_margin: float, std: float) -> float:
    return 1 - norm_cdf(threshold, mu=mean_margin, sigma=std)


def fair_total_prob(threshold: float, mean_total: float, std: float) -> float:
    return 1 - norm_cdf(threshold, mu=mean_total, sigma=std)


def pinnacle_mean_from_spread(spread_point: float, cover_prob: float, std: float) -> float:
    """
    Invert normal CDF: given P(margin > spread_point) = cover_prob, find mean margin.
    Uses binary search approximation of the normal quantile function.
    """
    p_target = 1 - cover_prob
    lo, hi = spread_point - 4 * std, spread_point + 4 * std
    for _ in range(60):
        mid = (lo + hi) / 2
        if norm_cdf(spread_point, mu=mid, sigma=std) < p_target:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ── Threshold equivalence ────────────────────────────────────────────────────
def _lines_equivalent(a: float, b: float) -> bool:
    """
    Two thresholds produce identical outcomes for integer-scored sports when
    no integer score sits strictly in (min, max].

    P(score > 8.0) == P(score > 8.5) == P(score >= 9)  → equivalent
    P(score > 8.5) != P(score > 9.0)                   → not equivalent (9 can hit)
    P(margin > 1.0) == P(margin > 1.5) == P(margin >= 2) → equivalent
    P(margin > 1.5) != P(margin > 2.0)                 → not equivalent (2 can hit)

    Uses ceil(x + ε) so integer endpoints (8.0, 9.0) round up to the NEXT
    integer — matching "strictly greater than" semantics.
    """
    return math.ceil(a + 1e-9) == math.ceil(b + 1e-9)


# ── Text helpers ─────────────────────────────────────────────────────────────
_MONTH_NUM = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def _parse_ticker_game_time(ticker: str) -> Optional[datetime]:
    """Return the game start time as UTC, or None if unavailable/unparseable.

    Delegates to _parse_ticker_start_time for proper ET→UTC conversion.
    Returns None (not midnight UTC) when the ticker has no time component,
    so callers don't erroneously treat same-day NBA markets as already in-progress.
    NBA tickers omit the time (e.g. KXNBATOTAL-26APR26CLETOR) — returning
    midnight UTC would cause every same-day game to be skipped.
    """
    return _parse_ticker_start_time(ticker)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _abbr_to_name(abbr: str, abbr_map: Dict[str, str]) -> Optional[str]:
    return abbr_map.get(abbr.upper())


def build_city_lookup(abbr_map: Dict[str, str]) -> Dict[str, str]:
    lu: Dict[str, str] = {}
    for abbr, full in abbr_map.items():
        lu[_norm(abbr)] = full
        lu[_norm(full)] = full
        words = full.split()
        for i in range(1, len(words)):
            city_key = _norm(" ".join(words[:i]))
            if city_key not in lu:
                lu[city_key] = full
        nick = _norm(words[-1])
        if nick not in lu:
            lu[nick] = full
    return lu


def _find_game(
    team_a: str,
    team_b: str,
    game_index: Dict[str, dict],
    game_date: Optional[str] = None,
) -> Optional[dict]:
    """Look up a game by team name, preferring the date-keyed entry to avoid
    doubleheader / back-to-back collisions where two games map to the same
    bare-team key (e.g. Mets @ Dodgers on Apr 13 vs Apr 14).

    Late-night games (e.g. 10 PM ET = 2 AM UTC) cause a 1-day date skew:
    Kalshi tickers use ET date while Pinnacle uses UTC date.  We allow a
    ±1-day tolerance so these games match correctly.
    """
    from datetime import date as _date, timedelta as _td
    # Build a list of candidate dates: exact match + next day (ET→UTC skew)
    candidate_dates: list = []
    if game_date:
        candidate_dates.append(game_date)
        try:
            next_day = (_date.fromisoformat(game_date) + _td(days=1)).isoformat()
            candidate_dates.append(next_day)
        except ValueError:
            pass

    def _both_teams_match(entry: dict, ta: Optional[str], tb: Optional[str]) -> bool:
        """Verify both teams appear in the entry to avoid same-city collisions
        (e.g. SF plays both LAD and ATH on the same day — looking up SF alone
        would match the wrong game without this cross-check)."""
        if ta is None or tb is None:
            return True   # can't verify, allow it through
        entry_home = _norm(entry.get("home", ""))
        entry_away = _norm(entry.get("away", ""))
        return (_norm(ta) in (entry_home, entry_away)
                and _norm(tb) in (entry_home, entry_away))

    for t in [team_a, team_b]:
        if t is None:
            continue
        key = _norm(t)
        # 1. Try each candidate date key (exact date first, then +1 day)
        for cdate in candidate_dates:
            date_key = f"{key}_{cdate}"
            if date_key in game_index:
                entry = game_index[date_key]
                if _both_teams_match(entry, team_a, team_b):
                    return entry
        # 2. Bare-team fallback — verify date is within ±1 day to avoid
        #    using a completely different series game's Pinnacle odds.
        if key in game_index:
            entry = game_index[key]
            stored_date = entry.get("_game_date", "")
            if game_date and stored_date and stored_date not in candidate_dates:
                continue
            if _both_teams_match(entry, team_a, team_b):
                return entry
    return None


# ── Event ticker parsers ──────────────────────────────────────────────────────
def _parse_nba_event(ticker: str, abbr_map: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    suffix = re.sub(r"^KX\w+?-\d+[A-Z]+\d+", "", ticker).lstrip("-")
    suffix = suffix.split("-")[0]  # strip market suffix like -OKC4 (spread threshold)
    for i in range(2, len(suffix) - 1):
        a, b = suffix[:i], suffix[i:]
        na = _abbr_to_name(a, abbr_map)
        nb = _abbr_to_name(b, abbr_map)
        if na and nb:
            return na, nb
    return None, None


def _parse_mlb_event(ticker: str, abbr_map: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    suffix = re.sub(r"^KX\w+?-\d+[A-Z]+\d+\d{4}", "", ticker)
    if not suffix:
        suffix = re.sub(r"^KX\w+?-\d+[A-Z]+\d+", "", ticker).lstrip("-")
    for i in range(2, len(suffix) - 1):
        a, b = suffix[:i], suffix[i:]
        na = _abbr_to_name(a, abbr_map)
        nb = _abbr_to_name(b, abbr_map)
        if na and nb:
            return na, nb
    return None, None


def _parse_moneyline_team(
    market: dict,
    away_name: str,
    home_name: str,
    city_lu: Dict[str, str],
) -> Optional[str]:
    """
    Parse which team a moneyline Kalshi market refers to from its title.
    Returns the full team name (matching away_name or home_name) or None.

    Prefers yes_sub_title over title. On KXMLBGAME, `title` is the SHARED
    game-level string ("St. Louis vs Arizona Winner?") — identical across both
    team markets in an event — while `yes_sub_title` is the team-specific field
    ("St. Louis" / "Arizona"). Using title first would resolve BOTH markets to
    whichever team's name appears first in the word-scan fallback, misassigning
    one of the two. Confirmed live 2026-07-14 against a real KXMLBGAME event.
    """
    title = (market.get("yes_sub_title") or market.get("title") or "").lower()
    for name in [away_name, home_name]:
        if name is None:
            continue
        if _norm(name) in _norm(title):
            return name
        # Also check city name or nickname (last word)
        last = _norm(name.split()[-1])
        if len(last) > 3 and last in _norm(title):
            return name
    # Fallback: check each word in the title against the city lookup
    for word in re.split(r"[\s\?\-]+", title):
        key = _norm(word)
        if not key:
            continue
        full = city_lu.get(key)
        if full in (away_name, home_name):
            return full
    return None


def _parse_market_team_and_threshold(
    market: dict,
    series: str,
    event_ticker: str,
    away_name: str,
    home_name: str,
) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    ticker    = market.get("ticker", "")
    title     = market.get("title", "") or ""
    floor_str = market.get("floor_strike")

    try:
        threshold = float(floor_str) if floor_str is not None else None
    except (TypeError, ValueError):
        threshold = None

    if threshold is None:
        return None, None, None

    if "TOTAL" in series:
        if "over" in title.lower():
            return None, "total_over", threshold
        if "under" in title.lower():
            return None, "total_under", threshold
        return None, "total_over", threshold

    title_lc = title.lower()
    for name in [away_name, home_name]:
        if name and _norm(name.split()[-1]) in title_lc:
            return name, "spread_team", threshold
        if name and _norm(name) in title_lc:
            return name, "spread_team", threshold

    mkt_suffix = ticker.replace(event_ticker, "").lstrip("-").upper()
    for team in [away_name, home_name]:
        if team is None:
            continue
        for abbr, full in {**NBA_ABBR, **MLB_ABBR}.items():
            if full == team and mkt_suffix.startswith(abbr):
                return team, "spread_team", threshold

    return None, None, threshold


# ── Book confidence ────────────────────────────────────────────────────────────
def _book_confidence(books_detail: dict) -> float:
    """
    How tightly do the contributing books agree on fair probability?
    Returns 0.0 – 1.0:
      1.0  all books identical (perfect agreement)
      0.0  books spread ≥ 10 pp apart (high disagreement / stale line)
      0.5  only one book available (unverified)

    High confidence + high EV = strongest bet signal.
    Low confidence can mean: line moving, injury news, one book is stale.
    """
    probs = [v for v in books_detail.values() if isinstance(v, (int, float))]
    if len(probs) < 2:
        return 0.5
    spread = max(probs) - min(probs)
    return round(max(0.0, 1.0 - spread / 0.10), 3)


# ── Odds index fetch (decoupled from Kalshi scan) ─────────────────────────────
def fetch_odds_index(
    odds_sport: str,
    total_range: Tuple[float, float] = (0, 9999),
    spread_limit: float = 99,
    include_h2h: bool = False,
) -> Tuple[Optional[Dict], str]:
    """
    Fetch book odds and build the consensus game index.
    Costs exactly 1 Odds API credit per call (+1 more if include_h2h=True).

    Intended for the slow 30-min refresh loop so the fast 2-min Kalshi
    scan can reuse the cached result without spending credits.
    """
    try:
        games, remaining = fetch_book_odds(odds_sport, include_h2h=include_h2h)
        index = build_consensus_game_index(games, total_range, spread_limit)
        n = len(index) // max(1, 2)
        print(f"  Odds index refreshed [{odds_sport}]: {n} matchups  "
              f"|  credits left: {remaining}")
        return index, remaining
    except Exception as exc:
        print(f"  ERROR refreshing odds index [{odds_sport}]: {exc}")
        return None, "?"


# ── Scanner ───────────────────────────────────────────────────────────────────
def scan_sport(
    label: str,
    spread_series: str,
    total_series: str,
    odds_sport: str,
    abbr_map: Dict[str, str],
    spread_std: float,
    total_std: float,
    game_index: Optional[Dict] = None,   # pass cached index to skip Odds API call
    ml_series: str = "",                 # Kalshi moneyline series (optional)
    include_h2h: bool = False,           # fetch h2h for moneyline fair value (fallback-fetch path only; live path passes h2h via the cached game_index)
) -> List[dict]:
    """
    Scan one sport's Kalshi spread, total, and optionally moneyline markets
    vs the weighted-consensus fair probability from Pinnacle / DraftKings / FanDuel.

    Returns a list of edge dicts, each containing:
      ticker, title, matchup, side, kalshi, fair, raw_edge, edge (adj),
      mkt_type, pin_line, books_used, books_detail, consensus_prob, kalshi_price_ts
    """

    print(f"\n{'═'*70}")
    print(f"  {label}  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Books: {' + '.join(f'{b}({w:.0%})' for b, w in BOOK_WEIGHTS.items())}")
    print(f"  EV haircut: {EV_HAIRCUT:.0%}   Min adj. EV: {EDGE_THRESHOLD:.0%}   Top N: {TOP_BETS_PER_CYCLE}")
    print(f"{'═'*70}")

    # ── 1. Game index (use cached if provided, otherwise fetch — costs 1 credit) ──
    if game_index is not None:
        unique_matchups = len(game_index) // max(1, 2)
        print(f"  Using cached odds index: {unique_matchups} matchups  (0 credits)")
    else:
        try:
            if "baseball" in odds_sport:
                game_index, remaining = fetch_odds_index(
                    odds_sport, total_range=(5.0, 14.0), spread_limit=3.0,
                    include_h2h=include_h2h,
                )
            else:
                game_index, remaining = fetch_odds_index(
                    odds_sport, total_range=(170.0, 280.0), spread_limit=40.0
                )
            if game_index is None:
                return []
        except Exception as exc:
            print(f"  ERROR — book odds: {exc}")
            return []

    # ── 2. Kalshi markets ─────────────────────────────────────────────────
    edges:           List[dict] = []
    seen_edges:      set        = set()
    market_snapshot: dict       = {}   # {ticker|side: {adj_edge, kalshi, fair}} — ALL markets, not just ≥3%
    no_price_count              = 0
    city_lu = build_city_lookup(abbr_map)
    now_utc = datetime.now(timezone.utc)

    # Diagnostic counters — surfaced in /api/scan so we can diagnose zero-edge runs
    _diag_kalshi_events  = 0   # Kalshi events fetched across all series
    _diag_games_matched  = 0   # events where Pinnacle game was found
    _diag_line_matches   = 0   # events where an exact Pinnacle line matched
    _diag_edges_raw      = 0   # markets that exceeded EDGE_THRESHOLD before final filters
    _diag_best_adj       = -999.0  # best adjusted edge seen across all markets this scan

    def resolve(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        if _norm(raw) in game_index:
            return raw
        n = _norm(raw)
        return city_lu.get(n, raw)

    series_list = [(spread_series, "spread"), (total_series, "total")]
    if ml_series:
        series_list.append((ml_series, "moneyline"))

    for series, mkt_type in series_list:
        if not series:
            continue
        try:
            events = fetch_kalshi_events(series)
        except Exception as e:
            print(f"  ERROR — Kalshi {series}: {e}")
            continue

        print(f"  Kalshi [{series}]: {len(events)} event(s)")
        _diag_kalshi_events += len(events)

        for evt in events:
            ev_ticker = evt.get("event_ticker", "")
            ev_title  = evt.get("title", "")

            # Skip expired events
            exp_str = evt.get("expected_expiration_time") or evt.get("close_time") or ""
            if not exp_str:
                # NBA/WNBA tickers carry no embedded time, and the /events list
                # endpoint doesn't surface expiration at the event level — only
                # inside each nested market (see with_nested_markets above).
                _nested_mkts = evt.get("markets") or []
                if _nested_mkts:
                    exp_str = _nested_mkts[0].get("expected_expiration_time") or _nested_mkts[0].get("close_time") or ""
            if exp_str:
                try:
                    exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    if exp_dt < now_utc:
                        continue
                except ValueError:
                    pass

            # Skip in-progress games (Kalshi live prices ≠ pre-game book prices)
            # Rule: if we cannot confirm the game hasn't started, skip it.
            game_dt = _parse_ticker_game_time(ev_ticker)
            if game_dt is None and exp_str:
                # NBA tickers have no time component — estimate start from expiration.
                # NBA games run up to 3.5h (OT + delays) → use 5h offset (conservative).
                # MLB games run ~3h + buffer → use 3.5h offset.
                try:
                    from datetime import timedelta as _tde
                    exp_dt_g = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    is_nba   = "NBA" in series.upper()
                    game_dt  = exp_dt_g - _tde(hours=5.0 if is_nba else 3.5)
                except (ValueError, AttributeError):
                    pass
            # If game start time is still unknown, skip — never scan unknowns.
            # Scanning a game of unknown start risks comparing in-game Kalshi
            # prices against pre-game book lines, producing phantom edges.
            if game_dt is None:
                continue
            if game_dt < now_utc:
                continue

            if "MLB" in series:
                away_raw, home_raw = _parse_mlb_event(ev_ticker, abbr_map)
            else:
                away_raw, home_raw = _parse_nba_event(ev_ticker, abbr_map)

            if away_raw is None or home_raw is None:
                title_clean = re.sub(r":\s*(spread|total.*)", "", ev_title, flags=re.IGNORECASE)
                parts = re.split(r"\s+(?:at|vs\.?)\s+", title_clean, flags=re.IGNORECASE)
                if len(parts) == 2:
                    away_raw, home_raw = parts[0].strip(), parts[1].strip()

            away_name = resolve(away_raw)
            home_name = resolve(home_raw)
            game_date = _parse_ticker_date(ev_ticker)
            game_info = _find_game(away_name, home_name, game_index, game_date)
            if game_info is None:
                continue
            _diag_games_matched += 1

            try:
                time.sleep(0.2)
                mkts = fetch_event_markets(ev_ticker)
            except Exception:
                continue

            for mkt in mkts:
                prices = kalshi_prices(mkt)
                if prices is None:
                    no_price_count += 1
                    continue
                yes_bid, yes_ask = prices

                if mkt_type == "moneyline":
                    # Moneylines have no threshold — parse team from title only
                    team_name = _parse_moneyline_team(mkt, away_name, home_name, city_lu)
                    if team_name is None:
                        continue
                    direction = "moneyline"
                    threshold = None
                else:
                    team_name, direction, threshold = _parse_market_team_and_threshold(
                        mkt, series, ev_ticker, away_name, home_name
                    )
                    if direction is None or threshold is None:
                        continue

                # ── Compute consensus fair value ──────────────────────────
                fair           = None
                books_detail   = {}
                books_used     = []
                consensus_prob = None
                total_fair_src = "exact"   # all edges require a direct Pinnacle line

                mkt_team_is_fav = False   # set below when spread fav is identified
                if direction == "spread_team" and team_name:
                    fav_spread_info = None
                    for candidate in [home_name, away_name, team_name]:
                        if candidate is None:
                            continue
                        si = game_info["spread"].get(candidate)
                        if si is None:
                            for pname, sdata in game_info["spread"].items():
                                if _norm(candidate) == _norm(pname):
                                    si = sdata
                                    break
                        if si and si[1] <= 0:
                            fav_spread_info = (candidate, si)
                            break

                    if fav_spread_info is None:
                        for candidate in [home_name, away_name]:
                            if candidate is None:
                                continue
                            si = game_info["spread"].get(candidate)
                            if si:
                                fav_spread_info = (candidate, si)
                                break

                    if fav_spread_info:
                        fav_name, (fav_cover_prob, fav_spread_pt, fav_books) = fav_spread_info
                        mkt_team_is_fav = _norm(team_name or "") == _norm(fav_name or "")
                        # Require a direct Pinnacle alternate spread line at this
                        # exact threshold.  No inference — if Pinnacle doesn't post
                        # this line, skip.  Their alternate prices already embed all
                        # matchup context; no model math needed.
                        spread_pin_lines = game_info.get("spread_pin_lines", {})
                        # Look up the Kalshi market's team directly
                        team_pin_lines: Optional[Dict[float, float]] = spread_pin_lines.get(team_name)
                        if team_pin_lines is None and team_name:
                            for pname, plines in spread_pin_lines.items():
                                if _norm(pname) == _norm(team_name):
                                    team_pin_lines = plines
                                    break
                        if not team_pin_lines:
                            continue  # no Pinnacle alternate spread data for this team
                        # Find exact match (±0.25) for the Kalshi threshold
                        pin_match: Optional[Tuple[float, float]] = None
                        for pin_pt, pin_cov in team_pin_lines.items():
                            if abs(pin_pt - threshold) <= 0.25:
                                pin_match = (pin_pt, pin_cov)
                                break
                        if pin_match is None:
                            continue  # Pinnacle doesn't post this exact spread line
                        _diag_line_matches += 1
                        pin_match_pt, fair = pin_match

                        # ── Push correction for integer spread lines ──────────
                        # Same issue as totals: Pinnacle's -2.0 line has a push
                        # (margin = exactly 2), but Kalshi >2.0 is binary — a
                        # margin of 2 is a LOSS (2 is not strictly > 2.0).
                        # Half-point lines (1.5, 2.5 …) have no push — skip them.
                        if abs(pin_match_pt - round(pin_match_pt)) < 1e-9:
                            corr     = _push_correction(fair, pin_match_pt, spread_std)
                            push_pct = (1.0 - corr) * 100
                            print(f"    [push-corr] integer spread {pin_match_pt:.1f}: "
                                  f"push≈{push_pct:.1f}%  "
                                  f"cover {fair:.3f}→{fair*corr:.3f}")
                            fair *= corr

                        books_detail   = {"pinnacle": fair}
                        books_used     = ["pinnacle"]
                        consensus_prob = fair

                elif direction in ("total_over", "total_under"):
                    total_info = game_info.get("total", {})
                    if total_info.get("over_point") is not None:
                        # ── Prefer exact Pinnacle line match over equivalence rule ──
                        # Check all stored Pinnacle lines for an exact or equivalent match.
                        # Two thresholds are equivalent when they produce the same outcome
                        # in integer-scored sports (e.g. 8.0 == 8.5 in baseball — both
                        # require 9+ runs). Prefer exact/equivalent matches over Gaussian.
                        pin_lines    = total_info.get("pin_lines", {})
                        exact_match  = None
                        for pin_pt, pin_probs in pin_lines.items():
                            if abs(pin_pt - threshold) <= 0.25 or _lines_equivalent(pin_pt, threshold):
                                exact_match = (pin_pt, pin_probs)
                                break
                        if exact_match:
                            _diag_line_matches += 1
                            # Use the Pinnacle line that directly matches Kalshi threshold
                            pin_total       = exact_match[0]
                            po              = exact_match[1]["over_prob"]
                            pu              = exact_match[1]["under_prob"]
                            total_fair_src  = "exact"   # Pinnacle has this exact line

                            # ── Push correction for integer Pinnacle lines ────────
                            # Pinnacle posts integer totals (e.g. 8.0) as 3-outcome
                            # markets: over / push / under.  Their no-vig probability
                            # redistributes the push, inflating the over prob vs the
                            # true P(total > line).  Kalshi is binary — a total landing
                            # on the integer is a LOSS, not a push.  Correct before use.
                            if abs(pin_total - round(pin_total)) < 1e-9:
                                corr      = _push_correction(po, pin_total, total_std)
                                push_pct  = (1.0 - corr) * 100
                                print(f"    [push-corr] integer line {pin_total:.1f}: "
                                      f"push≈{push_pct:.1f}%  "
                                      f"over {po:.3f}→{po*corr:.3f}  "
                                      f"under {pu:.3f}→{pu*corr:.3f}")
                                po *= corr
                                pu *= corr
                        else:
                            # No exact Pinnacle alternate line — skip entirely.
                            # Gaussian extrapolation was removed: even a 0.5-run
                            # step can systematically mis-estimate edge direction
                            # and was the likely cause of YES/over bias.
                            continue

                        books_used  = total_info.get("books_used", [])
                        books_detail = {
                            bk: td["over_prob"]
                            for bk, td in total_info.get("per_book", {}).items()
                        }
                        consensus_prob = po
                        # Direct no-vig probability from Pinnacle's matching line
                        fair = po if direction == "total_over" else pu

                elif direction == "moneyline":
                    # Look up this team's no-vig win probability from h2h consensus
                    h2h = game_info.get("h2h", {})
                    h2h_entry = h2h.get(team_name)
                    if h2h_entry is None:
                        # Try normalized name match
                        for k, v in h2h.items():
                            if _norm(k) == _norm(team_name):
                                h2h_entry = v
                                break
                    if h2h_entry is None:
                        continue
                    consensus_prob, bk_detail = h2h_entry
                    books_detail   = bk_detail
                    books_used     = list(books_detail.keys())
                    # Fair value = no-vig P(this team wins)
                    fair = consensus_prob

                if fair is None:
                    continue

                # Safety: require at least Pinnacle, or 2+ books
                if "pinnacle" not in books_used and len(books_used) < 2:
                    continue

                # ── EV calculation: fee deduction then uncertainty haircut ──
                # Use ASK for YES entry, BID-complement for NO entry.
                yes_raw_edge = fair - yes_ask
                no_raw_edge  = (1 - fair) - (1 - yes_bid)

                # Step 1: subtract Kalshi profit fee (charged on winnings only).
                # fee_cost = KALSHI_FEE_RATE × fair_prob × (1 − entry_price)
                yes_fee  = KALSHI_FEE_RATE * fair        * (1 - yes_ask)
                no_fee   = KALSHI_FEE_RATE * (1 - fair)  * yes_bid
                yes_fee_adj = yes_raw_edge - yes_fee
                no_fee_adj  = no_raw_edge  - no_fee

                # Step 2: apply model-uncertainty haircut
                yes_adj = yes_fee_adj * (1 - EV_HAIRCUT)
                no_adj  = no_fee_adj  * (1 - EV_HAIRCUT)

                best_adj = max(yes_adj, no_adj)
                if best_adj > _diag_best_adj:
                    _diag_best_adj = best_adj
                if best_adj > 0:
                    _diag_edges_raw += 1   # count positive-EV markets before threshold filter

                # ── Market snapshot: record current edge for BOTH sides ───────
                # Used by Open Positions to show live value on existing bets even
                # when below the 3% flag threshold.  Zero extra credits — we've
                # already computed this data, we're just capturing it.
                _snap_ticker = mkt.get("ticker", "")
                if _snap_ticker:
                    # Note: pin_line is not yet computed at this point in the loop
                    # (it's set below, only for edges that clear EDGE_THRESHOLD).
                    # We store kalshi_line (threshold) which IS available — the UI
                    # falls back to snap.kalshi_line when snap.pin_line is null.
                    market_snapshot[f"{_snap_ticker}|YES"] = {
                        "adj_edge":   round(yes_adj, 4),
                        "kalshi":     round(yes_ask, 4),
                        "fair":       round(fair, 4),
                        "edge_pct":   round(yes_adj * 100, 1),
                        "kalshi_line": threshold,   # Kalshi threshold (e.g. 8.0)
                    }
                    market_snapshot[f"{_snap_ticker}|NO"] = {
                        "adj_edge":   round(no_adj, 4),
                        "kalshi":     round(1 - yes_bid, 4),
                        "fair":       round(1 - fair, 4),
                        "edge_pct":   round(no_adj * 100, 1),
                        "kalshi_line": threshold,
                    }

                # Log near-misses: markets within 5pp of threshold in either direction
                _near_miss_gap = best_adj - EDGE_THRESHOLD  # negative = below threshold
                if -0.05 < _near_miss_gap < 0:
                    _best_side = "YES" if yes_adj >= no_adj else "NO"
                    _matchup_str = f"{game_info.get('away','?')} @ {game_info.get('home','?')}"
                    print(
                        f"  [near-miss] {mkt_type:<6} {_matchup_str:<30} {_best_side} "
                        f"fair={fair:.3f} ask={yes_ask:.3f} bid={yes_bid:.3f} "
                        f"adj={best_adj:+.1%} (need {EDGE_THRESHOLD:.0%})"
                    )
                if best_adj < EDGE_THRESHOLD:
                    continue
                # Cap: edges above MAX_EDGE are almost certainly line mismatches
                # or stale book data — not real market opportunities.
                if best_adj > MAX_EDGE:
                    continue

                if yes_adj >= no_adj:
                    side      = "YES"
                    k_side    = yes_ask
                    f_side    = fair
                    raw_edge  = yes_raw_edge
                    adj_edge  = yes_adj
                else:
                    side      = "NO"
                    k_side    = 1 - yes_bid
                    f_side    = 1 - fair
                    raw_edge  = no_raw_edge
                    adj_edge  = no_adj

                # Price floor: skip markets priced below MIN_KALSHI_PRICE.
                # Data shows 0/18 wins on sub-15¢ markets — likely threshold
                # mismatches (e.g. Kalshi ">2.5 runs" vs Pinnacle ">8.5 total").
                if k_side < MIN_KALSHI_PRICE:
                    continue

                matchup   = f"{game_info['away']} @ {game_info['home']}"
                # For moneylines, use team_name instead of threshold (no line to dedup on)
                dedup_key = (_norm(matchup), mkt_type,
                             _norm(team_name) if mkt_type == "moneyline" else threshold,
                             side)
                if dedup_key in seen_edges:
                    continue
                seen_edges.add(dedup_key)

                # ── Book-consensus validation ─────────────────────────────
                # books_detail always stores the CANONICAL direction probability:
                #   • total markets  → over_prob  (YES on OVER market)
                #   • spread markets → fav_cover_prob (YES on FAV-covers market)
                #
                # For markets where Kalshi YES = the OPPOSITE canonical direction
                # (total_under or underdog-spread), we must flip the per-book
                # probabilities before passing to the validator so it compares
                # the right side against k_side.
                #
                # "canonical_yes_flip" = True when books_detail represents the
                # OPPOSITE of what Kalshi's YES side means.
                canonical_yes_flip = (
                    direction == "total_under"
                    or (direction == "spread_team"
                        and fav_spread_info is not None
                        and not mkt_team_is_fav)
                )
                if canonical_yes_flip:
                    # Re-express books_detail as P(Kalshi YES wins) per book
                    books_for_validation = {
                        b: round(1.0 - p, 4) for b, p in books_detail.items()
                    }
                else:
                    books_for_validation = books_detail

                consensus_valid, consensus_reason = _validate_book_consensus(
                    books_for_validation, side, k_side
                )
                if not consensus_valid:
                    book_str_rej = "  ".join(
                        f"{b}={p:.1%}" for b, p in sorted(books_for_validation.items())
                    )
                    print(
                        f"  ✗ {mkt_type:<6} {matchup:<35} {side}  "
                        f"REJECTED — {consensus_reason}  [{book_str_rej}]"
                    )
                    continue

                pin_line = None
                if direction == "spread_team" and fav_spread_info:
                    _, (_, fav_pt, _) = fav_spread_info
                    pin_line = fav_pt
                elif direction in ("total_over", "total_under"):
                    pin_line = game_info.get("total", {}).get("over_point")

                raw_title = mkt.get("title") or mkt.get("yes_sub_title") or mkt.get("ticker", "")
                if threshold is None:
                    prop_label = raw_title
                elif str(threshold) in raw_title:
                    prop_label = raw_title
                else:
                    prop_label = f"{raw_title} (>{threshold})"

                # ── Confidence score ──────────────────────────────────────
                confidence = _book_confidence(books_detail)

                # ── Match audit log (totals only) ─────────────────────────
                # Searchable record of which Pinnacle game/line was matched.
                # Lets you spot wrong-game matches by checking pin_line vs kalshi_thresh.
                if mkt_type == "total":
                    _pin_t = locals().get("pin_total", "?")
                    _pin_o = round(locals().get("po", 0) * 100, 1) if locals().get("po") is not None else "?"
                    print(
                        f"  [match] {matchup:<35}  "
                        f"kalshi_thresh={threshold}  pin_line={_pin_t}  pin_over={_pin_o}%  adj={adj_edge:+.1%}"
                    )

                # ── Diagnostic log ────────────────────────────────────────
                book_str = "  ".join(f"{b}={p:.1%}" for b, p in sorted(books_detail.items()))
                print(
                    f"  ✓ {mkt_type:<6} {matchup:<35} {side}  "
                    f"kalshi={k_side:.1%}  fair={f_side:.1%}  "
                    f"raw={raw_edge:+.1%}  adj={adj_edge:+.1%}  "
                    f"conf={confidence:.0%}  [{book_str}]  [{consensus_reason}]"
                )

                # ── Consensus YES/NO probabilities (renormalised to sum to 1) ────
                # consensus_prob is always the OVER/fav-cover (canonical) probability.
                # For total_under / underdog-spread markets, Kalshi YES = canonical NO,
                # so flip to get the true P(Kalshi YES wins).
                if canonical_yes_flip:
                    cons_yes = round(1.0 - consensus_prob, 4) if consensus_prob else None
                else:
                    cons_yes = round(consensus_prob, 4) if consensus_prob else None
                cons_no = round(1.0 - cons_yes, 4) if cons_yes is not None else None

                # per_book_novig: use the already-corrected books_for_validation dict
                # so YES/NO labels in the UI match the actual Kalshi market direction.
                per_book_novig = _build_per_book_novig(books_for_validation, side)

                edges.append({
                    "ticker":               mkt.get("ticker", ""),
                    "title":                prop_label,
                    "kalshi_line":          threshold,   # Kalshi floor_strike (e.g. 8.5) — the line to bet on Kalshi
                    "matchup":              matchup,
                    "side":                 side,
                    "kalshi":               round(k_side, 4),
                    "fair":                 round(f_side, 4),
                    "raw_edge":             round(raw_edge, 4),
                    "edge":                 round(adj_edge, 4),   # post-haircut — used for display/filter
                    "confidence":           confidence,           # book-agreement score 0-1
                    "mkt_type":             mkt_type,
                    "pin_line":             pin_line,
                    "fair_source":          total_fair_src,  # always "exact" — every edge backed by a direct Pinnacle line
                    "books_used":           books_used,
                    "books_detail":         books_detail,
                    "per_book_novig":       per_book_novig,       # {book: {yes_prob, no_prob, yes_american, no_american}}
                    "consensus_yes":        cons_yes,             # weighted-consensus P(YES)
                    "consensus_no":         cons_no,              # weighted-consensus P(NO)  — always 1 - cons_yes
                    "consensus_yes_american": prob_to_american(cons_yes) if cons_yes else None,
                    "consensus_no_american":  prob_to_american(cons_no)  if cons_no  else None,
                    "consensus_prob":       cons_yes,             # legacy alias
                    "is_valid_consensus":   True,                 # always True here — invalids were discarded above
                    "consensus_reason":     consensus_reason,     # e.g. "Confirmed by Pinnacle + DraftKings"
                    "kalshi_price_ts":      now_utc.isoformat(),
                    # Reliable game start (Pinnacle commence_time) for sports whose
                    # ticker doesn't encode it (WNBA/NBA) — see game_index fix above.
                    "commence_time":        game_info.get("commence_time") if game_info else None,
                })

    if no_price_count:
        print(f"  (skipped {no_price_count} markets with no price)")

    _pin_game_count = len(game_index) // max(1, 2)  # approximate unique matchups
    _best_adj_str = f"{_diag_best_adj:+.1%}" if _diag_best_adj > -999 else "n/a"
    print(
        f"  [diag] pin_games={_pin_game_count}  kalshi_events={_diag_kalshi_events}  "
        f"games_matched={_diag_games_matched}  line_matches={_diag_line_matches}  "
        f"edges_raw(+EV)={_diag_edges_raw}  edges_final={len(edges)}  "
        f"best_adj={_best_adj_str}"
    )

    # ── 3. Sort by confidence × adj_edge, correlation-control, top N ─────
    # Confidence-weighted score: rewards bets where all books agree AND edge is large.
    # A 10% edge where DK/FD/Pinnacle all agree outranks a 12% edge where only one
    # book has data or books are far apart.
    edges.sort(key=lambda x: x["confidence"] * x["edge"], reverse=True)
    edges = _apply_correlation_control(edges)
    edges = edges[:TOP_BETS_PER_CYCLE]

    # ── 4. Print final table ──────────────────────────────────────────────
    if not edges:
        print(f"\n  No edges ≥ {EDGE_THRESHOLD:.0%} (adj.) found.")
    else:
        print(f"\n  Top {len(edges)} edge(s) — sorted by confidence × adj.EV:")
        print(f"  {'TYPE':<7} {'MATCHUP':<35} {'PROP':<35} {'SIDE':<4} "
              f"{'PRICE':>7} {'FAIR':>7} {'RAW':>7} {'ADJ':>7} {'CONF':>6} {'BOOKS'}")
        print(f"  {'─'*7} {'─'*35} {'─'*35} {'─'*4} {'─'*7} {'─'*7} {'─'*7} {'─'*7} {'─'*6} {'─'*20}")
        for e in edges:
            bks = ",".join(e.get("books_used", []))
            stars = "★★★" if e["confidence"] >= 0.8 else "★★" if e["confidence"] >= 0.5 else "★"
            print(
                f"  {e['mkt_type']:<7} {e['matchup']:<35} {e['title'][:35]:<35} "
                f"{e['side']:<4} {e['kalshi']:>6.1%} {e['fair']:>6.1%} "
                f"\033[90m{e['raw_edge']:>+6.1%}\033[0m "
                f"\033[92m{e['edge']:>+6.1%}\033[0m "
                f"{stars:>4}  {bks}"
            )

    scan_stats = {
        "pin_games":      _pin_game_count,
        "kalshi_events":  _diag_kalshi_events,
        "games_matched":  _diag_games_matched,
        "line_matches":   _diag_line_matches,
        "edges_raw":      _diag_edges_raw,
        "edges_final":    len(edges),
        "best_adj_pct":   round(_diag_best_adj * 100, 1) if _diag_best_adj > -999 else None,
    }
    return edges, scan_stats, market_snapshot


# ── Player-props helpers ──────────────────────────────────────────────────────
# Standard markets only. Pinnacle does NOT post *_alternate player props on the
# Odds API (verified — only DK/FanDuel do), and we require Pinnacle as the sharp
# anchor, so requesting alternates just wastes ~1.8x credits on lines we reject.
# Crucially, Pinnacle's STANDARD market already returns MULTIPLE lines per player
# (e.g. total bases [0.5, 1.5], strikeouts [4.5, 5.5]). The per-line index in
# build_all_player_props() captures all of them, which is what lets us match
# Kalshi's 0.5 / 2.5 milestone markets to a real Pinnacle price.
# Home runs retired 2026-06-24: extreme-longshot market where the proportional
# no-vig de-vig overstates the fair value, so apparent CLV was an artifact — 43
# shadow bets ran -5.5pp vs expected. Not scanned (saves credits), not displayed.
# Hits and RBIs cut 2026-07-10: zero flagged bets in the full 170-bet sample
# (106 K, 56 TB, 8 Total, 0 Hits, 0 RBI) — pure credit cost with no signal.
# Also halves the per-event props cost (4 credits/event -> 2), the single
# biggest driver of Odds API credit usage.
#
# TOTAL BASES TERMINATED 2026-07-23 (user call). TB was fully shadowed since
# 2026-07-20 (phantom-vig overs, true Kalshi CLV ~0, tradeable + Shin de-vig
# both dead — see project-tb-full-shadow). Shadow logging cost real credits
# (~half of all prop spend) for a market with no viable path to profitability
# without a long outcome-recalibration slog. Dropped from the scan entirely to
# reclaim that budget for MLS (moneyline + total goals). The settled TB track
# record is frozen and still displayed (tagged TERMINATED in the UI); no NEW TB
# is fetched, priced, or flagged. This also ends the TB_NO_EXPERIMENT under-side
# capture (its premise was already weak — see project-tb-full-shadow).
PLAYER_PROP_MARKETS = "pitcher_strikeouts"
NBA_PLAYER_PROP_MARKETS = "player_points,player_assists,player_threes"
# WNBA: Pinnacle carries points/rebounds/assists but NOT threes (verified
# 2026-07-10 against the live Odds API — DK/FanDuel have player_threes,
# Pinnacle doesn't). Pinnacle is the required sole fair-value anchor
# (BOOK_WEIGHTS), so threes is omitted — a Pinnacle-less market would never
# produce a valid fair probability anyway.
WNBA_PLAYER_PROP_MARKETS = "player_points,player_rebounds,player_assists"

# WNBA prop line-extrapolation (2026-07-22). MLB rejects a Kalshi rung that
# doesn't match Pinnacle's posted line (see the guard in scan_player_props) —
# Pinnacle posts one basketball line per player, so ~2/3 of Kalshi's rungs were
# dropped. But points/rebounds/assists are high-volume "all-game" accumulators:
# low coefficient of variation, so a distribution fit to Pinnacle's anchor is
# stable near it. We fit that anchor and evaluate at NEARBY Kalshi rungs, ONLY
# for these self-averaging stats and ONLY within WNBA_EXTRAP_MAX_RUNGS — far
# extrapolation reintroduces the thin-tail overconfidence that burned Total Bases
# (a low-volume, lumpy stat).
#
# Model: a NORMAL with SD = cv*mean, NOT Poisson. Poisson forces variance=mean
# (sigma≈sqrt(mu)≈3.3 for ~11 pts), far too thin for points (real SD≈5-6) — it
# over/under-states a rung 1 away by ~5pp, enough to fake an edge. The cv values
# are per-stat priors (unvalidated — watch calibration once bets settle).
WNBA_EXTRAP_PROP_TYPES = {"player_points", "player_rebounds", "player_assists"}
WNBA_EXTRAP_MAX_RUNGS  = 1.5
WNBA_EXTRAP_CV = {"player_points": 0.45, "player_rebounds": 0.45, "player_assists": 0.55}

MLB_PROP_SERIES: Dict[str, str] = {
    "KXMLBKS":  "pitcher_strikeouts",
    "KXMLBTB":  "batter_total_bases",
}

NBA_PROP_SERIES: Dict[str, str] = {
    "KXNBAPTS": "player_points",
    "KXNBAAST": "player_assists",
    "KXNBA3PT": "player_threes",
}

WNBA_PROP_SERIES: Dict[str, str] = {
    "KXWNBAPTS": "player_points",
    "KXWNBAREB": "player_rebounds",
    "KXWNBAAST": "player_assists",
}


def _norm_player(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def _find_player_in_title(title: str, prop_lookup: Dict[str, dict]) -> Optional[dict]:
    title_norm = re.sub(r"[^a-z]", "", title.lower())
    for key, pp in prop_lookup.items():
        if key and key in title_norm:
            return pp
    for key, pp in prop_lookup.items():
        last = re.sub(r"[^a-z]", "", pp["player"].split()[-1].lower())
        if len(last) > 3 and last in title_norm:
            return pp
    return None


def build_all_player_props(
    odds_sport: str,
    odds_events: List[dict],
    needed_teams: Optional[set] = None,
    markets: str = None,
) -> Dict[str, Dict[str, dict]]:
    """
    Fetch player-prop odds for all books and build a weighted-consensus
    no-vig over probability per (player, prop_type).

    Returns: { player_norm: { prop_type: {player, line, over_prob, lambda} } }
    """
    if markets is None:
        markets = "pitcher_strikeouts,batter_hits"
    now = datetime.now(timezone.utc)
    target_events = []
    for ev in odds_events:
        ct_str = ev.get("commence_time", "")
        try:
            ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
            if ct <= now or (ct - now).total_seconds() > 172800:
                continue
        except ValueError:
            continue
        if needed_teams:
            home_n = _norm(ev.get("home_team", ""))
            away_n = _norm(ev.get("away_team", ""))
            if not (home_n in needed_teams or away_n in needed_teams):
                continue
        target_events.append(ev)

    target_events.sort(key=lambda e: e.get("commence_time", ""))
    target_events = target_events[:MAX_PROP_EVENTS]

    player_lookup: Dict[str, Dict[str, dict]] = {}
    fetched = 0

    for ev in target_events:
        try:
            edata = fetch_player_prop_odds_event(odds_sport, ev["id"], markets=markets)
            time.sleep(0.3)
        except Exception as e:
            print(f"    ERROR fetching props for {ev.get('away_team')} @ {ev.get('home_team')}: {e}")
            continue

        # Accumulate per-book, per-player, per-prop-type, PER-LINE prices.
        # Standard and *_alternate markets are merged under the same prop_type so
        # all of a player's lines (0.5 / 1.5 / 2.5 / 3.5 …) coexist.
        # accum[player_norm][prop_type][line][book_key] = {player, over_price, under_price}
        accum: Dict[str, Dict[str, Dict[float, Dict[str, dict]]]] = {}
        # std_line[player_norm][prop_type] = Pinnacle's STANDARD (non-alt) line —
        # used to pick the "main" line for backward-compatible default pricing.
        std_line: Dict[str, Dict[str, float]] = {}

        for bm in edata.get("bookmakers", []):
            bkey = bm["key"]
            if bkey not in BOOK_WEIGHTS:
                continue
            for mkt in bm.get("markets", []):
                mtype_raw = mkt.get("key", "")
                is_alt    = mtype_raw.endswith("_alternate")
                mtype     = mtype_raw.replace("_alternate", "")
                outcomes  = mkt.get("outcomes", [])
                for o in outcomes:
                    direction = (o.get("name") or "").lower()
                    pname     = (o.get("description") or "").strip()
                    pt        = float(o.get("point") or 0)
                    price     = o.get("price", 0)
                    if not pname or direction not in ("over", "under"):
                        continue
                    key = _norm_player(pname)
                    lb = accum.setdefault(key, {}).setdefault(mtype, {}).setdefault(pt, {}).setdefault(bkey, {
                        "player": pname,
                        "over_price": None, "under_price": None,
                    })
                    if direction == "over":
                        lb["over_price"] = price
                    else:
                        lb["under_price"] = price
                    if not is_alt and bkey == "pinnacle":
                        std_line.setdefault(key, {})[mtype] = pt

        # Convert per-book prices → weighted consensus no-vig over probability,
        # building one entry per distinct line so alternate lines are priced from
        # Pinnacle's REAL posted number (not Poisson-extrapolated).
        for player_key, props in accum.items():
            for mtype, lines_dict in props.items():
                line_entries: Dict[float, dict] = {}

                for line_val, book_entries in lines_dict.items():
                    book_probs: Dict[str, float] = {}
                    for bkey, be in book_entries.items():
                        op = be.get("over_price")
                        up = be.get("under_price")
                        if op is None or up is None:
                            continue
                        po, _ = no_vig_prob(op, up)
                        book_probs[bkey] = po

                    if not book_probs:
                        continue
                    # Safety: require Pinnacle or 2+ books per line
                    if "pinnacle" not in book_probs and len(book_probs) < 2:
                        continue

                    consensus_po, _ = _weighted_consensus(book_probs)
                    lam = poisson_lambda_from_line(line_val, consensus_po)
                    if lam is None:
                        continue

                    # Per-book lambdas — each book fitted to THIS line.
                    per_book_lambdas: Dict[str, float] = {}
                    for bkey, be in book_entries.items():
                        op = be.get("over_price")
                        up = be.get("under_price")
                        if op is None or up is None:
                            continue
                        bpo, _ = no_vig_prob(op, up)
                        blam = poisson_lambda_from_line(line_val, bpo)
                        if blam is not None:
                            per_book_lambdas[bkey] = blam

                    line_entries[line_val] = {
                        "player":            next(iter(book_entries.values()))["player"],
                        "line":              line_val,
                        "over_prob":         consensus_po,
                        "lambda":            lam,
                        "books_used":        list(book_probs.keys()),
                        "books_detail":      book_probs,
                        "per_book_lambdas":  per_book_lambdas,
                        # RAW per-book over/under American odds at this line — the
                        # lossy-lost input for the de-vig study. Carried into the
                        # edge's devig_inputs (TB-only at that stage) so Shin/power
                        # fair can be recomputed offline. Live pricing unaffected.
                        "raw_odds":          {
                            bk: {"over": be.get("over_price"), "under": be.get("under_price")}
                            for bk, be in book_entries.items()
                            if be.get("over_price") is not None and be.get("under_price") is not None
                        },
                    }

                if not line_entries:
                    continue

                # Main line (backward-compat default): Pinnacle's standard line if
                # present, else the line closest to a coin flip.
                main_line = std_line.get(player_key, {}).get(mtype)
                if main_line not in line_entries:
                    main_line = min(
                        line_entries.keys(),
                        key=lambda L: abs(line_entries[L]["over_prob"] - 0.5),
                    )

                # ── TB-only: Negative Binomial overdispersion diagnostic ────────
                # Uses the independently-priced lines above (each already fit to
                # its own Poisson lambda) as two data points to fit a dispersion
                # parameter directly from the market. Diagnostic only -- not read
                # by any live pricing path; see fit_neg_binom_two_point docstring.
                nb_fit = None
                if mtype == "batter_total_bases" and len(line_entries) >= 2:
                    _sorted_lines = sorted(line_entries.keys())
                    _lo_L, _hi_L = _sorted_lines[0], _sorted_lines[-1]
                    nb_fit = fit_neg_binom_two_point(
                        _lo_L, line_entries[_lo_L]["over_prob"],
                        _hi_L, line_entries[_hi_L]["over_prob"],
                    )

                entry = dict(line_entries[main_line])   # default = main line
                entry["lines"] = line_entries           # all lines (incl. alternates)
                entry["nb_fit"] = nb_fit                # (mu, r) diagnostic fit, or None
                entry["commence_time"] = ev.get("commence_time")  # game start — CLV freeze needs it for WNBA/NBA (tickers omit time)
                # Doubleheader collision guard: the same player + prop across two
                # same-day games collides on this name-only key. We can't tell which
                # game's line pairs with which Kalshi market by name alone, so flag
                # the survivor unusable and skip it downstream — better than pricing
                # the G1 market off G2's line (see the suppressed Muncy bet). Proper
                # fix = game-aware keying (deferred). Interim: skip.
                _prev = player_lookup.get(player_key, {}).get(mtype)
                if _prev is not None and _prev.get("commence_time") != entry["commence_time"]:
                    entry["_dh_collision"] = True
                player_lookup.setdefault(player_key, {})[mtype] = entry

        fetched += 1
        print(f"    [{fetched}] {ev.get('away_team')} @ {ev.get('home_team')} — props fetched")

    print(f"  Built player index: {len(player_lookup)} players across {fetched} game(s)")
    return player_lookup


def scan_player_props(
    odds_sport: str = "baseball_mlb",
    abbr_map: Optional[Dict[str, str]] = None,
    max_games: int = 15,
    prop_series: Optional[Dict[str, str]] = None,
    prop_markets: Optional[str] = None,
    sport_label: str = "MLB",
    mkt_type_label: str = "prop",
    parse_event_fn=None,
) -> List[dict]:
    """
    Scan Kalshi player-prop markets vs consensus no-vig props.
    Sport-agnostic — pass prop_series, prop_markets, and sport_label for each sport.
    Defaults to MLB configuration.
    """
    if abbr_map is None:
        abbr_map = MLB_ABBR
    if prop_series is None:
        prop_series = MLB_PROP_SERIES
    if prop_markets is None:
        prop_markets = PLAYER_PROP_MARKETS
    if parse_event_fn is None:
        parse_event_fn = _parse_mlb_event

    print(f"\n{'═'*70}")
    print(f"  {sport_label} Player Props  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  EV haircut: {EV_HAIRCUT:.0%}   Min adj. EV: {EDGE_THRESHOLD:.0%}")
    print(f"{'═'*70}")

    now_utc = datetime.now(timezone.utc)
    prop_snapshot: Dict[str, dict] = {}   # all scanned markets regardless of threshold

    # 1. Collect Kalshi prop markets
    kalshi_props: List[dict] = []
    for series, prop_type in prop_series.items():
        try:
            evts = fetch_kalshi_events(series)
        except Exception as e:
            print(f"  [{series}] ERROR: {e}")
            continue
        print(f"  [{series}] → {prop_type}: {len(evts)} event(s)")
        for evt in evts:
            exp_str = evt.get("expected_expiration_time") or evt.get("close_time") or ""
            if not exp_str:
                _nested_mkts = evt.get("markets") or []
                if _nested_mkts:
                    exp_str = _nested_mkts[0].get("expected_expiration_time") or _nested_mkts[0].get("close_time") or ""
            if exp_str:
                try:
                    exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    if exp_dt < now_utc:
                        continue
                except ValueError:
                    pass

            ev_ticker = evt.get("event_ticker", "")
            game_dt   = _parse_ticker_game_time(ev_ticker)
            if game_dt is None and exp_str:
                try:
                    from datetime import timedelta as _tde
                    exp_dt_g = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    is_nba   = any(s in ev_ticker.upper() for s in ("NBA", "KXNBA"))
                    game_dt  = exp_dt_g - _tde(hours=4.0 if is_nba else 3.5)
                except (ValueError, AttributeError):
                    pass
            if game_dt is not None and game_dt <= now_utc:
                continue  # in-progress game

            ev_title = evt.get("title", "") or ""
            try:
                time.sleep(0.15)
                mkts = fetch_event_markets(ev_ticker)
            except Exception:
                continue
            for mkt in mkts:
                prices = kalshi_prices(mkt)
                if prices is None:
                    continue
                yes_bid, yes_ask = prices
                prop_mid = (yes_bid + yes_ask) / 2
                if prop_mid < PROP_MIN_PRICE or prop_mid > PROP_MAX_PRICE:
                    continue
                floor_str = mkt.get("floor_strike")
                try:
                    threshold = float(floor_str) if floor_str is not None else None
                except (TypeError, ValueError):
                    threshold = None
                if threshold is None:
                    continue
                kalshi_props.append({
                    "series":    series,
                    "prop_type": prop_type,
                    "ev_title":  ev_title,
                    "mkt_title": mkt.get("title") or ev_title or "",
                    "ticker":    mkt.get("ticker", ""),
                    "threshold": threshold,
                    "yes_bid":   yes_bid,
                    "yes_ask":   yes_ask,
                })

    if not kalshi_props:
        print("  No open Kalshi prop markets found.")
        return [], {}
    print(f"  Kalshi prop markets collected: {len(kalshi_props)}")

    # 2. Build needed-teams set
    needed_teams: set = set()
    for series in prop_series:
        try:
            evts = fetch_kalshi_events(series)
        except Exception:
            continue
        for evt in evts:
            away_raw, home_raw = parse_event_fn(evt.get("event_ticker", ""), abbr_map)
            lu = build_city_lookup(abbr_map)
            for raw in [away_raw, home_raw]:
                if raw:
                    needed_teams.add(_norm(raw))
                    full = lu.get(_norm(raw))
                    if full:
                        needed_teams.add(_norm(full))

    # 3. Fetch Odds API event list
    try:
        odds_events = fetch_odds_events_list(odds_sport)
    except Exception as e:
        print(f"  ERROR — Odds API events list: {e}")
        return [], {}

    # 4. Build player prop consensus index
    player_lookup = build_all_player_props(odds_sport, odds_events, needed_teams or None, markets=prop_markets)
    if not player_lookup:
        print("  No player prop data available — skipping.")
        return []

    # 5. Match Kalshi markets → consensus fair value
    edges:      List[dict] = []
    seen_edges: set        = set()

    for kp in kalshi_props:
        prop_type    = kp["prop_type"]
        search_title = kp["mkt_title"] or kp["ev_title"]

        prop_sublookup = {
            k: v[prop_type]
            for k, v in player_lookup.items()
            if prop_type in v
        }
        if not prop_sublookup:
            continue

        matched = _find_player_in_title(search_title, prop_sublookup)
        if matched is None:
            continue
        # Captured before any reassignment below -- the per-line dicts matched
        # onto in the exact-alternate-line-match step don't carry nb_fit /
        # commence_time (only the top-level entry from build_all_player_props does).
        _nb_fit = matched.get("nb_fit")
        _commence = matched.get("commence_time")
        # Doubleheader name collision (see build_all_player_props) — skip rather
        # than risk pricing this Kalshi market off the other game's line.
        if matched.get("_dh_collision"):
            print(f"  ⚠ prop  {matched.get('player','?'):<25} SKIPPED — doubleheader name collision (can't game-match)")
            continue

        # ── Exact alternate-line match ───────────────────────────────────────
        # If Pinnacle posts a real line at the Kalshi threshold (incl. alternate
        # 0.5 / 2.5 / 3.5 milestone lines), price off that real number rather than
        # the default main line. No Poisson extrapolation — Pinnacle's posted
        # no-vig price at the exact line IS the fair value. Requires Pinnacle to
        # be present on that line so it stays a true sharp anchor.
        _kthresh    = kp["threshold"]
        _line_ents  = matched.get("lines", {})
        for _Lval, _lent in _line_ents.items():
            if int(_Lval) == int(_kthresh) and "pinnacle" in _lent.get("books_detail", {}):
                matched = _lent
                break

        # ── Ghost-edge safeguard: integer boundary check ─────────────────────
        # Pinnacle uses half-integer lines (4.5, 5.5, 6.5 …).
        # Kalshi stores integer floor_strikes (5, 6, 7 …) where floor_strike=k
        # means the market resolves YES if the stat is ≥ k+1 (i.e. > k, i.e. ≥ k+1).
        # A Pinnacle "over 5.5" line has int(5.5)=5, which matches Kalshi floor_strike=5.
        # A Pinnacle "over 4.5" line has int(4.5)=4, which does NOT match floor_strike=5.
        # The old 0.5 raw-diff tolerance allowed int(4.5)=4 → floor_strike=5 through
        # because abs(4.5-5.0)=0.5 ≤ 0.5. That caused the Canning ghost edge:
        # Pinnacle "over 4.5 Ks" (P(X≥5)) matched to Kalshi "6+" (P(X≥6)), giving a
        # fake 8.9% edge. The integer boundary check is the correct gate: int(line)
        # must equal int(threshold). abs(5.5-5.0)=0.5 would have passed the old check
        # too, but int(5.5)=5 == int(5.0)=5 is the right semantic test.
        pin_line      = matched["line"]
        kalshi_thresh = kp["threshold"]
        # WNBA self-averaging stats may extrapolate off Pinnacle's anchor to a
        # nearby Kalshi rung (see WNBA_EXTRAP_* notes) instead of being rejected.
        # The Poisson fair below is then a genuine extrapolation, not a wrong-line
        # ghost edge. MLB and far/lumpy WNBA lines stay on the strict guard.
        _wnba_extrap = (
            mkt_type_label == "wnba_prop"
            and prop_type in WNBA_EXTRAP_PROP_TYPES
            and abs(pin_line - kalshi_thresh) <= WNBA_EXTRAP_MAX_RUNGS
        )
        if int(pin_line) != int(kalshi_thresh) and not _wnba_extrap:
            print(
                f"  ✗ prop  {matched.get('player','?'):<25} REJECTED — line mismatch  "
                f"pin={pin_line} (int={int(pin_line)})  kalshi_thresh={kalshi_thresh} (int={int(kalshi_thresh)})"
            )
            continue
        # Also catch any remaining large absolute differences (>0.6) as an extra
        # belt-and-suspenders guard in case a book uses full-integer lines.
        if abs(pin_line - kalshi_thresh) > 0.6 and not _wnba_extrap:
            print(
                f"  ✗ prop  {matched.get('player','?'):<25} REJECTED — abs line diff too large  "
                f"pin={pin_line}  kalshi_thresh={kalshi_thresh}  diff={abs(pin_line - kalshi_thresh):.2f}"
            )
            continue
        if _wnba_extrap and int(pin_line) != int(kalshi_thresh):
            print(
                f"  ↝ WNBA extrap  {matched.get('player','?'):<25} "
                f"pin={pin_line} → kalshi={kalshi_thresh}  (Poisson from anchor, Δ={abs(pin_line-kalshi_thresh):.1f} rungs)"
            )
        print(
            f"  → prop line match  {matched.get('player','?'):<25} "
            f"pin={pin_line}  kalshi_thresh={kalshi_thresh}  (int boundary: {int(pin_line)}=={int(kalshi_thresh)})"
        )

        lam       = matched["lambda"]
        threshold = kp["threshold"]
        t_int     = int(threshold)

        if _wnba_extrap:
            # Normal extrapolation off Pinnacle's anchor (see WNBA_EXTRAP_*).
            # Continuity boundaries: P(X >= n) == P(X > n-0.5) == eval at int+0.5.
            _cv = WNBA_EXTRAP_CV.get(prop_type, 0.45)
            _nf = normal_prop_fair_over(int(matched["line"]) + 0.5,
                                        matched["over_prob"],
                                        int(threshold) + 0.5, _cv)
            fair_over = _nf if _nf is not None else (1.0 - poisson_cdf(t_int, lam))
        else:
            fair_over = 1.0 - poisson_cdf(t_int, lam)
        fair_under = 1.0 - fair_over

        # ── TB-only: parallel Negative Binomial fair value (diagnostic) ─────
        # Computed alongside the live Poisson fair_over above but NEVER used
        # for fair/edge/raw_edge below -- those stay Poisson-only. Purely for
        # forward comparison against realized outcomes once enough bets
        # accumulate, same "capture now, decide later" pattern as the 2026-
        # 07-15 de-vig study. None when no 2-line fit was available (see
        # fit_neg_binom_two_point) or this isn't a TB prop.
        nb_fair_over  = None
        nb_fair_under = None
        if _nb_fit is not None:
            _nb_mu, _nb_r = _nb_fit
            nb_fair_over  = 1.0 - neg_binom_cdf(t_int, _nb_mu, _nb_r)
            nb_fair_under = 1.0 - nb_fair_over

        # ── Prop lambda sanity check ─────────────────────────────────────────
        # Convert each retail book's lambda to the same Kalshi threshold and
        # compare against Pinnacle. A >12pp gap means Pinnacle's line is likely
        # stale (pre-lineup early price). Shadow the bet rather than flagging.
        _per_book_lams = matched.get("per_book_lambdas", {})
        _retail_lams   = {b: l for b, l in _per_book_lams.items() if b != "pinnacle"}
        _prop_sanity_shadow = False
        if "pinnacle" in _per_book_lams and _retail_lams:
            _retail_fairs = [1.0 - poisson_cdf(t_int, l) for l in _retail_lams.values()]
            _avg_retail   = sum(_retail_fairs) / len(_retail_fairs)
            _gap          = abs(fair_over - _avg_retail)
            if _gap > PROP_LAMBDA_SANITY_GAP:
                _retail_str = "  ".join(
                    f"{b}={1.0 - poisson_cdf(t_int, l):.1%}"
                    for b, l in _retail_lams.items()
                )
                print(
                    f"  ⚠ prop  {matched.get('player','?'):<25} "
                    f"SANITY SHADOW — Pinnacle fair={fair_over:.1%} vs retail avg={_avg_retail:.1%} "
                    f"(gap={_gap:.1%} > {PROP_LAMBDA_SANITY_GAP:.0%})  [{_retail_str}]"
                )
                _prop_sanity_shadow = True

        yes_bid, yes_ask = kp["yes_bid"], kp["yes_ask"]
        yes_raw  = fair_over  - yes_ask
        no_raw   = fair_under - (1.0 - yes_bid)

        yes_fee  = KALSHI_FEE_RATE * fair_over  * (1 - yes_ask)
        no_fee   = KALSHI_FEE_RATE * fair_under * yes_bid
        yes_adj  = (yes_raw - yes_fee) * (1 - EV_HAIRCUT)
        no_adj   = (no_raw  - no_fee)  * (1 - EV_HAIRCUT)

        best_adj = max(yes_adj, no_adj)

        # Full prop snapshot — capture current Kalshi + Pinnacle for ALL markets
        # regardless of threshold so the UI can show live prices on logged bets
        # even after the edge has closed.
        _snap_ticker = kp.get("ticker", "")
        if _snap_ticker:
            prop_snapshot[f"{_snap_ticker}|YES"] = {
                "adj_edge": round(yes_adj, 4), "kalshi": round(yes_ask, 4),
                "fair": round(fair_over, 4),   "edge_pct": round(yes_adj * 100, 1),
            }
            prop_snapshot[f"{_snap_ticker}|NO"] = {
                "adj_edge": round(no_adj, 4), "kalshi": round(1 - yes_bid, 4),
                "fair": round(fair_under, 4), "edge_pct": round(no_adj * 100, 1),
            }

        # TB NO-side experiment: let a slight UNDER edge through even below the
        # global threshold (TB is fully shadowed downstream, so this only builds a
        # risk-free sample — see TB_NO_EXPERIMENT_THRESHOLD).
        _tb_no_exp = (_snap_ticker.upper().startswith("KXMLBTB")
                      and no_adj >= TB_NO_EXPERIMENT_THRESHOLD)

        if best_adj < EDGE_THRESHOLD and not _tb_no_exp:
            continue
        if best_adj > PROP_MAX_EDGE:   # 15% cap — tighter than game-line MAX_EDGE (20%); very large prop edges are almost always a mismatch
            print(
                f"  ✗ prop  {matched.get('player','?'):<25} REJECTED — adj edge {best_adj:.1%} > PROP_MAX_EDGE {PROP_MAX_EDGE:.0%}"
            )
            continue

        # ── Large Pinnacle-only edge → DEFER (wait for retail) ────────────────
        # Pinnacle stays the driver: small edges fund on Pinnacle alone. But a
        # LARGE edge with no retail book posted yet is indistinguishable from a
        # stale/soft early Pinnacle line (the Marte case). Rather than fund a
        # possible fake or shadow a possible real edge, we SKIP it this cycle.
        # On a later scan, once DK/FanDuel post: if they confirm, it funds then;
        # if they disagree, _validate_book_consensus rejects it; if Pinnacle was
        # stale, it corrects and the edge is simply gone. Real early Pinnacle
        # edges survive and get funded; stale ones never do.
        if not _retail_lams and best_adj >= PROP_PIN_ONLY_MAX_EDGE:
            print(
                f"  ⏳ prop  {matched.get('player','?'):<25} "
                f"DEFERRED — Pinnacle-only edge {best_adj:.1%} ≥ {PROP_PIN_ONLY_MAX_EDGE:.0%}, "
                f"waiting for retail (DK/FD) to post & confirm before funding"
            )
            continue

        if _tb_no_exp:
            # experiment: force the UNDER side regardless of which side scored best
            side, k_side, f_side, raw_edge, adj_edge = "NO",  1 - yes_bid, fair_under, no_raw,  no_adj
            nb_f_side = nb_fair_under
        elif yes_adj >= no_adj:
            side, k_side, f_side, raw_edge, adj_edge = "YES", yes_ask,     fair_over,  yes_raw, yes_adj
            nb_f_side = nb_fair_over
        else:
            side, k_side, f_side, raw_edge, adj_edge = "NO",  1 - yes_bid, fair_under, no_raw,  no_adj
            nb_f_side = nb_fair_under

        # Price floor — same rule as game-line scanner
        if k_side < MIN_KALSHI_PRICE:
            continue

        matchup   = search_title.split(":")[0].strip() if ":" in search_title else search_title
        dedup_key = (kp["ticker"], side)
        if dedup_key in seen_edges:
            continue
        seen_edges.add(dedup_key)

        books_detail = matched.get("books_detail", {})

        # ── Book-consensus validation ─────────────────────────────────────
        consensus_valid, consensus_reason = _validate_book_consensus(
            books_detail, side, k_side
        )
        if not consensus_valid:
            book_str_rej = "  ".join(f"{b}={p:.1%}" for b, p in sorted(books_detail.items()))
            print(
                f"  ✗ prop  {matched['player']:<25} {side}  "
                f"REJECTED — {consensus_reason}  [{book_str_rej}]"
            )
            continue

        confidence   = _book_confidence(books_detail)
        book_str     = "  ".join(f"{b}={p:.1%}" for b, p in sorted(books_detail.items()))
        print(
            f"  ✓ prop  {matched['player']:<25} {side}  "
            f"kalshi={k_side:.1%}  fair={f_side:.1%}  "
            f"raw={raw_edge:+.1%}  adj={adj_edge:+.1%}  conf={confidence:.0%}  "
            f"[{book_str}]  [{consensus_reason}]"
        )

        # ── Consensus YES/NO for prop (over = YES side) ─────────────────
        prop_cons_yes = round(matched["over_prob"], 4)
        prop_cons_no  = round(1.0 - matched["over_prob"], 4)
        prop_per_book = _build_per_book_novig(books_detail, side)

        edges.append({
            "ticker":               kp["ticker"],
            "title":                f"{search_title} (line {matched['line']})",
            "kalshi_line":          kp.get("floor_strike"),
            "matchup":              matchup,
            "side":                 side,
            "kalshi":               round(k_side, 4),
            "fair":                 round(f_side, 4),
            "raw_edge":             round(raw_edge, 4),
            "edge":                 round(adj_edge, 4),
            "tb_no_experiment":     _tb_no_exp,   # flagged below global threshold for the TB under-side test
            "confidence":           confidence,
            "mkt_type":             mkt_type_label,
            "commence_time":        _commence,   # Pinnacle game start — CLV freeze needs it (WNBA/NBA tickers omit the time)
            "pin_line":             matched["line"],
            "prop_type":            prop_type,
            "books_used":           matched.get("books_used", []),
            "books_detail":         books_detail,
            "per_book_novig":       prop_per_book,
            # De-vig study (2026-07-14): TB-ONLY per user directive — Strikeouts
            # left as-is (Shin ≈ proportional on K's centered lines, ~0.1pp, so
            # capturing K adds nothing). For TB (lopsided, favorite-longshot
            # suspect) capture the raw per-book over/under odds at the matched
            # Pinnacle line + line + prop_type. With the Kalshi threshold (in the
            # ticker) this replays the full pipeline offline under proportional/
            # Shin/power for an out-of-sample calibration comparison. Live TB fair
            # above is still proportional — capture is observability only.
            "devig_inputs":         (
                {"line": matched["line"], "prop_type": prop_type,
                 "raw_odds": matched.get("raw_odds", {})}
                if prop_type == "batter_total_bases" else None
            ),
            # Negative Binomial overdispersion diagnostic (2026-07-19), TB-only.
            # Parallel fair value computed from a dispersion parameter fit off
            # Pinnacle's own two independently-posted lines -- NOT used for
            # fair/edge/raw_edge/staking above, which stay Poisson-only. None
            # when fewer than 2 usable lines were posted or the two-line fit
            # found no reliable overdispersion signal (see
            # fit_neg_binom_two_point). Compare against realized outcomes once
            # enough bets accumulate before ever wiring this into live pricing.
            "nb_fair":              round(nb_f_side, 4) if nb_f_side is not None else None,
            "nb_fit_params":        (
                {"mu": round(_nb_fit[0], 4), "r": round(_nb_fit[1], 4)}
                if _nb_fit is not None else None
            ),
            "consensus_yes":        prop_cons_yes,
            "consensus_no":         prop_cons_no,
            "consensus_yes_american": prob_to_american(prop_cons_yes),
            "consensus_no_american":  prob_to_american(prop_cons_no),
            "consensus_prob":       prop_cons_yes,   # legacy alias
            "is_valid_consensus":   True,
            "consensus_reason":     consensus_reason,  # e.g. "Confirmed by Pinnacle + FanDuel"
            "kalshi_price_ts":      now_utc.isoformat(),
            "sanity_shadow":        _prop_sanity_shadow,
        })

    # Sort by confidence × adj_edge, correlation-control, top N
    edges.sort(key=lambda x: x["confidence"] * x["edge"], reverse=True)
    edges = _apply_correlation_control(edges)
    edges = edges[:TOP_BETS_PER_CYCLE]

    if not edges:
        print(f"  No player-prop edges ≥ {EDGE_THRESHOLD:.0%} (adj.) found.")
    else:
        print(f"\n  Top {len(edges)} prop edge(s) — sorted by confidence × adj.EV:")
        print(f"  {'PROP TYPE':<22} {'PLAYER / TITLE':<42} {'SIDE':<4} {'RAW':>6} {'ADJ':>6} {'CONF':>5}")
        for e in edges:
            stars = "★★★" if e["confidence"] >= 0.8 else "★★" if e["confidence"] >= 0.5 else "★"
            print(
                f"  {e['prop_type']:<22} {e['title'][:42]:<42} "
                f"{e['side']:<4} {e['raw_edge']:>+5.1%} "
                f"\033[92m{e['edge']:>+5.1%}\033[0m  {stars}"
            )
    return edges, prop_snapshot


def scan_nba_player_props() -> List[dict]:
    """Scan Kalshi NBA player-prop markets (points, assists, 3-pointers)."""
    return scan_player_props(
        odds_sport      = "basketball_nba",
        abbr_map        = NBA_ABBR,
        prop_series     = NBA_PROP_SERIES,
        prop_markets    = NBA_PLAYER_PROP_MARKETS,
        sport_label     = "NBA",
        mkt_type_label  = "nba_prop",
        parse_event_fn  = _parse_nba_event,
    )


def scan_wnba_player_props() -> List[dict]:
    """Scan Kalshi WNBA player-prop markets (points, rebounds, assists).
    WNBA tickers use the same no-time-component format as NBA — reuse
    _parse_nba_event for team-abbreviation splitting."""
    return scan_player_props(
        odds_sport      = "basketball_wnba",
        abbr_map        = WNBA_ABBR,
        prop_series     = WNBA_PROP_SERIES,
        prop_markets    = WNBA_PLAYER_PROP_MARKETS,
        sport_label     = "WNBA",
        mkt_type_label  = "wnba_prop",
        parse_event_fn  = _parse_nba_event,
    )


# ── MLS (soccer) — moneyline (3-way) + total goals ───────────────────────────
# Added 2026-07-23 with the credits freed by terminating Total Bases. Soccer is
# structurally different from MLB/WNBA (a THIRD moneyline outcome — the draw —
# and low-count Poisson totals), so it gets a dedicated scanner rather than
# threading special-cases through scan_sport (keeps the proven MLB/WNBA path
# untouched). Pinnacle is the sole fair-value anchor, same as every other
# market. Launches SHADOW-first (KXMLS in SHADOW_MARKETS, UI side) until it
# earns a CLV/calibration track record.
#
# Kalshi MLS market shapes (verified live):
#   KXMLSGAME-<date><HOMEAWAY>-<CODE>  three binary markets per game:
#       -<HOMECODE>  YES = home team wins
#       -<AWAYCODE>  YES = away team wins
#       -TIE         YES = draw
#   KXMLSTOTAL-<date><HOMEAWAY>-<n>    over 0.5 / 1.5 / … / 5.5 goals (YES = over)
#
# DOUBLE CHANCE is automatic — it's the NO side of a team/tie market, which the
# YES/NO edge logic already evaluates:
#   NO on away-team market  = home win OR draw   (1X)
#   NO on home-team market  = away win OR draw   (X2)
#   NO on TIE               = either team wins    (12, "no draw")
# Correct 3-way de-vig (fair probs summing to 1) is what makes the NO/DC price
# right, so no separate market wiring is needed.
#
# code → (Pinnacle team name for odds matching, ESPN team id for logos/live).
# All 30 MLS teams; Kalshi codes taken from live KXMLSGAME market tickers.
MLS_TEAMS: Dict[str, Dict[str, str]] = {
    "ATL":  {"pin": "Atlanta United FC",      "espn": "18418"},
    "ATX":  {"pin": "Austin FC",              "espn": "20906"},
    "MTL":  {"pin": "CF Montreal",            "espn": "9720"},
    "CLT":  {"pin": "Charlotte FC",           "espn": "21300"},
    "CHI":  {"pin": "Chicago Fire",           "espn": "182"},
    "COL":  {"pin": "Colorado Rapids",        "espn": "184"},
    "CLB":  {"pin": "Columbus Crew SC",       "espn": "183"},
    "DCU":  {"pin": "D.C. United",            "espn": "193"},
    "CIN":  {"pin": "FC Cincinnati",          "espn": "18267"},
    "DAL":  {"pin": "FC Dallas",              "espn": "185"},
    "HOU":  {"pin": "Houston Dynamo",         "espn": "6077"},
    "MIA":  {"pin": "Inter Miami CF",         "espn": "20232"},
    "LAG":  {"pin": "LA Galaxy",              "espn": "187"},
    "LAFC": {"pin": "Los Angeles FC",         "espn": "18966"},
    "MIN":  {"pin": "Minnesota United FC",    "espn": "17362"},
    "NSH":  {"pin": "Nashville SC",           "espn": "18986"},
    "NE":   {"pin": "New England Revolution", "espn": "189"},
    "NYC":  {"pin": "New York City FC",       "espn": "17606"},
    "NYRB": {"pin": "New York Red Bulls",     "espn": "190"},
    "ORL":  {"pin": "Orlando City SC",        "espn": "12011"},
    "PHI":  {"pin": "Philadelphia Union",     "espn": "10739"},
    "POR":  {"pin": "Portland Timbers",       "espn": "9723"},
    "RSL":  {"pin": "Real Salt Lake",         "espn": "4771"},
    "SD":   {"pin": "San Diego FC",           "espn": "22529"},
    "SJ":   {"pin": "San Jose Earthquakes",   "espn": "191"},
    "SEA":  {"pin": "Seattle Sounders FC",    "espn": "9726"},
    "SKC":  {"pin": "Sporting Kansas City",   "espn": "186"},
    "STL":  {"pin": "St. Louis City SC",      "espn": "21812"},
    "TOR":  {"pin": "Toronto FC",             "espn": "7318"},
    "VAN":  {"pin": "Vancouver Whitecaps FC", "espn": "9727"},
}
_MLS_PIN_TO_CODE = {v["pin"]: k for k, v in MLS_TEAMS.items()}
MLS_MAX_TOTAL_RUNGS = 2.5   # only price Kalshi over-x.5 rungs within this many
                            # goals of Pinnacle's line — the Poisson tail gets
                            # unreliable far out (mild overdispersion), same
                            # guard philosophy as WNBA_EXTRAP_MAX_RUNGS.


SOCCER_LOOKAHEAD_H = 12   # zero-game short-circuit: only spend the paid odds
                          # call when a league has a game commencing within this
                          # many hours (checked via the FREE /events endpoint).


def fetch_soccer_odds(odds_key: str) -> Tuple[List[dict], str]:
    """Fetch Pinnacle h2h (3-way) + totals for a soccer league in one call
    (2 credits). Returns (games, remaining) where each game is:
        {"home", "away", "commence_time",
         "ml": {team_name: shin_fair_prob},   # home & away (draw is separate)
         "draw": shin_fair_prob,
         "total": {"line": L, "over_prob": p} or None}
    ml/draw are the 3-way Shin de-vigged probabilities (sum to 1)."""
    r = requests.get(f"{ODDS_BASE}/sports/{odds_key}/odds", params={
        "apiKey":     ODDS_API_KEY,
        "bookmakers": "pinnacle",
        "markets":    "h2h,totals",
        "oddsFormat": "american",
    }, timeout=15)
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining", "?")
    games: List[dict] = []
    for g in r.json():
        home, away = g.get("home_team", ""), g.get("away_team", "")
        if not home or not away:
            continue
        entry: dict = {"home": home, "away": away,
                       "commence_time": g.get("commence_time"),
                       "ml": {}, "draw": None, "total": None}
        for bm in g.get("bookmakers", []):
            if bm.get("key") != "pinnacle":
                continue
            for mk in bm.get("markets", []):
                outs = {o["name"]: o["price"] for o in mk.get("outcomes", []) if o.get("price") is not None}
                if mk.get("key") == "h2h" and home in outs and away in outs and "Draw" in outs:
                    ph, pa, pd = shin_devig_multi([american_to_implied(outs[home]),
                                                   american_to_implied(outs[away]),
                                                   american_to_implied(outs["Draw"])])
                    entry["ml"] = {home: ph, away: pa}
                    entry["draw"] = pd
                elif mk.get("key") == "totals":
                    over  = next((o for o in mk.get("outcomes", []) if o.get("name") == "Over"), None)
                    under = next((o for o in mk.get("outcomes", []) if o.get("name") == "Under"), None)
                    if over and under and over.get("point") is not None:
                        io = american_to_implied(over["price"]); iu = american_to_implied(under["price"])
                        if io + iu > 0:
                            entry["total"] = {"line": float(over["point"]), "over_prob": io / (io + iu)}
        if entry["ml"] and entry["draw"] is not None:
            games.append(entry)
    return games, remaining


def soccer_has_upcoming_game(odds_key: str, within_h: int = SOCCER_LOOKAHEAD_H) -> bool:
    """Zero-game short-circuit. Uses the FREE Odds-API /events endpoint (0
    credits) to decide whether a league has a game worth scanning right now —
    True if any game commences within `within_h` hours (or started in the last
    3h, so an in-progress slate still refreshes). Fails OPEN (returns True) on a
    transient error so a blip never silently blacks out a live slate."""
    try:
        events = fetch_odds_events_list(odds_key)
    except Exception:
        return True
    now = datetime.now(timezone.utc)
    from datetime import timedelta as _td
    lo, hi = now - _td(hours=3), now + _td(hours=within_h)
    for e in events:
        ct = e.get("commence_time")
        if not ct:
            continue
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if lo <= dt <= hi:
            return True
    return False


# ── Generalized soccer matching ──────────────────────────────────────────────
_MONTH3 = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def _soccer_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _soccer_ticker_date(ticker: str):
    """date object from the KX…-26JUL25… token, or None."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker or "")
    if not m or m.group(2) not in _MONTH3:
        return None
    from datetime import date as _date
    return _date(2000 + int(m.group(1)), _MONTH3[m.group(2)], int(m.group(3)))

def _soccer_name_match(kdisp: str, pinname: str) -> bool:
    """True if a Kalshi display name confidently refers to a Pinnacle team.
    Substring either way, or every significant (len>2) Kalshi token present in
    the Pinnacle name. Deliberately conservative — ghost matches are prevented
    at the game level (both teams must match a unique game), so this only needs
    to avoid matching genuinely-different clubs."""
    a, b = _soccer_norm(kdisp), _soccer_norm(pinname)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ta = [t for t in re.findall(r"[a-z]+", kdisp.lower()) if len(t) > 2]
    tb = set(re.findall(r"[a-z]+", pinname.lower()))
    return bool(ta) and all(t in tb for t in ta)

def _soccer_title_teams(evt: dict) -> Optional[Tuple[str, str]]:
    """Parse the two team display names from a KX soccer event title, e.g.
    'Riestra vs Boca Juniors' or 'Palmeiras vs Atletico Mineiro: Total Goals'
    or 'San Jose vs Los Angeles G Winner?'. Present on both GAME and TOTAL
    events, so it's the one uniform team source for name-matched leagues."""
    t = evt.get("title", "") or ""
    t = re.sub(r":\s*Total Goals\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*Winner\??\s*$", "", t, flags=re.I)
    parts = re.split(r"\s+vs\.?\s+", t, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    return (a, b) if a and b else None

def _soccer_safe_match(kteams: Tuple[str, str], kdate, games: List[dict]) -> Optional[dict]:
    """Match a Kalshi game (two display names + ticker date) to a Pinnacle game.
    SAFE BY CONSTRUCTION: requires BOTH Kalshi teams to name-match, to DIFFERENT
    Pinnacle teams, within ±1 day (absorbs the UTC-vs-local date offset on late
    kickoffs), and returns a match ONLY if it is UNIQUE. Any ambiguity → None
    (no bet). Verified against live Argentina/Brazil slates: 100% precision,
    correctly disambiguating same-city clubs (two Estudiantes, Rosario Central
    vs Central Cordoba, etc.). Games Pinnacle hasn't priced yet return None and
    are simply skipped — there's no line to devig against anyway."""
    if kdate is None:
        return None
    cands = []
    for g in games:
        cd = g.get("commence_time")
        try:
            pdt = datetime.fromisoformat(cd.replace("Z", "+00:00")).date() if cd else None
        except (ValueError, AttributeError):
            pdt = None
        if pdt is None or abs((pdt - kdate).days) > 1:
            continue
        m0 = [n for n in (g["home"], g["away"]) if _soccer_name_match(kteams[0], n)]
        m1 = [n for n in (g["home"], g["away"]) if _soccer_name_match(kteams[1], n)]
        if m0 and m1 and set(m0) != set(m1):
            cands.append(g)
    return cands[0] if len(cands) == 1 else None

def _mls_teams_from_event(evt: dict) -> Optional[Tuple[str, str]]:
    """(home_code, away_code) for a KXMLS event from its non-TIE market ticker
    suffixes (each market ticker ends in its own unambiguous team code)."""
    codes = []
    for m in (evt.get("markets") or []):
        suffix = m.get("ticker", "").split("-")[-1]
        if suffix and suffix != "TIE" and suffix in MLS_TEAMS:
            codes.append(suffix)
    seen: set = set()
    codes = [c for c in codes if not (c in seen or seen.add(c))]
    return (codes[0], codes[1]) if len(codes) == 2 else None


def _soccer_price_market(mkt: dict, fair: float, mkt_type: str, title: str,
                         matchup: str, commence_time, pin_line, threshold,
                         now_utc: datetime) -> Optional[dict]:
    """Shared YES/NO edge builder for all soccer — identical EV math to
    scan_sport (fee then haircut), single Pinnacle anchor. The NO side is where
    double chance lives (NO team = the other team or draw)."""
    prices = kalshi_prices(mkt)
    if prices is None:
        return None
    yes_bid, yes_ask = prices
    yes_raw = fair - yes_ask
    no_raw  = (1 - fair) - (1 - yes_bid)
    yes_adj = (yes_raw - KALSHI_FEE_RATE * fair       * (1 - yes_ask)) * (1 - EV_HAIRCUT)
    no_adj  = (no_raw  - KALSHI_FEE_RATE * (1 - fair) * yes_bid)       * (1 - EV_HAIRCUT)
    best_adj = max(yes_adj, no_adj)
    if best_adj < EDGE_THRESHOLD or best_adj > MAX_EDGE:
        return None
    if yes_adj >= no_adj:
        side, k_side, f_side, raw_edge, adj = "YES", yes_ask, fair, yes_raw, yes_adj
    else:
        side, k_side, f_side, raw_edge, adj = "NO", 1 - yes_bid, 1 - fair, no_raw, no_adj
    if k_side < MIN_KALSHI_PRICE:
        return None
    books_detail = {"pinnacle": round(fair, 4)}
    cons_yes, cons_no = round(fair, 4), round(1 - fair, 4)
    return {
        "ticker": mkt.get("ticker", ""), "title": title, "kalshi_line": threshold,
        "matchup": matchup, "side": side, "kalshi": round(k_side, 4), "fair": round(f_side, 4),
        "raw_edge": round(raw_edge, 4), "edge": round(adj, 4), "confidence": 1.0,
        "mkt_type": mkt_type, "pin_line": pin_line, "fair_source": "exact",
        "books_used": ["pinnacle"], "books_detail": books_detail,
        "per_book_novig": _build_per_book_novig(books_detail, side),
        "consensus_yes": cons_yes, "consensus_no": cons_no,
        "consensus_yes_american": prob_to_american(cons_yes),
        "consensus_no_american": prob_to_american(cons_no),
        "consensus_prob": cons_yes, "is_valid_consensus": True,
        "consensus_reason": "Pinnacle (sole sharp anchor)",
        "kalshi_price_ts": now_utc.isoformat(), "commence_time": commence_time,
    }


def _soccer_event_live_or_expired(evt: dict, now_utc: datetime) -> bool:
    """True if the game has started / the event expired (skip it)."""
    mkts = evt.get("markets") or []
    exp_str = (mkts[0].get("expected_expiration_time") or mkts[0].get("close_time")) if mkts else ""
    game_dt = _parse_ticker_game_time(evt.get("event_ticker", ""))
    if game_dt is None and exp_str:
        try:
            from datetime import timedelta as _tde
            game_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00")) - _tde(hours=2.5)
        except (ValueError, AttributeError):
            game_dt = None
    return game_dt is not None and now_utc >= game_dt


# Config-driven soccer leagues. `match`: 'map' = hardcoded code→Pinnacle-name
# map (MLS only — its Kalshi display names are odd abbreviations like
# "Los Angeles G" that name-matching can't resolve). 'name' = the safe runtime
# name-matcher (works wherever Kalshi uses full club names — Argentina, Brazil).
# All launch SHADOW-first (KX* prefixes in SHADOW_MARKETS on the UI side).
SOCCER_LEAGUES: List[dict] = [
    {"label": "MLS", "prefix": "mls", "match": "map", "team_map": MLS_TEAMS,
     "game_series": "KXMLSGAME", "total_series": "KXMLSTOTAL",
     "odds_key": "soccer_usa_mls", "espn": "usa.1"},
    {"label": "Argentina Primera", "prefix": "arg", "match": "name",
     "game_series": "KXARGPREMDIVGAME", "total_series": "KXARGPREMDIVTOTAL",
     "odds_key": "soccer_argentina_primera_division", "espn": "arg.1"},
    {"label": "Brazil Serie A", "prefix": "bra", "match": "name",
     "game_series": "KXBRASILEIROGAME", "total_series": "KXBRASILEIROTOTAL",
     "odds_key": "soccer_brazil_campeonato", "espn": "bra.1"},
]


def scan_soccer(cfg: dict, games: Optional[List[dict]] = None) -> Tuple[List[dict], str]:
    """Generalized soccer scanner (moneyline 3-way + total goals) for one league
    config. Zero-game short-circuit first (free /events), then one paid Pinnacle
    fetch feeds both markets. Same edge-dict format as scan_sport. Double chance
    is the NO side of team/tie markets (priced automatically by the 3-way fair)."""
    label, prefix = cfg["label"], cfg["prefix"]
    ml_type, tot_type = f"{prefix}_moneyline", f"{prefix}_total"
    remaining = "?"

    if games is None:
        if not soccer_has_upcoming_game(cfg["odds_key"]):
            print(f"  {label}: no game within {SOCCER_LOOKAHEAD_H}h — skip (0 credits)")
            return [], remaining
        try:
            games, remaining = fetch_soccer_odds(cfg["odds_key"])
        except Exception as exc:
            print(f"  ERROR — {label} book odds: {exc}")
            return [], remaining

    print(f"\n{'═'*70}\n  {label} — Moneyline (3-way) & Total Goals  —  "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'═'*70}")
    print(f"  Pinnacle {label} games: {len(games)}")

    is_map = cfg["match"] == "map"
    tmap = cfg.get("team_map") or {}
    index = {frozenset({g["home"], g["away"]}): g for g in games} if is_map else {}
    now_utc = datetime.now(timezone.utc)
    edges: List[dict] = []

    def find_game(evt: dict) -> Optional[dict]:
        if is_map:
            codes = _mls_teams_from_event(evt)
            if not codes:
                m = re.search(cfg["total_series"] + r"-\d{2}[A-Z]{3}\d{2}([A-Z]+)-",
                              evt.get("event_ticker", "").upper())
                if not m:
                    return None
                seg, found = m.group(1), None
                for n in (4, 3, 2):
                    a, h = seg[:n], seg[n:]
                    if a in tmap and h in tmap:
                        found = (a, h); break
                if not found:
                    return None
                codes = found
            hp, ap = tmap.get(codes[0], {}).get("pin"), tmap.get(codes[1], {}).get("pin")
            return index.get(frozenset({hp, ap})) if hp and ap else None
        # name strategy — teams from the event title, safe unique match
        tt = _soccer_title_teams(evt)
        if not tt:
            return None
        return _soccer_safe_match(tt, _soccer_ticker_date(evt.get("event_ticker", "")), games)

    def outcome_fair(mkt: dict, game: dict):
        """(fair_prob, title) for a moneyline market, or None."""
        suffix = mkt.get("ticker", "").split("-")[-1]
        if suffix == "TIE":
            return (game["draw"], "Draw (match ends level)")
        if is_map:
            name = tmap.get(suffix, {}).get("pin")
            if name and name in game["ml"]:
                return (game["ml"][name], f"{name} to win")
            return None
        disp = mkt.get("yes_sub_title", "")
        for name in (game["home"], game["away"]):
            if _soccer_name_match(disp, name):
                return (game["ml"][name], f"{name} to win")
        return None

    # ── Moneyline ────────────────────────────────────────────────────────────
    try:
        ml_events = fetch_kalshi_events(cfg["game_series"])
    except Exception as e:
        print(f"  ERROR — Kalshi {cfg['game_series']}: {e}")
        ml_events = []
    for evt in ml_events:
        if _soccer_event_live_or_expired(evt, now_utc):
            continue
        game = find_game(evt)
        if not game:
            continue
        matchup = f"{game['away']} @ {game['home']}"
        for mkt in (evt.get("markets") or []):
            of = outcome_fair(mkt, game)
            if not of or of[0] is None:
                continue
            e = _soccer_price_market(mkt, of[0], ml_type, of[1], matchup,
                                     game.get("commence_time"), None, None, now_utc)
            if e:
                edges.append(e)

    # ── Total goals (Poisson off Pinnacle's line) ────────────────────────────
    try:
        tot_events = fetch_kalshi_events(cfg["total_series"])
    except Exception as e:
        print(f"  ERROR — Kalshi {cfg['total_series']}: {e}")
        tot_events = []
    for evt in tot_events:
        if _soccer_event_live_or_expired(evt, now_utc):
            continue
        game = find_game(evt)
        if not game or not game.get("total"):
            continue
        lam = fit_poisson_lambda(game["total"]["line"], game["total"]["over_prob"])
        if lam is None:
            continue
        matchup = f"{game['away']} @ {game['home']}"
        for mkt in (evt.get("markets") or []):
            m = re.search(r"([0-9]+\.5)", mkt.get("yes_sub_title", "") or mkt.get("title", ""))
            if not m:
                continue
            line = float(m.group(1))
            if abs(line - game["total"]["line"]) > MLS_MAX_TOTAL_RUNGS:
                continue
            e = _soccer_price_market(mkt, poisson_over_prob(line, lam), tot_type,
                                     f"Over {line} goals", matchup,
                                     game.get("commence_time"), game["total"]["line"],
                                     line, now_utc)
            if e:
                edges.append(e)

    # Correlation control — one best moneyline + one best total per game (the 3
    # ML outcomes are mutually exclusive; adjacent rungs correlate).
    edges.sort(key=lambda x: x["edge"], reverse=True)
    best: Dict[Tuple[str, str], dict] = {}
    for e in edges:
        key = (e["matchup"], "total" if e["mkt_type"].endswith("_total") else "moneyline")
        best.setdefault(key, e)
    edges = sorted(best.values(), key=lambda x: x["edge"], reverse=True)
    print(f"  {label} edges ≥{EDGE_THRESHOLD:.0%}: {len(edges)}")
    return edges, remaining


def scan_mls(mls_index: Optional[List[dict]] = None) -> Tuple[List[dict], str]:
    """Back-compat wrapper — MLS is SOCCER_LEAGUES[0]."""
    return scan_soccer(SOCCER_LEAGUES[0], games=mls_index)


def scan_mls(mls_index: Optional[Dict[str, dict]] = None) -> Tuple[List[dict], str]:
    """Scan Kalshi MLS moneyline (KXMLSGAME, 3-way) and total-goals (KXMLSTOTAL)
    markets against Pinnacle's Shin-devigged 3-way probabilities and Poisson
    total model. Returns (edges, credits_remaining). Same edge-dict format as
    scan_sport so the UI consumes it identically. Double chance rides free on
    the NO side of team/tie markets (see module comment above)."""
    print(f"\n{'═'*70}")
    print(f"  MLS — Moneyline (3-way) & Total Goals  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*70}")

    remaining = "?"
    if mls_index is None:
        try:
            mls_index, remaining = fetch_mls_odds()
        except Exception as exc:
            print(f"  ERROR — MLS book odds: {exc}")
            return [], remaining
    print(f"  Pinnacle MLS games: {len(mls_index)}")

    def _find_game(home_code: str, away_code: str) -> Optional[dict]:
        hp = MLS_TEAMS.get(home_code, {}).get("pin")
        ap = MLS_TEAMS.get(away_code, {}).get("pin")
        if not hp or not ap:
            return None
        return mls_index.get(frozenset({hp, ap}))

    edges: List[dict] = []
    now_utc = datetime.now(timezone.utc)

    def _price_market(mkt: dict, fair: float, mkt_type: str, title: str,
                      matchup: str, commence_time, pin_line, threshold) -> Optional[dict]:
        """Shared YES/NO edge builder — identical EV math to scan_sport."""
        prices = kalshi_prices(mkt)
        if prices is None:
            return None
        yes_bid, yes_ask = prices
        yes_raw = fair - yes_ask
        no_raw  = (1 - fair) - (1 - yes_bid)
        yes_fee = KALSHI_FEE_RATE * fair       * (1 - yes_ask)
        no_fee  = KALSHI_FEE_RATE * (1 - fair) * yes_bid
        yes_adj = (yes_raw - yes_fee) * (1 - EV_HAIRCUT)
        no_adj  = (no_raw  - no_fee)  * (1 - EV_HAIRCUT)
        best_adj = max(yes_adj, no_adj)
        if best_adj < EDGE_THRESHOLD or best_adj > MAX_EDGE:
            return None
        if yes_adj >= no_adj:
            side, k_side, f_side, raw_edge, adj = "YES", yes_ask, fair, yes_raw, yes_adj
        else:
            side, k_side, f_side, raw_edge, adj = "NO", 1 - yes_bid, 1 - fair, no_raw, no_adj
        if k_side < MIN_KALSHI_PRICE:
            return None
        books_detail = {"pinnacle": round(fair, 4)}   # fair = canonical YES prob
        # For NO bets, express per-book as P(Kalshi YES) — already fair here.
        per_book_novig = _build_per_book_novig(books_detail, side)
        cons_yes = round(fair, 4)
        cons_no  = round(1 - fair, 4)
        return {
            "ticker":               mkt.get("ticker", ""),
            "title":                title,
            "kalshi_line":          threshold,
            "matchup":              matchup,
            "side":                 side,
            "kalshi":               round(k_side, 4),
            "fair":                 round(f_side, 4),
            "raw_edge":             round(raw_edge, 4),
            "edge":                 round(adj, 4),
            "confidence":           1.0,          # single sharp anchor (Pinnacle)
            "mkt_type":             mkt_type,
            "pin_line":             pin_line,
            "fair_source":          "exact",
            "books_used":           ["pinnacle"],
            "books_detail":         books_detail,
            "per_book_novig":       per_book_novig,
            "consensus_yes":        cons_yes,
            "consensus_no":         cons_no,
            "consensus_yes_american": prob_to_american(cons_yes),
            "consensus_no_american":  prob_to_american(cons_no),
            "consensus_prob":       cons_yes,
            "is_valid_consensus":   True,
            "consensus_reason":     "Pinnacle (sole sharp anchor)",
            "kalshi_price_ts":      now_utc.isoformat(),
            "commence_time":        commence_time,
        }

    def _event_live_or_expired(evt: dict) -> bool:
        """True if the game has started or the event expired (skip it)."""
        mkts = evt.get("markets") or []
        exp_str = ""
        if mkts:
            exp_str = mkts[0].get("expected_expiration_time") or mkts[0].get("close_time") or ""
        game_dt = _parse_ticker_game_time(evt.get("event_ticker", ""))
        if game_dt is None and exp_str:
            try:
                from datetime import timedelta as _tde
                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                game_dt = exp_dt - _tde(hours=2.5)   # soccer ≈ 2h + buffer
            except (ValueError, AttributeError):
                game_dt = None
        if game_dt is not None and now_utc >= game_dt:
            return True
        return False

    # ── Moneyline (KXMLSGAME) — 3-way ────────────────────────────────────────
    try:
        ml_events = fetch_kalshi_events("KXMLSGAME")
    except Exception as e:
        print(f"  ERROR — Kalshi KXMLSGAME: {e}")
        ml_events = []
    for evt in ml_events:
        if _event_live_or_expired(evt):
            continue
        codes = _mls_teams_from_event(evt)
        if not codes:
            continue
        home_code, away_code = codes
        game = _find_game(home_code, away_code)
        if not game:
            continue
        home_name, away_name = game["home"], game["away"]
        matchup = f"{away_name} @ {home_name}"
        outcome_fair = {
            home_code: (game["ml"].get(home_name), f"{home_name} to win"),
            away_code: (game["ml"].get(away_name), f"{away_name} to win"),
            "TIE":     (game["draw"],              "Draw (match ends level)"),
        }
        for mkt in (evt.get("markets") or []):
            suffix = mkt.get("ticker", "").split("-")[-1]
            fair_title = outcome_fair.get(suffix)
            if not fair_title or fair_title[0] is None:
                continue
            fair, title = fair_title
            e = _price_market(mkt, fair, "mls_moneyline", title, matchup,
                              game.get("commence_time"), None, None)
            if e:
                edges.append(e)

    # ── Total goals (KXMLSTOTAL) — Poisson off Pinnacle's line ───────────────
    try:
        tot_events = fetch_kalshi_events("KXMLSTOTAL")
    except Exception as e:
        print(f"  ERROR — Kalshi KXMLSTOTAL: {e}")
        tot_events = []
    for evt in tot_events:
        if _event_live_or_expired(evt):
            continue
        codes = _mls_teams_from_event(evt)
        # KXMLSTOTAL markets carry no team codes; parse from the event segment.
        if not codes:
            m = re.search(r"KXMLSTOTAL-\d{2}[A-Z]{3}\d{2}([A-Z]+)-", evt.get("event_ticker", "").upper())
            if not m:
                continue
            seg = m.group(1)
            # split against known codes (longest-first to disambiguate)
            found = None
            for n in (4, 3, 2):
                a, h = seg[:n], seg[n:]
                if a in MLS_TEAMS and h in MLS_TEAMS:
                    found = (a, h)
                    break
            if not found:
                continue
            codes = found
        game = _find_game(codes[0], codes[1])
        if not game or not game.get("total"):
            continue
        lam = fit_poisson_lambda(game["total"]["line"], game["total"]["over_prob"])
        if lam is None:
            continue
        matchup = f"{game['away']} @ {game['home']}"
        for mkt in (evt.get("markets") or []):
            # Kalshi over-x.5 line lives in yes_sub_title ("Over 2.5 goals scored")
            sub = mkt.get("yes_sub_title", "") or mkt.get("title", "")
            m = re.search(r"([0-9]+\.5)", sub)
            if not m:
                continue
            line = float(m.group(1))
            if abs(line - game["total"]["line"]) > MLS_MAX_TOTAL_RUNGS:
                continue   # too far from the anchor — Poisson tail unreliable
            fair = poisson_over_prob(line, lam)
            e = _price_market(mkt, fair, "mls_total", f"Over {line} goals",
                              matchup, game.get("commence_time"),
                              game["total"]["line"], line)
            if e:
                edges.append(e)

    edges.sort(key=lambda x: x["edge"], reverse=True)

    # Correlation control: the 3 moneyline outcomes (home/away/tie) plus their
    # NO/double-chance sides are all mutually-exclusive positions on one game,
    # and adjacent total-goal rungs move together. Never stack correlated soccer
    # bets — keep only the single best edge per (game, group), same as MLB's
    # moneyline cap=1. One moneyline + one total per game may still both surface
    # (weakly correlated, consistent with how MLB flags total & ML together).
    best_per_group: Dict[Tuple[str, str], dict] = {}
    for e in edges:   # already sorted best-first, so first seen per key wins
        group = "total" if e["mkt_type"] == "mls_total" else "moneyline"
        key = (e["matchup"], group)
        if key not in best_per_group:
            best_per_group[key] = e
    edges = sorted(best_per_group.values(), key=lambda x: x["edge"], reverse=True)

    print(f"  MLS edges ≥{EDGE_THRESHOLD:.0%}: {len(edges)}")
    return edges, remaining


# ── Main ──────────────────────────────────────────────────────────────────────
def run_once() -> int:
    mlb_edges, _ = scan_sport(
        label         = "MLB — Run Line & Totals",
        spread_series = "KXMLBSPREAD",
        total_series  = "KXMLBTOTAL",
        odds_sport    = "baseball_mlb",
        abbr_map      = MLB_ABBR,
        spread_std    = MLB_SPREAD_STD,
        total_std     = MLB_TOTAL_STD,
    )
    nba_edges, _ = scan_sport(
        label         = "NBA — Spread & Totals",
        spread_series = "KXNBASPREAD",
        total_series  = "KXNBATOTAL",
        odds_sport    = "basketball_nba",
        abbr_map      = NBA_ABBR,
        spread_std    = NBA_SPREAD_STD,
        total_std     = NBA_TOTAL_STD,
    )
    total = len(mlb_edges) + len(nba_edges)
    print(f"\n  Total edges flagged (adj. ≥{EDGE_THRESHOLD*100:.0f}%): {total}")
    return total


def main():
    parser = argparse.ArgumentParser(description="Kalshi EV scanner — MLB & NBA")
    parser.add_argument(
        "--loop", type=int, default=0, metavar="SECONDS",
        help="Re-run every N seconds (0 = run once)",
    )
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║          Kalshi EV Scanner  —  MLB & NBA  (v2)                  ║")
    print("║  Sources : Pinnacle (70%) + DraftKings (20%) + FanDuel (10%)    ║")
    print(f"║  EV      : raw edge × {1-EV_HAIRCUT:.0%} haircut ≥ {EDGE_THRESHOLD*100:.0f}% to flag              ║")
    print(f"║  Output  : Top {TOP_BETS_PER_CYCLE} bets, max {MAX_BETS_PER_GROUP} per game group                      ║")
    print("║  Markets : KXMLBSPREAD, KXMLBTOTAL, KXNBASPREAD, KXNBATOTAL    ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    if args.loop <= 0:
        run_once()
    else:
        print(f"  Loop mode: every {args.loop}s  (Ctrl-C to stop)\n")
        try:
            while True:
                run_once()
                print(f"\n  Sleeping {args.loop}s…\n")
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\n  Stopped.")


if __name__ == "__main__":
    main()
