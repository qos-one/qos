"""Push 1-minute bars to QOS Cloud for Agent forward paper runs (step 37, admin)

  qos feed status · qos feed push [--days 30] [--instrument EURUSD …]
  the API pushes them automatically every 5 minutes when QOS_CLOUD_FEED=1

For internal testing only: broker data (e.g. IBKR) must not be redistributed — production must use a feed from a licensed provider
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .client import Cloud, get

CHUNK = 5000


def local_instruments(catalog_dir=None) -> list[str]:
    from qos.sdk import catalog
    d = (catalog_dir or catalog.default_dir()) / "data" / "bar"
    return sorted({p.name.split(".")[0] for p in d.glob("*-1-MINUTE-*") if p.is_dir()}) if d.exists() else []


def status(c: Cloud | None = None) -> dict:
    return (c or get())._authed("GET", "/admin/feed/status")


def push(c: Cloud | None = None, instruments: list[str] | None = None, days: int = 30, catalog_dir=None, log=None) -> dict:
    import pandas as pd
    from qos.sdk import catalog
    c = c or get()
    log = log or (lambda *_: None)
    have = {r["instrument"]: r for r in status(c).get("instruments", [])}
    now = datetime.now(timezone.utc)
    out = {}
    for sym in instruments or local_instruments(catalog_dir):
        sym = sym.replace("/", "").upper()
        last = have.get(sym, {}).get("last")
        start = datetime.fromisoformat(last.replace("Z", "+00:00")) if last else now - timedelta(days=days)
        bars = catalog.bars(sym, "1m", start, now, catalog_dir)
        rows = [[pd.Timestamp(b.ts_event, unit="ns", tz="UTC").isoformat(), b.open.as_double(), b.high.as_double(),
                 b.low.as_double(), b.close.as_double()] for b in bars]
        sent = 0
        for i in range(0, len(rows), CHUNK):
            r = c._authed("POST", "/admin/feed/bars", json_body={"instrument": sym, "bars": rows[i:i + CHUNK]})
            sent += r.get("inserted", 0)
        out[sym] = sent
        log(f"  {sym}: {sent} new bars" + (f" (from {start:%Y-%m-%d %H:%M})" if rows else " (up to date)"))
    return out
