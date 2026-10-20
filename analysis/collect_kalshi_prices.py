import json, urllib.request, time, sys
from datetime import datetime, timezone, timedelta
B="https://api.elections.kalshi.com/trade-api/v2"
def get(u, tries=4):
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=40).read())
        except Exception as e:
            if "404" in str(e): raise
            time.sleep(1.5*(i+1))
    raise RuntimeError("failed "+u)
def page(path, key, cap=60):
    out=[];cur=None
    for _ in range(cap):
        d=get(B+path+(f"&cursor={cur}" if cur else "")); out+=d.get(key,[]); cur=d.get("cursor")
        if not cur or not d.get(key): break
    return out
def num(x):
    try: return float(x)
    except: return None
def norm(c):
    g=lambda o,*ks:next((num(o[k]) for k in ks if isinstance(o,dict) and o.get(k) not in (None,"")),None)
    return [c["end_period_ts"], g(c.get("yes_bid",{}),"close_dollars","close"), g(c.get("yes_ask",{}),"close_dollars","close"),
            g(c.get("price",{}),"close_dollars","close"), num(c.get("volume_fp") or c.get("volume"))]
series=sys.argv[1]; outf=sys.argv[2]; limit=int(sys.argv[3]) if len(sys.argv)>3 else 10**9
live=page(f"/markets?series_ticker={series}&status=settled&limit=1000","markets")
hist=page(f"/historical/markets?series_ticker={series}&limit=1000","markets")
seen=set(); mk=[]
for src,lst in (("live",live),("hist",hist)):
    for m in lst:
        if m["ticker"] in seen or m.get("result") not in ("yes","no"): continue
        seen.add(m["ticker"]); m["_src"]=src; mk.append(m)
one=len(sys.argv)>4 and sys.argv[4]=="one"
if one:
    byev={}
    for m in mk: byev.setdefault(m["event_ticker"],m)
    mk=list(byev.values())
print(series,"markets:",len(mk),"live",len(live),"hist",len(hist),flush=True)
res=[]; t0=time.time()
for i,m in enumerate(mk[:limit]):
    try:
        close=datetime.fromisoformat(m["close_time"].replace("Z","+00:00"))
        st=int((close-timedelta(hours=60)).timestamp()); en=int(close.timestamp())
        if m["_src"]=="live":
            url=f"/series/{series}/markets/{m['ticker']}/candlesticks?start_ts={st}&end_ts={en}&period_interval=60"
        else:
            url=f"/historical/markets/{m['ticker']}/candlesticks?start_ts={st}&end_ts={en}&period_interval=60"
        cs=[norm(c) for c in get(B+url).get("candlesticks",[])]
    except Exception as e:
        cs=None
    res.append({"ticker":m["ticker"],"event":m["event_ticker"],"result":m["result"],"sub":m.get("yes_sub_title"),
                "open":m.get("open_time"),"close":m.get("close_time"),"vol":num(m.get("volume_fp") or m.get("volume")),"candles":cs})
    if i%50==0: print(i,len(mk),f"{time.time()-t0:.0f}s",flush=True)
    time.sleep(0.12)
json.dump(res,open(outf,"w")); print("DONE",len(res),flush=True)
