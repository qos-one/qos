"""Step 39: compile an agent with Cython (results must match the source code), check pickle model files, machine-bound licence"""
import json
import pickle
import shutil
import time
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qos.cloud.build import compile_package, platform_tag, smoke
from qos.publish_check import pickle_globals, scan

EXAMPLES = Path(os.environ.get("QOS_EXAMPLES") or Path(__file__).resolve().parents[2] / "qos-examples")
needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")


def _bars():
    from qos.sdk import catalog
    idx = pd.date_range("2026-09-01", periods=4000, freq="15min", tz="UTC")
    rng = np.random.default_rng(11)
    c = 1.27 + np.cumsum(rng.normal(0, 0.0006, len(idx)))
    df = pd.DataFrame({"open": c, "high": c + 0.0004, "low": c - 0.0004, "close": c}, index=idx)
    return catalog.frame_to_bars(df, "GBP/USD", "15m")


def _backtest(strategies_dir: Path, monkeypatch) -> dict:
    from qx_strategies.backtest import run_backtest
    from qx_strategies.registry import REG
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(strategies_dir))
    REG.scan(force=True)
    cfg = {"strategy": "rsi_reversion@1.0.0", "strategy_id": "QOS-rsi-test", "instrument": "GBP/USD.IDEALPRO", "tf_minutes": 15,
           "trade_size": "20000"}
    r = run_backtest(cfg, bars=_bars(), log_level="OFF")
    return {k: r[k] for k in ("closed_positions", "pnl_usd", "orders")}


@needs_uvx
def test_compiled_matches_source(tmp_path, monkeypatch):
    src_dir, bin_dir = tmp_path / "src", tmp_path / "bin"
    shutil.copytree(EXAMPLES / "rsi_reversion", src_dir / "rsi_reversion@1.0.0", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    info = compile_package(src_dir / "rsi_reversion@1.0.0", bin_dir / "rsi_reversion@1.0.0")
    pkg = bin_dir / "rsi_reversion@1.0.0"
    assert info["platform"] == platform_tag() and info["modules"] == ["_agent_impl"]
    assert [p.name for p in pkg.glob("*.py")] == ["strategy.py"] and "RsiReversion" in (pkg / "strategy.py").read_text()
    assert "self.target" not in (pkg / "strategy.py").read_text()
    smoke(pkg)
    a = _backtest(src_dir, monkeypatch)
    b = _backtest(bin_dir, monkeypatch)
    assert a == b and a["closed_positions"] > 0


@needs_uvx
def test_compiled_ml_agent_loads_siblings_and_model(tmp_path, ml_example):
    pkg = tmp_path / "ml_bin"
    compile_package(ml_example, pkg)
    mods = sorted(p.name.split(".")[0] for p in pkg.iterdir() if p.suffix in (".so", ".pyd"))
    assert "_agent_impl" in mods and "features" in mods and (pkg / "model.joblib").exists()
    assert not (pkg / "features.py").exists() and not (pkg / "train.py").exists()
    smoke(pkg)


def test_pickle_scan(tmp_path, ml_example):
    import os
    assert pickle_globals(ml_example / "model.joblib") and all(
        g.startswith(("sklearn.", "numpy.", "joblib.")) for g in pickle_globals(ml_example / "model.joblib"))

    class Evil:
        def __reduce__(self):
            return (os.system, ("echo pwned",))
    pkg = tmp_path / "p"
    shutil.copytree(ml_example, pkg, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    assert scan(pkg)["ok"]
    (pkg / "model.joblib").write_bytes(pickle.dumps(Evil()))
    r = scan(pkg)
    assert not r["ok"] and any("system" in i["message"] for i in r["issues"])
    (pkg / "model.joblib").write_bytes(b"\x00garbage")
    r = scan(pkg)
    assert r["ok"] and any("could not be inspected" in i["message"] for i in r["issues"])


def test_licence_lets_paid_agent_start_offline(tmp_path):
    from qos.cloud import store
    from test_cloud_connector import FakeCloud, signed_in
    from test_store import _installed
    fake = FakeCloud()
    c = signed_in(fake, tmp_path)
    dest = _installed(fake, c, tmp_path / "strategies", sku="agent.rsi_reversion")
    assert "needs agent.rsi_reversion" in store.check_installed(dest, c)[1]
    did = c._state()["device_id"]
    lic = fake.sign({"sub": "u1", "did": did, "agent": "rsi_reversion", "sku": "agent.rsi_reversion", "perpetual": False},
                    "qos-lic+jwt", life=30 * 86400)
    store.licence_path(c, "rsi_reversion").parent.mkdir(parents=True, exist_ok=True)
    store.licence_path(c, "rsi_reversion").write_text(lic)
    assert store.check_installed(dest, c) == (True, "")
    other = fake.sign({"sub": "u1", "did": "another", "agent": "rsi_reversion"}, "qos-lic+jwt")
    store.licence_path(c, "rsi_reversion").write_text(other)
    assert not store.check_installed(dest, c)[0]
    expired = fake.sign({"sub": "u1", "did": did, "agent": "rsi_reversion"}, "qos-lic+jwt", life=-3600)
    store.licence_path(c, "rsi_reversion").write_text(expired)
    assert not store.check_installed(dest, c)[0]
