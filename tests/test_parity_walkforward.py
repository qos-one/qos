import pandas as pd
import pytest

from qx_strategies import backtest as bt
from qx_strategies import reports
from qx_strategies.parity import _NettingBook, compare_actions, run_parity
from qx_strategies.walkforward import expand_grid, make_folds, walk_forward
from test_qx_strategies import CFG, _sine_bars


def test_netting_book_flip_pnl():
    b = _NettingBook(usd_quote=True, commission_rate=0.0)
    b.fill(1, 20000, 1.1000)
    b.fill(2, -40000, 1.1050)          # close Long +100, then open Short 20000
    assert b.pos == -20000 and b.avg == 1.1050
    assert b.closed[0]["pnl"] == pytest.approx(100.0)
    b.fill(3, 20000, 1.1000)
    assert b.pos == 0 and b.closed[1]["pnl"] == pytest.approx(100.0)


def test_netting_book_usdjpy_converts_to_usd():
    b = _NettingBook(usd_quote=False, commission_rate=0.0)
    b.fill(1, 10000, 150.00)
    b.fill(2, -10000, 151.00)
    assert b.closed[0]["pnl"] == pytest.approx(10000 / 151.0)


def test_compare_actions():
    r = compare_actions([(1, 1), (2, -1), (3, 1)], [(1, 1), (2, -1), (4, 1)])
    assert r["matched"] == 2 and r["divergences"] == 2 and r["match_rate"] == pytest.approx(2 / 3, abs=1e-3)


def test_parity_legacy_agent_matches_nautilus():
    r = run_parity(CFG, bars=_sine_bars())
    assert r["old_actions"] >= 20
    assert r["match_rate"] >= 0.95
    assert abs(r["pnl_diff_usd"]) <= max(5.0, abs(r["old"]["pnl_usd"]) * 0.05)


def test_parity_long_only():
    r = run_parity({**CFG, "allow_short": False}, bars=_sine_bars())
    assert r["match_rate"] >= 0.95


def test_folds_and_grid():
    f = make_folds("2026-01-01", "2026-07-01", 60, 30)
    assert len(f) == 4 and f[0]["test_start"] == f[0]["train_end"]
    g = expand_grid({"fast": [10, 40], "slow": [30], "sl_tp": [[15, 30], [0, 0]]})
    assert g == [{"fast": 10, "slow": 30, "sl_pips": 15, "tp_pips": 30}, {"fast": 10, "slow": 30, "sl_pips": 0, "tp_pips": 0}]


def test_walk_forward_in_process(monkeypatch):
    bars = _sine_bars(n=8000)

    def fake_load(cfg, start=None, end=None, catalog_dir=None):
        s, e = pd.Timestamp(start).value, pd.Timestamp(end).value
        return [b for b in bars if s <= b.ts_event < e]
    monkeypatch.setattr(bt, "load_bars", fake_load)
    r = walk_forward(CFG, "2026-09-07", "2026-10-04", train_days=10, test_days=5,
                     grid={"fast": [5, 8], "slow": [15]}, min_trades=1, workers=1, progress=lambda *_: None)
    assert r["oos"]["folds"] >= 3
    assert all(f["best_params"]["fast"] in (5, 8) for f in r["folds"])
    assert r["oos"]["trades"] > 0 and len(r["oos"]["equity"]) == r["oos"]["trades"]


def test_reports_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS", tmp_path)
    p = reports.save_report("backtest", "QX-1", {"config": CFG, "pnl_usd": 12.5, "closed_positions": 3, "win_rate": 0.5,
                                                "equity": [[str(i), i] for i in range(2000)], "trades": [{"pnl": 1}] * 900})
    items = reports.list_reports()
    assert items[0]["kind"] == "backtest" and items[0]["pnl"] == 12.5 and items[0]["file"] == p.name
    d = reports.load_report("backtest", p.name)
    assert len(d["equity"]) <= 601 and len(d["trades"]) == 500 and d["trades_total"] == 900
    with pytest.raises(ValueError):
        reports.load_report("backtest", "../secret.json")
