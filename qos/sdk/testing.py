"""Synthetic data for testing strategies without a data catalog (qos install --bars synthetic, developer tests)"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


def synthetic_bars(instrument: str = "EUR/USD.IDEALPRO", tf_minutes: int = 5, n: int = 2000, period: int = 120,
                   amp: float = 0.004, start: datetime | None = None, seed: int = 7) -> list:
    """Nautilus bars from a sine wave + noise, deterministic (same seed = same bars every time) · Monday–Friday"""
    import random

    import pandas as pd
    from nautilus_trader.model.data import BarType
    from nautilus_trader.persistence.wranglers import BarDataWrangler

    from qos.sdk import catalog as cat
    from qx_strategies import bar_type_str

    rnd = random.Random(seed)
    sym = instrument.split(".")[0].replace("/", "")
    base = 150.0 if sym.endswith("JPY") else 1.10
    t = start or datetime(2026, 9, 7, tzinfo=timezone.utc)
    rows, i = [], 0
    while len(rows) < n:
        if t.weekday() < 5:
            mid = base * (1 + amp * math.sin(2 * math.pi * i / period) + rnd.gauss(0, amp / 20))
            rows.append({"t": t, "open": mid, "high": mid * 1.0002, "low": mid * 0.9998, "close": mid * 1.00005})
            i += 1
        t += timedelta(minutes=tf_minutes)
    df = pd.DataFrame(rows).set_index("t")
    df.index.name = "timestamp"
    df["volume"] = 1_000_000.0
    return BarDataWrangler(BarType.from_str(bar_type_str(instrument, tf_minutes)), cat.make_fx_instrument(sym)).process(df)
