"""First backtest without a broker account.

  python first_backtest.py              # synthetic sine-wave bars: checks that your install works
  python first_backtest.py my_bars.csv  # your own 5m OHLC CSV: time,open,high,low,close[,volume]; time = bar close, UTC

Synthetic bars are made up. Their P&L tells you nothing about the strategy or any market.
A backtest replays past bars: it is not a forecast, and costs are best case (no spread widening, no slippage).
"""
import argparse

from qos.sdk import quick_backtest
from qos.sdk.testing import synthetic_bars
from strategy import $Class

ap = argparse.ArgumentParser(description="First backtest without a broker account")
ap.add_argument("csv", nargs="?", help="your own 5m OHLC CSV; leave out for synthetic install-check bars")
csv = ap.parse_args().csv

if csv:
    import pandas as pd
    from qos.sdk.catalog import frame_to_bars
    df = pd.read_csv(csv, index_col="time", parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[["open", "high", "low", "close"]].astype(float).copy()  # volume left out: works on pandas 2 and 3
    bars, label = frame_to_bars(df, "EUR/USD.IDEALPRO", "5m"), csv
else:
    bars, label = synthetic_bars("EUR/USD.IDEALPRO", 5, n=3000), "SYNTHETIC bars (install check only)"

r = quick_backtest($Class, bars=bars)
if csv:
    pnl = r["pnl_usd"]
    print(f"{label}: {len(bars)} bars, {r['closed_positions']} closed positions, "
          f"P&L {'n/a' if pnl is None else f'{pnl:,.2f} USD'} (backtest, best-case costs; not a forecast)")
else:  # no P&L on made-up data, so nobody reads it as a result
    print(f"Install OK. {label}: {len(bars)} bars, {r['closed_positions']} closed positions. "
          "Run it on your own CSV next: python first_backtest.py my_bars.csv")
print("Next: edit strategy.py, run pytest tests, then qos install .")
