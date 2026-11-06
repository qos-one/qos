"""
qx_strategies — NautilusTrader strategies shared by backtest and live (one codebase)

  from qx_strategies import build_strategy, load_config
  strat = build_strategy(load_config("configs/strategies/ema_cross_eurusd_5m.json"), source="EXTERNAL")   # backtest
  strat = build_strategy(cfg, source="INTERNAL")                                                          # live (bars aggregated from quotes)

config is a plain dict: {"strategy": "ema_cross", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5, ...}
"strategy" can be given 3 ways: "ema_cross" (latest package version, else the built-in) · "ema_cross@1.0.0" · "ema_cross@builtin"
External strategies live in $QOS_HOME/strategies/<name>@<version>/strategy.py (see qos/paths.py) — see registry.py and qos/sdk/README.md
"""
from __future__ import annotations

import json
from pathlib import Path

from . import ema_cross
from .ema_cross import QXEmaCross, QXEmaCrossConfig
from .registry import REG, split_ref

_MODULES = [ema_cross]
for _m in _MODULES:
    REG.add_builtin(_m)

__all__ = ["REG", "REGISTRY", "SPECS", "QXEmaCross", "QXEmaCrossConfig", "bar_type_str", "build_strategy", "load_config",
           "check_config", "register", "resolve_ref", "summary"]


class _Specs(dict):
    """SPECS[name] / name in SPECS / SPECS.items() — read live from the registry (manifests only, no third-party code is imported)"""

    def _sync(self) -> None:
        super().clear()
        super().update({k: v for k, v in REG.specs().items() if "@" not in k})     # entries = names (resolved versions)

    def __getitem__(self, k):
        self._sync()
        if "@" in k:
            return REG.resolve(k).spec()                                            # name@version → spec of that version
        return super().__getitem__(k)

    def __contains__(self, k):
        self._sync()
        if "@" in k:
            return k in REGISTRY
        return super().__contains__(k)

    def get(self, k, default=None):
        try:
            return self[k]
        except (KeyError, ValueError):
            return default

    def __iter__(self):
        self._sync()
        return super().__iter__()

    def __len__(self):
        self._sync()
        return super().__len__()

    def keys(self):
        self._sync()
        return super().keys()

    def items(self):
        self._sync()
        return super().items()

    def values(self):
        self._sync()
        return super().values()


class _Registry(dict):
    """REGISTRY[name] → (Strategy class, Config class) — loaded (imported) on demand"""

    def __getitem__(self, k):
        m = REG.load(k)
        return (m.Strategy, m.Config)

    def __contains__(self, k):
        try:
            REG.resolve(k)
            return True
        except ValueError:
            return False

    def get(self, k, default=None):
        try:
            return self[k]
        except ValueError:
            return default

    def __iter__(self):
        return iter(REG.names())

    def keys(self):
        return REG.names()


SPECS = _Specs()
REGISTRY = _Registry()


def register(module_or_cls) -> str:
    """Register a strategy module (NAME/LABEL/PARAMS/GRID/summary/Config/Strategy) or a qos.sdk.Strategy class in this process"""
    if isinstance(module_or_cls, type):
        from qos.sdk import compile as _compile
        module_or_cls = _compile(module_or_cls)
    REG.add_builtin(module_or_cls)
    return module_or_cls.NAME


def check_config(cfg: dict, source: str = "EXTERNAL") -> None:
    """Validate a config (types/ranges/parameter relationships) without importing package code in this process — raises ValueError"""
    REG.check_config(cfg, source)


def resolve_ref(ref: str) -> str:
    """'ema_cross' → 'ema_cross@1.0.0' (or 'ema_cross@builtin'), whichever would actually be used now"""
    return REG.resolve(ref).ref


def bar_type_str(instrument: str, tf_minutes: int, source: str = "EXTERNAL") -> str:
    """EXTERNAL = prebuilt bars (catalog/IBKR historical) · INTERNAL = Nautilus aggregates bars from quotes in live"""
    return f"{instrument}-{int(tf_minutes)}-MINUTE-MID-{source}"


def load_config(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def build_strategy(cfg: dict, source: str = "EXTERNAL", strategy_cls: type | None = None, **overrides):
    """Build a Strategy from a config dict — the single entry point used by both scripts/backtest.py and nautilus_runner.py
    strategy_cls: a qos.sdk.Strategy class (registered as <name>@builtin; instrument/tf_minutes are filled from its metadata if the config lacks them)"""
    cfg = {**cfg, **overrides}
    if strategy_cls is not None:
        from qos.sdk import default_config
        cfg = {**default_config(strategy_cls), **cfg, "strategy": register(strategy_cls) + "@builtin"}
    ref = cfg.get("strategy", "ema_cross")
    m = REG.load(ref)
    return m.Strategy(config=m.Config.from_dict(cfg, source=source, params=m.PARAMS))


def summary(cfg: dict) -> str:
    """Short summary of the main parameters for lists, e.g. 'EMA 10/30', 'Donchian 20/10'"""
    ref = cfg.get("strategy", "ema_cross")
    try:
        e = REG.resolve(ref)
    except ValueError:
        return split_ref(ref)[0] or "?"
    if e.kind == "builtin":
        return e.module.summary(cfg)
    from qos.sdk.strategy import summary_text
    return summary_text(e.manifest, cfg)
