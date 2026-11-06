"""
Walk-forward: split the data into windows — find the best parameters on the training window (train) and apply them to the next unseen window (test)
Only the combined test-window (out-of-sample) result is trustworthy — compared with the original config values (baseline) on the same windows
"""
from __future__ import annotations

import itertools
import os
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

DEFAULT_GRID = {"fast": [10, 20], "slow": [30, 50], "sl_tp": [[15, 30], [0, 0]]}


def make_folds(start, end, train_days: int, test_days: int) -> list[dict]:
    s, e = pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tzinfo is None else pd.Timestamp(start), \
        pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tzinfo is None else pd.Timestamp(end)
    folds, t = [], s
    while t + pd.Timedelta(days=train_days + test_days) <= e + pd.Timedelta(days=1):
        tr_end = t + pd.Timedelta(days=train_days)
        te_end = min(tr_end + pd.Timedelta(days=test_days), e)
        folds.append({"train_start": t.isoformat(), "train_end": tr_end.isoformat(), "test_start": tr_end.isoformat(), "test_end": te_end.isoformat()})
        t += pd.Timedelta(days=test_days)
    return folds


def default_grid(cfg: dict) -> dict:
    from . import SPECS
    return SPECS.get(cfg.get("strategy", "ema_cross"), {}).get("grid") or DEFAULT_GRID


def valid_combos(cfg: dict, combos: list[dict]) -> list[dict]:
    """Drop parameter sets the Strategy rejects (e.g. exit_n > entry_n) using the real constructor"""
    from . import build_strategy
    out = []
    for p in combos:
        try:
            build_strategy({**cfg, **p})
            out.append(p)
        except Exception:
            pass
    return out


def expand_grid(grid: dict) -> list[dict]:
    keys = list(grid)
    out = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        p = dict(zip(keys, combo))
        if "sl_tp" in p:
            p["sl_pips"], p["tp_pips"] = p.pop("sl_tp")
        if "fast" in p and "slow" in p and p["fast"] >= p["slow"]:
            continue
        out.append(p)
    return out


def stitched_drawdown(mtms: list[dict | None]) -> float:
    """Peak-to-trough drawdown of the test windows chained one after another, from each window's marked-to-market
    path summary (backtest "mtm": min / max / end / mdd relative to the window start) — exact without the per-bar series"""
    off, peak, mdd = 0.0, 0.0, 0.0
    for m in mtms:
        if not m:
            continue
        mdd = max(mdd, float(m["mdd"]), peak - (off + float(m["min"])))
        peak = max(peak, off + float(m["max"]))
        off += float(m["end"])
    return mdd


def score(res: dict, min_trades: int) -> float:
    """PnL − half the max drawdown · fewer trades than the minimum = not counted"""
    if res["closed_positions"] < min_trades:
        return float("-inf")
    return res["pnl_usd"] - 0.5 * res["max_drawdown_usd"]


def _job(args):
    cfg, start, end, keep_trades = args
    from .backtest import NoBarsError, run_backtest
    try:
        r = run_backtest(cfg, start=start, end=end, log_level="OFF")
    except NoBarsError as e:                    # no data in that window — any other error (e.g. no USD rate) is raised to the caller
        return {"error": str(e), "closed_positions": 0, "pnl_usd": 0.0, "max_drawdown_usd": 0.0, "trades": [], "win_rate": None}
    if not keep_trades:
        r.pop("trades", None)
        r.pop("equity", None)
    return r


def walk_forward(cfg: dict, start, end, train_days: int = 90, test_days: int = 30, grid: dict | None = None,
                 min_trades: int = 10, workers: int | None = None, starting_usd: float = 100_000, progress=print) -> dict:
    grid = grid or default_grid(cfg)
    combos = valid_combos(cfg, expand_grid(grid))
    if not combos:
        raise ValueError("no valid parameter combinations in the grid")
    folds = make_folds(start, end, train_days, test_days)
    if not folds:
        raise ValueError("period too short for one train+test fold")
    workers = workers if workers is not None else max(1, (os.cpu_count() or 2) - 1)
    progress(f"walk-forward: {len(folds)} folds × {len(combos)} combos · train {train_days}d / test {test_days}d · workers {workers}")
    pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    run = (lambda jobs: list(pool.map(_job, jobs))) if pool else (lambda jobs: [_job(j) for j in jobs])
    out_folds, oos_trades, base_trades = [], [], []
    try:
        for k, f in enumerate(folds, 1):
            train = run([({**cfg, **p}, f["train_start"], f["train_end"], False) for p in combos])
            scored = sorted(zip(combos, train), key=lambda pr: score(pr[1], min_trades), reverse=True)
            best_p, best_r = scored[0]
            test, base = run([({**cfg, **best_p}, f["test_start"], f["test_end"], True),
                              (cfg, f["test_start"], f["test_end"], True)])
            oos_trades += test.get("trades", [])
            base_trades += base.get("trades", [])
            out_folds.append({**f, "best_params": best_p, "train_pnl": best_r["pnl_usd"], "train_trades": best_r["closed_positions"],
                              "train_score": None if score(best_r, min_trades) == float("-inf") else round(score(best_r, min_trades), 2),
                              "test_pnl": test["pnl_usd"], "test_trades": test["closed_positions"], "test_win_rate": test["win_rate"],
                              "test_max_dd": test.get("max_drawdown_usd"), "baseline_pnl": base["pnl_usd"], "_mtm": test.get("mtm"),
                              "top": [{"params": p, "pnl": r["pnl_usd"], "trades": r["closed_positions"]} for p, r in scored[:3]]})
            progress(f"  fold {k}/{len(folds)} test {f['test_start'][:10]}→{f['test_end'][:10]}: best {best_p} · "
                     f"train ${best_r['pnl_usd']:,.0f} · test ${test['pnl_usd']:,.0f} ({test['closed_positions']} trades) · baseline ${base['pnl_usd']:,.0f}")
    finally:
        if pool:
            pool.shutdown()

    def _eq(trades):
        eq, pts = starting_usd, []
        for t in sorted(trades, key=lambda t: t["closed"]):
            eq += t["pnl"]
            pts.append([t["closed"], round(eq, 2)])
        return pts

    oos_eq = _eq(oos_trades)
    mdd = stitched_drawdown([f.get("_mtm") for f in out_folds])
    for f in out_folds:
        f.pop("_mtm", None)
    pnls = [t["pnl"] for t in oos_trades]
    # most frequently chosen parameters — suggested for the next config
    picks = pd.Series([str(f["best_params"]) for f in out_folds]).value_counts()
    return {
        "config": cfg, "grid": grid, "start": folds[0]["train_start"], "end": folds[-1]["test_end"],
        "train_days": train_days, "test_days": test_days, "min_trades": min_trades, "folds": out_folds,
        "oos": {"folds": len(out_folds), "positive_folds": sum(f["test_pnl"] > 0 for f in out_folds),
                "pnl_usd": round(sum(f["test_pnl"] for f in out_folds), 2), "trades": len(pnls),   # = Σ fold test P&L (incl. positions closed at fold end)
                "win_rate": round(sum(p > 0 for p in pnls) / len(pnls), 3) if pnls else None,
                "max_drawdown_usd": round(mdd, 2), "equity": oos_eq},
        "baseline": {"pnl_usd": round(sum(f["baseline_pnl"] for f in out_folds), 2), "trades": len(base_trades), "equity": _eq(base_trades)},
        "most_picked": {"params": picks.index[0], "count": int(picks.iloc[0])} if len(picks) else None,
    }
