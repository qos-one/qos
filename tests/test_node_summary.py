import asyncio

import api.history as h
import api.soak as soak


def test_node_summary_combines_live_history_research_and_soak(tmp_path, monkeypatch):
    from api import server
    h.DB_FILE = tmp_path / "h.db"
    h._node_cache.clear()
    monkeypatch.setattr(soak, "DB_FILE", tmp_path / "soak.db")
    monkeypatch.setattr(soak, "STATE_FILE", tmp_path / "soak.json")
    monkeypatch.setattr(server, "active_account", lambda: "DU1")
    monkeypatch.setattr(server.nm, "status", lambda nid: {"id": nid, "config_file": "ema.json", "state": "running", "live": {}})
    monkeypatch.setattr(server.lab_mod, "list_configs", lambda nodes: [{"file": "ema.json", "latest": {"parity": {"label": "match 100.0%", "score": 1.0}}}])
    base = {"key": "FX:EURUSD", "symbol": "EURUSD", "qty": 20000, "commission": 0.0, "order_id": 1, "account": "DU1", "node": "ema", "strategy": "S"}
    h.record_node_fill({**base, "exec_id": "1", "ts": "2026-10-27T01:00:00+00:00", "side": "BUY", "price": 1.10, "order_ref": "O-20261027-010000-1-1-1"})
    h.record_node_fill({**base, "exec_id": "2", "ts": "2026-10-27T02:00:00+00:00", "side": "SELL", "price": 1.101, "order_ref": "O-20261027-020000-1-1-2"})
    h.upsert_fills([{**base, "exec_id": "2", "ts": "2026-10-27T02:00:00+00:00", "side": "SELL", "price": 1.101, "realized_pnl": 20.0,
                     "commission": 2.0, "order_ref": "O-20261027-020000-1-1-2:2"}])
    r = asyncio.run(server.node_summary("ema"))
    assert r["account"] == "DU1" and r["stats"]["trades"] == 1 and r["stats"]["net_pnl"] == 18.0
    assert len(r["trades"]) == 1 and r["curve"][-1]["v"] == 18.0
    assert r["research"]["parity"]["score"] == 1.0 and r["soak"]["verdict"] == "PENDING"
    assert r["live_perf"]["metrics"]["net"] == 18.0 and len(r["live_perf"]["daily"]) == 1
    assert r["expected_perf"] is None
    con = soak.connect()
    con.execute("INSERT INTO parity VALUES (?,?,?)", ("ema", "2026-10-27T06:00:00+00:00",
                '{"from": "2026-10-27T00:00:00+00:00", "to": "2026-10-27T05:00:00+00:00", "backtest_trades": [["2026-10-27T02:00:00+00:00", 25.0]]}'))
    con.commit()
    con.close()
    e = asyncio.run(server.node_summary("ema"))["expected_perf"]
    assert e["metrics"]["net"] == 25.0 and e["from"].startswith("2026-10-27T00")
