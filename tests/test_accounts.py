import pathlib

import api.history as h
import api.operators as ops


def test_history_scoped_by_account(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "DB_FILE", tmp_path / "h.db")
    rows = [
        {"exec_id": "1", "ts": "2026-10-20T10:00:00+00:00", "key": "FX:EURUSD", "symbol": "EURUSD", "side": "BUY", "qty": 1000, "price": 1.1, "commission": 1, "realized_pnl": 0, "order_id": 1, "order_ref": "manual", "account": "A"},
        {"exec_id": "2", "ts": "2026-10-20T10:00:00+00:00", "key": "FX:EURUSD", "symbol": "EURUSD", "side": "SELL", "qty": 1000, "price": 1.1, "commission": 1, "realized_pnl": 0, "order_id": 2, "order_ref": "manual", "account": "B"},
    ]
    h.upsert_fills(rows)
    assert len(h.list_trades()) == 2                       # different accounts → separate trades (not netted)
    assert [t["account"] for t in h.list_trades(account="A")] == ["A"]
    assert h.stats(account="B")["open_trades"] == 1


def test_operator_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "OPS_FILE", tmp_path / "ops.json")
    ops.create("Risk Desk", "risk", "9999")
    tok, sess = ops.login("risk_desk", "9999")
    assert ops.resolve(tok)["role"] == "risk"
    assert ops.allowed("risk", "PUT", "/risk") and not ops.allowed("risk", "POST", "/order")
    assert ops.allowed("viewer", "GET", "/positions") and not ops.allowed("viewer", "POST", "/positions/close_all")
    assert ops.allowed("trader", "POST", "/risk/kill") and not ops.allowed("trader", "POST", "/setup/login")
