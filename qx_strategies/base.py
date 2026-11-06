"""
Common base for every Strategy in qx_strategies (used in both backtest and live)

Every Strategy gets:
- risk check before opening a new position (risk.check_entry — same limits as the API) and an "Entry blocked" event
- bracket SL/TP (sl_pips / tp_pips > 0) · allow_short · close positions on stop
- go_to(target): move to target position +1 / 0 / −1 (close the current side first, then open the new one)
- actions [(ts, value)] for parity checks against the legacy agent · closed (from PositionClosed) · signals to message bus → Redis → Terminal
- PARAMS per Strategy (config key, type, range) → the Terminal form is built from these
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import PositionClosed
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy

from . import fx, package_limits, portfolio, risk

CASTS = {"int": int, "float": float, "str": str, "bool": bool}


def P(key: str, label: str, typ: str, default, lo=None, hi=None, step=None, field: str | None = None, help: str = "") -> dict:
    """One parameter definition: key = config JSON key, field = Config attribute name (if different)"""
    return {"key": key, "field": field or key, "label": label, "type": typ, "default": default,
            "min": lo, "max": hi, "step": step, "help": help}


def pip_size(instrument_id: InstrumentId) -> float:
    return 0.01 if str(instrument_id.symbol).endswith("JPY") else 0.0001


class QXBaseConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    sl_pips: float = 0.0
    tp_pips: float = 0.0
    allow_short: bool = True
    warmup_days: int = 0
    close_positions_on_stop: bool = True

    @classmethod
    def from_dict(cls, d: dict, source: str = "EXTERNAL", params: list[dict] | None = None):
        from . import bar_type_str
        inst = d["instrument"]
        kw = dict(
            strategy_id=d.get("strategy_id"),
            order_id_tag=str(d.get("order_id_tag", "001")),
            instrument_id=InstrumentId.from_str(inst),
            bar_type=BarType.from_str(bar_type_str(inst, d.get("tf_minutes", 5), source)),
            trade_size=Decimal(str(d.get("trade_size", "20000"))),
            sl_pips=float(d.get("sl_pips", 0)),
            tp_pips=float(d.get("tp_pips", 0)),
            allow_short=bool(d.get("allow_short", True)),
            warmup_days=int(d.get("warmup_days", 0)),
            close_positions_on_stop=bool(d.get("close_positions_on_stop", True)),
        )
        for p in params or []:
            v = d.get(p["key"], p["default"])
            kw[p["field"]] = CASTS[p["type"]](v) if p["type"] != "bool" else bool(v)
        if "state_file" in getattr(cls, "__struct_fields__", ()) and d.get("state_file"):
            kw["state_file"] = str(d["state_file"])             # qos.sdk strategies: where self.state persists across restarts
        return cls(**kw)


class QXBaseStrategy(Strategy):
    def __init__(self, config: QXBaseConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.signals: int = 0
        self.closed: list[dict] = []
        self.actions: list[tuple[int, int]] = []

    # ---------- For subclasses to override ----------
    def setup(self) -> None:
        """Called on start (after the instrument is available) — register indicators / load models"""

    def reset_state(self) -> None:
        """Reset the subclass's internal state"""

    # ---------- lifecycle ----------
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self.config.instrument_id}")
            self.stop()
            return
        self.setup()
        if self.config.warmup_days > 0:
            self.request_bars(self.config.bar_type, start=self._clock.utc_now() - pd.Timedelta(days=self.config.warmup_days))
        self.subscribe_bars(self.config.bar_type)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        if self.config.close_positions_on_stop:
            self.close_all_positions(self.config.instrument_id)
        self.unsubscribe_bars(self.config.bar_type)

    def on_reset(self) -> None:
        self.signals = 0
        self.closed = []
        self.actions = []
        self.reset_state()

    def on_position_closed(self, event: PositionClosed) -> None:
        """Record the closed position (reporting only, no effect on decisions); pnl in USD"""
        pnl = float(event.realized_pnl.as_double())
        if event.realized_pnl.currency.code != "USD":            # USD/JPY: JPY / price · crosses (EUR/JPY): USD rate of the quote currency
            rates = risk.payload_rates(risk.STATE.limits) if risk.STATE.limits else None      # live: rates from the API
            v = fx.pnl_to_usd(str(self.config.instrument_id), pnl, float(event.avg_px_close) if event.avg_px_close else None, rates)
            pnl = v if v is not None else float("nan")           # cross pair without a USD rate: unknown, never a wrong number
        self.closed.append({"side": "LONG" if event.entry == OrderSide.BUY else "SHORT",
                            "opened": event.ts_opened, "closed": event.ts_closed,
                            "entry": event.avg_px_open, "exit": event.avg_px_close,
                            "qty": float(event.peak_qty), "pnl": pnl})

    # ---------- helpers ----------
    def net(self) -> float:
        """Net position of this Strategy (+ Long / − Short)"""
        return float(self.portfolio.net_position(self.config.instrument_id))

    def publish_signal(self, bar: Bar, name: str, n_actions_before: int, **data) -> None:
        self.signals += 1
        self.msgbus.publish(topic=f"qx.signal.{self.id}", msg={
            "strategy": str(self.id), "instrument": str(self.config.instrument_id), "ts_ns": bar.ts_event,
            "name": name, "close": float(bar.close), **data,
            "action": self.actions[-1][1] if len(self.actions) > n_actions_before else None,
        })

    def go_to(self, target: int, bar: Bar) -> None:
        """Move to the target position: +1 Long, −1 Short, 0 Flat (allow_short=False → −1 becomes 0)"""
        if target < 0 and not self.config.allow_short:
            target = 0
        net = self.net()
        cur = (net > 0) - (net < 0)
        if target == cur:
            return
        iid = self.config.instrument_id
        if cur != 0:
            self.cancel_all_orders(iid)
            self.close_all_positions(iid)
        if target == 0:
            self.actions.append((bar.ts_event, 0))
        elif self._enter(OrderSide.BUY if target > 0 else OrderSide.SELL, bar):
            self.actions.append((bar.ts_event, target))
        elif cur != 0:
            self.actions.append((bar.ts_event, 0))           # closed the old side, but the new entry failed the risk check

    def risk_ok(self, order_side: OrderSide, qty, bar: Bar) -> bool:
        ok, why = risk.check_entry(str(self.config.instrument_id), float(qty), float(bar.close), abs(self.net()))
        if ok:
            return True
        n = risk.note_block(why)
        if n == 1 or n % 20 == 0:
            self.log.warning(f"entry blocked by risk: {why}")
        self.msgbus.publish(topic=f"qx.risk.{self.id}", msg={"event": "blocked", "reason": why, "side": order_side.name, "ts_ns": bar.ts_event,
                                                            "qty": float(qty), "instrument": str(self.config.instrument_id)})
        return False

    def package_ok(self, order_side: OrderSide, qty, bar: Bar) -> bool:
        """Strategy package limits (step 22): instruments / notional / orders per hour / flips per hour"""
        L = package_limits.LIMITS
        rates = risk.payload_rates(risk.STATE.limits) if risk.STATE.limits else None      # None = backtest (no API rates)
        ok, why, kind = L.check(str(self.config.instrument_id), 1 if order_side == OrderSide.BUY else -1, float(qty), float(bar.close),
                                self.net(), bar.ts_event, rates=rates, record=False)      # counted in _enter only once every check passed
        if ok:
            return True
        self.log.warning(f"order rejected by package limit {kind}: {why}")
        self.msgbus.publish(topic=f"qx.risk.{self.id}", msg={"event": "package_limit", "limit": kind, "reason": why, "side": order_side.name,
                                                            "qty": float(qty), "price": float(bar.close), "tripped": L.tripped,
                                                            "ts_ns": bar.ts_event, "instrument": str(self.config.instrument_id)})
        return False

    def portfolio_ok(self, order_side: OrderSide, qty, bar: Bar) -> bool:
        """Portfolio-wide limits (step 16): exposure per currency, correlated groups, gross, VaR — from the API payload"""
        L = risk.STATE.limits or {}
        ok, why, kind = portfolio.check_order(L.get("portfolio"), str(self.config.instrument_id), 1 if order_side == OrderSide.BUY else -1,
                                              float(qty), float(bar.close), self.net())
        if ok:
            return True
        n = risk.note_block(why)
        if n == 1 or n % 20 == 0:
            self.log.warning(f"entry blocked by portfolio limit {kind}: {why}")
        self.msgbus.publish(topic=f"qx.risk.{self.id}", msg={"event": "portfolio_limit", "limit": kind, "reason": why, "side": order_side.name,
                                                            "qty": float(qty), "ts_ns": bar.ts_event, "instrument": str(self.config.instrument_id)})
        return False

    def _enter(self, order_side: OrderSide, bar: Bar, qty_value=None, brackets: bool = True) -> bool:
        """Open/add to a position (risk-checked) — Market, or bracket if SL/TP is set"""
        qty = self.instrument.make_qty(qty_value if qty_value is not None else self.config.trade_size)
        if not self.risk_ok(order_side, qty, bar):
            return False
        if not self.package_ok(order_side, qty, bar):
            return False
        if not self.portfolio_ok(order_side, qty, bar):
            return False
        package_limits.LIMITS.commit(1 if order_side == OrderSide.BUY else -1, self.net(), bar.ts_event)   # entries blocked above do not count
        sl, tp = (self.config.sl_pips, self.config.tp_pips) if brackets else (0.0, 0.0)
        if sl <= 0 and tp <= 0:
            self.submit_order(self.order_factory.market(self.config.instrument_id, order_side, qty, time_in_force=TimeInForce.GTC))
            return True
        px, pip = float(bar.close), pip_size(self.config.instrument_id)
        sign = 1 if order_side == OrderSide.BUY else -1
        kw = {"sl_trigger_price": self.instrument.make_price(px - sign * (sl if sl > 0 else 1000) * pip),   # a bracket needs both legs
              "tp_price": self.instrument.make_price(px + sign * (tp if tp > 0 else 1000) * pip)}
        self.submit_order_list(self.order_factory.bracket(self.config.instrument_id, order_side, qty, **kw))
        return True

    def reduce(self, order_side: OrderSide, qty_value) -> None:
        """Reduce a position partially (reduce-only — no risk check needed)"""
        qty = self.instrument.make_qty(qty_value)
        self.submit_order(self.order_factory.market(self.config.instrument_id, order_side, qty, time_in_force=TimeInForce.GTC, reduce_only=True))
