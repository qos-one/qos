"""A second Close / Reverse / Close all is refused while a close order for the same position is still working
(two terminals at once, a double click, or an order stuck in PreSubmitted). Fake IB only, nothing leaves the process."""
import asyncio
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException
from ib_async import Forex

import api.history as h


def _fxc(pair):
    c = Forex(pair)
    c.localSymbol = f"{pair[:3]}.{pair[3:]}"
    return c


def _working(oid, ref, pair="EURUSD", done=False, acct="DU1"):
    return NS(order=NS(orderId=oid, action="SELL", totalQuantity=20000, orderType="MKT", parentId=0, account=acct, orderRef=ref),
              contract=_fxc(pair), orderStatus=NS(status="PreSubmitted", filled=0.0), isDone=lambda: done)


class FakeIB:
    def __init__(self, trades=()):
        self._trades = list(trades)
        self.placed = []

    def isConnected(self): return True
    def managedAccounts(self): return ["DU1"]
    def openTrades(self): return [t for t in self._trades if not t.isDone()]
    def trades(self): return self._trades
    def positions(self): return []
    def portfolio(self): return []
    def accountValues(self, acct=""): return []
    def cancelOrder(self, o): pass

    def placeOrder(self, c, o):
        o.orderId = 100 + len(self.placed)
        tr = NS(order=o, contract=c, orderStatus=NS(status="PreSubmitted", avgFillPrice=0.0, filled=0.0), isDone=lambda: False)
        self.placed.append(tr)
        self._trades.append(tr)
        return tr


@pytest.fixture
def srv(tmp_path, monkeypatch):
    from api import server as S
    monkeypatch.setattr(h, "DB_FILE", tmp_path / "h.db")
    for name, f in (("MODE_FILE", "mode.json"), ("RISK_FILE", "risk.json"), ("AUDIT_FILE", "audit.jsonl")):
        monkeypatch.setattr(S, name, tmp_path / f)
    monkeypatch.setattr(S, "active_account", lambda: "DU1")
    monkeypatch.setattr(S, "_node_owners", lambda k: [])
    monkeypatch.setattr(S, "position_qty", lambda k: 20000.0)

    async def ok():
        return None

    async def contract(k):
        await asyncio.sleep(0.01)                       # lets a second request run in between
        return _fxc(k.split(":")[1])
    monkeypatch.setattr(S, "ensure_connected", ok)
    monkeypatch.setattr(S, "contract_for", contract)
    S._flattening.clear()
    return S


def test_second_close_refused_while_first_is_working(srv, monkeypatch):
    fake = FakeIB([_working(7, "manual:close")])
    monkeypatch.setattr(srv, "ib", fake)
    with pytest.raises(HTTPException) as e:
        asyncio.run(srv.close_position("FX:EURUSD"))
    assert e.value.status_code == 409 and "7" in e.value.detail and not fake.placed


@pytest.mark.parametrize("trade", [_working(7, "manual:close", pair="GBPUSD"),          # other instrument
                                   _working(7, "manual:close", done=True),            # already filled
                                   _working(7, "manual"),                             # a normal resting order
                                   _working(7, "manual:close", acct="DU9")])          # other account
def test_close_allowed_otherwise(srv, monkeypatch, trade):
    fake = FakeIB([trade])
    monkeypatch.setattr(srv, "ib", fake)
    asyncio.run(srv.close_position("FX:EURUSD"))
    assert len(fake.placed) == 1 and fake.placed[0].order.orderRef == "manual:close"


def test_two_simultaneous_closes_send_one_order(srv, monkeypatch):
    fake = FakeIB()
    monkeypatch.setattr(srv, "ib", fake)

    async def both():
        return await asyncio.gather(srv.close_position("FX:EURUSD"), srv.close_position("FX:EURUSD"), return_exceptions=True)
    res = asyncio.run(both())
    assert len(fake.placed) == 1
    assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in res) == 1
    assert not srv._flattening                                       # released after the request


def test_claim_released_when_close_fails(srv, monkeypatch):
    monkeypatch.setattr(srv, "ib", FakeIB())
    monkeypatch.setattr(srv, "position_qty", lambda k: 0.0)
    with pytest.raises(HTTPException) as e:
        asyncio.run(srv.close_position("FX:EURUSD"))
    assert e.value.status_code == 404 and not srv._flattening


def test_close_all_refused_while_a_close_is_working(srv, monkeypatch):
    fake = FakeIB([_working(7, "manual:close_all", pair="GBPUSD")])
    monkeypatch.setattr(srv, "ib", fake)
    with pytest.raises(HTTPException) as e:
        asyncio.run(srv.close_all_positions())
    assert e.value.status_code == 409 and not fake.placed


def test_reverse_refused_while_a_close_is_working(srv, monkeypatch):
    monkeypatch.setattr(srv, "ib", FakeIB([_working(7, "manual:close")]))
    srv.save_mode("manual")
    sent = []

    async def submit(*a, **k):
        sent.append(a)
        return {"order_id": 9, "status": "Submitted"}
    monkeypatch.setattr(srv, "submit_order", submit)
    with pytest.raises(HTTPException) as e:
        asyncio.run(srv.reverse_position("FX:EURUSD"))
    assert e.value.status_code == 409 and not sent


def test_close_refused_while_reverse_is_working(srv, monkeypatch):
    fake = FakeIB([_working(7, "manual:reverse")])
    monkeypatch.setattr(srv, "ib", fake)
    with pytest.raises(HTTPException):
        asyncio.run(srv.close_position("FX:EURUSD"))
    assert not fake.placed
