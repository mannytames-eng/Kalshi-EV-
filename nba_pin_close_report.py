#!/usr/bin/env python3
"""NBA props: test the model against Pinnacle's CLOSING line (read-only analysis).

Run:  python3 nba_pin_close_report.py [--url https://evscanner-production.up.railway.app] [--max-stale 20]

Uses bets that carry `pin_fair_close` (recorded by _record_nba_prop_pin_close in
kalshi_ev_ui.py on every NBA props scan until tip).  Forward-only: bets flagged before the
capture shipped have no Pinnacle history and are not included.

Three questions, each with a game-clustered t-stat and bootstrap P(>0):
  1. SHARP-CLOSE EV   (q_pin - price_paid - fee) / price_paid
        If Pinnacle's closing fair for our side is the truth, is the bet +EV after the
        fee we actually pay?  This is the test Kalshi's own close could not answer
        (Kalshi props close wide/thin, so it may not be efficient).
  2. PINNACLE CLV     q_pin_close - fair_at_flag
        Did the sharp market move toward our fair after we bet?  (0 = Pinnacle never
        disagreed with our entry fair; >0 = we beat the sharp close.)
  3. KALSHI vs SHARP  q_pin_close - kalshi_close_mid(side)
        How far Kalshi's own closing mid sits from Pinnacle's: if >0 Kalshi's close is
        still cheap vs the sharp close, which is why Kalshi-close CLV understates edge.
Only captures within --max-stale minutes of tip count as a "close"."""
import argparse
import collections
import json
import math
import random
import statistics as S
import urllib.request


def fee(p):
    return math.ceil(0.07 * p * (1 - p) * 100 - 1e-9) / 100


def cluster_stats(vals, keys, boots=4000, seed=11):
    g = collections.defaultdict(list)
    for v, k in zip(vals, keys):
        g[k].append(v)
    n = len(vals)
    m = sum(vals) / n
    s = sum(sum(x - m for x in c) ** 2 for c in g.values())
    se = math.sqrt(s) / n if n else float("nan")
    t = m / se if se > 0 else float("nan")
    rng = random.Random(seed)
    cl = list(g.values())
    ms = []
    for _ in range(boots):
        tot = cnt = 0
        for c in rng.choices(cl, k=len(cl)):
            tot += sum(c)
            cnt += len(c)
        ms.append(tot / cnt)
    ms.sort()
    return m, t, sum(1 for x in ms if x > 0) / boots, ms[int(.05 * boots)], ms[int(.95 * boots)], len(cl)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://evscanner-production.up.railway.app")
    ap.add_argument("--max-stale", type=float, default=20.0, help="max minutes-before-tip of the last capture")
    a = ap.parse_args()
    d = json.load(urllib.request.urlopen(a.url + "/api/performance?since=2026-06-08", timeout=60))
    PFX = ("KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBAPRA")
    nba = [b for b in d["bets"] if b["ticker"].upper().startswith(PFX)]
    have = [b for b in nba if b.get("pin_fair_close") is not None]
    fresh = [b for b in have if (b.get("pin_close_min_to_start") or 999) <= a.max_stale]
    settled = [b for b in fresh if b["status"] in ("won", "lost")]
    print(f"NBA prop bets: {len(nba)} | with Pinnacle close captured: {len(have)} | within {a.max_stale:.0f} min of tip: "
          f"{len(fresh)} | settled: {len(settled)}")
    if len(fresh) < 10:
        print("Too few captured closes to say anything yet (this fills forward from the deploy date).")
        return

    def kind(b):
        return "off-line" if float((b.get("fair_band") or {}).get("rungs") or 0) > 0 else "exact"

    def side_close_mid(b):
        c = b.get("close_mid_pct")
        if c is None or b.get("clv_mid_status") != "ok":
            return None
        return c / 100 if b["side"] == "YES" else 1 - c / 100

    for lab in ("off-line", "exact", "all"):
        rows = [b for b in fresh if lab == "all" or kind(b) == lab]
        if len(rows) < 5:
            continue
        keys = [b["ticker"].split("-")[1] for b in rows]
        print(f"\n=== {lab} (n={len(rows)}, {len(set(keys))} games, median capture {S.median(b['pin_close_min_to_start'] for b in rows):.0f} min before tip) ===")
        ev = [(b["pin_fair_close"] - b["kalshi_price"] - fee(b["kalshi_price"])) / b["kalshi_price"] for b in rows]
        m, t, p, lo, hi, nc = cluster_stats(ev, keys)
        print(f"1. sharp-close EV per $ (after fee): {m:+.3f}  t={t:+.2f}  P(>0)={p:.2f}  90%CI[{lo:+.3f},{hi:+.3f}]")
        pv = [b["pin_fair_close"] - (b.get("fair") if b.get("fair") is not None else b["kalshi_price"]) for b in rows]
        m, t, p, lo, hi, nc = cluster_stats(pv, keys)
        print(f"2. Pinnacle CLV (close fair - entry fair): {m*100:+.2f}pp  t={t:+.2f}  P(>0)={p:.2f}  [{lo*100:+.2f},{hi*100:+.2f}]")
        kv = [(b["pin_fair_close"] - side_close_mid(b), k) for b, k in zip(rows, keys) if side_close_mid(b) is not None]
        if len(kv) >= 5:
            m, t, p, lo, hi, nc = cluster_stats([v for v, _ in kv], [k for _, k in kv])
            print(f"3. sharp close - Kalshi close mid: {m*100:+.2f}pp  t={t:+.2f}  (n={len(kv)}; >0 = Kalshi's close still cheap vs sharp)")
        st = [b for b in rows if b["status"] in ("won", "lost")]
        if st:
            exp = sum(b["pin_fair_close"] for b in st)
            print(f"   wins: actual {sum(b['status']=='won' for b in st)} of {len(st)} | expected at sharp close {exp:.1f} | at price paid {sum(b['kalshi_price'] for b in st):.1f}")
    print("\nHow to read it: trust (1) only with enough settled-independent games; it uses no outcomes, so it needs far "
          "fewer bets than realized P&L. Rough power (per-game sd of (1) is ~12%): a +4% sharp-close EV "
          "reaches t~2 at ~35 games, +3% at ~60, +2% at ~140.")


if __name__ == "__main__":
    main()
