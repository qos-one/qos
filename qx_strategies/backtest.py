"""
Backtest with Nautilus's BacktestEngine on data from the data catalog (step 3) — same Strategy as live

  from qx_strategies.backtest import run_backtest
  res = run_backtest(cfg, start="2026-09-01", end="2026-10-26")
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pandas as pd

from . import bar_type_str, build_strategy, fx


class NoBarsError(ValueError):
    """No bars for the instrument / period (walk-forward treats this window as empty)"""


class UsdRateError(ValueError):
    """A cross pair's quote currency cannot be converted to USD (no XXX/USD or USD/XXX bars and no rates given)"""


def _symbol(instrument: str) -> str:
    return instrument.split(".")[0].replace("/", "")          # "EUR/USD.IDEALPRO" → "EURUSD"


def load_bars(cfg: dict, start=None, end=None, catalog_dir: Path | None = None) -> list:
    """Bars from the data catalog · timeframes missing from the catalog (e.g. 15m, 1h) are built from 1-minute bars automatically"""
    from qos.sdk import catalog as cat
    c = cat.open_catalog(catalog_dir or cat.default_dir())
    tf = int(cfg.get("tf_minutes", 5))
    s = pd.Timestamp(start, tz="UTC") if start else None
    e = pd.Timestamp(end, tz="UTC") if end else None
    bt = bar_type_str(cfg["instrument"], tf, "EXTERNAL")
    bars = c.bars(bar_types=[bt], start=s, end=e)
    if bars or tf == 1:
        return bars
    one = c.bars(bar_types=[bar_type_str(cfg["instrument"], 1, "EXTERNAL")], start=s, end=e)
    return resample_bars(one, cfg["instrument"], tf)


def load_rate_bars(ccy: str, start_ns: int, end_ns: int, catalog_dir: Path | None = None) -> list:
    """Bars of CCY/USD or USD/CCY (1m, else 5m) from the data catalog for the period — used to convert a cross pair's P&L"""
    from qos.sdk import catalog as cat
    try:
        c = cat.open_catalog(catalog_dir or cat.default_dir())
    except Exception:
        return []
    s, e = pd.Timestamp(start_ns, unit="ns", tz="UTC"), pd.Timestamp(end_ns, unit="ns", tz="UTC")
    for inst in (f"{ccy}/USD.IDEALPRO", f"USD/{ccy}.IDEALPRO"):
        for tf in (1, 5):
            try:
                got = c.bars(bar_types=[bar_type_str(inst, tf, "EXTERNAL")], start=s, end=e)
            except Exception:
                got = []
            if got:
                return got
    return []


def rate_series(ccy: str, rate_bars: list) -> list[tuple[int, float]]:
    """[(ts_ns, USD value of one CCY)] from CCY/USD (close) or USD/CCY (1 / close) bars, sorted by time"""
    out = []
    for b in rate_bars:
        base, quote = fx.split_pair(str(b.bar_type.instrument_id))
        px = float(b.close)
        if px <= 0:
            continue
        if base == ccy and quote == "USD":
            out.append((int(b.ts_event), px))
        elif base == "USD" and quote == ccy:
            out.append((int(b.ts_event), 1.0 / px))
    return sorted(out)


def _env_rates() -> dict:
    """QOS_USD_RATES (JSON {"GBP": 1.27, …}) — today's rates the API exports for research jobs it starts"""
    try:
        v = json.loads(os.environ.get("QOS_USD_RATES") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


UNPRICED_NOTE = "P&L not converted to USD (no {q}/USD bars or rates): see pnl_quote / pnl_quote_ccy — trade, drawdown and equity amounts are in {q}"
RATE_GAP_NS = 86_400_000_000_000          # rate series ending more than a day before the last bar = conversion gap


def rate_coverage_note(quote: str, series: list | None, fills: list, bars: list) -> str | None:
    """Per-bar conversion: say so when the rate series starts after the first fill or ends long before the last bar
    (those amounts were converted at the nearest available rate, not the rate of their time)"""
    if not series or not bars:
        return None
    fill_ts = [int(f.ts_event) for f in fills]
    first_fill = min(fill_ts, default=None)
    starts_late = first_fill is not None and series[0][0] > first_fill
    ends_early = series[-1][0] < int(bars[-1].ts_event) - RATE_GAP_NS
    iso = lambda ns: pd.Timestamp(ns, unit="ns", tz="UTC").isoformat(timespec="minutes")   # noqa: E731
    if starts_late or ends_early:
        return (f"{quote}/USD rates only cover {iso(series[0][0])} → {iso(series[-1][0])}: amounts outside that range were "
                f"converted at the nearest available rate")
    hole = _rate_hole(series, first_fill, max(fill_ts, default=None))
    if hole:
        return (f"{quote}/USD rates have a gap {iso(hole[0])} → {iso(hole[1])}: amounts in it were converted at the last rate "
                f"before the gap")
    return None


def _weekday_ns(a: int, b: int) -> int:
    """Nanoseconds between a and b that fall on Monday–Friday (UTC) — weekends are not rate gaps"""
    day = 86_400_000_000_000
    total, t = 0, a
    while t < b:
        nxt = min(b, (t // day + 1) * day)
        if pd.Timestamp(t, unit="ns", tz="UTC").weekday() < 5:
            total += nxt - t
        t = nxt
    return total


def _rate_hole(series: list, first_fill: int | None, last_fill: int | None) -> tuple[int, int] | None:
    """First hole > 1 day (weekends ignored) between consecutive rate points that overlaps the fill range"""
    if first_fill is None:
        return None
    for (a, _), (b, _) in zip(series, series[1:]):
        if b - a > RATE_GAP_NS and a < last_fill and b > first_fill and _weekday_ns(a, b) > RATE_GAP_NS:
            return a, b
    return None


def resolve_usd_conversion(instrument: str, bars: list, usd_rates: dict | None = None, rate_bars: list | None = None,
                           catalog_dir: Path | None = None, unpriced_ok: bool = False) -> tuple[dict | None, list | None, str | None]:
    """How to convert the pair's quote-currency P&L to USD → (static rates, per-bar rate series, note)
    USD-quoted pairs and USD/XXX need nothing. Cross pairs (EUR/GBP): usd_rates given → those · else the XXX/USD or USD/XXX
    bars of the same period (rate_bars, else the data catalog) → rate at each fill / bar · else QOS_USD_RATES · else error
    (unpriced_ok: convert at 1.0 and say so — decision-only runs such as install checks)"""
    base, quote = fx.split_pair(instrument)
    if "USD" in (base, quote) or (usd_rates and fx.usd_rate(quote, usd_rates) is not None):
        return usd_rates, None, None
    rb = rate_bars if rate_bars is not None else load_rate_bars(quote, int(bars[0].ts_event) - 86_400_000_000_000,
                                                                int(bars[-1].ts_event), catalog_dir)
    series = rate_series(quote, rb or [])
    if series:
        return None, series, None
    env = _env_rates()
    if fx.usd_rate(quote, env) is not None:
        return env, None, f"{quote} converted to USD at today's rate (no {quote}/USD bars for the period)"
    if unpriced_ok:
        return {quote: 1.0}, None, UNPRICED_NOTE.format(q=quote)
    raise UsdRateError(f"cannot convert {quote} P&L of {instrument} to USD: no {quote}/USD or USD/{quote} bars in the data catalog "
                       f"for the period — build the catalog for {quote}USD (or USD{quote}) or pass usd_rates")


def resample_bars(one_min: list, instrument: str, tf: int) -> list:
    """Aggregate 1-minute bars (close time) into tf-minute bars — drops the last incomplete bar"""
    if not one_min:
        return []
    from nautilus_trader.model.data import BarType
    from nautilus_trader.persistence.wranglers import BarDataWrangler

    from qos.sdk import catalog as cat
    df = pd.DataFrame({"open": [b.open.as_double() for b in one_min], "high": [b.high.as_double() for b in one_min],
                       "low": [b.low.as_double() for b in one_min], "close": [b.close.as_double() for b in one_min]},
                      index=pd.to_datetime([b.ts_event for b in one_min], unit="ns", utc=True))
    agg = cat.resample_closed(df, f"{tf}min")
    agg = agg[agg.index <= df.index.max()].copy()
    agg.index.name = "timestamp"
    agg["volume"] = 1_000_000.0
    return BarDataWrangler(BarType.from_str(bar_type_str(instrument, tf, "EXTERNAL")), cat.make_fx_instrument(_symbol(instrument))).process(agg)


def run_backtest(cfg: dict, start=None, end=None, bars: list | None = None, catalog_dir: Path | None = None,
                 starting_usd: float = 100_000, leverage: float = 30, log_level: str = "ERROR",
                 _collect_actions: list | None = None, actors: list | None = None, strategy=None,
                 usd_rates: dict | None = None, bar_marks: bool = False, rate_bars: list | None = None,
                 unpriced_ok: bool = False) -> dict:
    """Return a summary: bar/signal/order/position counts, PnL, win rate, max drawdown and the list of positions
    Cross pairs (no USD leg) are converted to USD automatically from the XXX/USD bars of the same period (see
    resolve_usd_conversion): usd_rates = static USD value per currency ({"GBP": 1.27}) · rate_bars = XXX/USD bars to use
    instead of the catalog · unpriced_ok = never fail on a missing rate (P&L then stays in the quote currency, see "pnl_note")
    bar_marks: also return [[bar ts_ns, open position P&L USD (incl. its fees), opened ts_ns or 0]] for every bar (forward checks)"""
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import TraderId, Venue
    from nautilus_trader.model.objects import Money

    from qos.sdk import catalog as cat

    from .package_limits import LIMITS
    if LIMITS.limits is None:
        LIMITS.reset()                                       # no limits set: reset counters from the previous run (run times are not continuous)
    else:
        LIMITS.configure(LIMITS.limits)
    sym = _symbol(cfg["instrument"])
    if bars is None:
        bars = load_bars(cfg, start, end, catalog_dir)
    if not bars:
        raise NoBarsError(f"no bars in catalog for {cfg['instrument']} {cfg.get('tf_minutes', 5)}m {start}..{end}")
    usd_rates, series, pnl_note = resolve_usd_conversion(cfg["instrument"], bars, usd_rates, rate_bars, catalog_dir, unpriced_ok)

    engine = BacktestEngine(BacktestEngineConfig(trader_id=TraderId("BACKTEST-QX"),
                                                 logging=LoggingConfig(log_level=log_level, bypass_logging=log_level == "OFF")))
    quote = cfg["instrument"].split(".")[0].split("/")[1]
    if quote == "USD":
        engine.add_venue(Venue("IDEALPRO"), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                         starting_balances=[Money(starting_usd, USD)], base_currency=USD, default_leverage=Decimal(str(leverage)))
    else:   # e.g. USD/JPY: multi-currency account — PnL accrues in JPY and is converted to USD in the summary (bars carry no FX rate to convert immediately)
        from nautilus_trader.model.objects import Currency
        engine.add_venue(Venue("IDEALPRO"), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                         starting_balances=[Money(starting_usd, USD), Money(0, Currency.from_str(quote))], base_currency=None,
                         default_leverage=Decimal(str(leverage)))
    engine.add_instrument(cat.make_fx_instrument(sym))
    engine.add_data(bars)
    if strategy is None:                                         # qos.sdk.quick_backtest passes a Strategy it built itself (bypasses the registry)
        strategy = build_strategy(cfg, source="EXTERNAL", warmup_days=0)
    engine.add_strategy(strategy)
    fills: list = []                                             # every fill of this Strategy, incl. the close at the end of the data

    def _on_order_event(e) -> None:
        if type(e).__name__ == "OrderFilled" and e.strategy_id == strategy.id:
            fills.append(e)
    engine.kernel.msgbus.subscribe(topic="events.order.*", handler=_on_order_event)
    for a in actors or []:
        if getattr(a, "risk_engine", "n/a") is None:          # QXRiskGuard: bind this backtest's RiskEngine + Strategy
            a.risk_engine = engine.kernel.risk_engine
        if hasattr(a, "strategies") and not a.strategies:
            a.strategies = [strategy]
        engine.add_actor(a)
    try:
        import warnings
        with warnings.catch_warnings():                       # raised inside Nautilus itself (Timestamp.utcnow), harmless — keeps developer test output clean
            warnings.filterwarnings("ignore", message="Timestamp.utcnow is deprecated")
            engine.run()
        if _collect_actions is not None:
            _collect_actions.extend(strategy.actions)
        out = _summary(engine, strategy, bars, starting_usd, fills, usd_rates, bar_marks, series)
        quote = fx.split_pair(cfg["instrument"])[1]
        pnl_note = pnl_note or rate_coverage_note(quote, series, fills, bars)
        if pnl_note:
            out["pnl_note"] = pnl_note
        if unpriced_ok and pnl_note == UNPRICED_NOTE.format(q=quote):     # no rate at all: never show quote-currency amounts as USD
            out.update(pnl_quote=out["pnl_usd"], pnl_quote_ccy=quote, max_drawdown_quote=out["max_drawdown_usd"],
                       pnl_usd=None, balance_usd=None, return_pct=None, open_pnl_usd=None, max_drawdown_usd=None)
        return out
    finally:
        engine.dispose()


def equity_curve(closed: list[dict], starting_usd: float) -> list[list]:
    """[[close time ISO, equity]] from closed positions (sorted by time)"""
    eq, out = starting_usd, []
    for c in sorted(closed, key=lambda c: c["closed"]):
        eq += c["pnl"]
        out.append([pd.Timestamp(c["closed"], unit="ns", tz="UTC").isoformat(), round(eq, 2)])
    return out


def replay_fills(sym: str, fills: list, bars: list, starting_usd: float, usd_rates: dict | None = None,
                 bar_marks: bool = False, rate_series_: list | None = None) -> dict:
    """Rebuild the account from the engine's fills (netting) and mark it to market at every bar close
    → closed trades (incl. positions closed at the end of the data), P&L in USD and peak-to-trough drawdown on equity incl. open P&L
    rate_series_: [(ts_ns, USD per quote unit)] for cross pairs — the rate in force at each fill / bar is used"""
    book = _NettingBook(sym=sym, rates=usd_rates)
    quote = fx.split_pair(sym)[1]
    rs, ri = rate_series_ or [], 0

    def set_rate(t: int) -> None:
        nonlocal ri
        if not rs:
            return
        while ri + 1 < len(rs) and rs[ri + 1][0] <= t:
            ri += 1
        book.rates = {quote: rs[ri][1]}                            # last rate at or before t (the first one before the series starts)
    evs = sorted(((int(f.ts_event), k, f) for k, f in enumerate(fills)), key=lambda x: (x[0], x[1]))
    j, peak, mdd, lo, hi, marks = 0, starting_usd, 0.0, 0.0, 0.0, []
    eq = starting_usd
    for b in bars:
        t = int(b.ts_event)
        set_rate(t)
        while j < len(evs) and evs[j][0] <= t:
            f = evs[j][2]
            q = float(f.last_qty)
            book.fill(evs[j][0], q if f.order_side.name == "BUY" else -q, float(f.last_px),
                      fee_quote=float(f.commission.as_double()) if f.commission is not None else 0.0)
            j += 1
        px = float(b.close)
        u = book.unrealized(px)
        eq = starting_usd + book.realized + u                     # realized already holds the fees paid on the open position
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        lo, hi = min(lo, eq - starting_usd), max(hi, eq - starting_usd)
        if bar_marks:
            marks.append([t, u + book.open_trade_pnl() if book.pos else 0.0, book.opened if book.pos else 0])
    for _, _, f in evs[j:]:                                       # fills after the last bar (should not happen)
        set_rate(int(f.ts_event))
        q = float(f.last_qty)
        book.fill(int(f.ts_event), q if f.order_side.name == "BUY" else -q, float(f.last_px),
                  fee_quote=float(f.commission.as_double()) if f.commission is not None else 0.0)
        eq = starting_usd + book.total()
        peak, mdd = max(peak, eq), max(mdd, max(peak, eq) - eq)
        lo, hi = min(lo, eq - starting_usd), max(hi, eq - starting_usd)
    open_pnl = (book.unrealized(float(bars[-1].close)) + book.open_trade_pnl()) if (bars and book.pos) else 0.0
    return {"book": book, "pnl": eq - starting_usd, "open_pnl": open_pnl,
            "mdd": mdd, "min": lo, "max": hi, "marks": marks}


def _summary(engine, strategy, bars, starting_usd, fills, usd_rates=None, bar_marks=False, series=None) -> dict:
    ts = lambda ns: pd.Timestamp(ns, unit="ns", tz="UTC").isoformat()   # noqa: E731
    rp = replay_fills(str(strategy.config.instrument_id), fills, bars, starting_usd, usd_rates, bar_marks, series)
    book = rp["book"]
    closed = book.closed
    pnls = [c["pnl"] for c in closed]
    pnl = rp["pnl"]
    eq_pts = equity_curve(closed, starting_usd)
    if book.pos:                                                   # position still open (close_positions_on_stop=False): mark at the last close
        eq_pts.append([ts(bars[-1].ts_event), round(starting_usd + pnl, 2)])
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x <= 0]
    out = {
        "strategy": str(strategy.id),
        "bar_type": str(strategy.config.bar_type),
        "from": ts(bars[0].ts_event),
        "to": ts(bars[-1].ts_event),
        "bars": len(bars),
        "signals": strategy.signals,
        "orders": len(engine.cache.orders()),
        "closed_positions": len(closed),
        "open_positions": 1 if book.pos else 0,
        "pnl_usd": round(pnl, 2),                                    # = Σ trades pnl + open_pnl_usd exactly (positions closed at the end are trades)
        "open_pnl_usd": round(rp["open_pnl"], 2),                    # open position: unrealized at the last close + fees / partial P&L already booked
        "balance_usd": round(starting_usd + pnl, 2),
        "return_pct": round(pnl / starting_usd * 100, 3),
        "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "max_drawdown_usd": round(rp["mdd"], 2),                     # peak-to-trough of equity marked to market at every bar close
        "mtm": {"min": round(rp["min"], 2), "max": round(rp["max"], 2), "end": round(pnl, 2), "mdd": round(rp["mdd"], 2)},
        "equity": eq_pts,
        "trades": [{**c, "opened": ts(c["opened"]), "closed": ts(c["closed"]), "entry": round(c["entry"], 6), "exit": round(c["exit"], 6),
                    "pnl": round(c["pnl"], 2)} for c in closed],
    }
    if bar_marks:
        out["marks"] = rp["marks"]
    return out


class _NettingBook:
    """Simulated netting account (like IBKR) — PnL in USD, 0.2 bps fees, supports partial reductions (grid)"""

    def __init__(self, usd_quote: bool | None = None, commission_rate: float = 0.00002, sym: str | None = None,
                 rates: dict | None = None):
        self.sym = sym                   # set → quote-currency amounts are converted with qx_strategies.fx (crosses need rates)
        self.rates = rates
        self.pos = 0.0
        self.avg = 0.0
        self.opened = 0
        self.usd_quote = usd_quote
        self.rate = commission_rate
        self.closed: list[dict] = []
        self.realized = 0.0              # total realized PnL (after fees)
        self._trade_pnl = 0.0            # accumulated for the current position (incl. partial reductions)
        self._peak = 0.0
        self.last_px = 0.0

    def _usd(self, x: float, px: float) -> float:
        if self.sym is None:
            return x if self.usd_quote else x / px      # legacy flag: USDJPY PnL in JPY → divide by price
        v = fx.pnl_to_usd(self.sym, x, px, self.rates)
        if v is None:
            raise ValueError(f"cannot convert {fx.split_pair(self.sym)[1]} amounts to USD for {self.sym} — pass USD rates for cross pairs")
        return v

    def fill(self, ts: int, signed_qty: float, px: float, fee_quote: float | None = None) -> None:
        """fee_quote: the actual commission in quote currency (default: commission_rate × notional)"""
        self.last_px = px
        fee = self._usd(abs(signed_qty) * px * self.rate if fee_quote is None else fee_quote, px)
        self.realized -= fee
        self._trade_pnl -= fee
        q = signed_qty
        if self.pos == 0 or (self.pos > 0) == (q > 0):          # open/add
            if self.pos == 0:
                self.opened = ts
            self.avg = (self.avg * abs(self.pos) + px * abs(q)) / (abs(self.pos) + abs(q))
            self.pos += q
            self._peak = max(self._peak, abs(self.pos))
            return
        close_qty = min(abs(q), abs(self.pos))                   # reduce/close/flip
        direction = 1 if self.pos > 0 else -1
        pnl = self._usd(direction * (px - self.avg) * close_qty, px)
        self.realized += pnl
        self._trade_pnl += pnl
        self.pos += direction * -close_qty
        rest = abs(q) - close_qty
        if self.pos == 0:
            self.closed.append({"side": "LONG" if direction > 0 else "SHORT", "opened": self.opened, "closed": ts,
                                "entry": self.avg, "exit": px, "qty": self._peak, "pnl": self._trade_pnl})
            self._trade_pnl, self._peak, self.avg = 0.0, 0.0, 0.0
        if rest > 0:
            self.pos = (1 if q > 0 else -1) * rest
            self.avg, self.opened, self._peak = px, ts, rest

    def open_trade_pnl(self) -> float:
        """P&L already booked on the open position (its fees, partial reductions) — part of realized, not yet a trade"""
        return self._trade_pnl if self.pos else 0.0

    def unrealized(self, px: float | None = None) -> float:
        px = px or self.last_px
        if not self.pos or not px:
            return 0.0
        return self._usd((px - self.avg) * self.pos, px)

    def total(self, px: float | None = None) -> float:
        """Total PnL = realized + open position (marked at the last price) — Nautilus closes positions at the end of the backtest"""
        return self.realized + self.unrealized(px)
