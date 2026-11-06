"""pytest tests — runs on synthetic bars, no QOS or data catalog needed"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qos.sdk import quick_backtest, sibling, validate  # noqa: E402
from qos.sdk.testing import synthetic_bars  # noqa: E402
from strategy import $Class  # noqa: E402

BARS = synthetic_bars("EUR/USD.IDEALPRO", 5, n=3000, seed=11)
features = sibling(str(Path(__file__).resolve().parents[1] / "strategy.py"), "features")


def test_manifest_is_valid():
    assert validate($Class) == []


def test_features_use_only_past_bars():
    closes = [1.0 + i * 1e-4 for i in range(features.WINDOW + 5)]
    assert features.make_features(closes) == features.make_features(closes[: features.WINDOW + 5])
    assert len(features.make_features(closes)) == features.WINDOW


def test_model_loads_and_strategy_runs():
    r = quick_backtest($Class, {"threshold": 0.5}, bars=BARS)
    assert len(r["actions"]) > 0


def test_same_decisions_every_run():
    assert quick_backtest($Class, bars=BARS)["actions"] == quick_backtest($Class, bars=BARS)["actions"]
