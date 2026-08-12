#!/usr/bin/env python3
"""
Daily strikeout edge-distribution snapshot — read-only diagnostic.

WHY THIS EXISTS (2026-08-10)
K bet volume ran 2.6-4.0/day for eight straight weeks, then fell to ~0 from
Aug 5. Investigation ruled out the scanner: Pinnacle coverage is 100% during
game hours, line-matching works, zero false collision-rejects. An INDEPENDENT
edge calculation (no-vig from Pinnacle's raw odds vs Kalshi's ask, bypassing
the scanner's edge code entirely) found the median taker edge is now ~0.0pp
with every market quoted exactly 1c wide -- i.e. Kalshi is now pricing
strikeouts essentially at Pinnacle fair.

Crucially the avg raw edge of FOUND bets held at 4.6-4.9pp right through the
final week rather than compressing toward the 2% floor, so this is a DISCRETE
change around Aug 4-5, not gradual in-season efficiency decay.

CORRECTION (2026-08-11): the original read here was "a market maker arrived
and tightened spreads." Kalshi candlesticks survive settlement, so the
pre-cliff spread WAS recoverable after all -- pre-game median spread was
1.00pp in the 2026-07-14..22 baseline and is 1.00pp now. Market structure
never changed; only the price LEVEL moved onto Pinnacle fair. This is
REPRICING, not restructuring. Spread is still tracked below as a tripwire in
case that changes.

This script samples the full live distribution once a day so we can tell
whether that's permanent or whether the market loosens back up -- the
automation build planned for after the World Cup is premised on this edge
existing, so it needs an answer before engineering time goes in.

Reports BOTH:
  taker edge = pinnacle_fair - kalshi_ask   (what the scanner assumes today)
  maker edge = pinnacle_fair - kalshi_bid   (resting a limit, not crossing)
The maker column matters because at 1c spreads it's the entire difference
between ~0 and ~+1pp of available edge.

Cost: 1 Odds API credit per game (~10/day). Read-only; touches no bet records.
Usage:  python3 k_edge_snapshot.py [--json-out FILE]
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kalshi_ev_scanner as s  # noqa: E402

ODDS_KEY = os.environ.get("ODDS_API_KEY", s.ODDS_API_KEY)
KALSHI_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"


def _get(url):
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read()), r.headers.get("x-requests-remaining")


def snapshot(max_games=16):
    """Sample the live K edge distribution.

    Only PRE-GAME markets count. Once a game starts Pinnacle drops its lines
    while Kalshi keeps the contract active, so in-progress games contribute
    Kalshi-only rows and silently shrink and skew the sample -- an afternoon run
    collapsed to n=2 on 2026-08-12 for exactly this reason. Skipping them keeps
    the daily numbers comparable regardless of what time the job actually fires.
    """
    kev, _ = _get(f"{KALSHI_PUBLIC}/events?series_ticker=KXMLBKS&status=open&limit=200")
    oev, rem_before = _get(
        f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/?apiKey={ODDS_KEY}"
    )
    oidx = {(s._norm(e["away_team"]), s._norm(e["home_team"])): e for e in oev}

    rows, used, rem = [], 0, rem_before
    now = datetime.now(timezone.utc)
    meta = {"events_seen": 0, "skipped_started": 0, "skipped_unparsed": 0,
            "skipped_no_odds_event": 0, "sampled": 0}
    for ev in kev.get("events", [])[:max_games]:
        et = ev.get("event_ticker", "")
        meta["events_seen"] += 1
        away, home = s._parse_mlb_event(et, s.MLB_ABBR)
        if not away or not home:
            meta["skipped_unparsed"] += 1
            continue
        # PRE-GAME ONLY -- see snapshot() docstring.
        gstart = s._parse_ticker_start_time(et)
        if gstart is not None and gstart <= now:
            meta["skipped_started"] += 1
            continue
        oe = oidx.get((s._norm(away), s._norm(home)))
        if not oe:
            meta["skipped_no_odds_event"] += 1
            continue
        meta["sampled"] += 1
        try:
            md, _ = _get(f"{KALSHI_PUBLIC}/markets?event_ticker={et}&status=open&limit=100")
            d, rem = _get(
                f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{oe['id']}"
                f"/odds/?apiKey={ODDS_KEY}&bookmakers=pinnacle"
                f"&markets=pitcher_strikeouts&oddsFormat=american"
            )
        except Exception as exc:
            print(f"  fetch error on {et}: {exc}", file=sys.stderr)
            continue
        used += 1

        pin = {}
        for bm in d.get("bookmakers", []):
            for mk in bm.get("markets", []):
                for o in mk.get("outcomes", []):
                    nm = (o.get("description") or "").strip()
                    direction = (o.get("name") or "").lower()
                    if not nm:
                        continue
                    pin.setdefault((s._norm_player(nm), float(o.get("point") or 0)), {})[
                        direction
                    ] = o.get("price")

        for m in md.get("markets", []):
            prices = s.kalshi_prices(m)
            floor = m.get("floor_strike")
            if prices is None or floor is None:
                continue
            bid, ask = prices
            title_norm = re.sub(r"[^a-z]", "", (m.get("title") or "").lower())
            for (pkey, point), od in pin.items():
                if not pkey or pkey not in title_norm:
                    continue
                if "over" not in od or "under" not in od:
                    continue
                if int(point) != int(float(floor)):
                    continue
                fair, _ = s.no_vig_prob(od["over"], od["under"])
                rows.append({
                    "market": (m.get("title") or "")[:44],
                    "line": point,
                    "pin_fair_pct": round(fair * 100, 2),
                    "kalshi_bid_pct": round(bid * 100, 2),
                    "kalshi_ask_pct": round(ask * 100, 2),
                    "spread_pp": round((ask - bid) * 100, 2),
                    "taker_edge_pp": round((fair - ask) * 100, 2),
                    "maker_edge_pp": round((fair - bid) * 100, 2),
                })
    return rows, used, rem, meta


def summarize(rows):
    if not rows:
        return {}
    tk = sorted((r["taker_edge_pp"] for r in rows), reverse=True)
    mk = sorted((r["maker_edge_pp"] for r in rows), reverse=True)
    sp = [r["spread_pp"] for r in rows]
    mid = len(tk) // 2
    return {
        "n_markets": len(rows),
        "avg_spread_pp": round(sum(sp) / len(sp), 2),
        "taker_best_pp": tk[0], "taker_median_pp": tk[mid],
        "maker_best_pp": mk[0], "maker_median_pp": mk[mid],
        # >=4pp raw is where every funded K bet historically lived (avg 4.6-4.9)
        "taker_ge_4pp": sum(1 for x in tk if x >= 4.0),
        "maker_ge_4pp": sum(1 for x in mk if x >= 4.0),
        "taker_ge_2pp": sum(1 for x in tk if x >= 2.0),
        "maker_ge_2pp": sum(1 for x in mk if x >= 2.0),
    }


def main():
    out_path = None
    if "--json-out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--json-out") + 1]

    rows, used, rem, meta = snapshot()
    summ = summarize(rows)
    stamp = datetime.now(timezone.utc).isoformat()

    pdt_hour = (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%H:%M")
    print(f"=== K edge snapshot  {stamp}  ({pdt_hour} PDT, {used} games, "
          f"{rem} credits left) ===")
    print(f"    slate: {meta['events_seen']} events | sampled {meta['sampled']} "
          f"| skipped: {meta['skipped_started']} already started, "
          f"{meta['skipped_no_odds_event']} no odds-event, "
          f"{meta['skipped_unparsed']} unparsed")
    if not rows:
        print("  no matched markets (off-hours, or no Pinnacle coverage yet)")
        return
    print(f"{'market':<46}{'fair':>7}{'bid':>6}{'ask':>6}{'taker':>8}{'maker':>8}")
    for r in sorted(rows, key=lambda r: -r["maker_edge_pp"]):
        print(f"{r['market']:<46}{r['pin_fair_pct']:>7}{r['kalshi_bid_pct']:>6}"
              f"{r['kalshi_ask_pct']:>6}{r['taker_edge_pp']:>8}{r['maker_edge_pp']:>8}")
    print()
    for k, v in summ.items():
        print(f"  {k}: {v}")

    if out_path:
        with open(out_path, "a") as f:
            f.write(json.dumps({"ts": stamp, "pdt": pdt_hour, "meta": meta,
                                "summary": summ, "rows": rows}) + "\n")
        print(f"\nappended to {out_path}")


if __name__ == "__main__":
    main()
