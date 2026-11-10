"""No operators configured (personal machine): a request from the MCP server is still a limited assistant, never the
local admin. Read-only default, paper only, assistant limits and Ask first all apply. The Terminal and CLI stay admin."""
import pytest
from fastapi.testclient import TestClient

from api import operators as ops

MCP = {"X-QOS-Client": "mcp", "X-Source": "mcp"}
BUY = {"key": "FX:EURUSD", "side": "BUY", "qty": 20000}


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "OPS_FILE", tmp_path / "operators.json")
    monkeypatch.setattr(ops, "DATA_DIR", tmp_path)
    monkeypatch.delenv("QOS_PUBLIC_TERMINAL", raising=False)
    ops._tokens.clear()
    from api import server as S
    for name, f in (("RISK_FILE", "risk.json"), ("AUDIT_FILE", "audit.jsonl"), ("MODE_FILE", "mode.json")):
        monkeypatch.setattr(S, name, tmp_path / f)
    state = {"account": "DU1", "pos": 0.0, "sent": [], "events": []}

    class Tick:
        bid, ask, last, close = 1.1199, 1.12, 1.12, 1.12

    async def ok(*a, **k):
        return None

    async def ticker(key):
        return Tick()

    async def usd(mkt, sym, ref):
        return ref

    async def submit(ref, key, side, qty, *a, **k):
        state["sent"].append((ref, key, side, qty))
        return {"order_id": 7, "status": "Filled"}

    async def bcast(msg):
        state["events"].append(msg)
    monkeypatch.setattr(S, "active_account", lambda: state["account"])
    monkeypatch.setattr(S, "load_mode", lambda: "manual")
    monkeypatch.setattr(S, "ensure_connected", ok)
    monkeypatch.setattr(S, "risk_gate", ok)
    monkeypatch.setattr(S, "ticker_for", ticker)
    monkeypatch.setattr(S, "_usd_per_unit", usd)
    monkeypatch.setattr(S, "market_tradable", lambda m: True)
    monkeypatch.setattr(S, "_check_position", lambda *a, **k: None)
    monkeypatch.setattr(S, "position_qty", lambda k: state["pos"])
    monkeypatch.setattr(S, "_working_qty", lambda k: (0.0, 0.0))
    monkeypatch.setattr(S, "submit_order", submit)
    monkeypatch.setattr(S, "ws_broadcast", bcast)
    monkeypatch.setattr(S.notify, "fire", lambda *a, **k: None)
    S._tickets.clear(); S._ticket_done.clear(); S._assistant_orders.clear()
    assert not ops.configured()
    return S, TestClient(S.app), state


def test_mcp_is_read_only_by_default_without_operators(legacy):
    S, c, st = legacy
    r = c.post("/order/prepare", json=BUY, headers=MCP)
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert c.post("/order/confirm", json={"token": "x"}, headers=MCP).status_code == 403
    assert not S._tickets and not st["sent"]


def test_mcp_can_still_read_without_operators(legacy):
    S, c, st = legacy
    assert c.get("/mode", headers=MCP).json() == {"mode": "manual"}
    assert c.get("/assistant", headers=MCP).json()["write_enabled"] is False


def test_mcp_is_refused_on_a_live_account_without_operators(legacy):
    S, c, st = legacy
    S.save_assistant({"write_enabled": True})
    st["account"] = "U1234567"
    r = c.post("/order/prepare", json=BUY, headers=MCP)
    assert r.status_code == 403 and "paper accounts" in r.json()["detail"] and not S._tickets


def test_mcp_hits_the_assistant_limits_without_operators(legacy):
    S, c, st = legacy
    S.save_assistant({"write_enabled": True, "max_order_notional_usd": 10000, "max_orders_per_hour": 2})
    r = c.post("/order/prepare", json=BUY, headers=MCP)                                   # 22,400 USD > 10,000
    assert r.status_code == 400 and "order notional" in r.json()["detail"]
    r = c.post("/order/prepare", json={**BUY, "side": "SELL", "qty": 5000}, headers=MCP)   # flat → would be short
    assert r.status_code == 400 and "short positions" in r.json()["detail"]
    for _ in range(2):
        assert c.post("/order/prepare", json={**BUY, "qty": 5000}, headers=MCP).status_code == 200
    r = c.post("/order/prepare", json={**BUY, "qty": 5000}, headers=MCP)                   # third within the hour
    assert r.status_code == 429 and "orders in the last hour" in r.json()["detail"]
    assert not st["sent"]


def test_mcp_cannot_use_admin_routes_without_operators(legacy):
    S, c, st = legacy
    S.save_assistant({"write_enabled": True})
    assert c.put("/assistant", json={"auto_confirm": True}, headers=MCP).status_code == 403      # cannot lift its own limits
    assert c.post("/order", json=BUY, headers=MCP).status_code == 403                              # no direct submit
    assert c.post("/risk/kill", headers=MCP).status_code == 403
    assert S.load_assistant()["auto_confirm"] is False


def test_mcp_orders_wait_for_approval_without_operators(legacy):
    S, c, st = legacy
    S.save_assistant({"write_enabled": True})
    t = c.post("/order/prepare", json=BUY, headers=MCP).json()
    r = c.post("/order/confirm", json={"token": t["token"]}, headers=MCP).json()
    assert r["status"] == "awaiting_approval" and not st["sent"]                          # Ask first: held, not sent
    assert c.post(f"/assistant/tickets/{t['token']}/approve", headers=MCP).status_code == 403     # the AI cannot approve itself
    r = c.post(f"/assistant/tickets/{t['token']}/approve")                                # the local admin in the Terminal can
    assert r.status_code == 200 and st["sent"] == [("assistant", "FX:EURUSD", "BUY", 20000.0)]


def test_old_mcp_server_is_recognised_by_its_source(legacy):
    S, c, st = legacy
    r = c.post("/order/prepare", json=BUY, headers={"X-Source": "mcp:claude-desktop"})    # MCP server from before the marker header
    assert r.status_code == 403 and "read-only" in r.json()["detail"]


def test_terminal_and_cli_stay_local_admin_without_operators(legacy):
    S, c, st = legacy
    assert c.put("/assistant", json={"write_enabled": False}).status_code == 200          # admin-only route, AI still Read-only
    t = c.post("/order/prepare", json=BUY, headers={"X-Source": "terminal"})
    assert t.status_code == 200 and "assistant limits ok" not in t.json().get("checks", [])
    r = c.post("/order/confirm", json={"token": t.json()["token"]}, headers={"X-Source": "terminal"})
    assert r.status_code == 200 and r.json()["status"] == "Filled" and st["sent"] == [("manual", "FX:EURUSD", "BUY", 20000.0)]


def test_mcp_server_always_sends_the_marker(monkeypatch):
    import mcp_server
    h = mcp_server.Api()._h()
    assert h["X-QOS-Client"] == "mcp" and h["X-Operator-Token"] == ""
    monkeypatch.setattr(mcp_server, "SOURCE", "renamed")
    assert mcp_server.Api()._h("grok")["X-QOS-Client"] == "mcp"                           # independent of MCP_SOURCE
