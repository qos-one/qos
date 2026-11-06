"""Example 1: EMA crossover on 1-minute bars (trend following)"""
from .base import Agent, AgentContext, ParamSpec, TimeframeParam, ema


class EmaCrossAgent(Agent):
    id = "ema_cross"
    name = "EMA Cross"
    description = "Long when fast EMA crosses above slow EMA, short (or flat) when it crosses below. Evaluates on each new bar of the selected timeframe."
    interval_s = 10
    params = [
        ParamSpec("key", "Instrument", "instrument", "FX:EURUSD"),
        TimeframeParam("1m"),
        ParamSpec("fast", "Fast EMA", "number", 9, 2, 100, 1),
        ParamSpec("slow", "Slow EMA", "number", 21, 3, 400, 1),
        ParamSpec("qty", "Quantity", "number", 20000, 0, 1e7, 1000),
        ParamSpec("allow_short", "Allow short", "bool", True),
    ]

    async def step(self, ctx: AgentContext) -> None:
        key = self.p("key")
        bars = await ctx.bars(key)
        slow = int(self.p("slow"))
        if len(bars) < slow + 2:
            self.stats["last_signal"] = f"waiting for bars ({len(bars)}/{slow + 2})"
            return
        last_t = bars[-1]["t"]
        if self.state.get("last_bar") == last_t:
            return  # no new bar yet
        self.state["last_bar"] = last_t

        closes = [b["c"] for b in bars]
        f = ema(closes, int(self.p("fast")))
        s = ema(closes, slow)
        cross_up = f[-2] <= s[-2] and f[-1] > s[-1]
        cross_dn = f[-2] >= s[-2] and f[-1] < s[-1]
        pos = ctx.position(key)
        qty = float(self.p("qty"))
        self.stats["last_signal"] = f"fast {f[-1]:.5f} / slow {s[-1]:.5f} / pos {pos:g}"

        if cross_up and pos <= 0 and self.can_trade():
            ctx.log(f"cross UP at {closes[-1]}")
            await ctx.submit(key, "BUY", qty + abs(pos))
            self.mark_traded()
        elif cross_dn and pos >= 0 and self.can_trade():
            ctx.log(f"cross DOWN at {closes[-1]}")
            target = (qty + pos) if self.p("allow_short") else pos
            if target > 0:
                await ctx.submit(key, "SELL", target)
                self.mark_traded()
