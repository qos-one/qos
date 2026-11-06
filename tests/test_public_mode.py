"""QOS_PUBLIC_TERMINAL=1 (Terminal served on a public domain): no legacy no-operator admin, no web bootstrap,
8-character PINs, a global login-failure limit and an operator token for the live WebSocket."""
import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import operators as ops


@pytest.fixture
def public(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "OPS_FILE", tmp_path / "operators.json")
    monkeypatch.setattr(ops, "DATA_DIR", tmp_path)
    monkeypatch.setenv("QOS_PUBLIC_TERMINAL", "1")
    ops._tokens.clear()
    ops._fail.clear()
    ops._fail_all.clear()
    from api import server as S
    monkeypatch.setattr(S, "AUDIT_FILE", tmp_path / "audit.jsonl")
    return S


def test_pin_length(public, monkeypatch):
    with pytest.raises(ValueError):
        ops.create("Alice", "admin", "1234567")
    assert ops.create("Alice", "admin", "12345678")["role"] == "admin"
    monkeypatch.delenv("QOS_PUBLIC_TERMINAL")
    assert ops.create("Bob", "viewer", "1234")["id"] == "bob"                 # personal machine: 4 is still fine


def test_global_failure_limit_stops_rotating_ids(public):
    ops.create("Alice", "admin", "correct-pin")
    for i in range(ops.FAIL_ALL_MAX):
        with pytest.raises(PermissionError):
            ops.login(f"guess{i}", "nope")
    with pytest.raises(PermissionError, match="too many"):
        ops.login("alice", "correct-pin")                                      # even the right PIN waits 10 minutes


def test_web_bootstrap_refused(public):
    with pytest.raises(HTTPException) as e:
        asyncio.run(public.operators_bootstrap(public.OpCreateBody(name="Mallory", pin="12345678")))
    assert e.value.status_code == 403 and not ops.configured()


def test_no_operator_means_no_access(public, monkeypatch):
    c = TestClient(public.app)
    r = c.get("/risk")
    assert r.status_code == 503 and "qos operator add" in r.json()["detail"]
    assert c.get("/operators/status").status_code == 200                       # sign-in page still loads


def test_personal_machine_keeps_legacy_mode(public, monkeypatch):
    monkeypatch.delenv("QOS_PUBLIC_TERMINAL")
    monkeypatch.setattr(public, "load_mode", lambda: "manual")
    assert TestClient(public.app).get("/mode").json() == {"mode": "manual"}       # no operators: local admin as before


def test_ws_needs_operator_in_public_mode(public, monkeypatch):
    ops.create("Alice", "admin", "correct-pin")
    c = TestClient(public.app)
    with pytest.raises(Exception):
        with c.websocket_connect("/ws") as ws:
            ws.receive_text()
    tok, _ = ops.login("alice", "correct-pin")
    with c.websocket_connect(f"/ws?op_token={tok}") as ws:
        assert '"conn"' in ws.receive_text()
    c.cookies.set(public.OP_COOKIE, tok)
    with c.websocket_connect("/ws") as ws:                                     # the web Terminal sends the cookie
        assert '"conn"' in ws.receive_text()


def test_cli_operator_add(public, monkeypatch, capsys):
    from qos import cli
    pins = iter(["long-enough-pin", "long-enough-pin"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(pins))
    assert cli.main(["operator", "add", "Ops Admin", "--role", "admin"]) == 0
    assert ops.configured() and "ops_admin" in capsys.readouterr().out
    cli.main(["operator", "list"])
    assert "ops_admin" in capsys.readouterr().out
