import math

import pytest

from qx_strategies import fx

R = {"EUR": 1.08, "GBP": 1.25, "JPY": 1 / 150.0}


def test_split_pair_forms():
    assert fx.split_pair("EUR/USD.IDEALPRO") == ("EUR", "USD")
    assert fx.split_pair("FX:USDJPY") == ("USD", "JPY")
    assert fx.split_pair("AAPL") == ("AAPL", "USD")


@pytest.mark.parametrize("sym,qty,px,usd", [
    ("EUR/USD", 10_000, 1.08, 10_800), ("USD/JPY", 20_000, 150.0, 20_000),
    ("EUR/GBP", 100_000, 0.864, 108_000), ("EUR/JPY", 10_000, 162.0, 10_800), ("AAPL", 10, 200.0, 2_000),
])
def test_notional_usd(sym, qty, px, usd):
    assert fx.notional_usd(sym, qty, px, R) == pytest.approx(usd, rel=1e-9)


def test_pnl_to_usd():
    assert fx.pnl_to_usd("USD/JPY", 100_000, 151.0) == pytest.approx(662.25, abs=0.01)
    assert fx.pnl_to_usd("EUR/GBP", 1_000, 0.86, R) == pytest.approx(1_250)
    assert fx.pnl_to_usd("EUR/USD", 50, 1.1) == 50


def test_unknown_rate_is_none_not_zero():
    assert fx.notional_usd("NZD/CHF", 1_000, 0.55, R) is None
    assert fx.pnl_to_usd("EUR/CHF", 10, 0.95, {}) is None
    assert fx.notional_usd("EUR/USD", math.nan, 1.1) is None


def test_rates_from_prices():
    r = fx.rates_from_prices({"EURUSD": 1.08, "USDJPY": 150.0, "EURGBP": 0.86, "GBPUSD": 0})
    assert r["EUR"] == 1.08 and r["JPY"] == pytest.approx(1 / 150) and "GBP" not in r and r["USD"] == 1.0
