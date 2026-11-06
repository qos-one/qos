"""quick_backtest — test a strategy on a developer machine with the same Nautilus BacktestEngine QOS uses (qx_strategies.backtest.run_backtest)"""
from __future__ import annotations


def bar_info(bars: list) -> tuple[str, int]:
    bt = bars[0].bar_type
    return str(bt.instrument_id), int(bt.spec.timedelta.total_seconds() // 60)


def quick_backtest(cls, params: dict | None = None, bars: list | None = None, *, trade_size: str | int = "20000",
                   sl_pips: float = 0.0, tp_pips: float = 0.0, allow_short: bool = True, starting_usd: float = 100_000,
                   usd_rates: dict | None = None) -> dict:
    """Backtest a qos.sdk.Strategy class on bars (from qos.sdk.catalog.bars or qos.sdk.testing.synthetic_bars)

    Returns the same dict as a QOS backtest: pnl_usd, return_pct, closed_positions, win_rate, max_drawdown_usd, trades, equity …
    + actions [(ts_ns, side/layers)] — every decision (use it to compare results before/after a code change)
    """
    from qx_strategies.backtest import run_backtest

    from .strategy import compile, default_config
    if not bars:
        raise ValueError("bars is required — e.g. qos.sdk.catalog.bars('EUR/USD', '5m', start, end) or qos.sdk.testing.synthetic_bars()")
    keys = {p.name for p in cls.params()}
    unknown = set(params or {}) - keys
    if unknown:
        raise ValueError(f"unknown parameter(s) {', '.join(sorted(unknown))} — {cls.__name__} has {', '.join(sorted(keys)) or 'none'}")
    mod = compile(cls)
    inst, tf = bar_info(bars)
    cfg = {**default_config(cls), **(params or {}), "strategy": f"{cls.name}@{cls.version}", "instrument": inst, "tf_minutes": tf,
           "trade_size": str(int(float(trade_size))), "sl_pips": float(sl_pips), "tp_pips": float(tp_pips), "allow_short": bool(allow_short),
           "warmup_days": 0}
    strategy = mod.Strategy(config=mod.Config.from_dict(cfg, source="EXTERNAL", params=mod.PARAMS))
    acts: list = []
    # cross pairs: P&L converted from the catalog's XXX/USD bars when present; with no rate source pnl_usd is None and the
    # result carries pnl_quote / pnl_quote_ccy instead (see res["pnl_note"])
    res = run_backtest(cfg, bars=bars, starting_usd=starting_usd, log_level="OFF", _collect_actions=acts, strategy=strategy,
                       usd_rates=usd_rates, unpriced_ok=usd_rates is None)
    res["actions"] = [(int(t), int(v)) for t, v in acts]
    return res
