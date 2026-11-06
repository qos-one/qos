"""
Walk-forward of a Strategy in qx_strategies on the data catalog (fit on the train window → test on the unseen test window)

  uv run scripts/walkforward.py configs/strategies/ema_cross_eurusd_5m.json
  uv run scripts/walkforward.py configs/strategies/ema_cross_eurusd_5m.json --days 365 --train 90 --test 30
  Custom parameter grid:
  uv run scripts/walkforward.py configs/strategies/ema_cross_eurusd_5m.json --grid '{"fast":[5,10,20],"slow":[30,50,100],"sl_tp":[[15,30],[25,50],[0,0]]}'
"""
from __future__ import annotations

import argparse
from pathlib import Path
import json

import _cli  # noqa: F401
from qx_strategies import load_config
from qx_strategies.reports import save_report
from qx_strategies.walkforward import walk_forward


from qx_strategies.reports import shown as _rep_shown  # noqa: E402

def main() -> None:
    ap = argparse.ArgumentParser()
    _cli.add_common(ap, 365)
    ap.add_argument("--train", type=int, default=90, help="days in the training window")
    ap.add_argument("--test", type=int, default=30, help="days in the test window")
    ap.add_argument("--grid", default="", help="JSON parameter grid to try (omit = the Strategy's default grid)")
    ap.add_argument("--min-trades", type=int, default=10)
    ap.add_argument("--workers", type=int, default=None, help="number of processes (default = CPU count − 1)")
    a = ap.parse_args()
    cfg = _cli.apply_sets(load_config(a.config), a.set)
    start, end = _cli.period(a)
    r = walk_forward(cfg, start, end, a.train, a.test, json.loads(a.grid) if a.grid else None, a.min_trades, a.workers)
    path = save_report("walkforward", f"{cfg.get('strategy_id', cfg['strategy'])}", {"config_file": Path(a.config).name, **r})
    o, b = r["oos"], r["baseline"]
    print(f"\nout-of-sample  ${o['pnl_usd']:,.2f} · {o['trades']} trades · win {((o['win_rate'] or 0) * 100):.1f}% · "
          f"max DD ${o['max_drawdown_usd']:,.2f} · positive folds {o['positive_folds']}/{o['folds']}")
    print(f"baseline       ${b['pnl_usd']:,.2f} · {b['trades']} trades (current config, not tuned)")
    if r["most_picked"]:
        print(f"most picked    {r['most_picked']['params']} ({r['most_picked']['count']}/{o['folds']} folds)")
    print(f"\nsaved → {_rep_shown(path)}")


if __name__ == "__main__":
    main()
