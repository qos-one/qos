"""
Agent framework (example automated trading system in quantX)

To add a new agent: create a file in api/agents/ with a class that inherits Agent and defines
    id, name, description, params (defaults), then implement `async def step(self, ctx)`
The system discovers the file automatically when the API starts
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


@dataclass
class ParamSpec:
    key: str
    label: str
    type: str            # "number" | "text" | "bool" | "instrument"
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type, "default": self.default,
                "min": self.min, "max": self.max, "step": self.step, "help": self.help}


class AgentContext:
    """What the agent uses to talk to the market and the account (passed in by the runner)"""

    def __init__(self, agent: "Agent", quote, bars, position, submit, now: Callable[[], float]):
        self._agent = agent
        self._quote = quote
        self._bars = bars
        self._position = position
        self._submit = submit
        self.now = now

    def quote(self, key: str) -> dict:
        return self._quote(key)

    async def bars(self, key: str, bar_size: str | None = None, duration: str | None = None) -> list[dict]:
        bs, du = self._agent.ibkr_bars()
        return await self._bars(key, bar_size or bs, duration or du)

    def position(self, key: str) -> float:
        return self._position(key)

    async def submit(self, key: str, side: str, qty: float, order_type: str = "MKT", price: float | None = None) -> dict:
        res = await self._submit(self._agent.id, key, side, qty, order_type, price)
        self._agent.stats["orders_sent"] += 1
        self._agent.stats["last_order"] = f"{side} {qty:g} {key} → {res.get('status')}"
        self._agent.log(f"ORDER {side} {qty:g} {key} → {res.get('status')} {res.get('avg_fill_price') or ''}")
        return res

    def log(self, msg: str) -> None:
        self._agent.log(msg)


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400}
TF_IBKR = {"1m": ("1 min", "1 D"), "5m": ("5 mins", "1 W"), "15m": ("15 mins", "2 W"), "30m": ("30 mins", "1 M"),
           "1h": ("1 hour", "1 M"), "4h": ("4 hours", "3 M"), "1D": ("1 day", "1 Y")}
DEFAULT_RISK = {"max_position": 0.0, "max_daily_loss": 0.0, "max_orders_per_day": 50, "max_consecutive_errors": 5}


class Agent:
    id: str = "base"
    name: str = "Base agent"
    description: str = ""
    kind: str = "python"                # python | nautilus
    interval_s: float = 10.0            # used when run_on_bar_close = False
    run_on_bar_close: bool = True       # run step right after a bar of the timeframe closes
    params: list[ParamSpec] = []

    def __init__(self) -> None:
        self.config: dict[str, Any] = {p.key: p.default for p in self.params}
        if "timeframe" not in self.config:
            self.config["timeframe"] = "1m"
        self.risk: dict[str, Any] = dict(DEFAULT_RISK)
        self.active: bool = False
        self.state: dict[str, Any] = {}
        self.stats: dict[str, Any] = {"orders_sent": 0, "runs": 0, "errors": 0, "last_run": None,
                                      "last_signal": "", "last_order": "", "last_error": "",
                                      "orders_today": 0, "day": "", "consecutive_errors": 0, "stop_reason": ""}
        self._log: deque[str] = deque(maxlen=200)
        self._last_order_ts: float = 0.0

    @property
    def timeframe(self) -> str:
        return str(self.config.get("timeframe", "1m"))

    def ibkr_bars(self) -> tuple[str, str]:
        return TF_IBKR.get(self.timeframe, ("1 min", "1 D"))

    # ---- lifecycle ----
    async def on_start(self, ctx: AgentContext) -> None: ...
    async def on_stop(self, ctx: AgentContext) -> None: ...
    async def step(self, ctx: AgentContext) -> None:
        raise NotImplementedError

    # ---- helpers ----
    def p(self, key: str) -> Any:
        return self.config.get(key)

    def log(self, msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._log.append(f"{ts} {msg}")

    def can_trade(self, min_gap_s: float = 60.0) -> bool:
        """Prevent sending orders too often"""
        return time.time() - self._last_order_ts >= min_gap_s

    def mark_traded(self) -> None:
        self._last_order_ts = time.time()

    def to_dict(self, tail: int = 8) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description, "kind": self.kind,
            "interval_s": self.interval_s, "run_on_bar_close": self.run_on_bar_close, "active": self.active,
            "params": [p.to_dict() for p in self.params], "config": self.config, "risk": self.risk,
            "stats": self.stats, "log": list(self._log)[-tail:],
        }


# ---------- indicator helpers ----------
def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def mean_std(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / n
    return m, var ** 0.5


def TimeframeParam(default: str = "1m") -> ParamSpec:
    return ParamSpec("timeframe", "Timeframe", "timeframe", default, help="Bar size the agent evaluates on")
