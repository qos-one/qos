import api.attribution as attr
import api.history as h

COID = "O-20261027-093000-001-000-1"
TS = "2026-10-27T09:30:00+00:00"


def _db(tmp_path):
    h.DB_FILE = tmp_path / "h.db"
    h._node_cache.clear()
    return h


def _ib(exec_id, side, qty, price, ref, realized=0.0, comm=2.0, ts=TS, key="FX:EURUSD"):
    return {"exec_id": exec_id, "ts": ts, "key": key, "symbol": key.split(":")[1], "side": side, "qty": qty, "price": price,
            "commission": comm, "realized_pnl": realized, "order_id": 11, "order_ref": ref, "account": "DU1"}


def _node_fill(exec_id, side, qty, price, coid=COID, node="ema_eurusd", ts=TS):
    return {"exec_id": exec_id, "ts": ts, "key": "FX:EURUSD", "symbol": "EURUSD", "side": side, "qty": qty, "price": price,
            "commission": 0.0, "order_id": 11, "order_ref": coid, "account": "DU1", "node": node, "strategy": "EMACross-000",
            "instrument": "EUR/USD.IDEALPRO"}


def test_classify_and_parse():
    assert attr.client_order_id(f"{COID}:1234") == COID
    assert attr.client_order_id("agent:ema_cross") == ""
    assert attr.classify("") == "manual" and attr.classify("manual") == "manual"
    assert attr.classify("agent:ema_cross") == "agent:ema_cross"
    assert attr.classify(f"{COID}:1234") == "nautilus"
    assert attr.classify(f"{COID}:1234", "ema_eurusd") == "node:ema_eurusd"
    assert attr.nautilus_to_key("EUR/USD.IDEALPRO") == "FX:EURUSD"
    assert attr.nautilus_to_key("AAPL.SMART") == "STK:AAPL"
    assert attr.nautilus_to_key("BTC/USD.PAXOS") == "CRYPTO:BTC"


def test_ib_fill_attributed_after_order_event(tmp_path):
    h = _db(tmp_path)
    h.upsert_fills([_ib("e1", "BUY", 20000, 1.1, f"{COID}:11")])
    assert h.list_trades()[0]["source"] == "nautilus"
    assert h.record_node_order(COID, "ema_eurusd", "EMACross-000") == 1
    t = h.list_trades()[0]
    assert t["source"] == "node:ema_eurusd" and t["node"] == "ema_eurusd" and t["strategy"] == "EMACross-000"
    assert h.node_for(COID) == ("ema_eurusd", "EMACross-000")


def test_node_fill_first_then_ib_adds_realized(tmp_path):
    h = _db(tmp_path)
    assert h.record_node_fill(_node_fill("e1", "BUY", 20000, 1.1))
    assert h.record_node_fill(_node_fill("e2", "SELL", 20000, 1.101, coid=COID[:-1] + "2", ts="2026-10-27T10:00:00+00:00"))
    assert not h.record_node_fill(_node_fill("e1", "BUY", 20000, 1.1))
    h.upsert_fills([_ib("e2", "SELL", 20000, 1.101, f"{COID[:-1]}2:12", realized=20.0, ts="2026-10-27T10:00:00+00:00")])
    con = h.connect()
    r = dict(con.execute("SELECT * FROM fills WHERE exec_id='e2'").fetchone())
    con.close()
    assert r["node"] == "ema_eurusd" and r["realized_pnl"] == 20.0 and r["commission"] == 2.0 and r["order_ref"].endswith(":12")
    t = h.list_trades()
    assert len(t) == 1 and t[0]["open"] == 0 and t[0]["source"] == "node:ema_eurusd"
    s = h.stats(source="node:")
    assert s["trades"] == 1 and s["by_source"][0]["name"] == "node:ema_eurusd"
    assert h.stats(source="manual")["trades"] == 0


def test_node_and_manual_books_are_separate(tmp_path):
    h = _db(tmp_path)
    h.record_node_fill(_node_fill("n1", "BUY", 20000, 1.1))
    h.upsert_fills([_ib("m1", "SELL", 5000, 1.1005, "manual", ts="2026-10-27T09:40:00+00:00")])
    books = h.book_positions("DU1")["FX:EURUSD"]
    assert books == {"node:ema_eurusd": 20000, "": -5000}
    trades = h.list_trades()
    assert {(t["source"], t["side"], t["open"]) for t in trades} == {("node:ema_eurusd", "LONG", 1), ("manual", "SHORT", 1)}
    assert h.open_sources("DU1") == {"FX:EURUSD": "manual"}


def _live(qty, age=2.0, side="LONG"):
    return {"heartbeat_age_s": age, "positions": [{"instrument": "EUR/USD.IDEALPRO", "strategy": "EMACross-000", "side": side, "qty": qty}]}


def test_reconcile_statuses():
    books = {"FX:EURUSD": {"node:ema_eurusd": 20000.0, "": -5000.0}}
    ok = attr.reconcile({"FX:EURUSD": 15000.0}, {"ema_eurusd": _live(20000)}, books, {"ema_eurusd"})
    assert ok["status"] == "ok" and ok["rows"][0]["status"] == "ok"
    bad = attr.reconcile({"FX:EURUSD": -5000.0}, {"ema_eurusd": _live(20000)}, books, {"ema_eurusd"})
    assert bad["status"] == "mismatch" and bad["issues"] == 1 and bad["rows"][0]["diff"] == -20000
    stale = attr.reconcile({"FX:EURUSD": 15000.0}, {"ema_eurusd": _live(20000, age=90)}, books, {"ema_eurusd"})
    assert stale["rows"][0]["status"] == "stale" and stale["status"] == "warn"
    orphan = attr.reconcile({"FX:EURUSD": 15000.0}, {}, books, set())
    assert orphan["rows"][0]["status"] == "orphan" and orphan["status"] == "mismatch"
    ghost = attr.reconcile({}, {"ema_eurusd": _live(20000)}, {"FX:EURUSD": {"node:ema_eurusd": 20000.0}}, {"ema_eurusd"})
    assert ghost["rows"][0]["status"] == "mismatch"
    ext = attr.reconcile({"STK:AAPL": 10.0}, {}, {"STK:AAPL": {"": 10.0}}, set())
    assert ext["rows"][0]["status"] == "external" and ext["status"] == "ok"
    untracked = attr.reconcile({"STK:AAPL": 10.0}, {}, {}, set())
    assert untracked["rows"][0]["status"] == "untracked" and untracked["status"] == "ok"


def test_annotate_positions():
    books = {"FX:EURUSD": {"node:ema_eurusd": 20000.0, "": -5000.0}, "STK:AAPL": {"": 10.0}}
    rep = attr.reconcile({"FX:EURUSD": 15000.0, "STK:AAPL": 10.0}, {"ema_eurusd": _live(20000)}, books, {"ema_eurusd"})
    rows = [{"key": "FX:EURUSD", "position": 15000.0}, {"key": "STK:AAPL", "position": 10.0}]
    attr.annotate(rows, rep, {"FX:EURUSD": "manual", "STK:AAPL": "agent:ema_cross"})
    assert rows[0]["source"] == "mixed" and rows[0]["recon"] == "ok"
    assert [s["source"] for s in rows[0]["sources"]] == ["node:ema_eurusd", "manual"]
    assert rows[1]["source"] == "agent:ema_cross" and rows[1]["recon"] == "external"
