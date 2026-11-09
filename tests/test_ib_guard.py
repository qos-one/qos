"""Cancel routing for orders other IBKR clients placed, gateway-down alert timing, catalog weekend skip"""
import asyncio
from types import SimpleNamespace as NS

import pandas as pd

from api import ib_guard
from api.catalog import fx_last_close, up_to_close


def trade(oid, cid, ref=""):
    return NS(order=NS(orderId=oid, clientId=cid, orderRef=ref, permId=oid * 10))


def coid(ref):
    return ref.rpartition(":")[0] if ref.startswith("O-") else ""


def test_plan_routes_by_owner():
    trades = [trade(1, 60), trade(101, 109, "O-20261106-001-1:101"), trade(102, 113, "O-20261106-002-1:102"), trade(5, 0)]
    node_of = {101: "ml_eurusd", 102: "grid_audusd"}
    items = ib_guard.plan(trades, 60, lambda tr: node_of.get(tr.order.orderId, ""), lambda n: n == "grid_audusd", coid)
    m = {it["order_id"]: it["method"] for it in items}
    assert m == {1: "api", 101: "client", 102: "node", 5: "client"}
    assert items[2]["client_order_id"] == "O-20261106-002-1"
    assert ib_guard.by_client(items) == {109: [101], 0: [5]}


def test_running_node_without_coid_falls_back_to_client():
    items = ib_guard.plan([trade(7, 113, "manual")], 60, lambda tr: "grid", lambda n: True, coid)
    assert items[0]["method"] == "client"


class FakeIB:
    instances = []

    def __init__(self, fail=False):
        self.fail, self.cancelled, self.disconnected, self.cid = fail, [], False, None
        FakeIB.instances.append(self)

    async def connectAsync(self, host, port, clientId, timeout):
        if self.fail:
            raise ConnectionError("clientId already in use")
        self.cid = clientId

    def cancelOrder(self, o):
        self.cancelled.append((o.orderId, o.clientId))

    def disconnect(self):
        self.disconnected = True


def test_cancel_as_client_uses_owner_id():
    res = asyncio.run(ib_guard.cancel_as_client("h", 1, 109, [101, 103], wait=0, ib_factory=FakeIB))
    ib = FakeIB.instances[-1]
    assert res["ok"] and ib.cid == 109 and ib.cancelled == [(101, 109), (103, 109)] and ib.disconnected


def test_cancel_as_client_reports_connect_failure():
    res = asyncio.run(ib_guard.cancel_as_client("h", 1, 109, [101], wait=0, ib_factory=lambda: FakeIB(fail=True)))
    assert not res["ok"] and "client 109" in res["error"]


def test_down_alert_due_once_after_threshold():
    assert not ib_guard.down_alert_due(None, 1000, False)
    assert not ib_guard.down_alert_due(100, 999, False)
    assert ib_guard.down_alert_due(100, 1000, False)
    assert not ib_guard.down_alert_due(100, 5000, True)


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def test_fx_last_close():
    assert fx_last_close(ts("2026-11-06 20:00")) is None                     # Fri 15:00 NY (EST) open
    assert fx_last_close(ts("2026-11-07 10:00")) == ts("2026-11-06 22:00")   # Saturday
    assert fx_last_close(ts("2026-11-08 21:00")) == ts("2026-11-06 22:00")   # Sun 16:00 NY
    assert fx_last_close(ts("2026-11-08 22:30")) is None                     # Sun 17:30 NY open
    assert fx_last_close(ts("2026-10-31 12:00")) == ts("2026-10-30 21:00")   # EDT close


def test_up_to_close_skips_only_complete_catalog():
    sat = ts("2026-11-07 10:00")
    assert up_to_close(ts("2026-11-06 21:59"), sat)
    assert not up_to_close(ts("2026-11-06 18:00"), sat)
    assert not up_to_close(None, sat)
    assert not up_to_close(ts("2026-11-06 21:59"), ts("2026-11-09 03:00"))
