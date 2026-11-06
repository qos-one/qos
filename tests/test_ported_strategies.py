import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from api import catalog as cat
from qx_strategies import SPECS, build_strategy, summary
from qx_strategies.backtest import resample_bars, run_backtest
from qx_strategies.parity import run_parity

BASE = {"instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5, "trade_size": "20000"}


def _noisy_bars(n=3000, seed=7, inst="EUR/USD.IDEALPRO", sym="EURUSD", base=1.10, tf=5):
    """Random walk plus a slow wave, so trend-following strategies get plenty of signals."""
    from nautilus_trader.model.data import BarType
    from nautilus_trader.persistence.wranglers import BarDataWrangler

    from qx_strategies import bar_type_str
    rnd = random.Random(seed)
    t0 = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    p, rows = base, []
    for i in range(n):
        p *= 1 + rnd.gauss(0, 0.0004) + 0.0003 * math.sin(2 * math.pi * i / 300)
        o, c = p, p * (1 + rnd.gauss(0, 0.0002))
        rows.append({"t": t0 + timedelta(minutes=tf * i), "o": o, "h": max(o, c) * 1.0002, "l": min(o, c) * 0.9998, "c": c})
    df = cat.ib_bars_to_frame(rows, bar_minutes=tf)
    df["volume"] = 1_000_000.0
    return BarDataWrangler(BarType.from_str(bar_type_str(inst, tf)), cat.make_fx_instrument(sym)).process(df)


def test_parity_with_legacy_agent():
    r = run_parity({**BASE, "strategy": "ema_cross", "fast": 10, "slow": 30}, bars=_noisy_bars())
    assert r["old_actions"] >= 10, r["old_actions"]
    assert r["match_rate"] >= 0.99, r["divergence_rows"][:3]


def test_validation_errors():
    with pytest.raises(ValueError):
        build_strategy({**BASE, "strategy": "ema_cross", "fast": 30, "slow": 10})


def test_specs_and_summary():
    assert "ema_cross" in SPECS
    assert all(p["key"] and p["type"] in ("int", "float", "str", "bool") for s in SPECS.values() for p in s["params"])
    assert summary({"strategy": "ema_cross", "fast": 10, "slow": 30}).startswith("EMA 10/30")


def test_resample_15m_from_1m():
    one = _noisy_bars(46, tf=1)
    b15 = resample_bars(one, "EUR/USD.IDEALPRO", 15)
    assert len(b15) == 3
    first = one[:15]
    assert b15[0].open == first[0].open and b15[0].close == first[-1].close
    assert b15[0].high.as_double() == max(b.high.as_double() for b in first)
    assert str(b15[0].bar_type) == "EUR/USD.IDEALPRO-15-MINUTE-MID-EXTERNAL"


def test_usdjpy_pnl_converted_to_usd():
    bars = _noisy_bars(3000, inst="USD/JPY.IDEALPRO", sym="USDJPY", base=150.0, tf=15)
    res = run_backtest({"strategy": "ema_cross", "instrument": "USD/JPY.IDEALPRO", "tf_minutes": 15, "trade_size": "20000",
                        "fast": 10, "slow": 30}, bars=bars, log_level="OFF")
    assert res["closed_positions"] > 5 and res["pnl_usd"] != 0
    assert abs(res["pnl_usd"] - sum(t["pnl"] for t in res["trades"])) < max(25.0, abs(res["pnl_usd"]) * 0.1)
