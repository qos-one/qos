"""
Forward paper run of an Agent on QOS Cloud (step 37) — the worker calls it periodically with real bars that arrived after the approval date

  python -m qos.forward_check <package-folder> --bars DIR --start 2026-10-28T08:00:00Z [--json]

DIR contains <SYMBOL>.csv (ts,open,high,low,close · ts = 1-minute bar close time, UTC) · bars before --start are used as indicator warm-up
Only trades opened from --start onward are counted · prints JSON on the last line:
{"ok", "instrument", "start", "last_bar", "days", "trades", "pnl_usd" (closed + open P&L), "open_pnl_usd", "win_rate",
 "max_drawdown_usd", "max_drawdown_pct" (equity marked to market per bar),
 "equity": [[ts, equity], …], "recent_trades": […], "error"}
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

START_USD = 100_000.0


def _symbol(inst: str) -> str:
    return inst.split(".")[0].replace("/", "").upper()


def _thin(points: list, n: int = 500) -> list:
    if len(points) <= n:
        return points
    step = len(points) / n
    out = [points[int(i * step)] for i in range(n)]
    return out[:-1] + [points[-1]]


def _rate_bars(bars_dir: Path, instrument: str, catalog) -> list | None:
    """Cross pair (e.g. EUR/GBP): 1-minute GBPUSD.csv / USDGBP.csv from the same folder to convert P&L to USD
    (None = not a cross pair or no such file → run_backtest falls back to the data catalog / QOS_USD_RATES)"""
    import pandas as pd
    from qx_strategies import fx
    base, quote = fx.split_pair(instrument)
    if "USD" in (base, quote):
        return None
    for sym in (f"{quote}USD", f"USD{quote}"):
        f = bars_dir / f"{sym}.csv"
        if f.exists():
            df = pd.read_csv(f, parse_dates=["ts"]).set_index("ts").sort_index()
            df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
            return catalog.frame_to_bars(df[["open", "high", "low", "close"]], sym, "1m")
    return None


def run(pkg: str | Path, bars_dir: str | Path, start: str) -> dict:
    import pandas as pd
    from qos.publish_check import SKIP_DIRS, scan
    pkg, bars_dir = Path(pkg).resolve(), Path(bars_dir)
    sc = scan(pkg)
    if not sc["ok"]:
        return {"ok": False, "error": "package no longer passes the scan"}
    t0 = pd.Timestamp(start)
    t0 = t0.tz_localize("UTC") if t0.tzinfo is None else t0.tz_convert("UTC")
    with tempfile.TemporaryDirectory(prefix="qos-forward-") as td:
        sdir = Path(td) / "strategies"
        dest = sdir / f"{sc['name']}@{sc['version']}"
        shutil.copytree(pkg, dest, ignore=lambda _d, names: [n for n in names if n in SKIP_DIRS or n.endswith(".pyc")])
        os.environ["QOS_STRATEGIES_DIR"] = str(sdir)
        os.environ["QOS_REPORTS_DIR"] = str(Path(td) / "reports")
        from qos.sdk import catalog
        from qx_strategies.backtest import resample_bars, run_backtest
        from qx_strategies.registry import REG
        REG.scan(force=True)
        entry = REG.resolve(f"{sc['name']}@{sc['version']}")
        if entry.status != "ok":
            return {"ok": False, "error": f"package cannot be loaded: {entry.error}"}
        m = entry.manifest
        tf = int(m.get("tf_minutes") or 5)
        inst = next((i for i in m.get("instruments") or [] if (bars_dir / f"{_symbol(i)}.csv").exists()), None)
        if inst is None:
            return {"ok": True, "waiting": True, "error": "no live bars yet for the agent's instruments",
                    "start": t0.isoformat(), "trades": 0, "days": 0.0}
        df = pd.read_csv(bars_dir / f"{_symbol(inst)}.csv", parse_dates=["ts"]).set_index("ts").sort_index()
        df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
        last = df.index.max()
        if last <= t0:
            return {"ok": True, "waiting": True, "instrument": catalog.instrument_id(inst), "start": t0.isoformat(),
                    "last_bar": last.isoformat(), "trades": 0, "days": 0.0, "error": "no bars after the forward start yet"}
        bars = catalog.frame_to_bars(df[["open", "high", "low", "close"]], inst, "1m")
        if tf != 1:
            bars = resample_bars(bars, catalog.instrument_id(inst), tf)
        cfg = {"strategy": entry.ref, "strategy_id": f"QOS-{entry.name}-{entry.version}".replace("_", "-"), "tf_minutes": tf,
               "trade_size": "20000", "instrument": catalog.instrument_id(inst)}
        rate_bars = _rate_bars(bars_dir, cfg["instrument"], catalog)
        try:
            res = run_backtest({**cfg, "close_positions_on_stop": False}, bars=bars, log_level="OFF", bar_marks=True,   # the live position stays open
                               rate_bars=rate_bars)
        except Exception as e:
            return {"ok": False, "instrument": cfg["instrument"], "error": f"strategy failed: {str(e)[:400]}"}
    t0_ns = t0.value
    trades = [t for t in res.get("trades", []) if pd.Timestamp(t["opened"]).tz_convert("UTC") >= t0]
    closes = sorted((pd.Timestamp(t["closed"]).value, float(t["pnl"])) for t in trades)
    # equity marked to market at every bar close from the forward start: closed forward trades + open P&L (incl. the fees
    # already paid) of a position opened ≥ start → pnl_usd = Σ forward trades + open_pnl_usd exactly
    realized, k, peak, mdd, open_pnl = 0.0, 0, START_USD, 0.0, 0.0
    for ts_ns, unreal, opened_ns in res.get("marks", []):
        if ts_ns < t0_ns:
            continue
        while k < len(closes) and closes[k][0] <= ts_ns:
            realized += closes[k][1]
            k += 1
        open_pnl = unreal if opened_ns and opened_ns >= t0_ns else 0.0
        eq = START_USD + realized + open_pnl
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    realized += sum(p for _, p in closes[k:])
    pnl = realized + open_pnl
    eq, pts = START_USD, [[t0.isoformat(), START_USD]]
    for t in sorted(trades, key=lambda x: x["closed"]):
        eq += float(t["pnl"])
        pts.append([str(t["closed"]), round(eq, 2)])
    pts.append([last.isoformat(), round(START_USD + pnl, 2)])
    wins = [t for t in trades if t["pnl"] > 0]
    return {"ok": True, "instrument": cfg["instrument"], "start": t0.isoformat(), "last_bar": last.isoformat(),
            "days": round((last - t0).total_seconds() / 86400, 2), "trades": len(trades), "pnl_usd": round(pnl, 2),
            "open_pnl_usd": round(open_pnl, 2),
            "win_rate": round(len(wins) / len(trades), 3) if trades else None, "max_drawdown_usd": round(mdd, 2),
            "max_drawdown_pct": round(mdd / START_USD * 100, 3), "dd_cap_pct": float(m.get("max_drawdown_pct") or 10.0),
            "open_positions": res.get("open_positions", 0), "equity": _thin(pts), "recent_trades": trades[-20:]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m qos.forward_check")
    ap.add_argument("package")
    ap.add_argument("--bars", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        r = run(a.package, a.bars, a.start)
    except Exception as e:
        r = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"}
    print(json.dumps(r, default=str), flush=True)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
