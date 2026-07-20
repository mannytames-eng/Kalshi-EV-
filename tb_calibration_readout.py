#!/usr/bin/env python3
"""
TB calibration readout — split by cohort (pre/post the 2026-07-14 fair-ceiling
fix), then funded vs shadow, then by model-fair band. Answers "did the fix work?"
without the pre-fix bets confounding the numbers.

Diagnostic only. Reads the live paper history; touches nothing. Re-run over the
coming days/weeks to watch the post-fix sample mature.

    python3 tb_calibration_readout.py            # fetch live /api/paper
    python3 tb_calibration_readout.py paper.json # use a saved snapshot

Reads: fair, side, status, tb_cohort, flagged_at, shadow, clv/clv_pct.
"""
import json, math, sys, urllib.request

API = "https://evscanner-production.up.railway.app/api/paper"
FIX_DATE = "2026-07-14"          # fair-ceiling shadow shipped
BANDS = [(0.0, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 1.01)]
CEILING = 0.45                   # TB_CAL_FAIR_CEILING — bands >= this are shadowed post-fix


def load(argv):
    if len(argv) > 1:
        return json.load(open(argv[1]))
    with urllib.request.urlopen(API, timeout=30) as r:
        return json.load(r)


def side_fair(b):
    f = b.get("fair")
    if f is None:
        return None
    return f if b.get("side") == "YES" else 1.0 - f


def cohort(b):
    """post = tagged post_fix OR flagged on/after the fix date; else pre."""
    if b.get("tb_cohort") == "post_fix_20260714":
        return "post"
    return "post" if (b.get("flagged_at", "")[:10] >= FIX_DATE) else "pre"


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def binom_z_p(k, n, p0):
    """Two-sided normal-approx test of realized rate vs claimed p0."""
    if n == 0 or p0 <= 0 or p0 >= 1:
        return (float("nan"), float("nan"))
    se = math.sqrt(p0 * (1 - p0) / n)
    z = (k / n - p0) / se
    p = math.erfc(abs(z) / math.sqrt(2))   # two-sided
    return (z, p)


def summarize(bets, label, indent=0):
    settled = [b for b in bets if b.get("status") in ("won", "lost") and side_fair(b) is not None]
    pad = "  " * indent
    if not settled:
        print(f"{pad}{label}: (no settled bets)")
        return
    k = sum(b["status"] == "won" for b in settled)
    n = len(settled)
    claimed = sum(side_fair(b) for b in settled) / n
    realized = k / n
    gap = realized - claimed
    lo, hi = wilson(k, n)
    z, pv = binom_z_p(k, n, claimed)
    clvs = [b.get("clv") for b in settled if b.get("clv") is not None]
    clv = (sum(clvs) / len(clvs)) if clvs else None
    sig = "***" if pv < 0.01 else "**" if pv < 0.05 else "*" if pv < 0.10 else ""
    clv_s = f"  CLV {clv:+.1f}" if clv is not None else ""
    print(f"{pad}{label:<26} n={n:>3}  claim {claimed*100:4.1f}%  real {realized*100:4.1f}% "
          f"[{lo*100:4.1f},{hi*100:4.1f}]  gap {gap*100:+5.1f}pp {sig:<3}{clv_s}")


def by_band(bets, indent):
    pad = "  " * indent
    for lo, hi in BANDS:
        g = [b for b in bets if b.get("status") in ("won", "lost")
             and (sf := side_fair(b)) is not None and lo <= sf < hi]
        if not g:
            continue
        tag = " (funded band)" if hi <= CEILING else " (ceiling-shadowed band)" if lo >= CEILING else ""
        summarize(g, f"fair [{lo:.2f},{hi:.2f}){tag}", indent)


def main():
    d = load(sys.argv)
    tb = [b for b in d["bets"] if b.get("ticker", "").startswith("KXMLBTB")]
    print(f"KXMLBTB bets: {len(tb)}  (settled analyzed below)")
    print("legend: claim=mean model side-fair, real=win rate, [..]=95% Wilson CI on real,")
    print("        gap=real-claim (neg=overconfident), sig vs claim: * p<.10 ** p<.05 *** p<.01, CLV=mean\n")

    for coh in ("pre", "post"):
        sub = [b for b in tb if cohort(b) == coh]
        head = "PRE-fix (<07-14, old regime — bets the ceiling was built to stop)" if coh == "pre" \
               else "POST-fix (>=07-14, tb_cohort=post_fix_20260714 — THE VALIDATION SAMPLE)"
        print(f"══ {head} ══")
        summarize(sub, "  all TB")
        for who, flag in (("funded", False), ("shadow", True)):
            grp = [b for b in sub if bool(b.get("shadow")) == flag]
            if not grp:
                continue
            summarize(grp, f"{who}", indent=1)
            by_band(grp, indent=2)
        print()

    # decision line: is the post-fix FUNDED book yet judgeable / calibrated?
    postf = [b for b in tb if cohort(b) == "post" and not b.get("shadow")
             and b.get("status") in ("won", "lost") and side_fair(b) is not None]
    n = len(postf)
    print("── VERDICT READINESS ──")
    if n < 30:
        print(f"  Post-fix funded n={n}. Underpowered — need ~30+ to resolve a ~7pp gap "
              f"from noise (SE at n=30 ≈ 9pp). Keep accumulating; do not conclude yet.")
    else:
        k = sum(b["status"] == "won" for b in postf)
        claimed = sum(side_fair(b) for b in postf) / n
        z, pv = binom_z_p(k, n, claimed)
        verdict = ("MISCALIBRATED (overconfident)" if (k/n - claimed) < 0 and pv < 0.05
                   else "calibrated within noise")
        print(f"  Post-fix funded n={n}: gap {(k/n-claimed)*100:+.1f}pp, p={pv:.3f} → {verdict}")

    recal_readiness(tb)


# Gates for wiring an over-haircut recalibration (see design notes in the repo).
MIN_FIT_N = 50     # min settled overs to fit even a 1-parameter constant haircut
PLATT_N   = 100    # settled overs before a 2-param logistic/curve fit is worth it


def recal_readiness(tb):
    """Is there enough data to estimate the over-side haircut and wire recal?

    Pricing (`fair`) is UNCHANGED across the 2026-07-14 staking fix (that fix only
    shadowed bets; de-vig/NB stayed unwired). So ALL settled overs are on the same
    pricing and pool for estimating the over bias — this is the recal sample, and
    it includes shadowed overs (they carry real outcomes)."""
    overs = [b for b in tb if b.get("side") == "YES"
             and b.get("status") in ("won", "lost") and side_fair(b) is not None]
    print("\n── RECALIBRATION READINESS (over-haircut) ──")
    n = len(overs)
    if n == 0:
        print("  no settled overs yet"); return
    k = sum(b["status"] == "won" for b in overs)
    claimed = sum(side_fair(b) for b in overs) / n
    real = k / n
    gap = real - claimed                    # negative = overconfident
    se = math.sqrt(real * (1 - real) / n) if 0 < real < 1 else float("nan")
    lo, hi = gap - 1.96 * se, gap + 1.96 * se
    excl0 = (hi < 0) or (lo > 0)
    print(f"  settled overs n={n} (pooled, all cohorts — same pricing):")
    print(f"  claimed {claimed*100:.1f}%  realized {real*100:.1f}%  gap {gap*100:+.1f}pp "
          f"[95% CI {lo*100:+.1f},{hi*100:+.1f}]  => est. haircut ~{-gap*100:.1f}pp")
    # stationarity: is the bias stable enough to pool pre+post?
    def cohort_gap(coh):
        s = [b for b in overs if cohort(b) == coh]
        if not s:
            return None, 0
        kk = sum(b["status"] == "won" for b in s)
        return kk / len(s) - sum(side_fair(b) for b in s) / len(s), len(s)
    gp, np_ = cohort_gap("pre")
    gq, nq_ = cohort_gap("post")
    if gp is not None and gq is not None:
        drift = "consistent (poolable)" if abs(gp - gq) < 0.06 else "DIVERGING — use rolling window"
        print(f"  stationarity: pre {gp*100:+.1f}pp (n={np_}) vs post {gq*100:+.1f}pp (n={nq_}) → {drift}")
    # the gate
    if n >= MIN_FIT_N and excl0:
        print(f"  >>> READY: n>={MIN_FIT_N} and CI excludes 0. Fit the constant over-haircut,")
        print(f"      validate walk-forward, then wire it UNWIRED-first. Curve/Platt at n>={PLATT_N}.")
    else:
        need = int((1.96 / abs(gap)) ** 2 * real * (1 - real)) if gap else 999999
        more = max(0, need - n)
        why = []
        if n < MIN_FIT_N:
            why.append(f"n={n}<{MIN_FIT_N}")
        if not excl0:
            why.append("CI still spans 0")
        print(f"  >>> NOT READY ({', '.join(why)}). At the current gap, ~{need} overs total "
              f"(~{more} more) makes the CI exclude 0.")


if __name__ == "__main__":
    main()
