"""
Backtest a Strategy from qx_strategies on data from the data catalog

  uv run scripts/backtest.py configs/strategies/ema_cross_eurusd_5m.json --days 30
  uv run scripts/backtest.py configs/strategies/ema_cross_eurusd_5m.json --start 2026-01-01 --end 2026-06-30
  Override parameters temporarily:  --set fast=8 --set slow=21 --set sl_pips=20

A summary is printed and the full result is saved to reports/backtests/<strategy>_<time>.json (view it in Terminal → Backtests)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import warnings  # noqa: E402

import pandas as pd  # noqa: E402

warnings.filterwarnings("ignore", message="Timestamp.utcnow is deprecated")   # raised inside Nautilus itself, harmless

from qx_strategies import load_config  # noqa: E402
from qx_strategies.reports import save_report  # noqa: E402
from qx_strategies.backtest import run_backtest  # noqa: E402


from qx_strategies.reports import shown as _rep_shown  # noqa: E402

def _val(v: str):
    try:
        return json.loads(v)
    except Exception:
        return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--days", type=int, default=30, help="number of days back from the latest data (if --start is not given)")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--set", action="append", default=[], help="key=value to override a config value")
    ap.add_argument("--log", default="ERROR", help="Nautilus log level (ERROR/WARNING/INFO/OFF)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    for kv in a.set:
        k, v = kv.split("=", 1)
        cfg[k] = _val(v)
    end = a.end or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    start = a.start or (pd.Timestamp(end) - pd.Timedelta(days=a.days)).strftime("%Y-%m-%d %H:%M")
    res = run_backtest(cfg, start=start, end=end, log_level=a.log)
    path = save_report("backtest", res["strategy"], {"config": cfg, "config_file": Path(a.config).name, "start": start, "end": end, **res})
    keys = ("strategy", "bar_type", "from", "to", "bars", "signals", "orders", "closed_positions", "open_positions",
            "pnl_usd", "return_pct", "win_rate", "avg_win", "avg_loss", "max_drawdown_usd")
    w = max(map(len, keys))
    for k in keys:
        print(f"{k:<{w}}  {res[k]}")
    if res.get("pnl_note"):
        print(f"note: {res['pnl_note']}")
    print(f"\nsaved → {_rep_shown(path)}")


if __name__ == "__main__":
    main()
