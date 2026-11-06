"""QA regression tests — research metrics (backtest / walk-forward / forward / publish check) and soak gates.
Every expected value is computed by hand from small deterministic bar series (fee = 0.2 bps of notional, in quote currency)."""
from __future__ import annotations

import shutil
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

EXAMPLES = Path(os.environ.get("QOS_EXAMPLES") or Path(__file__).resolve().parents[2] / "qos-examples")
T0 = pd.Timestamp("2026-09-07 00:05", tz="UTC")          # Monday, first bar CLOSE time
E = {"instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5, "trade_size": "100000"}
J = {"instrument": "USD/JPY.IDEALPRO", "tf_minutes": 5, "trade_size": "100000"}
FEE = 0.00002


def fee(px, q=100_000):
    return px * q * FEE


def bars_from_closes(closes, instrument, tf=5, t0=T0):
    from qos.sdk import catalog
    eps = 0.002 if instrument.split(".")[0].endswith("JPY") else 0.00002
    rows, prev = [], closes[0]
    for c in closes:
        rows.append({"open": prev, "high": max(prev, c) + eps, "low": min(prev, c) - eps, "close": c})
        prev = c
    idx = pd.DatetimeIndex([t0 + pd.Timedelta(minutes=tf * i) for i in range(len(closes))])
    return catalog.frame_to_bars(pd.DataFrame(rows, index=idx), instrument, tf)


def scripted(cfg, schedule):
    """Strategy that calls go_to(schedule[i]) on bar i"""
    from qx_strategies.base import QXBaseConfig, QXBaseStrategy

    class Scripted(QXBaseStrategy):
        def __init__(self, config):
            super().__init__(config)
            self.i = -1

        def on_bar(self, bar):
            self.i += 1
            if self.i in schedule:
                self.go_to(schedule[self.i], bar)

    return Scripted(config=QXBaseConfig.from_dict({"strategy_id": "QA-SCRIPT", **cfg}))


def run(cfg, closes, schedule, **kw):
    from qx_strategies.backtest import run_backtest
    bars = bars_from_closes(closes, cfg["instrument"], cfg.get("tf_minutes", 5))
    return run_backtest(cfg, bars=bars, log_level="OFF", strategy=scripted(cfg, schedule), **kw)


# ---------------- backtest ----------------
def test_round_trip_eurusd_exact():
    r = run(E, [1.1, 1.1, 1.102, 1.105, 1.105], {1: 1, 3: 0})
    exp = 500 - fee(1.1) - fee(1.105)
    assert r["trades"][0]["pnl"] == pytest.approx(exp, abs=0.01) and r["pnl_usd"] == pytest.approx(exp, abs=0.01)
    assert r["trades"][0]["opened"] == (T0 + pd.Timedelta(minutes=5)).isoformat()


def test_position_open_at_end_is_a_trade():
    r = run(E, [1.1, 1.1, 1.1, 1.105, 1.104], {1: 1})
    exp = 400 - fee(1.1) - fee(1.104)
    assert r["closed_positions"] == 1 and len(r["trades"]) == 1 and r["open_positions"] == 0
    assert r["trades"][0]["exit"] == pytest.approx(1.104)
    assert r["pnl_usd"] == pytest.approx(exp, abs=0.01)
    assert sum(t["pnl"] for t in r["trades"]) == pytest.approx(r["pnl_usd"], abs=0.01)
    assert r["equity"][-1][1] == pytest.approx(100_000 + r["pnl_usd"], abs=0.01) and r["win_rate"] == 1.0


def test_open_position_kept_when_not_closed_on_stop():
    r = run({**E, "close_positions_on_stop": False}, [1.1, 1.1, 1.1, 1.105, 1.104], {1: 1})
    assert r["closed_positions"] == 0 and r["open_positions"] == 1
    assert r["open_pnl_usd"] == pytest.approx(400 - fee(1.1), abs=0.5)        # open P&L includes the entry fee already paid
    assert r["pnl_usd"] == pytest.approx(400 - fee(1.1), abs=0.5)
    assert r["pnl_usd"] == pytest.approx(sum(t["pnl"] for t in r["trades"]) + r["open_pnl_usd"], abs=0.011)


def test_drawdown_is_marked_to_market():
    """Long 1,000,000 at 1.10, price 1.05, back to 1.10 → equity trough ≈ −50,000 although the closed trade is ≈ flat"""
    r = run({**E, "trade_size": "1000000"}, [1.1, 1.1, 1.08, 1.05, 1.08, 1.1, 1.1], {1: 1, 5: 0})
    assert r["closed_positions"] == 1 and abs(r["trades"][0]["pnl"]) < 100
    assert 49_900 < r["max_drawdown_usd"] < 50_200
    assert r["mtm"]["min"] == pytest.approx(-r["max_drawdown_usd"], abs=0.01)


def test_usdjpy_pnl_consistent_with_trades():
    """Exit at 151, last bar at 140: trade and pnl_usd both convert JPY at the rate when it was realized"""
    r = run(J, [150.0, 150.0, 150.5, 151.0, 140.0], {1: 1, 3: 0})
    exp = 100_000 / 151 - fee(150) / 150 - fee(151) / 151
    assert r["trades"][0]["pnl"] == pytest.approx(exp, abs=0.01)
    assert r["pnl_usd"] == pytest.approx(exp, abs=0.01)


def test_cross_pair_needs_usd_rates(tmp_path, monkeypatch):
    from qx_strategies.backtest import run_backtest
    monkeypatch.delenv("QOS_USD_RATES", raising=False)            # isolate from the machine's env and data catalog
    cfg = {"instrument": "EUR/JPY.IDEALPRO", "tf_minutes": 5, "trade_size": "100000"}
    bars = bars_from_closes([160.0, 160.0, 161.0, 161.0], cfg["instrument"])
    with pytest.raises(ValueError, match="usd_rates"):
        run_backtest(cfg, bars=bars, log_level="OFF", strategy=scripted(cfg, {1: 1, 2: 0}), catalog_dir=tmp_path / "empty")
    r = run_backtest(cfg, bars=bars, log_level="OFF", strategy=scripted(cfg, {1: 1, 2: 0}), usd_rates={"JPY": 1 / 150})
    exp = (100_000 - fee(160) - fee(161)) / 150                     # JPY P&L at the JPY→USD rate, not ÷ EURJPY
    assert r["trades"][0]["pnl"] == pytest.approx(exp, abs=0.01) and r["pnl_usd"] == pytest.approx(exp, abs=0.01)


# ---------------- walk-forward ----------------
def test_stitched_drawdown_by_hand():
    from qx_strategies.walkforward import stitched_drawdown
    assert stitched_drawdown([{"min": -100, "max": 200, "end": 150, "mdd": 120},
                              {"min": -300, "max": 50, "end": -250, "mdd": 350}]) == 350
    # peak in fold 1, trough in fold 2 → drawdown spans the fold boundary
    assert stitched_drawdown([{"min": 0, "max": 500, "end": 0, "mdd": 500},
                              {"min": -200, "max": 0, "end": -200, "mdd": 200}]) == 700
    assert stitched_drawdown([None, {"min": -5, "max": 0, "end": -5, "mdd": 5}]) == 5


def test_walkforward_oos_pnl_equals_fold_sum(monkeypatch):
    from qx_strategies import backtest as bt
    from qx_strategies.walkforward import walk_forward
    from test_qx_strategies import CFG, _sine_bars
    bars = _sine_bars(n=8000)

    def fake_load(cfg, start=None, end=None, catalog_dir=None):
        s, e = pd.Timestamp(start).value, pd.Timestamp(end).value
        return [b for b in bars if s <= b.ts_event < e]
    monkeypatch.setattr(bt, "load_bars", fake_load)
    r = walk_forward(CFG, "2026-09-07", "2026-10-04", train_days=10, test_days=5,
                     grid={"fast": [5, 8], "slow": [15]}, min_trades=1, workers=1, progress=lambda *_: None)
    assert r["oos"]["pnl_usd"] == pytest.approx(sum(f["test_pnl"] for f in r["folds"]), abs=0.05)
    assert r["oos"]["pnl_usd"] == pytest.approx(r["oos"]["equity"][-1][1] - 100_000, abs=0.5)   # fold-end positions are trades now
    assert r["baseline"]["pnl_usd"] == pytest.approx(sum(f["baseline_pnl"] for f in r["folds"]), abs=0.05)
    assert r["oos"]["max_drawdown_usd"] >= max(f["test_max_dd"] for f in r["folds"]) - 0.01
    assert all("_mtm" not in f for f in r["folds"])


# ---------------- forward check ----------------
def test_forward_pnl_includes_open_position_and_mtm_dd(tmp_path, monkeypatch):
    from qos.forward_check import run as fwd
    for k in ("QOS_STRATEGIES_DIR", "QOS_REPORTS_DIR"):
        monkeypatch.setenv(k, "")
    d = tmp_path / "bars"
    d.mkdir()
    idx = pd.date_range(end=pd.Timestamp("2026-10-28 08:00", tz="UTC"), periods=10 * 1440, freq="1min")
    c = 1.27 + np.cumsum(np.random.default_rng(3).normal(0, 0.0002, len(idx)))
    pd.DataFrame({"ts": idx.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": c, "high": c + 0.0002, "low": c - 0.0002,
                  "close": c}).to_csv(d / "GBPUSD.csv", index=False)
    pkg = tmp_path / "pkg"
    shutil.copytree(EXAMPLES / "rsi_reversion", pkg, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    r = fwd(pkg, d, "2026-10-23T08:00:00Z")
    assert r["ok"] and r["trades"] > 0, r
    closed = sum(t["pnl"] for t in r["recent_trades"]) if r["trades"] <= 20 else None
    if closed is not None:
        assert r["pnl_usd"] == pytest.approx(closed + r["open_pnl_usd"], abs=0.05)
    eq, peak, dd = 100_000.0, 100_000.0, 0.0                       # closed-trade drawdown is a lower bound of the MTM one
    for t in sorted(r["recent_trades"], key=lambda t: t["closed"]):
        eq += t["pnl"]
        peak, dd = max(peak, eq), max(dd, max(peak, eq) - eq)
    assert r["max_drawdown_usd"] >= dd - 0.01


# ---------------- publish check ----------------
@pytest.mark.parametrize("bt_ok,wf_ok,stage", [(False, True, "backtest"), (True, False, "walkforward"), (True, True, "done")])
def test_publish_check_ok_follows_criteria(tmp_path, monkeypatch, bt_ok, wf_ok, stage):
    import qos.install
    from qos import publish_check as pc
    for k in ("QOS_CATALOG", "QOS_STRATEGIES_DIR", "QOS_REPORTS_DIR"):
        monkeypatch.setenv(k, "")
    pkg = tmp_path / "p"
    shutil.copytree(EXAMPLES / "rsi_reversion", pkg, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(qos.install, "run_checks", lambda *a, **k: {"ok": True, "manifest": {}})
    bt = {"ok": bt_ok, "pnl_usd": 10.0 if bt_ok else -5.0, "max_drawdown_pct": 1.0, "dd_cap_pct": 10.0}
    wf = {"ok": wf_ok, "folds": 9, "positive_folds": 6 if wf_ok else 3}
    monkeypatch.setattr(pc, "_evaluate", lambda *a, **k: (bt, wf))
    r = pc.run(pkg)
    assert r["ok"] is (bt_ok and wf_ok) and r["stage"] == stage
    if not r["ok"]:
        assert "criteria not met" in r["error"]


# ---------------- soak ----------------
@pytest.fixture
def soak_db(tmp_path, monkeypatch):
    from api import soak
    monkeypatch.setattr(soak, "DB_FILE", tmp_path / "soak.db")
    monkeypatch.setattr(soak, "STATE_FILE", tmp_path / "soak.json")
    return soak


def _fills(soak, slips_pips):
    rec = soak.Recorder()
    base = int(pd.Timestamp("2026-10-01", tz="UTC").value)
    for i, s in enumerate(slips_pips):
        ts = base + i * 600 * 10**9
        rec.on_event({"type": "signal", "node": "n1", "ts_ns": ts, "instrument": "EUR/USD.IDEALPRO", "_id": f"{ts // 10**6}-0",
                      "data": {"action": 1, "close": 1.1, "name": "x"}})
        rec.on_event({"type": "fill", "node": "n1", "ts_ns": ts + 10**9, "instrument": "EUR/USD.IDEALPRO",
                      "_id": f"{ts // 10**6 + 1000}-0", "data": {"side": "BUY", "qty": 1000, "price": 1.1 + s * 0.0001,
                                                               "client_order_id": f"O{i}"}})


def _slip_check(soak):
    rep = soak.report("n1")
    return rep, next(c for c in rep["checks"] if c["name"].startswith("Avg slippage"))


def test_soak_slippage_does_not_cancel_out(soak_db):
    _fills(soak_db, [5, -5, 5, -5])
    rep, chk = _slip_check(soak_db)
    assert rep["avg_slippage_pips"] == 0.0 and rep["avg_abs_slippage_pips"] == 5.0 and chk["status"] == "fail"


def test_soak_small_slippage_passes(soak_db):
    _fills(soak_db, [0.5, -0.5, 0.8])
    _, chk = _slip_check(soak_db)
    assert chk["status"] == "pass"


def test_soak_evidence_is_per_version(soak_db):
    soak = soak_db
    t = int(pd.Timestamp("2026-10-01", tz="UTC").value)
    assert soak.note_ref("n1", "demo@1.0.0", t) is False                       # first record: legacy evidence kept
    assert soak.soak_ref("n1") == "demo@1.0.0"
    assert soak.note_ref("n1", "demo@1.0.0", t + 10**9) is False               # same version: nothing changes
    r = soak.report("n1", ref="demo@1.1.0")
    assert r["verdict"] == "PENDING" and "demo@1.0.0" in r["note"]
    assert soak.report("n1", ref="demo@1.0.0").get("note") != r["note"]
    later = t + 86_400 * 10**9
    assert soak.note_ref("n1", "demo@1.1.0", later) is True                    # version changed → soak restarts
    assert soak.start_ns("n1") == later and soak.soak_ref("n1") == "demo@1.1.0"
    assert soak.report("n1", ref="demo@1.0.0")["verdict"] == "PENDING"


def test_soak_unrecorded_version_is_pending(soak_db):
    assert soak_db.report("n1", ref="demo@1.0.0")["verdict"] == "PENDING"


def test_soak_parity_one_bar_late_is_not_a_match(soak_db, monkeypatch):
    """Live stamps signals at bar open. A live action at the catalog bar's CLOSE time is one bar late → must not match."""
    soak = soak_db
    from qx_strategies import backtest as bt
    from test_qx_strategies import CFG, _sine_bars
    bars = _sine_bars()
    monkeypatch.setattr(bt, "load_bars", lambda cfg, s=None, e=None, c=None: bars)
    acts = []
    bt.run_backtest(CFG, bars=bars, log_level="OFF", _collect_actions=acts)
    rec = soak.Recorder()
    for i, (t, v) in enumerate(acts):                     # stamped at bar close = one bar later than a correct live signal
        rec.on_event({"type": "signal", "node": "n1", "ts_ns": t, "_id": f"{t // 10**6}-{i}",
                      "data": {"name": "x", "close": 1.1, "action": int(v)}})
    con = soak.connect()
    con.executemany("INSERT OR IGNORE INTO alive VALUES (?,?)", [("n1", b.ts_event // (60 * 10**9)) for b in bars])
    con.commit()
    con.close()
    res = soak.run_parity("n1", CFG, since=bars[0].ts_event, until=bars[-1].ts_event)
    assert res["live_vs_backtest"]["matched"] == 0


# ---------------- lifecycle: soak GO is judged per strategy version (server._package_facts) ----------------
def test_soak_go_not_inherited_by_new_version(monkeypatch, tmp_path):
    """Node n1 soaked (GO) while running demo@1.0.0. User installs demo@1.1.0 (passes BT/WF) and stops n1.
    _node_ref() of the stopped node resolves the latest version; server._package_facts judges soak per version
    (soak.report(nid, ref=...)), so 1.1.0 has no soak evidence and stays wf_passed (no allow_live without its own soak)."""
    import types
    monkeypatch.setenv("QOS_STATUS_FILE", str(tmp_path / "st.json"))
    from api import server
    from api import strategy_status as sst
    import qx_strategies
    from qx_strategies.registry import REG
    monkeypatch.setattr(server.nm, "nodes", {"n1": {"id": "n1", "config_file": "demo.json"}})
    monkeypatch.setattr(server.nm, "effective_config", lambda nid: {"strategy": "demo"})
    monkeypatch.setattr(server.nm, "_running", lambda nid: False)                 # node stopped
    monkeypatch.setattr(qx_strategies, "resolve_ref", lambda ref: "demo@1.1.0")   # newest installed version
    soak = server.soak
    monkeypatch.setattr(soak, "STATE_FILE", tmp_path / "soak.json")
    monkeypatch.setattr(soak, "DB_FILE", tmp_path / "soak.db")
    soak.note_ref("n1", "demo@1.0.0")                                             # n1 soaked while running 1.0.0
    real = soak.report

    def fake_report(nid, full=False, ref=None):                                   # GO for the evidence it has, real version gate
        if ref is not None and soak.soak_ref(nid) != ref:
            return real(nid, full, ref)
        return {"verdict": "GO"}
    monkeypatch.setattr(soak, "report", fake_report)
    ents = [types.SimpleNamespace(ref=r, status="ok", kind="package") for r in ("demo@1.0.0", "demo@1.1.0")]
    monkeypatch.setattr(REG, "entries", lambda: ents)
    for r in ("demo@1.0.0", "demo@1.1.0"):
        sst.ensure(r)
        sst.set_result(r, {"ok": True, "pnl_usd": 1}, {"ok": True, "positive_folds": 6, "folds": 9})
    doc = sst.refresh(server._package_facts())
    assert doc["demo@1.1.0"]["state"] == "wf_passed", f"1.1.0 promoted to {doc['demo@1.1.0']['state']} without its own soak"


# ---------------- round 2: cross pairs convert automatically ----------------
EG = {"instrument": "EUR/GBP.IDEALPRO", "tf_minutes": 5, "trade_size": "100000"}


def test_cross_pair_per_bar_rate_by_hand(monkeypatch):
    """Long 100k EUR/GBP 0.85 → 0.86; GBP/USD is 1.20 at entry and 1.30 at exit. Each GBP amount at the rate of its time:
    −1.70 GBP fee × 1.20, +1,000 GBP × 1.30, −1.72 GBP fee × 1.30 = 1,295.724 USD."""
    from qx_strategies import backtest as bt
    monkeypatch.delenv("QOS_USD_RATES", raising=False)
    gbp = bars_from_closes([1.20, 1.20, 1.25, 1.30, 1.30], "GBP/USD.IDEALPRO")
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: gbp)
    r = run(EG, [0.85, 0.85, 0.855, 0.86, 0.86], {1: 1, 3: 0})
    exp = -fee(0.85) * 1.20 + 1000 * 1.30 - fee(0.86) * 1.30
    assert r["trades"][0]["pnl"] == pytest.approx(exp, abs=0.01) and r["pnl_usd"] == pytest.approx(exp, abs=0.01)
    assert "pnl_note" not in r


def test_cross_pair_rate_from_usd_xxx_bars(monkeypatch):
    """EUR/JPY with USD/JPY bars: 1 JPY = 1 / USDJPY USD"""
    from qx_strategies import backtest as bt
    monkeypatch.delenv("QOS_USD_RATES", raising=False)
    uj = bars_from_closes([150.0] * 4, "USD/JPY.IDEALPRO")
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: uj)
    cfg = {"instrument": "EUR/JPY.IDEALPRO", "tf_minutes": 5, "trade_size": "100000"}
    r = run(cfg, [160.0, 160.0, 161.0, 161.0], {1: 1, 2: 0})
    assert r["pnl_usd"] == pytest.approx((100_000 - fee(160) - fee(161)) / 150, abs=0.01)


def test_cross_pair_fallbacks(monkeypatch):
    from qx_strategies import backtest as bt
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: [])
    monkeypatch.delenv("QOS_USD_RATES", raising=False)
    with pytest.raises(bt.UsdRateError, match="GBP/USD"):
        run(EG, [0.85, 0.85, 0.86, 0.86], {1: 1, 2: 0})
    r = run(EG, [0.85, 0.85, 0.86, 0.86], {1: 1, 2: 0}, unpriced_ok=True)            # decision-only runs (install checks)
    assert "GBP" in r["pnl_note"]
    assert r["pnl_usd"] is None and r["balance_usd"] is None and r["max_drawdown_usd"] is None   # never GBP shown as USD
    assert r["pnl_quote_ccy"] == "GBP" and r["pnl_quote"] == pytest.approx(1000 - fee(0.85) - fee(0.86), abs=0.01)
    monkeypatch.setenv("QOS_USD_RATES", '{"USD": 1.0, "GBP": 1.25}')                  # exported by the API for its jobs
    r = run(EG, [0.85, 0.85, 0.86, 0.86], {1: 1, 2: 0})
    assert r["pnl_usd"] == pytest.approx((1000 - fee(0.85) - fee(0.86)) * 1.25, abs=0.01) and "today" in r["pnl_note"]


def test_walkforward_only_swallows_no_bars(monkeypatch):
    from qx_strategies import backtest as bt
    from qx_strategies.walkforward import _job
    monkeypatch.setattr(bt, "load_bars", lambda *a, **k: [])
    assert "no bars" in _job((dict(E, strategy="ema_cross"), "2026-09-07", "2026-09-08", False))["error"]

    def boom(*a, **k):
        raise bt.UsdRateError("no GBP/USD bars")
    monkeypatch.setattr(bt, "run_backtest", boom)
    with pytest.raises(bt.UsdRateError):
        _job((dict(EG, strategy="ema_cross"), "2026-09-07", "2026-09-08", False))


def test_forward_check_reads_rate_csv(tmp_path):
    from qos.forward_check import _rate_bars
    from qos.sdk import catalog
    d = tmp_path / "bars"
    d.mkdir()
    idx = pd.date_range("2026-10-01 00:01", periods=5, freq="1min", tz="UTC")
    pd.DataFrame({"ts": idx.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": 1.25, "high": 1.2501, "low": 1.2499, "close": 1.25}).to_csv(d / "GBPUSD.csv", index=False)
    rb = _rate_bars(d, "EUR/GBP.IDEALPRO", catalog)
    assert len(rb) == 5 and str(rb[0].bar_type.instrument_id) == "GBP/USD.IDEALPRO"
    assert _rate_bars(d, "EUR/USD.IDEALPRO", catalog) is None


# ---------------- round 2: first version record ----------------
def test_soak_first_record_keeps_only_current_process(soak_db):
    soak = soak_db
    t1 = int(pd.Timestamp("2026-10-01", tz="UTC").value)
    soak.reset("a", t1)
    assert soak.note_ref("a", "demo@1.1.0", started_ns=t1 + 86_400 * 10**9) is True       # restarted (maybe upgraded) after t1
    assert soak.start_ns("a") == t1 + 86_400 * 10**9
    soak.reset("b", t1)
    assert soak.note_ref("b", "demo@1.0.0", started_ns=t1 - 10**9) is False                # this process produced all evidence
    assert soak.start_ns("b") == t1
    soak.reset("c", t1)
    assert soak.note_ref("c", "demo@1.0.0", now_ns=t1 + 5 * 10**9) is True                 # unknown process → fresh soak


# ---------------- round 3: rate coverage and unpriced results ----------------
def _eg_bars(n):
    return [0.85, 0.85] + [0.86] * (n - 2)


def test_rate_series_starting_after_first_fill_is_flagged(monkeypatch):
    from qx_strategies import backtest as bt
    gbp = bars_from_closes([1.25] * 6, "GBP/USD.IDEALPRO")[3:]                    # starts at bar 3, entry fill at bar 1
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: gbp)
    r = run(EG, _eg_bars(6), {1: 1, 4: 0})
    assert "only cover" in r["pnl_note"] and r["pnl_usd"] is not None


def test_rate_series_ending_long_before_last_bar_is_flagged(monkeypatch):
    from qx_strategies import backtest as bt
    gbp = bars_from_closes([1.25] * 3, "GBP/USD.IDEALPRO")                        # ends at bar 2
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: gbp)
    n = 2 * 288 + 10                                                              # > 1 day of 5m bars after the last rate
    r = run(EG, _eg_bars(n), {1: 1, n - 2: 0})
    assert "only cover" in r["pnl_note"]


def test_full_rate_coverage_has_no_note(monkeypatch):
    from qx_strategies import backtest as bt
    gbp = bars_from_closes([1.25] * 6, "GBP/USD.IDEALPRO")
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: gbp)
    assert "pnl_note" not in run(EG, _eg_bars(6), {1: 1, 4: 0})


def test_quick_backtest_cross_without_rates_reports_quote_pnl(monkeypatch):
    from qos.sdk import Param, Strategy, quick_backtest
    from qx_strategies import backtest as bt
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: [])
    monkeypatch.delenv("QOS_USD_RATES", raising=False)

    class QaQuickCross(Strategy):
        name = "qa_quick_cross"
        label = "QA quick cross"
        version = "1.0.0"
        instruments = ["EUR/GBP"]
        timeframe = "5m"
        fast = Param(3, min=2, max=10)

        def on_bar(self, bar):
            self.target(+1 if bar.close > bar.open else -1)
    closes = [0.85 + 0.001 * ((i * 7) % 5 - 2) for i in range(60)]
    res = quick_backtest(QaQuickCross, bars=bars_from_closes(closes, "EUR/GBP.IDEALPRO"), trade_size=100_000)
    assert res["pnl_usd"] is None and res["pnl_quote_ccy"] == "GBP" and isinstance(res["pnl_quote"], float)
    assert res["actions"]


def test_rate_hole_in_the_middle_is_flagged(monkeypatch):
    """Rate bars with a 3-weekday hole while the position is open → note; a weekend-only hole → no note."""
    from qx_strategies import backtest as bt
    n = 6 * 288                                                                  # 6 days of 5m bars from Monday
    full = bars_from_closes([1.25] * n, "GBP/USD.IDEALPRO")
    hole = [b for i, b in enumerate(full) if not (288 <= i < 4 * 288)]           # Tue..Thu missing
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: hole)
    r = run(EG, _eg_bars(n), {1: 1, n - 2: 0})
    assert "gap" in r["pnl_note"]
    t0 = pd.Timestamp("2026-09-11 00:05", tz="UTC")                              # Friday → weekend → Monday
    m = 4 * 288
    wk = bars_from_closes([1.25] * m, "GBP/USD.IDEALPRO", t0=t0)
    wk = [b for b in wk if pd.Timestamp(b.ts_event, unit="ns", tz="UTC").weekday() < 5]
    monkeypatch.setattr(bt, "load_rate_bars", lambda *a, **k: wk)
    from qx_strategies.backtest import run_backtest
    eg = bars_from_closes(_eg_bars(m), "EUR/GBP.IDEALPRO", t0=t0)
    r = run_backtest(EG, bars=eg, log_level="OFF", strategy=scripted(EG, {1: 1, m - 2: 0}))
    assert "pnl_note" not in r
