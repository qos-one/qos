"""
Central data catalog (NautilusTrader ParquetDataCatalog format) — the single data source for backtests, research and model training

- Fetch 1-minute FX bars (MIDPOINT) from IBKR with ib_async and build 5-minute bars from them locally (fewer requests)
- Bar time: ts_event = bar close time (Nautilus convention) — IBKR gives the bar open time, so the bar length is added
- update mode: continue from the latest time in the catalog (2-hour overlap, then dedupe); only write closed bars newer than existing data
- Instrument: CurrencyPair on venue IDEALPRO with the same id as the live adapter (e.g. EUR/USD.IDEALPRO)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = paths.catalog_dir()
MANIFEST = CATALOG_DIR / "_manifest.json"
CATALOG_CLIENT_ID = 58                     # reserved in nautilus_bridge.RESERVED_IDS
DEFAULT_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD")
TIMEFRAMES = {"1m": ("1-MINUTE", "1min"), "5m": ("5-MINUTE", "5min")}


# ---------- instruments / bar types ----------
def instrument_id_str(symbol: str) -> str:
    return f"{symbol[:3]}/{symbol[3:]}.IDEALPRO"


def bar_type_str(symbol: str, tf: str) -> str:
    return f"{instrument_id_str(symbol)}-{TIMEFRAMES[tf][0]}-MID-EXTERNAL"


from qos.sdk.catalog import make_fx_instrument, resample_closed  # noqa: E402,F401  (moved to the SDK in step 23 — same code)


# ---------- pure helpers (testable without connecting to IBKR) ----------
def ib_bars_to_frame(rows: list[dict], bar_minutes: int = 1) -> pd.DataFrame:
    """rows: [{t (bar open time, tz-aware), o,h,l,c}] → DataFrame index = bar close time UTC, sorted, unique, abnormal prices removed"""
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame(rows).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})
    df["timestamp"] = pd.to_datetime(df["t"], utc=True) + pd.Timedelta(minutes=bar_minutes)
    df = df.set_index("timestamp")[["open", "high", "low", "close"]].astype(float)
    df = df[(df > 0).all(axis=1) & (df["high"] >= df["low"])]
    return df[~df.index.duplicated(keep="last")].sort_index()


def only_new_closed(df: pd.DataFrame, last: pd.Timestamp | None, now: pd.Timestamp) -> pd.DataFrame:
    """Keep only closed bars (close time <= now) that are newer than existing data"""
    if df.empty:
        return df
    m = df.index <= now
    if last is not None:
        m &= df.index > last
    return df[m]


def _weekend_overlap_min(a: pd.Timestamp, b: pd.Timestamp) -> float:
    """Minutes of [a, b] inside the weekly FX closure, taken as Friday 21:00 → Sunday 22:00 UTC (covers both EDT and EST)"""
    total = 0.0
    day = (a - pd.Timedelta(days=(a.weekday() - 4) % 7)).normalize()          # the Friday on/before a
    while day <= b:
        lo, hi = day + pd.Timedelta(hours=21), day + pd.Timedelta(days=2, hours=22)
        total += max(0.0, (min(b, hi) - max(a, lo)).total_seconds() / 60)
        day += pd.Timedelta(days=7)
    return total


def gap_report(df1m: pd.DataFrame, max_gap_min: int = 15) -> list[dict]:
    """Gaps on FX trading days longer than max_gap_min minutes (weekend closure time is not counted — an outage that starts
    on a Friday during market hours is still reported)"""
    if len(df1m) < 2:
        return []
    idx = df1m.index
    gaps = []
    for a, b in zip(idx[:-1], idx[1:]):
        mins = (b - a).total_seconds() / 60
        open_mins = mins - _weekend_overlap_min(a, b)
        daily_break = a.hour in (21, 22) and mins <= 20                # daily break at 17:00 New York
        holiday = (a.month, a.day) in ((12, 24), (12, 31)) and mins <= 28 * 60   # Christmas / New Year
        if open_mins > max_gap_min and not (daily_break or holiday):
            gaps.append({"from": a.isoformat(), "to": b.isoformat(), "minutes": int(mins), "open_minutes": int(open_mins)})
    return gaps


# ---------- catalog ----------
def open_catalog(path: Path = CATALOG_DIR):
    from nautilus_trader.persistence.catalog import ParquetDataCatalog
    path.mkdir(parents=True, exist_ok=True)
    return ParquetDataCatalog(str(path))


def last_timestamp(catalog, bar_type: str) -> pd.Timestamp | None:
    from nautilus_trader.model.data import Bar
    try:
        ts = catalog.query_last_timestamp(Bar, identifier=bar_type)
    except Exception:
        return None
    return pd.Timestamp(ts).tz_convert("UTC") if ts is not None else None


def write_bars(catalog, symbol: str, tf: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    from nautilus_trader.model.data import BarType
    from nautilus_trader.persistence.wranglers import BarDataWrangler
    w = BarDataWrangler(BarType.from_str(bar_type_str(symbol, tf)), make_fx_instrument(symbol))
    df = df.copy()
    df["volume"] = 1_000_000.0                 # MIDPOINT has no real volume → constant, same as the wrangler default
    bars = w.process(df)
    catalog.write_data(bars)
    return len(bars)


# ---------- IBKR fetch ----------
async def fetch_1m(ib, symbol: str, start: datetime, end: datetime, log=print) -> list[dict]:
    """Fetch 1-minute bars backwards from end to start in chunks

    Try 1 W first — if the result is empty (IBKR timeout returns an empty list without an error), drop to 2 D → 1 D and retry the same chunk
    Skip the chunk only if 1 D is still empty (real holiday/weekend)
    """
    from ib_async import Forex
    c = Forex(symbol)
    await ib.qualifyContractsAsync(c)
    out: dict[str, dict] = {}
    cur_end = end
    durations = [("1 W", 7), ("2 D", 2), ("1 D", 1)]
    di = 0
    while cur_end > start:
        dur, days = durations[di]
        try:
            data = await ib.reqHistoricalDataAsync(c, endDateTime=cur_end, durationStr=dur, barSizeSetting="1 min",
                                                   whatToShow="MIDPOINT", useRTH=False, formatDate=2, timeout=240)
        except Exception as e:
            data = None
            log(f"  {symbol} {dur} until {cur_end:%Y-%m-%d %H:%M}: {e}")
        if not data:
            if di < len(durations) - 1:
                di += 1
                log(f"  {symbol} {dur} until {cur_end:%Y-%m-%d %H:%M}: empty → retry {durations[di][0]}")
                await asyncio.sleep(2.0)
                continue
            log(f"  {symbol} no data for 1 D until {cur_end:%Y-%m-%d %H:%M} (holiday/weekend)")
            cur_end = cur_end - timedelta(days=1)
        else:
            for b in data:
                t = b.date if isinstance(b.date, datetime) else datetime.combine(b.date, datetime.min.time(), tzinfo=timezone.utc)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                out[t.isoformat()] = {"t": t, "o": b.open, "h": b.high, "l": b.low, "c": b.close}
            first = min(b.date for b in data)
            first = first if first.tzinfo else first.replace(tzinfo=timezone.utc)
            cur_end = first if first < cur_end else cur_end - timedelta(days=days)
            log(f"  {symbol} {dur} ← {first:%Y-%m-%d %H:%M}  ({len(out):,} bars)")
        di = 0 if data else di
        await asyncio.sleep(1.0)                # pause to respect IBKR pacing
    return [r for r in out.values() if r["t"] >= start - timedelta(minutes=1)]


def read_bars(catalog, symbol: str, tf: str = "1m") -> pd.DataFrame:
    """Read bars from the catalog back into a DataFrame (index = bar close time UTC)"""
    bars = catalog.bars(bar_types=[bar_type_str(symbol, tf)])
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame({"open": [b.open.as_double() for b in bars], "high": [b.high.as_double() for b in bars],
                       "low": [b.low.as_double() for b in bars], "close": [b.close.as_double() for b in bars]},
                      index=pd.to_datetime([b.ts_event for b in bars], unit="ns", utc=True))
    df.index.name = "timestamp"
    return df[~df.index.duplicated(keep="last")].sort_index()


def rewrite_series(catalog, catalog_dir: Path, symbol: str, df1m: pd.DataFrame) -> tuple[int, int]:
    """Delete this pair's existing files (1m/5m) and rewrite the whole set — used after repairing gaps"""
    import shutil
    for tf in TIMEFRAMES:
        d = catalog_dir / "data" / "bar" / bar_type_str(symbol, tf).replace("/", "")
        if d.exists():
            shutil.rmtree(d)
    df5 = resample_closed(df1m, "5min")
    df5 = df5[df5.index <= df1m.index.max()]   # drop the last incomplete 5m bar
    return write_bars(catalog, symbol, "1m", df1m), write_bars(catalog, symbol, "5m", df5)


async def build(symbols=DEFAULT_SYMBOLS, days: int = 365, update: bool = False, repair: bool = False, host: str = "127.0.0.1",
                port: int = 4004, client_id: int = CATALOG_CLIENT_ID, catalog_dir: Path = CATALOG_DIR, log=print) -> dict:
    from ib_async import IB
    catalog = open_catalog(catalog_dir)
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=30)
    ib.reqMarketDataType(3)
    now = pd.Timestamp.now(tz="UTC").floor("min")
    summary = {"ts": now.isoformat(), "mode": "repair" if repair else "update" if update else "backfill", "port": port, "symbols": {}}
    try:
        for sym in symbols:
            if repair:
                summary["symbols"][sym] = await _repair(ib, catalog, catalog_dir, sym, log)
                continue
            catalog.write_data([make_fx_instrument(sym)])
            last1 = last_timestamp(catalog, bar_type_str(sym, "1m")) if update else None
            last5 = last_timestamp(catalog, bar_type_str(sym, "5m")) if update else None
            start = (last1 - pd.Timedelta(hours=2)).to_pydatetime() if last1 is not None else (now - pd.Timedelta(days=days)).to_pydatetime()
            log(f"{sym}: fetching 1m from {start:%Y-%m-%d %H:%M} UTC")
            rows = await fetch_1m(ib, sym, start, now.to_pydatetime(), log=log)
            df1 = ib_bars_to_frame(rows, 1)
            df5 = resample_closed(df1, "5min")
            n1 = write_bars(catalog, sym, "1m", only_new_closed(df1, last1, now))
            n5 = write_bars(catalog, sym, "5m", only_new_closed(df5, last5, now))
            gaps = gap_report(df1)
            summary["symbols"][sym] = {"bars_1m_written": n1, "bars_5m_written": n5,
                                       "first": df1.index.min().isoformat() if not df1.empty else None,
                                       "last": df1.index.max().isoformat() if not df1.empty else None,
                                       "gaps_over_15m": len(gaps), "largest_gaps": sorted(gaps, key=lambda g: -g["minutes"])[:5]}
            log(f"{sym}: wrote {n1:,} × 1m, {n5:,} × 5m · gaps>15m: {len(gaps)}")
    finally:
        ib.disconnect()
    write_manifest(summary, catalog_dir)
    return summary


async def _repair(ib, catalog, catalog_dir: Path, sym: str, log) -> dict:
    """Re-fetch only the missing ranges (excluding weekends/daily breaks/holidays) and rewrite the series"""
    df1 = read_bars(catalog, sym, "1m")
    gaps = gap_report(df1, max_gap_min=60)
    log(f"{sym}: {len(df1):,} bars · gaps>60m to repair: {len(gaps)}")
    rows = []
    for g in gaps:
        a, b = pd.Timestamp(g["from"]), pd.Timestamp(g["to"])
        log(f"{sym}: repairing {a:%Y-%m-%d %H:%M} → {b:%Y-%m-%d %H:%M}")
        rows += await fetch_1m(ib, sym, (a - pd.Timedelta(minutes=1)).to_pydatetime(), b.to_pydatetime(), log=log)
    n1 = n5 = 0
    if rows:
        df1 = pd.concat([df1, ib_bars_to_frame(rows, 1)])
        df1 = df1[~df1.index.duplicated(keep="last")].sort_index()
    if not df1.empty:                          # always rewrite so 5m is built from the same 1m set
        n1, n5 = rewrite_series(catalog, catalog_dir, sym, df1)
    left = gap_report(df1)
    log(f"{sym}: repaired · total {n1:,} × 1m · gaps>15m left: {len(left)}")
    return {"bars_1m_written": n1, "bars_5m_written": n5,
            "first": df1.index.min().isoformat() if not df1.empty else None,
            "last": df1.index.max().isoformat() if not df1.empty else None,
            "gaps_over_15m": len(left), "largest_gaps": sorted(left, key=lambda g: -g["minutes"])[:5]}


def write_manifest(summary: dict, catalog_dir: Path = CATALOG_DIR) -> None:
    path = catalog_dir / "_manifest.json"
    try:
        doc = json.loads(path.read_text())
    except Exception:
        doc = {"runs": []}
    doc["last_run"] = summary
    doc["runs"] = (doc.get("runs", []) + [{k: summary[k] for k in ("ts", "mode")} | {
        "written": {s: v["bars_1m_written"] for s, v in summary["symbols"].items()}}])[-60:]
    path.write_text(json.dumps(doc, indent=2, default=str))


def status(catalog_dir: Path = CATALOG_DIR) -> dict:
    """Summary of what is in the catalog (used by /catalog/status)"""
    out = {"path": str(catalog_dir), "exists": catalog_dir.exists(), "series": {}}
    if not catalog_dir.exists():
        return out
    try:
        out["last_run"] = json.loads((catalog_dir / "_manifest.json").read_text()).get("last_run")
    except Exception:
        out["last_run"] = None
    try:
        from nautilus_trader.model.data import Bar
        catalog = open_catalog(catalog_dir)
        for sym in DEFAULT_SYMBOLS:
            for tf in TIMEFRAMES:
                bt = bar_type_str(sym, tf)
                iv = catalog.get_intervals(Bar, bt)
                if iv:
                    out["series"][bt] = {"from": pd.Timestamp(iv[0][0], tz="UTC").isoformat(),
                                         "to": pd.Timestamp(iv[-1][1], tz="UTC").isoformat(), "files": len(iv)}
    except Exception as e:
        out["error"] = str(e)[:200]
    return out
