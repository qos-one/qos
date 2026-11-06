"""
Fetch historical ticks (BID_ASK) of a Forex pair from IBKR for the last N days
and save them as Parquet in data/ticks/<SYMBOL>_<YYYY-MM-DD>.parquet

Usage: uv run scripts/fetch_ticks.py --symbol EURUSD --days 1
"""
import argparse
import time
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pandas as pd
from ib_async import IB, Forex

TICKS_PER_REQ = 1000          # IBKR limit per request
PAUSE_SEC = 0.6               # delay to stay under the pacing limit (~60 req / 10 minutes)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qos import paths  # noqa: E402

OUT_DIR = paths.data_dir() / "ticks"


def fetch_day(ib: IB, contract, day_start: datetime, day_end: datetime) -> pd.DataFrame:
    rows = []
    cursor = day_start
    n_req = 0
    last_ts = None
    while cursor < day_end:
        for attempt in range(5):
            try:
                ticks = ib.reqHistoricalTicks(
                    contract,
                    startDateTime=cursor,
                    endDateTime="",
                    numberOfTicks=TICKS_PER_REQ,
                    whatToShow="BID_ASK",
                    useRth=False,
                )
                break
            except Exception as e:  # pacing / timeout → wait and retry
                wait = 10 * (attempt + 1)
                print(f"  error: {e} → retrying in {wait}s")
                time.sleep(wait)
        else:
            raise RuntimeError("Failed to fetch data after 5 attempts")

        n_req += 1
        if not ticks:
            print("  no more ticks, stopped at", cursor)
            break

        for t in ticks:
            if t.time > day_end:
                continue
            rows.append(
                {
                    "ts": t.time,
                    "bid": t.priceBid,
                    "ask": t.priceAsk,
                    "bid_size": float(t.sizeBid),
                    "ask_size": float(t.sizeAsk),
                }
            )

        newest = ticks[-1].time
        print(f"  req#{n_req:4d}  {len(ticks):4d} ticks  up to {newest:%Y-%m-%d %H:%M:%S}  total {len(rows):,}")
        if newest == last_ts or newest >= day_end:
            break
        last_ts = newest
        # move the cursor 1 second past the last tick (IBKR has 1-second resolution)
        cursor = newest + timedelta(seconds=1)
        time.sleep(PAUSE_SEC)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.drop_duplicates().sort_values("ts").reset_index(drop=True)
    df["mid"] = (df["bid"] + df["ask"]) / 2
    df["spread"] = df["ask"] - df["bid"]
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--days", type=int, default=1, help="number of days back (counting from today, UTC)")
    ap.add_argument("--port", type=int, default=4004)
    args = ap.parse_args()

    ib = IB()
    ib.connect("127.0.0.1", args.port, clientId=55, timeout=60)
    contract = Forex(args.symbol)
    ib.qualifyContracts(contract)
    print("contract:", contract.localSymbol, contract.conId)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    for d in range(args.days, 0, -1):
        day_start = today - timedelta(days=d)
        day_end = day_start + timedelta(days=1)
        out = OUT_DIR / f"{args.symbol}_{day_start:%Y-%m-%d}.parquet"
        if out.exists():
            print(f"skipping {day_start:%Y-%m-%d} (file already exists)")
            continue
        print(f"\n=== {args.symbol} {day_start:%Y-%m-%d} UTC ===")
        t0 = time.time()
        df = fetch_day(ib, contract, day_start, day_end)
        if df.empty:
            print("no data (market holiday?)")
            continue
        df.to_parquet(out, index=False, compression="zstd")
        print(
            f"saved {out.name}: {len(df):,} ticks  "
            f"{df.ts.min():%H:%M:%S}–{df.ts.max():%H:%M:%S}  "
            f"average spread {df.spread.mean()*1e4:.2f} pip  took {time.time()-t0:.0f}s"
        )

    ib.disconnect()


if __name__ == "__main__":
    main()
