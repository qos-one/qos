import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from qos import install as qi
from qos.sdk._check import lookahead_diff

REAL = Path(os.environ.get("QOS_REAL_STRATEGIES") or os.path.expanduser("~/trading/strategies")) / "ema_cross@1.0.0"

EMA = '''
from qos.sdk import Param, Strategy


class EmaCross(Strategy):
    name = "ema_cross"
    label = "EMA Cross"
    version = "1.0.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"
    summary = "EMA {fast}/{slow}"
    fast = Param(10, min=2, max=200, grid=[10, 20])
    slow = Param(30, min=3, max=400, grid=[30, 50])

    def on_bar(self, bar):
        f, s = self.indicator("ema", self.p.fast), self.indicator("ema", self.p.slow)
        if self.ready():
            self.target(+1 if f.value > s.value else -1)
'''

NET = '''
import socket
from qos.sdk import Strategy


class Leaky(Strategy):
    name = "leaky"
    version = "0.1.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"

    def on_bar(self, bar):
        try:
            socket.create_connection(("example.com", 80), timeout=0.1)
        except Exception:
            pass
        self.target(+1 if bar.close > bar.open else 0)
'''

RANDOM = '''
import random
from qos.sdk import Strategy


class Dice(Strategy):
    name = "dice"
    version = "0.1.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"

    def on_bar(self, bar):
        self.target(random.choice([-1, 0, 1]))
'''


@pytest.fixture()
def pkgdir(tmp_path, monkeypatch):
    d = tmp_path / "strategies"
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(d))
    return d


def _src(tmp_path: Path, name: str, code: str) -> Path:
    d = tmp_path / "src" / name
    d.mkdir(parents=True)
    (d / "strategy.py").write_text(code)
    return d


def _install(src, api=None, **kw):
    lines = []
    out = qi.install(str(src), bars="synthetic", days=10, api=api, log=lines.append, **kw)
    return out, lines


def test_example_package_passes_every_gate(tmp_path, pkgdir):
    src = REAL if (REAL / "strategy.py").exists() else _src(tmp_path, "ema", EMA)
    out, lines = _install(src, api="http://127.0.0.1:9")
    assert out["ref"] == "ema_cross@1.0.0"
    assert [c["name"] for c in out["checks"]] == ["import", "manifest", "network", "deterministic", "lookahead"]
    assert all(c["ok"] for c in out["checks"])
    dest = pkgdir / "ema_cross@1.0.0"
    assert (dest / "strategy.py").exists() and (dest / "install.json").exists()
    assert not (dest / "manifest.json").exists() and not (dest / "__pycache__").exists()
    assert any("QOS API is not running" in ln for ln in lines)
    with pytest.raises(qi.InstallError, match="already installed"):
        _install(src)
    out2, _ = _install(src, force=True)
    assert out2["ref"] == "ema_cross@1.0.0"


def test_single_file_and_git_sources(tmp_path, pkgdir):
    src = _src(tmp_path, "ema", EMA)
    out, _ = _install(src / "strategy.py")
    assert out["ref"] == "ema_cross@1.0.0"
    if not shutil.which("git"):
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "strategy.py").write_text(EMA.replace('"1.0.0"', '"1.1.0"'))
    for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(cmd, cwd=repo, check=True)
    out, _ = _install(f"git+file://{repo}#pkg")
    assert out["ref"] == "ema_cross@1.1.0" and (pkgdir / "ema_cross@1.1.0" / "strategy.py").exists()


def test_socket_in_on_bar_is_rejected(tmp_path, pkgdir):
    with pytest.raises(qi.InstallError) as e:
        _install(_src(tmp_path, "leaky", NET))
    assert e.value.result["failed"] == "network"
    assert "create_connection" in e.value.result["checks"][-1]["detail"]
    assert not (pkgdir / "leaky@0.1.0").exists()


def test_non_deterministic_package_is_rejected(tmp_path, pkgdir):
    with pytest.raises(qi.InstallError) as e:
        _install(_src(tmp_path, "dice", RANDOM))
    assert e.value.result["failed"] == "deterministic"
    assert [c["ok"] for c in e.value.result["checks"]] == [True, True, True, False]
    assert not (pkgdir / "dice@0.1.0").exists()


def test_bad_manifest_and_missing_file(tmp_path, pkgdir):
    with pytest.raises(qi.InstallError, match="no strategy.py"):
        _install(tmp_path)
    with pytest.raises(qi.InstallError) as e:
        _install(_src(tmp_path, "bad", EMA.replace('version = "1.0.0"', 'version = "one"')))
    assert e.value.result["failed"] == "manifest"


def test_lookahead_diff():
    full = [(1, 1), (5, -1), (9, 1)]
    assert lookahead_diff(full, [(1, 1), (5, -1)], 6) == []
    assert lookahead_diff(full, [(1, 1), (4, -1)], 6)
    assert lookahead_diff(full, [(1, 1)], 6)


# ---------- status / API ----------
@pytest.fixture()
def status_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QOS_STATUS_FILE", str(tmp_path / "strategy_status.json"))
    from api import strategy_status as sst
    return sst


BT_OK = {"ok": True, "pnl_usd": 120.0, "max_drawdown_pct": 1.2, "dd_cap_pct": 10}
WF_OK = {"ok": True, "positive_folds": 6, "folds": 9, "pnl_usd": 300.0}


def test_status_moves_forward_and_back(status_file):
    sst = status_file
    sst.ensure("demo@1.0.0")
    assert sst.state_of("demo@1.0.0") == "installed"
    sst.set_result("demo@1.0.0", BT_OK, {"ok": False, "positive_folds": 3, "folds": 9})
    assert sst.state_of("demo@1.0.0") == "backtested"
    sst.set_result("demo@1.0.0", BT_OK, WF_OK)
    assert sst.state_of("demo@1.0.0") == "wf_passed"
    doc = sst.refresh({"demo@1.0.0": {"soak_go": True, "live": False}})
    assert doc["demo@1.0.0"]["state"] == "soak_go"
    sst.refresh({"demo@1.0.0": {"soak_go": True, "live": True}})
    assert sst.state_of("demo@1.0.0") == "live"
    sst.refresh({"demo@1.0.0": {"soak_go": False, "live": False}})
    assert sst.state_of("demo@1.0.0") == "wf_passed"
    sst.set_result("demo@1.0.0", {**BT_OK, "ok": False, "pnl_usd": -50.0}, WF_OK)
    doc = sst.refresh({"demo@1.0.0": {"soak_go": True, "live": False}})
    assert doc["demo@1.0.0"]["state"] == "installed" and "net -50" in doc["demo@1.0.0"]["note"]
    hist = [h["state"] for h in doc["demo@1.0.0"]["history"]]
    assert hist == ["installed", "backtested", "wf_passed", "soak_go", "live", "wf_passed", "installed"]
    sst.refresh({"ema_cross@builtin": {"builtin": True, "soak_go": False}})
    assert sst.state_of("ema_cross@builtin") == "wf_passed"


def _server(monkeypatch, sst, ref="demo@1.0.0"):
    from api import server
    monkeypatch.setattr(server, "sst", sst)
    monkeypatch.setattr(server.nm, "nodes", {"demo_eur": {"id": "demo_eur", "config_file": "demo.json"}})
    monkeypatch.setattr(server.nm, "save", lambda: None)
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_node_ref", lambda nid: ref)
    monkeypatch.setattr(server, "require_live_ok", lambda code, target: None)      # step 14: the live code is tested separately in test_live_ramp
    monkeypatch.setattr(server, "_live_readiness_global", lambda: [])
    return server


def test_allow_live_refused_until_soak_go(status_file, monkeypatch):
    from fastapi import HTTPException
    sst = status_file
    server = _server(monkeypatch, sst)
    facts = {"demo@1.0.0": {"soak_go": False, "live": False}}
    monkeypatch.setattr(server, "_package_facts", lambda using=None: facts)
    sst.ensure("demo@1.0.0")
    sst.set_result("demo@1.0.0", BT_OK, WF_OK)
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.nodes_allow_live("demo_eur", server.AllowLive(enabled=True)))
    assert e.value.status_code == 409 and "wf_passed" in e.value.detail
    assert not server.nm.nodes["demo_eur"].get("allow_live")
    facts["demo@1.0.0"]["soak_go"] = True
    r = asyncio.run(server.nodes_allow_live("demo_eur", server.AllowLive(enabled=True)))
    assert r["allow_live"] is True and server.nm.nodes["demo_eur"]["allow_live"] is True
    r = asyncio.run(server.nodes_allow_live("demo_eur", server.AllowLive(enabled=False)))
    assert r["allow_live"] is False


def test_live_gateway_needs_node_allow_live(status_file, monkeypatch):
    sst = status_file
    server = _server(monkeypatch, sst)
    monkeypatch.setattr(server, "load_risk", lambda account=None: {"allow_live": True})
    monkeypatch.setattr(server.nb, "resolve_gateway", lambda: {"mode": "paper"})
    assert server._node_can_start("demo_eur") == (True, "")
    monkeypatch.setattr(server.nb, "resolve_gateway", lambda: {"mode": "live"})
    ok, why = server._node_can_start("demo_eur")
    assert not ok and "paper-only" in why
    server.nm.nodes["demo_eur"]["allow_live"] = True
    sst.ensure("demo@1.0.0")
    ok, why = server._node_can_start("demo_eur")
    assert not ok and "soak_go" in why
    sst.set_result("demo@1.0.0", BT_OK, WF_OK)
    sst.refresh({"demo@1.0.0": {"soak_go": True}})
    assert server._node_can_start("demo_eur") == (True, "")


def test_uninstall_refused_while_a_node_uses_it(tmp_path, status_file, monkeypatch):
    from fastapi import HTTPException
    sst = status_file
    server = _server(monkeypatch, sst)
    d = tmp_path / "pkgs" / "demo@1.0.0"
    d.mkdir(parents=True)

    class E:
        ref, kind, path, status, error = "demo@1.0.0", "package", d, "ok", ""
    monkeypatch.setattr(server, "_package_entry", lambda ref: E)
    monkeypatch.setattr(server, "_package_usage", lambda: {"demo@1.0.0": ["demo_eur"]})
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.strategies_packages_delete("demo@1.0.0"))
    assert e.value.status_code == 409 and "demo_eur" in e.value.detail and d.exists()
    monkeypatch.setattr(server, "_package_usage", lambda: {})
    sst.ensure("demo@1.0.0")
    assert asyncio.run(server.strategies_packages_delete("demo@1.0.0"))["ok"] and not d.exists()
    assert sst.state_of("demo@1.0.0") is None


def test_force_refused_when_node_uses_version(tmp_path, pkgdir, monkeypatch):
    src = _src(tmp_path, "ema", EMA)
    _install(src)
    monkeypatch.setenv("QOS_USED_REFS", '{"ema_cross@1.0.0": ["ema_eurusd"]}')
    with pytest.raises(qi.InstallError, match="ema_eurusd"):
        _install(src, force=True)


def test_partial_result_keeps_other_part(status_file):
    sst = status_file
    sst.ensure("demo@1.0.0")
    sst.set_result("demo@1.0.0", BT_OK, WF_OK)
    sst.set_result("demo@1.0.0", walkforward={"ok": False, "positive_folds": 2, "folds": 9})
    doc = sst.load()["demo@1.0.0"]
    assert doc["backtest"] == BT_OK and doc["state"] == "backtested"
    sst.reset("demo@1.0.0", source="~/dev/demo")
    doc = sst.load()["demo@1.0.0"]
    assert doc["state"] == "installed" and doc["backtest"] is None and doc["history"][-1]["why"] == "reinstalled"


def test_param_overrides_from_manifest(tmp_path, monkeypatch):
    import json as _json
    from api import node_manager as nmod
    monkeypatch.setattr(nmod, "CONFIG_DIR", tmp_path)
    (tmp_path / "pkg.json").write_text(_json.dumps({"strategy": "ema_cross", "instrument": "USD/JPY.IDEALPRO", "tf_minutes": 5}))
    casts = nmod.param_casts("pkg.json")
    assert casts and all(k in casts for k in ("fast", "slow"))
    ov = nmod.clean_overrides({"fast": "30", "nope": 1, "allow_short": "false", "trade_size": 30000}, casts)
    assert ov == {"fast": 30, "allow_short": False, "trade_size": "30000"}


def test_install_done_registers_and_queues_evaluation(status_file, monkeypatch):
    sst = status_file
    server = _server(monkeypatch, sst)
    monkeypatch.setattr(server, "_eval_queue", [])
    job = {"result": {"ok": True, "ref": "demo@1.0.0"}, "source": "~/dev/demo", "force": False, "status": "done"}
    server._install_done(job)
    assert job["package"] == "demo@1.0.0" and server._eval_queue == ["demo@1.0.0"]
    st = sst.load()["demo@1.0.0"]
    assert st["state"] == "installed" and st["evaluating"] and st["source"] == "~/dev/demo"
    server._install_done({"result": None})
    assert server._eval_queue == ["demo@1.0.0"]


def test_deploy_package_creates_base_config_and_node_overrides(tmp_path, status_file, monkeypatch):
    import json as _json
    sst = status_file
    server = _server(monkeypatch, sst)
    monkeypatch.setattr(server.lab_mod, "CONFIG_DIR", tmp_path)
    from api import node_manager as nmod
    monkeypatch.setattr(nmod, "CONFIG_DIR", tmp_path)
    created = {}

    def fake_create(name, file, overrides, auto, nid):
        ov = nmod.clean_overrides(overrides, nmod.param_casts(file))
        created.update(name=name, file=file, overrides=ov)
        return {"id": "ema_usdjpy2", "overrides": ov}
    monkeypatch.setattr(server.nm, "create", fake_create)

    class E:
        ref, name, version, kind, status, error = "ema_cross@builtin", "ema_cross", "builtin", "builtin", "ok", ""
        manifest = {"params": []}
    monkeypatch.setattr(server, "_package_entry", lambda ref: E)
    r = asyncio.run(server.strategies_packages_deploy("ema_cross@builtin", server.PackageDeploy(
        name="ema_usdjpy2", instrument="USD/JPY.IDEALPRO", overrides={"fast": 25, "trade_size": "30000"})))
    assert r["config_file"] == "pkg_ema_cross_builtin_usdjpy_5m.json"
    cfg = _json.loads((tmp_path / r["config_file"]).read_text())
    assert cfg["strategy"] == "ema_cross@builtin" and cfg["strategy_id"].startswith("QXEmaCross-")
    assert created["overrides"] == {"fast": 25, "trade_size": "30000"}
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(server.strategies_packages_deploy("ema_cross@builtin", server.PackageDeploy(instrument="BTC/USD.PAXOS")))
