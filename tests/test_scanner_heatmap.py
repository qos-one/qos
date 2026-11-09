"""Scanner heatmap groups: whole-market US stock scans by IBKR (top gainers / losers / most active). Fake IB only."""
import asyncio
import time
from types import SimpleNamespace as NS

import pytest
from ib_async import Stock

from api.groups import SCAN_ROWS, scan_leader, scan_weight


def test_scan_weight_and_leader():
    assert scan_weight("TOP_PERC_GAIN", 0, 12.5) == 12.5
    assert scan_weight("TOP_PERC_LOSE", 3, -8.0) == 8.0
    assert scan_weight("TOP_PERC_GAIN", 0, 0.0) == 0.1                 # never a zero-size tile
    assert scan_weight("MOST_ACTIVE", 0, 1.0) == SCAN_ROWS and scan_weight("MOST_ACTIVE", 49, 1.0) == 1
    rows = [{"symbol": "A", "change_pct": 4.0, "weight": 4}, {"symbol": "B", "change_pct": 9.0, "weight": 9},
            {"symbol": "C", "change_pct": -6.0, "weight": 6}]
    assert scan_leader(rows, "TOP_PERC_GAIN")["symbol"] == "B"
    assert scan_leader(rows, "TOP_PERC_LOSE")["symbol"] == "C"
    assert scan_leader([], "MOST_ACTIVE") is None


def _stk(sym, con_id):
    c = Stock(sym, "NASDAQ", "USD")
    c.conId = con_id
    return c


class FakeIB:
    def __init__(self, rows, prices):
        self.rows, self.prices, self.subs, self.cancelled = rows, prices, [], []

    def isConnected(self): return True

    async def reqScannerDataAsync(self, sub):
        self.subs.append(sub)
        return self.rows

    def reqMktData(self, c, *a):
        last, close = self.prices[c.symbol]
        return NS(contract=c, last=last, bid=float("nan"), ask=float("nan"), close=close)

    def cancelMktData(self, c): self.cancelled.append(c.symbol)

    async def qualifyContractsAsync(self, *cs):
        self.qualified = [c.symbol for c in cs]
        for i, c in enumerate(cs):
            if c.symbol != "OLD":
                c.conId = 100 + i
        return [c for c in cs if c.conId]


@pytest.fixture
def srv(monkeypatch):
    from api import server as S

    async def ok():
        return None
    real_sleep = asyncio.sleep
    monkeypatch.setattr(S, "ensure_connected", ok)
    monkeypatch.setattr(S.asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.setattr(S, "_tickers", {})
    monkeypatch.setattr(S, "_contracts", {})
    for k in ("GAINERS", "LOSERS", "ACTIVE"):
        S._heat_cache.pop(k, None)
    yield S
    for k in ("GAINERS", "LOSERS", "ACTIVE"):
        S._heat_cache.pop(k, None)


def test_gainers_heatmap_from_scanner(srv, monkeypatch):
    rows = [NS(rank=0, contractDetails=NS(contract=_stk("AAA", 1), longName="Alpha")),
            NS(rank=1, contractDetails=NS(contract=_stk("BBB", 2), longName="")),
            NS(rank=2, contractDetails=NS(contract=NS(secType="OPT", conId=3, symbol="X"), longName=""))]   # not a stock → skipped
    fake = FakeIB(rows, {"AAA": (11.0, 10.0), "BBB": (21.0, 20.0)})
    monkeypatch.setattr(srv, "ib", fake)
    r = asyncio.run(srv.heatmap("gainers"))
    sub = fake.subs[0]
    assert sub.scanCode == "TOP_PERC_GAIN" and sub.locationCode == "STK.US.MAJOR" and sub.numberOfRows == SCAN_ROWS
    assert sub.abovePrice >= 5 and sub.aboveVolume >= 100_000
    assert [i["symbol"] for i in r["items"]] == ["AAA", "BBB"]               # +10% before +5%
    assert r["items"][0]["name"] == "Alpha" and r["items"][0]["weight"] == 10.0
    assert sorted(fake.cancelled) == ["AAA", "BBB"] and r["breadth"]["up"] == 2


def test_market_card_shows_scan_leader(srv, monkeypatch):
    monkeypatch.setattr(srv, "GROUPS", [])
    srv._heat_cache["GAINERS"] = (time.time(), [{"key": "STK:AAA", "symbol": "AAA", "last": 11.0, "prev_close": 10.0,
                                                 "change_pct": 10.0, "decimals": 2, "weight": 10.0}])
    cards = {c["id"]: c for c in asyncio.run(srv.market())["items"]}
    assert cards["GAINERS"]["symbol"] == "AAA" and cards["GAINERS"]["change_pct"] == 10.0 and cards["GAINERS"]["members"] == 1
    assert cards["LOSERS"]["last"] == 0.0 and cards["LOSERS"]["members"] == SCAN_ROWS        # not scanned yet


def test_scanner_empty_is_an_error(srv, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(srv, "ib", FakeIB([], {}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(srv.heatmap("active"))
    assert e.value.status_code == 502


def test_fixed_group_reports_missing_members(srv, monkeypatch):
    from fastapi import HTTPException
    g = {"id": "TESTG", "name": "Test", "market": "STK", "members": [("AAA", "Alpha", 2.0), ("OLD", "Renamed", 1.0)]}
    monkeypatch.setitem(srv.GROUP_BY_ID, "TESTG", g)
    fake = FakeIB([], {"AAA": (11.0, 10.0)})
    monkeypatch.setattr(srv, "ib", fake)
    try:
        r = asyncio.run(srv.heatmap("TESTG"))
        assert fake.qualified == ["AAA", "OLD"]                                  # one batch, not one call per member
        assert [i["symbol"] for i in r["items"]] == ["AAA"] and r["missing"] == ["OLD"] and not r["cached"]
        assert asyncio.run(srv.heatmap("TESTG"))["missing"] == ["OLD"]          # cached answer keeps the list
    finally:
        srv._heat_cache.pop("TESTG", None)


def test_old_snapshot_returned_at_once_and_refreshed(srv, monkeypatch):
    g = {"id": "TESTH", "name": "Test", "market": "STK", "members": [("AAA", "Alpha", 2.0)]}
    monkeypatch.setitem(srv.GROUP_BY_ID, "TESTH", g)
    fake = FakeIB([], {"AAA": (12.0, 10.0)})
    monkeypatch.setattr(srv, "ib", fake)
    old = [{"key": "STK:AAA", "symbol": "AAA", "last": 11.0, "prev_close": 10.0, "change_pct": 10.0, "decimals": 2, "weight": 2.0}]

    async def go():
        srv._heat_cache["TESTH"] = (time.time() - 300, old)
        r = await srv.heatmap("TESTH")
        assert r["cached"] and r["refreshing"] and r["age_s"] >= 299 and r["items"] == old
        await srv._heat_tasks["TESTH"]                                           # background refresh finishes
        r2 = await srv.heatmap("TESTH")
        assert not r2["refreshing"] and r2["items"][0]["last"] == 12.0
        srv._heat_cache["TESTH"] = (time.time() - 3600, old)                     # too old: wait for a fresh one
        r3 = await srv.heatmap("TESTH")
        assert not r3["cached"] and r3["items"][0]["last"] == 12.0
    try:
        asyncio.run(go())
    finally:
        srv._heat_cache.pop("TESTH", None)


def test_heat_due_oldest_first_and_failures_wait():
    from api.server import heat_due
    now = 10_000.0
    cache = {"A": (now - 30, [1]), "B": (now - 500, [1]), "C": (now - 200, [1])}
    assert heat_due(now, cache, ["A", "B", "C", "D"], every=120) == ["D", "B", "C"]       # D never built
    assert heat_due(now, cache, ["A", "B", "C", "D"], every=120, failed={"D": now - 10}) == ["B", "C"]


def test_prefetch_builds_only_while_a_terminal_is_open(srv, monkeypatch):
    g = {"id": "TESTP", "name": "Test", "market": "STK", "members": [("AAA", "Alpha", 2.0)]}
    monkeypatch.setattr(srv, "GROUPS", [g])
    monkeypatch.setattr(srv, "SCANNERS", [])
    monkeypatch.setitem(srv.GROUP_BY_ID, "TESTP", g)
    monkeypatch.setattr(srv, "ib", FakeIB([], {"AAA": (11.0, 10.0)}))
    srv._heat_cache.pop("TESTP", None)
    calls = {"n": 0}

    async def sleep(s):                     # first sleep = start-up delay, then one loop round, then stop
        calls["n"] += 1
        if calls["n"] > 4:
            raise asyncio.CancelledError

    async def run(clients):
        monkeypatch.setattr(srv, "_ws_clients", clients)
        calls["n"] = 0
        monkeypatch.setattr(srv.asyncio, "sleep", sleep)
        try:
            await srv.heat_prefetch_loop()
        except asyncio.CancelledError:
            pass
    try:
        asyncio.run(run(set()))
        assert "TESTP" not in srv._heat_cache                                   # nobody connected: nothing fetched
        asyncio.run(run({object()}))
        assert srv._heat_cache["TESTP"][1][0]["last"] == 11.0
    finally:
        srv._heat_cache.pop("TESTP", None)


def test_member_with_its_own_market(srv, monkeypatch):
    g = {"id": "TESTV", "name": "Vol", "market": "STK", "members": [("IDX:VIX", "VIX Index", 2.0), ("VXX", "VIX ETN", 1.0)]}
    monkeypatch.setitem(srv.GROUP_BY_ID, "TESTV", g)
    fake = FakeIB([], {"VIX": (24.0, 25.0), "VXX": (40.0, 41.0)})
    monkeypatch.setattr(srv, "ib", fake)
    try:
        r = asyncio.run(srv.heatmap("TESTV"))
        assert {i["key"] for i in r["items"]} == {"IDX:VIX", "STK:VXX"} and r["missing"] == []
        assert {i["symbol"] for i in r["items"]} == {"VIX", "VXX"}
    finally:
        srv._heat_cache.pop("TESTV", None)
