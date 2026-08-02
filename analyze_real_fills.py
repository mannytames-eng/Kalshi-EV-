#!/usr/bin/env python3
"""
analyze_real_fills.py — did real execution keep the strikeout edge?

READ-ONLY. Pulls YOUR real strikeout (KXMLBKS) fills from the Kalshi API
(authenticated with the same creds the scanner already uses), matches each to the
scanner's flagged paper bet, and reports whether real execution preserved the
edge: fill slippage vs the flag price, real win rate vs the price you paid, and
real ROI vs the paper model. Outcome (won/lost) comes from the scanner's own
record, so the ONLY thing read from your account is the price you filled at.
Places NO orders. Fetches nothing but KXMLBKS.

Run where the Kalshi creds live:
    railway run --service <scanner-service> python3 analyze_real_fills.py
or locally with KALSHI_API_KEY + KALSHI_PRIVATE_KEY (or KALSHI_PRIVKEY_B64) set.

Model bets come from the deployed dashboard (BETS_URL, default /api/paper).
"""
import json
import os
import urllib.request

import kalshi_ev_scanner as S

BETS_URL = os.environ.get("BETS_URL", "https://evscanner-production.up.railway.app/api/paper")


def _paginated(path, key):
    """All pages of a signed Kalshi portfolio endpoint."""
    out, cursor = [], None
    for _ in range(60):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = S.kalshi_get(path, params)
        out.extend(data.get(key, []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def _model_bets():
    """Scanner's settled KXMLBKS bets keyed by 'TICKER|SIDE' (has flag price,
    edge, and the win/lost outcome)."""
    raw = json.load(urllib.request.urlopen(BETS_URL, timeout=20))
    rows = raw if isinstance(raw, list) else (raw.get("bets") or raw.get("rows") or [])
    m = {}
    for b in rows:
        tk = (b.get("ticker") or "").upper()
        if tk.startswith("KXMLBKS"):
            m[f"{tk}|{(b.get('side') or '').upper()}"] = b
    return m


def main():
    print("Pulling your real KXMLBKS fills (read-only)…")
    fills = _paginated("/portfolio/fills", "fills")

    # Aggregate real fills by (ticker, side): weighted-avg fill price + count.
    real = {}
    for f in fills:
        tk = (f.get("ticker") or "").upper()
        if not tk.startswith("KXMLBKS"):
            continue
        if (f.get("action") or "buy") != "buy":     # opening buys only
            continue
        side = (f.get("side") or "").upper()          # yes/no -> YES/NO
        cnt = int(f.get("count") or 0)
        px = f.get("yes_price") if side == "YES" else f.get("no_price")
        if not cnt or px is None:
            continue
        r = real.setdefault(f"{tk}|{side}", {"count": 0, "cost_cents": 0})
        r["count"] += cnt
        r["cost_cents"] += cnt * int(px)

    model = _model_bets()
    print(f"real KXMLBKS positions: {len(real)}   |   model KXMLBKS bets on record: {len(model)}\n")
    if not real:
        print("No strikeout fills found on the account. Either none were placed, or the")
        print("Kalshi fills endpoint/field names differ — share the raw first fill and I'll adjust.")
        return

    matched, unmatched = [], []
    for key, r in real.items():
        tk, side = key.split("|")
        avg_fill = r["cost_cents"] / r["count"] / 100.0   # dollars
        mb = model.get(key)
        if not mb or mb.get("status") not in ("won", "lost"):
            unmatched.append((tk, side, avg_fill, r["count"]))
            continue
        won = mb["status"] == "won"
        real_pnl = r["count"] * (1.0 - avg_fill) if won else -r["count"] * avg_fill
        matched.append({
            "ticker": tk, "side": side, "n": r["count"],
            "real_fill": avg_fill, "flag": mb.get("kalshi_price"),
            "edge": mb.get("edge_pct"), "won": won, "pnl": real_pnl,
            "paper_pnl": mb.get("paper_pnl"),
        })

    print(f"{'ticker':38s} {'sd':3s} {'fill':>4s} {'flag':>4s} {'slip':>5s} {'edge':>5s} {'WL':>2s} {'real$':>7s}")
    slips = []
    for x in sorted(matched, key=lambda z: z["ticker"]):
        slip = (x["real_fill"] - x["flag"]) * 100 if x["flag"] is not None else None
        if slip is not None:
            slips.append(slip)
        print(f"{x['ticker']:38.38s} {x['side']:3s} {x['real_fill']*100:4.0f} "
              f"{(x['flag'] or 0)*100:4.0f} {(slip if slip is not None else 0):+5.1f} "
              f"{x['edge'] or 0:5.1f} {'W' if x['won'] else 'L':>2s} {x['pnl']:+7.2f}")

    n = len(matched)
    if n:
        wins = sum(1 for x in matched if x["won"])
        cost = sum(x["n"] * x["real_fill"] for x in matched)
        pnl = sum(x["pnl"] for x in matched)
        avg_impl = sum(x["real_fill"] for x in matched) / n * 100
        flat_u = sum((1 - x["real_fill"]) / x["real_fill"] if x["won"] else -1.0 for x in matched)
        paper_pnl = sum((x["paper_pnl"] or 0) for x in matched)
        print(f"\n── REAL execution vs the model  (n={n} matched & settled) ──")
        print(f"  real win rate:      {wins/n*100:.1f}%   vs implied {avg_impl:.1f}%  →  {wins/n*100-avg_impl:+.1f}pp beat-the-price")
        print(f"  avg fill slippage:  {sum(slips)/len(slips):+.2f}¢ vs the flag price   (+ = you paid MORE than flagged)")
        print(f"  real ROI:           {pnl/cost*100:+.1f}%   (net ${pnl:+.2f} on ${cost:.2f} actually staked)")
        print(f"  real flat units:    {flat_u:+.2f}u")
        print(f"  model paper P&L on these same bets (Kelly): ${paper_pnl:+.2f}")
        print("\n  Read: if real ROI ≈ the model and slippage ≈ 0, execution kept the edge.")
        print("  If slippage is meaningfully positive or real ROI << paper, fills are eroding it.")
    if unmatched:
        print(f"\n  {len(unmatched)} real strikeout position(s) had no settled model bet "
              f"(bet a ticker/side the scanner didn't flag, or not yet settled).")


if __name__ == "__main__":
    main()
