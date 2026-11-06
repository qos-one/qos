# qos.sdk — write a QOS strategy in one file

```python
from qos.sdk import Strategy, Param

class VolBreakout(Strategy):
    """Enter when price breaks the 20-bar high with rising ATR"""
    name = "vol_breakout"            # a-z 0-9 _ (used as the source in history)
    label = "Volatility Breakout"
    version = "1.0.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"                 # 1m 5m 15m 30m 1h

    lookback = Param(20, min=5, max=200, grid=[10, 20, 40])     # → form in the Terminal + walk-forward
    atr_k = Param(1.5, min=0.5, max=4.0, step=0.1, help="ATR multiple")

    def on_start(self):
        self.atr = self.indicator("atr", 14)
        self.state.setdefault("highs", [])

    def on_bar(self, bar):
        if not self.ready():
            return
        close = float(bar.close)
        highs = [float(b.high) for b in self.bars(self.p.lookback)]
        if close > max(highs[:-1] or [close]) and self.atr.value > self.p.atr_k * 0.0001:
            self.target(+1)
            self.publish("breakout_up", close=close, atr=self.atr.value)
        elif close < min(float(b.low) for b in self.bars(self.p.lookback)[:-1] or [bar]):
            self.target(-1)
```

Run a backtest right away:

```python
from qx_strategies import build_strategy, backtest
from qos.sdk import default_config
cfg = default_config(VolBreakout, trade_size="20000", sl_pips=15, tp_pips=30)
build_strategy(cfg, strategy_cls=VolBreakout)
print(backtest.run_backtest(cfg, start="2026-07-01", end="2026-10-01"))
```

## What developers do **not** have to do themselves (QOS handles it)

- Order submission, bracket SL/TP (the node's `sl_pips` / `tp_pips`), position size (`trade_size`), `allow_short`
- Account risk limits, kill switch, news pause, reduce-only when halted
- Closing positions and cancelling orders when the node stops, broker reconnects, reconciliation
- Events to the Terminal (order/fill/position/heartbeat), attribution in history, paper soak
- Saving `self.state` to disk and loading it back when the node restarts

## Contract

| hook | when |
|---|---|
| `on_start()` | Once when the node starts — create indicators / load models |
| `on_bar(bar)` | Every `timeframe` bar — make decisions here only |
| `on_stop()` | When the node stops |
| `on_timer(interval_s)` | Every `timer_interval_s` seconds, if set > 0 |

| Service | Purpose |
|---|---|
| `self.target(+1 / 0 / -1)` | Move to the target side (closes the current side first) — only callable in `on_bar` |
| `self.target_qty(qty)` | Hold qty units (+ long / − short / 0 flat), partial increase/decrease allowed |
| `self.position()` | Net position of this strategy |
| `self.publish(name, **data)` | Signal to the Terminal and soak |
| `self.indicator("ema" \| "sma" \| "rsi" \| "atr" \| "bollinger", *args)` | Indicator updated from bars automatically (call in `on_start`) |
| `self.ready()` | Indicator warm-up complete |
| `self.bars(n)` | Last n bars, oldest → newest |
| `self.p.<name>` | Parameter values (read-only, set via the node config) |
| `self.state` | dict that persists across restarts (JSON values only) |
| `self.log.info(...)` | Node log |

Restrictions: no network in `on_bar`, no file writes outside `self.state`, no attributes named `submit_order` / `portfolio` / `cache` / `clock` / `msgbus` (validate rejects them)

`qos.sdk.validate(cls)` returns a list of errors · `qos.sdk.manifest(cls)` returns the metadata used by the Terminal and walk-forward
