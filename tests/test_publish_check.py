"""Step 36: pre-publish agent check (scan + checks + backtest + walk-forward)"""
import shutil
import os
from pathlib import Path

import pandas as pd
import pytest

from qos.publish_check import run, scan, scan_source

EXAMPLES = Path(os.environ.get("QOS_EXAMPLES") or Path(__file__).resolve().parents[2] / "qos-examples")


@pytest.mark.parametrize("src,needle", [
    ("import socket", "socket"), ("from urllib.request import urlopen", "urllib"), ("import subprocess as sp", "subprocess"),
    ("import os\nos.system('ls')", "os.system"), ("import os as o\no.environ['X']", "os.environ"),
    ("from os import getenv", "os.getenv"), ("eval('1')", "eval()"), ("exec('x=1')", "exec()"), ("__import__('os')", "__import__"),
    ("().__class__.__bases__[0].__subclasses__()", "__subclasses__"), ("import importlib", "importlib"),
    ("import pickle", "pickle"), ("import ctypes", "ctypes"), ("from qos.cloud import client", "qos.cloud"),
    ("getattr(x, 'y')", "getattr()"), ("import sys", "sys"), ("x = '" + "a" * 20001 + "'", "literal"),
])
def test_scan_blocks(src, needle):
    issues = scan_source("strategy.py", src)
    assert issues and any(needle in i["message"] for i in issues), issues


def test_scan_allows_normal_code():
    src = "import math\nimport numpy as np\nimport pandas as pd\nfrom qos.sdk import Param, Strategy\nimport os.path\nx = os.path.join('a', 'b')\n"
    assert scan_source("strategy.py", src) == []


def test_scan_package_rules(tmp_path):
    pkg = tmp_path / "p"
    shutil.copytree(EXAMPLES / "rsi_reversion", pkg, ignore=shutil.ignore_patterns("__pycache__"))
    ok = scan(pkg, "rsi_reversion", "1.0.0")
    assert ok["ok"] and ok["name"] == "rsi_reversion", ok["issues"]
    assert not scan(pkg, "other", "1.0.0")["ok"]
    assert not scan(pkg, "rsi_reversion", "2.0.0")["ok"]
    (pkg / "evil.so").write_bytes(b"\x7fELF")
    (pkg / "link.py").symlink_to("/etc/passwd")
    msgs = " ".join(i["message"] for i in scan(pkg)["issues"])
    assert "file type .so" in msgs and "symlinks" in msgs
    (pkg / "evil.so").unlink()
    (pkg / "link.py").unlink()
    (pkg / "strategy.py").write_text((pkg / "strategy.py").read_text().replace('version = "1.0.0"', 'version = "1.0.0"\n    requires = ["ta==0.11.0"]'))
    assert any("requires" in i["message"] for i in scan(pkg)["issues"])
    assert not scan(tmp_path / "missing")["ok"]


def test_ml_example_warns_about_model(tmp_path, ml_example):
    pkg = tmp_path / "ml"
    shutil.copytree(ml_example, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    r = scan(pkg)
    assert r["ok"], r["issues"]
    assert any(i["level"] == "warn" and "model file" in i["message"] for i in r["issues"])


def _catalog(tmp_path) -> Path:
    from qos.sdk import catalog
    cat_dir = tmp_path / "cat"
    cat = catalog.open_catalog(cat_dir)
    end = pd.Timestamp.now(tz="UTC").floor("min")
    idx = pd.date_range(end=end, periods=60 * 24 * 40, freq="1min", tz="UTC")
    import numpy as np
    rng = np.random.default_rng(7)
    close = 1.27 + np.cumsum(rng.normal(0, 0.00015, len(idx)))
    df = pd.DataFrame({"open": close, "high": close + 0.0002, "low": close - 0.0002, "close": close}, index=idx)
    cat.write_data([catalog.make_fx_instrument("GBPUSD")])
    cat.write_data(catalog.frame_to_bars(df, "GBP/USD", "1m"))
    return cat_dir


def test_full_run_on_catalog(tmp_path, monkeypatch):
    for k in ("QOS_CATALOG", "QOS_STRATEGIES_DIR", "QOS_REPORTS_DIR"):
        monkeypatch.setenv(k, "")
    pkg = tmp_path / "p"
    shutil.copytree(EXAMPLES / "rsi_reversion", pkg, ignore=shutil.ignore_patterns("__pycache__"))
    logs = []
    r = run(pkg, str(_catalog(tmp_path)), days=20, wf_days=35, log=logs.append)
    assert r["checks"]["ok"], r["checks"]
    # random-walk data: the evaluation runs, and ok reflects the criteria (it used to be true even when they failed)
    assert r["backtest"] is not None and r["walkforward"] is not None, r.get("error")
    passed = bool(r["backtest"]["ok"] and r["walkforward"]["ok"])
    assert r["ok"] is passed and r["stage"] == ("done" if passed else ("backtest" if not r["backtest"]["ok"] else "walkforward"))
    assert r["meta"]["name"] == "rsi_reversion" and r["meta"]["timeframe"] == "15m"
    assert r["backtest"]["instrument"] == "GBP/USD.IDEALPRO" and r["backtest"]["trades"] >= 0
    assert "folds" in r["walkforward"] or "error" in r["walkforward"]
    assert any(s.startswith("backtest") for s in logs)


def test_scan_failure_stops_early(tmp_path):
    pkg = tmp_path / "p"
    shutil.copytree(EXAMPLES / "rsi_reversion", pkg, ignore=shutil.ignore_patterns("__pycache__"))
    (pkg / "helper.py").write_text("import socket\n")
    r = run(pkg)
    assert not r["ok"] and r["stage"] == "scan" and r["checks"] is None


def test_pack_excludes_tests_and_caches(tmp_path):
    import tarfile
    from qos.cloud.agents import pack
    pkg = tmp_path / "p"
    shutil.copytree(EXAMPLES / "rsi_reversion", pkg)
    (pkg / "__pycache__").mkdir(exist_ok=True)
    (pkg / "__pycache__" / "x.pyc").write_bytes(b"x")
    info = pack(pkg, tmp_path / "a.tgz")
    names = tarfile.open(tmp_path / "a.tgz").getnames()
    assert "strategy.py" in names and not any(n.startswith(("tests", "__pycache__")) for n in names) and info["files"] == len(names)


def test_cli_agent_status(monkeypatch, capsys):
    from qos import cli
    from qos.cloud import agents as ca
    monkeypatch.setattr(ca, "mine", lambda c=None: [{"slug": "trend", "title": "Trend", "versions": [
        {"version": "1.0.0", "status": "check_failed", "review_notes": "", "evaluation": {},
         "report": {"stage": "scan", "scan": {"issues": [{"level": "error", "file": "strategy.py", "line": 3, "message": "import of 'socket' is not allowed"}]}}},
        {"version": "0.9.0", "status": "approved", "review_notes": "ok", "report": {},
         "evaluation": {"backtest": {"days": 90, "pnl_usd": 120.0, "trades": 9, "max_drawdown_pct": 1.2}, "walkforward": {"folds": 9, "positive_folds": 6}}}]}])
    assert cli.main(["agent", "status"]) == 0
    out = capsys.readouterr().out
    assert "check_failed" in out and "socket" in out and "pnl +120" in out and "6/9 folds" in out and "reviewer: ok" in out
