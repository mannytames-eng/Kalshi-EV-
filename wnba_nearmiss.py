#!/usr/bin/env python3
"""
wnba_nearmiss.py — READ-ONLY WNBA prop near-miss report. Places NO bets.

Runs scan_wnba_player_props() against live Pinnacle (via the Odds API) and PUBLIC
Kalshi market data, captures the FULL sub-threshold snapshot (every scanned market,
not just >=2% edges), and prints the markets closest to the 2% edge threshold with
each one's disposition. WNBA is hypothetical-shadow — nothing here trades real money.

Usage:  python3 wnba_nearmiss.py
"""
import contextlib
import io
import json
import urllib.parse
import urllib.request

import kalshi_ev_scanner as S


def _public_kalshi_get(path, params=None, _retries=3):
    """Kalshi market-data GETs are public — no signing/credentials needed for the
    events/markets endpoints this scan reads. Avoids needing the private key."""
    url = S.KALSHI_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.load(r)


def main():
    S.kalshi_get = _public_kalshi_get   # read-only: swap signed GET for public GET

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = S.scan_wnba_player_props()
    edges, snap = res if isinstance(res, tuple) else (res, {})
    log = buf.getvalue()

    print(f"WNBA prop scan — games priced: {log.count('props fetched')} | "
          f"sides scanned: {len(snap)} | edges >=2%: {len(edges)}")
    if not snap:
        print("No Pinnacle-priced WNBA props on the board — either too early "
              "(Pinnacle hasn't posted), no games, or games already started. "
              "Cannot measure how close we got.")
        return

    rows = [{"k": k, **v} for k, v in snap.items()]
    rows.sort(key=lambda r: r.get("adj_edge", -9), reverse=True)

    titles = {}
    for r in rows[:8]:
        tk = r["k"].split("|")[0]
        if tk not in titles:
            try:
                titles[tk] = _public_kalshi_get(f"/markets/{tk}").get("market", {}).get("title", tk)
            except Exception:
                titles[tk] = tk

    edge_keys = {f"{e['ticker']}|{e['side']}" for e in edges}
    print(f"\n{'edge%':>6} {'side':>4} {'K':>5} {'fair':>5}  market                          disposition")
    for r in rows[:8]:
        tk, side = r["k"].split("|")
        kp = int(round(r["kalshi"] * 100))
        if r["k"] in edge_keys:
            disp = "FLAGGED >=2%"
        elif r["edge_pct"] < 2.0:
            disp = "under threshold (genuine near-miss)"
        else:
            hit = [l for l in log.splitlines() if f"Kalshi {kp}." in l or f"Kalshi {kp}%" in l]
            disp = "REJECTED (phantom) — " + (
                hit[0].split("REJECTED —")[1].strip()[:50] if hit and "REJECTED —" in hit[0]
                else "book consensus didn't confirm")
        print(f"{r['edge_pct']:>+6.1f} {side:>4} {r['kalshi']:>5.2f} {r['fair']:>5.2f}  "
              f"{titles.get(tk, tk):<32.32}{disp}")


if __name__ == "__main__":
    main()
