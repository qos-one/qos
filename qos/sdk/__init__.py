"""qos.sdk — write a single-file strategy that QOS can run in both backtest and live (see docs/DEVELOPER.md)"""
from .params import Param
from .quick import quick_backtest
from .strategy import Strategy, compile, default_config, manifest, sibling, validate

__version__ = "0.1.0"
__all__ = ["Param", "Strategy", "compile", "default_config", "manifest", "quick_backtest", "sibling", "validate"]
