"""
Fetch the latest BID_ASK ticks from IBKR → Parquet → upload to a Databricks Volume → COPY INTO a Delta table

Configure in the QOS .env (qos home show → env_file):
    DATABRICKS_HOST=https://dbc-xxxxxxxx-xxxx.cloud.databricks.com
    DATABRICKS_TOKEN=dapi...
    DATABRICKS_WAREHOUSE_ID=   (optional; the first SQL warehouse is used)

Usage: uv run scripts/ticks_to_databricks.py --symbol EURUSD --ticks 500
"""
import argparse
import io
import sys
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import VolumeType
from databricks.sdk.service.sql import StatementState
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qos import paths  # noqa: E402
from ib_async import IB, Forex

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(paths.env_file())

CATALOG = "workspace"
SCHEMA = "default"
VOLUME = "fx_ticks"
TABLE = f"{CATALOG}.{SCHEMA}.fx_ticks"
LOCAL_DIR = paths.data_dir() / "ticks"


# ---------- 1) Fetch from IBKR ----------
def fetch_ticks(symbol: str, n: int, port: int) -> pd.DataFrame:
    ib = IB()
    ib.connect("127.0.0.1", port, clientId=57, timeout=60)
    try:
        contract = Forex(symbol)
        ib.qualifyContracts(contract)
        ticks = ib.reqHistoricalTicks(
            contract,
            startDateTime="",
            endDateTime=datetime.now(timezone.utc),
            numberOfTicks=min(n, 1000),
            whatToShow="BID_ASK",
            useRth=False,
        )
    finally:
        ib.disconnect()

    if not ticks:
        return pd.DataFrame()
    df = pd.DataFrame(
        {
            "ts": pd.to_datetime([t.time for t in ticks], utc=True),
            "symbol": symbol,
            "bid": [t.priceBid for t in ticks],
            "ask": [t.priceAsk for t in ticks],
            "bid_size": [float(t.sizeBid) for t in ticks],
            "ask_size": [float(t.sizeAsk) for t in ticks],
        }
    )
    df = df.drop_duplicates().sort_values("ts").reset_index(drop=True)
    df["mid"] = (df["bid"] + df["ask"]) / 2
    df["spread"] = df["ask"] - df["bid"]
    df["ingested_at"] = datetime.now(timezone.utc)
    return df


# ---------- 2) Databricks ----------
def get_client() -> WorkspaceClient:
    host, token = os.getenv("DATABRICKS_HOST"), os.getenv("DATABRICKS_TOKEN")
    if not host or not token:
        raise SystemExit("Please set DATABRICKS_HOST and DATABRICKS_TOKEN in .env first")
    return WorkspaceClient(host=host, token=token)


def ensure_volume(w: WorkspaceClient):
    full = f"{CATALOG}.{SCHEMA}.{VOLUME}"
    try:
        w.volumes.read(full)
    except Exception:
        print(f"Created volume {full}")
        w.volumes.create(catalog_name=CATALOG, schema_name=SCHEMA, name=VOLUME, volume_type=VolumeType.MANAGED)


def pick_warehouse(w: WorkspaceClient) -> str:
    wid = os.getenv("DATABRICKS_WAREHOUSE_ID")
    if wid:
        return wid
    whs = list(w.warehouses.list())
    if not whs:
        raise SystemExit("No SQL warehouse found in the workspace")
    print("Using warehouse:", whs[0].name, whs[0].id)
    return whs[0].id


def run_sql(w: WorkspaceClient, warehouse_id: str, sql: str, wait_s: int = 300):
    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=sql, wait_timeout="50s"
    )
    t0 = time.time()
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.time() - t0 > wait_s:
            raise TimeoutError("SQL took too long")
        time.sleep(3)
        resp = w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"SQL failed: {resp.status.error}")
    return resp


def upload_and_load(w: WorkspaceClient, df: pd.DataFrame, symbol: str) -> str:
    ensure_volume(w)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{symbol}_{stamp}.parquet"
    remote_dir = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}/raw/{symbol}"
    remote_path = f"{remote_dir}/{fname}"

    # keep a local copy too
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(LOCAL_DIR / fname, index=False, compression="zstd")

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, compression="zstd")
    buf.seek(0)
    w.files.create_directory(remote_dir)
    w.files.upload(remote_path, buf, overwrite=True)
    print("Uploaded:", remote_path)

    wid = pick_warehouse(w)
    run_sql(
        w, wid,
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            ts TIMESTAMP, symbol STRING, bid DOUBLE, ask DOUBLE,
            bid_size DOUBLE, ask_size DOUBLE, mid DOUBLE, spread DOUBLE,
            ingested_at TIMESTAMP
        ) USING DELTA
        """,
    )
    # COPY INTO remembers loaded files → re-runs add no duplicate rows
    run_sql(
        w, wid,
        f"""
        COPY INTO {TABLE}
        FROM '{remote_dir}'
        FILEFORMAT = PARQUET
        COPY_OPTIONS ('mergeSchema' = 'true')
        """,
    )
    res = run_sql(
        w, wid,
        f"SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM {TABLE} WHERE symbol = '{symbol}'",
    )
    n, first_ts, last_ts = res.result.data_array[0]
    print(f"table {TABLE} [{symbol}]: {n} rows  {first_ts} → {last_ts}")
    return remote_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--ticks", type=int, default=500)
    ap.add_argument("--port", type=int, default=4004)
    args = ap.parse_args()

    df = fetch_ticks(args.symbol, args.ticks, args.port)
    if df.empty:
        print("No ticks received from IBKR")
        return
    print(f"Got {len(df)} ticks  {df.ts.min():%Y-%m-%d %H:%M:%S} – {df.ts.max():%H:%M:%S} UTC  "
          f"average spread {df.spread.mean()*1e4:.2f} pip")

    w = get_client()
    upload_and_load(w, df, args.symbol)


if __name__ == "__main__":
    main()
