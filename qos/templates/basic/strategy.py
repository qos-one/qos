"""$label — EMA crossover: long when the fast EMA is above the slow EMA, short when below"""
from qos.sdk import Param, Strategy


class $Class(Strategy):
    name = "$name"
    version = "0.1.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"
    summary = "EMA {fast}/{slow}"

    fast = Param(10, min=2, max=100, grid=[10, 20], help="fast EMA period (bars)")
    slow = Param(30, min=5, max=300, grid=[30, 50], help="slow EMA period (bars)")

    def on_configure(self):
        if self.p.fast >= self.p.slow:
            raise ValueError("fast must be smaller than slow")

    def on_start(self):
        self.fast_ema = self.indicator("ema", self.p.fast)
        self.slow_ema = self.indicator("ema", self.p.slow)

    def on_bar(self, bar):
        if self.ready():
            self.target(+1 if self.fast_ema.value > self.slow_ema.value else -1)
