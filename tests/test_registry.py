import json
import os
import shutil
from pathlib import Path

import pytest

from qx_strategies import REGISTRY, SPECS, backtest as bt, build_strategy, resolve_ref, summary
from qx_strategies.registry import REG, Registry, strategies_dir
from test_qx_strategies import CFG, _sine_bars

REAL_PKGS = Path(os.environ.get("QOS_REAL_STRATEGIES") or os.path.expanduser("~/trading/strategies"))
GOOD = '''
from qos.sdk import Param, Strategy

class Demo(Strategy):
    name = "demo"
    version = "{ver}"
    instruments = ["EUR/USD"]
    timeframe = "5m"
    k = Param({k}, min=1, max=10)

    def on_bar(self, bar):
        if not self.state.get("done"):
            self.target(+1)
            self.state["done"] = True
'''


@pytest.fixture()
def pkgdir(tmp_path, monkeypatch):
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(tmp_path))
    reg = Registry()
    for m in REG.builtins.values():
        reg.add_builtin(m.module)
    monkeypatch.setattr("qx_strategies.registry.REG", reg)
    monkeypatch.setattr("qx_strategies.REG", reg)
    return tmp_path


def _write(base: Path, name: str, ver: str, src: str) -> Path:
    d = base / f"{name}@{ver}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "strategy.py").write_text(src)
    return d


def test_discovery_versions_and_fallback(pkgdir):
    from qx_strategies import REG as reg
    assert resolve_ref("ema_cross") == "ema_cross@builtin"
    _write(pkgdir, "demo", "1.0.0", GOOD.format(ver="1.0.0", k=3))
    reg.scan(force=True)
    assert resolve_ref("demo") == "demo@1.0.0"
    assert (pkgdir / "demo@1.0.0" / "manifest.json").exists()
    assert "demo" in SPECS and SPECS["demo"]["params"][0]["key"] == "k" and SPECS["demo"]["versions"] == ["1.0.0"]
    _write(pkgdir, "demo", "1.1.0", GOOD.format(ver="1.1.0", k=5))
    reg.scan(force=True)
    assert resolve_ref("demo") == "demo@1.1.0" and SPECS["demo"]["versions"] == ["1.0.0", "1.1.0"]
    assert SPECS["demo@1.0.0"]["params"][0]["default"] == 3 and SPECS["demo"]["params"][0]["default"] == 5
    assert "demo@1.0.0" in REGISTRY and "demo@9.9.9" not in REGISTRY
    assert build_strategy({"strategy": "demo@1.0.0", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5}).user.p.k == 3
    assert build_strategy({"strategy": "demo", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5}).user.p.k == 5
    assert summary({"strategy": "demo", "k": 7}) == "Demo 7"


def test_broken_package_does_not_break_registry(pkgdir):
    from qx_strategies import REG as reg
    _write(pkgdir, "demo", "1.0.0", GOOD.format(ver="1.0.0", k=3))
    _write(pkgdir, "broken", "1.0.0", "import nonexistent_module_xyz\n")
    _write(pkgdir, "mismatch", "1.0.0", GOOD.format(ver="2.0.0", k=3).replace('"demo"', '"mismatch"'))
    reg.scan(force=True)
    assert resolve_ref("demo") == "demo@1.0.0" and resolve_ref("ema_cross") == "ema_cross@builtin"
    assert SPECS["broken"]["status"] == "error" and "nonexistent_module_xyz" in SPECS["broken"]["error"]
    assert "does not match folder" in SPECS["mismatch"]["error"]
    with pytest.raises(ValueError, match="nonexistent_module_xyz"):
        build_strategy({"strategy": "broken", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5})
    assert "broken" not in REGISTRY


def test_edit_during_run_is_picked_up(pkgdir):
    from qx_strategies import REG as reg
    d = _write(pkgdir, "demo", "1.0.0", GOOD.format(ver="1.0.0", k=3))
    reg.scan(force=True)
    assert SPECS["demo"]["params"][0]["default"] == 3
    (d / "strategy.py").write_text(GOOD.format(ver="1.0.0", k=4))
    os.utime(d / "strategy.py", (1, 1))        # force a different mtime
    reg.scan(force=True)
    assert SPECS["demo"]["params"][0]["default"] == 4
    assert build_strategy({"strategy": "demo", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5}).user.p.k == 4


@pytest.mark.skipif(not REAL_PKGS.exists(), reason="packages not installed on this machine")
@pytest.mark.parametrize("name,cfg", [
    ("ema_cross", {**CFG}),
])
def test_packages_match_builtins(name, cfg, monkeypatch):
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(REAL_PKGS))
    REG.scan(force=True)
    assert resolve_ref(name) == f"{name}@1.0.0"
    bars = _sine_bars()
    a, b = [], []
    old = bt.run_backtest({**cfg, "strategy": f"{name}@builtin"}, bars=bars, log_level="OFF", _collect_actions=a)
    new = bt.run_backtest({**cfg, "strategy": f"{name}@1.0.0"}, bars=bars, log_level="OFF", _collect_actions=b)
    assert len(a) >= 1 and a == b
    assert new["pnl_usd"] == pytest.approx(old["pnl_usd"], abs=0.01) and new["signals"] == old["signals"]


CATALOG = Path(os.environ.get("QOS_CATALOG") or os.path.expanduser("~/trading/bot/data/catalog"))
BOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not (REAL_PKGS.exists() and CATALOG.exists()), reason="needs installed packages and the data catalog")
@pytest.mark.parametrize("file", ["ema_cross_eurusd_5m.json"])
def test_packages_match_builtins_on_catalog(file, monkeypatch):
    """60-day parity on real data: the @1.0.0 package must decide like the built-in strategy on every bar"""
    import pandas as pd
    from qx_strategies import load_config
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(REAL_PKGS))
    REG.scan(force=True)
    cfg = load_config(BOT / "configs" / "strategies" / file)
    name = cfg["strategy"].split("@")[0]
    if name == "ml_signal" and not (BOT / "models" / cfg["model_file"]).exists():
        pytest.skip("model file missing")
    end = pd.Timestamp.now(tz="UTC").floor("D")
    bars = bt.load_bars(cfg, (end - pd.Timedelta(days=60)).isoformat(), end.isoformat(), CATALOG)
    if len(bars) < 500:
        pytest.skip("catalog too short")
    a, b = [], []
    old = bt.run_backtest({**cfg, "strategy": f"{name}@builtin"}, bars=bars, log_level="OFF", _collect_actions=a, catalog_dir=CATALOG)
    new = bt.run_backtest({**cfg, "strategy": f"{name}@1.0.0"}, bars=bars, log_level="OFF", _collect_actions=b, catalog_dir=CATALOG)
    assert len(a) >= 3, f"{name}: only {len(a)} decisions in 60 days"
    assert a == b, f"{name}: {sum(1 for x, y in zip(a, b) if x != y)} of {len(a)} decisions differ"
    assert new["pnl_usd"] == pytest.approx(old["pnl_usd"], abs=0.01)
    assert new["closed_positions"] == old["closed_positions"]
