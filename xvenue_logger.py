#!/usr/bin/env python3
"""xvenue_logger.py -- shadow-only Polymarket US <-> Kalshi cross-venue gap logger.

PURPOSE (added 2026-11-25): answer "do real, tradable arbitrage gaps exist between
Polymarket US and Kalshi, how big, and how long do they last?" with data instead of
guesses.  A one-off probe on 2026-11-25 found ZERO fee-positive arbs (NBA props, all
sports winners, BTC strikes); gaps seen on stale snapshots were either quote-age
artifacts or rule mismatches.  This logger keeps looking, continuously, with fresh
quotes, so that conclusion is either confirmed or overturned by evidence.

HARD GUARANTEES
  * READ-ONLY.  Only public, unauthenticated GET endpoints on both venues.  No API
    keys, no signing, no orders.  Nothing here can place a trade.
  * ISOLATED.  Imports nothing from the scanner, never touches ev_bets.json, bets,
    stakes, Odds API credits or the dashboard.  Writes only under DATA_DIR/xvenue/.
  * Costs $0 (no Odds API credits).  Respects Polymarket's 25 req/s per-IP limit.

HOW IT WORKS
  discover()  every XV_DISCOVER_SEC: map Polymarket markets to the SAME Kalshi
              contract (NBA player props, all two-team sports winners, BTC strike
              markets), excluding pairs whose settlement rules differ (draw pays
              $0.50, penalties included vs 90 min, ...).
  sweep()     every XV_INTERVAL seconds: refresh all matched quotes, compute both
              hedge directions after both venues' taker fees, log changes.
  hot tier    pairs closest to profitable are re-quoted every XV_HOT_SEC seconds
              between sweeps so gap DURATION is measured at seconds resolution.
  confirm     any gap that looks net-positive is immediately re-quoted fresh and
              logged with raw order-book levels, tagged confirmed / not confirmed.

USAGE
  python3 xvenue_logger.py              # run forever
  python3 xvenue_logger.py --once       # one discover + one sweep, print summary
  python3 xvenue_logger.py --report [days]   # analyse the logs

Not a trading tool.  Adding execution is a separate, deliberate decision.
"""
import argparse
import collections
import datetime as dt
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata

import requests
from concurrent.futures import ThreadPoolExecutor

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = None

# ── config ───────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
OUT_DIR = os.path.join(DATA_DIR, "xvenue")

PM_BASE = "https://gateway.polymarket.us"
K_BASE = "https://api.elections.kalshi.com/trade-api/v2"

XV_INTERVAL = float(os.environ.get("XV_INTERVAL", "45"))          # full sweep cadence (s)
XV_DISCOVER_SEC = float(os.environ.get("XV_DISCOVER_SEC", "900"))  # re-map markets (s)
XV_HOT_SEC = float(os.environ.get("XV_HOT_SEC", "5"))              # hot-tier cadence (s)
XV_PM_RPS = float(os.environ.get("XV_PM_RPS", "10"))               # limit is 25/s per IP
XV_K_RPS = float(os.environ.get("XV_K_RPS", "8"))
XV_HOT_N = int(os.environ.get("XV_HOT_N", "10"))
XV_WINDOW_PAST_H = float(os.environ.get("XV_WINDOW_PAST_H", "8"))   # include live games
XV_WINDOW_FUTURE_H = float(os.environ.get("XV_WINDOW_FUTURE_H", "30"))
XV_KEEP_DAYS = int(os.environ.get("XV_KEEP_DAYS", "14"))
XV_WORKERS = int(os.environ.get("XV_WORKERS", "6"))
XV_DEPTH_SUSPECT = float(os.environ.get("XV_DEPTH_SUSPECT", "5000"))   # contracts: depth above this is flagged, not trusted
XV_FAR_EVERY = int(os.environ.get("XV_FAR_EVERY", "4"))   # far-from-tip pairs: every Nth sweep
XV_NEAR_H = float(os.environ.get("XV_NEAR_H", "4"))       # "near" = within this many hours of start
XV_MAX_CONFIRMS = 12            # fresh re-quotes per sweep (call budget)
NEAR_THRESHOLD = -0.01          # log changes for dirs within 1c of break-even
HOT_THRESHOLD = -0.02

# Fee model.  Kalshi taker: 0.07 x series fee_multiplier x p(1-p) per contract
# (published series fee_multiplier is read live; real fees round up per order).
# Polymarket US API taker: 0.0695 x p(1-p) with a 70% rebate through 2026-12-31
# (docs.polymarket.us/fees) -> "net_rebate"; "net_full" assumes no rebate.
K_FEE = 0.07
PM_FEE_FULL = 0.0695
PM_REBATE = 0.70

STAT_SERIES = {
    "basketball_player_points": "KXNBAPTS",
    "basketball_player_rebounds": "KXNBAREB",
    "basketball_player_assists": "KXNBAAST",
    "basketball_player_threes": "KXNBA3PT",
    "basketball_player_points_rebounds_assists": "KXNBAPRA",
}
# PM market types whose settlement differs from Kalshi's equivalent -> never paired
BLOCKED_PM_TYPES = {"soccer_game_to_advance", "cricket_match_winner"}
BTC_SERIES = {"btc-strike-hourly": "KXBTCD", "btc-strike-daily": "KXBTCD",
              "btc-strike-weekly": "KXBTCD"}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
MON_NAME = {v: k for k, v in MON.items()}
UA = {"User-Agent": "xvenue-logger/1.0 (read-only research)"}


def now_ts() -> float:
    return time.time()


def iso(ts=None) -> str:
    return dt.datetime.fromtimestamp(ts or now_ts(), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── http with rate limiting ──────────────────────────────────────────────────
class Http:
    """Rate-limited GET client.  Thread-safe: one shared limiter, one Session per thread."""

    def __init__(self, base, rps):
        self.base, self.min_gap = base, 1.0 / max(rps, 0.5)
        self._tl = threading.local()
        self._lock = threading.Lock()
        self._next = 0.0
        self.calls = 0
        self.errors = 0

    def _session(self):
        if not hasattr(self._tl, "s"):
            self._tl.s = requests.Session()
            self._tl.s.headers.update(UA)
        return self._tl.s

    def _slot(self):
        with self._lock:
            t = max(time.time(), self._next)
            self._next = t + self.min_gap
        wait = t - time.time()
        if wait > 0:
            time.sleep(wait)

    def get(self, path, params=None, timeout=45, retries=3):
        url = self.base + path
        err = None
        for a in range(retries):
            self._slot()
            try:
                r = self._session().get(url, params=params, timeout=timeout)
                self.calls += 1
                if r.status_code == 429:
                    time.sleep(2 ** a)
                    continue
                if r.status_code >= 500:
                    time.sleep(1 + a)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as ex:  # noqa: BLE001 - network is messy, retry then give up
                err = ex
                self.errors += 1
                time.sleep(0.5 + a)
        raise RuntimeError(f"GET {path} failed: {err}")


PM = Http(PM_BASE, XV_PM_RPS)
KA = Http(K_BASE, XV_K_RPS)


# ── name / math helpers ──────────────────────────────────────────────────────
def _fold(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", " ", s.replace(".", " ").replace("-", " "))


def person(s):
    return " ".join(w for w in _fold(s).split() if w not in ("jr", "sr", "ii", "iii", "iv"))


def team_toks(s):
    return frozenset(w for w in _fold(s).split() if w not in ("fc", "cf", "sc", "afc", "the", "de", "la", "club", "fk"))


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def k_fee(p, mult=1.0):
    return K_FEE * mult * p * (1 - p)


def pm_fee_full(p):
    return PM_FEE_FULL * p * (1 - p)


def pm_fee_net(p):
    return PM_FEE_FULL * (1 - PM_REBATE) * p * (1 - p)


def evaluate(q, k_mult=1.0):
    """Both hedge directions for one matched binary market.
    q = {kb,ka,kna,pb,pa}: Kalshi yes bid/ask + no ask, Polymarket yes bid/ask.
    Direction A: buy Kalshi YES @ka + buy Polymarket NO @(1-pb).
    Direction B: buy Polymarket YES @pa + buy Kalshi NO @kna.
    Exactly one of the two contracts of a hedge pays $1, so profit/contract =
    1 - cost - fees (assuming identical settlement rules, which discover() enforces)."""
    out = []
    kb, ka, kna, pb, pa = (q.get(x) for x in ("kb", "ka", "kna", "pb", "pa"))
    legs = []
    if ka is not None and pb is not None:
        legs.append(("K_YES+PM_NO", ka, 1 - pb, ka, 1 - pb))
    if pa is not None and kna is not None:
        legs.append(("PM_YES+K_NO", pa, kna, kna, pa))
    for name, x, y, kp, pp in legs:
        if not (0 < x < 1 and 0 < y < 1):
            continue
        gross = 1 - x - y
        fk = k_fee(kp, k_mult)
        out.append({"dir": name, "cost": round(x + y, 4), "gross": round(gross, 4),
                    "net_rebate": round(gross - fk - pm_fee_net(pp), 5),
                    "net_full": round(gross - fk - pm_fee_full(pp), 5)})
    return out


def _selftest():
    # hand-checked: even-money markets, no gap -> loses both fees
    e = evaluate({"kb": .49, "ka": .50, "kna": .51, "pb": .49, "pa": .50}, 1.0)
    a = [x for x in e if x["dir"] == "K_YES+PM_NO"][0]
    assert a["gross"] == round(1 - .50 - (1 - .49), 4)           # -0.01
    assert abs(a["net_full"] - (-0.01 - 0.07 * .5 * .5 - 0.0695 * .51 * .49)) < 1e-4
    # a 10c gap is profitable after fees
    e = evaluate({"kb": .39, "ka": .40, "kna": .61, "pb": .50, "pa": .51}, 1.0)
    assert max(x["net_rebate"] for x in e) > 0.05
    # junk quotes are skipped
    assert evaluate({"kb": None, "ka": 1.0, "kna": 0.0, "pb": None, "pa": None}) == []
    return True


# ── Polymarket / Kalshi fetch helpers ────────────────────────────────────────
def pm_events_all():
    ev, off = [], 0
    while True:
        d = PM.get("/v1/events", {"active": "true", "closed": "false", "limit": 100, "offset": off}).get("events", [])
        ev += d
        off += 100
        if len(d) < 100 or off > 60000:
            return ev


def pm_event_detail(slug):
    return PM.get(f"/v1/events/slug/{slug}")["event"]


def pm_bbo(slug):
    md = PM.get(f"/v1/markets/{slug}/bbo")["marketData"]
    b, a = md.get("bestBid") or {}, md.get("bestAsk") or {}
    return f(b.get("value")), f(a.get("value")), md.get("state")


def k_markets_by_series(series):
    out, cur = [], None
    while True:
        p = {"series_ticker": series, "status": "open", "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = KA.get("/markets", p)
        out += d.get("markets", [])
        cur = d.get("cursor")
        if not cur:
            return out


def k_markets_by_event(event_ticker):
    return KA.get("/markets", {"event_ticker": event_ticker, "limit": 1000}).get("markets", [])


def k_quote(m):
    return {"kb": f(m.get("yes_bid_dollars")), "ka": f(m.get("yes_ask_dollars")), "kna": f(m.get("no_ask_dollars"))}


def k_date(event_ticker):
    m = re.search(r"-(\d\d)([A-Z]{3})(\d\d)", event_ticker or "")
    return dt.date(2000 + int(m.group(1)), MON[m.group(2)], int(m.group(3))) if m else None


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


# ── discovery: build the matched-pair table ──────────────────────────────────
def rule_flag(pm_desc, k_rules):
    """Return a reason string when the two contracts likely settle differently.
    Any PM clause that pays $0.50 (draw / cancelled / different gamer) that Kalshi does
    not mirror, or PM counting penalty shootouts where Kalshi is 90-minutes only."""
    d, k = (pm_desc or "").lower(), (k_rules or "").lower()
    if "$0.50" in d and not any(x in k for x in ("$0.50", "50 cents", "50\u00a2", "50%")):
        return "pm_settles_half_where_kalshi_doesnt"
    if "shootout" in d and "does not include extra time" in k:
        return "pm_includes_shootout"
    return None


def discover():
    t0 = now_ts()
    lo = dt.timedelta(hours=XV_WINDOW_PAST_H)
    hi = dt.timedelta(hours=XV_WINDOW_FUTURE_H)
    now = dt.datetime.now(dt.timezone.utc)
    series_fee = {}
    try:
        for s in KA.get("/series").get("series", []):
            series_fee[s["ticker"]] = f(s.get("fee_multiplier")) or 1.0
    except Exception as ex:  # noqa: BLE001
        print(f"  [xv] series fee map unavailable ({ex}); assuming 1.0x")
    events = pm_events_all()
    pairs, skipped = [], collections.Counter()

    def in_window(e):
        st = parse_ts(e.get("startTime") or "")
        return st and (now - lo) <= st <= (now + hi)

    # --- sports winners (two-team, single binary market per team) -------------
    k_game_events = {}
    game_series = sorted(s for s in series_fee if re.search(r"(GAME|MATCH|FIGHT)$", s))
    cand = [e for e in events if e.get("category") == "sports" and len(e.get("teams") or []) == 2 and in_window(e)]
    if cand:
        wanted_dates = {parse_ts(e["startTime"]).date() for e in cand}
        # one call per game-like series, in parallel (Kalshi keeps no per-day index)
        def _ser(sname):
            try:
                return k_markets_by_series(sname)
            except Exception:  # noqa: BLE001
                return []
        with ThreadPoolExecutor(max_workers=XV_WORKERS) as ex:
            for ms in ex.map(_ser, game_series):
                for m in ms:
                    d = k_date(m.get("event_ticker"))
                    if d and any(abs((d - w).days) <= 1 for w in wanted_dates):
                        k_game_events.setdefault(m["event_ticker"], []).append(m)
    kinfo = {}
    for et, ms in k_game_events.items():
        subs = {}
        for m in ms:
            s = (m.get("yes_sub_title") or "").strip()
            if s.lower() in ("tie", "draw", ""):
                continue
            subs[team_toks(s)] = m
        kinfo[et] = (k_date(et), subs)
    for e in cand:
        mk = [m for m in e["markets"] if (m.get("sportsMarketType") or "").endswith(("winner", "_winner_2"))
              and m.get("sportsMarketType") not in BLOCKED_PM_TYPES]
        if not mk:
            skipped["no_winner_market_or_blocked"] += 1
            continue
        sd = parse_ts(e["startTime"]).date()
        names = [[team_toks(n) for n in (t.get("name"), t.get("safeName"), t.get("alias")) if n] for t in e["teams"]]
        found = []
        for et, (kd, subs) in kinfo.items():
            if kd is None or abs((kd - sd).days) > 1:
                continue
            used, assign, ok = set(), [], True
            for nm in names:
                hit = next((ks for ks in subs if ks not in used and any(a and ks and (a <= ks or ks <= a) for a in nm)), None)
                if hit is None:
                    ok = False
                    break
                used.add(hit)
                assign.append(hit)
            if ok:
                found.append((et, assign, subs))
        if len(found) != 1:
            skipped["winner_unmatched_or_ambiguous"] += 1
            continue
        et, assign, subs = found[0]
        t2k = {e["teams"][i]["name"]: subs[assign[i]] for i in range(2)}
        for m in mk:
            sides = m["marketSides"]
            longs = [s for s in sides if s.get("long")]
            if m["sportsMarketType"].startswith("soccer"):
                tm = (sides[0].get("team") or {}).get("name")
            else:
                tm = (longs[0].get("team") or {}).get("name") if longs else None
            km = t2k.get(tm)
            if not km:
                continue
            flag = rule_flag(m.get("description"), (km.get("rules_primary") or "") + " " + (km.get("rules_secondary") or ""))
            if flag:
                skipped["rule_mismatch:" + flag] += 1
                continue
            pairs.append({"kind": "winner", "pm": m["slug"], "k": km["ticker"], "ke": km["event_ticker"],
                          "label": f"{tm} ({e['slug']})", "start": e["startTime"], "fee": series_fee.get(km["event_ticker"].split("-")[0], 1.0),
                          "pmtype": m["sportsMarketType"], "game": e["slug"]})

    # --- NBA player props -----------------------------------------------------
    nba = [e for e in events if e.get("seriesSlug") == "nba-2025" and e["slug"].startswith("nba-") and in_window(e)]
    if nba:
        k_prop = {}
        for series in set(STAT_SERIES.values()):
            for m in k_markets_by_series(series):
                nm = (m.get("yes_sub_title") or "").split(":")[0]
                if m.get("floor_strike") is None:
                    continue
                n = int(float(m["floor_strike"])) + 1
                d = k_date(m["event_ticker"])
                k_prop[(series, person(nm), n, d.isoformat() if d else "")] = m
        for e in nba:
            try:
                d = pm_event_detail(e["slug"])
            except Exception:  # noqa: BLE001
                skipped["prop_event_detail_fail"] += 1
                continue
            date = e["slug"][-10:]
            for m in d["markets"]:
                series = STAT_SERIES.get(m.get("sportsMarketType"))
                if not series:
                    continue
                mm = re.match(r"Will (.+?) record at least (\d+)", m.get("question", ""))
                if not mm:
                    continue
                km = k_prop.get((series, person(mm.group(1)), int(mm.group(2)), date))
                if not km:
                    skipped["prop_no_kalshi_twin"] += 1
                    continue
                pairs.append({"kind": "prop", "pm": m["slug"], "k": km["ticker"], "ke": km["event_ticker"], "series": series,
                              "label": f"{mm.group(1)} {mm.group(2)}+ {series[5:]}", "start": e["startTime"],
                              "fee": series_fee.get(series, 1.0), "pmtype": m["sportsMarketType"], "game": e["slug"]})

    # --- BTC strike markets (both settle on CF Benchmarks BRTI) ---------------
    for e in events:
        ks = BTC_SERIES.get(e.get("seriesSlug"))
        if not ks or _ET is None:
            continue
        end = parse_ts(e.get("endDate") or "")
        if not end or not (now - dt.timedelta(hours=1) <= end <= now + dt.timedelta(days=8)):
            continue
        et_t = end.astimezone(_ET)
        kev = f"{ks}-{et_t:%y}{MON_NAME[et_t.month]}{et_t:%d}{et_t:%H}"
        try:
            kms = k_markets_by_event(kev)
            d = pm_event_detail(e["slug"])
        except Exception:  # noqa: BLE001
            continue
        kby = {round(float(m["floor_strike"]) + 0.01): m for m in kms if m.get("floor_strike") is not None}
        for m in d["markets"]:
            mm = re.search(r"-(\d+)$", m["slug"])
            km = kby.get(int(mm.group(1))) if mm else None
            if km:
                pairs.append({"kind": "btc", "pm": m["slug"], "k": km["ticker"], "ke": kev, "label": f"BTC>={mm.group(1)} {kev}",
                              "start": e.get("startTime") or iso(), "fee": series_fee.get(ks, 1.0), "pmtype": "btc_strike", "game": e["slug"],
                              "settle": end.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    by_kind = collections.Counter(p["kind"] for p in pairs)
    print(f"  [xv] discovered {len(pairs)} pairs {dict(by_kind)} in {now_ts()-t0:.0f}s; skipped {dict(skipped)}")
    return pairs, dict(skipped)


# ── quote refresh ────────────────────────────────────────────────────────────
def _is_near(p, now):
    if p["kind"] == "btc":
        return True
    st = parse_ts(p["start"])
    return bool(st) and abs((st - now).total_seconds()) <= XV_NEAR_H * 3600


def refresh(pairs, sweep_n):
    """Fresh quotes for the pairs due this sweep (near-tip every sweep, far every Nth).
    Fetches run concurrently under the shared rate limiter.  Every quote carries the
    time each venue's number was fetched (tk / tp) so cross-venue skew is knowable."""
    t0 = now_ts()
    now = dt.datetime.now(dt.timezone.utc)
    due = [p for p in pairs if _is_near(p, now) or sweep_n % XV_FAR_EVERY == 1]
    k_jobs, pm_jobs = {}, {}
    for p in due:
        if p["kind"] == "prop":
            k_jobs[("s", p["series"])] = None
            pm_jobs[("g", p["game"])] = None
        elif p["kind"] == "btc":
            k_jobs[("e", p["ke"])] = None
            pm_jobs[("g", p["game"])] = None
        else:
            k_jobs[("e", p["ke"])] = None
            pm_jobs[("b", p["pm"])] = None

    def kjob(key):
        kind, v = key
        ms = k_markets_by_series(v) if kind == "s" else k_markets_by_event(v)
        return key, ms, now_ts()

    def pjob(key):
        kind, v = key
        if kind == "g":
            ms = pm_event_detail(v)["markets"]
            return key, {m["slug"]: (f((m.get("bestBidQuote") or {}).get("value")), f((m.get("bestAskQuote") or {}).get("value")))
                         for m in ms}, now_ts()
        b, a, _ = pm_bbo(v)
        return key, {v: (b, a)}, now_ts()

    kq, pq = {}, {}
    with ThreadPoolExecutor(max_workers=XV_WORKERS) as ex:
        futs = [ex.submit(kjob, k) for k in k_jobs] + [ex.submit(pjob, k) for k in pm_jobs]
        for fu in futs:
            try:
                key, data, ts = fu.result()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, list):
                for m in data:
                    kq[m["ticker"]] = (m, ts)
            else:
                for slug, v in data.items():
                    pq[slug] = (v, ts)
    q = {}
    for p in due:
        km, pp = kq.get(p["k"]), pq.get(p["pm"])
        if not km or not pp:
            continue
        m, tk = km
        (pb, pa), tp = pp
        if m.get("status") not in (None, "active", "open") or pb is None or pa is None or not pb < pa:
            continue
        d = k_quote(m)
        if None in (d["kb"], d["ka"], d["kna"]):
            continue
        d.update({"pb": pb, "pa": pa, "tk": tk, "tp": tp})
        q[p["id"]] = d
    return q, now_ts() - t0


def fresh_requote(p):
    """Re-fetch one pair's two markets back-to-back (cross-venue skew ~ one round trip)."""
    b, a, st = pm_bbo(p["pm"])
    t_p = now_ts()
    km = KA.get(f"/markets/{p['k']}")["market"]
    t_k = now_ts()
    d = k_quote(km)
    d.update({"pb": b, "pa": a, "tp": t_p, "tk": t_k, "pm_state": st, "k_status": km.get("status")})
    return d


def _lvl(x):
    """First element of a book level -> (price, qty) floats; tolerant of both venues' shapes."""
    try:
        if isinstance(x, dict):
            return float(x["px"]["value"]), float(x["qty"])
        return float(x[0]), float(x[1])
    except Exception:  # noqa: BLE001
        return None, None


def walk_depth(leg_k, leg_p, k_mult, pm_fee=None):
    """Walk both books together.  leg_k / leg_p = [(price, qty), ...] sorted cheapest
    first (the ask you would pay on each venue).  Keep buying while the MARGINAL
    contract is still profitable after both fees.  Returns (contracts, total_profit_usd).
    This is the number that matters: a 'gap' with 0.01 contracts behind it is dust."""
    i = j = 0
    qk = leg_k[0][1] if leg_k else 0.0
    qp = leg_p[0][1] if leg_p else 0.0
    total_q = total_pnl = 0.0
    while i < len(leg_k) and j < len(leg_p):
        x, y = leg_k[i][0], leg_p[j][0]
        net = 1 - x - y - k_fee(x, k_mult) - (pm_fee or pm_fee_net)(y)
        if net <= 0:
            break
        q = min(qk, qp)
        if q <= 0:
            break
        total_q += q
        total_pnl += q * net
        qk -= q
        qp -= q
        if qk <= 1e-9:
            i += 1
            qk = leg_k[i][1] if i < len(leg_k) else 0.0
        if qp <= 1e-9:
            j += 1
            qp = leg_p[j][1] if j < len(leg_p) else 0.0
    return round(total_q, 2), round(total_pnl, 2)


def books_and_size(p, direction):
    """Raw top-of-book for both venues plus the executable size / dollar profit for this
    hedge direction, from a depth walk of both books (see walk_depth)."""
    out = {}
    pm_bids = pm_offers = k_yes_bids = k_no_bids = None
    try:
        bk = PM.get(f"/v1/markets/{p['pm']}/book")["marketData"]
        pm_bids = [l for l in (_lvl(x) for x in (bk.get("bids") or [])) if l[0] is not None]     # best (highest) first
        pm_offers = [l for l in (_lvl(x) for x in (bk.get("offers") or [])) if l[0] is not None]  # best (lowest) first
        out["pm_bids"], out["pm_offers"] = (bk.get("bids") or [])[:4], (bk.get("offers") or [])[:4]
    except Exception as ex:  # noqa: BLE001
        out["pm_err"] = str(ex)[:80]
    try:
        ob = KA.get(f"/markets/{p['k']}/orderbook").get("orderbook_fp") or {}
        k_yes_bids = sorted((l for l in (_lvl(x) for x in (ob.get("yes_dollars") or [])) if l[0] is not None), reverse=True)   # best first
        k_no_bids = sorted((l for l in (_lvl(x) for x in (ob.get("no_dollars") or [])) if l[0] is not None), reverse=True)
        out["k_yes_bids_top"], out["k_no_bids_top"] = (ob.get("yes_dollars") or [])[-4:], (ob.get("no_dollars") or [])[-4:]
    except Exception as ex:  # noqa: BLE001
        out["k_err"] = str(ex)[:80]
    try:
        if direction == "K_YES+PM_NO":
            leg_k = [(round(1 - px, 4), q) for px, q in k_no_bids]       # Kalshi YES ask = 1 - NO bid
            leg_p = [(round(1 - px, 4), q) for px, q in pm_bids]         # PM NO ask = 1 - YES bid
        else:
            leg_p = [(px, q) for px, q in pm_offers]
            leg_k = [(round(1 - px, 4), q) for px, q in k_yes_bids]      # Kalshi NO ask = 1 - YES bid
        qty, pnl = walk_depth(leg_k, leg_p, p["fee"])
        out["exec_contracts"], out["exec_profit_usd"] = qty, pnl
        # same walk with Polymarket's FULL taker fee (no 70% rebate): the number to trust
        out["exec_contracts_full"], out["exec_profit_full_usd"] = walk_depth(leg_k, leg_p, p["fee"], pm_fee_full)
    except Exception as ex:  # noqa: BLE001
        out["walk_err"] = str(ex)[:80]
    return out


# ── logging ──────────────────────────────────────────────────────────────────
def _path(kind):
    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.join(OUT_DIR, f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d}-{kind}.jsonl")


def wlog(kind, row):
    try:
        with open(_path(kind), "a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError as ex:
        print(f"  [xv] log write failed: {ex}")


def prune():
    try:
        cutoff = now_ts() - XV_KEEP_DAYS * 86400
        for n in os.listdir(OUT_DIR):
            fp = os.path.join(OUT_DIR, n)
            if n.endswith(".jsonl") and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
    except OSError:
        pass


QK = ("kb", "ka", "kna", "pb", "pa")


class State:
    def __init__(self):
        self.pairs, self.skipped, self.pmap = [], {}, {}
        self.last_sig = {}      # (pair,dir) -> quote signature, so near-miss rows log only on change
        self.episodes = {}      # (pair,dir) -> open, CONFIRMED gap
        self.checked = {}       # (pair,dir) -> ts of last confirmation attempt (cooldown)
        self.fail = {}          # (pair,dir) -> consecutive failed re-quotes of an open episode
        self.sweep_n = 0

    def set_pairs(self, pairs, skipped):
        for p in pairs:
            p["id"] = p["pm"] + "|" + p["k"]
        self.pairs, self.skipped = pairs, skipped
        self.pmap = {p["id"]: p for p in pairs}
        # Episodes whose pair vanished from the map (game over / market delisted) can never be
        # re-quoted again.  Close them, or the hot loop raises KeyError on them every cycle.
        for key in [k for k in self.episodes if k[0] not in self.pmap]:
            self.close_episode(key, "pair_dropped")

    def close_episode(self, key, reason, label=""):
        ep = self.episodes.pop(key, None)
        self.fail.pop(key, None)
        if ep:
            wlog("hits", {"ts": iso(), "kind": "episode_end", "pair": key[0], "label": label or key[0][:60], "dir": key[1],
                          "t0": iso(ep["t0"]), "duration_s_lower_bound": round(ep["tlast"] - ep["t0"], 1), "obs": ep["n"],
                          "max_net_rebate": ep["max"], "res_s": XV_HOT_SEC, "end_reason": reason})


def confirm(st, p, direction, snap_ev, snap_q, summary):
    """A sweep snapshot is only a CANDIDATE (its two venue numbers were fetched seconds
    apart).  The truth is a fresh back-to-back re-quote; only that can open an episode."""
    t1 = now_ts()
    try:
        fq = fresh_requote(p)
        fev = next((x for x in evaluate(fq, p["fee"]) if x["dir"] == direction), None)
    except Exception as ex:  # noqa: BLE001
        print(f"  [xv] confirm failed {p['id']}: {ex}")
        return
    ok = bool(fev and fev["net_rebate"] > 0)
    row = {"ts": iso(), "kind": "hit", "pair": p["id"], "pkind": p["kind"], "label": p["label"], "pmtype": p["pmtype"], "dir": direction,
           "snapshot": {**snap_ev, "q": {k: snap_q[k] for k in QK}, "age_s": round(t1 - min(snap_q["tk"], snap_q["tp"]), 1),
                        "venue_skew_s": round(abs(snap_q["tk"] - snap_q["tp"]), 1)},
           "confirmed": ok, "confirm_latency_s": round(now_ts() - t1, 2)}
    if p.get("settle"):
        sd = parse_ts(p["settle"])
        if sd:
            row["min_to_settle"] = round((sd - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60, 1)
    if fev:
        row["fresh"] = {**fev, "q": {k: fq[k] for k in QK}, "venue_skew_s": round(abs(fq["tk"] - fq["tp"]), 2),
                        "pm_state": fq.get("pm_state"), "k_status": fq.get("k_status")}
    if ok:
        row["books"] = books_and_size(p, direction)
        row["exec_contracts"] = row["books"].get("exec_contracts")
        row["exec_contracts_full"] = row["books"].get("exec_contracts_full")
        row["exec_profit_usd"] = row["books"].get("exec_profit_usd")
        row["exec_profit_full_usd"] = row["books"].get("exec_profit_full_usd")
        row["depth_suspect"] = (row["exec_contracts_full"] or row["exec_contracts"] or 0) > XV_DEPTH_SUSPECT
        st.episodes[(p["id"], direction)] = {"t0": fq["tk"], "tlast": fq["tk"], "max": fev["net_rebate"], "n": 1}
        summary["confirmed"] += 1
    wlog("hits", row)


def process_sweep(st, quotes, summary):
    """Evaluate every quoted pair from a sweep snapshot: count, log near-miss changes,
    and send net-positive candidates to fresh confirmation."""
    confirms = 0
    cands = []
    for pid, q in quotes.items():
        p = st.pmap[pid]
        for ev in evaluate(q, p["fee"]):
            key = (pid, ev["dir"])
            summary["dirs"] += 1
            summary["gross_pos"] += ev["gross"] > 0
            nr = ev["net_rebate"]
            summary["best_net"] = max(summary["best_net"], nr)
            sig = tuple(q[k] for k in QK)
            if nr > NEAR_THRESHOLD and st.last_sig.get(key) != sig:
                wlog("near", {"ts": iso(), "pair": pid, "kind": p["kind"], "label": p["label"], "dir": ev["dir"],
                              "q": {k: q[k] for k in QK}, "snapshot_age_s": round(now_ts() - min(q["tk"], q["tp"]), 1), **ev})
            st.last_sig[key] = sig
            if nr > 0:
                summary["net_pos"] += 1
                if key not in st.episodes and now_ts() - st.checked.get(key, 0) > 30:
                    cands.append((nr, key, p, ev, q))
    cands.sort(key=lambda c: -c[0])
    for nr, key, p, ev, q in cands[:XV_MAX_CONFIRMS]:
        st.checked[key] = now_ts()
        confirm(st, p, key[1], ev, q, summary)
        confirms += 1
    return confirms


def hot_tick(st, pids):
    """Fresh re-quote of the pairs closest to profitable + every open episode, so a gap's
    life (open -> close) is measured at ~XV_HOT_SEC resolution."""
    for pid in pids:
        p = st.pmap.get(pid)
        if p is None:                       # pair dropped by a re-discovery: close its episodes, never KeyError
            for d in ("K_YES+PM_NO", "PM_YES+K_NO"):
                st.close_episode((pid, d), "pair_dropped")
            continue
        try:
            fq = fresh_requote(p)
        except Exception:  # noqa: BLE001
            for d in ("K_YES+PM_NO", "PM_YES+K_NO"):    # a market that keeps failing (closed/settled) must not pin an episode open
                k2 = (pid, d)
                if k2 in st.episodes:
                    st.fail[k2] = st.fail.get(k2, 0) + 1
                    if st.fail[k2] >= 6:
                        st.close_episode(k2, "requote_failing", p["label"])
            continue
        evs = {e["dir"]: e for e in evaluate(fq, p["fee"])}
        for d in ("K_YES+PM_NO", "PM_YES+K_NO"):
            key, ev = (pid, d), evs.get(d)
            ep = st.episodes.get(key)
            if ep:
                st.fail.pop(key, None)
            if ev and ev["net_rebate"] > 0:
                if ep:
                    ep["tlast"], ep["max"], ep["n"] = fq["tk"], max(ep["max"], ev["net_rebate"]), ep["n"] + 1
                elif now_ts() - st.checked.get(key, 0) > 30:
                    st.checked[key] = now_ts()
                    confirm(st, p, d, ev, fq, {"confirmed": 0})
            elif ep:
                st.close_episode(key, "gap_closed", p["label"])


def hot_ids(st, quotes):
    scored = []
    for pid, q in quotes.items():
        if pid not in st.pmap:
            continue
        evs = evaluate(q, st.pmap[pid]["fee"])
        if evs:
            scored.append((max(e["net_rebate"] for e in evs), pid))
    scored.sort(reverse=True)
    ids = [pid for s, pid in scored[:XV_HOT_N] if s > HOT_THRESHOLD]
    for (pid, _d) in st.episodes:
        if pid not in ids:
            ids.append(pid)
    return ids


def run_sweep(st):
    quotes, took = refresh(st.pairs, st.sweep_n + 1)
    st.sweep_n += 1
    summary = {"dirs": 0, "gross_pos": 0, "net_pos": 0, "confirmed": 0, "best_net": -9.0}
    process_sweep(st, quotes, summary)
    kinds = collections.Counter(st.pmap[pid]["kind"] for pid in quotes)
    row = {"ts": iso(), "sweep": st.sweep_n, "pairs": len(st.pairs), "quoted": len(quotes), "by_kind": dict(kinds),
           "refresh_s": round(took, 1), "pm_calls": PM.calls, "k_calls": KA.calls, "pm_err": PM.errors, "k_err": KA.errors,
           "open_episodes": len(st.episodes), **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items()}}
    wlog("sweeps", row)
    print(f"  [xv] sweep {st.sweep_n}: quoted {len(quotes)}/{len(st.pairs)} in {took:.0f}s | gross>0 {summary['gross_pos']} "
          f"net>0(snapshot) {summary['net_pos']} -> confirmed fresh {summary['confirmed']} | open {len(st.episodes)}")
    return quotes


def hot_loop(st, quotes, until):
    while now_ts() + XV_HOT_SEC < until:
        ids = hot_ids(st, quotes)
        if not ids:
            return
        time.sleep(XV_HOT_SEC)
        hot_tick(st, ids)


def main_loop():
    _selftest()
    print(f"  [xv] start  out={OUT_DIR}  interval={XV_INTERVAL}s discover={XV_DISCOVER_SEC}s hot={XV_HOT_SEC}s workers={XV_WORKERS} (read-only)")
    st = State()
    last_disc = 0.0
    while True:
        try:
            if now_ts() - last_disc > XV_DISCOVER_SEC or not st.pairs:
                st.set_pairs(*discover())
                last_disc = now_ts()
                prune()
            t0 = now_ts()
            quotes = run_sweep(st)
            hot_loop(st, quotes, t0 + XV_INTERVAL)
            rest = t0 + XV_INTERVAL - now_ts()
            if rest > 0:
                time.sleep(rest)
        except KeyboardInterrupt:
            print("  [xv] stopped")
            return
        except Exception as ex:  # noqa: BLE001 - never die; log and retry
            print(f"  [xv] cycle error: {ex}")
            wlog("sweeps", {"ts": iso(), "error": str(ex)[:200]})
            time.sleep(15)


# ── report ───────────────────────────────────────────────────────────────────
def _read(kind, days):
    rows = []
    if not os.path.isdir(OUT_DIR):
        return rows
    cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    for n in sorted(os.listdir(OUT_DIR)):
        if n.endswith(f"-{kind}.jsonl") and n[:10] >= cut:
            with open(os.path.join(OUT_DIR, n)) as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    return rows


def report(days=7):
    sw = [r for r in _read("sweeps", days) if "sweep" in r]
    hits = _read("hits", days)
    near = _read("near", days)
    print(f"== xvenue report, last {days} day(s) ==")
    if not sw:
        print("no sweeps logged yet")
        return
    span_h = (parse_ts(sw[-1]["ts"]) - parse_ts(sw[0]["ts"])).total_seconds() / 3600
    print(f"sweeps: {len(sw)} over {span_h:.1f}h | avg pairs quoted {sum(r['quoted'] for r in sw)/len(sw):.0f} "
          f"| avg refresh {sum(r['refresh_s'] for r in sw)/len(sw):.0f}s | pair-directions evaluated {sum(r['dirs'] for r in sw):,}")
    print(f"gross>0 observations: {sum(r['gross_pos'] for r in sw)} | net>0 (first look): {sum(r['net_pos'] for r in sw)} "
          f"| best net/contract seen: {max(r['best_net'] for r in sw):+.4f}")
    h = [r for r in hits if r.get("kind") == "hit"]
    conf = [r for r in h if r["confirmed"]]
    print(f"gap episodes opened: {len(h)} | CONFIRMED on fresh re-quote: {len(conf)} | not confirmed (stale/vanished): {len(h)-len(conf)}")
    ends = [r for r in hits if r.get("kind") == "episode_end"]
    if ends:
        d = sorted(r["duration_s_lower_bound"] for r in ends)
        print(f"episode duration (lower bound, s): median {d[len(d)//2]:.0f}  p90 {d[int(len(d)*0.9)]:.0f}  max {d[-1]:.0f}")
    if conf:
        print("top confirmed:")
        for r in sorted(conf, key=lambda r: -r["fresh"]["net_rebate"])[:10]:
            print(f"  {r['ts'][:19]} {r['pkind']:6} {r['label'][:44]:44} {r['dir']} net {r['fresh']['net_rebate']:+.4f} "
                  f"(full-fee {r['fresh']['net_full']:+.4f}) size {r.get('exec_contracts')} ${r.get('exec_profit_usd')}")
    # Trust the FULL-fee walk (no Polymarket rebate); fall back to the rebate figure for old records.
    def _full(r):
        v = r.get("exec_profit_full_usd")
        return v if v is not None else (r.get("exec_profit_usd") or 0)
    sus = [r for r in conf if r.get("depth_suspect") or (r.get("exec_contracts") or 0) > XV_DEPTH_SUSPECT]
    ok_ = [r for r in conf if r not in sus]
    money = [r for r in ok_ if _full(r) >= 1.0]
    print(f"confirmed AND >= $1 executable at FULL fee, depth <= {XV_DEPTH_SUSPECT:,.0f} contracts: {len(money)} "
          f"(${sum(_full(r) for r in money):,.2f}) | flagged suspicious depth (>{XV_DEPTH_SUSPECT:,.0f}): {len(sus)} "
          f"(${sum(_full(r) for r in sus):,.2f} -- do NOT count)")
    print(f"(old rebate-basis figure for reference: {sum(1 for r in conf if (r.get('exec_profit_usd') or 0) >= 1.0)} hits, "
          f"${sum((r.get('exec_profit_usd') or 0) for r in conf):,.2f})")
    bt = [r for r in money if r["pkind"] == "btc" and r.get("min_to_settle") is not None]
    if bt:
        m = sorted(r["min_to_settle"] for r in bt)
        print(f"BTC money-hits minutes-to-settlement: n={len(m)} min {m[0]:.0f} median {m[len(m)//2]:.0f} max {m[-1]:.0f} "
              f"| within 15 min of settle: {sum(1 for x in m if 0 <= x <= 15)}")
    if any(r.get("exec_profit_full_usd") is None for r in conf):
        print(f"  (note: {sum(1 for r in conf if r.get('exec_profit_full_usd') is None)} older records predate the full-fee walk; "
              f"for those the figure above is the rebate basis)")
    reasons = collections.Counter(r.get("end_reason", "legacy") for r in ends)
    if ends:
        print("episode end reasons:", dict(reasons))
    by = collections.Counter(r["pkind"] for r in conf)
    print("confirmed by kind:", dict(by) or "none")
    print(f"near-miss rows (within 1c of break-even): {len(near)}")
    print("NOTE: a hit is only tradable if depth exists at both best prices -- see 'books' in hits.jsonl.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="one discover + one sweep, then exit")
    ap.add_argument("--report", nargs="?", const=7, type=int, metavar="DAYS")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        print("selftest ok" if _selftest() else "FAIL")
    elif a.report is not None:
        report(a.report)
    elif a.once:
        _selftest()
        s = State()
        s.set_pairs(*discover())
        run_sweep(s)
        print(f"  [xv] calls: PM {PM.calls} (err {PM.errors}), Kalshi {KA.calls} (err {KA.errors})")
    else:
        main_loop()
