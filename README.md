Kalshi EV Scanner

An automated system that finds mispriced sports markets on Kalshi by comparing them against de-vigged Pinnacle lines, sizes positions with fractional Kelly, and grades every bet against the closing line.

Live dashboard: https://evscanner-production.up.railway.app

Status: paper trading. The edge is real and measured, but no live capital is deployed yet. More on that below, because I think how you measure an edge matters more than the edge itself.


The thesis

Pinnacle takes enormous professional volume and runs on thin margins, which keeps their odds pinned close to true probability. They are, for practical purposes, the sharpest public price.

Kalshi is an exchange. Prices only move when someone trades. On low-volume MLB player props, that repricing lags behind sharp movement in the underlying market.

The gap between the sharp shift and the exchange catching up is where the edge lives. This system automates finding it.


How it works

1. Ingest. Pull Kalshi market prices and Pinnacle odds (via The Odds API) for MLB game totals and player props (strikeouts, total bases).

2. De-vig. Raw sportsbook odds include the book's margin. Strip it out to recover a fair implied probability. This is the benchmark — comparing against raw odds would mean measuring against a price nobody can actually get.

3. Compare and flag. Where Kalshi's implied probability deviates from Pinnacle's fair value by more than the edge threshold (2.5% for props, 3% for game totals), flag it.

4. Size. Quarter-Kelly (0.25 fractional), capped at 3% of bankroll per position, with a 15% daily exposure cap. Market types whose CLV hasn't stabilized run at 0.5x until they do.

5. Alert. Qualified edges push to Discord in real time.

6. Grade. Every position is tracked to settlement and scored against the Pinnacle closing line, captured every 2 minutes until game start.

The system runs unattended on Railway.


Results

V2.0, since 2026-06-08. 133 settled positions.

MetricValueWhat it meansAvg CLV vs Pinnacle close+4.8ppI'm consistently buying below where the sharp market settles. This is the number that matters.Statistical significance of CLVt = 5.16, p < 0.001The closing-line move toward my entries is not noise.Kalshi move toward entry+1.20¢ avgThe exchange repriced in my direction after I bought, 57% of the time vs 22% against.Win rate vs implied45.2% actual vs 44.1% implied+1.1pp over the market's own pricing. Real, but thin.Flat-stake units+6.04u (+0.048u/bet)Positive, small per-bet edge.Kelly P&L+1.33% of bankrollNoisy at this sample. Don't judge the system by this.

Why CLV and not P&L

At 133 bets, return is mostly variance. A system with no edge can print a positive ROI over a sample this size, and a genuinely good one can print a negative one. Closing line value is the standard because it measures whether you bought at a better price than the sharpest available consensus — and it converges on truth far faster than P&L does.

So the honest claim here is not "this made money." It's: I bought below the sharp close, consistently, and the effect is statistically significant. That's a defensible statement about edge. The P&L is a footnote until the sample is 5-10x bigger.


Where it's failing

The whole point of grading yourself against the close is to find out what isn't working. Currently:

Market typeRecordWin rateKelly P&LUnitsMLB Total5-1(n=6, insufficient)+6.02%+5.17uStrikeouts (K)37-4346.2%+4.04%+5.15uTotal Bases15-2537.5%-8.74%-4.29u

Total Bases is a clear loser and is dragging the aggregate down. Strikeouts is the most stable market by sample size and is modestly positive. MLB Totals looks best but n=6 is meaningless.

Edge-bucket analysis is also instructive:

Edge bucketNActual win rateImpliedDelta2-4%11846.6%44.4%+2.2pp4-6%425%36%-11pp (n<10)6-8%30%45.3%-45.3pp (n<10)8%+1100%32%+68pp (n<10)

118 of the 133 bets live in the 2-4% bucket, and that bucket is beating its implied probability by 2.2pp. The larger "edges" have samples too small to mean anything — a -45pp reading on 3 bets is noise, not signal. The real, trustworthy edge is entirely in the 2-4% bucket. Anything else the dashboard shows in those tiers should be ignored until the sample grows.

The open question I'm collecting data on: is Total Bases a structurally bad market for this approach (my model misprices the distribution), or is 40 bets just not enough to tell? I'm not patching it until I have the sample to justify the change. Shipping a fix to a pattern that might be noise is how you overfit.


Stack


Python — scanner, de-vig math, Kelly sizing, settlement grading
Kalshi API — market prices, order book
The Odds API — Pinnacle lines
Railway — continuous deployment, scheduled polling
Discord webhooks — real-time alerts
Chart.js — dashboard visualization


kalshi_ev_scanner.py    # core: ingest, de-vig, edge detection, sizing, alerting
kalshi_ev_ui.py         # dashboard: portfolio, CLV tracking, performance breakdowns
railway.toml            # deploy config
requirements.txt


Roadmap


 Core scanner: de-vig, edge detection, Quarter-Kelly sizing
 Railway deployment with continuous uptime
 Discord alerting
 CLV capture every 2 min through game start
 Web dashboard with performance and edge-bucket breakdowns
 Grow sample to 500-1,000 settled positions before drawing conclusions on market-type performance
 SMS alerts (Twilio)
 WNBA market expansion (caveat: CLV validation is weak in thin markets, where price movement requires actual trades)
 Autonomous execution
 Deploy real capital



A note on the paper-trading status

The bankroll is simulated. I lead with that rather than bury it, because the alternative — implying live results I don't have — is exactly the kind of thing CLV tracking exists to prevent.

What the paper portfolio does establish: the pricing signal is real, it's statistically significant, and the position sizing is disciplined rather than reckless. What it doesn't establish: execution under real fills, slippage, and the psychology of live money. Those are the next problem, and I'd rather solve them with a validated signal than an unvalidated one.


Built by Emanuel Tames-Kaimowitz · manny.tames@gmail.com
