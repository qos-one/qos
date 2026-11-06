"""
EMA Cross — fast EMA crossing above slow EMA = Long, crossing below = Short (matches the legacy agent api/agents/ema_cross.py)

- enters only on an actual "cross" · opposite position open → close it first, then open the new side · allow_short=False → a downward cross only closes the Long
"""
from __future__ import annotations

from nautilus_trader.config import PositiveInt
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar

from .base import P, QXBaseConfig, QXBaseStrategy

NAME, LABEL = "ema_cross", "EMA Cross"
PARAMS = [
    P("fast", "Fast EMA", "int", 10, 2, 200, 1, field="fast_ema_period"),
    P("slow", "Slow EMA", "int", 30, 3, 400, 1, field="slow_ema_period"),
]

GRID = {"fast": [10, 20], "slow": [30, 50], "sl_tp": [[15, 30], [0, 0]]}          # parameter sets walk-forward tries by default


def summary(c: dict) -> str:
    return f"EMA {c.get('fast', 10)}/{c.get('slow', 30)}"


class QXEmaCrossConfig(QXBaseConfig, frozen=True):
    fast_ema_period: PositiveInt = 10
    slow_ema_period: PositiveInt = 30


class QXEmaCross(QXBaseStrategy):
    def __init__(self, config: QXEmaCrossConfig) -> None:
        if config.fast_ema_period >= config.slow_ema_period:
            raise ValueError(f"fast ({config.fast_ema_period}) must be < slow ({config.slow_ema_period})")
        super().__init__(config)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        self._side: int = 0            # previous EMA side: +1 fast>slow, −1 fast<slow, 0 unknown

    def setup(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)

    def reset_state(self) -> None:
        self.fast_ema.reset()
        self.slow_ema.reset()
        self._side = 0

    def on_bar(self, bar: Bar) -> None:
        if not self.indicators_initialized() or bar.is_single_price():
            return
        f, s = self.fast_ema.value, self.slow_ema.value
        side = 1 if f > s else -1 if f < s else 0
        prev, self._side = self._side, side or self._side
        if side == 0 or prev == 0 or side == prev:
            return
        n = len(self.actions)
        self.go_to(side, bar)
        self.publish_signal(bar, "cross_up" if side > 0 else "cross_down", n, fast=round(f, 6), slow=round(s, 6))


Config, Strategy = QXEmaCrossConfig, QXEmaCross
