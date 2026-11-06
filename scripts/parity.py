"""
Parity: compare the legacy agent (via the API) with the new Strategy (Nautilus) on the same bars from the data catalog

  uv run scripts/parity.py configs/strategies/ema_cross_eurusd_5m.json --days 60
  Passes when ≥ 95% of decisions match (adjust with --min-match 0.98)
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import _cli  # noqa: F401  (sets sys.path + silences warnings)
from qx_strategies import load_config
from qx_strategies.parity import run_parity
from qx_strategies.reports import save_report


from qx_strategies.reports import shown as _rep_shown  # noqa: E402

def main() -> None:
    ap = argparse.ArgumentParser()
    _cli.add_common(ap, 60)
    ap.add_argument("--window", type=int, default=500, help="number of bars the legacy agent sees per step")
    ap.add_argument("--min-match", type=float, default=0.95)
    a = ap.parse_args()
    cfg = _cli.apply_sets(load_config(a.config), a.set)
    start, end = _cli.period(a)
    r = run_parity(cfg, start=start, end=end, window=a.window)
    ok = r["match_rate"] >= a.min_match
    path = save_report("parity", f"{cfg.get('strategy_id', cfg['strategy'])}", {"config_file": Path(a.config).name, "start": start, "end": end, "passed": ok, "min_match": a.min_match, **r})
    print(f"period        {r['from'][:16]} → {r['to'][:16]}  ({r['bars']:,} bars)")
    print(f"decisions     old {r['old_actions']} · new {r['new_actions']} · matched {r['matched']}")
    print(f"match rate    {r['match_rate'] * 100:.2f}%   {'PASS' if ok else 'FAIL'} (min {a.min_match * 100:.0f}%)")
    print(f"P&L           old ${r['old']['pnl_usd']:,.2f} · new ${r['new']['pnl_usd']:,.2f} · diff ${r['pnl_diff_usd']:,.2f}")
    print(f"trades        old {r['old']['closed_positions']} · new {r['new']['closed_positions']}")
    for d in r["divergence_rows"][:10]:
        print(f"  differs at {d['ts'][:16]}  old {d['old']}  new {d['new']}")
    print(f"\nsaved → {_rep_shown(path)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
