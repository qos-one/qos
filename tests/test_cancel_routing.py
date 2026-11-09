"""Cancel / node stop / kill reach orders that a node placed (IBKR ties orders to the sending clientId).
Fake IB only, nothing leaves the process."""
import asyncio
from types import SimpleNamespace as NS

import pytest
from ib_async import Forex

import api.history as h


def _tr(oid, cid, ref="", acct="DU1"):
    c = Forex("EURUSD")
    c.localSymbol = "EUR.USD"
    st = NS(status="Submitted", filled=0.0, avgFillPrice=0.0)
    t = NS(order=NS(orderId=oid, clientId=cid, orderRef=ref, permId=oid, account=acct, action="BUY", totalQuantity=1000,
                    orderType="MKT", parentId=0), contract=c, orderStatus=st)
    t.isDone = lambda: t.orderStatus.status in ("Cancelled", "Filled")
    return t


class FakeIB:
    def __init__(self, trades):
        self.live = list(trades)            # what IBKR really has open
        self.stale = list(trades)           # what openTrades() shows (other clients' orders are not kept in sync)
        self.cancelled, self.global_cancel = [], False

    def isConnected(self): return True
    def openTrades(self): return list(self.stale)

    async def reqAllOpenOrdersAsync(self):
        return list(self.live)

    def cancelOrder(self, o):
        self.cancelled.append(o.orderId)
        for t in self.live:
            if t.order.orderId == o.orderId and o.clientId == 60:       # only the owner can cancel
                t.orderStatus.status = "Cancelled"
        self.live = [t for t in self.live if not t.isDone()]

    def reqGlobalCancel(self):
        self.global_cancel = True
        self.live = []


@pytest.fixture
def srv(tmp_path, monkeypatch):
    from api import server as S
    monkeypatch.setattr(h, "DB_FILE", tmp_path / "h.db")
    for name, f in (("MODE_FILE", "mode.json"), ("RISK_FILE", "risk.json"), ("AUDIT_FILE", "audit.jsonl")):
        monkeypatch.setattr(S, name, tmp_path / f)
    monkeypatch.setattr(S, "active_account", lambda: "DU1")
    monkeypatch.setattr(S.nb, "_load_clients", lambda: {"ml_eurusd": 108, "grid_audusd": 112})
    monkeypatch.setattr(S.history, "node_for", lambda coid: None)
    sent, warned = [], []

    async def ok():
        return None

    async def control(cmd, **f):
        sent.append((cmd, f))
        return 1

    async def bcast(msg):
        if msg.get("type") == "warning":
            warned.append(msg)
    monkeypatch.setattr(S, "ensure_connected", ok)
    monkeypatch.setattr(S, "send_control", control)
    monkeypatch.setattr(S, "ws_broadcast", bcast)
    monkeypatch.setattr(S.notify, "fire", lambda *a, **k: None)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(S.asyncio, "sleep", lambda s: real_sleep(0))
    S._t = NS(sent=sent, warned=warned)
    return S


def _as_client(fake, log):
    async def f(host, port, cid, oids, **k):
        log.append((cid, oids))
        fake.live = [t for t in fake.live if t.order.orderId not in oids]
        return {"client_id": cid, "ok": True, "order_ids": oids}
    return f


def test_cancel_own_order(srv, monkeypatch):
    fake = FakeIB([_tr(5, 60, "manual")])
    monkeypatch.setattr(srv, "ib", fake)
    r = asyncio.run(srv.cancel_order(5))
    assert r["method"] == "api" and not r["still_working"] and fake.cancelled == [5]


def test_cancel_stopped_node_order_as_its_client(srv, monkeypatch):
    fake = FakeIB([_tr(101, 109, "O-20261106-093000-001-1:101")])
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv.nm, "_running", lambda n: False)
    log = []
    monkeypatch.setattr(srv.ib_guard, "cancel_as_client", _as_client(fake, log))
    r = asyncio.run(srv.cancel_order(101))
    assert log == [(109, [101])] and r["method"] == "client" and not r["still_working"]


def test_cancel_running_node_order_goes_to_node(srv, monkeypatch):
    fake = FakeIB([_tr(102, 113, "O-20261106-093000-002-1:102")])
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv.nm, "_running", lambda n: n == "grid_audusd")
    r = asyncio.run(srv.cancel_order(102))
    assert srv._t.sent == [("cancel_order", {"node": "grid_audusd", "client_order_id": "O-20261106-093000-002-1", "reason": "manual cancel"})]
    assert r["method"] == "node" and r["still_working"]                 # the fake node never cancels → warned
    assert srv._t.warned and "still working" in srv._t.warned[0]["text"]


def test_stale_order_found_via_fresh_list(srv, monkeypatch):
    fake = FakeIB([_tr(7, 60)])
    fake.stale = []
    monkeypatch.setattr(srv, "ib", fake)
    assert asyncio.run(srv.cancel_order(7))["status"] == "Cancelled"


def test_node_stop_cancels_leftovers_and_reports(srv, monkeypatch):
    fake = FakeIB([_tr(101, 109, "O-20261106-093000-009-1:101"), _tr(9, 60, "manual")])
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv.nm, "_running", lambda n: False)
    log = []
    monkeypatch.setattr(srv.ib_guard, "cancel_as_client", _as_client(fake, log))
    left = asyncio.run(srv._after_node_stop("ml_eurusd"))
    assert left == [] and log == [(109, [101])] and [t.order.orderId for t in fake.live] == [9]
    assert srv._t.warned[0]["event"] == "info"


def test_node_stop_warns_when_order_survives(srv, monkeypatch):
    fake = FakeIB([_tr(101, 109, "O-20261106-093000-009-1:101")])
    monkeypatch.setattr(srv, "ib", fake)
    monkeypatch.setattr(srv.nm, "_running", lambda n: False)

    async def busy(host, port, cid, oids, **k):
        return {"client_id": cid, "ok": False, "error": "cannot connect as client 109: in use"}
    monkeypatch.setattr(srv.ib_guard, "cancel_as_client", busy)
    left = asyncio.run(srv._after_node_stop("ml_eurusd"))
    assert [o["order_id"] for o in left] == [101]
    assert srv._t.warned[0]["event"] == "error" and "ml_eurusd" in srv._t.warned[0]["text"]


def test_gateway_down_alert_once_then_recovery(srv, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(srv.time, "time", lambda: clock[0])
    srv._gw_down.update(since=None, alerted=False)
    asyncio.run(srv._gateway_down_check())
    clock[0] += 899
    asyncio.run(srv._gateway_down_check())
    assert not srv._t.warned
    clock[0] += 2
    asyncio.run(srv._gateway_down_check())
    asyncio.run(srv._gateway_down_check())
    assert len(srv._t.warned) == 1 and "15 min" in srv._t.warned[0]["text"]
    asyncio.run(srv._gateway_back())
    assert len(srv._t.warned) == 2 and srv._t.warned[1]["event"] == "info" and srv._gw_down["since"] is None
