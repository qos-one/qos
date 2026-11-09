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


# ---------- on_stop ----------
class StopFake(Fake):
    def __init__(self, now_ns, net):
        super().__init__(now_ns)
        self._net = net
        self.config = NS(**vars(self.config), close_positions_on_stop=True, bar_type="EUR/USD.IDEALPRO-5-MINUTE-MID-INTERNAL")
        self.calls = []
        self.cancel_all_orders = lambda iid: self.calls.append("cancel")
        self.close_all_positions = lambda iid: self.calls.append("close")
        self.unsubscribe_bars = lambda bt: self.calls.append("unsub")
    net = lambda self: self._net
    on_stop = QXBaseStrategy.on_stop


def test_stop_keeps_position_when_fx_closed(live):
    f = StopFake(ns("2026-11-07 10:00"), -20000.0)
    f.on_stop()
    assert f.calls == ["cancel", "unsub"] and f.published[0]["event"] == "kept_on_stop" and "market closed" in f.warnings[0]


def test_stop_closes_position_when_market_open(live):
    f = StopFake(ns("2026-11-09 10:00"), -20000.0)
    f.on_stop()
    assert f.calls == ["cancel", "close", "unsub"] and not f.published


def test_backtest_stop_always_closes(monkeypatch):
    monkeypatch.setattr(risk.STATE, "require_sync", False)
    f = StopFake(ns("2026-11-07 10:00"), 20000.0)
    f.on_stop()
    assert "close" in f.calls


# ---------- SDK timeframe 1h · qos --version ----------
def test_sdk_accepts_1h():
    from qos.sdk import strategy as s
    from qos.sdk.catalog import TF_MINUTES
    assert s.TIMEFRAMES["1h"] == 60 and TF_MINUTES["1h"] == 60


def test_hour_bars_use_nautilus_hour_type():
    """Nautilus rejects "60-MINUTE"; one hour must be a 1-HOUR bar type (backtest, catalog and live aggregation)"""
    from nautilus_trader.model.data import BarType
    from qx_strategies import bar_type_str
    assert bar_type_str("EUR/USD.IDEALPRO", 60, "INTERNAL") == "EUR/USD.IDEALPRO-1-HOUR-MID-INTERNAL"
    assert bar_type_str("EUR/USD.IDEALPRO", 30) == "EUR/USD.IDEALPRO-30-MINUTE-MID-EXTERNAL"
    BarType.from_str(bar_type_str("EUR/USD.IDEALPRO", 60))
    from qos.sdk.testing import synthetic_bars
    assert len(synthetic_bars("EUR/USD.IDEALPRO", 60, n=100)) == 100


def test_cli_version(capsys):
    from qos import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip().startswith("qos 0.1.")
