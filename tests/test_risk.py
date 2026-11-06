import time

from qx_strategies.backtest import run_backtest
from qx_strategies.risk import STATE, RiskState, api_key, check_entry, notional_usd
from qx_strategies.risk_guard import QXRiskGuard, QXRiskGuardConfig
from test_events import FakePub
from test_qx_strategies import CFG, _sine_bars

LIM = {"halted": False, "max_order_notional_usd": 50_000, "max_position_notional_usd": 60_000, "max_open_positions": 2,
       "max_daily_loss_usd": 1000, "daily_pnl_usd": -100, "open_keys": [], "position_notional_usd": {}}


def st(**over):
    s = RiskState()
    s.update({**LIM, **over})
    return s


def test_keys_and_notional():
    assert api_key("EUR/USD.IDEALPRO") == "FX:EURUSD" and api_key("BTC/USD.PAXOS") == "CRYPTO:BTC"
    assert notional_usd("EUR/USD.IDEALPRO", 20000, 1.1) == 22000
    assert notional_usd("USD/JPY.IDEALPRO", 20000, 150) == 20000


def test_check_entry_rules():
    assert check_entry("EUR/USD.IDEALPRO", 20000, 1.1, state=st()) == (True, "")
    ok, why = check_entry("EUR/USD.IDEALPRO", 50000, 1.1, state=st())
    assert not ok and "order notional" in why
    ok, why = check_entry("EUR/USD.IDEALPRO", 20000, 1.1, state=st(position_notional_usd={"FX:EURUSD": 45000}))
    assert not ok and "position notional" in why
    ok, why = check_entry("GBP/USD.IDEALPRO", 20000, 1.3, state=st(open_keys=["FX:EURUSD", "FX:USDJPY"]))
    assert not ok and "max open positions" in why
    assert check_entry("EUR/USD.IDEALPRO", 20000, 1.1, state=st(open_keys=["FX:EURUSD", "FX:USDJPY"]))[0]   # adding to an already-open instrument is allowed
    ok, why = check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=st(halted=True, halt_reason="kill switch"))
    assert not ok and "halted" in why
    ok, why = check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=st(daily_pnl_usd=-1000))
    assert not ok and "daily loss" in why


def test_require_sync_and_kill():
    s = RiskState(require_sync=True)
    assert check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=s)[1] == "waiting for risk limits from API"
    s.update(LIM)
    assert check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=s)[0]
    s.killed = True
    assert "kill switch" in check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=s)[1]


def test_backtest_without_guard_is_unlimited():
    assert run_backtest(CFG, bars=_sine_bars(), log_level="OFF")["signals"] >= 20 and STATE.limits is None


class FakeSource:
    """Return normal limits until the halt_at tick, then switch to halted · kill_at = send the kill command"""
    def __init__(self, halt_at=None, kill_at=None, resume_at=None):
        self.n, self.halt_at, self.kill_at, self.resume_at = 0, halt_at, kill_at, resume_at
        self.cur = dict(LIM, max_order_notional_usd=1e9, max_position_notional_usd=1e9, max_open_positions=50, max_daily_loss_usd=0)

    def latest(self):
        self.n += 1
        if self.halt_at and self.n == self.halt_at:
            self.cur = {**self.cur, "halted": True, "halt_reason": "test halt"}
        if self.resume_at and self.n == self.resume_at:
            self.cur = {**self.cur, "halted": False, "halt_reason": ""}
        return self.cur

    def take_command(self):
        if self.kill_at and self.n == self.kill_at:
            self.kill_at = None
            return {"cmd": "kill", "reason": "test kill", "node": "*"}
        return None


def _run(source, cfg=CFG):
    pub = FakePub()
    from qx_strategies.events import QXEventBridge, QXEventBridgeConfig
    bridge = QXEventBridge(QXEventBridgeConfig(node_id="t", heartbeat_secs=3600), publisher=pub)
    guard = QXRiskGuard(QXRiskGuardConfig(node_id="t", tick_secs=300), source=source)
    res = run_backtest(cfg, bars=_sine_bars(), log_level="OFF", actors=[bridge, guard])
    return res, pub.events


def _opened_after(events, ts_ns):
    return [e for e in events if e["type"] == "position" and e["data"]["event"] == "Opened" and e["ts_ns"] > ts_ns]


def test_halt_blocks_new_entries_and_reduces():
    res, ev = _run(FakeSource(halt_at=700))
    halt = next(e for e in ev if e["type"] == "risk" and e["data"]["event"] == "halt")
    states = [e["data"]["state"] for e in ev if e["type"] == "risk" and e["data"]["event"] == "trading_state"]
    assert states and states[-1] in ("REDUCING", "HALTED")
    assert _opened_after(ev, halt["ts_ns"] + 10 * 60 * 1_000_000_000) == []     # no new positions after halt
    assert any(e["type"] == "risk" and e["data"]["event"] == "blocked" for e in ev)
    assert res["signals"] >= 20


def test_kill_flattens_and_halts():
    res, ev = _run(FakeSource(kill_at=900))
    kill = next(e for e in ev if e["type"] == "risk" and e["data"]["event"] == "kill")
    after = [e for e in ev if e["ts_ns"] >= kill["ts_ns"]]
    assert _opened_after(ev, kill["ts_ns"]) == []
    assert any(e["type"] == "position" and e["data"]["event"] == "Closed" for e in after) or kill["data"]["positions_closing"] == 0
    assert res["open_positions"] == 0 and STATE.killed


def test_resume_after_halt():
    res, ev = _run(FakeSource(halt_at=500, resume_at=1200))
    resume = next(e for e in ev if e["type"] == "risk" and e["data"]["event"] == "resume")
    assert _opened_after(ev, resume["ts_ns"])                                   # can open positions again
    assert [e["data"]["state"] for e in ev if e["type"] == "risk" and e["data"]["event"] == "trading_state"][-1] == "ACTIVE"


def test_api_daily_loss_monitor_halts(monkeypatch):
    import api.server as s
    saved, audits = {}, []
    monkeypatch.setattr(s, "load_risk", lambda account=None: {"halted": False, "halt_reason": ""})
    monkeypatch.setattr(s, "save_risk", lambda d, account=None: saved.update(d))
    monkeypatch.setattr(s, "audit", lambda ev, **k: audits.append(ev))
    monkeypatch.setattr(s.notify, "fire", lambda *a, **k: None)
    p = {"halted": False, "max_daily_loss_usd": 1000.0, "daily_pnl_usd": -1200.0}
    assert s._halt_for_daily_loss(p) and p["halted"] and saved["halted"] and "daily loss" in saved["halt_reason"] and audits == ["halt"]
    assert not s._halt_for_daily_loss({"halted": False, "max_daily_loss_usd": 1000.0, "daily_pnl_usd": -500.0})
    assert not s._halt_for_daily_loss({"halted": False, "max_daily_loss_usd": 0, "daily_pnl_usd": -5000.0})


def test_redis_risk_source_end_to_end(monkeypatch):
    import json
    import os
    import uuid

    import pytest
    redis = pytest.importorskip("redis")
    url = os.getenv("REDIS_URL_TEST", "redis://127.0.0.1:6379/15")
    try:
        r = redis.Redis.from_url(url, socket_connect_timeout=0.3)
        r.ping()
    except Exception:
        pytest.skip("Redis not running")
    from qx_strategies import risk_guard as rg
    # Redis pub/sub channels are shared by every database number: never publish on the live qx:control channel
    # (a kill there reaches the running nodes) and never touch the live qx:risk key
    tag = uuid.uuid4().hex[:8]
    monkeypatch.setattr(rg, "CONTROL_CHANNEL", f"qx:test:control:{tag}")
    monkeypatch.setattr(rg, "RISK_KEY", f"qx:test:risk:{tag}")
    r.set(rg.RISK_KEY, json.dumps(LIM), ex=30)
    src = rg.RedisRiskSource(url, poll_secs=0.2)
    t0 = time.time()
    while src.latest() is None and time.time() - t0 < 3:
        time.sleep(0.05)
    assert src.latest()["max_open_positions"] == 2
    time.sleep(0.3)                                     # let the subscribe finish before publishing
    r.publish(rg.CONTROL_CHANNEL, json.dumps({"cmd": "kill", "node": "*", "reason": "t"}))
    cmd = None
    while cmd is None and time.time() - t0 < 5:
        cmd = src.take_command()
        time.sleep(0.05)
    src.close()
    r.delete(rg.RISK_KEY)
    assert cmd and cmd["cmd"] == "kill"
