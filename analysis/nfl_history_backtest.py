import csv, math, collections
def f(x):
    try: return float(x)
    except: return None
R=list(csv.DictReader(open("games.csv")))
G=[]
for r in R:
    res=f(r["result"]); sp=f(r["spread_line"])
    if res is None or sp is None: continue
    r["res"]=res; r["sp"]=sp; r["tot"]=f(r["total_line"]); r["season"]=int(r["season"])
    r["hml"]=f(r["home_moneyline"]); r["aml"]=f(r["away_moneyline"])
    r["pt"]=bool(r["gametime"]) and r["gametime"]>="19:00"
    r["scr"]=f(r["home_score"])+f(r["away_score"])
    r["hrest"]=f(r["home_rest"]); r["arest"]=f(r["away_rest"])
    r["div"]=r["div_game"]=="1"
    G.append(r)
def binom_p(k,n,p=0.5):
    # exact two-sided
    if n==0: return None
    pm=lambda i: math.exp(math.lgamma(n+1)-math.lgamma(i+1)-math.lgamma(n-i+1)+i*math.log(p)+(n-i)*math.log(1-p))
    obs=pm(k); return min(1.0,sum(pm(i) for i in range(n+1) if pm(i)<=obs*(1+1e-9)))
def ats(rows, side):
    """side(r)-> 'fav'|'dog'|'home'|'away'|team picker returning True if bet side covers; returns (wins,losses,pushes)"""
    w=l=p=0
    for r in rows:
        m=r["res"]-r["sp"]            # home cover margin (>0 home covers)
        if m==0: p+=1; continue
        homecov=m>0
        pick=side(r)                   # 'home' or 'away' pick
        if pick is None: continue
        if (pick=="home")==homecov: w+=1
        else: l+=1
    return w,l,p
fav_side=lambda r: None if r["sp"]==0 else ("home" if r["sp"]>0 else "away")
dog_side=lambda r: None if r["sp"]==0 else ("away" if r["sp"]>0 else "home")
def roi(w,l): return (w*(100/110)-l)/(w+l)*100 if w+l else None   # -110 flat ROI %
def row(name,rows,side,alpha_n=None):
    w,l,p=ats(rows,side); n=w+l
    if n==0: print(f"  {name:48s} n=0"); return None
    pv=binom_p(w,n); tag=""
    print(f"  {name:48s} n={n:5d}  cover {w/n:6.1%}  vs 52.4% breakeven  ROI {roi(w,l):+5.1f}%  p={pv:.3f}")
    return w,l,pv
print("BASE: all games", len(G), "seasons", min(r['season'] for r in G),"-",max(r['season'] for r in G))
# outright underdog win rate overall / primetime
def dog_won(r):
    if r["sp"]==0 or r["res"]==0: return None
    return (r["res"]>0)!=(r["sp"]>0)
for nm,rows in (("all",G),("primetime",[r for r in G if r["pt"]]),("non-primetime",[r for r in G if not r["pt"]])):
    o=[dog_won(r) for r in rows if dog_won(r) is not None]; print(f"  outright underdog win rate {nm:14s}: {sum(o)/len(o):.1%} (n={len(o)})")
print("\nPRE-REGISTERED HYPOTHESES (ATS vs closing spread; 10 tests -> Bonferroni alpha = 0.005)")
TR=[r for r in G if r["season"]<=2012]; HO=[r for r in G if 2013<=r["season"]<=2025]; Y26=[r for r in G if r["season"]==2026]
tests=[
 ("H1 all underdogs", lambda r:True, dog_side),
 ("H2 primetime underdogs", lambda r:r["pt"], dog_side),
 ("H3 road underdogs", lambda r:dog_side(r)=="away", dog_side),
 ("H4 favorites of 7+", lambda r:abs(r["sp"])>=7, fav_side),
 ("H5 division-game underdogs", lambda r:r["div"], dog_side),
 ("H6 primetime favorites of 7+", lambda r:r["pt"] and abs(r["sp"])>=7, fav_side),
 ("H7 team with 3+ extra rest days", lambda r:r["hrest"] is not None and r["arest"] is not None and abs(r["hrest"]-r["arest"])>=3,
        lambda r: "home" if r["hrest"]>r["arest"] else "away"),
 ("H8 Thursday-night home favorites", lambda r:r["weekday"]=="Thursday" and r["sp"]>0, lambda r:"home"),
 ("H9 primetime home underdogs", lambda r:r["pt"] and r["sp"]<0, lambda r:"home"),
 ("H10 road underdogs of 7+ (home fav 7+)", lambda r:r["sp"]>=7, lambda r:"away"),
]
results={}
for name,flt,side in tests:
    print(name)
    a=row("  all seasons 1999-2026",[r for r in G if flt(r)],side)
    b=row("  TRAIN 1999-2012",[r for r in TR if flt(r)],side)
    c=row("  HOLDOUT 2013-2025",[r for r in HO if flt(r)],side)
    d=row("  2026 YTD",[r for r in Y26 if flt(r)],side)
    results[name]=(a,b,c)

print("\n\nMONEYLINE CALIBRATION (does the favorite win as often as the no-vig line says?)  2006+")
def imp(a): return 100/(a+100) if a>0 else -a/(-a+100)
def devig(h,a):
    ph,pa=imp(h),imp(a); s=ph+pa; return ph/s
M=[r for r in G if r["hml"] is not None and r["aml"] is not None and r["res"]!=0]
for r in M:
    r["ph"]=devig(r["hml"],r["aml"]); r["hw"]=1 if r["res"]>0 else 0
def calib(rows,label):
    bins=[(0,.3),(.3,.4),(.4,.5),(.5,.6),(.6,.7),(.7,.8),(.8,1.01)]
    print(f"  {label}")
    tot_edge=0
    for lo,hi in bins:
        # use each game twice conceptually: restrict to FAVORITE side (p>=.5) and DOG side by bucket of that team's prob
        rows_b=[]
        for r in rows:
            for team_p,won in ((r["ph"],r["hw"]),(1-r["ph"],1-r["hw"])):
                if lo<=team_p<hi: rows_b.append((team_p,won))
        if not rows_b: continue
        n=len(rows_b); exp=sum(p for p,_ in rows_b)/n; act=sum(w for _,w in rows_b)/n
        se=math.sqrt(exp*(1-exp)/n)
        print(f"    implied {lo:.0%}-{min(hi,1):.0%}  n={n:5d}  avg implied {exp:6.1%}  actual {act:6.1%}  gap {100*(act-exp):+5.1f}pp  (z={(act-exp)/se:+.1f})")
calib(M,"ALL 2006-2026")
calib([r for r in M if r["season"]<=2015],"TRAIN 2006-2015")
calib([r for r in M if 2016<=r["season"]<=2025],"HOLDOUT 2016-2025")
calib([r for r in M if r["pt"]],"PRIMETIME only (all seasons)")
calib([r for r in M if r["season"]==2026],"2026 YTD")
