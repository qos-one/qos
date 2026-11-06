"""Shared helpers for the backtest/parity/walkforward scripts"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", message="Timestamp.utcnow is deprecated")   # raised inside Nautilus itself


def val(v: str):
    try:
        return json.loads(v)
    except Exception:
        return v


def period(a) -> tuple[str, str]:
    import pandas as pd
    end = a.end or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    start = a.start or (pd.Timestamp(end) - pd.Timedelta(days=a.days)).strftime("%Y-%m-%d %H:%M")
    return start, end


def apply_sets(cfg: dict, sets: list[str]) -> dict:
    for kv in sets:
        k, v = kv.split("=", 1)
        cfg[k] = val(v)
    return cfg


def add_common(ap, days: int) -> None:
    ap.add_argument("config")
    ap.add_argument("--days", type=int, default=days, help="number of days back from now (if --start is not given)")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--set", action="append", default=[], help="key=value to override a config value")
