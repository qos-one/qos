"""
Evaluate a strategy package after install (qos install calls it via the API → Strategy lab job)

  uv run scripts/evaluate_package.py ema_cross@1.0.0 [--days 90] [--wf-days 365] [--train 90] [--test 30]

1. 90-day backtest on the data catalog with the package defaults → passes if net > 0 and max DD ≤ max_drawdown_pct (manifest, default 10%) of starting capital
2. walk-forward with the manifest grid → passes if ≥ 60% of folds are positive out-of-sample
Results are saved in reports/ (Backtests tab) and a final RESULT {json} line is printed for the API to update data/strategy_status.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import _cli  # noqa: F401
import pandas as pd

from qx_strategies.backtest import NoBarsError, UsdRateError, run_backtest
from qx_strategies.registry import REG
from qx_strategies.reports import save_report
from qx_strategies.walkforward import walk_forward

START_USD = 100_000.0
WF_MIN_POSITIVE = 0.6


from qx_strategies.reports import shown as _rep_shown  # noqa: E402

def base_config(ref: str, trade_size: str) -> tuple[dict, dict]:
    REG.scan(force=True)
    try:
        entry = REG.resolve(ref)
    except (KeyError, ValueError) as e:
        raise SystemExit(f"package {ref} not found: {e}")
    if entry.status != "ok":
        raise SystemExit(f"package {entry.ref} cannot be loaded: {entry.error}")
    m = entry.manifest
    name, ver = entry.name, entry.version
    cfg = {"strategy": entry.ref, "strategy_id": f"QOS-{name}-{ver}".replace("_", "-"), "tf_minutes": int(m.get("tf_minutes") or 5),
           "trade_size": trade_size}
    return cfg, m


def pick_instrument(cfg: dict, m: dict, start: str, end: str) -> tuple[dict, dict]:
    errors = []
    for inst in m.get("instruments") or []:
        c = {**cfg, "instrument": inst}
        try:
            return c, run_backtest(c, start=start, end=end, log_level="OFF")
        except UsdRateError as e:                                  # data exists but the P&L cannot be priced in USD — say so
            raise SystemExit(f"{inst}: {e}")
        except NoBarsError as e:
            errors.append(f"{inst}: {e}")
    raise SystemExit("no catalog data for any instrument in the manifest — " + "; ".join(errors))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ref")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--wf-days", type=int, default=365)
    ap.add_argument("--train", type=int, default=90)
    ap.add_argument("--test", type=int, default=30)
    ap.add_argument("--trade-size", default="20000")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--only", choices=["backtest", "walkforward"], default=None, help="run only one part (the other result is kept)")
    a = ap.parse_args()
    cfg, m = base_config(a.ref, a.trade_size)
    ref = cfg["strategy"]
    dd_cap = float(m.get("max_drawdown_pct") or 10.0)
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    start = (pd.Timestamp(end) - pd.Timedelta(days=a.days)).strftime("%Y-%m-%d %H:%M")
    print(f"evaluate {ref}: backtest {a.days}d · walk-forward {a.wf_days}d ({a.train}/{a.test}) · DD cap {dd_cap:g}%", flush=True)
    cfg, res = pick_instrument(cfg, m, start, end)
    result: dict = {"ref": ref}
    if a.only != "walkforward":
        dd_pct = res["max_drawdown_usd"] / START_USD * 100
        bt_ok = res["pnl_usd"] > 0 and dd_pct <= dd_cap
        path = save_report("backtest", cfg["strategy_id"], {"config": cfg, "package": ref, "start": start, "end": end, **res})
        print(f"pnl_usd {res['pnl_usd']:,.2f} · trades {res['closed_positions']} · max DD {res['max_drawdown_usd']:,.2f} ({dd_pct:.2f}%) → "
              f"{'PASS' if bt_ok else 'FAIL'}", flush=True)
        print(f"saved → {_rep_shown(path)}", flush=True)
        result["backtest"] = {"ok": bt_ok, "pnl_usd": round(res["pnl_usd"], 2), "trades": res["closed_positions"],
                              "max_drawdown_usd": round(res["max_drawdown_usd"], 2), "max_drawdown_pct": round(dd_pct, 3), "dd_cap_pct": dd_cap,
                              "from": res.get("from"), "to": res.get("to"), "instrument": cfg["instrument"], "report": path.name, "days": a.days}
    if a.only != "backtest":
        grid = m.get("grid") or {"trade_size": [cfg["trade_size"]]}          # no grid = walk-forward with the defaults only
        wstart = (pd.Timestamp(end) - pd.Timedelta(days=a.wf_days)).strftime("%Y-%m-%d %H:%M")
        try:
            r = walk_forward(cfg, wstart, end, a.train, a.test, grid, workers=a.workers, progress=lambda s: print(s, flush=True))
            o = r["oos"]
            share = o["positive_folds"] / o["folds"] if o["folds"] else 0.0
            wp = save_report("walkforward", cfg["strategy_id"], {"package": ref, **r})
            result["walkforward"] = {"ok": share >= WF_MIN_POSITIVE, "positive_folds": o["positive_folds"], "folds": o["folds"],
                                     "share": round(share, 3), "pnl_usd": round(o["pnl_usd"], 2), "trades": o["trades"],
                                     "max_drawdown_usd": round(o["max_drawdown_usd"], 2), "report": wp.name, "days": a.wf_days}
            print(f"out-of-sample ${o['pnl_usd']:,.2f} · positive folds {o['positive_folds']}/{o['folds']} → "
                  f"{'PASS' if result['walkforward']['ok'] else 'FAIL'}", flush=True)
            print(f"saved → {_rep_shown(wp)}", flush=True)
        except ValueError as e:
            result["walkforward"] = {"ok": False, "error": str(e)}
            print(f"walk-forward: {e}", flush=True)
    print("RESULT " + json.dumps(result, default=str), flush=True)

if __name__ == "__main__":
    main()
