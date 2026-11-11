"""No operators configured (personal machine): admin is the local token file, not a request header.

A caller with no token, a spoofed X-Source or User-Agent, or an MCP marker is the assistant.
Read-only default, paper only, assistant limits and Ask first all apply. A valid local token
is the local admin, except on the MCP path, which stays the assistant even with that token.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from api import operators as ops
from qos.local_token import HEADER

MCP = {"X-QOS-Client": "mcp", "X-Source": "mcp"}
BUY = {"key": "FX:EURUSD", "side": "BUY", "qty": 20000}


def _admin_headers(**extra):
    from qos.local_token import read_token
    return {HEADER: read_token(), **extra}


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "OPS_FILE", tmp_path / "operators.json")
    monkeypatch.setattr(ops, "DATA_DIR", tmp_path)
    monkeypatch.delenv("QOS_PUBLIC_TERMINAL", raising=False)
    monkeypatch.setenv("QOS_LOCAL_TOKEN_FILE", str(tmp_path / "local_token"))
    from qos.local_token import ensure_local_token
    ensure_local_token()
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
    r = c.post(f"/assistant/tickets/{t['token']}/approve", headers=_admin_headers())            # the local token can
    assert r.status_code == 200 and st["sent"] == [("assistant", "FX:EURUSD", "BUY", 20000.0)]


def test_old_mcp_server_is_recognised_by_its_source(legacy):
    S, c, st = legacy
    r = c.post("/order/prepare", json=BUY, headers={"X-Source": "mcp:claude-desktop"})    # MCP server from before the marker header
    assert r.status_code == 403 and "read-only" in r.json()["detail"]


def _refused(r):
    return r.status_code == 403 and "read-only" in r.json()["detail"]


def test_omitted_identifying_header_is_not_admin(legacy):
    S, c, st = legacy
    assert _refused(c.post("/order/prepare", json=BUY))                                          # no X-QOS-Client, no X-Source
    assert _refused(c.post("/order/prepare", json=BUY, headers={"User-Agent": "python-httpx/0.28.1"}))
    assert c.post("/order", json=BUY).status_code == 403                                          # no direct submit
    assert not S._tickets and not st["sent"]


def test_legacy_remote_ai_source_is_the_assistant(legacy):
    S, c, st = legacy
    for headers in ({"X-Source": "remote-ai"}, {"X-Source": "remote-ai:grok"}, {"X-Source": "remote_ai"}):
        assert _refused(c.post("/order/prepare", json=BUY, headers=headers))
    # a local-looking source or client value does not raise privileges for the MCP client
    assert _refused(c.post("/order/prepare", json=BUY, headers={"X-Source": "terminal", "User-Agent": "python-httpx/0.28.1"}))
    assert _refused(c.post("/order/prepare", json=BUY, headers={"X-Source": "terminal", "X-Request-Id": "abc"}))
    assert _refused(c.post("/order/prepare", json=BUY, headers={"X-QOS-Client": "admin", "X-Source": "terminal"}))
    assert not st["sent"]


def test_ai_callers_still_need_approval_when_writes_are_on(legacy):
    S, c, st = legacy
    S.save_assistant({"write_enabled": True})
    t = c.post("/order/prepare", json=BUY, headers={"X-Source": "remote-ai"}).json()
    r = c.post("/order/confirm", json={"token": t["token"]}, headers={"X-Source": "remote-ai"}).json()
    assert r["status"] == "awaiting_approval" and not st["sent"]
    assert c.post("/order", json=BUY).status_code == 403                                          # omitted marker: no direct submit
    assert not st["sent"]


def test_normal_ai_header_stays_the_assistant(legacy):
    S, c, st = legacy
    assert _refused(c.post("/order/prepare", json=BUY, headers=MCP))
    assert _refused(c.post("/order/prepare", json=BUY, headers={"X-QOS-Client": "mcp", "X-Source": "remote-ai"}))
    assert not st["sent"]


def test_valid_local_token_is_admin_without_operators(legacy):
    S, c, st = legacy
    assert c.put("/assistant", json={"write_enabled": True}, headers=_admin_headers()).status_code == 200
    assert S.load_assistant()["write_enabled"] is True                                  # switch AI mode
    cli = _admin_headers(**{"X-Source": "qos-cli"})
    assert c.put("/assistant", json={"write_enabled": False}, headers=cli).status_code == 200
    t = c.post("/order/prepare", json=BUY, headers=_admin_headers())
    assert t.status_code == 200 and "assistant limits ok" not in t.json().get("checks", [])
    r = c.post("/order/confirm", json={"token": t.json()["token"]}, headers=_admin_headers())
    assert r.status_code == 200 and r.json()["status"] == "Filled" and st["sent"] == [("manual", "FX:EURUSD", "BUY", 20000.0)]
    assert c.post("/order", json=BUY, headers=_admin_headers()).status_code == 200


def test_no_token_or_spoofed_headers_are_the_assistant(legacy):
    S, c, st = legacy
    from api.server import _legacy_operator
    spoofed = (
        [(b"accept", b"*/*")],
        [(b"x-source", b"terminal")],
        [(b"x-source", b"qos-cli")],
        [(b"x-source", b"reset_node_books")],
        [(b"user-agent", b"reqwest/0.12.28")],
        [(b"user-agent", b"reqwest/0.12.9"), (b"x-source", b"terminal")],
        [(b"user-agent", b"python-httpx/0.28.1"), (b"x-source", b"terminal")],
    )
    for raw in spoofed:
        assert _legacy_operator(Headers(raw=raw))["role"] == "assistant"
        headers = {k.decode(): v.decode() for k, v in raw}
        assert _refused(c.post("/order/prepare", json=BUY, headers=headers))
    assert c.post("/order", json=BUY, headers={"User-Agent": "reqwest/0.12.28", "X-Source": "terminal"}).status_code == 403
    wrong = _admin_headers()
    wrong[HEADER] = wrong[HEADER][:8]
    assert _refused(c.post("/order/prepare", json=BUY, headers=wrong))
    assert not st["sent"]


def test_mcp_path_is_never_admin_even_with_the_local_token(legacy):
    S, c, st = legacy
    from api.server import _legacy_operator
    tok = _admin_headers()[HEADER].encode()
    marked = (
        [(b"x-qos-client", b"mcp"), (b"x-source", b"mcp"), (b"x-qos-local-token", tok)],
        [(b"x-source", b"mcp:claude-desktop"), (b"x-qos-local-token", tok)],
        [(b"x-source", b"remote-ai"), (b"x-qos-local-token", tok)],
        [(b"x-source", b"remote_ai:grok"), (b"x-qos-local-token", tok)],
        [(b"user-agent", b"reqwest/0.12.28"), (b"x-qos-client", b"mcp"), (b"x-qos-local-token", tok)],
    )
    for raw in marked:
        assert _legacy_operator(Headers(raw=raw))["role"] == "assistant"
    both = {**MCP, **_admin_headers()}
    assert c.put("/assistant", json={"write_enabled": True}, headers=both).status_code == 403
    assert c.post("/order", json=BUY, headers=both).status_code == 403
    assert _refused(c.post("/order/prepare", json=BUY, headers=both))
    assert S.load_assistant()["write_enabled"] is False and not st["sent"]


def test_bootstrap_requires_the_local_token(legacy):
    S, c, st = legacy
    body = {"name": "Ada", "pin": "1234"}
    assert c.post("/operators/bootstrap", json=body).status_code == 403
    for headers in (
        {"X-Source": "terminal"},
        {"X-Source": "qos-cli"},
        {"User-Agent": "reqwest/0.12.28"},
        {"User-Agent": "reqwest/0.12.28", "X-Source": "terminal"},
    ):
        assert c.post("/operators/bootstrap", json=body, headers=headers).status_code == 403
    assert c.post("/operators/bootstrap", json=body, headers={**MCP, **_admin_headers()}).status_code == 403
    S.save_assistant({"write_enabled": True})   # the refusal is the token check, not the read-only default
    assert c.post("/operators/bootstrap", json=body, headers={"X-Source": "terminal", "User-Agent": "reqwest/0.12.28"}).status_code == 403
    assert not ops.configured() and not st["sent"]
    # other public routes do not mint an admin session or change AI mode / operators
    assert c.post("/operators/login", json={"id": "ada", "pin": "1234"}).status_code == 401
    assert c.post("/operators/logout").status_code == 200 and not ops.configured()
    assert c.put("/assistant", json={"write_enabled": True}).status_code == 403
    assert c.put("/security/live_code", json={"code": "123456"}).status_code == 403
    assert c.post("/operators", json={"name": "Eve", "role": "admin", "pin": "1234"}).status_code == 403
    assert not ops.configured()
    r = c.post("/operators/bootstrap", json=body, headers=_admin_headers())
    assert r.status_code == 200 and r.json()["operator"]["role"] == "admin"
    session = {"X-Operator-Token": r.json()["token"]}
    assert c.post("/order", json=BUY, headers=session).status_code == 200
    assert st["sent"] == [("manual", "FX:EURUSD", "BUY", 20000.0)]


def test_local_token_does_not_bypass_a_configured_operator(legacy):
    S, c, st = legacy
    ops.create("Ann", "admin", "1234")
    assert c.put("/assistant", json={"write_enabled": True}, headers=_admin_headers()).status_code == 401
    assert S.load_assistant()["write_enabled"] is False


# Captured from reqwest 0.12.28 built the same way as qos-terminal src/api.rs client()/send()
# (Client::builder().default_headers(empty x-operator-token).timeout(15s), then .json()).
# The request carried accept, content-type and an empty x-operator-token. No User-Agent, no X-Source.
TERMINAL_WIRE = [
    (b"content-type", b"application/json"),
    (b"accept", b"*/*"),
    (b"x-operator-token", b""),
]


def test_real_terminal_wire_headers_need_the_local_token(legacy):
    S, c, st = legacy
    from api.server import _legacy_operator
    assert _legacy_operator(Headers(raw=TERMINAL_WIRE))["role"] == "assistant"
    as_http = {k.decode(): v.decode() for k, v in TERMINAL_WIRE}
    assert _refused(c.post("/order/prepare", json=BUY, headers=as_http))
    assert c.put("/assistant", json={"write_enabled": True}, headers=as_http).status_code == 403
    tok = _admin_headers()[HEADER].encode()
    authed = Headers(raw=[*TERMINAL_WIRE, (b"x-qos-local-token", tok)])
    assert _legacy_operator(authed)["role"] == "admin"
    sent = {**as_http, HEADER: tok.decode()}
    assert c.put("/assistant", json={"write_enabled": True}, headers=sent).status_code == 200
    t = c.post("/order/prepare", json=BUY, headers=sent)
    assert t.status_code == 200 and "assistant limits ok" not in t.json().get("checks", [])
    r = c.post("/order/confirm", json={"token": t.json()["token"]}, headers=sent)
    assert r.status_code == 200 and r.json()["status"] == "Filled"
    assert st["sent"] == [("manual", "FX:EURUSD", "BUY", 20000.0)]
    assert c.post("/assistant/tickets/nope/approve", headers=as_http).status_code == 403


def test_mcp_server_always_sends_the_marker(monkeypatch, tmp_path):
    import mcp_server
    from pathlib import Path
    monkeypatch.setenv("QOS_LOCAL_TOKEN_FILE", str(tmp_path / "local_token"))
    from qos.local_token import ensure_local_token
    ensure_local_token()
    h = mcp_server.Api()._h()
    assert h["X-QOS-Client"] == "mcp" and h["X-Operator-Token"] == ""
    assert HEADER not in h and all(k.lower() != HEADER.lower() for k in h)
    monkeypatch.setattr(mcp_server, "SOURCE", "renamed")
    sent = mcp_server.Api()._h("grok")
    assert sent["X-QOS-Client"] == "mcp" and HEADER not in sent                         # independent of MCP_SOURCE
    src = Path(mcp_server.__file__).read_text()
    assert "local_token" not in src and "QOS_LOCAL_TOKEN" not in src
    assert "x-qos-local-token" not in src.lower()
