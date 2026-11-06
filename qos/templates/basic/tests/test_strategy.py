"""pytest tests — runs on synthetic bars, no QOS or data catalog needed"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qos.sdk import quick_backtest, validate  # noqa: E402
from qos.sdk.testing import synthetic_bars  # noqa: E402
from strategy import $Class  # noqa: E402

BARS = synthetic_bars("EUR/USD.IDEALPRO", 5, n=3000)


def test_manifest_is_valid():
    assert validate($Class) == []


def test_trades_on_synthetic_bars():
    r = quick_backtest($Class, bars=BARS)
    assert r["closed_positions"] > 0


def test_same_decisions_every_run():
    assert quick_backtest($Class, bars=BARS)["actions"] == quick_backtest($Class, bars=BARS)["actions"]


def test_rejects_bad_parameters():
    with pytest.raises(ValueError):
        quick_backtest($Class, {"fast": 50, "slow": 20}, bars=BARS)
