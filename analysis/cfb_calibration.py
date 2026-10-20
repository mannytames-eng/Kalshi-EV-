import json, re, math, collections
from datetime import datetime, timezone
MON={m:i+1 for i,m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
S=json.load(open("espn/cfb_sched.json"))
def nz(s): return re.sub(r"[^a-z0-9]","",(s or "").lower())
def find_game(date,sub):
    s=nz(sub)
    if not s: return None
    games=S.get(date,[])
    exact=[];loose=[]
    for g in games:
        for loc,disp,ab in g["teams"]:
            L,Dn,A=nz(loc),nz(disp),nz(ab)
            if s==L or s==Dn: exact.append(g); break
            if L.startswith(s) or s.startswith(L) and len(L)>=4 or Dn.startswith(s): loose.append(g); break
    c=exact if exact else loose
    return c[0] if len(c)==1 else None
D=json.load(open("cfb.json"))
rows=[];nomatch=0;nocand=0
for m in D:
    if not m["candles"]: nocand+=1; continue
    mm=re.match(r"KXNCAAFGAME-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$",m["event"])
    if not mm: continue
    yy,mon,dd,_=mm.groups(); date=f"20{yy}-{MON[mon]:02d}-{dd}"
    g=find_game(date,m["sub"])
    if not g: nomatch+=1; continue
    ko=datetime.fromisoformat(g["t"].replace("Z","+00:00")).timestamp()
    best=None
    for c in m["candles"]:
        if c[0]<=ko: best=c
    if not best: continue
    ts,bid,ask,px,vol=best
    if bid is None or ask is None or ask<=0 or ask-bid>0.10 or ko-ts>3*3600: continue
    rows.append(dict(p=(bid+ask)/2,bid=bid,ask=ask,win=1 if m["result"]=="yes" else 0,yr=int(yy),vol=m["vol"] or 0))
print(f"Kalshi CFB events collected: {len(D)}  | matched to an ESPN kickoff with a clean pregame price: {len(rows)}  (no-match {nomatch}, no-candles {nocand})")
print("by year:",dict(collections.Counter(r['yr'] for r in rows)))
def fee(a): return 0.07*a*(1-a)
pts=[]
for r in rows:
    pts.append((r["p"],r["win"],r["ask"],r["yr"]))
    pts.append((1-r["p"],1-r["win"],1-r["bid"],r["yr"]))
def table(sel,label):
    print(f"\n{label}")
    for nm,lo,hi in (("heavy dog <15%",0,.15),("dog 15-30%",.15,.30),("dog 30-45%",.30,.45),("toss-up 45-55%",.45,.55),("fav 55-70%",.55,.70),("fav 70-85%",.70,.85),("heavy fav >85%",.85,1.01)):
        x=[(p,w,a) for p,w,a,y in sel if lo<=p<hi]
        if len(x)<40: continue
        n=len(x); e=sum(p for p,_,_ in x)/n; ac=sum(w for _,w,_ in x)/n; se=math.sqrt(e*(1-e)/(n/2))
        roi=sum(w-a-fee(a) for _,w,a in x)/sum(a+fee(a) for _,_,a in x)*100
        print(f"  {nm:15s} games≈{n//2:4d}  implied {e:5.1%} actual {ac:5.1%}  gap {100*(ac-e):+5.2f}pp (z={(ac-e)/se:+.1f}) | buy-all ROI after fee {roi:+5.1f}%")
table(pts,"ALL college football (Kalshi price at kickoff vs result)")
table([x for x in pts if x[3]==25],"2025 season")
table([x for x in pts if x[3]==26],"2026 season")
for nm,sel in (("favorites (>=55%)",[(p,w,a) for p,w,a,y in pts if p>=.55]),("underdogs (<=45%)",[(p,w,a) for p,w,a,y in pts if p<=.45])):
    n=len(sel); e=sum(p for p,_,_ in sel)/n; ac=sum(w for _,w,_ in sel)/n; se=math.sqrt(e*(1-e)/n)
    roi=sum(w-a-fee(a) for _,w,a in sel)/sum(a+fee(a) for _,_,a in sel)*100
    print(f"\nALL {nm}: n={n}  implied {e:.2%} actual {ac:.2%}  gap {100*(ac-e):+.2f}pp (z={(ac-e)/se:+.1f})  buy-all ROI after fee {roi:+.1f}%")
