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
