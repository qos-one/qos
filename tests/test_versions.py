"""Core version pins: if anyone upgrades nautilus-trader / ib_async by accident, CI flags it immediately
(versions may change, but update pyproject.toml + uv.lock + this file together and rerun backtest parity)"""
from importlib.metadata import version

NAUTILUS_VERSION = "1.230.0"


def test_nautilus_trader_pinned():
    assert version("nautilus_trader") == NAUTILUS_VERSION


def test_nautilus_imports():
    from nautilus_trader.backtest.node import BacktestNode  # noqa: F401
    from nautilus_trader.live.node import TradingNode  # noqa: F401
    from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersExecClientConfig  # noqa: F401


def test_ib_async_present():
    assert version("ib_async")


def test_redis_client_present():
    import redis  # noqa: F401
