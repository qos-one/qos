"""
qos install checks — run in a subprocess (python -I) so package code never enters the CLI/API process

usage: python -I qos/sdk/_check.py <strategy.py> [--bars auto|catalog|synthetic] [--days 30] [--catalog DIR]
Prints JSON on the last line: {"ok", "manifest", "checks": [{"name", "ok", "detail"}], "failed": <name of the failed check>}

Checks (stops at the first failure):
  import         imports within 60 seconds and makes no network calls on import
  manifest       qos.sdk.validate passes and a Strategy can be built from the defaults
  network        30-day backtest with sockets forbidden (any attempt = fail, even if the strategy catches the exception)
  deterministic  a repeated backtest must produce identical decisions and P&L
  lookahead      drop the last 30% of bars; decisions before the cut must not change (no use of future data)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


class NetworkBlocked(RuntimeError):
    pass


class NetGuard:
    """Forbid sockets during the checks — record every attempt (caught even if the strategy wraps it in try/except)"""

    def __init__(self):
        self.attempts: list[str] = []
        self._saved: list = []

    def _block(self, what):
        def f(*a, **k):
            self.attempts.append(f"{what}{a[1:2] if what == 'connect' else a[:1]}")
            raise NetworkBlocked("network access is not allowed in strategy code")
        return f

    def __enter__(self):
        for obj, name, label in ((socket.socket, "connect", "connect"), (socket.socket, "connect_ex", "connect"),
                                 (socket, "create_connection", "create_connection"), (socket, "getaddrinfo", "getaddrinfo")):
            self._saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, self._block(label))
        return self

    def __exit__(self, *exc):
        for obj, name, orig in reversed(self._saved):
            setattr(obj, name, orig)
        self._saved.clear()
        return False


def load_class(path: str):
    from qos.sdk import Strategy
    spec = importlib.util.spec_from_file_location("qos_pkg_check", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qos_pkg_check"] = mod
    spec.loader.exec_module(mod)
    classes = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == mod.__name__]
    if len(classes) != 1:
        raise ValueError(f"strategy.py must define exactly one qos.sdk.Strategy subclass (found {len(classes)})")
    return classes[0]


def pick_bars(manifest: dict, mode: str, days: int, catalog: str | None) -> tuple[str, list]:
    tf = int(manifest["tf_minutes"])
    if mode == "synthetic":
        from qos.sdk.testing import synthetic_bars
        inst = manifest["instruments"][0]
        return inst, synthetic_bars(inst, tf, n=max(400, int(days * 24 * 60 / tf * 5 / 7)))
    import pandas as pd

    from qx_strategies.backtest import load_bars
    end = pd.Timestamp.now(tz="UTC").floor("D")
    for inst in manifest["instruments"]:
        try:
            bars = load_bars({"instrument": inst, "tf_minutes": tf}, (end - pd.Timedelta(days=days + 3)).isoformat(), end.isoformat(),
                             Path(catalog) if catalog else None)
        except Exception:
            bars = []
        cut = end.value - days * 86_400_000_000_000
        bars = [b for b in bars if b.ts_event >= cut]
        if len(bars) >= 100:
            return inst, bars
    raise ValueError(f"no data in the catalog for {', '.join(manifest['instruments'])} {manifest['timeframe']} — "
                     "build the catalog or use --bars synthetic")


def run_once(cls, cfg: dict, bars: list, catalog: str | None = None) -> tuple[list, dict]:
    from qx_strategies import backtest as bt
    acts: list = []
    # checks compare decisions: a cross pair without XXX/USD data still runs (P&L then stays in the quote currency)
    res = bt.run_backtest(cfg, bars=bars, log_level="OFF", _collect_actions=acts, catalog_dir=Path(catalog) if catalog else None,
                          unpriced_ok=True)
    return [(int(t), int(v)) for t, v in acts], res


def lookahead_diff(full: list, part: list, cutoff_ns: int) -> list:
    """Decisions at times ≤ cutoff must match in both runs — returns the differences"""
    a = [x for x in full if x[0] <= cutoff_ns]
    b = [x for x in part if x[0] <= cutoff_ns]
    if a == b:
        return []
    return [{"full": x, "truncated": y} for x, y in zip(a + [None] * (len(b) - len(a)), b + [None] * (len(a) - len(b))) if x != y][:5]


def check(path: str, mode: str = "catalog", days: int = 30, catalog: str | None = None) -> dict:
    checks: list[dict] = []
    out = {"ok": False, "manifest": None, "checks": checks, "failed": None}

    def step(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            out["failed"] = name
        return ok

    from qos.sdk import default_config, manifest, validate
    guard = NetGuard()
    t0 = time.time()
    try:
        with guard:
            cls = load_class(path)
    except Exception as e:
        step("import", False, f"{type(e).__name__}: {e}" + (f" · network attempts: {guard.attempts[:3]}" if guard.attempts else ""))
        return out
    if guard.attempts:
        step("import", False, f"strategy.py opens a network connection on import: {guard.attempts[:3]}")
        return out
    step("import", True, f"{cls.__name__} in {time.time() - t0:.1f}s")

    errs = validate(cls)
    if errs:
        step("manifest", False, "; ".join(errs))
        return out
    m = manifest(cls)
    out["manifest"] = m
    try:
        from qx_strategies import register
        from qx_strategies.registry import REG
        mod = REG.builtins[register(cls)].module
        cfg = {**default_config(cls), "strategy": f"{m['name']}@builtin"}
        mod.Strategy(config=mod.Config.from_dict(cfg, params=mod.PARAMS))
    except Exception as e:
        step("manifest", False, f"cannot build the strategy with default parameters: {type(e).__name__}: {e}")
        return out
    step("manifest", True, f"{m['name']}@{m['version']} · {len(m['params'])} params · {m['timeframe']} · {', '.join(m['instruments'])}")

    notes = []
    try:
        inst, bars = pick_bars(m, "synthetic" if mode == "synthetic" else "catalog", days, catalog)
    except Exception as e:
        if mode != "auto":
            step("network", False, f"no bars to test with: {e}")
            return out
        inst, bars = pick_bars(m, "synthetic", days, catalog)                 # developer machine without a data catalog
        notes.append("no data catalog here — checks ran on synthetic bars (QOS re-checks on real data when you install there)")
    cfg = {**cfg, "instrument": inst}
    try:
        with guard:
            a1, r1 = run_once(cls, cfg, bars, catalog)
    except Exception as e:
        if guard.attempts:
            step("network", False, f"network access during backtest: {guard.attempts[:3]}")
        else:
            step("network", False, f"backtest failed: {type(e).__name__}: {e}")
        out["traceback"] = traceback.format_exc()[-1500:]
        return out
    if guard.attempts:
        step("network", False, f"network access during backtest: {guard.attempts[:3]} (strategy code must not use the network)")
        return out
    pnl_txt = f"{r1['pnl_usd']:,.2f}" if r1.get("pnl_usd") is not None else f"{r1['pnl_quote']:,.2f} {r1['pnl_quote_ccy']} (no USD rate)"
    step("network", True, f"{len(bars)} bars {inst} {m['timeframe']} · {len(a1)} decisions · P&L {pnl_txt} · no network calls")

    try:
        a2, r2 = run_once(cls, cfg, bars, catalog)
    except Exception as e:
        step("deterministic", False, f"second run failed: {type(e).__name__}: {e}")
        return out
    pnl = lambda r: r["pnl_usd"] if r.get("pnl_usd") is not None else r["pnl_quote"]   # noqa: E731
    if a1 != a2 or abs(pnl(r1) - pnl(r2)) > 1e-6:
        diff = next((i for i, (x, y) in enumerate(zip(a1, a2)) if x != y), min(len(a1), len(a2)))
        step("deterministic", False, f"two identical runs differ ({len(a1)} vs {len(a2)} decisions, first difference at #{diff}) — "
                                     "remove randomness without a fixed seed, wall-clock time or external state")
        return out
    step("deterministic", True, "two runs gave identical decisions and P&L")

    k = int(len(bars) * 0.7)
    try:
        a3, _ = run_once(cls, cfg, bars[:k], catalog)
    except Exception as e:
        step("lookahead", False, f"truncated run failed: {type(e).__name__}: {e}")
        return out
    diff = lookahead_diff(a1, a3, bars[k - 1].ts_event)
    if diff:
        step("lookahead", False, f"decisions before the cut-off change when later bars are removed: {diff}")
        return out
    step("lookahead", True, f"decisions up to bar {k}/{len(bars)} unchanged without future bars")
    out["ok"] = True
    out["sample"] = {"instrument": inst, "bars": len(bars), "decisions": len(a1), "pnl_usd": r1["pnl_usd"],
                     "closed_positions": r1["closed_positions"]}
    if not a1:
        notes.append("the strategy made no decisions on the test data — check parameters and timeframe")
    if notes:
        out["warnings"] = notes
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--bars", default="auto", choices=["auto", "catalog", "synthetic"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--catalog", default=None)
    a = ap.parse_args()
    try:
        res = check(a.path, a.bars, a.days, a.catalog)
    except Exception as e:                                   # noqa: BLE001
        res = {"ok": False, "checks": [], "failed": "internal", "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-1500:]}
    print(json.dumps(res, default=str))
