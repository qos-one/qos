"""Regression tests for the accounting QA findings (history / perf / attribution)."""
from datetime import datetime, timedelta, timezone

import pytest

import api.history as hist
from api import attribution as attr
from api.perf import perf_block

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(hist, "DB_FILE", tmp_path / "h.db")
    hist._node_cache.clear()
    hist.set_fx_rates({})
    yield hist
    hist.set_fx_rates({})


def F(i, side, qty, price, mins=0, comm=0.0, realized=0.0, key="FX:EURUSD", ref="manual", acct="DU1", ts=None):
    return {"exec_id": f"e{i}", "ts": ts or (T0 + timedelta(minutes=mins)).isoformat(), "key": key, "symbol": key.split(":")[1],
            "side": side, "qty": qty, "price": price, "commission": comm, "realized_pnl": realized,
            "order_id": 1, "order_ref": ref, "account": acct}


def trades(h, **kw):
    return sorted(h.list_trades(limit=10**6, **kw), key=lambda t: (t["entry_ts"], t["first_exec"]))


# ---------- pairing ----------
def test_flip_in_one_fill_is_split_into_two_trades(h):
    h.upsert_fills([F(1, "BUY", 10000, 1.10, 0, comm=1.0), F(2, "SELL", 15000, 1.11, 10, comm=3.0), F(3, "BUY", 5000, 1.105, 20, comm=1.0)])
    t = trades(h)
    assert [(x["side"], x["qty"], x["open"]) for x in t] == [("LONG", 10000, 0), ("SHORT", 5000, 0)]
    assert t[0]["entry_price"] == 1.10 and t[0]["exit_price"] == 1.11 and t[0]["pnl"] == pytest.approx(100.0)
    assert t[1]["entry_price"] == 1.11 and t[1]["exit_price"] == 1.105 and t[1]["pnl"] == pytest.approx(25.0)
    assert t[0]["commission"] == pytest.approx(1.0 + 2.0) and t[1]["commission"] == pytest.approx(1.0 + 1.0)   # split pro rata
    assert t[1]["first_exec"] == "e2:flip"


def test_open_trade_after_flip_shows_real_side_and_cost(h):
    h.upsert_fills([F(1, "BUY", 10000, 1.10, 0), F(2, "SELL", 15000, 1.11, 10)])
    o = [x for x in trades(h) if x["open"]]
    assert len(o) == 1 and o[0]["side"] == "SHORT" and o[0]["entry_price"] == 1.11 and o[0]["qty"] == 5000


def test_repeated_flips_close_each_trade(h):
    fills = [F(1, "BUY", 10000, 1.10, 0)] + [F(i, "SELL" if i % 2 == 0 else "BUY", 20000, 1.10 + 0.001 * i, i) for i in range(2, 8)]
    h.upsert_fills(fills)
    assert h.stats()["trades"] == 6 and h.stats()["open_trades"] == 1


def test_realized_pnl_from_prices_for_node_fills_without_ib_row(h):
    for i, (side, px) in enumerate([("BUY", 1.1000), ("SELL", 1.1050)]):
        h.record_node_fill({"exec_id": f"n{i}", "ts": (T0 + timedelta(minutes=i)).isoformat(), "key": "FX:EURUSD", "symbol": "EURUSD",
                            "side": side, "qty": 10000, "price": px, "commission": 2.0, "order_id": None,
                            "order_ref": f"O-20261020-100000-001-000-{i + 1}", "account": "DU1", "node": "n1", "strategy": "s"})
    t = trades(h)[0]
    assert t["pnl"] == pytest.approx(50.0) and t["net_pnl"] == pytest.approx(46.0)
    curve = h.pnl_curves_by_source()["node:n1"]
    assert curve[-1]["v"] == pytest.approx(46.0)


def test_scale_in_partial_close_average_cost(h):
    h.upsert_fills([F(1, "BUY", 10000, 1.1000, 0), F(2, "BUY", 30000, 1.1040, 5), F(3, "SELL", 20000, 1.1100, 10), F(4, "SELL", 20000, 1.1060, 20)])
    t = trades(h)[0]
    assert t["entry_price"] == pytest.approx(1.1030) and t["exit_price"] == pytest.approx(1.1080) and t["pnl"] == pytest.approx(200.0)


def test_usdjpy_pnl_converted_to_usd_at_exit_price(h):
    h.upsert_fills([F(1, "SELL", 100000, 150.00, 0, key="FX:USDJPY"), F(2, "BUY", 100000, 149.50, 30, key="FX:USDJPY")])
    assert trades(h)[0]["pnl"] == pytest.approx(50000 / 149.50)


def test_cross_pair_uses_rates_else_ib_realized(h):
    fills = [F(1, "BUY", 100000, 0.8500, 0, key="FX:EURGBP"), F(2, "SELL", 100000, 0.8600, 5, key="FX:EURGBP", realized=1290.0)]
    h.upsert_fills([dict(f) for f in fills])
    assert trades(h)[0]["pnl"] == pytest.approx(1290.0)          # no GBP rate → IBKR realizedPNL fallback
    h.set_fx_rates({"GBP": 1.30})
    h.rebuild_trades()
    assert trades(h)[0]["pnl"] == pytest.approx(1300.0)          # 1,000 GBP × 1.30


def test_stock_pnl_unchanged(h):
    h.upsert_fills([F(1, "BUY", 10, 100.0, 0, key="STK:AAPL", comm=1.0), F(2, "SELL", 10, 105.0, 5, key="STK:AAPL", comm=1.0)])
    t = trades(h)[0]
    assert t["pnl"] == 50.0 and t["net_pnl"] == 48.0


# ---------- adjust_book ----------
def test_adjust_book_price_null_param_and_unique_ids(h, monkeypatch):
    fixed = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)

    class FDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(h, "datetime", FDT)
    assert h.adjust_book("DU1", "STK:AAPL", 10) == 10
    assert h.adjust_book("DU1", "STK:AAPL", 25) == 15
    assert h.book_positions("DU1")["STK:AAPL"][""] == 25            # same second, both rows recorded
    con = h.connect()
    prices = [r[0] for r in con.execute("SELECT price FROM fills WHERE source = 'adjustment'")]
    con.close()
    assert prices == [None, None]                                   # never 0.0
    h.adjust_book("DU1", "STK:MSFT", 5, price=400.0)
    assert trades(h, key="STK:MSFT")[0]["entry_price"] == 400.0


def test_adjustment_excluded_from_average_cost(h):
    h.upsert_fills([F(1, "BUY", 10, 100.0, 0, key="STK:X")])
    h.adjust_book("DU1", "STK:X", 20, price=50.0)                    # +10 at a different price must not move the cost
    h.upsert_fills([F(3, "SELL", 20, 110.0, 60 * 24 * 30, key="STK:X")])
    t = trades(h, key="STK:X")[0]
    assert t["entry_price"] == 100.0 and t["pnl"] == pytest.approx(200.0)   # 20 × (110 − 100)


def test_adjustment_close_has_no_pnl(h):
    h.upsert_fills([F(1, "BUY", 40000, 1.30, 0, key="FX:GBPUSD", comm=2.0)])
    h.adjust_book("DU1", "FX:GBPUSD", 0)
    t = trades(h, key="FX:GBPUSD")[0]
    assert t["open"] == 0 and t["pnl"] == 0.0 and t["exit_price"] == 1.30 and t["net_pnl"] == -2.0


# ---------- stats / equity / export ----------
def _rt(h, pnls, acct="DU1", start=T0):
    fills = []
    for n, p in enumerate(pnls):
        t = start + timedelta(hours=n)
        fills += [F(f"{acct}{2*n}", "BUY", 10, 100.0, ts=t.isoformat(), key="STK:A", comm=0.5, acct=acct),
                  F(f"{acct}{2*n+1}", "SELL", 10, 100.0 + p / 10, ts=(t + timedelta(minutes=30)).isoformat(), key="STK:A", comm=0.5, acct=acct)]
    h.upsert_fills(fills)


def test_stats_breakeven_and_avg_loss_sign(h):
    _rt(h, [11.0, 1.0, -9.0])                # net +10, 0, −10
    s = h.stats(account="DU1")
    assert (s["wins"], s["losses"], s["breakeven"]) == (1, 1, 1)
    assert s["avg_loss"] == pytest.approx(-10.0) and s["profit_factor"] == pytest.approx(1.0)


def test_all_accounts_equity_is_combined(h):
    con = h.connect()
    for m in range(5):
        ts = (T0 + timedelta(minutes=m)).isoformat()
        con.execute("INSERT INTO equity VALUES (?,?,?,?,?)", (ts, 100000.0 + m, 0, 0, "DU1"))
        con.execute("INSERT INTO equity VALUES (?,?,?,?,?)", (ts, 10000.0 + m, 0, 0, "DU2"))
    con.commit(); con.close()
    s = h.stats(account=None)
    assert s["max_drawdown"] == 0.0 and s["equity_start"] == 110000.0 and s["equity_end"] == 110008.0
    assert len(h.list_equity(account=None)) == 5
    assert len(h.list_equity(account=None, combine=False)) == 10
    assert h.stats(account="DU2")["equity_end"] == 10004.0


def test_range_uses_exit_time_and_viewer_timezone(h):
    now = datetime.now(timezone.utc)
    h.upsert_fills([F(1, "BUY", 10, 100, ts=(now - timedelta(days=8)).isoformat(), key="STK:X"),
                    F(2, "SELL", 10, 110, ts=(now - timedelta(minutes=5)).isoformat(), key="STK:X")])
    assert h.stats("week", account="DU1")["trades"] == 1
    assert h.stats("today", account="DU1", tz="Asia/Bangkok")["trades"] == 1
    since = datetime.fromisoformat(h._since("today", "Asia/Bangkok"))
    assert since.astimezone(timezone(timedelta(hours=7))).hour == 0


def test_source_filter_exact_or_sub_source(h):
    for n, node in enumerate(["a", "ab"]):
        for i, (side, px) in enumerate([("BUY", 1.0), ("SELL", 1.1)]):
            h.record_node_fill({"exec_id": f"{node}{i}", "ts": (T0 + timedelta(minutes=10 * n + i)).isoformat(), "key": "STK:Y",
                                "symbol": "Y", "side": side, "qty": 10, "price": px, "commission": 0.0, "order_id": None,
                                "order_ref": f"O-20261020-10000{n}-001-000-{i + 1}", "account": "DU1", "node": node, "strategy": ""})
    h.upsert_fills([F(9, "BUY", 1, 10.0, 50, key="STK:Z", ref="manual:reverse")])
    assert [t["source"] for t in h.list_trades(source="node:a")] == ["node:a"]
    assert [t["source"] for t in h.list_trades(source="manual")] == ["manual:reverse"]


def test_equity_downsample_keeps_latest_point(h):
    con = h.connect()
    for m in range(2002):
        con.execute("INSERT INTO equity VALUES (?,?,?,?,?)", ((T0 + timedelta(minutes=m)).isoformat(), 1000.0 + m, 0, 0, "DU1"))
    con.commit(); con.close()
    pts = h.list_equity(account="DU1")
    assert pts[-1]["netliq"] == 3001.0 and len(pts) <= 2000


def test_export_fills_respects_range_and_account(h):
    now = datetime.now(timezone.utc)
    h.upsert_fills([F(1, "BUY", 10, 100, ts=(now - timedelta(days=40)).isoformat(), key="STK:Z"),
                    F(2, "BUY", 10, 100, ts=now.isoformat(), key="STK:Z"),
                    F(3, "BUY", 10, 100, ts=now.isoformat(), key="STK:Z", acct="DU2")])
    out = h.export_csv("fills", "week", account="DU1")
    assert "e2," in out and "e1," not in out and "e3," not in out


# ---------- perf ----------
def test_perf_drawdown_pct_relative_to_its_own_peak():
    m = perf_block([("2026-10-05T12:00:00+00:00", 50_000.0), ("2026-10-06T12:00:00+00:00", -20_000.0),
                    ("2026-10-07T12:00:00+00:00", 170_000.0)], 100_000)["metrics"]
    assert m["max_drawdown"] == 20_000 and m["max_drawdown_pct"] == pytest.approx(13.333)


def test_perf_breakeven_not_a_loss():
    m = perf_block([("2026-10-05T12:00:00+00:00", 10.0), ("2026-10-06T12:00:00+00:00", 0.0), ("2026-10-07T12:00:00+00:00", -4.0)], 1000)["metrics"]
    assert m["avg_loss"] == -4.0 and m["profit_factor"] == 2.5


# ---------- attribution ----------
def _live(qty, age=1.0):
    return {"heartbeat_age_s": age, "positions": [{"instrument": "EUR/USD.IDEALPRO", "qty": qty, "side": "LONG", "strategy": "s"}] if qty else []}


def test_restarted_node_flat_while_history_and_ib_hold_is_mismatch():
    rep = attr.reconcile({"FX:EURUSD": 10000}, {"n1": _live(0)}, {"FX:EURUSD": {"node:n1": 10000}}, {"n1"})
    assert rep["rows"][0]["status"] == "mismatch" and rep["status"] == "mismatch"


def test_node_live_vs_history_disagreement_is_mismatch():
    rep = attr.reconcile({"FX:EURUSD": 10000}, {"n1": _live(10000)}, {}, {"n1"})
    assert rep["rows"][0]["status"] == "mismatch" and "disagree" in rep["rows"][0]["note"]


def test_manual_book_while_ib_flat_is_flagged():
    rep = attr.reconcile({}, {}, {"STK:AAPL": {"": 5.0}})
    r = rep["rows"][0]
    assert r["status"] == "untracked" and r["drift"] and rep["status"] == "warn" and r["diff"] == -5.0


def test_open_cost_uses_shared_book_open_trade_only(h):
    base = {"key": "FX:EURUSD", "symbol": "EURUSD", "commission": 0, "realized_pnl": 0, "order_id": 1, "account": "DU1"}
    h.upsert_fills([{**base, "exec_id": "m1", "ts": "2026-10-20T10:00:00+00:00", "side": "BUY", "qty": 30000, "price": 1.10, "order_ref": "manual"}])
    for i, side in enumerate(["BUY", "SELL"]):
        h.record_node_fill({**base, "exec_id": f"n{i}", "ts": f"2026-10-21T10:0{i}:00+00:00", "side": side, "qty": 10000, "price": 1.20,
                            "order_ref": f"O-20261021-100000-001-000-{i+1}", "node": "n1", "strategy": ""})
    h.record_node_fill({**base, "exec_id": "n9", "ts": "2026-10-22T10:00:00+00:00", "side": "BUY", "qty": 10000, "price": 1.30,
                        "order_ref": "O-20261022-100000-001-000-9", "node": "n1", "strategy": ""})
    assert h.open_cost("DU1", "FX:EURUSD") == 1.10
    assert h.open_cost("DU1", "FX:GBPUSD") is None


# ---------- round 3 ----------
def test_r3_cross_fill_recorded_without_rate_gets_rate_once_then_frozen(h):
    h.upsert_fills([F(1, "BUY", 100000, 0.85, 0, key="FX:EURGBP"), F(2, "SELL", 100000, 0.86, 5, key="FX:EURGBP")])
    h.set_fx_rates({"GBP": 1.30})
    assert trades(h)[0]["pnl"] == pytest.approx(1300.0)          # filled in (and trades rebuilt) on the first rates update
    h.set_fx_rates({"GBP": 1.10}); h.rebuild_trades()
    assert trades(h)[0]["pnl"] == pytest.approx(1300.0)          # frozen afterwards
    con = h.connect()
    assert con.execute("SELECT COUNT(*) FROM fx_rate_pending").fetchone()[0] == 0
    assert {r[0] for r in con.execute("SELECT usd_rate FROM fills")} == {1.30}
    con.close()


def test_r3_usd_pairs_never_pending(h):
    h.upsert_fills([F(1, "BUY", 1000, 1.1, 0), F(2, "BUY", 1000, 150.0, 1, key="FX:USDJPY"), F(3, "BUY", 1, 10.0, 2, key="STK:X")])
    con = h.connect()
    assert con.execute("SELECT COUNT(*) FROM fx_rate_pending").fetchone()[0] == 0
    con.close()


def _eq(h, ts, v, acct):
    con = h.connect()
    con.execute("INSERT INTO equity VALUES (?,?,?,?,?)", (ts, v, 0.0, 0.0, acct))
    con.commit(); con.close()


def test_r4_all_accounts_keeps_full_history_and_marks_partial(h):
    for d in range(1, 31):
        _eq(h, f"2026-09-{d:02d}T10:00:00+00:00", 100000 + d * 100, "A")
    for m in range(3):
        _eq(h, f"2026-10-01T10:0{m}:00+00:00", 50000, "B")
    pts = h.list_equity("all", account=None)
    assert pts[0]["ts"].startswith("2026-09-01") and len(pts) == 33
    assert pts[0]["netliq"] == 100100 + 50000 and pts[-1]["netliq"] == 103000 + 50000   # flat extension, no jump
    assert all(p["partial"] for p in pts)                        # A and B never overlap
    s = h.stats(account=None)
    assert s["equity_partial"] is True and s["max_drawdown"] == 0.0


def test_r4_full_overlap_is_not_partial(h):
    for m in range(3):
        _eq(h, f"2026-10-28T10:0{m}:00+00:00", 100000, "A")
        _eq(h, f"2026-10-28T10:0{m}:00+00:00", 50000, "B")
    pts = h.list_equity("all", account=None)
    assert [p["netliq"] for p in pts] == [150000] * 3 and not any(p["partial"] for p in pts)
    assert h.stats(account=None)["equity_partial"] is False
