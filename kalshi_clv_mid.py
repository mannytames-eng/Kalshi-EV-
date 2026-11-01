"""Mid-to-mid closing-line value from Kalshi 1-minute candlesticks.

Why this exists (2026-11-01): the old CLV compared the price we PAID at entry
(YES ask / NO-side ask) with the same-side ask at the last poll before game
start. Entry spreads are tight (NBA props: median 1c) but market makers widen or
pull quotes in the final minutes (mean 12.6c, last candle), and a wider spread
pushes the ask up / the bid down on EVERY bet -- counted as "CLV" whether or not
the market actually moved. Measured on 43 NBA bets: ask/bid CLV +3.3c = -1.9c real
mid movement + 5.2c spread widening. This module measures what actually moved:
the quote MIDPOINT at entry vs the midpoint of the last TIGHT quote before game
start (a pulled / one-sided quote's midpoint is meaningless, so those are skipped).

Pure functions + one fetch helper that takes the caller's kalshi_get, so it is
unit-testable without network or the scanner's heavy imports.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

MAX_CLOSE_SPREAD_C = 6.0      # ignore closing quotes wider than this (cents)
CLOSE_LOOKBACK_MIN = 180      # how far before game start to look for a tight quote
ENTRY_LOOKBACK_MIN = 120      # a quiet market has no candle for the entry minute -> use the prevailing quote
ENTRY_SLACK_S = 60            # a candle ending <=60s after the flag still reflects the flag-time quote


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _first(o, *keys) -> Optional[float]:
    if not isinstance(o, dict):
        return None
    for k in keys:
        v = _num(o.get(k))
        if v is not None:
            return v
    return None


def norm_candles(raw: list) -> List[Tuple[int, float, float]]:
    """Raw Kalshi candles -> [(end_ts, yes_bid_c, yes_ask_c)] in cents, ascending.
    Handles both schemas (live: `close_dollars`, historical: `close`). Candles with
    a missing/one-sided/crossed quote are dropped."""
    out = []
    for c in raw or []:
        ts = c.get("end_period_ts")
        bid = _first(c.get("yes_bid"), "close_dollars", "close")
        ask = _first(c.get("yes_ask"), "close_dollars", "close")
        if ts is None or bid is None or ask is None:
            continue
        bid, ask = bid * 100.0, ask * 100.0
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        out.append((int(ts), bid, ask))
    out.sort(key=lambda r: r[0])
    return out


def compute_mid_quotes(candles: List[Tuple[int, float, float]], flag_ts: float, game_ts: float,
                       max_spread: float = MAX_CLOSE_SPREAD_C) -> dict:
    """Entry + closing midpoints (YES-price cents) from normalized candles.

    entry : last candle ending <= flag + 60s (the quote prevailing when we bet).
    close : last candle ending <= game start whose spread <= max_spread.
    Returns {status, entry_mid_pct, entry_spread_c, close_mid_pct, close_spread_c,
    close_quote_ts}; status is "ok", "no_entry_quote", "no_tight_close" or "no_data"."""
    out = {"entry_mid_pct": None, "entry_spread_c": None, "close_mid_pct": None,
           "close_spread_c": None, "close_quote_ts": None}
    if not candles:
        return {"status": "no_data", **out}
    entry = [c for c in candles if c[0] <= flag_ts + ENTRY_SLACK_S]
    if not entry:
        return {"status": "no_entry_quote", **out}
    e = entry[-1]
    out["entry_mid_pct"] = round((e[1] + e[2]) / 2, 2)
    out["entry_spread_c"] = round(e[2] - e[1], 2)
    tight = [c for c in candles if c[0] <= game_ts and (c[2] - c[1]) <= max_spread]
    if not tight:
        return {"status": "no_tight_close", **out}
    c = tight[-1]
    out["close_mid_pct"] = round((c[1] + c[2]) / 2, 2)
    out["close_spread_c"] = round(c[2] - c[1], 2)
    out["close_quote_ts"] = c[0]
    return {"status": "ok", **out}


def mid_clv(entry_mid: Optional[float], close_mid: Optional[float], side: str) -> Optional[float]:
    """Side-aware mid-to-mid CLV in cents. Positive = the market moved toward the bet."""
    if entry_mid is None or close_mid is None:
        return None
    return (entry_mid - close_mid) if side == "NO" else (close_mid - entry_mid)


def _is_404(exc: Exception) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def fetch_candles(kalshi_get: Callable, ticker: str, start_ts: int, end_ts: int) -> list:
    """1-minute candles for one market. Recent markets live under /series/..., older
    settled ones under /historical/...; try live first, fall back on 404 / empty.
    Raises on anything other than a 404 (rate limit, 5xx) so the caller can retry later."""
    series = ticker.split("-")[0]
    params = {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": 1}
    for path in (f"/series/{series}/markets/{ticker}/candlesticks",
                 f"/historical/markets/{ticker}/candlesticks"):
        try:
            cs = (kalshi_get(path, params) or {}).get("candlesticks") or []
        except Exception as exc:  # noqa: BLE001
            if _is_404(exc):
                continue
            raise
        if cs:
            return cs
    return []


def restate_bet(kalshi_get: Callable, ticker: str, flag_ts: float, game_ts: float) -> dict:
    """Fetch candles around entry and close for one bet and compute its mid quotes."""
    start = int(flag_ts - ENTRY_LOOKBACK_MIN * 60)
    end = int(game_ts + 60)
    close_start = int(game_ts - CLOSE_LOOKBACK_MIN * 60)
    if close_start <= flag_ts + 3 * 60:      # flagged shortly before start: one window covers both
        raw = fetch_candles(kalshi_get, ticker, start, end)
    else:                                    # long gap: entry window + close window, merged
        raw = (fetch_candles(kalshi_get, ticker, start, int(flag_ts + 3 * 60))
               + fetch_candles(kalshi_get, ticker, close_start, end))
    return compute_mid_quotes(norm_candles(raw), flag_ts, game_ts)
