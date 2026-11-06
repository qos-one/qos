"""
Fetch the latest N ticks (BID_ASK) of a Forex pair from IBKR
Saved as Parquet at data/ticks/<SYMBOL>_recent_<timestamp>.parquet

Usage: uv run scripts/fetch_recent_ticks.py --symbol EURUSD --ticks 500
"""
import argparse
from datetime import datetime, timezone
import sys
from pathlib import Path

import pandas as pd
from ib_async import IB, Forex

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qos import paths  # noqa: E402

OUT_DIR = paths.data_dir() / "ticks"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--ticks", type=int, default=500, help="max 1000 per request")
    ap.add_argument("--port", type=int, default=4004)
    args = ap.parse_args()

    ib = IB()
    ib.connect("127.0.0.1", args.port, clientId=56, timeout=60)
    contract = Forex(args.symbol)
    ib.qualifyContracts(contract)
    print("contract:", contract.localSymbol, contract.conId)

    # empty startDateTime + empty endDateTime = the latest N ticks counting back from now
    ticks = ib.reqHistoricalTicks(
        contract,
        startDateTime="",
        endDateTime=datetime.now(timezone.utc),
        numberOfTicks=min(args.ticks, 1000),
        whatToShow="BID_ASK",
        useRth=False,
    )
    ib.disconnect()

    if not ticks:
        print("No data received (market closed, or the account has no historical tick permission)")
        return

    df = pd.DataFrame(
        {
            "ts": [t.time for t in ticks],
            "bid": [t.priceBid for t in ticks],
            "ask": [t.priceAsk for t in ticks],
            "bid_size": [float(t.sizeBid) for t in ticks],
            "ask_size": [float(t.sizeAsk) for t in ticks],
        }
    )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.drop_duplicates().sort_values("ts").reset_index(drop=True)
    df["symbol"] = args.symbol
    df["mid"] = (df["bid"] + df["ask"]) / 2
    df["spread"] = df["ask"] - df["bid"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"{args.symbol}_recent_{stamp}.parquet"
    df.to_parquet(out, index=False, compression="zstd")

    print(f"Got {len(df)} ticks  range {df.ts.min():%Y-%m-%d %H:%M:%S} – {df.ts.max():%H:%M:%S} UTC")
    print(f"average spread {df.spread.mean()*1e4:.2f} pip")
    print("Saved to:", out)
    print(df.tail(5).to_string(index=False))


if __name__ == "__main__":
    main()
