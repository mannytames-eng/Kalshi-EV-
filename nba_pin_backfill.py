#!/usr/bin/env python3
"""Backfill Pinnacle CLOSING lines for settled NBA-prop bets from The Odds API historical endpoints.

  python3 nba_pin_backfill.py --pilot 3            # 3 games, ~30 credits, prints true cost per call
  python3 nba_pin_backfill.py --full --cap 2200    # everything left; aborts if the cap would be exceeded
  python3 nba_pin_backfill.py --report             # analysis from the cache only (0 credits)

Spends Odds API credits (historical: 10 x markets returned x regions; events list: 1).  Every response is
cached on disk (--cache), so re-runs and --report cost nothing.  Pinnacle is requested ONLY (bookmakers=pinnacle
= 1 region).  The closing fair is computed with the scanner's OWN functions (no_vig_prob / power_devig_prob /
count_model_fair_band, Pinnacle as the sole anchor -- BOOK_WEIGHTS), so it is comparable with `fair` at flag time.
Snapshot = the 5-minute snapshot at or before (tip - 3 min).  Read-only for the scanner: writes nothing to bets.
"""
import argparse
import collections
import datetime as dt
import json
import math
import os
import random
import re
import statistics as S
import sys
import types
import urllib.request

import requests

# kalshi_ev_scanner imports `cryptography` for Kalshi signing at module load; the pure pricing functions
# used here don't need it, so stub it when it isn't installed.
try:
    import cryptography  # noqa: F401
except ImportError:
    for _m in ("cryptography", "cryptography.hazmat", "cryptography.hazmat.backends", "cryptography.hazmat.primitives",
               "cryptography.hazmat.primitives.asymmetric", "cryptography.hazmat.primitives.asymmetric.padding",
               "cryptography.hazmat.primitives.hashes", "cryptography.hazmat.primitives.serialization"):
        _mod = types.ModuleType(_m)
        _mod.__getattr__ = lambda n: object
        sys.modules[_m] = _mod
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kalshi_ev_scanner as sc  # noqa: E402

API = "https://api.the-odds-api.com/v4/historical/sports/basketball_nba"
KEY = os.environ.get("ODDS_API_KEY") or sc.ODDS_API_KEY
PREFIX = {"KXNBAPTS": "player_points", "KXNBAREB": "player_rebounds", "KXNBAAST": "player_assists",
          "KXNBA3PT": "player_threes", "KXNBAPRA": "player_points_rebounds_assists"}
MINUTES_BEFORE_TIP = 3


def fee(p):
    return math.ceil(0.07 * p * (1 - p) * 100 - 1e-9) / 100


class Spend:
    def __init__(self, cap):
        self.cap, self.used, self.calls, self.remaining = cap, 0, 0, None


def fetch(url, params, cache_path, spend, expect=None):
    """GET with disk cache.  Aborts BEFORE spending past the cap, and when a call costs more than expected."""
    if os.path.exists(cache_path):
        return json.load(open(cache_path))
    if expect is not None and spend.used + expect > spend.cap:
        raise SystemExit(f"STOP: next call (~{expect} credits) would exceed cap {spend.cap} (used {spend.used}).")
    r = requests.get(url, params={**params, "apiKey": KEY}, timeout=60)
    cost = int(r.headers.get("x-requests-last", "0") or 0)
    spend.used += cost
    spend.calls += 1
    spend.remaining = r.headers.get("x-requests-remaining", spend.remaining)
    if r.status_code != 200:
        raise SystemExit(f"HTTP {r.status_code}: {r.text[:200]}")
    if expect is not None and cost > expect * 1.5 + 1:
        json.dump(r.json(), open(cache_path, "w"))
        raise SystemExit(f"STOP: call cost {cost} vs expected {expect} -- pricing differs from the docs. Used {spend.used}.")
    data = r.json()
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    json.dump(data, open(cache_path, "w"))
    print(f"    [credits] call cost {cost} | used this run {spend.used} | account remaining {spend.remaining}")
    return data


def load_bets(url):
    d = json.load(urllib.request.urlopen(url + "/api/performance?since=2026-06-08", timeout=60))
    out = []
    for b in d["bets"]:
        tk = b["ticker"].upper()
        pt = next((v for k, v in PREFIX.items() if tk.startswith(k)), None)
        if pt and b["status"] in ("won", "lost") and b.get("game_time"):
            b["_ptype"] = pt
            b["_game"] = b["ticker"].split("-")[1]
            b["_off"] = float((b.get("fair_band") or {}).get("rungs") or 0) > 0
            out.append(b)
    return out


def snap_date(game_time):
    t = dt.datetime.fromisoformat(game_time.replace("Z", "+00:00")) - dt.timedelta(minutes=MINUTES_BEFORE_TIP)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def find_event(bet, cache, spend):
    n1, n2 = sc._parse_nba_event(bet["ticker"], sc.NBA_ABBR)
    if not n1:
        return None, None
    date = snap_date(bet["game_time"])
    ev = fetch(f"{API}/events", {"date": date}, os.path.join(cache, "events", date.replace(":", "") + ".json"), spend, expect=1)
    want = {sc._norm(n1), sc._norm(n2)}
    tip = dt.datetime.fromisoformat(bet["game_time"].replace("Z", "+00:00"))
    for e in ev.get("data", []):
        if {sc._norm(e["home_team"]), sc._norm(e["away_team"])} == want:
            ct = dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
            if abs((ct - tip).total_seconds()) <= 1800:
                return e["id"], date
    return None, date


def pinnacle_lines(odds_json):
    """{(player_norm, market): (line, over_american, under_american)} from one historical event-odds response."""
    out = {}
    for bm in (odds_json.get("data") or {}).get("bookmakers", []):
        if bm.get("key") != "pinnacle":
            continue
        for m in bm.get("markets", []):
            acc = collections.defaultdict(dict)
            for o in m.get("outcomes", []):
                key = (sc._norm_player(o.get("description", "")), o.get("point"))
                acc[key][o["name"].lower()] = o["price"]
            for (pl, pt), px in acc.items():
                if "over" in px and "under" in px:
                    out[(pl, m["key"])] = (pt, px["over"], px["under"])
    return out


def close_fair(bet, line, o_over, o_under):
    """P(bet wins) at the close, via the scanner's own pricing path.  None when the rung is unpriceable."""
    k = int(bet["ticker"].rsplit("-", 1)[1])               # Kalshi "k+" -> P(X >= k)
    thresh = k - 0.5                                       # floor(thresh)+1 == k, as in scan_player_props
    pt = bet["_ptype"]
    prop = sc.no_vig_prob(o_over, o_under)[0]
    band = sc.count_model_fair_band(line, {"proportional": prop, "power": sc.power_devig_prob(o_over, o_under)[0]},
                                    thresh, sc.count_model_r(pt, line), sc.count_model_max_rungs(pt), sc.count_model_bias(pt))
    if band is None:
        return None
    over = band["point"] if band["rungs"] > 0 else prop
    return over if bet["side"] == "YES" else 1.0 - over


def pick_pilot(bets, n):
    """A small mix: first off-line-only games on the most recent dates with a single market (cheapest), plus one exact."""
    g = collections.defaultdict(list)
    for b in bets:
        g[b["_game"]].append(b)
    cand = [(k, v) for k, v in g.items() if len({x["_ptype"] for x in v}) == 1]
    cand.sort(key=lambda kv: kv[1][0]["game_time"], reverse=True)
    off = [kv for kv in cand if any(x["_off"] for x in kv[1])]
    ex = [kv for kv in cand if not any(x["_off"] for x in kv[1])]
    picks = off[:max(n - 1, 1)] + ex[:1]
    return [k for k, _ in picks[:n]]


def run(bets, games, cache, spend):
    res_path = os.path.join(cache, "results.json")
    results = json.load(open(res_path)) if os.path.exists(res_path) else {}
    for gk in games:
        gb = [b for b in bets if b["_game"] == gk]
        eid, date = find_event(gb[0], cache, spend)
        if not eid:
            print(f"  {gk}: no matching event in historical list -> skipped")
            continue
        mkts = sorted({b["_ptype"] for b in gb})
        odds = fetch(f"{API}/events/{eid}/odds",
                     {"date": date, "bookmakers": "pinnacle", "markets": ",".join(mkts), "oddsFormat": "american"},
                     os.path.join(cache, "odds", f"{eid}_{date.replace(':', '')}.json"), spend, expect=10 * len(mkts))
        lines = pinnacle_lines(odds)
        got = 0
        for b in gb:
            pl = sc._norm_player(b["matchup"])
            ent = lines.get((pl, b["_ptype"]))
            if not ent:
                continue
            fair = close_fair(b, *ent)
            if fair is None:
                continue
            results[b["id"]] = {"game": gk, "off": b["_off"], "side": b["side"], "ptype": b["_ptype"], "price": b["kalshi_price"],
                                "fair_at_flag": b.get("fair"), "pin_fair_close": round(fair, 4), "pin_line": ent[0],
                                "pin_odds": [ent[1], ent[2]], "snap_ts": odds.get("timestamp"), "status": b["status"],
                                "close_mid_pct": b.get("close_mid_pct"), "clv_mid_status": b.get("clv_mid_status"),
                                "entry_mid_pct": b.get("entry_mid_pct")}
            got += 1
        print(f"  {gk}: {len(gb)} bets, priced {got} (markets {mkts}, snapshot {odds.get('timestamp')})")
    os.makedirs(cache, exist_ok=True)
    json.dump(results, open(res_path, "w"), indent=1)
    return results


def cl_stats(vals, keys, boots=4000, seed=5):
    g = collections.defaultdict(list)
    for v, k in zip(vals, keys):
        g[k].append(v)
    n = len(vals)
    m = sum(vals) / n
    se = math.sqrt(sum(sum(x - m for x in c) ** 2 for c in g.values())) / n
    t = m / se if se > 0 else float("nan")
    rng = random.Random(seed)
    cl = list(g.values())
    ms = sorted(sum(sum(c) for c in rc) / sum(len(c) for c in rc) for rc in (rng.choices(cl, k=len(cl)) for _ in range(boots)))
    return m, t, sum(1 for x in ms if x > 0) / boots, ms[int(.05 * boots)], ms[int(.95 * boots)], len(cl)


def report(cache):
    res = json.load(open(os.path.join(cache, "results.json")))
    rows = list(res.values())
    print(f"backfilled bets: {len(rows)} ({sum(r['off'] for r in rows)} off-line, {sum(not r['off'] for r in rows)} exact)")
    if len(rows) < 8:
        print("too few for statistics")
        return
    for lab, sel in (("off-line", lambda r: r["off"]), ("exact", lambda r: not r["off"]), ("all", lambda r: True)):
        rs = [r for r in rows if sel(r)]
        if len(rs) < 8:
            continue
        keys = [r["game"] for r in rs]
        print(f"\n=== {lab}: n={len(rs)} bets, {len(set(keys))} games ===")
        ev = [(r["pin_fair_close"] - r["price"] - fee(r["price"])) / r["price"] for r in rs]
        m, t, p, lo, hi, _ = cl_stats(ev, keys)
        print(f"1. sharp-close EV per $ after fee: {m:+.3f}  t={t:+.2f}  P(>0)={p:.2f}  90%CI[{lo:+.3f},{hi:+.3f}]")
        pv = [r["pin_fair_close"] - r["fair_at_flag"] for r in rs if r["fair_at_flag"] is not None]
        kk = [r["game"] for r in rs if r["fair_at_flag"] is not None]
        m, t, p, lo, hi, _ = cl_stats(pv, kk)
        print(f"2. Pinnacle CLV (close fair - fair at flag): {m*100:+.2f}pp  t={t:+.2f}  P(>0)={p:.2f}  [{lo*100:+.2f},{hi*100:+.2f}]")
        kv = []
        for r in rs:
            c = r["close_mid_pct"]
            if c is None or r["clv_mid_status"] != "ok":
                continue
            kv.append((r["pin_fair_close"] - (c / 100 if r["side"] == "YES" else 1 - c / 100), r["game"]))
        if len(kv) >= 8:
            m, t, p, lo, hi, _ = cl_stats([v for v, _ in kv], [k for _, k in kv])
            print(f"3. sharp close - Kalshi close mid: {m*100:+.2f}pp  t={t:+.2f}  (n={len(kv)}; >0 = Kalshi's close still cheap vs sharp)")
        w = sum(r["status"] == "won" for r in rs)
        print(f"   wins: actual {w}/{len(rs)} | expected at sharp close {sum(r['pin_fair_close'] for r in rs):.1f} | at price paid {sum(r['price'] for r in rs):.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://evscanner-production.up.railway.app")
    ap.add_argument("--cache", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pin_backfill_cache"))
    ap.add_argument("--pilot", type=int, metavar="N_GAMES")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--cap", type=int, default=2200, help="hard credit cap for this run")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        return report(a.cache)
    bets = load_bets(a.url)
    games = sorted({b["_game"] for b in bets})
    mk = collections.defaultdict(set)
    for b in bets:
        mk[b["_game"]].add(b["_ptype"])
    est = sum(10 * len(v) + 1 for v in mk.values())
    print(f"settled NBA-prop bets: {len(bets)} in {len(games)} games | full-run estimate ~{est} credits")
    spend = Spend(a.cap)
    if a.pilot:
        picks = pick_pilot(bets, a.pilot)
        print(f"pilot games: {picks}")
        run(bets, picks, a.cache, spend)
    elif a.full:
        run(bets, games, a.cache, spend)
    else:
        return print("nothing to do: pass --pilot N, --full or --report")
    print(f"\nTOTAL credits spent this run: {spend.used} over {spend.calls} calls | account remaining: {spend.remaining}")


if __name__ == "__main__":
    main()
