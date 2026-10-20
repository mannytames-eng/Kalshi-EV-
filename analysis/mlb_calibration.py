import json, re, math, collections
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
ET=ZoneInfo("America/New_York")
MON={m:i+1 for i,m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
D=json.load(open("mlb.json"))
rows=[]
for m in D:
    if not m["candles"]: continue
    mm=re.match(r"KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})(\d{4})([A-Z]+)$",m["event"])
    if not mm: continue
    yy,mon,dd,hhmm,_=mm.groups()
    ko=datetime(2000+int(yy),MON[mon],int(dd),int(hhmm[:2]),int(hhmm[2:]),tzinfo=ET).astimezone(timezone.utc).timestamp()
    best=None
    for c in m["candles"]:
        if c[0]<=ko: best=c
    if not best: continue
    ts,bid,ask,px,vol=best
    if bid is None or ask is None or ask<=0 or ask-bid>0.10 or ko-ts>3*3600: continue
    rows.append(dict(p=(bid+ask)/2,bid=bid,ask=ask,win=1 if m["result"]=="yes" else 0,yr=int(yy),ev=m["event"]))
print("MLB games with a usable pregame price:",len(rows),"| by year:",dict(collections.Counter(r['yr'] for r in rows)))
def fee(a): return 0.07*a*(1-a)
# fold each game into both teams so buckets are symmetric
pts=[]
for r in rows:
    pts.append((r["p"],r["win"],r["ask"],r["yr"]))                      # this team: buy YES at ask
    pts.append((1-r["p"],1-r["win"],1-r["bid"],r["yr"]))                # other team: buy its YES at 1 - bid
def table(sel,label):
    print(f"\n{label}")
    bins=[("heavy dog <30%",0,.30),("dog 30-45%",.30,.45),("toss-up 45-55%",.45,.55),("fav 55-70%",.55,.70),("heavy fav >70%",.70,1.01)]
    for nm,lo,hi in bins:
        x=[(p,w,a) for p,w,a,y in sel if lo<=p<hi]
        if len(x)<30: continue
        n=len(x); e=sum(p for p,_,_ in x)/n; ac=sum(w for _,w,_ in x)/n
        # games counted twice (both sides) -> effective n = n/2 for the SE
        se=math.sqrt(e*(1-e)/(n/2))
        roi=sum(w-a-fee(a) for _,w,a in x)/sum(a+fee(a) for _,_,a in x)*100
        print(f"  {nm:15s} games≈{n//2:5d}  implied {e:5.1%}  actual {ac:5.1%}  gap {100*(ac-e):+5.2f}pp (z={(ac-e)/se:+.1f})  | buy-all ROI after fee {roi:+5.1f}%")
table(pts,"ALL MLB games (Kalshi price at first pitch vs result)")
table([x for x in pts if x[3]==25],"2025")
table([x for x in pts if x[3]==26],"2026")
# overall favorite vs dog summary
fav=[(p,w,a) for p,w,a,y in pts if p>=.5]; dog=[(p,w,a) for p,w,a,y in pts if p<.5]
for nm,x in (("favorites",fav),("underdogs",dog)):
    n=len(x); e=sum(p for p,_,_ in x)/n; ac=sum(w for _,w,_ in x)/n; se=math.sqrt(e*(1-e)/n)
    roi=sum(w-a-fee(a) for _,w,a in x)/sum(a+fee(a) for _,_,a in x)*100
    print(f"\nALL {nm}: n={n}  implied {e:.2%} actual {ac:.2%}  gap {100*(ac-e):+.2f}pp (z={(ac-e)/se:+.1f})  buy-all ROI after fee {roi:+.1f}%")
