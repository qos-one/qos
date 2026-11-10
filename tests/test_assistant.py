"""AI assistant (MCP) permission scope and prompt-injection guard: no confirm = no order"""
import api.operators as ops


def test_assistant_role_rules():
    assert ops.allowed("assistant", "GET", "/positions")
    assert not ops.allowed("assistant", "POST", "/order")                 # no direct submit; must prepare → confirm
    assert ops.allowed("assistant", "POST", "/order/prepare")
    assert ops.allowed("assistant", "POST", "/order/confirm")
    assert not ops.allowed("assistant", "POST", "/positions/close_all")
    assert not ops.allowed("assistant", "POST", "/positions/FX:EURUSD/reverse")
    assert ops.allowed("assistant", "POST", "/positions/FX:EURUSD/close")
    assert not ops.allowed("assistant", "PUT", "/risk")
    assert not ops.allowed("assistant", "POST", "/risk/kill")
    assert not ops.allowed("assistant", "POST", "/setup/login")
    assert not ops.allowed("assistant", "PUT", "/assistant")               # cannot change its own permissions
    assert not ops.allowed("assistant", "POST", "/agents/migrate")         # cutover is human-only
    assert not ops.allowed("assistant", "POST", "/nodes/ema_eurusd/start")


def test_prompt_injection_cannot_place_order_without_confirm():
    """Text in news/documents saying 'buy now' can at most prepare — an unconfirmed token never becomes an order"""
    from api import server
    server._tickets.clear()
    server._tickets["tok"] = {"req": {}, "key": "FX:EURUSD", "ref": 1.1, "notional": 1.0, "expires": 0, "operator": "assistant", "account": ""}
    # expired → confirm must reject and the token is discarded
    import asyncio
    from fastapi import HTTPException
    try:
        asyncio.run(server.confirm_order(server.ConfirmBody(token="tok")))
        assert False, "expired token must not be accepted"
    except HTTPException as e:
        assert e.status_code == 410
    assert "tok" not in server._tickets
    try:
        asyncio.run(server.confirm_order(server.ConfirmBody(token="tok")))
        assert False
    except HTTPException as e:
        assert e.status_code == 404                                        # single-use


# ---------- AI control: Ask first (operator approves) vs Auto ----------
import asyncio
import time

import pytest


@pytest.fixture
def asst(tmp_path, monkeypatch):
    from api import server as S
    for name, f in (("RISK_FILE", "risk.json"), ("AUDIT_FILE", "audit.jsonl"), ("MODE_FILE", "mode.json")):
        monkeypatch.setattr(S, name, tmp_path / f)
    monkeypatch.setattr(S, "active_account", lambda: "DU1")
    monkeypatch.setattr(S, "load_mode", lambda: "manual")
    sent, events = [], []

    async def submit(ref, key, side, qty, *a, **k):
        sent.append((ref, key, side, qty))
        return {"order_id": 7, "status": "Filled"}

    async def bcast(msg):
        events.append(msg)
    monkeypatch.setattr(S, "submit_order", submit)
    monkeypatch.setattr(S, "ws_broadcast", bcast)
    monkeypatch.setattr(S.notify, "fire", lambda *a, **k: None)
    S._tickets.clear(); S._ticket_done.clear(); S._assistant_orders.clear()
    S._t = type("T", (), {"sent": sent, "events": events})

    def ticket(token="tok", role="assistant"):
        S._tickets[token] = {"req": {"key": "FX:EURUSD", "side": "BUY", "qty": 20000.0, "order_type": "MKT", "price": None, "tif": "DAY",
                                     "take_profit": None, "stop_loss": None, "trail_amount": None, "cash_qty": None},
                             "key": "FX:EURUSD", "ref": 1.12, "notional": 22400.0, "expires": time.time() + 90,
                             "operator": "remote_ai", "role": role, "account": "DU1"}
    S._mk = ticket
    return S


def _as(S, role):
    return S.ops.current.set({"id": "x", "role": role})


def test_default_is_ask_first(asst):
    assert asst.load_assistant()["auto_confirm"] is False


def test_ask_first_holds_the_order_until_the_operator_approves(asst):
    S = asst
    S.save_assistant({"write_enabled": True})
    S._mk()
    tok = _as(S, "assistant")
    try:
        r = asyncio.run(S.confirm_order(S.ConfirmBody(token="tok")))
        assert r["status"] == "awaiting_approval" and not S._t.sent                      # nothing sent
        assert [e["type"] for e in S._t.events] == ["assistant_ticket"]
        r = asyncio.run(S.confirm_order(S.ConfirmBody(token="tok")))                     # asking again does not re-notify
        assert r["status"] == "awaiting_approval" and len(S._t.events) == 1
        assert [t["token"] for t in asyncio.run(S.assistant_tickets())["tickets"]] == ["tok"]
    finally:
        S.ops.current.reset(tok)
    tok = _as(S, "admin")
    try:
        res = asyncio.run(S.assistant_ticket_approve("tok"))
        assert res["status"] == "Filled" and S._t.sent == [("assistant", "FX:EURUSD", "BUY", 20000.0)]
        assert asyncio.run(S.assistant_tickets())["tickets"] == []
    finally:
        S.ops.current.reset(tok)
    tok = _as(S, "assistant")
    try:
        r = asyncio.run(S.confirm_order(S.ConfirmBody(token="tok")))                     # the AI learns the outcome
        assert r["approval"] == "approved" and r["status"] == "Filled" and r["order_id"] == 7 and len(S._t.sent) == 1
    finally:
        S.ops.current.reset(tok)


def test_reject_drops_the_order(asst):
    S = asst
    S.save_assistant({"write_enabled": True})
    S._mk()
    tok = _as(S, "assistant")
    try:
        asyncio.run(S.confirm_order(S.ConfirmBody(token="tok")))
    finally:
        S.ops.current.reset(tok)
    assert asyncio.run(S.assistant_ticket_reject("tok"))["status"] == "rejected"
    tok = _as(S, "assistant")
    try:
        assert asyncio.run(S.confirm_order(S.ConfirmBody(token="tok")))["status"] == "rejected" and not S._t.sent
    finally:
        S.ops.current.reset(tok)


def test_auto_sends_at_once(asst):
    S = asst
    S.save_assistant({"write_enabled": True, "auto_confirm": True})
    S._mk()
    tok = _as(S, "assistant")
    try:
        r = asyncio.run(S.confirm_order(S.ConfirmBody(token="tok")))
        assert r["status"] == "Filled" and S._t.sent == [("assistant", "FX:EURUSD", "BUY", 20000.0)]
    finally:
        S.ops.current.reset(tok)


def test_approval_expires_and_the_assistant_cannot_approve(asst):
    S = asst
    from fastapi import HTTPException
    assert not S.ops.allowed("assistant", "POST", "/assistant/tickets/tok/approve")
    assert not S.ops.allowed("trader", "POST", "/assistant/tickets/tok/approve") and S.ops.allowed("admin", "POST", "/assistant/tickets/tok/approve")
    S.save_assistant({"write_enabled": True})
    S._mk()
    tok = _as(S, "assistant")
    try:
        asyncio.run(S.confirm_order(S.ConfirmBody(token="tok")))
    finally:
        S.ops.current.reset(tok)
    S._tickets["tok"]["expires"] = time.time() - 1
    with pytest.raises(HTTPException) as e:
        asyncio.run(S.assistant_ticket_approve("tok"))
    assert e.value.status_code == 410 and not S._t.sent
