"""Pre-registered FORWARD test of the NFL favorite / primetime-underdog hypotheses.

Registered 2026-10-20, BEFORE any of the counted games were played. Only games
kicking off on/after REGISTERED_ON count, so nothing here can be tuned to data
already seen. Hypotheses and the stopping rule are fixed in advance:

  H1  Kalshi-priced NFL favorites (>=65% at kickoff) win LESS often than their
      Kalshi price implies (the 'bettors overbet favorites' claim).
  H2  Primetime underdogs win MORE often than their Kalshi price implies.

Stopping rule: judge each once, at n >= N_REQUIRED (below), at alpha = 0.01
(Bonferroni for the two tests). Before that this script reports INSUFFICIENT
DATA no matter how good the running numbers look -- peeking at a streak is
exactly how the 'underdogs won 8 of 9' impression arose.

Inputs (produced by collect_kalshi_prices.py on Railway + the nflverse CSV):
  python3 forward_test.py nfl.json games.csv
"""
import csv, json, math, re, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

REGISTERED_ON = "2026-10-21"
N_REQUIRED = {"H1": 350, "H2": 150}   # games; ~2 seasons / ~3 seasons of NFL respectively
ET = ZoneInfo("America/New_York")
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
CODES = set("ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAC KC LV LAC LAR MIA MIN NE NO NYG NYJ PHI PIT SF SEA TB TEN WAS".split())
ALIAS = {"JAC": "JAX", "LAR": "LA"}

def main(nfl_path, games_path, since=REGISTERED_ON):
    G = {(r["gameday"], r["home_team"], r["away_team"]): r for r in csv.DictReader(open(games_path))}
    byev = {}
    for m in json.load(open(nfl_path)):
        byev.setdefault(m["event"], []).append(m)
    obs = []
    for e, ms in byev.items():
        mm = re.match(r"KXNFLGAME-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$", e)
        if not mm or len(ms) != 2:
            continue
        yy, mon, dd, t = mm.groups(); date = f"20{yy}-{MON[mon]:02d}-{dd}"
        if date < since:
            continue
        sp = [(t[:i], t[i:]) for i in range(2, len(t) - 1) if t[:i] in CODES and t[i:] in CODES]
        if len(sp) != 1:
            continue
        away, home = [ALIAS.get(x, x) for x in sp[0]]
        r = G.get((date, home, away))
        if not r or not r["gametime"]:
            continue
        ko = datetime.strptime(f"{date} {r['gametime']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET).astimezone(timezone.utc).timestamp()
        hm = [m for m in ms if ALIAS.get(m["ticker"].split("-")[-1], m["ticker"].split("-")[-1]) == home]
        if len(hm) != 1:
            continue
        hm = hm[0]; am = [m for m in ms if m is not hm][0]
        def px(m):
            best = None
            for c in m["candles"] or []:
                if c[0] <= ko: best = c
            if not best or best[1] is None or best[2] is None or best[2] - best[1] > 0.10:
                return None
            return (best[1] + best[2]) / 2
        ph, pa = px(hm), px(am)
        if ph is None or pa is None:
            continue
        p_home = (ph + (1 - pa)) / 2
        obs.append(dict(p=p_home, hw=1 if hm["result"] == "yes" else 0, pt=r["gametime"] >= "19:00"))
    print(f"Pre-registered {REGISTERED_ON}; settled NFL games counted so far: {len(obs)}")
    fav = [(max(o["p"], 1 - o["p"]), o["hw"] if o["p"] >= .5 else 1 - o["hw"]) for o in obs if max(o["p"], 1 - o["p"]) >= .65]
    dog = [(min(o["p"], 1 - o["p"]), o["hw"] if o["p"] < .5 else 1 - o["hw"]) for o in obs if o["pt"]]
    for name, xs, sign in (("H1 favorites>=65% underperform price", fav, -1), ("H2 primetime dogs outperform price", dog, +1)):
        key = name[:2]; n = len(xs)
        if n == 0:
            print(f"  {name}: n=0 / {N_REQUIRED[key]} required -> INSUFFICIENT DATA"); continue
        e = sum(p for p, _ in xs) / n; a = sum(w for _, w in xs) / n
        se = math.sqrt(e * (1 - e) / n); z = (a - e) / se
        verdict = "INSUFFICIENT DATA (do not act)" if n < N_REQUIRED[key] else (
            "SUPPORTED at alpha=0.01" if sign * z > 2.576 else "NOT supported")
        print(f"  {name}: n={n}/{N_REQUIRED[key]}  implied {e:.1%}  actual {a:.1%}  gap {100*(a-e):+.1f}pp  z={z:+.2f}  -> {verdict}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else REGISTERED_ON)
