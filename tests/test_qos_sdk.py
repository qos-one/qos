import json

import pytest

from qos.sdk import Param, Strategy, compile, default_config, manifest, validate
from qx_strategies import REGISTRY, SPECS, backtest as bt, build_strategy
from test_qx_strategies import CFG, _sine_bars


class SdkEma(Strategy):
    """EMA cross written against the SDK — must behave exactly like the built-in ema_cross"""
    name = "sdk_ema"
    label = "SDK EMA"
    version = "1.0.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"
    fast = Param(5, min=2, max=200, grid=[5, 10])
    slow = Param(15, min=3, max=400, grid=[15, 30])

    def on_start(self):
        self.f = self.indicator("ema", self.p.fast)
        self.s = self.indicator("ema", self.p.slow)
        self.state.setdefault("side", 0)
        self.state.setdefault("bars", 0)

    def on_bar(self, bar):
        self.state["bars"] += 1
        if not self.ready():
            return
        side = 1 if self.f.value > self.s.value else -1 if self.f.value < self.s.value else 0
        prev = self.state["side"]
        if side:
            self.state["side"] = side
        if side == 0 or prev == 0 or side == prev:
            return
        self.target(side)
        self.publish("cross_up" if side > 0 else "cross_down", fast=round(self.f.value, 6))


def test_param_descriptor_and_manifest():
    m = manifest(SdkEma)
    assert m["name"] == "sdk_ema" and m["tf_minutes"] == 5 and m["instruments"] == ["EUR/USD.IDEALPRO"]
    assert [p["key"] for p in m["params"]] == ["fast", "slow"] and m["params"][0]["type"] == "int"
    assert m["grid"] == {"fast": [5, 10], "slow": [15, 30]}
    assert validate(SdkEma) == []
    assert SdkEma.fast.default == 5


def test_validate_catches_mistakes():
    class Bad(Strategy):
        name = "Bad Name"
        version = "1"
        instruments = []
        timeframe = "2m"
        k = Param(5.0, min=10)
        trade_size = Param(1)
    errs = validate(Bad)
    joined = " | ".join(errs)
    for frag in ("name", "version", "instruments", "timeframe", "default 5.0 < min", "reserved", "on_bar"):
        assert frag in joined, (frag, errs)
    assert validate(Strategy) == ["not a subclass of qos.sdk.Strategy"]
    with pytest.raises(ValueError):
        compile(Bad)


def test_sdk_strategy_matches_builtin_ema_cross():
    bars = _sine_bars()
    legacy_actions: list = []
    legacy = bt.run_backtest(CFG, bars=bars, log_level="OFF", _collect_actions=legacy_actions)
    sdk_actions: list = []
    cfg = default_config(SdkEma, trade_size=CFG["trade_size"], fast=5, slow=15)
    build_strategy(cfg, strategy_cls=SdkEma)                      # registers
    sdk = bt.run_backtest(cfg, bars=bars, log_level="OFF", _collect_actions=sdk_actions)
    assert len(legacy_actions) >= 20
    assert sdk_actions == legacy_actions
    assert sdk["pnl_usd"] == pytest.approx(legacy["pnl_usd"], abs=0.01)
    assert sdk["signals"] == legacy["signals"]
    assert "sdk_ema" in REGISTRY and SPECS["sdk_ema"]["sdk"] is True


def test_params_are_read_only_and_overridable():
    s = build_strategy({"fast": 7, "slow": 21}, strategy_cls=SdkEma)
    assert s.user.p.fast == 7 and s.user.p.slow == 21 and s.user.fast == 7
    with pytest.raises(AttributeError):
        s.user.p.fast = 9
    with pytest.raises(AttributeError):
        s.user.fast = 9


def test_state_persists_across_runs(tmp_path):
    bars = _sine_bars(n=600)
    sf = tmp_path / "node.json"
    cfg = default_config(SdkEma, state_file=str(sf))
    bt.run_backtest(cfg, bars=bars, log_level="OFF")
    saved = json.loads(sf.read_text())
    assert saved["bars"] == len(bars) and saved["side"] in (-1, 1)
    bt.run_backtest(cfg, bars=bars[:50], log_level="OFF")
    assert json.loads(sf.read_text())["bars"] == len(bars) + 50      # loads the previous state and keeps counting


def test_target_qty_scales_in_and_out(tmp_path):
    class Scaler(Strategy):
        name = "sdk_scaler"
        instruments = ["EUR/USD"]
        timeframe = "5m"
        step = Param(10000, min=1000, max=100000)

        def on_bar(self, bar):
            n = self.state.get("n", 0) + 1
            self.state["n"] = n
            plan = {10: self.p.step, 20: 3 * self.p.step, 30: self.p.step, 40: -2 * self.p.step, 50: 0}
            if n in plan:
                self.target_qty(plan[n])
            if n in (15, 25, 35, 45, 55):
                self.state.setdefault("pos", []).append(self.position())
    bars = _sine_bars(n=80)
    sf = tmp_path / "scaler.json"
    cfg = default_config(Scaler, trade_size="10000", state_file=str(sf))
    build_strategy(cfg, strategy_cls=Scaler)
    res = bt.run_backtest(cfg, bars=bars, log_level="OFF")
    assert json.loads(sf.read_text())["pos"] == [10000.0, 30000.0, 10000.0, -20000.0, 0.0]
    assert res["closed_positions"] == 2


def test_target_qty_respects_allow_short(tmp_path):
    class ShortSeeker(Strategy):
        name = "sdk_short"
        instruments = ["EUR/USD"]
        timeframe = "5m"

        def on_bar(self, bar):
            n = self.state.get("n", 0) + 1
            self.state["n"] = n
            if n == 5:
                self.target_qty(-10000)
            if n == 8:
                self.state["pos"] = self.position()
    sf = tmp_path / "s.json"
    cfg = default_config(ShortSeeker, allow_short=False, state_file=str(sf))
    build_strategy(cfg, strategy_cls=ShortSeeker)
    bt.run_backtest(cfg, bars=_sine_bars(n=20), log_level="OFF")
    assert json.loads(sf.read_text())["pos"] == 0.0
