"""Step 2: ports per gateway profile + non-colliding clientIds"""
import json

import pytest

import api.gateways as gw
import api.nautilus_bridge as nb


@pytest.fixture()
def tmp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, "CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(nb, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(gw, "GW_FILE", tmp_path / "gateways.json")
    monkeypatch.setattr(gw, "IBGW_ROOT", tmp_path / "ibgw")
    monkeypatch.delenv("IB_PORT", raising=False)
    return tmp_path


def test_allocation_is_stable_and_unique(tmp_files):
    a = nb.allocate_client_id("ema_eurusd")
    b = nb.allocate_client_id("mr_gbpusd")
    assert a % 2 == 0 and b % 2 == 0 and a != b
    assert {a, a + 1}.isdisjoint({b, b + 1})
    assert nb.allocate_client_id("ema_eurusd") == a          # stable across restarts
    for x in (a, a + 1, b, b + 1):
        assert x not in nb._reserved() and 100 <= x <= 199


def test_requested_id_validation(tmp_files):
    with pytest.raises(ValueError):
        nb.allocate_client_id("x", 60)                         # collides with the API
    a = nb.allocate_client_id("first")
    with pytest.raises(ValueError):
        nb.allocate_client_id("second", a + 1)                 # collides with another node's exec id
    assert nb.allocate_client_id("second", 150) == 150


def test_port_follows_selected_gateway(tmp_files):
    paper = gw.upsert("demo_user", "pw", "paper")
    live = gw.upsert("demo_user", "pw", "live")
    (tmp_files / "session.json").write_text(json.dumps({"gateway_id": live["id"], "account_id": "U123"}))
    t = nb.resolve_gateway()
    assert t["port"] == live["api_port"] and t["mode"] == "live" and t["account_id"] == "U123"
    (tmp_files / "session.json").write_text(json.dumps({"gateway_id": paper["id"], "account_id": "DU1"}))
    assert nb.resolve_gateway()["port"] == paper["api_port"]


def test_env_port_override(tmp_files, monkeypatch):
    monkeypatch.setenv("IB_PORT", "4999")
    assert nb.resolve_gateway()["port"] == 4999
