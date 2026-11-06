"""
Parity: compare the legacy agent (api/agents, run via the API) with the new Strategy in qx_strategies on the same bars from the data catalog

- the legacy agent runs its real code (step()) through a simulated AgentContext: bars = window of the last N bars, orders = filled at the bar close
- the new Strategy runs in Nautilus's BacktestEngine (same as live)
- compares "decisions": (bar time, target side +1/0/-1) from both sides, reports the % match and where they differ
"""
from __future__ import annotations

import asyncio

import pandas as pd

from .backtest import _NettingBook, _symbol, equity_curve, load_bars, run_backtest  # noqa: F401  (_NettingBook re-exported)

# legacy agent ↔ new Strategy + parameter mapping + compared value (default = side +1/0/−1)
def _sign(pos: float, cfg: dict) -> int:
    return (pos > 0) - (pos < 0)


def _tf(c: dict) -> str:
    return f"{int(c['tf_minutes'])}m"


PAIRS = {
    "ema_cross": {
        "agent": ("api.agents.ema_cross", "EmaCrossAgent"),
        "to_agent": lambda c: {"fast": c["fast"], "slow": c["slow"], "qty": float(c["trade_size"]),
                               "allow_short": c.get("allow_short", True), "timeframe": _tf(c)},
    },
}


def _bars_to_rows(bars, bar_minutes: int = 1) -> list[dict]:
    """Nautilus bar → the format the legacy agent gets from the API (t = bar open time as IBKR, ts = close time for comparison)"""
    off = bar_minutes * 60 * 1_000_000_000
    return [{"ts": b.ts_event, "t": pd.Timestamp(b.ts_event - off, unit="ns", tz="UTC").isoformat(),
             "o": b.open.as_double(), "h": b.high.as_double(), "l": b.low.as_double(), "c": b.close.as_double(), "v": 0}
            for b in bars]


def replay_agent(name: str, cfg: dict, bars, window: int = 500) -> dict:
    """Run the legacy agent's real code on the given bars; returns actions/trades/PnL"""
    import importlib

    from api.agents.base import AgentContext

    mod, cls = PAIRS[name]["agent"]
    agent = getattr(importlib.import_module(mod), cls)()
    key = f"FX:{_symbol(cfg['instrument'])}"
    agent.config.update({"key": key, **PAIRS[name]["to_agent"](cfg)})
    agent.can_trade = lambda *a, **k: True                       # no real time in the simulation — 1m+ bars never hit the 60-second rule anyway
    agent.mark_traded = lambda: None
    if hasattr(agent, "_ensure_model"):
        agent._ensure_model()
    rows = _bars_to_rows(bars, int(cfg.get("tf_minutes", 1)))
    value = PAIRS[name].get("value", _sign)
    if "window" in PAIRS[name]:
        window = PAIRS[name]["window"](cfg)
    book = _NettingBook(sym=cfg["instrument"])
    actions: list[tuple[int, int]] = []
    i = 0

    async def _bars(k, bs, du):
        return rows[max(0, i + 1 - window): i + 1]

    async def _submit(agent_id, k, side, qty, order_type="MKT", price=None):
        r = rows[i]
        book.fill(r["ts"], qty if side == "BUY" else -qty, r["c"])
        actions.append((r["ts"], value(book.pos, cfg)))
        return {"status": "Filled", "avg_fill_price": r["c"]}

    ctx = AgentContext(agent, quote=lambda k: {"last": rows[i]["c"]}, bars=_bars, position=lambda k: book.pos,
                       submit=_submit, now=lambda: rows[i]["ts"] / 1e9)

    async def _run():
        nonlocal i
        for i in range(len(rows)):
            await agent.step(ctx)
    asyncio.run(_run())
    pnls = [c["pnl"] for c in book.closed]
    last = rows[-1]["c"] if rows else None
    return {"actions": actions, "closed": book.closed, "closed_positions": len(book.closed), "orders": len(actions),
            "pnl_usd": round(book.total(last), 2), "realized_usd": round(book.realized, 2), "open_position": book.pos,
            "win_rate": round(sum(p > 0 for p in pnls) / len(pnls), 3) if pnls else None}


def compare_actions(old: list[tuple[int, int]], new: list[tuple[int, int]], max_rows: int = 30) -> dict:
    o, n = dict(old), dict(new)
    ts_all = sorted(set(o) | set(n))
    matched = sum(1 for t in ts_all if o.get(t) == n.get(t) and t in o and t in n)
    diffs = [{"ts": pd.Timestamp(t, unit="ns", tz="UTC").isoformat(), "old": o.get(t), "new": n.get(t)}
             for t in ts_all if o.get(t) != n.get(t)]
    denom = max(len(o), len(n)) or 1
    return {"matched": matched, "old_actions": len(o), "new_actions": len(n), "match_rate": round(matched / denom, 4),
            "divergences": len(diffs), "divergence_rows": diffs[:max_rows]}


def run_parity(cfg: dict, start=None, end=None, bars=None, window: int = 500, starting_usd: float = 100_000,
               catalog_dir=None, log_level: str = "OFF") -> dict:
    name = cfg.get("strategy", "ema_cross")
    if name not in PAIRS:
        raise ValueError(f"no legacy agent paired with '{name}'")
    pcfg = {**cfg, "sl_pips": 0, "tp_pips": 0}                   # the legacy agent has no SL/TP → disable them on the new side too so they compare
    if bars is None:
        bars = load_bars(pcfg, start, end, catalog_dir)
    old = replay_agent(name, pcfg, bars, window)
    new_strategy_actions: list = []
    new = run_backtest(pcfg, bars=bars, starting_usd=starting_usd, log_level=log_level, _collect_actions=new_strategy_actions)
    cmp = compare_actions(old["actions"], new_strategy_actions)
    pnl_diff = round(new["pnl_usd"] - old["pnl_usd"], 2)
    return {
        "config": pcfg, "window": window, "from": new["from"], "to": new["to"], "bars": len(bars),
        **cmp,
        "pnl_diff_usd": pnl_diff,
        "old": {k: old[k] for k in ("closed_positions", "orders", "pnl_usd", "win_rate")} | {"equity": equity_curve(old["closed"], starting_usd)},
        "new": {k: new[k] for k in ("closed_positions", "orders", "pnl_usd", "win_rate", "max_drawdown_usd")} | {"equity": new["equity"]},
        "notes": ["Legacy agent: EMA recomputed from the N-bar window each time · Nautilus: continuous EMA — may differ slightly at the start",
                  "Both sides fill at the bar close, 0.2 bps fees, no SL/TP"],
    }
