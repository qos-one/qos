"""Performance figures from closed trades (used for both live node results and backtest results) — equity, drawdown, daily, per-trade P&L distribution"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta


def _day(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()


def perf_block(trades: list[tuple[str, float]], base: float = 100_000.0, max_points: int = 300, max_days: int = 400) -> dict:
    """trades: (close time ISO, net P&L USD) · base: starting capital for % and Sharpe"""
    trades = sorted((t, float(p)) for t, p in trades if t)
    base = base if base and base > 0 else 100_000.0
    out = {"base": base, "curve": [], "daily": [], "monthly": [], "trade_pnl": [p for _, p in trades][-500:], "metrics": {}}
    if not trades:
        return out
    cum, peak, max_dd, max_dd_pct, dd_start, longest, cur_start = 0.0, 0.0, 0.0, 0.0, None, 0.0, None
    curve = []
    for t, p in trades:
        cum += p
        curve.append([t, round(cum, 2)])
        if cum >= peak:
            if cur_start is not None:
                longest = max(longest, (_day(t) - cur_start).days)
            peak, cur_start = cum, None
        else:
            if cur_start is None:
                cur_start = _day(dd_start or t)
            max_dd = max(max_dd, peak - cum)
            max_dd_pct = max(max_dd_pct, (peak - cum) / (base + peak) * 100)      # % of equity at the peak this drawdown started from
        dd_start = t
    if cur_start is not None:
        longest = max(longest, (_day(trades[-1][0]) - cur_start).days)
    if len(curve) > max_points:
        step = len(curve) // max_points + 1
        curve = curve[::step] + [curve[-1]]
    out["curve"] = curve

    by_day: dict[date, list[float]] = {}
    for t, p in trades:
        by_day.setdefault(_day(t), []).append(p)
    d0, d1 = min(by_day), max(by_day)
    series = []
    d = d0
    while d <= d1:
        if d.weekday() < 5 or d in by_day:
            series.append((d, sum(by_day.get(d, [])), len(by_day.get(d, []))))
        d += timedelta(days=1)
    out["daily"] = [{"d": x.isoformat(), "pnl": round(v, 2), "n": n} for x, v, n in series[-max_days:]]
    months: dict[str, list] = {}
    for x, v, n in series:
        mm = months.setdefault(x.strftime("%Y-%m"), [0.0, 0, 0, 0])
        mm[0] += v
        mm[1] += n
        mm[2] += 1 if n and v > 0 else 0
        mm[3] += 1 if n else 0
    run = 0.0
    for k in sorted(months):
        v, n, pos, act = months[k]
        out["monthly"].append({"m": k, "pnl": round(v, 2), "n": n, "return_pct": round(v / (base + run) * 100, 3),
                               "positive_days": pos, "active_days": act})
        run += v
    active = [v for _, v, n in series if n]
    rets = [v / base for _, v, _ in series]
    sharpe = 0.0
    if len(rets) > 1:
        m = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = m / sd * math.sqrt(252) if sd > 0 else 0.0
    pnls = [p for _, p in trades]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]     # breakeven trades are neither
    gw, gl = sum(wins), -sum(losses)
    out["metrics"] = {
        "net": round(cum, 2), "return_pct": round(cum / base * 100, 3), "trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls) * 100, 1), "profit_factor": round(gw / gl, 2) if gl > 0 else (999.0 if gw > 0 else 0.0),
        "expectancy": round(cum / len(pnls), 2), "avg_win": round(gw / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0, "best_trade": round(max(pnls), 2), "worst_trade": round(min(pnls), 2),
        "max_drawdown": round(max_dd, 2), "max_drawdown_pct": round(max_dd_pct, 3),
        "longest_drawdown_days": int(longest), "days": len(series), "active_days": len(active),
        "positive_days_pct": round(sum(1 for v in active if v > 0) / len(active) * 100, 1) if active else 0.0,
        "best_day": round(max(active), 2) if active else 0.0, "worst_day": round(min(active), 2) if active else 0.0,
        "sharpe": round(sharpe, 2), "from": trades[0][0], "to": trades[-1][0],
    }
    return out
