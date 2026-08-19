"""
Market-freshness watcher.

Thesis (see the 2026-08-19 CFO memo): strikeouts weren't special -- K worked
for months because Kalshi's own pricing hadn't caught up to Pinnacle yet, and
stopped working the moment it did (the Aug 4-5 repricing). That gap, a market
young enough that it hasn't been arbitraged tight, is the reproducible asset.
This module looks for the next one on purpose instead of finding out about it
by accident.

Two phases:

  1. DISCOVERY (free -- 0 Odds API credits). Poll Kalshi's public /series
     endpoint for the Sports category and diff against every ticker we've
     ever seen. Kalshi doesn't expose a creation timestamp, so "new" means
     "new to us" -- first run bootstraps the seen-set without alerting on
     anything (there are ~3,500 Sports series already live; day one isn't a
     discovery, it's a baseline).

  2. WATCH (paid, tightly bounded). A newly-discovered series only gets an
     automated Pinnacle price check if BOTH:
       a) its ticker suffix matches the game-line pattern this codebase
          already recognizes across every sport it scans (GAME/ML/SPREAD/
          TOTAL) -- the one market shape that maps generically onto Odds
          API's h2h/spreads/totals without having to guess a prop's market
          key, and
       b) its sport prefix is one we already carry a team abbreviation map
          for (MLB, WNBA, NFL, NHL today -- see SPORT_ABBR_MAPS below; add
          an entry there, same shape as MLB_ABBR, to extend further. NFL/NHL
          use NFL_SPREAD_STD/NFL_TOTAL_STD/NHL_SPREAD_STD/NHL_TOTAL_STD,
          which are textbook approximations, not calibrated -- fine for a
          "does this look interesting" check, not for ever funding a bet).
     Everything else that's newly discovered -- a new prop type, or a sport
     we don't have team-matching infra for -- gets flagged in the same
     Discord alert with no automated check. Deciding the right Odds-API
     market key for a genuinely new prop type is exactly the judgment call
     that added Outs/WNBA/Soccer/Tennis to this scanner; the watcher's job
     is making sure nothing new goes unnoticed, not replacing that judgment.

     Each actively-watched series gets checked once/day via scan_sport() --
     reusing the same de-vig, team-matching, and edge math as the live
     scanner, not a parallel implementation -- for WATCH_DAYS days. Crossing
     ALERT_RAW_EDGE_PP at any point fires one Discord alert (never repeats
     for the same series) and the series drops out of active watch; it
     otherwise ages out silently after WATCH_DAYS with no edge found.

Read-only except for the one Discord side effect. Never stakes anything,
never adds a market to the funded scan on its own -- purely a "look here"
signal for a human to act on, same as the daily K tripwire.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import kalshi_ev_scanner as s

KALSHI_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"

WATCH_DAYS             = 14   # how long a newly-discovered series stays on paid watch
ALERT_RAW_EDGE_PP      = 4.0  # raw edge -- matches what every funded K bet historically cleared
MAX_ACTIVE_WATCH       = 8    # hard cap on concurrent paid-checked series (bounds credit spend)
MAX_FLAG_ONLY_PER_RUN  = 15   # cap how many no-auto-check flags one alert lists (readability)

# Ticker prefix -> (odds_sport, abbr_map, spread_std, total_std). Only sports
# with a real team-abbreviation map get automated checks (scan_sport needs it
# to match Kalshi's team names against Pinnacle's). Add an entry here (build
# the abbr map the same shape as MLB_ABBR first) to extend coverage.
SPORT_ABBR_MAPS = {
    "KXMLB":  ("baseball_mlb",         s.MLB_ABBR,  s.MLB_SPREAD_STD,  s.MLB_TOTAL_STD),
    "KXWNBA": ("basketball_wnba",      s.WNBA_ABBR, s.WNBA_SPREAD_STD, s.WNBA_TOTAL_STD),
    "KXNFL":  ("americanfootball_nfl", s.NFL_ABBR,  s.NFL_SPREAD_STD,  s.NFL_TOTAL_STD),
    "KXNHL":  ("icehockey_nhl",        s.NHL_ABBR,  s.NHL_SPREAD_STD,  s.NHL_TOTAL_STD),
}

# Series we already actively scan (funded, shadow, or paused-but-built) --
# never worth re-flagging as a "new" opportunity even though they're still
# new to a from-scratch seen-set on first bootstrap.
ALREADY_COVERED_PREFIXES = {
    "KXMLBTOTAL", "KXMLBOUTS", "KXMLBKS", "KXMLBSPREAD", "KXMLBGAME",
    "KXMLBTB", "KXMLBHR", "KXMLBHIT", "KXMLBRBI",
    "KXWNBASPREAD", "KXWNBATOTAL", "KXWNBAPTS", "KXWNBAREB", "KXWNBAAST",
    "KXMLS", "KXARGPREMDIV", "KXBRASILEIRO", "KXLIGAMX", "KXCONMEBOLSUD",
    "KXCHLLDP", "KXATPMATCH", "KXWTAMATCH",
}

_GAME_LINE_KIND = {"GAME": "h2h", "ML": "h2h", "SPREAD": "spread", "TOTAL": "total"}


def _game_line_kind(series_ticker: str) -> Optional[str]:
    t = series_ticker.upper()
    for suffix, kind in _GAME_LINE_KIND.items():
        if t.endswith(suffix):
            return kind
    return None


def _sport_prefix(series_ticker: str) -> Optional[str]:
    t = series_ticker.upper()
    for prefix in SPORT_ABBR_MAPS:
        if t.startswith(prefix):
            return prefix
    return None


def _get(url: str) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state_file: str, state: dict) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(state_file) or ".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, state_file)
    except Exception as exc:
        print(f"  WARNING: could not save freshness-watch state: {exc}")


def discover_and_watch(state_file: str, send_discord: Callable[[dict, str], bool]) -> dict:
    """One run of the watcher. Call this on its own slow (daily) cadence from
    the main scan loop. Returns a small summary dict for logging/status."""
    now = datetime.now(timezone.utc)
    state = _load_state(state_file)
    seen: dict = state.setdefault("seen", {})       # ticker -> {first_seen}
    watch: dict = state.setdefault("watch", {})      # ticker -> {until, best_edge, alerted, kind, odds_sport}

    try:
        series_list = _get(f"{KALSHI_PUBLIC}/series?category=Sports&limit=200").get("series", [])
    except Exception as exc:
        print(f"  Freshness watcher: series fetch failed ({exc}), skipping this run")
        return {"error": str(exc)}

    bootstrap = not seen
    new_tickers = []
    for sr in series_list:
        tk = sr.get("ticker", "")
        if not tk or tk in seen:
            continue
        seen[tk] = {"first_seen": now.isoformat(), "title": sr.get("title", "")}
        if not bootstrap:
            new_tickers.append(sr)

    flag_only = []      # newly discovered, no automated check possible
    added_to_watch = 0
    for sr in new_tickers:
        tk = sr.get("ticker", "")
        if any(tk.upper().startswith(p) for p in ALREADY_COVERED_PREFIXES):
            continue
        kind = _game_line_kind(tk)
        prefix = _sport_prefix(tk)
        eligible = kind is not None and prefix is not None
        if eligible and len(watch) < MAX_ACTIVE_WATCH:
            odds_sport, abbr_map, spread_std, total_std = SPORT_ABBR_MAPS[prefix]
            watch[tk] = {
                "started": now.isoformat(),
                "until": (now + timedelta(days=WATCH_DAYS)).isoformat(),
                "kind": kind, "odds_sport": odds_sport,
                "title": sr.get("title", ""), "best_edge": None, "alerted": False,
            }
            added_to_watch += 1
        else:
            flag_only.append(sr)

    # --- Paid phase: check everything currently in-window ---
    checked, alerts_fired = 0, []
    expired = [tk for tk, w in watch.items()
               if datetime.fromisoformat(w["until"]) <= now]
    for tk, w in list(watch.items()):
        if datetime.fromisoformat(w["until"]) <= now:
            continue  # expired this run, swept below
        prefix = _sport_prefix(tk)
        if prefix is None:
            continue
        odds_sport, abbr_map, spread_std, total_std = SPORT_ABBR_MAPS[prefix]
        kwargs = dict(label=f"Freshness watch: {tk}", odds_sport=odds_sport,
                      abbr_map=abbr_map, spread_std=spread_std, total_std=total_std)
        # Route the single new series into whichever scan_sport() slot
        # matches its detected kind; the other two stay unset (None/"").
        kwargs["spread_series"] = tk if w["kind"] == "spread" else None
        kwargs["total_series"]  = tk if w["kind"] == "total"  else None
        kwargs["ml_series"]     = tk if w["kind"] == "h2h"    else ""
        try:
            edges, _stats, _snap = s.scan_sport(**kwargs)
        except Exception as exc:
            print(f"  Freshness watcher: check failed for {tk}: {exc}")
            continue
        checked += 1
        best = max((e.get("raw_edge", 0.0) for e in edges), default=0.0) * 100
        if w["best_edge"] is None or best > w["best_edge"]:
            w["best_edge"] = best
        if best >= ALERT_RAW_EDGE_PP and not w["alerted"]:
            w["alerted"] = True
            alerts_fired.append((tk, w, best))

    for tk in expired:
        watch.pop(tk, None)

    # --- Alert: new-but-unwatchable series + any watch-list edge hits ---
    if flag_only or alerts_fired:
        fields = []
        for sr in flag_only[:MAX_FLAG_ONLY_PER_RUN]:
            fields.append({
                "name": sr.get("ticker", "?"),
                "value": (sr.get("title", "") or "(no title)")[:200] + "\n_no auto-check — new prop type or unmapped sport, needs a human look_",
                "inline": False,
            })
        for tk, w, best in alerts_fired:
            fields.append({
                "name": f"🎯 {tk}",
                "value": f"{w['title']}\nBest raw edge seen: **{best:.1f}pp** (watching since {w.get('started','?')[:10]})",
                "inline": False,
            })
        embed = {
            "title": "Market freshness watcher — new listings this run",
            "color": 0xC69A4E if alerts_fired else 0x55606B,
            "fields": fields[:25],  # Discord embed field cap
            "footer": {"text": f"{len(new_tickers)} new series seen · {added_to_watch} added to paid watch · "
                                f"{len(flag_only)} flagged only · {checked} checked this run"},
        }
        content = (f"🎯 Freshness watch: {len(alerts_fired)} series cleared {ALERT_RAW_EDGE_PP}pp raw edge"
                   if alerts_fired else
                   f"👀 Freshness watch: {len(flag_only)} new Kalshi sports series need a manual look")
        send_discord(embed, content)

    _save_state(state_file, state)
    return {
        "bootstrap": bootstrap, "total_series_seen": len(seen),
        "new_this_run": len(new_tickers), "added_to_watch": added_to_watch,
        "flag_only": len(flag_only), "checked": checked,
        "alerts_fired": len(alerts_fired), "active_watch": len(watch),
    }
