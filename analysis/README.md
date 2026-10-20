# Kalshi calibration / backtest scripts (2026-10-20 study)

Kalshi's API is geoblocked outside the US, so price collection runs on Railway:

    B64=$(base64 < analysis/collect_kalshi_prices.py | tr -d '\n')
    railway ssh -- "echo $B64 | base64 -d > /tmp/c.py && cd /tmp && (nohup python3 c.py KXNFLGAME /tmp/nfl.json 5000 > /tmp/nfl.log 2>&1 &)"
    railway ssh -- "cat /tmp/nfl.json" > nfl.json        # when /tmp/nfl.log says DONE
    # last arg `one` fetches one market per event (enough for MLB/CFB where only the winner side is needed)

Ground truth / closing lines: https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv

- nfl_history_backtest.py  10 pre-registered ATS hypotheses + moneyline calibration, 1999-2026, train/holdout split
- mlb_calibration.py, cfb_calibration.py  Kalshi price at kickoff vs result (cfb needs ESPN schedule json)
- forward_test.py  pre-registered forward test; refuses to judge before the stated sample sizes
