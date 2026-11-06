"""Step 23: external developer path — qos new → pytest → qos install, quick_backtest = QOS backtest, exported data, public examples"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from qos import install as qi
from qos.cli import new_package

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(os.environ.get("QOS_CATALOG") or ROOT / "data" / "catalog")
EXAMPLES = Path(os.environ.get("QOS_EXAMPLES") or ROOT.parent / "qos-examples")
HAS_CATALOG = (CATALOG / "data").exists()


@pytest.fixture()
def pkgdir(tmp_path, monkeypatch):
    from qx_strategies.registry import REG, Registry
    d = tmp_path / "strategies"
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(d))
    reg = Registry()
    for m in REG.builtins.values():
        reg.add_builtin(m.module)
    monkeypatch.setattr("qx_strategies.registry.REG", reg)
    monkeypatch.setattr("qx_strategies.REG", reg)
    return d


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"t_{abs(hash(str(path)))}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    from qos.sdk import Strategy
    return next(v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy)


def _pytest(folder: Path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(folder / "tests")],
                          cwd=folder, capture_output=True, text=True, env=env, timeout=300)


@pytest.mark.parametrize("ml", [False, True], ids=["rule", "ml"])
def test_new_then_install_without_edits(tmp_path, pkgdir, ml):
    name = "my_model" if ml else "my_ema"
    d = new_package(name, ml=ml, parent=tmp_path, log=lambda *_: None)
    assert (d / "strategy.py").exists() and (d / "tests" / "test_strategy.py").exists() and (d / "README.md").exists()
    if ml:
        assert (d / "features.py").exists() and (d / "model.joblib").exists() and not (d / "__pycache__").exists()
    r = _pytest(d)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-800:]
    out = qi.install(str(d), bars="synthetic", days=10, api=None, log=lambda *_: None)
    assert out["ref"] == f"{name}@0.1.0" and all(c["ok"] for c in out["checks"])
    assert (pkgdir / f"{name}@0.1.0" / "strategy.py").exists()
    if ml:
        assert (pkgdir / f"{name}@0.1.0" / "model.joblib").exists()


def test_new_refuses_bad_names_and_existing(tmp_path):
    with pytest.raises(ValueError):
        new_package("Bad-Name", parent=tmp_path, log=lambda *_: None)
    new_package("dup", parent=tmp_path, log=lambda *_: None)
    with pytest.raises(ValueError, match="already exists"):
        new_package("dup", parent=tmp_path, log=lambda *_: None)


def test_quick_backtest_equals_qos_backtest(tmp_path, pkgdir):
    from qos.sdk import quick_backtest
    from qos.sdk.testing import synthetic_bars
    from qx_strategies import backtest as bt
    d = new_package("eq_ema", parent=tmp_path, log=lambda *_: None)
    qi.install(str(d), bars="synthetic", days=10, api=None, log=lambda *_: None)
    cls = _load(d / "strategy.py")
    bars = synthetic_bars("EUR/USD.IDEALPRO", 5, n=3000)
    for params in ({}, {"fast": 5, "slow": 40}):
        q = quick_backtest(cls, params, bars)
        acts: list = []
        cfg = {"strategy": "eq_ema@0.1.0", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5, "trade_size": "20000", **params}
        r = bt.run_backtest(cfg, bars=bars, log_level="OFF", _collect_actions=acts)
        assert q["actions"] == [(int(t), int(v)) for t, v in acts] and q["actions"]
        for k in ("pnl_usd", "closed_positions", "max_drawdown_usd", "win_rate", "orders"):
            assert q[k] == r[k], k
    with pytest.raises(ValueError, match="unknown parameter"):
        quick_backtest(cls, {"fats": 3}, bars)


@pytest.mark.skipif(not HAS_CATALOG, reason="no data catalog")
def test_catalog_bars_and_export_roundtrip(tmp_path):
    from qos.sdk import catalog
    a = catalog.bars("EURUSD", "15m", "2026-10-01", "2026-10-08", source=CATALOG)
    info = catalog.export("EUR/USD", "5m", tmp_path / "eur.parquet", "2026-10-01", "2026-10-08", catalog=CATALOG)
    assert info["bars"] > 1000 and info["instrument"] == "EUR/USD.IDEALPRO"
    b = catalog.bars("EUR/USD", "15m", "2026-10-01", "2026-10-08", source=tmp_path / "eur.parquet")
    assert len(a) > 100 and abs(len(a) - len(b)) <= 1
    common = {x.ts_event: float(x.close) for x in a}
    assert all(abs(common[x.ts_event] - float(x.close)) < 1e-9 for x in b if x.ts_event in common)
    with pytest.raises(ValueError, match="holds"):
        catalog.bars("GBP/USD", "5m", source=tmp_path / "eur.parquet")


@pytest.mark.skipif(not EXAMPLES.exists(), reason="no strategies-examples folder")
@pytest.mark.parametrize("name", ["rsi_reversion"])
def test_examples_install_and_tests(name, pkgdir):
    d = EXAMPLES / name
    out = qi.install(str(d), bars="synthetic", days=10, api=None, log=lambda *_: None)
    assert out["ref"] == f"{name}@1.0.0" and all(c["ok"] for c in out["checks"])
    r = _pytest(d)
    assert r.returncode == 0, r.stdout[-1500:]


@pytest.mark.skipif(not (EXAMPLES.exists() and HAS_CATALOG), reason="needs examples + data catalog")
@pytest.mark.parametrize("name,inst", [("rsi_reversion", "GBP/USD")])
def test_examples_pass_backtest_criteria_on_fixed_window(name, inst):
    """installed → backtested criteria (net > 0, DD ≤ 10%) on the 90-day window checked when the example was made (Jul 29 – Oct 27, 2026)"""
    from qos.sdk import catalog, quick_backtest
    cls = _load(EXAMPLES / name / "strategy.py")
    r = quick_backtest(cls, None, catalog.bars(inst, "15m", "2026-07-29", "2026-10-27", source=CATALOG))
    assert r["pnl_usd"] > 0 and r["max_drawdown_usd"] <= 10_000, (r["pnl_usd"], r["max_drawdown_usd"])
