import math
from datetime import datetime, timedelta, timezone

import pytest

from api import catalog as cat
from qx_strategies import QXEmaCross, bar_type_str, build_strategy, load_config
from qx_strategies.backtest import run_backtest

CFG = {"strategy": "ema_cross", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5, "fast": 5, "slow": 15, "trade_size": "20000"}


def _sine_bars(n=2000, period=120, amp=0.004):
    """Price oscillates in waves → EMAs must cross many times (5m bars, Monday–Friday)"""
    t0 = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        mid = 1.10 + amp * math.sin(2 * math.pi * i / period)
        rows.append({"t": t0 + timedelta(minutes=5 * i), "o": mid, "h": mid + 0.0002, "l": mid - 0.0002, "c": mid + 0.00005})
    df = cat.ib_bars_to_frame(rows, bar_minutes=5)
    from nautilus_trader.model.data import BarType
    from nautilus_trader.persistence.wranglers import BarDataWrangler
    df["volume"] = 1_000_000.0
    w = BarDataWrangler(BarType.from_str(bar_type_str(CFG["instrument"], 5)), cat.make_fx_instrument("EURUSD"))
    return w.process(df)


def test_config_file_builds():
    s = build_strategy(load_config("configs/strategies/ema_cross_eurusd_5m.json"))
    assert type(s).__name__ == "QXEmaCross"                   # built-in or the ema_cross@1.0.0 package (the adapter uses the same name)
    assert str(s.config.bar_type) == "EUR/USD.IDEALPRO-5-MINUTE-MID-EXTERNAL"


def test_live_uses_internal_bars():
    s = build_strategy(CFG, source="INTERNAL")
    assert str(s.config.bar_type).endswith("-MID-INTERNAL")


def test_fast_must_be_less_than_slow():
    with pytest.raises(ValueError):
        build_strategy({**CFG, "fast": 30, "slow": 10})


def test_unknown_strategy():
    with pytest.raises(ValueError):
        build_strategy({**CFG, "strategy": "nope"})


def test_backtest_market_entries_flip():
    res = run_backtest(CFG, bars=_sine_bars(), log_level="OFF")
    assert res["signals"] >= 20
    assert res["closed_positions"] >= res["signals"] - 2         # every cross = close the old position + open a new one
    assert res["open_positions"] <= 1


def test_backtest_bracket_and_long_only():
    res = run_backtest({**CFG, "sl_pips": 10, "tp_pips": 20, "allow_short": False}, bars=_sine_bars(), log_level="OFF")
    assert res["signals"] >= 20
    assert res["orders"] >= 3 * (res["signals"] // 2) - 3          # entry + SL + TP per Long entry
    assert all(t["side"] == "LONG" for t in res["trades"])
