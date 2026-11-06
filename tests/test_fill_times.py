from datetime import datetime, timezone

import api.history as h

ROW = {"commission": 1.0, "realized_pnl": 0.0, "account": "DU1", "symbol": "EURUSD", "key": "FX:EURUSD", "side": "BUY", "qty": 20000, "price": 1.1}
NOW = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)


def test_fix_fill_times_shifts_only_wrong_rows(tmp_path):
    h.DB_FILE = tmp_path / "h.db"
    h._node_cache.clear()
    h.upsert_fills([
        {**ROW, "exec_id": "agent1", "ts": "2026-10-26T22:20:06+00:00", "order_id": 5, "order_ref": "agent:ml_signal"},
        {**ROW, "exec_id": "ok1", "ts": "2026-10-26T17:55:06+00:00", "order_id": 6, "order_ref": "agent:ml_signal"},
        {**ROW, "exec_id": "naut", "ts": "2026-10-26T07:24:00+00:00", "order_id": 101, "order_ref": "O-20261026-072400-NAUTILUS-000-1:101"},
        {**ROW, "exec_id": "man", "ts": "2026-10-26T22:41:11+00:00", "order_id": 3760, "order_ref": "manual"},
        {**ROW, "exec_id": "future", "ts": "2026-10-27T05:30:00+00:00", "order_id": 9, "order_ref": "manual"},
    ])
    orders = {5: "2026-10-27T05:20:06+00:00", 6: "2026-10-26T17:55:05+00:00"}
    dry = h.fix_fill_times(orders, 7.0, now=NOW)
    assert dry["changed"] == 2 and not dry["applied"]
    res = h.fix_fill_times(orders, 7.0, apply=True, now=NOW)
    assert res["changed"] == 2
    con = h.connect()
    ts = dict(con.execute("SELECT exec_id, ts FROM fills").fetchall())
    con.close()
    assert ts["agent1"] == "2026-10-27T05:20:06+00:00" and ts["man"] == "2026-10-27T05:41:11+00:00"
    assert ts["ok1"].startswith("2026-10-26T17:55") and ts["naut"].startswith("2026-10-26T07:24") and ts["future"].startswith("2026-10-27T05:30")
    assert h.fix_fill_times(orders, 7.0, apply=True, now=NOW)["changed"] == 0


def test_api_reads_naive_ib_times_as_utc():
    from api import server
    assert server.ib.TimezoneTWS == "UTC"
