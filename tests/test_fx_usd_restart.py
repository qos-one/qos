"""USDxxx positions after an IB Gateway restart. IBKR's virtual CASH position for USDJPY resets to zero on every restart
and then counts only new trades, so it can show -20,000 while the account is flat. Fake IB only, no network."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

import api.history as h

T0 = datetime.now(timezone.utc) - timedelta(hours=6)


class FakeIB:
    def __init__(self, positions=(), values=()):
        self._pos, self._vals = list(positions), list(values)

    def isConnected(self): return True
    def managedAccounts(self): return ["DU1"]
    def positions(self): return self._pos
    async def reqPositionsAsync(self): return self._pos
    def portfolio(self): return []
    def accountValues(self, acct=""): return self._vals


def _pos(sym, qty, avg=0.0):
    return NS(account="DU1", position=qty, avgCost=avg,
              contract=NS(secType="CASH", localSymbol=f"{sym[:3]}.{sym[3:]}", symbol=sym[:3], currency=sym[3:]))


def _cash(ccy, value):
    return NS(tag="$LEDGER-CashBalance", value=str(value), currency=ccy, account="DU1")


def _fill(key, side, qty, price, ts, source="node:breakout_usdjpy", ref=""):
    con = h.connect()
    h._insert_fill(con, {"exec_id": f"e{ts.timestamp()}{side}{qty}", "ts": ts.isoformat(timespec="seconds"), "key": key,
                         "symbol": key.split(":")[1], "side": side, "qty": qty, "price": price, "commission": 0.0,
                         "realized_pnl": 0.0, "order_id": None, "order_ref": ref, "account": "DU1",
                         "node": source[5:] if source.startswith("node:") else "", "strategy": "", "source": source})
    con.commit()
    con.close()
    h.rebuild_trades()


@pytest.fixture
def srv(tmp_path, monkeypatch):
    from api import server as S
    monkeypatch.setattr(h, "DB_FILE", tmp_path / "h.db")
    h._node_cache.clear()
    monkeypatch.setattr(S, "_tickers", {"FX:USDJPY": NS(bid=150.0, ask=150.0, last=150.0, close=150.0, marketPrice=lambda: 150.0)})
    monkeypatch.setattr(S, "_contracts", {})
    monkeypatch.setattr(S, "load_session", lambda: {"account_id": "DU1"})
    monkeypatch.setattr(S, "active_account", lambda: "DU1")
    monkeypatch.setattr(S, "_reconcile_positions", lambda rows: None)
    monkeypatch.setattr(S, "_usd_rates", lambda: {})
    monkeypatch.setattr(S.nm, "nodes", {})
    monkeypatch.setattr(S.bus, "nodes", {})

    async def ok():
        return None
    monkeypatch.setattr(S, "ensure_connected", ok)
    S._cash_warn_sent.clear()
    return S


def _rows(srv):
    return {r["key"]: r for r in asyncio.run(srv.positions())["positions"]}


def test_flat_after_restart_ignores_reset_virtual_position(srv, monkeypatch):
    _fill("FX:USDJPY", "BUY", 20000, 150.0, T0)                     # long opened before the restart
    _fill("FX:USDJPY", "SELL", 20000, 150.02, T0 + timedelta(hours=1))   # closed after it: virtual shows -20,000
    monkeypatch.setattr(srv, "ib", FakeIB([_pos("USDJPY", -20000, 150.02)], [_cash("JPY", -450)]))
    assert srv.position_qty("FX:USDJPY") == 0.0
    assert "FX:USDJPY" not in _rows(srv)


def test_open_long_after_restart_from_cash_and_books(srv, monkeypatch):
    _fill("FX:USDJPY", "BUY", 20000, 150.0, T0)
    monkeypatch.setattr(srv, "ib", FakeIB([], [_cash("JPY", -3_000_000)]))     # virtual position gone after restart
    assert srv.position_qty("FX:USDJPY") == 20000.0
    assert _rows(srv)["FX:USDJPY"]["position"] == 20000.0


def test_virtual_position_kept_while_it_matches_books(srv, monkeypatch):
    _fill("FX:USDJPY", "BUY", 20000, 150.0, T0)
    monkeypatch.setattr(srv, "ib", FakeIB([_pos("USDJPY", 20000, 150.0)], [_cash("JPY", -3_001_000)]))
    assert srv.position_qty("FX:USDJPY") == 20000.0
    row = _rows(srv)["FX:USDJPY"]
    assert row["position"] == 20000.0 and row.get("origin") != "cash_balance"


def test_virtual_wrong_after_restart_uses_books(srv, monkeypatch):
    _fill("FX:USDJPY", "BUY", 40000, 150.0, T0)
    _fill("FX:USDJPY", "SELL", 20000, 150.0, T0 + timedelta(hours=1))    # after a restart virtual counts only this sell
    monkeypatch.setattr(srv, "ib", FakeIB([_pos("USDJPY", -20000, 150.0)], [_cash("JPY", -3_000_000)]))
    assert srv.position_qty("FX:USDJPY") == 20000.0
    row = _rows(srv)["FX:USDJPY"]
    assert row["position"] == 20000.0 and row["origin"] == "cash_balance" and row["ib_virtual_qty"] == -20000.0


def test_cash_not_updated_yet_after_a_close(srv, monkeypatch):
    """IBKR updates CashBalance about every 3 minutes: right after a close the books (0) win over stale cash (+20,000)"""
    _fill("FX:USDJPY", "BUY", 20000, 150.0, T0)
    _fill("FX:USDJPY", "SELL", 20000, 150.0, datetime.now(timezone.utc) - timedelta(seconds=30))
    monkeypatch.setattr(srv, "ib", FakeIB([_pos("USDJPY", -20000, 150.0)], [_cash("JPY", -3_000_000)]))
    assert srv.position_qty("FX:USDJPY") == 0.0
    assert "FX:USDJPY" not in _rows(srv)


def test_trade_outside_qos_still_shows_cash_value(srv, monkeypatch):
    monkeypatch.setattr(srv, "ib", FakeIB([], [_cash("JPY", -1_500_000)]))       # no books: the cash balance is the position
    assert srv.position_qty("FX:USDJPY") == 10000.0


def test_no_balance_data_keeps_virtual(srv, monkeypatch):
    monkeypatch.setattr(srv, "ib", FakeIB([_pos("USDJPY", 20000, 150.0)], []))
    assert srv.position_qty("FX:USDJPY") == 20000.0


def test_xxxusd_unchanged(srv, monkeypatch):
    monkeypatch.setattr(srv, "ib", FakeIB([_pos("EURUSD", 0.0)], [_cash("EUR", 20000.4)]))
    _fill("FX:EURUSD", "BUY", 20000, 1.10, T0, source="manual")
    assert srv.position_qty("FX:EURUSD") == 20000.0


def test_last_fill_ts_skips_adjustments(srv):
    _fill("FX:USDJPY", "BUY", 20000, 150.0, T0)
    _fill("FX:USDJPY", "SELL", 20000, None, datetime.now(timezone.utc), source="adjustment", ref="adjustment")
    assert abs((h.last_fill_ts("DU1", "FX:USDJPY") - T0).total_seconds()) < 2
