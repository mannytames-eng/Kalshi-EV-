Kalshi EV Scanner

An automated system that finds mispriced MLB, NFL, and WNBA markets on Kalshi by comparing them against de-vigged Pinnacle lines, sizes bets with fractional Kelly, and tracks every bet to the close and to settlement.

Live dashboard: https://evscanner-production.up.railway.app

Status: paper trading. The bankroll is a simulated $1,000. No real capital is deployed. Every result below is a paper result, and I would rather say that first than bury it.

The thesis

Pinnacle takes enormous professional volume on thin margins, which keeps its odds close to true probability. For practical purposes it is the sharpest public price.

Kalshi is an exchange. Prices only move when someone trades. On low-volume markets like player props, Kalshi's price lags behind sharp movement in the underlying market.

The gap between the sharp price and Kalshi catching up is where the edge lives. This system automates finding it.

How it works

1. Ingest. Pull Kalshi prices and Pinnacle odds (via The Odds API) for MLB totals and player props, plus early coverage of NFL and WNBA markets.

2. De-vig. Sportsbook odds include the book's margin. Strip it out to get a fair implied probability. That fair price is the benchmark, because comparing against raw odds would measure against a price nobody can actually get.

3. Compare and flag. Where Kalshi's price is below fair value by more than the threshold (2.5% for props, 3% for game totals), flag it.

4. Size. Quarter-Kelly, capped at 3% of bankroll per bet, with a 15% daily exposure cap.

5. Alert. Qualified edges push to Discord in real time.

6. Track. Open bets update every 2 minutes. Kalshi's price is captured every 2 minutes until game start, so I can see whether the market moved toward my entry. Every bet is graded at settlement.

7. Shadow-test new markets. A new market type first runs on a parallel hypothetical ledger: $0 stake, sized exactly like a live bet, and excluded from every headline number. It only counts once it has enough data to judge.

Results

V2.0, since June 8, 2026. As of October 9, 2026: 364 settled bets, 3 open.

Metric	Value	What it means
Flat-unit profit	+25.40u (+0.077u per bet)	Profit if every bet were $1. Covers all 364 settled bets, including the Total Bases market I terminated.
Avg CLV	+0.78¢	After I flagged a bet, Kalshi's price moved toward my entry by game start. It moved my way on 51% of bets.
CLV significance	t = 3.57	The move toward my entries is unlikely to be luck.
Win rate vs implied	50.7% actual vs 44.3% implied (136-132)	Excludes Total Bases (268 bets). Including it, the record is 173-191 (47.5%).
Simulated Kelly return	+43.61% of the paper bankroll	Secondary. Sizing-model diagnostic, not an edge metric. Noisy at this sample.
How to read these numbers

CLV is the main evidence, because it measures whether I bought before the market caught up, and it converges faster than profit does.

Flat-unit profit is positive but not yet statistically proven (t = 1.52, and the 95% range includes zero). I treat it as encouraging, not settled.

The Kelly return is the biggest number and the least reliable one. Most of it comes from a single market (strikeouts), and sizing amplifies variance. I do not lead with it.

CLV alone is also not enough. Total Bases showed positive CLV and still lost money. That is why I look at CLV, units, and win rate against implied together, and why none of them is treated as proof by itself.

What's working and what isn't

Per-market results from the dashboard:

Market	Record	Win rate	Expected	Kelly P&L (% bank)	Units
Strikeouts	87-77	53.0%	44.8%	+43.33%	+30.47u
MLB Total	25-21	54.3%	43.8%	+4.49%	+10.33u
Pitcher Outs (whole-inning)	11-14	44.0%	46.6%	+1.00%	-1.38u
Total Bases (terminated)	21-41	33.9%	42.7%	-6.71%	-13.04u

Strikeouts carry the result. It is the largest market and the biggest source of profit. One market doing most of the work is a risk, and the other markets are too small to confirm it.

Total Bases lost money, so I terminated it. It beat the closing line and still lost, which is a useful reminder that a statistically real CLV does not guarantee a profitable market.

Everything else is too early to judge. Pitcher Outs on other lines, WNBA, and NFL props each have fewer than 12 bets. I track them, but I draw no conclusions from them.

Stack
Python: scanner, de-vig math, Kelly sizing, settlement grading
Kalshi API: market prices
The Odds API: Pinnacle lines
Railway: continuous deployment, scheduled polling
Discord webhooks: real-time alerts
Chart.js: dashboard charts
kalshi_ev_scanner.py    # core: ingest, de-vig, edge detection, sizing, alerting
kalshi_ev_ui.py         # dashboard: portfolio, CLV tracking, per-market breakdowns
railway.toml            # deploy config
requirements.txt
Roadmap
 Core scanner: de-vig, edge detection, Quarter-Kelly sizing
 Railway deployment with continuous uptime
 Discord alerting
 Price capture every 2 minutes through game start
 Web dashboard with live portfolio tracker and per-market breakdowns
 Shadow ledger for testing new markets at $0 stake
 Terminate markets that lose (Total Bases)
 Early NFL and WNBA coverage (small samples so far)
 Grow the sample to 500-1,000 settled bets before drawing conclusions by market
 SMS alerts (Twilio)
 Autonomous execution
 Deploy real capital
A note on paper trading

The bankroll is simulated. What the paper portfolio shows: the pricing signal looks real, the sizing is disciplined, and the system tells me when a market stops working. What it does not show: execution under real fills, slippage, and how I behave with real money on the line. Those are the next problem, and I would rather take them on with a signal I have tested than one I have not.

Built by Emanuel Tames-Kaimowitz · manny.tames@gmail.com
