import pytest

from api.perf import perf_block


def test_perf_block_equity_drawdown_daily_and_metrics():
    trades = [("2026-10-19T10:00:00+00:00", 100.0), ("2026-10-19T15:00:00+00:00", -40.0), ("2026-10-20T10:00:00+00:00", -80.0),
              ("2026-10-22T10:00:00+00:00", 150.0), ("2026-10-23T10:00:00+00:00", 20.0)]
    r = perf_block(trades, base=10_000)
    m = r["metrics"]
    assert [p[1] for p in r["curve"]] == [100.0, 60.0, -20.0, 130.0, 150.0]
    assert m["net"] == 150.0 and m["return_pct"] == pytest.approx(1.5)
    assert m["max_drawdown"] == 120.0 and m["longest_drawdown_days"] == 3
    assert m["trades"] == 5 and m["win_rate"] == 60.0 and m["profit_factor"] == pytest.approx(270 / 120, abs=0.01)
    assert [d["d"] for d in r["daily"]] == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    assert r["daily"][2] == {"d": "2026-10-21", "pnl": 0.0, "n": 0} and r["daily"][0]["pnl"] == 60.0
    assert m["active_days"] == 4 and m["positive_days_pct"] == 75.0 and m["best_day"] == 150.0 and m["worst_day"] == -80.0
    assert m["sharpe"] > 0 and r["trade_pnl"] == [100.0, -40.0, -80.0, 150.0, 20.0]


def test_perf_block_empty():
    r = perf_block([], 0)
    assert r["curve"] == [] and r["metrics"] == {} and r["base"] == 100_000.0


def test_perf_block_monthly():
    r = perf_block([("2026-09-29T10:00:00+00:00", 100.0), ("2026-09-30T10:00:00+00:00", -30.0), ("2026-10-01T10:00:00+00:00", 50.0)], base=1000)
    assert [(m["m"], m["pnl"], m["n"]) for m in r["monthly"]] == [("2026-09", 70.0, 2), ("2026-10", 50.0, 1)]
    assert r["monthly"][0]["return_pct"] == pytest.approx(7.0) and r["monthly"][1]["return_pct"] == pytest.approx(50 / 1070 * 100, abs=1e-3)
    assert r["monthly"][0]["positive_days"] == 1 and r["monthly"][0]["active_days"] == 2
