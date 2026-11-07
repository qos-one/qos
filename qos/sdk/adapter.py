"""adapter: QXBaseStrategy (Nautilus) that wraps a qos.sdk.Strategy instance"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide

from qx_strategies.base import QXBaseStrategy

from . import sandbox
from .params import ParamValues
from .strategy import INDICATORS


def make_adapter(cls, Config):
    class SdkAdapter(QXBaseStrategy):
        SDK_CLASS = cls

        def __init__(self, config) -> None:
            super().__init__(config)
            self.user = cls()
            self.user._ctx = self
            values = {p.name: getattr(config, p.name) for p in cls.params()}
            for p in cls.params():                           # value ranges from Param (like the validation in built-in strategies' __init__)
                v = values[p.name]
                if p.type in ("int", "float") and ((p.min is not None and v < p.min) or (p.max is not None and v > p.max)):
                    raise ValueError(f"{p.name}={v} outside [{p.min}, {p.max}]")
            self.user.p = ParamValues(values)
            self.user.state = {}
            self._bar: Bar | None = None
            self._n_actions = 0
            self._state_path = Path(config.state_file) if config.state_file else None
            self._state_snapshot = "{}"
            self._n_indicators = 0
            try:
                import inspect
                pkg_dir = Path(inspect.getfile(cls)).resolve().parent
            except (TypeError, OSError):
                pkg_dir = None
            self._allowed = sandbox.default_allowed(pkg_dir, self._state_path)      # step 22: may write only to the package folder + strategy_state
            self.user.on_configure()                       # validate parameter relationships (raises ValueError)

        # ---------- lifecycle (QXBaseStrategy) ----------
        def setup(self) -> None:
            self._load_state()
            with sandbox.scope(self._allowed):
                self.user.on_start()
            if cls.timer_interval_s > 0:
                self.clock.set_timer("qos_sdk_timer", timedelta(seconds=float(cls.timer_interval_s)), callback=self._on_timer)

        def reset_state(self) -> None:
            self.user.state = {}
            self._bar = None

        def on_bar(self, bar: Bar) -> None:
            self._bar = bar
            self._n_actions = len(self.actions)
            with sandbox.scope(self._allowed):
                self.user.on_bar(bar)
            self._save_state()

        def on_stop(self) -> None:
            try:
                self.clock.cancel_timer("qos_sdk_timer")
            except Exception:
                pass
            try:
                with sandbox.scope(self._allowed):
                    self.user.on_stop()
            finally:
                self._save_state(force=True)
                super().on_stop()

        def _on_timer(self, _event=None) -> None:
            with sandbox.scope(self._allowed):
                self.user.on_timer(float(cls.timer_interval_s))
            self._save_state()

        # ---------- services for the SDK ----------
        def sdk_target(self, side: int) -> bool:
            if self._bar is None:
                raise RuntimeError("target() may only be called from on_bar()")
            n = len(self.actions)
            self.go_to(max(-1, min(1, side)), self._bar)
            return len(self.actions) > n

        def _level(self, qty: float) -> int:
            """Value recorded in actions: number of layers (qty / trade_size) if it is a whole multiple, otherwise the side"""
            unit = float(self.config.trade_size)
            lv = qty / unit if unit else 0.0
            return int(round(lv)) if abs(lv - round(lv)) < 1e-9 else (qty > 0) - (qty < 0)

        def sdk_target_qty(self, qty: float) -> bool:
            if self._bar is None:
                raise RuntimeError("target_qty() may only be called from on_bar()")
            bar = self._bar
            if qty < 0 and not self.config.allow_short:
                qty = 0.0
            cur = self.net()
            want_side = (qty > 0) - (qty < 0)
            cur_side = (cur > 0) - (cur < 0)
            if want_side == 0:
                return self.sdk_target(0)
            if cur_side != want_side:
                if self.market_closed():
                    self._blocked(bar, "market closed — no orders until the FX open (Sunday 17:00 New York)")
                    return False
                if cur_side != 0:
                    self.cancel_all_orders(self.config.instrument_id)
                    self.close_all_positions(self.config.instrument_id)
                if self._enter(OrderSide.BUY if want_side > 0 else OrderSide.SELL, bar, qty_value=abs(qty)):
                    self.actions.append((bar.ts_event, self._level(qty)))
                    return True
                if cur_side != 0:
                    self.actions.append((bar.ts_event, 0))
                    return True
                return False
            delta = abs(qty) - abs(cur)
            if abs(delta) < 1e-9:
                return False
            if self.market_closed():
                self._blocked(bar, "market closed — no orders until the FX open (Sunday 17:00 New York)")
                return False
            if delta > 0:
                if not self._enter(OrderSide.BUY if want_side > 0 else OrderSide.SELL, bar, qty_value=delta, brackets=False):
                    return False
            else:
                self.reduce(OrderSide.SELL if want_side > 0 else OrderSide.BUY, -delta)
            self.actions.append((bar.ts_event, self._level(qty)))
            return True

        def sdk_publish(self, name: str, data: dict) -> None:
            if self._bar is None:
                return
            self.publish_signal(self._bar, name, self._n_actions, **data)

        def sdk_indicator(self, kind: str, *args):
            import nautilus_trader.indicators as ind
            if kind not in INDICATORS:
                raise ValueError(f"unknown indicator '{kind}' (have: {', '.join(INDICATORS)})")
            obj = getattr(ind, INDICATORS[kind])(*args)
            self.register_indicator_for_bars(self.config.bar_type, obj)
            self._n_indicators += 1
            return obj

        def sdk_ready(self) -> bool:
            return True if self._n_indicators == 0 else self.indicators_initialized()

        def sdk_bars(self, n: int) -> list:
            bars = self.cache.bars(self.config.bar_type)            # newest → oldest
            return list(reversed(bars[:n]))

        # ---------- state ----------
        def _load_state(self) -> None:
            if self._state_path and self._state_path.exists():
                try:
                    self.user.state = json.loads(self._state_path.read_text())
                    self._state_snapshot = json.dumps(self.user.state, sort_keys=True, default=str)
                except Exception as e:
                    self.log.warning(f"state file unreadable, starting empty: {e}")

        def _save_state(self, force: bool = False) -> None:
            if not self._state_path:
                return
            snap = json.dumps(self.user.state, sort_keys=True, default=str)
            if snap == self._state_snapshot and not force:
                return
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(snap)
            self._state_snapshot = snap

    SdkAdapter.__name__ = SdkAdapter.__qualname__ = cls.__name__ if cls.__name__.startswith("QX") else f"QX{cls.__name__}"   # strategy_id = QX<Class>-<tag>
    return SdkAdapter
