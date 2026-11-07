"""
qos.sdk.Strategy — the contract for strategy developers (no direct NautilusTrader access)

class MyStrategy(Strategy):
    name = "my_strategy"; label = "My Strategy"; version = "1.0.0"
    instruments = ["EUR/USD"]; timeframe = "5m"
    lookback = Param(20, min=5, max=200, grid=[10, 20, 40])

    def on_start(self): self.ema = self.indicator("ema", self.p.lookback)
    def on_bar(self, bar):
        if not self.ready(): return
        self.target(+1 if bar.close > self.ema.value else -1)

Everything money-related lives outside the strategy: trade size (trade_size), SL/TP, allow_short, risk limits, kill switch, closing positions on stop
— QOS handles it through the existing QXBaseStrategy; a strategy can only say "which side I want to be on" (target) or "how much I want to hold" (target_qty)
"""
from __future__ import annotations

import re
from typing import Any

from .params import Param, ParamValues

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
INDICATORS = {"ema": "ExponentialMovingAverage", "sma": "SimpleMovingAverage", "atr": "AverageTrueRange",
              "rsi": "RelativeStrengthIndex", "bollinger": "BollingerBands"}


def normalize_instrument(s: str) -> str:
    """'EUR/USD' / 'EURUSD' / 'EUR/USD.IDEALPRO' → 'EUR/USD.IDEALPRO'"""
    s = s.strip().upper()
    if "." in s:
        return s
    s = s.replace("/", "")
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}/{s[3:]}.IDEALPRO"
    raise ValueError(f"unsupported instrument '{s}' (use e.g. EUR/USD or EUR/USD.IDEALPRO)")


class Strategy:
    """Base class for SDK strategies — subclass it and override the hooks below"""

    # ---- metadata (override as class attributes) ----
    name: str = ""
    label: str = ""
    version: str = "1.0.0"
    description: str = ""
    instruments: list[str] = []
    timeframe: str = "5m"
    requires: list[str] = []                 # extra pip requirements (step 22: separate venv)
    summary: str = ""                        # short text format in listings, e.g. "EMA {fast}/{slow}" (empty = label + grid values)
    max_drawdown_pct: float = 10.0           # backtest threshold: maximum acceptable drawdown (% of 100,000 capital) — moves status installed → backtested
    timer_interval_s: float = 0.0            # > 0 → call on_timer every N seconds (node time)
    # ---- limits enforced by QOS (step 22) · None = QOS default ----
    max_notional_usd: float | None = None    # maximum position value per instrument (default 500,000 USD)
    max_orders_per_hour: int | None = None   # orders that open/increase a position per hour (default 60)
    max_flips_per_hour: int | None = None    # long ↔ short side flips per hour (default 30)
    max_memory_mb: int | None = None         # node memory (default 2048 MB)

    # ---- hooks ----
    def on_start(self) -> None:
        """Called once when the node starts — create indicators / load models"""

    def on_bar(self, bar) -> None:
        """Called on every bar (bar.open/high/low/close are Price · use float(bar.close)) — decide, then call self.target(...)"""

    def on_stop(self) -> None:
        """Called when the node stops. Afterwards QOS cancels this strategy's orders and, while the market is open, closes the positions this node opened (a restarted node does not know older positions; with FX closed positions are kept)."""

    def on_timer(self, interval_s: float) -> None:
        """Called every timer_interval_s seconds (if set) — for work not tied to bars, e.g. reloading a model"""

    def on_configure(self) -> None:
        """Called before on_start with self.p — raise ValueError if parameters conflict (e.g. fast >= slow)"""

    # ---- filled in by the SDK at runtime ----
    p: ParamValues
    state: dict
    _ctx: Any = None                          # adapter (QXBaseStrategy) — do not use directly

    # ---- services ----
    def target(self, side: int) -> bool:
        """Move to the target side: +1 Long · 0 Flat · −1 Short (size from the node's trade_size, subject to QOS risk/SL/TP)
        Returns True if an order was sent (False = already on that side, or blocked by risk)"""
        return self._ctx.sdk_target(int(side))

    def target_qty(self, qty: float) -> bool:
        """Hold qty units (+ Long / − Short / 0 Flat) — partial increases/decreases allowed · returns True if an order was sent"""
        return self._ctx.sdk_target_qty(float(qty))

    def position(self) -> float:
        """Net position of this strategy (+ Long / − Short)"""
        return self._ctx.net()

    def publish(self, name: str, **data) -> None:
        """Send a signal to the Terminal (Nodes tab · Node activity) and record it in the soak log"""
        self._ctx.sdk_publish(name, data)

    def indicator(self, kind: str, *args):
        """Create a Nautilus indicator that updates from bars automatically: ema/sma/rsi(period) · atr(period) · bollinger(period, k)"""
        return self._ctx.sdk_indicator(kind, *args)

    def ready(self) -> bool:
        """All indicators have finished warming up (no indicators = always ready)"""
        return self._ctx.sdk_ready()

    @property
    def trade_size(self) -> float:
        """The node's trade size (in base-currency units) — use it to compute layers for target_qty"""
        return float(self._ctx.config.trade_size)

    @property
    def tf_minutes(self) -> int:
        return int(self._ctx.config.bar_type.spec.timedelta.total_seconds() // 60)

    @property
    def allow_short(self) -> bool:
        return bool(self._ctx.config.allow_short)

    def load_model(self, filename: str = "model.joblib"):
        """Load a model (joblib / pickle) from the package folder — the file ships with the package on qos install · call in on_start"""
        import inspect
        from pathlib import Path
        path = Path(inspect.getfile(type(self))).resolve().parent / filename
        if not path.exists():
            raise FileNotFoundError(f"{filename} not found next to strategy.py ({path.parent})")
        import joblib
        return joblib.load(path)

    def bars(self, n: int = 100) -> list:
        """The last n bars (oldest → newest)"""
        return self._ctx.sdk_bars(n)

    @property
    def log(self):
        return self._ctx.log

    @property
    def instrument(self) -> str:
        return str(self._ctx.config.instrument_id)

    # ---- introspection ----
    @classmethod
    def params(cls) -> list[Param]:
        out: dict[str, Param] = {}
        for klass in reversed(cls.__mro__):
            for k, v in vars(klass).items():
                if isinstance(v, Param):
                    out[k] = v
        return list(out.values())


# ---------- manifest / validate ----------
def manifest(cls: type[Strategy]) -> dict:
    params = [p.spec() for p in cls.params()]
    grid = {p.name: p.grid for p in cls.params() if p.grid}
    return {"name": cls.name, "label": cls.label or cls.name.replace("_", " ").title(), "version": cls.version,
            "description": (cls.description or (cls.__doc__ or "")).strip().splitlines()[0] if (cls.description or cls.__doc__) else "",
            "instruments": [normalize_instrument(i) for i in cls.instruments],
            "timeframe": cls.timeframe, "tf_minutes": TIMEFRAMES.get(cls.timeframe),
            "params": params, "grid": grid, "requires": list(cls.requires), "timer_interval_s": float(cls.timer_interval_s),
            "summary": str(cls.summary or ""), "max_drawdown_pct": float(cls.max_drawdown_pct), "limits": limits(cls), "sdk": 1}


def limits(cls: type[Strategy]) -> dict:
    """Effective limits (declared values or the QOS defaults)"""
    from qx_strategies.package_limits import DEFAULTS, KEYS
    out = {}
    for k in KEYS:
        v = getattr(cls, k, None)
        out[k] = DEFAULTS[k] if v is None else (int(v) if k != "max_notional_usd" else float(v))
    return out


def validate(cls: type[Strategy]) -> list[str]:
    """Return a list of errors (empty = valid)"""
    errs: list[str] = []
    if not isinstance(cls, type) or not issubclass(cls, Strategy) or cls is Strategy:
        return ["not a subclass of qos.sdk.Strategy"]
    if not NAME_RE.match(cls.name or ""):
        errs.append(f"name '{cls.name}' must be 2–32 chars: a-z, 0-9, _ (starting with a letter)")
    if not VERSION_RE.match(str(cls.version)):
        errs.append(f"version '{cls.version}' must be MAJOR.MINOR.PATCH")
    if not cls.instruments or not isinstance(cls.instruments, (list, tuple)):
        errs.append("instruments must be a non-empty list, e.g. ['EUR/USD']")
    else:
        for i in cls.instruments:
            try:
                normalize_instrument(i)
            except ValueError as e:
                errs.append(str(e))
    try:
        if not 0 < float(cls.max_drawdown_pct) <= 100:
            errs.append("max_drawdown_pct must be between 0 and 100")
    except (TypeError, ValueError):
        errs.append("max_drawdown_pct must be a number")
    for k, lo, hi in (("max_notional_usd", 1_000, 50_000_000), ("max_orders_per_hour", 1, 3_600), ("max_flips_per_hour", 0, 3_600),
                      ("max_memory_mb", 256, 32_768)):
        v = getattr(cls, k, None)
        if v is None:
            continue
        try:
            if not lo <= float(v) <= hi:
                errs.append(f"{k} must be between {lo:,} and {hi:,}")
        except (TypeError, ValueError):
            errs.append(f"{k} must be a number")
    if cls.timeframe not in TIMEFRAMES:
        errs.append(f"timeframe '{cls.timeframe}' must be one of {', '.join(TIMEFRAMES)}")
    seen = set()
    for p in cls.params():
        if p.name in seen:
            errs.append(f"duplicate param '{p.name}'")
        seen.add(p.name)
        if p.name in RESERVED:
            errs.append(f"param '{p.name}' clashes with a reserved config key")
        errs += p.check()
    if "on_bar" not in {k for klass in cls.__mro__[:-2] for k in vars(klass)} and cls.on_bar is Strategy.on_bar:
        errs.append("on_bar() is not implemented")
    for forbidden in ("submit_order", "order_factory", "portfolio", "cache", "msgbus", "clock"):
        if forbidden in vars(cls):
            errs.append(f"'{forbidden}' is reserved for QOS and may not be defined by a strategy")
    return errs


RESERVED = {"strategy", "strategy_id", "instrument", "tf_minutes", "trade_size", "sl_pips", "tp_pips", "allow_short",
            "warmup_days", "order_id_tag", "state_file", "close_positions_on_stop", "instrument_id", "bar_type"}


# ---------- adapter: SDK class → QXBaseStrategy ----------
def compile(cls: type[Strategy]):
    """Return a qx_strategies-style module object: NAME, LABEL, PARAMS, GRID, summary(), Config, Strategy
    The returned Strategy is a QXBaseStrategy wrapping an instance of cls (the developer never sees Nautilus)"""
    import msgspec

    from qx_strategies.base import QXBaseConfig

    errs = validate(cls)
    if errs:
        raise ValueError(f"{cls.__name__}: " + "; ".join(errs))
    m = manifest(cls)
    pytypes = {"int": int, "float": float, "str": str, "bool": bool}
    fields = [(p.name, pytypes[p.type], p.default) for p in cls.params()] + [("state_file", str, "")]
    Config = msgspec.defstruct(f"{cls.__name__}Config", fields, bases=(QXBaseConfig,), kw_only=True, frozen=True)

    from .adapter import make_adapter
    Adapter = make_adapter(cls, Config)

    def summary(c: dict) -> str:
        return summary_text(m, c)

    class _Module:
        NAME, LABEL, PARAMS, GRID = m["name"], m["label"], m["params"], m["grid"]
        MANIFEST = m
        SDK_CLASS = cls
        __doc__ = m["description"] or m["label"]
    _Module.Config, _Module.Strategy, _Module.summary = Config, Adapter, staticmethod(summary)
    return _Module


def summary_text(m: dict, c: dict) -> str:
    """Short text from the manifest (no need to import strategy code): the summary format string or label + grid parameter values"""
    values = {p["key"]: c.get(p["key"], p["default"]) for p in m.get("params", [])}
    fmt = m.get("summary") or ""
    if fmt:
        try:
            return fmt.format(**{k: (str(v).rsplit(".", 1)[0] if k.endswith("_file") else v) for k, v in values.items()})
        except (KeyError, IndexError, ValueError):
            pass
    shown = [p for p in m.get("params", []) if p["key"] in (m.get("grid") or {})][:3] or m.get("params", [])[:3]
    return (m.get("label", m.get("name", "?")) + " " + "/".join(str(values[p["key"]]) for p in shown)).strip()


def default_config(cls: type[Strategy], **overrides) -> dict:
    """Default config dict for build_strategy / backtest from the class metadata"""
    m = manifest(cls)
    cfg = {"strategy": m["name"], "instrument": m["instruments"][0], "tf_minutes": m["tf_minutes"],
           "trade_size": "20000", "sl_pips": 0, "tp_pips": 0, "allow_short": True}
    cfg.update({p["key"]: p["default"] for p in m["params"]})
    cfg.update(overrides)
    return cfg


def sibling(file: str, name: str):
    """Import a file next to strategy.py (e.g. features.py) without clashing with other packages:  features = sibling(__file__, "features")
    Supports compiled modules (features.cpython-313-darwin.so) of Agents from the QOS Store (step 39)"""
    import hashlib
    import importlib.machinery
    import importlib.util
    import sys
    from pathlib import Path
    folder = Path(file).resolve().parent
    path = folder / f"{name}.py"
    tag = hashlib.sha1(str(folder / name).encode()).hexdigest()[:12]
    if not path.exists():
        ext = next((folder / f"{name}{sfx}" for sfx in importlib.machinery.EXTENSION_SUFFIXES if (folder / f"{name}{sfx}").exists()), None)
        if ext is not None:
            modname = f"qos_sibling_{tag}.{name}"              # the last part must match the compiled module's PyInit_<name>
            if modname in sys.modules:
                return sys.modules[modname]
            spec = importlib.util.spec_from_file_location(modname, ext)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            return mod
    modname = f"qos_sibling_{tag}_{name}"
    if modname in sys.modules:
        return sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod
