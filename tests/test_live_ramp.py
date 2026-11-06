"""Step 14: live readiness + ramp-up"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from api import ramp

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)          # Monday
OK = {"net_usd": 50.0, "max_dd_usd": 20.0, "trades": 5, "crashes": 0, "rejects": 0, "reconcile": 0, "match": 0.97}


def test_sizes_and_days():
    r = ramp.new_ramp(now=T0)
    assert ramp.multiplier(r) == 0.1 and ramp.scaled_size("20000", 0.1) == "2000" and ramp.scaled_size("20000", 0.25) == "5000"
    with pytest.raises(ValueError, match="below one lot"):                   # QA fix: 300 < 1 lot → refuse (1 lot would be 33% at the 10% stage)
        ramp.scaled_size("3000", 0.1)
    assert ramp.scaled_size("15000", 0.1) == "1000"                          # rounds down, never above the stage size
    assert ramp.multiplier(None) == 0.1 and ramp.stage_cap(r) == 100.0
    assert ramp.business_days(T0.isoformat(), T0 + timedelta(days=4)) == 4      # Tue–Fri
    assert ramp.business_days(T0.isoformat(), T0 + timedelta(days=6)) == 4      # skips the weekend
    with pytest.raises(ValueError):
        ramp.clean_config({"stages": [0.5, 0.25, 1.0]})
    with pytest.raises(ValueError):
        ramp.clean_config({"stages": [0.1, 0.5]})


def test_evaluate_decisions():
    r = ramp.new_ramp(now=T0)
    early = T0 + timedelta(days=1)
    assert ramp.evaluate(r, OK, early)["action"] == "hold"                     # fewer than 3 business days yet
    later = T0 + timedelta(days=3)
    assert ramp.evaluate(r, OK, later)["action"] == "advance"
    assert ramp.evaluate(r, {**OK, "trades": 1}, later)["action"] == "hold"
    assert ramp.evaluate(r, {**OK, "match": 0.5}, later)["action"] == "hold"
    assert ramp.evaluate(r, {**OK, "match": None}, later)["action"] == "advance"
    assert ramp.evaluate(r, {**OK, "max_dd_usd": 150.0}, later)["action"] == "pause"   # first stage DD exceeded = pause
    assert ramp.evaluate(r, {**OK, "crashes": 1}, early)["action"] == "pause"
    assert ramp.evaluate(r, {**OK, "rejects": 2}, early)["action"] == "pause"
    ramp.apply(r, "advance", now=later)
    assert r["stage"] == 1 and ramp.stage_cap(r) == 250.0
    assert ramp.evaluate(r, {**OK, "net_usd": -300.0}, later + timedelta(days=5))["action"] == "demote"
    for _ in range(5):
        ramp.apply(r, "advance", now=later)
    assert r["stage"] == 3 and ramp.evaluate(r, OK, later + timedelta(days=9))["action"] == "complete"


def test_apply_history_and_resume():
    r = ramp.new_ramp(now=T0)
    assert ramp.apply(r, "advance", by="op1", why="ok", now=T0) is True
    assert ramp.apply(r, "pause", why="crash", now=T0) is False and r["status"] == "paused"
    ramp.apply(r, "resume", by="admin", now=T0 + timedelta(hours=1))
    assert r["status"] == "active" and r["stage_started"] == (T0 + timedelta(hours=1)).isoformat(timespec="seconds")
    assert [h["action"] for h in r["history"]] == ["start", "advance", "pause", "resume"]
    assert ramp.apply(r, "demote", now=T0) and r["stage"] == 0
    assert ramp.apply(r, "demote", now=T0) is False


# ---------- server ----------
@pytest.fixture()
def srv(tmp_path, monkeypatch):
    from api import server
    monkeypatch.setattr(server.nm, "nodes", {"n1": {"id": "n1", "config_file": "x.json", "allow_live": True,
                                                    "ramp": ramp.new_ramp(now=T0)}})
    monkeypatch.setattr(server.nm, "save", lambda: None)
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server.notify, "fire", lambda *a, **k: None)
    monkeypatch.setattr(server, "_node_ref", lambda nid: "demo@1.0.0")
    monkeypatch.setattr(server.sst, "state_of", lambda ref: "soak_go")
    return server


def _ready_env(server, monkeypatch, **over):
    risk = {"allow_live": True, "max_position_notional_usd": 100000, "halted": False}
    monkeypatch.setattr(server, "load_risk", lambda account=None: dict(risk))
    monkeypatch.setattr(server, "active_account", lambda: "U1234567")
    monkeypatch.setattr(server, "_risk_doc", lambda: {"default": {}, "accounts": {"U1234567": {"max_daily_loss_usd": over.get("daily", 500),
                                                                                               "portfolio": {"max_dd_pct": 5}}}})
    monkeypatch.setattr(server.ops, "live_code_set", lambda: over.get("code", True))
    monkeypatch.setattr(server.nb, "resolve_gateway", lambda: {"mode": "live"})


def test_live_start_needs_readiness(srv, monkeypatch):
    _ready_env(srv, monkeypatch, daily=0)
    ok, why = srv._node_can_start("n1")
    assert not ok and "daily loss limit" in why
    _ready_env(srv, monkeypatch, code=False)
    ok, why = srv._node_can_start("n1")
    assert not ok and "live confirmation code" in why
    _ready_env(srv, monkeypatch)
    assert srv._node_can_start("n1") == (True, "")
    srv.nm.nodes["n1"]["ramp"]["status"] = "paused"
    ok, why = srv._node_can_start("n1")
    assert not ok and "paused" in why
    r = asyncio.run(srv.live_readiness(node="n1"))
    assert not r["ready"] and r["blocked"] == ["ramp_active"]


def test_allow_live_needs_code_and_starts_ramp_small(srv, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(srv.sst, "refresh", lambda facts: {"demo@1.0.0": {"state": "soak_go"}})
    monkeypatch.setattr(srv, "_package_facts", lambda using=None: {})
    monkeypatch.setattr(srv, "load_risk", lambda account=None: {"allow_live": True})
    monkeypatch.setattr(srv.ops, "live_code_set", lambda: True)
    monkeypatch.setattr(srv.ops, "check_live_code", lambda c: c == "2468")
    srv.nm.nodes["n1"] = {"id": "n1", "config_file": "x.json"}
    with pytest.raises(HTTPException) as e:
        asyncio.run(srv.nodes_allow_live("n1", srv.AllowLive(enabled=True, live_code="0000")))
    assert e.value.status_code == 403 and not srv.nm.nodes["n1"].get("allow_live")
    asyncio.run(srv.nodes_allow_live("n1", srv.AllowLive(enabled=True, live_code="2468")))
    assert srv.nm.nodes["n1"]["allow_live"] and srv.nm.nodes["n1"]["ramp"]["stage"] == 0


def test_spawn_scales_size_only_on_live(tmp_path, monkeypatch):
    from api import nautilus_bridge as nb
    from api import node_manager as nmod
    from test_node_manager import CFG, sleeper
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ema.json").write_text(json.dumps(CFG))
    for k, v in (("NODES_FILE", tmp_path / "nodes.json"), ("PID_DIR", tmp_path / "pids"), ("CONFIG_DIR", tmp_path / "configs")):
        monkeypatch.setattr(nmod, k, v)
    monkeypatch.setattr(nb, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(nb, "CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(tmp_path / "none"))
    seen = {}

    def launch(nid, cfg, log):
        seen[nid] = cfg["trade_size"]
        return sleeper()(nid, cfg, log)
    m = nmod.NodeManager(launcher=launch)
    m.size_multiplier = lambda nid: 0.25
    m.create("pp", "ema.json"), m.create("ll", "ema.json")
    monkeypatch.setattr(nb, "resolve_gateway", lambda: {"host": "h", "port": 1, "gateway_id": "g", "mode": "paper", "account_id": "DU1"})
    m.start("pp")
    monkeypatch.setattr(nb, "resolve_gateway", lambda: {"host": "h", "port": 1, "gateway_id": "g", "mode": "live", "account_id": "U1"})
    st = m.start("ll")
    assert seen == {"pp": "20000", "ll": "5000"}
    assert st["live_size"] == {"multiplier": 0.25, "trade_size": "5000", "base_trade_size": "20000"}
    m.stop("pp"), m.stop("ll")


def _tick_env(srv, monkeypatch, metrics, running=True, flat=True):
    monkeypatch.setattr(srv.nb, "resolve_gateway", lambda: {"mode": "live"})
    monkeypatch.setattr(srv.nm, "_running", lambda nid: running)
    monkeypatch.setattr(srv.nm, "live", lambda nid: {} if flat else {"positions": [{"qty": 1}]})
    monkeypatch.setattr(srv.nm, "effective_config", lambda nid: {"trade_size": "20000"})
    monkeypatch.setattr(srv, "_ramp_metrics", lambda nid, since: dict(metrics))
    calls = []
    monkeypatch.setattr(srv, "_node_call", lambda fn, *a: calls.append((fn.__name__, a)))
    monkeypatch.setattr(srv.nm, "rt", {"n1": {}})
    return calls


def test_ramp_tick_steps_down_and_pauses(srv, monkeypatch):
    rp = srv.nm.nodes["n1"]["ramp"]
    rp["stage"] = 2
    calls = _tick_env(srv, monkeypatch, {**OK, "max_dd_usd": 900.0})
    acted = asyncio.run(srv.ramp_tick())
    assert acted[0]["action"] == "demote" and rp["stage"] == 1 and srv.nm.rt["n1"]["restart_when_flat"]
    monkeypatch.setattr(srv, "_ramp_metrics", lambda nid, since: dict(OK))
    asyncio.run(srv.ramp_tick())                                               # flat → restart with the new size
    assert calls[0][0] == "restart" and not srv.nm.rt["n1"]["restart_when_flat"]
    _tick_env(srv, monkeypatch, {**OK, "crashes": 1})
    calls = _tick_env(srv, monkeypatch, {**OK, "crashes": 1})
    asyncio.run(srv.ramp_tick())
    assert rp["status"] == "paused" and calls[0][0] == "stop"


def test_ramp_tick_waits_for_flat_before_restart(srv, monkeypatch):
    calls = _tick_env(srv, monkeypatch, OK, flat=False)
    srv.nm.rt["n1"]["restart_when_flat"] = True
    srv.nm.nodes["n1"]["ramp"]["stage_started"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    asyncio.run(srv.ramp_tick())
    assert not calls and srv.nm.rt["n1"]["restart_when_flat"]


def test_advance_needs_approval_unless_auto(srv, monkeypatch):
    from fastapi import HTTPException
    rp = srv.nm.nodes["n1"]["ramp"]
    rp["stage_started"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    _tick_env(srv, monkeypatch, OK)
    asyncio.run(srv.ramp_tick())
    assert rp["ready"] and rp["stage"] == 0
    r = asyncio.run(srv.node_ramp_action("n1", "advance", srv.RampAction()))
    assert r["changed"] and rp["stage"] == 1
    with pytest.raises(HTTPException) as e:                                    # new stage has not completed its days yet
        asyncio.run(srv.node_ramp_action("n1", "advance", srv.RampAction()))
    assert e.value.status_code == 409
    rp["config"]["auto_advance"] = True
    rp["stage_started"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    asyncio.run(srv.ramp_tick())
    assert rp["stage"] == 2


def test_resume_needs_live_code(srv, monkeypatch):
    from fastapi import HTTPException
    rp = srv.nm.nodes["n1"]["ramp"]
    ramp.apply(rp, "pause", why="crash")
    _tick_env(srv, monkeypatch, OK)
    monkeypatch.setattr(srv, "load_risk", lambda account=None: {"allow_live": True})
    monkeypatch.setattr(srv.ops, "live_code_set", lambda: True)
    monkeypatch.setattr(srv.ops, "check_live_code", lambda c: c == "2468")
    with pytest.raises(HTTPException):
        asyncio.run(srv.node_ramp_action("n1", "resume", srv.RampAction(live_code="1")))
    asyncio.run(srv.node_ramp_action("n1", "resume", srv.RampAction(live_code="2468")))
    assert rp["status"] == "active"


def test_ramp_metrics_from_local_history_and_soak(tmp_path, monkeypatch):
    import api.history as h
    import api.soak as soak
    from api import server
    h.DB_FILE = tmp_path / "h.db"
    h._node_cache.clear()
    monkeypatch.setattr(soak, "DB_FILE", tmp_path / "soak.db")
    monkeypatch.setattr(soak, "STATE_FILE", tmp_path / "soak.json")
    monkeypatch.setattr(server, "active_account", lambda: "U1")
    base = {"key": "FX:EURUSD", "symbol": "EURUSD", "qty": 2000, "commission": 0.0, "order_id": 1, "account": "U1", "node": "n1", "strategy": "S"}
    h.record_node_fill({**base, "exec_id": "1", "ts": "2026-11-03T01:00:00+00:00", "side": "BUY", "price": 1.10, "order_ref": "O-20261103-010000-1-1-1"})
    h.record_node_fill({**base, "exec_id": "2", "ts": "2026-11-03T02:00:00+00:00", "side": "SELL", "price": 1.099, "order_ref": "O-20261103-020000-1-1-2"})
    h.upsert_fills([{**base, "exec_id": "2", "ts": "2026-11-03T02:00:00+00:00", "side": "SELL", "price": 1.099, "realized_pnl": -2.0,
                     "commission": 0.5, "order_ref": "O-20261103-020000-1-1-2:2"}])
    soak.incident("n1", "crash", "exit 1", ts_ns=int(datetime(2026, 11, 3, 3, tzinfo=timezone.utc).timestamp() * 1e9))
    soak.incident("n1", "crash", "old", ts_ns=int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1e9))
    m = server._ramp_metrics("n1", "2026-11-02T00:00:00+00:00")
    assert m["trades"] == 1 and m["net_usd"] == -2.5 and m["max_dd_usd"] == 2.5 and m["crashes"] == 1 and m["match"] is None
