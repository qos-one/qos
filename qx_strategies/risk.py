"""
Central risk limits for Strategies in a Nautilus node — the same values as the API (risk.json of the selected account)

The API writes a risk summary to Redis key `qx:risk` every 5 seconds (and immediately on edit / halt / kill)
→ QXRiskGuard in the node loads it into STATE → Strategies call check_entry() before every new entry

Does not import nautilus — easy to test and works in live/backtest (backtest has no guard → empty STATE → no limits)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import fx

STALE_S = 60.0          # live: a payload older than this blocks new entries (the API publishes every 5 s, Redis key expires after 30 s)


@dataclass
class RiskState:
    limits: dict | None = None          # latest payload from the API
    received_at: float = 0.0
    require_sync: bool = False          # live: must receive values from the API before opening positions
    killed: bool = False                # kill switch (latched until the node restarts)
    kill_reason: str = ""
    trading_state: str = "ACTIVE"       # ACTIVE | REDUCING | HALTED (state of the node's RiskEngine)
    blocked: dict = field(default_factory=dict)   # reason → number of times blocked
    node_id: str = ""                   # step 15: this node's name (set by QXRiskGuard) — matched against blocked_nodes in the payload

    def update(self, payload: dict) -> None:
        self.limits = payload
        self.received_at = time.time()

    def age(self) -> float | None:
        return round(time.time() - self.received_at, 1) if self.received_at else None

    def halt_reason(self) -> str:
        """Reason new entries must stop ("" = trading allowed)"""
        if self.killed:
            return f"kill switch: {self.kill_reason or 'activated'}"
        L = self.limits
        if not L:
            return "waiting for risk limits from API" if self.require_sync else ""
        age = self.age()
        if self.require_sync and age is not None and age > STALE_S:     # API / Redis gone: old P&L, holds and halts must not be trusted
            return f"risk limits from API are stale ({age:.0f} s)"
        if L.get("halted"):
            return f"trading halted: {L.get('halt_reason') or 'halted'}"
        held = (L.get("blocked_nodes") or {}).get(self.node_id) if self.node_id else None
        if held:                                                # step 15: pause new entries after sleep/restart until the operator confirms positions
            return f"held: {held} — confirm positions in the Terminal to resume"
        lim, pnl = float(L.get("max_daily_loss_usd") or 0), L.get("daily_pnl_usd")
        if lim > 0 and pnl is None:                             # the API has no NetLiq / baseline: the daily loss is unknown → fail closed
            return "daily P&L unknown (no account data / unconfirmed baseline at the API) — new entries paused"
        if lim > 0 and float(pnl) <= -lim:
            return f"daily loss limit reached ({float(pnl):,.0f} / -{lim:,.0f} USD)"
        return ""

    def reset(self) -> None:
        self.__init__()


STATE = RiskState()


def api_key(instrument_id: str) -> str:
    """'EUR/USD.IDEALPRO' → 'FX:EURUSD' (same key format as the API)"""
    sym, _, venue = instrument_id.partition(".")
    if venue == "IDEALPRO":
        return "FX:" + sym.replace("/", "")
    if venue == "PAXOS":
        return "CRYPTO:" + sym.split("/")[0]
    return "STK:" + sym


def fx_symbol(instrument_id: str) -> str:
    """'EUR/USD.IDEALPRO' → 'EUR/USD' · 'AAPL.NASDAQ' → 'AAPL/USD' (shares / coins are priced in USD) — input for qx_strategies.fx"""
    sym, _, venue = instrument_id.partition(".")
    return sym if ("/" in sym or venue == "IDEALPRO") else f"{sym}/USD"


def payload_rates(limits: dict | None) -> dict:
    """USD value of one unit of each currency, as sent by the API (fx_rates, else the portfolio block's rates)"""
    L = limits or {}
    return dict(L.get("fx_rates") or (L.get("portfolio") or {}).get("rates") or {})


def notional_usd(instrument_id: str, qty: float, price: float, rates: dict | None = None) -> float | None:
    """Value in USD of |qty| base units: XXX/USD = qty × price · USD/XXX = qty · crosses (EUR/GBP, EUR/JPY) = qty × USD rate of the base
    (or price × USD rate of the quote) · None = cannot be priced (risk checks must block)"""
    return fx.notional_usd(fx_symbol(instrument_id), qty, price, rates)


def check_entry(instrument_id: str, qty: float, price: float, own_position_qty: float = 0.0, state: RiskState = STATE) -> tuple[bool, str]:
    """Check before a new entry — returns (ok?, reason) · same rules as the API's risk_gate"""
    why = state.halt_reason()
    if why:
        return False, why
    L = state.limits
    if not L:
        return True, ""                                   # backtest / no guard
    news = set(L.get("news_pause_currencies") or [])
    if news:                                              # no new entries around high-impact news (formerly in the agent runner)
        sym = instrument_id.split(".")[0]
        hit = (set(sym.split("/")) if "/" in sym else {"USD"}) & news
        if hit:
            return False, f"news pause: high-impact {', '.join(sorted(hit))} event within {L.get('news_pause_minutes')} min"
    rates = payload_rates(L)
    n = notional_usd(instrument_id, qty, price, rates)
    max_order = float(L.get("max_order_notional_usd") or 0)
    if n is None and (max_order > 0 or float(L.get("max_position_notional_usd") or 0) > 0):
        return False, f"cannot value {instrument_id} in USD (no FX rate) — entry blocked"   # unknown rate = block, never treat as zero
    if max_order > 0 and n > max_order:
        return False, f"order notional {n:,.0f} USD exceeds limit {max_order:,.0f}"
    key = api_key(instrument_id)
    max_pos = float(L.get("max_position_notional_usd") or 0)
    if max_pos > 0:
        own = notional_usd(instrument_id, own_position_qty, price, rates)
        if own is None:
            return False, f"cannot value the {instrument_id} position in USD (no FX rate) — entry blocked"
        existing = max(float((L.get("position_notional_usd") or {}).get(key, 0)), own)
        if existing + n > max_pos:
            return False, f"position notional {existing + n:,.0f} USD would exceed limit {max_pos:,.0f}"
    open_keys = set(L.get("open_keys") or [])
    max_open = int(L.get("max_open_positions") or 0)
    if max_open > 0 and key not in open_keys and own_position_qty == 0 and len(open_keys) >= max_open:
        return False, f"max open positions ({max_open}) reached"
    return True, ""


def note_block(reason: str, state: RiskState = STATE) -> int:
    k = reason.split(":")[0].split(" (")[0][:60]
    state.blocked[k] = state.blocked.get(k, 0) + 1
    return state.blocked[k]
