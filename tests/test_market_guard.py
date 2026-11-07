"""Live strategies must not send orders while FX is closed, and must not stack a new entry on an unfilled one.
(2026-11-06: a node re-sent SELL 20,000 EUR/USD every 5 minutes after the Friday close — 15 orders waited at IBKR.)
Backtests are unaffected: the market-hours check only runs with the live risk feed."""
from types import SimpleNamespace as NS

import pandas as pd
import pytest
from nautilus_trader.model.enums import OrderSide, OrderType
from nautilus_trader.model.identifiers import InstrumentId

from qx_strategies import base, risk
from qx_strategies.base import QXBaseStrategy, fx_market_open


def ns(ts: str) -> int:
    return pd.Timestamp(ts, tz="America/New_York").value


@pytest.mark.parametrize("ts, is_open", [
    ("2026-11-06 16:59", True),       # Friday before the close (EST)
    ("2026-11-06 17:00", False),      # Friday close
    ("2026-11-07 10:00", False),      # Saturday
    ("2026-11-08 16:59", False),      # Sunday before the open
    ("2026-11-08 17:00", True),       # Sunday open
    ("2026-11-09 03:00", True),       # Monday
    ("2026-07-10 17:30", False),      # Friday after the close in summer time (EDT)
    ("2026-07-12 17:05", True),       # Sunday open in summer time
])
def test_fx_hours(ts, is_open):
    assert fx_market_open(ns(ts)) is is_open


class Fake:
    """Just enough of a QXBaseStrategy for the guard methods"""
    def __init__(self, now_ns, orders=(), venue="IDEALPRO"):
        self.config = NS(instrument_id=InstrumentId.from_str(f"EUR/USD.{venue}"), trade_size=20000, sl_pips=0.0, tp_pips=0.0)
        self.id = "QXTest-001"
        self.clock = NS(timestamp_ns=lambda: now_ns)
        self.cache = NS(orders_open=lambda **k: list(orders), orders_inflight=lambda **k: [])
        self.instrument = NS(make_qty=lambda v: v)
        self.published, self.submitted, self.warnings = [], [], []
        self.msgbus = NS(publish=lambda topic, msg: self.published.append(msg))
        self.log = NS(warning=self.warnings.append)
        self.submit_order = self.submitted.append
        self.order_factory = NS(market=lambda *a, **k: ("MKT", a, k))
    market_closed = QXBaseStrategy.market_closed
    stale_entries = QXBaseStrategy.stale_entries
    _blocked = QXBaseStrategy._blocked
    _enter = QXBaseStrategy._enter
    risk_ok = lambda self, *a: True
    package_ok = lambda self, *a: True
    portfolio_ok = lambda self, *a: True
    net = lambda self: 0.0


def order(cid, ts, otype=OrderType.MARKET, reduce_only=False):
    return NS(client_order_id=NS(value=cid), order_type=otype, is_reduce_only=reduce_only, ts_init=ts)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(risk.STATE, "require_sync", True)
    monkeypatch.setattr(base.package_limits.LIMITS, "commit", lambda *a: None)
    risk.STATE.blocked.clear()


def test_live_entry_refused_when_fx_closed(live):
    f = Fake(ns("2026-11-06 17:10"))
    assert f._enter(OrderSide.SELL, NS(ts_event=ns("2026-11-06 17:10"), close=1.11)) is False
    assert not f.submitted and f.published[0]["reason"].startswith("market closed")


def test_backtest_never_blocked_by_hours(monkeypatch):
    monkeypatch.setattr(risk.STATE, "require_sync", False)
    monkeypatch.setattr(base.package_limits.LIMITS, "commit", lambda *a: None)
    f = Fake(ns("2026-11-07 10:00"))
    assert f._enter(OrderSide.SELL, NS(ts_event=ns("2026-11-07 10:00"), close=1.11)) is True and len(f.submitted) == 1


def test_stocks_and_other_venues_not_blocked(live):
    assert Fake(ns("2026-11-07 10:00"), venue="SMART").market_closed() is False


def test_entry_refused_while_an_earlier_entry_waits(live):
    t = ns("2026-11-09 10:05")
    f = Fake(t, [order("O-1", t - 300 * 10**9)])
    assert f._enter(OrderSide.SELL, NS(ts_event=t, close=1.11)) is False
    assert not f.submitted and "order pending" in f.published[0]["reason"] and "O-1" in f.published[0]["reason"]


@pytest.mark.parametrize("o", [
    order("O-close", 0, reduce_only=True),                 # closing order of a reversal
    order("O-sl", 0, otype=OrderType.STOP_MARKET),        # bracket stop leg of the open position
    order("O-now", None),                                  # sent on this same bar (ts_init filled in below)
])
def test_orders_that_do_not_block(live, o):
    t = ns("2026-11-09 10:05")
    if o.ts_init is None:
        o.ts_init = t
    f = Fake(t, [o])
    assert f._enter(OrderSide.BUY, NS(ts_event=t, close=1.11)) is True and len(f.submitted) == 1
