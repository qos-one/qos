import json

import pytest

import api.soak as soak
from test_qx_strategies import CFG, _sine_bars

NS = 1_000_000_000
T0 = 1_790_000_000 * NS


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(soak, "DB_FILE", tmp_path / "soak.db")
    monkeypatch.setattr(soak, "STATE_FILE", tmp_path / "soak.json")
    return soak


def _ev(etype, ts_ns, data, sid, node="ema_eurusd", inst="EUR/USD.IDEALPRO"):
    return {"_id": f"{(ts_ns + 300_000_000) // 1_000_000}-{sid}", "type": etype, "node": node, "strategy": "EMACross-000",
            "instrument": inst, "ts_ns": ts_ns, "ts": soak._iso(ts_ns), "data": data}


def test_recorder_dedup_alive_and_gap(db):
    r = soak.Recorder()
    e = _ev("signal", T0, {"name": "cross_up", "close": 1.1, "action": 1}, 0)
    r.on_event(e)
    r.on_event(e, replay=True)
    for i in range(3):
        r.on_event({"type": "heartbeat", "node": "ema_eurusd"}, now=T0 / NS + i * 5)
    r.on_event({"type": "heartbeat", "node": "ema_eurusd"}, now=T0 / NS + 100)
    con = soak.connect()
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM alive").fetchone()[0] == 2
    assert [x[0] for x in con.execute("SELECT kind FROM incidents")] == ["heartbeat_gap"]
    r.on_event(_ev("node", T0 + 200 * NS, {"event": "stopping"}, 1))
    r.on_event({"type": "heartbeat", "node": "ema_eurusd"}, now=T0 / NS + 900)
    assert con.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    con.close()


def test_executions_slippage_and_latency(db):
    r = soak.Recorder()
    r.on_event(_ev("signal", T0, {"name": "cross_up", "close": 1.10000, "action": 1}, 0))
    r.on_event(_ev("fill", T0 + 1 * NS, {"client_order_id": "O-1", "side": "BUY", "qty": 10000, "price": 1.10002}, 1))
    r.on_event(_ev("fill", T0 + 1 * NS + 1, {"client_order_id": "O-1", "side": "BUY", "qty": 10000, "price": 1.10004}, 2))
    r.on_event(_ev("signal", T0 + 300 * NS, {"name": "cross_down", "close": 1.10100, "action": -1}, 3))
    r.on_event(_ev("fill", T0 + 302 * NS, {"client_order_id": "O-2", "side": "SELL", "qty": 40000, "price": 1.10090}, 4))
    con = soak.connect()
    ex = soak.executions(con, "ema_eurusd", T0)
    con.close()
    assert len(ex) == 2
    assert ex[0]["slippage_pips"] == pytest.approx(0.3) and ex[0]["qty"] == 20000
    assert ex[1]["slippage_pips"] == pytest.approx(1.0) and ex[1]["side"] == "SELL"
    assert 0.5 <= ex[0]["latency_s"] <= 1.0
    assert soak.pip_size("USD/JPY.IDEALPRO") == 0.01


def test_match_actions_tolerates_one_bar_offset():
    tf = 300 * NS
    live = [(T0 + tf, 1), (T0 + 10 * tf, -1), (T0 + 20 * tf, 1)]
    exp = [(T0, 1), (T0 + 10 * tf, -1), (T0 + 30 * tf, 1)]
    m = soak.match_actions(live, exp, tf)
    assert m["matched"] == 2 and m["divergences"] == 2 and m["match_rate"] == pytest.approx(2 / 3, abs=1e-3)


def test_report_verdicts(db):
    r = soak.Recorder()
    rep = soak.report("ema_eurusd")
    assert rep["verdict"] == "PENDING" and rep["since"] is None
    r.on_event(_ev("signal", T0, {"name": "cross_up", "close": 1.1, "action": 1}, 0))
    for i in range(170):
        r.on_event({"type": "heartbeat", "node": "ema_eurusd"}, now=T0 / NS + i * 25)
    rep = soak.report("ema_eurusd")
    assert rep["verdict"] == "PENDING" and rep["days"] == 1
    r.on_event(_ev("order", T0 + 5 * NS, {"event": "Rejected", "client_order_id": "O-9", "reason": "x"}, 1))
    rep = soak.report("ema_eurusd", full=True)
    assert rep["verdict"] == "NO-GO" and rep["totals"]["rejects"] == 1
    soak.save_state({"criteria": {"max_rejects": 5}})
    assert soak.report("ema_eurusd")["verdict"] == "PENDING"
    soak.reset("ema_eurusd", T0 + 10_000 * NS)
    assert soak.report("ema_eurusd")["totals"]["rejects"] == 0


def test_run_parity_live_equals_backtest(db, monkeypatch):
    from qx_strategies import backtest as bt
    bars = _sine_bars()
    monkeypatch.setattr(bt, "load_bars", lambda cfg, s=None, e=None, c=None: bars)
    acts = []
    bt.run_backtest(CFG, bars=bars, log_level="OFF", _collect_actions=acts)
    r = soak.Recorder()
    tf_ns = CFG["tf_minutes"] * 60 * NS
    for i, (t, v) in enumerate(acts):           # live nodes stamp signals with the bar OPEN time (catalog bars: close time)
        r.on_event(_ev("signal", t - tf_ns, {"name": "x", "close": 1.1, "action": int(v)}, i))
    con = soak.connect()
    con.executemany("INSERT OR IGNORE INTO alive VALUES (?,?)", [("ema_eurusd", b.ts_event // (60 * NS)) for b in bars])
    con.commit()
    con.close()
    res = soak.run_parity("ema_eurusd", CFG, since=bars[0].ts_event, until=bars[-1].ts_event)
    lvb = res["live_vs_backtest"]
    assert lvb["live_actions"] >= 20 and lvb["match_rate"] == 1.0
    assert res["shadow"]["match_rate"] >= 0.9
    assert len(res["backtest_trades"]) >= 10 and all(res["from"] <= t <= res["to"] or t >= res["from"] for t, _ in res["backtest_trades"])
    con = soak.connect()
    assert json.loads(con.execute("SELECT result FROM parity").fetchone()[0])["node"] == "ema_eurusd"
    con.close()
