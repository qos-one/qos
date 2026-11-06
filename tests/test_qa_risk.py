"""QA regression tests — risk controls (node-side limits, package limits, portfolio, ramp, operator roles).

Each test asserts the CORRECT behaviour for a finding from the QA audit (IDs match qa/risk/).
API-side (api/server.py) fixes are tracked in qa/risk/SERVER_CHANGES.md and tested in qa/risk/.
"""
from __future__ import annotations

import pytest

import api.operators as ops
from api import ramp
from api.package_guard import PackageGuard
from qx_strategies import package_limits as plm
from qx_strategies import portfolio as pf
from qx_strategies.risk import STATE, RiskState, check_entry, notional_usd

RATES = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 1 / 150, "CHF": 1.13, "AUD": 0.66}
M = 60 * 1_000_000_000


def st(**over):
    s = RiskState()
    s.update({"halted": False, "max_order_notional_usd": 150_000, "max_position_notional_usd": 400_000, "max_open_positions": 3,
              "max_daily_loss_usd": 2000, "daily_pnl_usd": 0.0, "open_keys": [], "position_notional_usd": {}, "fx_rates": RATES, **over})
    return s


# ---------- F-CROSS-1 / F-CROSS-2: cross-pair notional in USD (node) ----------
def test_cross_notional_in_usd():
    assert notional_usd("EUR/USD.IDEALPRO", 20_000, 1.1) == pytest.approx(22_000)               # USD-quoted pairs unchanged
    assert notional_usd("USD/JPY.IDEALPRO", 20_000, 150) == 20_000
    assert notional_usd("AAPL.NASDAQ", 10, 200) == 2_000
    assert notional_usd("EUR/GBP.IDEALPRO", 100_000, 0.85, RATES) == pytest.approx(108_000)    # base EUR × 1.08
    assert notional_usd("EUR/JPY.IDEALPRO", 1_000, 162, RATES) == pytest.approx(1_080)
    assert notional_usd("EUR/GBP.IDEALPRO", 100_000, 0.85) is None                              # no rates → cannot price


def test_check_entry_cross_pairs():
    ok, why = check_entry("EUR/GBP.IDEALPRO", 145_000, 0.85, state=st())                         # 156.6k USD > 150k (was 123k in GBP)
    assert not ok and "order notional" in why
    assert check_entry("EUR/JPY.IDEALPRO", 1_000, 162.0, state=st())[0]                         # was counted as 162k "USD"
    ok, why = check_entry("EUR/GBP.IDEALPRO", 1_000, 0.85, state=st(fx_rates={}))               # unknown rate = block
    assert not ok and "no FX rate" in why
    # rates may also come from the portfolio block of the payload
    assert check_entry("EUR/JPY.IDEALPRO", 1_000, 162.0, state=st(fx_rates=None, portfolio={"rates": RATES}))[0]


def test_check_entry_unchanged_rules():
    assert check_entry("EUR/USD.IDEALPRO", 136_000, 1.10, state=st())[0]
    assert not check_entry("EUR/USD.IDEALPRO", 137_000, 1.10, state=st())[0]
    assert not check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=st(daily_pnl_usd=-2000))[0]
    s = RiskState(); s.require_sync = True
    assert not check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=s)[0]


# ---------- F-PKG-COUNT: entries blocked after package_ok must not count ----------
def test_package_check_without_record_then_commit():
    L = plm.PackageLimits(); L.configure({"max_notional_usd": 1e9, "max_orders_per_hour": 2, "max_flips_per_hour": 30, "instruments": []})
    for i in range(10):                                                    # checked but never sent
        assert L.check("EUR/USD.IDEALPRO", 1, 1000, 1.1, 0, i * M, record=False)[0]
    assert L.summary()["orders_last_hour"] == 0 and not L.tripped
    L.commit(1, 0, 11 * M); L.commit(1, 0, 12 * M)
    assert not L.check("EUR/USD.IDEALPRO", 1, 1000, 1.1, 0, 13 * M, record=False)[0]


def test_backtest_entries_blocked_by_portfolio_do_not_trip_package():
    """Through QXBaseStrategy._enter: every entry is blocked by the portfolio limit → 0 orders counted, never tripped"""
    from qx_strategies.backtest import run_backtest
    from test_qx_strategies import CFG, _sine_bars
    plm.LIMITS.configure({"instruments": ["EUR/USD.IDEALPRO"], "max_notional_usd": 1e9, "max_orders_per_hour": 2, "max_flips_per_hour": 30})
    STATE.update({"halted": False, "fx_rates": RATES,
                  "portfolio": {"limits_usd": {"gross": 1.0}, "rates": RATES, "exposure": {}}})
    try:
        r = run_backtest(dict(CFG), bars=_sine_bars(600), log_level="OFF")
        assert r["closed_positions"] == 0
        assert plm.LIMITS.summary()["orders_last_hour"] == 0 and not plm.LIMITS.tripped and plm.LIMITS.stats["breaches"] == 0
    finally:
        plm.LIMITS.reset()


def test_package_cross_notional():
    L = plm.PackageLimits(); L.configure({"max_notional_usd": 100_000, "max_orders_per_hour": 60, "max_flips_per_hour": 30, "instruments": []})
    ok, _, kind = L.check("EUR/GBP.IDEALPRO", 1, 95_000, 0.85, 0, 0, rates=RATES)                # 102.6k USD
    assert not ok and kind == "max_notional_usd"
    assert L.check("EUR/JPY.IDEALPRO", 1, 50_000, 162, 0, 1, rates=RATES)[0]                     # 54k USD
    ok, _, kind = L.check("NZD/SEK.IDEALPRO", 1, 1000, 6.4, 0, 2, rates=RATES)                  # live, unknown rate → block (no breach)
    assert not ok and kind == "rate" and L.stats["breaches"] == 1
    assert L.check("NZD/SEK.IDEALPRO", 1, 1000, 6.4, 0, 3, rates=None)[0]                       # backtest: no rate source → cap skipped


# ---------- F-PKG-LIMIT: layer 2 checks every non-reduce-only order type ----------
def _guard(limits, rates=None):
    return PackageGuard(lambda node: limits, rates_of=(lambda: rates) if rates else None)


def _order(coid, typ, qty, price, inst="EUR/USD.IDEALPRO", reduce_only=None):
    return {"node": "n1", "type": "order", "instrument": inst, "ts_ns": 1,
            "data": {"event": "Submitted", "client_order_id": coid, "side": "BUY", "qty": qty, "price": price, "order_type": typ,
                     "reduce_only": reduce_only}}


def test_guard_checks_limit_and_stop_entries():
    g = _guard({"max_notional_usd": 100_000, "max_orders_per_hour": 60, "max_flips_per_hour": 30, "instruments": []})
    out = g.on_event(_order("O-1", "LIMIT", 10_000_000, 1.1))
    assert out and out[0]["limit"] == "max_notional_usd"
    ev = _order("O-2", "STOP_MARKET", 10_000_000, None)
    ev["data"]["trigger_price"] = 1.1
    out = g.on_event(ev)
    assert out and out[0]["limit"] == "max_notional_usd"
    assert g.on_event(_order("O-3", "LIMIT", 10_000_000, 1.1, reduce_only=True)) == []         # exits are never flagged


def test_guard_cross_pair_in_usd():
    g = _guard({"max_notional_usd": 100_000, "max_orders_per_hour": 60, "max_flips_per_hour": 30, "instruments": []}, rates=RATES)
    assert g.on_event(_order("C-1", "MARKET", 50_000, 162.0, inst="EUR/JPY.IDEALPRO")) == []      # 54k USD (was 8.1M)
    out = g.on_event(_order("C-2", "MARKET", 100_000, 0.85, inst="EUR/GBP.IDEALPRO"))             # 108k USD (was 85k)
    assert out and out[0]["limit"] == "max_notional_usd"


# ---------- F-PF-RATE / F-PF-NAN: portfolio ----------
def test_portfolio_unknown_rate_blocks():
    p = {"limits_usd": {"gross": 100_000, "currency": 1e9, "cluster": 1e9, "var": 1e9}, "rates": {"EUR": 1.1}, "exposure": {}}
    ok, why, kind = pf.check_order(p, "NZD/SEK.IDEALPRO", 1, 1_000, 6.4)
    assert not ok and kind == "rate"
    assert not pf.check_order(p, "NZD/USD.IDEALPRO", 1, 50_000_000, 0.6)[0]                     # priced from the order price → over gross
    assert pf.check_order(p, "NZD/USD.IDEALPRO", 1, 1_000, 0.6)[0]


def test_portfolio_limits_must_be_finite():
    for k in ("max_dd_pct", "max_gross_x", "max_var_pct"):
        for bad in (float("nan"), float("inf")):
            with pytest.raises(ValueError):
                pf.clean_limits({k: bad})


# ---------- F-RAMP-LOT: below one lot is refused, never rounded up ----------
def test_ramp_size_below_one_lot_refused():
    assert ramp.scaled_size("20000", 0.1) == "2000"
    assert ramp.scaled_size("20000", 0.25) == "5000"
    assert ramp.scaled_size("25000", 0.1) == "2000"                                             # 2,500 → 2 lots (never above the stage)
    for base in ("1000", "5000"):
        assert "below one lot" in ramp.size_problem(base, 0.1)
        with pytest.raises(ValueError):
            ramp.scaled_size(base, 0.1)
    assert ramp.size_problem("20000", 0.1) == ""


def test_ramp_final_stage_uses_configured_trade_size():
    """Round 3: the 100% stage trades the configured trade_size as is — no floor to lots, no below-one-lot refusal"""
    for base in ("20000", "2500", "1500", "500", "12345"):
        assert ramp.size_problem(base, 1.0) == ""
        assert ramp.scaled_size(base, 1.0) == base
    assert ramp.scaled_size("2500", 1.0, lot=1000) == "2500"                                    # partial stages still floor …
    assert ramp.scaled_size("2500", 0.5, lot=1000) == "1000"
    with pytest.raises(ValueError, match="below one lot"):                                      # … and still refuse below 1 lot
        ramp.scaled_size("1500", 0.5, lot=1000)
    r = ramp.new_ramp()
    r["stage"] = len(r["config"]["stages"]) - 1
    assert ramp.multiplier(r) == 1.0 and ramp.scaled_size("1500", ramp.multiplier(r), r["config"]["lot"]) == "1500"
    assert "invalid trade_size" in ramp.size_problem("nan", 1.0)                                # bad input is still refused
    with pytest.raises(ValueError):
        ramp.scaled_size("nan", 1.0)


# ---------- F-ASSIST-MODIFY / F-ASSIST-ACCT: assistant permissions ----------
@pytest.mark.parametrize("method,path,ok", [
    ("PUT", "/order/123", False), ("PUT", "/order/123/", False), ("POST", "/order", False), ("POST", "/order/", False),
    ("POST", "/setup/account", False), ("POST", "/order/123/cancel", True), ("POST", "/order/prepare", True),
    ("POST", "/order/confirm", True), ("PUT", "/order/prepare", False), ("POST", "/positions/FX:EURUSD/close", True),
    ("POST", "/positions/close_all", False), ("POST", "/positions/FX:EURUSD/reverse", False), ("GET", "/order/123", True),
])
def test_assistant_order_permissions(method, path, ok):
    assert ops.allowed("assistant", method, path) is ok


def test_mcp_has_no_modify_or_switch_account_tool():
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parent.parent / "mcp_server.py").read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert "modify_order" not in names and "switch_account" not in names and "prepare_order" in names
