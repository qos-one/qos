"""Step 16: portfolio-wide risk — per-currency exposure, correlated groups, VaR, drawdown, margin, 2-layer enforcement"""
import asyncio
import json
import math
import os
import random
from pathlib import Path

import pytest

from qx_strategies import portfolio as pf

RATES = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "AUD": 0.66, "JPY": 1 / 150}


def _payload(exp=None, netliq=100_000.0, limits=None, returns=None, clusters=None, signs=None):
    lim = pf.limits_usd(pf.clean_limits(limits), netliq)
    return {"netliq": netliq, "exposure": exp or {}, "rates": RATES, "clusters": clusters or [], "signs": signs or {},
            "returns": returns or {}, "limits_usd": lim}


def test_currency_legs():
    assert pf.order_delta("EUR/USD.IDEALPRO", 1, 20_000, 1.08, RATES) == {"EUR": pytest.approx(21_600)}
    d = pf.order_delta("FX:USDJPY", 1, 20_000, 150.0, RATES)
    assert d == {"JPY": pytest.approx(-20_000)}                      # long USD/JPY = short JPY 3,000,000 = −20k USD
    d = pf.order_delta("EUR/GBP", -1, 10_000, 0.85, RATES)
    assert d["EUR"] == pytest.approx(-10_800) and d["GBP"] == pytest.approx(10_000 * 0.85 * 1.27)
    full = pf.with_usd({"EUR": 21_600, "GBP": 25_400, "AUD": 13_200})
    assert full["USD"] == pytest.approx(-60_200) and pf.max_currency({"EUR": 21_600, "GBP": 25_400, "AUD": 13_200}) == ("USD", pytest.approx(60_200))


def test_three_dollar_shorts_hit_usd_limit_but_closing_passes():
    exp = {"EUR": 60_000, "GBP": 50_000, "AUD": 30_000}               # long EUR/GBP/AUD vs USD = USD −140k
    p = _payload(exp, limits={"max_currency_pct": 150})
    ok, why, kind = pf.check_order(p, "AUD/USD.IDEALPRO", 1, 20_000, 0.66, 0)
    assert not ok and kind == "currency" and "USD exposure" in why
    assert pf.check_order(p, "EUR/USD.IDEALPRO", -1, 20_000, 1.08, 20_000)[0]     # closing/reducing long EUR = reduces USD short
    assert pf.check_order(p, "USD/JPY.IDEALPRO", -1, 20_000, 150.0, 0)[0] is False   # short USD/JPY = more USD short
    assert pf.check_order(p, "USD/JPY.IDEALPRO", 1, 20_000, 150.0, 0)[0]          # long USD reduces risk


def test_flip_counts_the_close():
    exp = {"EUR": 140_000}
    p = _payload(exp, limits={"max_currency_pct": 150, "max_gross_x": 1.45})
    assert pf.check_order(p, "EUR/USD", -1, 20_000, 1.0, 129_629)[0]              # flip from large long → small short = reduction
    assert not pf.check_order(p, "EUR/USD", 1, 20_000, 1.08, 0)[0]                # adding long → gross over 145k


def test_clusters_and_signs():
    rnd = random.Random(1)
    base = [rnd.gauss(0, 0.005) for _ in range(60)]
    rets = {"EUR": [x + rnd.gauss(0, 0.0008) for x in base], "GBP": [x + rnd.gauss(0, 0.0008) for x in base],
            "CHF": [-x + rnd.gauss(0, 0.0008) for x in base], "JPY": [rnd.gauss(0, 0.005) for _ in range(60)]}
    cl, signs, cm = pf.clusters_from(rets, 0.7)
    assert cl == [["CHF", "EUR", "GBP"]] and signs == {"CHF": 1, "EUR": -1, "GBP": -1} and cm["EUR/GBP"] > 0.9
    ce = pf.cluster_exposures({"EUR": 50_000, "GBP": 40_000, "CHF": 30_000}, cl, signs)
    assert ce[0]["exposure"] == pytest.approx(60_000)                # CHF long partly offsets EUR+GBP long


def test_var_historical():
    rets = {"EUR": [-0.02] + [0.001] * 19, "GBP": [-0.01] * 2 + [0.0] * 18}
    assert pf.var95({"EUR": 100_000}, rets) == 2_000.0
    assert pf.var95({"EUR": 100_000, "GBP": 100_000}, rets) == pytest.approx(3_000.0)
    assert pf.var95({"EUR": 100_000, "JPY": 5_000}, rets) is None     # no JPY data = no VaR decision
    p = _payload({"EUR": 60_000}, limits={"max_var_pct": 1.5}, returns=rets)
    ok, why, kind = pf.check_order(p, "EUR/USD", 1, 40_000, 1.08, 0)
    assert not ok and kind == "var"


def test_currency_returns_from_closes():
    closes = {"EURUSD": [("2026-10-01", 1.0), ("2026-10-02", 1.01), ("2026-10-03", 1.0)],
              "USDJPY": [("2026-10-01", 150.0), ("2026-10-02", 147.0), ("2026-10-03", 150.0)]}
    dates, r = pf.currency_returns(closes)
    assert dates == ["2026-10-02", "2026-10-03"] and r["EUR"][0] == pytest.approx(0.01)
    assert r["JPY"][0] == pytest.approx(150 / 147 - 1)               # USD/JPY down = JPY stronger


def test_existing_strategies_fit_default_limits():
    """The soak sizes (trade_size 20,000 · grid max ~57k) all held in one direction on 100,000 capital stay within the defaults"""
    exp = {}
    for inst, side, qty, px in (("EUR/USD", 1, 20_000, 1.08), ("GBP/USD", 1, 20_000, 1.27), ("AUD/USD", 1, 57_000, 0.66),
                                ("USD/JPY", -1, 20_000, 150.0), ("EUR/USD", 1, 20_000, 1.08)):
        for k, v in pf.order_delta(inst, side, qty, px, RATES).items():
            exp[k] = exp.get(k, 0.0) + v
    rnd = random.Random(3)
    common = [rnd.gauss(0, 0.004) for _ in range(60)]
    rets = {c: [x + rnd.gauss(0, 0.002) for x in common] for c in ("EUR", "GBP", "AUD", "JPY")}
    cl, signs, _ = pf.clusters_from(rets, 0.7)
    m = pf.metrics(exp, {"clusters": cl, "signs": signs, "returns": rets})
    lim = pf.limits_usd(pf.clean_limits(None), 100_000)
    assert m["currency"] < lim["currency"] and m["gross"] < lim["gross"] and m["cluster"] < lim["cluster"]
    assert m["var"] < lim["var"], (m["var"], lim["var"])


def test_node_blocks_entry_with_portfolio_payload():
    from qx_strategies import backtest as bt, risk
    from test_qx_strategies import CFG, _sine_bars
    risk.STATE.reset()
    try:
        risk.STATE.update({"portfolio": _payload({"EUR": 200_000}, limits={"max_currency_pct": 150})})
        r = bt.run_backtest({**CFG, "allow_short": False}, bars=_sine_bars(800), log_level="OFF")    # more long EUR = blocked
        assert r["closed_positions"] == 0 and any("exposure" in k for k in risk.STATE.blocked)
        risk.STATE.reset()
        risk.STATE.update({"portfolio": _payload({"EUR": 200_000}, limits={"max_currency_pct": 150})})
        r = bt.run_backtest({**CFG, "allow_short": True}, bars=_sine_bars(800), log_level="OFF")     # risk-reducing short = allowed
        assert r["closed_positions"] > 0
    finally:
        risk.STATE.reset()


# ---------- server ----------
@pytest.fixture()
def srv(tmp_path, monkeypatch):
    from api import holds, server
    monkeypatch.setenv("QOS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    risk = {"halted": False, "halt_reason": ""}
    monkeypatch.setattr(server, "load_risk", lambda account=None: dict(risk))
    monkeypatch.setattr(server, "save_risk", lambda d, account=None: risk.update(d))
    monkeypatch.setattr(server, "active_account", lambda: "U1")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(server.notify, "fire", lambda msg, kind="": sent.append((kind, msg)))

    async def pub():
        return {}
    monkeypatch.setattr(server, "publish_risk", pub)
    monkeypatch.setattr(server, "_pf_state", {"stats": {}, "stats_loading": False, "last": None, "prev": None, "alerts": {}, "fills": []})
    server._test_risk, server._test_sent = risk, sent
    return server


def _snap(metrics, util=None, dd=0.0, excess=60.0, lim_usd=None):
    lim = pf.clean_limits(None)
    return {"metrics": {"gross": 0, "currency": 0, "cluster": 0, "var": 0, **metrics}, "limits": lim,
            "limits_usd": lim_usd or pf.limits_usd(lim, 100_000), "utilization": util or {}, "drawdown": {"dd_pct": dd, "peak": 105_000},
            "excess_pct": excess}


def test_drawdown_halts_and_resume_resets_peak(srv):
    srv._pf_state["last"] = _snap({}, dd=6.0)
    acted = asyncio.run(srv._portfolio_watch())
    assert acted == ["halt"] and srv._test_risk["halted"] and srv._test_risk["halt_reason"].startswith("portfolio drawdown")
    (srv.DATA_DIR / "portfolio_equity.json").write_text(json.dumps({"U1": {"peak": 105_000}}))
    asyncio.run(srv.resume_trading())
    assert not srv._test_risk["halted"] and json.loads((srv.DATA_DIR / "portfolio_equity.json").read_text()) == {}


def test_low_margin_halts(srv):
    srv._pf_state["last"] = _snap({}, excess=20.0)
    assert asyncio.run(srv._portfolio_watch()) == ["halt"] and "excess liquidity" in srv._test_risk["halt_reason"]


def test_alert_once_per_day(srv):
    srv._pf_state["last"] = _snap({"currency": 130_000}, util={"currency": 86.7})
    assert asyncio.run(srv._portfolio_watch()) == ["alert:currency"]
    assert asyncio.run(srv._portfolio_watch()) == []


def test_api_layer_holds_node_that_pushed_over(srv):
    import time
    from api import holds
    srv._pf_state["prev"] = _snap({"currency": 140_000})
    srv._pf_state["last"] = _snap({"currency": 165_000})
    srv._pf_state["fills"] = [(time.time() - 5, "ml_eurusd"), (time.time() - 600, "old_node")]
    acted = asyncio.run(srv._portfolio_watch())
    assert "hold:ml_eurusd" in acted and "old_node" not in holds.payload() and "currency" in holds.payload()["ml_eurusd"]


def test_drawdown_peak_tracking(srv):
    assert srv._pf_drawdown(100_000)["dd_pct"] == 0.0
    assert srv._pf_drawdown(110_000)["peak"] == 110_000
    assert srv._pf_drawdown(104_500)["dd_pct"] == pytest.approx(5.0)


def test_live_readiness_requires_portfolio_limits(monkeypatch):
    from api import server
    monkeypatch.setattr(server, "load_risk", lambda account=None: {"allow_live": True, "max_position_notional_usd": 1})
    monkeypatch.setattr(server, "active_account", lambda: "U1")
    monkeypatch.setattr(server.ops, "live_code_set", lambda: True)
    monkeypatch.setattr(server, "_risk_doc", lambda: {"default": {}, "accounts": {"U1": {"max_daily_loss_usd": 500}}})
    c = {x["name"]: x for x in server._live_readiness_global()}
    assert not c["portfolio_limits"]["ok"] and c["portfolio_limits"]["level"] == "block"
    monkeypatch.setattr(server, "_risk_doc", lambda: {"default": {}, "accounts": {"U1": {"max_daily_loss_usd": 500, "portfolio": {"max_dd_pct": 4}}}})
    assert {x["name"]: x for x in server._live_readiness_global()}["portfolio_limits"]["ok"]
