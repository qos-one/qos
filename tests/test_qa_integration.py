"""QA integration (fix phase): api/server.py changes requested by the QA areas (terminal / accounting / ops) that the
qa/* suites do not already prove. Fake IB only — no orders leave the process, no network."""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException
from ib_async import Forex, Stock

import api.history as h
from api.server import _reconcile_positions as _REAL_RECONCILE

NAN = float("nan")


def run(coro):
    return asyncio.run(coro)


class FakeIB:
    def __init__(self, positions=(), values=(), trades=(), fills=()):
        self._pos, self._vals, self._trades, self._fills = list(positions), list(values), list(trades), list(fills)
        self.placed = []

    def isConnected(self): return True
    def managedAccounts(self): return ["DU1"]
    def positions(self): return self._pos
    async def reqPositionsAsync(self): return self._pos
    def portfolio(self): return []
    def accountValues(self, acct=""): return self._vals
    def openTrades(self): return self._trades
    def trades(self): return [p[2] for p in self.placed]
    def fills(self): return self._fills
    async def reqExecutionsAsync(self, flt=None): return self._fills

    def placeOrder(self, c, o):
        tr = NS(order=o, orderStatus=NS(status="Submitted", avgFillPrice=0.0, filled=0.0), isDone=lambda: True)
        self.placed.append((c, o, tr))
        return tr


@pytest.fixture
def srv(tmp_path, monkeypatch):
    from api import server as S
    monkeypatch.setattr(h, "DB_FILE", tmp_path / "h.db")
    h._node_cache.clear()
    for name in ("DATA_DIR",):
        monkeypatch.setattr(S, name, tmp_path)
    for name, f in (("MODE_FILE", "mode.json"), ("DAILY_FILE", "daily.json"), ("RISK_FILE", "risk.json"), ("AUDIT_FILE", "audit.jsonl")):
        monkeypatch.setattr(S, name, tmp_path / f)
    monkeypatch.setattr(S, "_tickers", {})
    monkeypatch.setattr(S, "_contracts", {})
    monkeypatch.setattr(S, "load_session", lambda: {"account_id": "DU1"})
    monkeypatch.setattr(S, "_reconcile_positions", lambda rows: None)
    S._acct_cache.update(ts=0.0, values={})
    return S


# ---------- Terminal #1: websocket snapshot carries reconcile / running_nodes / account_ok ----------
def test_ws_snapshot_flags_missing_account(srv, monkeypatch):
    sent = []

    async def boom():
        raise HTTPException(503, "no account")

    async def pos():
        return {"positions": [], "reconcile": {"status": "ok", "rows": []}}

    async def empty(*a, **k):
        return {"orders": [], "fills": [], "quotes": []}

    async def capture(msg):
        sent.append(msg)
        if msg.get("type") == "snapshot":
            raise asyncio.CancelledError

    monkeypatch.setattr(srv, "ib", FakeIB())
    monkeypatch.setattr(srv, "_ws_clients", {object()})
    monkeypatch.setattr(srv, "account", boom)
    monkeypatch.setattr(srv, "positions", pos)
    monkeypatch.setattr(srv, "orders", empty)
    monkeypatch.setattr(srv, "fills", empty)
    monkeypatch.setattr(srv, "quotes", empty)
    monkeypatch.setattr(srv, "ws_broadcast", capture)
    monkeypatch.setattr(srv, "_running_nodes", lambda: ["n1"])
    with pytest.raises(asyncio.CancelledError):
        run(srv.ws_pusher())
    snap = next(m for m in sent if m["type"] == "snapshot")
    assert snap["account_ok"] is False and snap["account"] == {}
    assert snap["reconcile"] == {"status": "ok", "rows": []} and snap["running_nodes"] == ["n1"]


# ---------- Terminal #2: IB's unset double never reaches /fills ----------
def test_fills_filter_unset_realized_pnl(srv, monkeypatch):
    c = Forex("EURUSD")
    f = NS(contract=c, execution=NS(acctNumber="DU1", execId="e1", time=datetime(2026, 10, 30, 1, tzinfo=timezone.utc), side="BOT",
                                    shares=20000, price=1.1, orderId=1, orderRef="manual"),
           commissionReport=NS(commission=1.7976931348623157e308, realizedPNL=1.7976931348623157e308, currency="USD"))
    monkeypatch.setattr(srv, "ib", FakeIB(fills=[f]))
    row = run(srv.fills())["fills"][0]
    assert row["realized_pnl"] is None and row["commission"] is None


# ---------- Terminal #4: closing a running node's position needs force ----------
def test_close_node_owned_position_needs_force(srv, monkeypatch):
    c = Stock("AAPL", "SMART", "USD"); c.conId = 1
    fake = FakeIB(positions=[NS(account="DU1", contract=c, position=10, avgCost=200.0)])
    monkeypatch.setattr(srv, "ib", fake)
    srv._contracts["STK:AAPL"] = c
    monkeypatch.setattr(srv, "_node_owners", lambda k: ["n1"] if k == "STK:AAPL" else [])
    with pytest.raises(HTTPException) as ei:
        run(srv.close_position("STK:AAPL"))
    assert ei.value.status_code == 409 and not fake.placed
    run(srv.close_position("STK:AAPL", force=True))
    o = fake.placed[0][1]
    assert (o.action, o.totalQuantity, o.orderRef, o.account) == ("SELL", 10, "manual:close", "DU1")
    assert any(json.loads(ln)["event"] == "close_position" for ln in srv.AUDIT_FILE.read_text().splitlines())


# ---------- Terminal #5: node orders are not modifiable in Agent mode ----------
def test_modify_node_order_in_agent_mode_refused(srv, monkeypatch):
    c = Forex("EURUSD")
    o = NS(orderId=7, orderRef="O-20261030-010000-001-000-1", orderType="LMT", action="BUY", totalQuantity=20000, parentId=0, account="DU1")
    monkeypatch.setattr(srv, "ib", FakeIB(trades=[NS(order=o, contract=c, orderStatus=NS(status="Submitted", filled=0.0))]))
    srv.save_mode("agent")
    with pytest.raises(HTTPException) as ei:
        run(srv.modify_order(7, srv.ModifyRequest(price=1.1)))
    assert ei.value.status_code == 409


# ---------- Terminal #7: cached heatmap still returns breadth ----------
def test_heatmap_cached_has_breadth(srv, monkeypatch):
    gid = next(iter(srv.GROUP_BY_ID))
    monkeypatch.setattr(srv, "ib", FakeIB())
    srv._heat_cache[gid] = (time.time(), [{"change_pct": 1.0, "weight": 1}, {"change_pct": -0.5, "weight": 1}, {"change_pct": 0.0, "weight": 1}])
    try:
        r = run(srv.heatmap(gid))
    finally:
        srv._heat_cache.pop(gid, None)
    assert r["cached"] and r["breadth"] == {"up": 1, "down": 1, "total": 3}


# ---------- Accounting #15: daily-loss baseline = last NetLiq before the 17:00 New York boundary ----------
def test_daily_pnl_baseline_from_equity_before_day_start(srv, monkeypatch):
    start = srv._day_start()
    con = h.connect()
    for ts, nl in ((start - timedelta(hours=20), 90_000.0), (start - timedelta(minutes=5), 101_000.0), (start + timedelta(minutes=5), 99_000.0)):
        con.execute("INSERT INTO equity (ts, netliq, unrealized, realized, account) VALUES (?,?,?,?,?)", (ts.isoformat(), nl, 0, 0, "DU1"))
    con.commit()
    con.close()

    async def vals():
        return {"NetLiquidation": "100000", "account_id": "DU1"}
    monkeypatch.setattr(srv, "account_values", vals)
    d = run(srv.daily_pnl())
    assert d["start_netliq"] == 101_000.0 and d["pnl"] == -1_000.0          # the API started after the boundary: the loss is still seen


def test_day_start_is_new_york_1700():
    from api.server import NY_TZ, _day_start
    now = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)                # 16:00 New York (EDT) → day began yesterday 17:00
    assert _day_start(now).astimezone(NY_TZ).strftime("%m-%d %H:%M") == "10-29 17:00"
    assert _day_start(now + timedelta(hours=2)).astimezone(NY_TZ).strftime("%m-%d %H:%M") == "10-30 17:00"


# ---------- Accounting #16: commissions in a foreign currency are converted ----------
def test_commission_converted_to_usd(srv, monkeypatch):
    monkeypatch.setattr(srv, "_usd_rates", lambda: {"USD": 1.0, "JPY": 1 / 150.0})
    assert srv._commission_usd(300.0, "JPY") == pytest.approx(2.0)
    assert srv._commission_usd(2.0, "USD") == 2.0
    assert srv._commission_usd(5.0, "CHF") == 5.0                       # no rate: raw value, audited once


# ---------- Accounting #10: accountValues fallback keeps BASE/USD rows only ----------
def test_one_row_per_tag_prefers_base():
    from api.server import _one_row_per_tag
    rows = [NS(tag="UnrealizedPnL", currency=c, value=v) for c, v in (("JPY", 100000), ("USD", 0.0), ("BASE", 662.25))]
    assert [(r.tag, r.currency) for r in _one_row_per_tag(rows)] == [("UnrealizedPnL", "BASE")]


# ---------- Ops #8: an auto-restarted node that still holds a position is held ----------
def test_restarted_node_with_position_is_held(srv, monkeypatch):
    added = []
    monkeypatch.setattr(srv.holds, "add", lambda node, reason: added.append((node, reason)) or True)
    monkeypatch.setattr(srv, "_main_loop", None)
    h.record_node_fill({"exec_id": "e1", "ts": "2026-10-28T10:00:00+00:00", "key": "FX:EURUSD", "symbol": "EURUSD", "side": "BUY",
                        "qty": 20000, "price": 1.08, "order_ref": "O-20261028-100000-001-000-1", "account": "DU1", "node": "eu1", "strategy": "EMA"})
    srv._node_event("node_restarted", node="eu1", attempt=1, pid=1)
    srv._node_event("node_restarted", node="flat1", attempt=1, pid=2)
    assert [n for n, _ in added] == ["eu1"] and "FX:EURUSD +20,000" in added[0][1]


# ---------- Ops #4: a stale risk payload blocks new entries of a live node ----------
def test_stale_risk_payload_blocks_live_entries():
    from qx_strategies.risk import STALE_S, STATE
    STATE.require_sync = True
    STATE.update({"halted": False})
    assert STATE.halt_reason() == ""
    STATE.received_at = time.time() - STALE_S - 5
    assert "stale" in STATE.halt_reason()
    STATE.require_sync = False                                          # backtest: no API, never stale
    assert STATE.halt_reason() == ""


def test_ws_refuses_other_websites_but_allows_terminal():
    from api import server
    assert server._local_origin(None) and server._local_origin("dioxus://index.html")
    assert server._local_origin("http://dioxus.index.html") and server._local_origin("http://127.0.0.1:8090")
    assert not server._local_origin("https://evil.example") and not server._local_origin("http://localhost.evil.example")


# ================= re-QA round 2 =================
def test_host_header_guard(srv):
    from fastapi.testclient import TestClient
    c = TestClient(srv.app)
    assert c.get("/health", headers={"host": "evil.example:8000"}).status_code == 403          # DNS rebinding
    assert srv._host_ok("127.0.0.1:8000") and srv._host_ok("localhost") and srv._host_ok("[::1]:8000") and srv._host_ok(None)
    assert not srv._host_ok("127.0.0.1.evil.com")


def test_local_origin_exact():
    from api.server import _local_origin
    assert _local_origin(None) and _local_origin("dioxus://index.html") and _local_origin("http://dioxus.index.html")
    assert _local_origin("http://localhost:3000") and _local_origin("http://[::1]:8000")
    for o in ("https://dioxus.evil.com", "http://evil.localhost", "tauri://x", "file://", "null"):
        assert not _local_origin(o), o


def test_assistant_may_not_switch_mode_or_force_close(srv, monkeypatch):
    from api import operators as ops
    assert not ops.allowed("assistant", "PUT", "/mode")
    tok = ops.current.set({"id": "bot", "role": "assistant"})
    try:
        with pytest.raises(HTTPException) as ei:
            run(srv.close_position("STK:AAPL", force=True))
    finally:
        ops.current.reset(tok)
    assert ei.value.status_code == 403


def test_close_all_cancels_selected_account_orders_only(srv, monkeypatch):
    mine = NS(order=NS(orderId=1, account="DU1", clientId=60), contract=Forex("EURUSD"))
    other = NS(order=NS(orderId=2, account="DU2", clientId=60), contract=Forex("EURUSD"))
    fake = FakeIB(trades=[mine, other])
    cancelled = []
    fake.cancelOrder = lambda o: cancelled.append(o.orderId)
    monkeypatch.setattr(srv, "ib", fake)

    async def no_pos():
        return {"positions": []}
    monkeypatch.setattr(srv, "positions", no_pos)
    run(srv.close_all_positions())
    assert cancelled == [1]


def test_unconfirmed_daily_baseline_blocks_until_confirmed(srv, monkeypatch):
    start = srv._day_start()
    con = h.connect()                                    # equity known, but only 5 h before the trading-day start
    con.execute("INSERT INTO equity (ts, netliq, unrealized, realized, account) VALUES (?,?,?,?,?)",
                ((start - timedelta(hours=5)).isoformat(), 104_000.0, 0, 0, "DU1"))
    con.commit()
    con.close()

    async def vals():
        return {"NetLiquidation": "100000", "account_id": "DU1"}

    async def pub():
        return None
    monkeypatch.setattr(srv, "account_values", vals)
    monkeypatch.setattr(srv, "publish_risk", pub)
    monkeypatch.setattr(srv, "ib", FakeIB())
    srv.save_risk({"max_daily_loss_usd": 2000.0, "halted": False}, account="DU1")
    d = run(srv.daily_pnl())
    assert d["baseline"] == "unconfirmed" and not d["baseline_confirmed"]
    with pytest.raises(HTTPException) as ei:
        run(srv.risk_gate(1000.0, "FX:EURUSD"))
    assert ei.value.status_code == 409 and "/risk/daily_baseline" in ei.value.detail
    d = run(srv.confirm_daily_baseline(srv.DailyBaselineBody(netliq=101_000.0)))
    assert d["baseline"] == "operator" and d["start_netliq"] == 101_000.0 and d["pnl"] == -1_000.0
    run(srv.risk_gate(1000.0, "FX:EURUSD"))                            # unblocked


def test_node_blocks_entries_when_daily_pnl_unknown():
    from qx_strategies.risk import RiskState, check_entry
    st = RiskState()
    st.update({"max_daily_loss_usd": 2000, "daily_pnl_usd": None, "max_order_notional_usd": 1e9, "max_position_notional_usd": 1e9})
    assert not check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=st)[0]
    st.update({"max_daily_loss_usd": 0, "daily_pnl_usd": None, "max_order_notional_usd": 1e9, "max_position_notional_usd": 1e9})
    assert check_entry("EUR/USD.IDEALPRO", 1000, 1.1, state=st)[0]          # no daily limit set: nothing to protect


def test_kill_switch_tells_nodes_before_publishing(srv, monkeypatch):
    order = []

    async def ctl(*a, **k):
        order.append("control")
        return 1

    async def pub():
        order.append("publish")
    monkeypatch.setattr(srv, "send_control", ctl)
    monkeypatch.setattr(srv, "publish_risk", pub)
    monkeypatch.setattr(srv.nm, "stop_all", lambda *a, **k: None)
    monkeypatch.setattr(srv, "ib", NS(isConnected=lambda: False))
    monkeypatch.setattr(srv.notify, "fire", lambda *a, **k: None)
    srv.save_risk({"halted": False}, account="DU1")
    srv.save_risk({"halted": False}, account="DU2")
    run(srv.kill_switch())
    assert order[:2] == ["control", "publish"]
    assert srv.load_risk(account="DU2")["halted"]                         # all accounts by default


# ================= re-QA round 3 =================
def _trade(oid, action, qty, otype="LMT", parent=0, filled=0.0, acct="DU1", key="EURUSD"):
    return NS(order=NS(orderId=oid, action=action, totalQuantity=qty, orderType=otype, parentId=parent, account=acct, orderRef="manual"),
              contract=_fxc(key), orderStatus=NS(status="Submitted", filled=filled))


def _fxc(pair):
    c = Forex(pair)
    c.localSymbol = f"{pair[:3]}.{pair[3:]}"
    return c


def test_leg_qty_capped_by_entry_and_side_position(srv, monkeypatch):
    # working BUY 10k entry with a SELL stop, account short 300k: the stop may not exceed the 10k entry
    entry, stop = _trade(1, "BUY", 10_000), _trade(2, "SELL", 10_000, "STP", parent=1)
    fake = FakeIB(trades=[entry, stop])
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv, "position_qty", lambda k: -300_000.0)
    with pytest.raises(HTTPException):
        srv._check_leg_qty(stop, "FX:EURUSD", 300_000)
    srv._check_leg_qty(stop, "FX:EURUSD", 10_000)
    # two filled 100k brackets (long 200k): legs per bracket count once, together ≤ the long position
    s1, t1 = _trade(11, "SELL", 100_000, "STP", parent=10), _trade(12, "SELL", 100_000, "LMT", parent=10)
    s2 = _trade(21, "SELL", 100_000, "STP", parent=20)
    fake = FakeIB(trades=[s1, t1, s2])
    fake.trades = lambda: [_trade(10, "BUY", 100_000, filled=100_000), _trade(20, "BUY", 100_000, filled=100_000), s1, t1, s2]
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv, "position_qty", lambda k: 200_000.0)
    srv._check_leg_qty(s1, "FX:EURUSD", 60_000)                         # reducing is fine
    with pytest.raises(HTTPException):
        srv._check_leg_qty(s1, "FX:EURUSD", 200_000)                    # above its filled entry
    monkeypatch.setattr(srv, "position_qty", lambda k: 150_000.0)
    with pytest.raises(HTTPException):
        srv._check_leg_qty(s1, "FX:EURUSD", 100_000)                    # 100k + 100k legs > 150k long


def test_close_cancels_working_bracket_entry_with_its_legs(srv, monkeypatch):
    entry, stop, other = _trade(1, "BUY", 10_000), _trade(2, "SELL", 10_000, "STP", parent=1), _trade(3, "BUY", 5_000)
    fake = FakeIB(trades=[entry, stop, other])
    cancelled = []
    fake.cancelOrder = lambda o: cancelled.append(o.orderId)
    monkeypatch.setattr(srv, "ib", fake)
    assert sorted(srv._cancel_bracket_legs("FX:EURUSD")) == [1, 2] and sorted(cancelled) == [1, 2]   # plain order 3 untouched


def test_resume_clears_all_accounts_kill(srv, monkeypatch):
    monkeypatch.setattr(srv, "active_account", lambda: "DU1")

    async def pub():
        return None
    monkeypatch.setattr(srv, "publish_risk", pub)
    srv.save_risk({"halted": False}, account="DU2")
    srv.halt_all_accounts(srv.KILL_ALL_REASON)
    srv.save_risk({**srv.load_risk(), "halted": True, "halt_reason": "kill switch"})
    run(srv.resume_trading())
    assert not srv.load_risk()["halted"] and not srv.load_risk(account="")["halted"] and not srv.load_risk(account="DU2")["halted"]


def test_daily_baseline_only_while_unconfirmed_and_not_below_netliq(srv, monkeypatch):
    async def vals():
        return {"NetLiquidation": "98100", "account_id": "DU1"}

    async def pub():
        return None
    monkeypatch.setattr(srv, "account_values", vals)
    monkeypatch.setattr(srv, "publish_risk", pub)
    srv.DAILY_FILE.write_text(json.dumps({"accounts": {"DU1": {"date": srv._today(), "start_netliq": 100_000.0, "baseline": "history"}}}))
    with pytest.raises(HTTPException) as ei:
        run(srv.confirm_daily_baseline(srv.DailyBaselineBody(netliq=90_000.0)))
    assert ei.value.status_code == 409
    srv.DAILY_FILE.write_text(json.dumps({"accounts": {"DU1": {"date": srv._today(), "start_netliq": 98_100.0, "baseline": "unconfirmed"}}}))
    with pytest.raises(HTTPException):
        run(srv.confirm_daily_baseline(srv.DailyBaselineBody(netliq=90_000.0)))      # would turn a loss into a profit
    assert run(srv.confirm_daily_baseline(srv.DailyBaselineBody(netliq=100_000.0)))["pnl"] == -1_900.0


def test_unconfirmed_baseline_notifies_once(srv, monkeypatch):
    fired = []
    monkeypatch.setattr(srv.notify, "fire", lambda text, kind="": fired.append(text))
    monkeypatch.setattr(srv.history, "netliq_before", lambda acct, ts, older=None: None if older else 101_000.0)

    async def vals():
        return {"NetLiquidation": "100000", "account_id": "DU1"}
    monkeypatch.setattr(srv, "account_values", vals)
    run(srv.daily_pnl())
    run(srv.daily_pnl())
    assert len(fired) == 1 and "unconfirmed" in fired[0]


def test_host_parsing_strict(srv, monkeypatch):
    for h_ in ("localhost:8000@evil.com", "evil.com:80:localhost", "localhost:0", "localhost:99999", "localhost:80a", "[::1", "localhost."):
        assert not srv._host_ok(h_), h_
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("QOS_TEST_HOSTS", raising=False)
    assert not srv._host_ok("testserver")                                  # outside tests the TestClient name is not trusted


def test_cancel_other_account_order_refused(srv, monkeypatch):
    fake = FakeIB(trades=[_trade(5, "BUY", 1000, acct="DU2")])
    cancelled = []
    fake.cancelOrder = lambda o: cancelled.append(o.orderId)
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv, "active_account", lambda: "DU1")

    async def ok():
        return None
    monkeypatch.setattr(srv, "ensure_connected", ok)
    with pytest.raises(HTTPException):
        run(srv.cancel_order(5))
    assert not cancelled


def test_to_tick():
    from api.server import _to_tick
    assert _to_tick(1.123456, 0.00005) == 1.12345 and _to_tick(None, 0.01) is None and _to_tick(150.006, 0.005) == 150.005


# ================= re-QA round 4 =================
def test_open_books_uses_quantity_still_open(srv, monkeypatch):
    monkeypatch.setattr(srv.history, "book_positions", lambda acct=None: {"FX:USDJPY": {"node:a": 99_000.0, "node:b": -50_000.0, "": 0.0}})
    monkeypatch.setattr(srv.history, "list_trades", lambda *a, **k: [
        {"open": 1, "source": "node:a", "qty": 100_000, "side": "LONG", "entry_price": 150.0},     # opened 100k, 1k closed since
        {"open": 1, "source": "node:b", "qty": 50_000, "side": "SHORT", "entry_price": 152.0}])
    q, px = srv._open_books("DU1", "FX:USDJPY")
    assert q == 49_000 and px == pytest.approx((99_000 * 150 - 50_000 * 152) / 49_000)


def test_check_position_counts_filled_bracket_legs(srv, monkeypatch):
    sl = _trade(2, "SELL", 300_000, "STP", parent=1)                 # entry 1 filled → long 300k, its SL leg works
    monkeypatch.setattr(srv, "ib", FakeIB(trades=[sl]))
    monkeypatch.setattr(srv, "position_qty", lambda k: 300_000.0)
    monkeypatch.setattr(srv, "load_risk", lambda account=None: {"max_position_notional_usd": 400_000.0})
    assert srv._working_legs("FX:EURUSD") == (0.0, 300_000.0)
    with pytest.raises(HTTPException):
        srv._check_position("FX:EURUSD", "SELL", 600_000, 1.1)       # 300k − 300k − 600k = −600k → 660k USD
    srv._check_position("FX:EURUSD", "SELL", 300_000, 1.1)           # −300k → 330k USD: ok


def test_leg_cap_counts_standalone_exit(srv, monkeypatch):
    sl, tp, stop = _trade(2, "SELL", 50_000, "STP", parent=1), _trade(3, "SELL", 50_000, "LMT", parent=1), _trade(4, "SELL", 100_000, "STP")
    fake = FakeIB(trades=[sl, tp, stop])
    fake.trades = lambda: [_trade(1, "BUY", 100_000, filled=100_000), sl, tp, stop]
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv, "position_qty", lambda k: 100_000.0)
    with pytest.raises(HTTPException):
        srv._check_leg_qty(sl, "FX:EURUSD", 100_000)                 # bracket 100k + standalone stop 100k > 100k long


def test_kill_all_keeps_existing_halt_reasons(srv, monkeypatch):
    monkeypatch.setattr(srv, "active_account", lambda: "DU1")

    async def pub():
        return None
    monkeypatch.setattr(srv, "publish_risk", pub)
    srv.save_risk({"halted": True, "halt_reason": "daily loss limit hit (-2,100 USD)"}, account="DU2")
    srv.save_risk({"halted": False}, account="DU3")
    srv.halt_all_accounts(srv.KILL_ALL_REASON)
    assert srv.load_risk(account="DU2")["halt_reason"].startswith("daily loss")
    run(srv.resume_trading())
    assert srv.load_risk(account="DU2")["halted"] and not srv.load_risk(account="DU3")["halted"]


def test_history_equity_partial_flag(srv, monkeypatch):
    monkeypatch.setattr(srv.history, "list_equity", lambda *a, **k: [{"ts": "t1", "netliq": 1.0, "partial": True}, {"ts": "t2", "netliq": 2.0}])
    assert run(srv.history_equity(range="all", account="ALL"))["partial"] is True
    monkeypatch.setattr(srv.history, "list_equity", lambda *a, **k: [{"ts": "t1", "netliq": 1.0}])
    assert run(srv.history_equity(range="all", account="ALL"))["partial"] is False


# ================= re-QA round 5 =================
def test_reverse_cancels_old_legs_after_the_order_is_accepted(srv, monkeypatch):
    sl = _trade(2, "SELL", 60_000, "STP", parent=1)
    fake = FakeIB(trades=[sl])
    cancelled = []
    fake.cancelOrder = lambda o: cancelled.append(o.orderId)
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv, "position_qty", lambda k: 60_000.0)

    async def ok():
        return None
    monkeypatch.setattr(srv, "ensure_connected", ok)
    srv.save_mode("manual")

    async def rejected(*a, **k):
        raise HTTPException(400, "limit")
    monkeypatch.setattr(srv, "submit_order", rejected)
    with pytest.raises(HTTPException):
        run(srv.reverse_position("FX:EURUSD"))
    assert not cancelled                                             # rejected reverse: old protection stays

    async def accepted(*a, **k):
        return {"order_id": 9, "status": "Submitted"}
    monkeypatch.setattr(srv, "submit_order", accepted)
    assert run(srv.reverse_position("FX:EURUSD"))["cancelled_legs"] == [2] and cancelled == [2]


def test_cash_far_from_books_is_a_reconcile_warning(srv, monkeypatch):
    rows = [{"key": "FX:USDJPY", "position": 100_000.0, "cash_warn": "cash balance implies +200,000; possible trade outside QOS"}]
    monkeypatch.setattr(srv.attr, "reconcile", lambda *a, **k: {"status": "ok", "rows": [{"key": "FX:USDJPY", "status": "ok", "note": "IBKR +100,000"}]})
    monkeypatch.setattr(srv.attr, "annotate", lambda rows, rep, src=None: None)
    monkeypatch.setattr(srv.history, "book_positions", lambda acct=None: {})
    monkeypatch.setattr(srv.history, "open_sources", lambda acct=None: {})
    rep = _REAL_RECONCILE(rows)                                     # the srv fixture stubs _reconcile_positions
    assert rep["status"] == "warn" and rep["rows"][0]["status"] == "warn" and "outside QOS" in rep["rows"][0]["note"]


def test_kill_all_without_account_is_cleared_by_resume(srv, monkeypatch):
    monkeypatch.setattr(srv, "active_account", lambda: "")

    async def none(*a, **k):
        return 0
    monkeypatch.setattr(srv, "send_control", none)
    monkeypatch.setattr(srv, "publish_risk", none)
    monkeypatch.setattr(srv.nm, "stop_all", lambda *a, **k: None)
    monkeypatch.setattr(srv, "ib", NS(isConnected=lambda: False))
    run(srv.kill_switch())
    assert srv.load_risk(account="")["halt_reason"] == srv.KILL_ALL_REASON
    monkeypatch.setattr(srv, "active_account", lambda: "DU1")             # an account is selected before resuming
    run(srv.resume_trading())
    assert not srv.load_risk(account="")["halted"]


def test_reverse_pending_keeps_legs_until_live(srv, monkeypatch):
    sl = _trade(2, "SELL", 60_000, "STP", parent=1)
    newer = _trade(3, "SELL", 60_000, "STP", parent=7)               # protection placed after the reverse: never touched
    fake = FakeIB(trades=[sl])
    cancelled = []
    fake.cancelOrder = lambda o: cancelled.append(o.orderId)
    rev = NS(order=NS(orderId=9), orderStatus=NS(status="PendingSubmit"))
    fake.trades = lambda: [rev]
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv, "position_qty", lambda k: 60_000.0)

    async def ok():
        return None
    monkeypatch.setattr(srv, "ensure_connected", ok)
    srv.save_mode("manual")

    async def pending(*a, **k):
        return {"order_id": 9, "status": "PendingSubmit"}
    monkeypatch.setattr(srv, "submit_order", pending)
    tasks = []
    real_create = asyncio.create_task

    async def go():
        monkeypatch.setattr(srv.asyncio, "create_task", lambda c: tasks.append(real_create(c)) or tasks[-1])
        res = await srv.reverse_position("FX:EURUSD")
        assert "warning" in res and not cancelled                     # unknown status: legs kept
        fake._trades.append(newer)
        rev.orderStatus.status = "Submitted"
        await asyncio.wait_for(tasks[0], 5)
    run(go())
    assert cancelled == [2]


def test_cash_warn_notifies_once_per_day(srv, monkeypatch):
    fired = []
    monkeypatch.setattr(srv.notify, "fire", lambda text, kind="": fired.append(text))
    monkeypatch.setattr(srv, "ib", FakeIB(values=[NS(tag="CashBalance", value="-30000000", currency="JPY", account="DU1")]))
    srv._tickers["FX:USDJPY"] = NS(bid=150.0, ask=150.0, last=150.0, close=150.0, marketPrice=lambda: 150.0)
    monkeypatch.setattr(srv, "_open_books", lambda acct, key: (100_000.0, 150.0))
    monkeypatch.setattr(srv, "_books_match_nodes", lambda acct, key: True)
    srv._cash_warn_sent.clear()
    for _ in range(3):
        e = srv._fx_cash_map()["FX:USDJPY"]
    assert e["qty"] == 100_000 and "outside QOS" in e["cash_warn"] and len(fired) == 1
