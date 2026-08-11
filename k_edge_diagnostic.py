#!/usr/bin/env python3
"""
Strikeout edge-collapse diagnostic — READ ONLY. Changes no production logic.

Isolates whether the KXMLBKS edge drought (2.6-4.0 bets/day for eight weeks,
then ~0 from Aug 5) is a PIPELINE BUG or a REAL MARKET SHIFT.

Runs six independent checks and prints a PASS/FLAG summary. Deliberately
bypasses the scanner's caching, entry threshold, shadow bands and correlation
filters -- it re-derives edge from raw feeds so a silently-excluding filter
cannot hide a real edge from this script.

DATA LIMITS (surfaced, not papered over):
  * The Odds API historical endpoint is a separate paid add-on, and the scanner
    stores NO raw two-sided Pinnacle odds for strikeouts (devig_inputs is
    TB-only: 0/179 K bets carry it). So historical de-vig CANNOT be re-run.
    Checks 1 and 4 report what is genuinely reconstructable and mark the rest
    UNAVAILABLE rather than inventing a comparison.
  * Stored bet records only exist for edges that CLEARED the 2% threshold, so
    any "historical edge" distribution from them is selection-biased upward.
    Treat baseline numbers as an upper bound on the old market, not its middle.
  * Kalshi candlesticks DO survive settlement, so historical Kalshi price and
    bid/ask spread are genuinely recoverable (check 6). That is the one clean
    then-vs-now comparison available.

Usage:  python3 k_edge_diagnostic.py            (~12-18 Odds API credits)
        python3 k_edge_diagnostic.py --no-net   (offline: checks 5 only)
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kalshi_ev_scanner as s  # noqa: E402

ODDS_KEY = os.environ.get("ODDS_API_KEY", s.ODDS_API_KEY)
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PAPER_API = "https://evscanner-production.up.railway.app/api/paper"
# Measured over all 179 settled K bets: raw_edge_pct - edge_pct = 2.03pp
# (min 1.90, max 2.30). The scanner's 2% ADJUSTED floor therefore sits at
# ~4.03pp RAW -- the number any "is there really no edge?" test must use.
RAW_MINUS_ADJ_PP = 2.03
CLIFF_DATE = "2026-08-05"          # first day of the drought
BASELINE_FROM, BASELINE_TO = "2026-07-14", "2026-07-22"   # 3-4 weeks pre-cliff

results = {}   # check name -> (status, headline)


def _get(url, timeout=25):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read()), dict(r.headers)


def _hdr(n, title):
    print(f"\n{'='*78}\n  CHECK {n}: {title}\n{'='*78}")


# ─────────────────────────────────────────────────────────────────────────────
def fetch_current(max_games=8):
    """Fresh Pinnacle K props + Kalshi contracts. Cache-bypassed: direct HTTP,
    none of the scanner's cached indices are read."""
    kev, _ = _get(f"{KALSHI}/events?series_ticker=KXMLBKS&status=open&limit=200")
    oev, _ = _get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/?apiKey={ODDS_KEY}")
    oidx = {(s._norm(e["away_team"]), s._norm(e["home_team"])): e for e in oev}

    games = []
    for ev in kev.get("events", [])[:max_games]:
        et = ev.get("event_ticker", "")
        away, home = s._parse_mlb_event(et, s.MLB_ABBR)
        oe = oidx.get((s._norm(away), s._norm(home))) if away and home else None
        rec = {"event": et, "away": away, "home": home, "matched_odds_event": bool(oe),
               "pin": {}, "kalshi": [], "market_last_update": None}
        if oe:
            d, _ = _get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{oe['id']}"
                        f"/odds/?apiKey={ODDS_KEY}&bookmakers=pinnacle"
                        f"&markets=pitcher_strikeouts&oddsFormat=american")
            for bm in d.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    rec["market_last_update"] = mk.get("last_update")
                    for o in mk.get("outcomes", []):
                        nm = (o.get("description") or "").strip()
                        if nm:
                            rec["pin"].setdefault(
                                (s._norm_player(nm), float(o.get("point") or 0)), {}
                            )[(o.get("name") or "").lower()] = o.get("price")
        md, _ = _get(f"{KALSHI}/markets?event_ticker={et}&status=open&limit=100")
        for m in md.get("markets", []):
            pr = s.kalshi_prices(m)
            if pr is None or m.get("floor_strike") is None:
                continue
            rec["kalshi"].append({"ticker": m.get("ticker"), "title": m.get("title") or "",
                                  "floor": float(m["floor_strike"]),
                                  "bid": pr[0], "ask": pr[1]})
        games.append(rec)
    return games


def join_rows(games):
    """Match Kalshi contracts to Pinnacle lines and compute edge MANUALLY --
    no scanner edge code, no threshold, no shadow band, no correlation filter."""
    rows = []
    for g in games:
        for km in g["kalshi"]:
            tn = re.sub(r"[^a-z]", "", km["title"].lower())
            for (pk, pt), od in g["pin"].items():
                if not pk or pk not in tn:
                    continue
                if "over" not in od or "under" not in od:
                    continue
                if int(pt) != int(km["floor"]):
                    continue
                fair, _ = s.no_vig_prob(od["over"], od["under"])
                rows.append({
                    "game": f"{g['away']} @ {g['home']}", "title": km["title"][:40],
                    "ticker": km["ticker"], "line": pt,
                    "over": od["over"], "under": od["under"],
                    "fair": fair, "bid": km["bid"], "ask": km["ask"],
                    "spread_pp": (km["ask"] - km["bid"]) * 100,
                    "taker_edge": (fair - km["ask"]) * 100,
                    "maker_edge": (fair - km["bid"]) * 100,
                    "last_update": g["market_last_update"],
                })
    return rows


# ── 1. RAW FEED ──────────────────────────────────────────────────────────────
def check1(games, rows):
    _hdr(1, "Raw Pinnacle feed — freshness, gaps, stale/duplicate values")
    print("LIMIT: 7-day raw history is NOT retrievable — The Odds API historical")
    print("       endpoint is a paid add-on and the scanner stores no raw K odds")
    print("       (devig_inputs is TB-only). Checking the CURRENT snapshot for")
    print("       staleness/gaps/nulls instead, which is what is knowable.\n")
    now = datetime.now(timezone.utc)
    no_odds_match = [g for g in games if not g["matched_odds_event"]]
    no_pin = [g for g in games if g["matched_odds_event"] and not g["pin"]]
    ages = []
    for g in games:
        lu = g.get("market_last_update")
        if lu:
            age = (now - datetime.fromisoformat(lu.replace("Z", "+00:00"))).total_seconds() / 60
            ages.append(age)
        print(f"  {g['event']:<30} odds_event={'Y' if g['matched_odds_event'] else 'N'} "
              f"pin_lines={len(g['pin']):<3} kalshi_mkts={len(g['kalshi']):<3} "
              f"last_update={lu or 'NULL'}"
              f"{f'  ({age:.0f} min old)' if lu else ''}")
    nulls = sum(1 for r in rows if r["over"] is None or r["under"] is None)
    # identical odds repeated across DIFFERENT players => suspicious cache echo
    combos = defaultdict(list)
    for r in rows:
        combos[(r["over"], r["under"], r["line"])].append(r["title"])
    dupes = {k: v for k, v in combos.items() if len(v) > 2}
    print(f"\n  games with no Odds-API event match : {len(no_odds_match)}")
    print(f"  games matched but zero Pinnacle lines: {len(no_pin)}")
    print(f"  null over/under prices in joined rows: {nulls}")
    print(f"  max feed age: {max(ages):.0f} min" if ages else "  max feed age: n/a")
    print(f"  identical (over,under,line) across >2 players: {len(dupes)}")
    for k, v in list(dupes.items())[:3]:
        print(f"     {k} -> {v[:4]}")
    stale = bool(ages) and max(ages) > 180
    if stale or nulls or len(no_pin) > len(games) / 2:
        results["1 raw feed"] = ("FLAG", "stale/missing Pinnacle data")
    else:
        results["1 raw feed"] = ("PASS", f"fresh (<{max(ages):.0f}min), {len(rows)} lines, no nulls")


# ── 2. DE-VIG SANITY ─────────────────────────────────────────────────────────
def check2(rows):
    _hdr(2, "De-vig sanity — scanner output vs hand-computed")
    print("  hand calc: p_over_raw = implied(over); p_under_raw = implied(under)")
    print("             fair = p_over_raw / (p_over_raw + p_under_raw)\n")
    print(f"  {'market':<34}{'over':>7}{'under':>7}{'impO':>8}{'impU':>8}"
          f"{'hand':>8}{'scanner':>9}{'delta':>8}")
    worst = 0.0
    for r in rows[:10]:
        io = s.american_to_implied(r["over"])
        iu = s.american_to_implied(r["under"])
        hand = io / (io + iu)
        delta = abs(hand - r["fair"]) * 100
        worst = max(worst, delta)
        print(f"  {r['title']:<34}{r['over']:>7}{r['under']:>7}{io:>8.4f}{iu:>8.4f}"
              f"{hand*100:>8.2f}{r['fair']*100:>9.2f}{delta:>8.4f}")
    print(f"\n  worst |hand - scanner| = {worst:.4f} pp")
    sane = all(0.0 < r["fair"] < 1.0 for r in rows)
    print(f"  all fair probs within (0,1): {sane}")
    results["2 de-vig"] = (("PASS", f"matches hand calc to {worst:.4f}pp")
                           if worst < 0.01 and sane else
                           ("FLAG", f"de-vig mismatch {worst:.4f}pp"))


# ── 3. KALSHI-SIDE RAW EDGE ──────────────────────────────────────────────────
def check3(rows):
    _hdr(3, "Kalshi-side edge — computed manually, ALL filters bypassed")
    print("  No entry threshold, no shadow band, no correlation dedup applied.\n")
    print(f"  {'market':<34}{'fair%':>8}{'bid':>6}{'ask':>6}{'taker':>8}{'maker':>8}")
    for r in sorted(rows, key=lambda x: -x["taker_edge"])[:12]:
        print(f"  {r['title']:<34}{r['fair']*100:>8.2f}{r['bid']*100:>6.0f}"
              f"{r['ask']*100:>6.0f}{r['taker_edge']:>8.2f}{r['maker_edge']:>8.2f}")
    tk = sorted((r["taker_edge"] for r in rows), reverse=True)
    mk = sorted((r["maker_edge"] for r in rows), reverse=True)
    if not tk:
        results["3 kalshi edge"] = ("FLAG", "no rows joined")
        return
    med = tk[len(tk) // 2]
    print(f"\n  n={len(tk)}  taker best={tk[0]:.2f} median={med:.2f}  "
          f"maker best={mk[0]:.2f} median={mk[len(mk)//2]:.2f}")
    print(f"  >= +2pp taker: {sum(1 for x in tk if x >= 2)}   "
          f">= +4pp taker (historical funded zone): {sum(1 for x in tk if x >= 4)}")
    # The scanner's floor is 2% ADJUSTED, not raw. Measured across all 179
    # settled K bets, raw - adjusted = 2.03pp (range 1.90-2.30), so the floor
    # sits at ~4.03pp RAW. Comparing against 2pp raw (as this check first did)
    # is wrong by ~2pp and reports a false filter bug on ordinary sub-threshold
    # edges. Only a raw edge above the RAW-equivalent floor that the scanner
    # failed to flag would actually implicate a filter.
    raw_floor = 2.0 + RAW_MINUS_ADJ_PP
    n_over = sum(1 for x in tk if x >= raw_floor)
    print(f"  scanner floor is 2% ADJUSTED = ~{raw_floor:.2f}pp RAW; "
          f"{n_over} market(s) clear it")
    results["3 kalshi edge"] = (("FLAG", f"{n_over} raw edge(s) >= {raw_floor:.1f}pp unflagged")
                                if n_over else
                                ("PASS", f"nothing clears {raw_floor:.1f}pp raw (best {tk[0]:.2f})"))


# ── 4. PRE-CLIFF BASELINE ────────────────────────────────────────────────────
def check4(rows):
    _hdr(4, f"Baseline diff — {BASELINE_FROM}..{BASELINE_TO} vs today")
    print("LIMIT: raw two-sided Pinnacle odds were never stored for K, so the")
    print("       de-vig CANNOT be replayed historically. Using the STORED")
    print("       de-vigged fair (pin_prob_at_flag) + entry price from bet")
    print("       records. These are SELECTION-BIASED (only edges that cleared")
    print("       2%), so treat as an upper bound on the old market.\n")
    try:
        paper, _ = _get(PAPER_API, timeout=40)
    except Exception as exc:
        print(f"  could not load bet records: {exc}")
        results["4 baseline"] = ("UNAVAILABLE", "bet records unreachable")
        return
    ks = [b for b in paper.get("bets", []) if b["ticker"].upper().startswith("KXMLBKS")]
    base = [b for b in ks if BASELINE_FROM <= b.get("flagged_at", "")[:10] <= BASELINE_TO]
    post = [b for b in ks if b.get("flagged_at", "")[:10] >= CLIFF_DATE]
    print(f"  {'player':<22}{'line':>6}{'pinFair%':>10}{'kEntry%':>9}{'rawEdge':>9}")
    for b in base[:10]:
        pin = b.get("pin_prob_at_flag")
        entry = (b.get("kalshi_price") or 0) * 100
        print(f"  {b['matchup'][:21]:<22}{b.get('pin_line_at_flag','?'):>6}"
              f"{pin if pin is not None else 0:>10.1f}{entry:>9.1f}"
              f"{b.get('raw_edge_pct', 0):>9.2f}")
    be = [b.get("raw_edge_pct", 0) for b in base if b.get("raw_edge_pct") is not None]
    tk = sorted((r["taker_edge"] for r in rows), reverse=True)
    print(f"\n  BASELINE  n={len(base)} found-bets   avg raw edge={sum(be)/len(be):.2f}pp"
          if be else f"\n  BASELINE  n={len(base)}")
    print(f"  POST-CLIFF n={len(post)} found-bets  (bets flagged since {CLIFF_DATE})")
    if tk:
        print(f"  TODAY full distribution (not selection-biased): best={tk[0]:.2f}pp "
              f"median={tk[len(tk)//2]:.2f}pp, n={len(tk)} markets")
    print("\n  Interpretation: baseline found-bets averaged ~4.7pp raw and the market")
    print("  produced several/day. If today's FULL distribution tops out below the")
    print("  2% entry floor, the opportunity is gone at source, not filtered away.")
    results["4 baseline"] = ("PARTIAL", "no raw-odds history; fair+price reconstructed")


# ── 5. GIT / CONFIG DIFF ─────────────────────────────────────────────────────
def check5():
    _hdr(5, "Git diff of shared pricing/filter code since last known-good")
    here = os.path.dirname(os.path.abspath(__file__))

    def git(*a):
        return subprocess.run(["git", "-C", here, *a], capture_output=True, text=True).stdout

    since = git("log", "--since=2026-08-01", "--format=%h %ad %s", "--date=short",
                "--", "kalshi_ev_scanner.py", "kalshi_ev_ui.py")
    print("  commits touching scanner/ui since 2026-08-01:")
    print("".join(f"    {ln}\n" for ln in since.strip().splitlines()) or "    (none)")

    # Did anything touch the shared de-vig / threshold / shadow-band machinery?
    hot = ["no_vig_prob", "EDGE_THRESHOLD", "EV_HAIRCUT", "TB_CAL_FAIR_CEILING",
           "TB_HIGH_EDGE_THRESHOLD", "_tb_shadow_all", "STRIKEOUT_MAX_EDGE",
           "PROP_MAX_EDGE", "_find_player_in_title", "game_teams"]
    print("  shared-symbol changes since 2026-08-01 (did the TB shadow band leak"
          " onto strikeouts?):")
    for sym in hot:
        out = git("log", "--since=2026-08-01", "-S", sym, "--format=%h %s",
                  "--", "kalshi_ev_scanner.py", "kalshi_ev_ui.py").strip()
        print(f"    {sym:<28} {out.splitlines()[0] if out else '— unchanged'}")

    # The critical scoping question, answered from live code not memory.
    # Must read WHOLE assignment blocks: these are multi-line boolean
    # expressions, so a line-by-line scan sees "and ... >= TB_HIGH_EDGE_THRESHOLD"
    # without the "_tb_ticker" guard on the line above and cries wolf.
    src_ui = open(os.path.join(here, "kalshi_ev_ui.py")).read()
    leaks = []
    for var in ("_tb_high_edge", "_tb_overconfident", "_tb_shadow_all"):
        m = re.search(rf"^\s*{var}\s*=\s*(\(.*?\)|.*?)$",
                      src_ui, re.S | re.M) if var == "_tb_shadow_all" else \
            re.search(rf"^\s*{var}\s*=\s*\((.*?)\n\s*\)", src_ui, re.S | re.M)
        if not m:
            leaks.append(f"{var}: definition not found")
            continue
        body = m.group(1)
        if "_tb_ticker" not in body and "KXMLBTB" not in body:
            leaks.append(f"{var}: no KXMLBTB gate in expression")
    # and confirm _tb_ticker itself is the ticker-prefix test
    if not re.search(r'_tb_ticker\s*=\s*e\.get\("ticker".*?KXMLBTB', src_ui, re.S):
        leaks.append("_tb_ticker is not a KXMLBTB prefix test")
    print(f"\n  TB shadow-band lines NOT gated on a KXMLBTB ticker check: {len(leaks)}")
    for l in leaks[:5]:
        print(f"    ! {l}")
    results["5 git/config"] = (("PASS", "TB bands are KXMLBTB-gated; no K leak")
                               if not leaks else
                               ("FLAG", f"{len(leaks)} ungated TB shadow lines"))


# ── 6. ORDER BOOK / LIQUIDITY ────────────────────────────────────────────────
def check6(rows):
    _hdr(6, "Kalshi order book — current depth/spread vs pre-cliff")
    print("  Historical spread IS recoverable: Kalshi candlesticks survive")
    print("  settlement and carry yes_bid/yes_ask. This is the one clean")
    print("  then-vs-now comparison available.\n")
    print("  --- CURRENT (live order book) ---")
    cur_spreads = []
    for r in rows[:5]:
        try:
            ob, _ = _get(f"{KALSHI}/markets/{r['ticker']}/orderbook")
            book = ob.get("orderbook_fp") or ob.get("orderbook") or {}
            yes = book.get("yes_dollars") or book.get("yes") or []
            no = book.get("no_dollars") or book.get("no") or []
            depth = sum(float(q) for _, q in yes) + sum(float(q) for _, q in no)
        except Exception as exc:
            depth, yes, no = -1, [], []
            print(f"    orderbook error {r['ticker']}: {exc}")
        cur_spreads.append(r["spread_pp"])
        print(f"    {r['title'][:36]:<38} spread={r['spread_pp']:.1f}pp  "
              f"levels(yes/no)={len(yes)}/{len(no)}  depth={depth:,.0f}")

    print("\n  --- PRE-CLIFF (candlestick reconstruction) ---")
    try:
        paper, _ = _get(PAPER_API, timeout=40)
    except Exception:
        paper = {"bets": []}
    old = [b for b in paper.get("bets", [])
           if b["ticker"].upper().startswith("KXMLBKS")
           and BASELINE_FROM <= b.get("flagged_at", "")[:10] <= BASELINE_TO]
    hist_spreads = []
    for b in old[:5]:
        try:
            ft = datetime.fromisoformat(b["flagged_at"].replace("Z", "+00:00"))
            # Stop at FIRST PITCH. In-game candles blow the spread out (one
            # sample averaged 8.03pp with a 43pp max) -- same contamination
            # class as the in-game CLV-capture bug. Pre-game only or the
            # comparison is meaningless.
            gt = b.get("game_time")
            gstart = (datetime.fromisoformat(gt.replace("Z", "+00:00"))
                      if gt else ft + timedelta(hours=2))
            st, en = int((ft - timedelta(hours=1)).timestamp()), int(gstart.timestamp())
            cs, _ = _get(f"{KALSHI}/series/KXMLBKS/markets/{b['ticker']}"
                         f"/candlesticks?start_ts={st}&end_ts={en}&period_interval=1")
            sp = []
            for c in cs.get("candlesticks", []):
                a = (c.get("yes_ask") or {}).get("close_dollars")
                d = (c.get("yes_bid") or {}).get("close_dollars")
                if a and d:
                    v = (float(a) - float(d)) * 100
                    if 0 < v < 50:
                        sp.append(v)
            if sp:
                sp.sort()
                avg = sp[len(sp) // 2]          # median, not mean
                hist_spreads.append(avg)
                print(f"    {b['matchup'][:22]:<24} {b['flagged_at'][:10]}  "
                      f"candles={len(sp):<4} median_spread={avg:.2f}pp  min={min(sp):.0f} max={max(sp):.0f}")
            else:
                print(f"    {b['matchup'][:22]:<24} {b['flagged_at'][:10]}  no usable candles")
        except Exception as exc:
            print(f"    candlestick error: {exc}")

    if cur_spreads and hist_spreads:
        cur_spreads.sort(); hist_spreads.sort()
        c = cur_spreads[len(cur_spreads) // 2]
        h = hist_spreads[len(hist_spreads) // 2]
        print(f"\n  MEDIAN spread (pre-game only)  PRE-CLIFF={h:.2f}pp   NOW={c:.2f}pp   change={c-h:+.2f}pp")
        print("  A tightening (h > c) with edge vanishing = market maker arrived.")
        print("  Unchanged spread + vanished edge = repricing without structural change.")
        results["6 order book"] = ("FLAG" if abs(c - h) >= 0.5 else "PASS",
                                   f"spread {h:.2f} -> {c:.2f}pp")
    else:
        results["6 order book"] = ("PARTIAL", "insufficient candlestick history")


def main():
    offline = "--no-net" in sys.argv
    rows, games = [], []
    if not offline:
        games = fetch_current()
        rows = join_rows(games)
        check1(games, rows)
        check2(rows)
        check3(rows)
        check4(rows)
    check5()
    if not offline:
        check6(rows)

    print(f"\n{'='*78}\n  SUMMARY\n{'='*78}")
    print(f"  {'check':<20}{'status':<14}detail")
    for k in sorted(results):
        st, detail = results[k]
        print(f"  {k:<20}{st:<14}{detail}")
    print("\n  PASS = matches expectation, rule this cause out")
    print("  FLAG = anomalous, investigate")
    print("  PARTIAL/UNAVAILABLE = data does not exist to decide this")


if __name__ == "__main__":
    main()
