import api.history as h


def _fresh_history(tmp_path):
    h.DB_FILE = tmp_path / "h.db"   # use a temporary database; do not touch data/history.db
    return h


def test_trade_pairing_long_then_flat(tmp_path):
    h = _fresh_history(tmp_path)
    rows = [
        {"exec_id": "1", "ts": "2026-10-20T10:00:00+00:00", "key": "FX:EURUSD", "symbol": "EURUSD", "side": "BUY", "qty": 20000, "price": 1.1000, "commission": 2.0, "realized_pnl": 0.0, "order_id": 1, "order_ref": "manual"},
        {"exec_id": "2", "ts": "2026-10-20T11:00:00+00:00", "key": "FX:EURUSD", "symbol": "EURUSD", "side": "SELL", "qty": 20000, "price": 1.1010, "commission": 2.0, "realized_pnl": 20.0, "order_id": 2, "order_ref": "manual"},
    ]
    assert h.upsert_fills(rows) == 2
    trades = h.list_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "LONG" and t["open"] == 0
    assert abs(t["net_pnl"] - 16.0) < 1e-9          # 20 realized − 4 commission
    assert t["holding_sec"] == 3600


def test_open_trade_and_stats(tmp_path):
    h = _fresh_history(tmp_path)
    h.upsert_fills([{"exec_id": "x", "ts": "2026-10-20T10:00:00+00:00", "key": "STK:AAPL", "symbol": "AAPL", "side": "SELL", "qty": 10, "price": 200.0, "commission": 1.0, "realized_pnl": 0.0, "order_id": 9, "order_ref": "agent:ema_cross"}])
    t = h.list_trades()[0]
    assert t["side"] == "SHORT" and t["open"] == 1
    s = h.stats()
    assert s["trades"] == 0 and s["open_trades"] == 1
