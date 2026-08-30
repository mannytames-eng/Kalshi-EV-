"""
UFC/MMA fight-card watcher.

Thesis: a full-session sweep across every sport with a live market
(2026-08-25) found MMA moneyline the closest signal anywhere -- individual-
fighter markets get less market-making/retail attention than team sports,
the same structural reason K worked before Kalshi's own pricing caught up.
Best single result across 9 fights checked by hand was +1.98pp raw, just
under the 2% funding floor -- not yet real, but the closest thing found in
the whole sweep (soccer's best was ~1.5pp, MLB's various re-checks all came
in smaller). This automates that check going forward instead of re-running
it by hand every time a card is announced.

Mechanics:
  1. Poll Kalshi's KXUFCFIGHT series for open, not-yet-started fight events
     (free, /events endpoint).
  2. Fetch Pinnacle's mma_mixed_martial_arts odds in ONE bulk call (h2h only,
     ~1-2 credits total regardless of how many fights are on -- the bulk
     list endpoint supports h2h fine, unlike the alternate-line markets that
     forced corners/onto per-event calls elsewhere in this codebase).
  3. Match each open Kalshi fight to a Pinnacle event by fighter name, reusing
     kalshi_ev_scanner._soccer_name_match -- the same conservative, no-
     hardcoded-roster matcher already proven for soccer club names. Safe by
     construction: BOTH Kalshi names must match, to DIFFERENT Pinnacle
     fighters, uniquely, or no match at all.
  4. De-vig Pinnacle's h2h, compare against both fighters' YES/NO Kalshi
     prices (raw edge on both sides of both markets, same math as everywhere
     else in this codebase).
  5. Track the best raw edge seen per fight (checked once, not re-spent
     every run the same day). Crossing ALERT_RAW_EDGE_PP fires one Discord
     alert per fight, never repeats. A fight drops out of watch once it's
     been fought (or its card date has passed).

Runs on its own daily-ish cadence from the main scan loop, same shape as the
freshness watcher. Read-only except for the Discord side effect -- never
stakes anything, never adds MMA to the funded scanner on its own.
"""
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

import kalshi_ev_scanner as s

KALSHI_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_SPORT = "mma_mixed_martial_arts"
FIGHT_SERIES = "KXUFCFIGHT"

# Watch bar matches the real EDGE_THRESHOLD (2%), not K's old 4pp funding
# bar -- this is scoping "is there anything here at all" (the best result
# found by hand so far was 1.98pp), not "is this ready to fund."
ALERT_RAW_EDGE_PP = 2.0
MAX_FIGHTS_PER_RUN = 40   # bounds credit spend on a night with several cards


def _get_kalshi(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _get_odds_bulk_h2h() -> tuple[list, Optional[object]]:
    """One bulk Pinnacle h2h fetch across the whole MMA sport. Also records
    the response into kalshi_ev_scanner.LAST_ODDS_USAGE so the dashboard's
    credit panel reflects this spend -- urllib's response doesn't match
    _record_odds_usage()'s expected requests.Response shape, so replicate
    its two header reads directly rather than importing requests just for
    this (not installed everywhere this module might run standalone)."""
    url = (f"https://api.the-odds-api.com/v4/sports/{ODDS_SPORT}/odds"
           f"?apiKey={s.ODDS_API_KEY}&bookmakers=pinnacle&markets=h2h&oddsFormat=american")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        events = json.loads(r.read())
        rem = r.headers.get("x-requests-remaining")
        used = r.headers.get("x-requests-used")
        if rem is not None:
            s.LAST_ODDS_USAGE["remaining"] = int(float(rem))
        if used is not None:
            s.LAST_ODDS_USAGE["used"] = int(float(used))
        s.LAST_ODDS_USAGE["at"] = datetime.now(timezone.utc).isoformat()
    return events, None


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
        print(f"  WARNING: could not save MMA-watch state: {exc}")


def _match_pinnacle_fight(sub_a: str, sub_b: str, odds_events: list) -> Optional[dict]:
    """Match a Kalshi fight (two yes_sub_title fighter names) to a Pinnacle
    event. Safe by construction, same guard as _soccer_safe_match: BOTH
    Kalshi names must confidently match, to DIFFERENT Pinnacle fighters,
    and the match must be the only one -- any ambiguity returns None."""
    cands = []
    for e in odds_events:
        away, home = e.get("away_team", ""), e.get("home_team", "")
        if not away or not home:
            continue
        a_away, a_home = s._soccer_name_match(sub_a, away), s._soccer_name_match(sub_a, home)
        b_away, b_home = s._soccer_name_match(sub_b, away), s._soccer_name_match(sub_b, home)
        if (a_away and b_home and not (a_home or b_away)) or \
           (a_home and b_away and not (a_away or b_home)):
            cands.append(e)
    return cands[0] if len(cands) == 1 else None


def run(state_file: str, send_discord: Callable[[dict, str], bool]) -> dict:
    """One run of the watcher. Call this on its own slow (daily) cadence from
    the main scan loop. Returns a small summary dict for logging/status."""
    now = datetime.now(timezone.utc)
    state = _load_state(state_file)
    fights: dict = state.setdefault("fights", {})   # event_ticker -> {names, best_edge, alerted, last_checked}

    try:
        kev = _get_kalshi(f"{KALSHI_PUBLIC}/events?series_ticker={FIGHT_SERIES}&status=open&limit=100")
    except Exception as exc:
        print(f"  MMA watcher: Kalshi events fetch failed ({exc}), skipping this run")
        return {"error": str(exc)}

    open_events = kev.get("events", [])
    due = [ev for ev in open_events
           if not fights.get(ev.get("event_ticker", ""), {}).get("alerted")][:MAX_FIGHTS_PER_RUN]

    if not due:
        _save_state(state_file, state)
        return {"open_fights": len(open_events), "checked": 0, "alerts": 0}

    try:
        odds_events, _ = _get_odds_bulk_h2h()
    except Exception as exc:
        print(f"  MMA watcher: Pinnacle odds fetch failed ({exc}), skipping this run")
        return {"error": str(exc)}

    checked, matched, alerts_fired = 0, 0, []
    for ev in due:
        et = ev.get("event_ticker", "")
        try:
            md = _get_kalshi(f"{KALSHI_PUBLIC}/markets?event_ticker={et}&status=open&limit=10")
        except Exception as exc:
            print(f"  MMA watcher: markets fetch failed for {et}: {exc}")
            continue
        markets = md.get("markets", [])
        if len(markets) != 2:
            continue
        checked += 1
        sub_a, sub_b = markets[0].get("yes_sub_title", ""), markets[1].get("yes_sub_title", "")
        oe = _match_pinnacle_fight(sub_a, sub_b, odds_events)
        if not oe:
            continue
        # Skip fights that have already started/concluded. Kalshi's market
        # stays "open" for days after a fight actually happens (same issue
        # found in the MLB sweep earlier this session) -- a decided fight
        # prices at ~99/100 vs ~0/1, which reads as a huge "edge" against a
        # stale/irrelevant Pinnacle pregame line. Caught live 2026-08-26:
        # Piazzon vs Uriel fired a false +37.5pp alert this way on the
        # watcher's very first run. Pinnacle's per-fight commence_time is the
        # real signal here (Kalshi's own close_time reflects the card window,
        # not the individual bout).
        ct = oe.get("commence_time")
        if ct:
            try:
                if datetime.fromisoformat(ct.replace("Z", "+00:00")) <= now:
                    continue
            except (ValueError, AttributeError):
                pass
        outs = {}
        for bm in oe.get("bookmakers", []):
            if bm.get("key") != "pinnacle":
                continue
            for mk in bm.get("markets", []):
                if mk.get("key") == "h2h":
                    for o in mk.get("outcomes", []):
                        outs[o["name"]] = o["price"]
        if len(outs) != 2:
            continue
        matched += 1
        names = list(outs.keys())
        p0, p1 = s.no_vig_prob(outs[names[0]], outs[names[1]])
        fair = {names[0]: p0, names[1]: p1}

        best_this_fight, best_desc = 0.0, ""
        for m in markets:
            sub = m.get("yes_sub_title", "")
            bid = float(m.get("yes_bid_dollars") or 0) * 100
            ask = float(m.get("yes_ask_dollars") or 0) * 100
            if bid == 0 and ask == 0:
                continue
            f = next((fair[n] for n in names if s._soccer_name_match(sub, n)), None)
            if f is None:
                continue
            f100 = f * 100
            yes_edge, no_edge = f100 - ask, bid - f100
            if yes_edge > best_this_fight:
                best_this_fight, best_desc = yes_edge, f"{sub} YES @ {ask/100:.2f} (fair {f100:.1f}%)"
            if no_edge > best_this_fight:
                best_this_fight, best_desc = no_edge, f"{sub} NO @ {bid/100:.2f} (fair {f100:.1f}%)"

        rec = fights.setdefault(et, {"title": ev.get("title", ""), "best_edge": None, "alerted": False})
        rec["best_edge"] = best_this_fight if rec["best_edge"] is None else max(rec["best_edge"], best_this_fight)
        rec["last_checked"] = now.isoformat()
        # Printed unconditionally (not just on alert) so a run's stdout always
        # shows what the best candidate was, without needing to guess whether
        # a Discord alert actually reached the user -- log retention is too
        # short to check that after the fact (found 2026-08-30: verified a
        # real +2.54pp edge by hand that the watcher may or may not have
        # alerted on before the fight happened, with no way to confirm from
        # logs after the window rolled off).
        if best_this_fight > 0:
            print(f"  MMA watcher: {ev.get('title','?')} best={best_this_fight:.2f}pp -- {best_desc}")
        if best_this_fight >= ALERT_RAW_EDGE_PP and not rec["alerted"]:
            rec["alerted"] = True
            alerts_fired.append((et, rec, best_this_fight))
            print(f"  MMA watcher: ALERT FIRED — {et} {best_desc} edge={best_this_fight:.2f}pp")

    # drop fights whose card has already started/passed -- nothing left to check
    for et in list(fights.keys()):
        if et not in {ev.get("event_ticker", "") for ev in open_events}:
            fights.pop(et, None)

    if alerts_fired:
        fields = [{
            "name": f"🥊 {et}",
            "value": f"{rec['title']}\nBest raw edge seen: **{best:.2f}pp**",
            "inline": False,
        } for et, rec, best in alerts_fired]
        embed = {
            "title": "UFC/MMA watcher — edge found",
            "color": 0xC69A4E,
            "fields": fields[:25],
            "footer": {"text": f"{len(open_events)} open fights · {checked} checked · {matched} matched to Pinnacle this run"},
        }
        send_discord(embed, f"🥊 MMA watch: {len(alerts_fired)} fight(s) cleared {ALERT_RAW_EDGE_PP}pp raw edge")

    _save_state(state_file, state)
    return {
        "open_fights": len(open_events), "checked": checked, "matched": matched,
        "alerts_fired": len(alerts_fired),
    }
