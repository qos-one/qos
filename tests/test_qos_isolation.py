import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from qx_strategies import backtest as bt, load_config
from qx_strategies.package_limits import DEFAULTS, LIMITS, PackageLimits, resolve
from test_qx_strategies import CFG, _sine_bars

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(os.environ.get("QOS_CATALOG") or ROOT / "data" / "catalog")
H = 3600 * 1_000_000_000
EUR = "EUR/USD.IDEALPRO"


@pytest.fixture(autouse=True)
def _clean_limits():
    LIMITS.reset()
    yield
    LIMITS.reset()


# ---------- Layer 1: in-node caps ----------
def test_each_limit_is_enforced():
    L = PackageLimits()
    L.configure({"instruments": [EUR], "max_notional_usd": 50_000, "max_orders_per_hour": 3, "max_flips_per_hour": 1})
    ok, why, kind = L.check("GBP/USD.IDEALPRO", 1, 20_000, 1.3, 0, 0)
    assert not ok and kind == "instruments"
    ok, _, kind = L.check(EUR, 1, 60_000, 1.1, 0, 1)
    assert not ok and kind == "max_notional_usd"
    L = PackageLimits()
    L.configure({"instruments": [EUR], "max_notional_usd": 50_000, "max_orders_per_hour": 3, "max_flips_per_hour": 1})
    assert L.check(EUR, 1, 20_000, 1.1, 20_000, 0)[0]                               # increase to 44k USD passes
    assert L.check(EUR, -1, 20_000, 1.1, 40_000, 1)[0]                              # one flip passes (new exposure = 22k)
    ok, _, kind = L.check(EUR, 1, 20_000, 1.1, -20_000, 2)
    assert not ok and kind == "max_flips_per_hour"
    assert L.check(EUR, 1, 10_000, 1.1, 0, 3)[0]                                    # 3rd order in the hour
    ok, _, kind = L.check(EUR, 1, 10_000, 1.1, 0, 4)
    assert not ok and kind == "max_orders_per_hour" and not L.tripped
    ok, _, kind = L.check(EUR, 1, 10_000, 1.1, 0, 5)
    assert not ok and L.tripped                                                               # more than 3 times in 10 minutes
    ok, _, kind = L.check(EUR, 1, 10_000, 1.1, 0, 2 * H)
    assert not ok and kind == "tripped"


def test_window_and_trip_reset_after_10_minutes():
    L = PackageLimits()
    L.configure({"instruments": [EUR], "max_notional_usd": 1e9, "max_orders_per_hour": 1, "max_flips_per_hour": 10})
    assert L.check(EUR, 1, 1, 1.1, 0, 0)[0]
    assert not L.check(EUR, 1, 1, 1.1, 0, 1)[0]
    assert not L.check(EUR, 1, 1, 1.1, 0, 11 * 60 * 10**9)[0]                      # old breach is past 10 minutes → no trip yet
    assert not L.tripped
    assert L.check(EUR, 1, 1, 1.1, 0, H + 1)[0]                                     # hour has passed → can send again


def test_limits_enforced_inside_backtest_and_trip():
    LIMITS.configure({"instruments": [EUR], "max_notional_usd": 1e9, "max_orders_per_hour": 1, "max_flips_per_hour": 30})
    from qos.sdk.testing import synthetic_bars
    r = bt.run_backtest({**CFG, "tf_minutes": 1, "fast": 2, "slow": 4}, bars=synthetic_bars(EUR, 1, n=3000, period=6), log_level="OFF")
    assert LIMITS.stats["breaches"] >= 3 and LIMITS.tripped, LIMITS.stats
    LIMITS.configure({"instruments": ["GBP/USD.IDEALPRO"], "max_notional_usd": 1e9, "max_orders_per_hour": 60, "max_flips_per_hour": 30})
    r = bt.run_backtest(dict(CFG), bars=_sine_bars(600), log_level="OFF")
    assert r["closed_positions"] == 0 and LIMITS.stats["breaches"] >= 1


def test_resolve_defaults_and_manifest_values():
    d = resolve(None, EUR)
    assert d["instruments"] == [EUR] and d["max_memory_mb"] == 2048 and d["max_flips_per_hour"] == DEFAULTS["max_flips_per_hour"]
    m = resolve({"instruments": [EUR, "GBP/USD.IDEALPRO"], "limits": {"max_orders_per_hour": 5, "max_memory_mb": 1024}})
    assert m["max_orders_per_hour"] == 5 and m["max_memory_mb"] == 1024 and m["max_notional_usd"] == DEFAULTS["max_notional_usd"]
    from qos.sdk import Strategy, manifest, validate

    class Tight(Strategy):
        name, version, instruments = "tight", "1.0.0", ["EUR/USD"]
        max_orders_per_hour, max_memory_mb = 4, 100

    assert manifest(Tight)["limits"]["max_orders_per_hour"] == 4
    assert any("max_memory_mb" in e for e in validate(Tight))


NODE_CONFIGS = ["ema_cross_eurusd_1m.json", "ema_cross_eurusd_5m.json"]


@pytest.mark.parametrize("file", NODE_CONFIGS)
def test_existing_strategies_stay_under_default_limits(file):
    cfg = load_config(str(ROOT / "configs" / "strategies" / file))
    if cfg.get("model_file"):
        from qx_strategies.ml_signal import MODELS_DIR
        if not (MODELS_DIR / cfg["model_file"]).exists():
            pytest.skip(f"model {cfg['model_file']} not on this machine (trained file, not in git)")
    LIMITS.configure(resolve(None, cfg["instrument"]))
    end = pd.Timestamp.now(tz="UTC").floor("D")
    try:
        r = bt.run_backtest(cfg, start=(end - pd.Timedelta(days=30)).isoformat(), end=end.isoformat(), log_level="OFF", catalog_dir=CATALOG)
    except ValueError:
        from qos.sdk.testing import synthetic_bars
        r = bt.run_backtest(cfg, bars=synthetic_bars(cfg["instrument"], int(cfg["tf_minutes"]), n=4000), log_level="OFF")
    assert LIMITS.stats["breaches"] == 0, LIMITS.stats
    assert r["closed_positions"] > 0 or file.startswith("grid")


# ---------- Layer 2: API ----------
def _ev(t, node="n1", inst=EUR, ts=0, **data):
    return {"type": t, "node": node, "instrument": inst, "ts_ns": ts, "data": data}


def test_api_guard_checks_orders_from_node_events():
    from api.package_guard import PackageGuard
    lim = {"instruments": [EUR], "max_notional_usd": 50_000, "max_orders_per_hour": 1, "max_flips_per_hour": 0}
    g = PackageGuard(lambda node: lim if node == "n1" else None)
    assert g.on_event(_ev("order", node="old", inst="GBP/USD.IDEALPRO", event="Submitted", client_order_id="X")) == []   # pre-step-22 node
    b = g.on_event(_ev("order", inst="GBP/USD.IDEALPRO", event="Submitted", client_order_id="A", side="BUY", qty=1000, order_type="MARKET"))
    assert b[0]["limit"] == "instruments" and b[0]["client_order_id"] == "A" and not b[0]["stop"]
    g.on_event(_ev("signal", close=1.1))
    b = g.on_event(_ev("order", event="Submitted", client_order_id="B", side="BUY", qty=60_000, order_type="MARKET", ts=1))
    assert b[0]["limit"] == "max_notional_usd"
    g.on_event(_ev("fill", side="BUY", qty=20_000, price=1.1, ts=2))
    assert g.on_event(_ev("order", event="Submitted", client_order_id="C", side="SELL", qty=20_000, order_type="MARKET", ts=3)) == []   # 1 spare
    b = g.on_event(_ev("order", event="Submitted", client_order_id="D", side="SELL", qty=20_000, order_type="MARKET", ts=4))
    assert [x["limit"] for x in b] == ["max_orders_per_hour", "max_flips_per_hour"]
    assert b[0]["stop"] and b[0]["count_10m"] == 3 and "n1" in g.stopped
    assert g.on_event(_ev("order", event="Submitted", client_order_id="E", side="SELL", qty=1, reduce_only=True, order_type="MARKET", ts=5)) == []
    g.on_event(_ev("node", event="started"))
    assert "n1" not in g.stopped and not g.breaches.get("n1")


def test_repeated_breaches_stop_the_node(monkeypatch):
    from api import server
    lim = {"instruments": [EUR], "max_notional_usd": 1e9, "max_orders_per_hour": 60, "max_flips_per_hour": 30}
    stopped, controls, audits = [], [], []
    monkeypatch.setitem(server.nm.rt, "pk1", {"package_limits": lim})
    monkeypatch.setattr(server.nm, "stop", lambda nid, reason="": stopped.append((nid, reason)))
    monkeypatch.setattr(server, "audit", lambda ev, **f: audits.append(ev))

    async def ctl(cmd, **f):
        controls.append((cmd, f))
        return 1

    async def ws(_m):
        return None
    monkeypatch.setattr(server, "send_control", ctl)
    monkeypatch.setattr(server, "ws_broadcast", ws)
    monkeypatch.setattr(server.notify, "fire", lambda *a, **k: None)
    monkeypatch.setattr(server.soak_rec, "on_event", lambda *a, **k: None)
    monkeypatch.setattr(server, "_attribute_node_event", lambda ev: None)
    server.pkg_guard.reset_node("pk1")
    for i in range(3):
        asyncio.run(server._on_node_event(_ev("risk", node="pk1", ts=i * 10**9, event="package_limit", limit="max_orders_per_hour",
                                              reason="too many"), False))
    assert stopped and stopped[0][0] == "pk1" and "3× in 10 min" in stopped[0][1]
    assert audits.count("package_limit_breach") == 3 and "node_stopped_package_limits" in audits
    asyncio.run(server._on_node_event(_ev("order", node="pk1", inst="GBP/USD.IDEALPRO", ts=5 * 10**9, event="Submitted",
                                          client_order_id="O-1", side="BUY", qty=1, order_type="MARKET"), False))
    assert controls and controls[-1][0] == "cancel_order" and controls[-1][1]["client_order_id"] == "O-1"


# ---------- Files ----------
WRITER = '''
import json
import os
from qos.sdk import Strategy

TARGET = {target!r}


class Writer(Strategy):
    name = "writer"
    version = "0.1.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"

    def on_bar(self, bar):
        if "env" in self.state:
            return
        try:
            fd = os.open(TARGET, os.O_WRONLY | os.O_APPEND)
            os.close(fd)
            self.state["env"] = "opened"
        except Exception as e:
            self.state["env"] = type(e).__name__
        try:
            open(os.path.expanduser("~/qos_sandbox_probe.txt"), "w").close()
            self.state["home"] = "written"
        except Exception as e:
            self.state["home"] = type(e).__name__
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "result.json"), "w") as f:
            json.dump(self.state, f)
'''


@pytest.fixture()
def pkgdir(tmp_path, monkeypatch):
    from qx_strategies.registry import REG, Registry
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(tmp_path))
    reg = Registry()
    for m in REG.builtins.values():
        reg.add_builtin(m.module)
    monkeypatch.setattr("qx_strategies.registry.REG", reg)
    monkeypatch.setattr("qx_strategies.REG", reg)
    return tmp_path


def test_strategy_cannot_write_bot_env_from_on_bar(pkgdir):
    d = pkgdir / "writer@0.1.0"
    d.mkdir()
    (d / "strategy.py").write_text(WRITER.format(target=str(ROOT / ".env")))
    cfg = {"strategy": "writer@0.1.0", "instrument": EUR, "tf_minutes": 5, "trade_size": "20000"}
    bt.run_backtest(cfg, bars=_sine_bars(50), log_level="OFF")
    res = json.loads((d / "result.json").read_text())
    assert res == {"env": "PermissionError", "home": "PermissionError"}
    assert not Path(os.path.expanduser("~/qos_sandbox_probe.txt")).exists()


def test_runner_level_guard(tmp_path):
    from qos.sdk import sandbox
    own = tmp_path / "pkg"
    own.mkdir()
    try:
        sandbox.install_global([own, tmp_path / "strategy_state"])
        with pytest.raises(PermissionError):
            open(ROOT / ".env", "a")
        with pytest.raises(PermissionError):
            os.open(str(ROOT / ".env"), os.O_WRONLY | os.O_APPEND)
        with pytest.raises(PermissionError):
            os.replace(own / "x", ROOT / "x")
        (own / "ok.txt").write_text("fine")
        (tmp_path / "strategy_state").mkdir()
        (tmp_path / "strategy_state" / "n.json").write_text("{}")
        assert (ROOT / "pyproject.toml").read_text()                                # readable as usual
    finally:
        sandbox.uninstall()
    open(tmp_path / "after.txt", "w").close()


# ---------- node: caps / resources / venv ----------
def test_spawn_passes_limits_and_resources(tmp_path, monkeypatch):
    from api import nautilus_bridge as nb
    from api import node_manager as nmod
    from test_node_manager import CFG as NCFG, sleeper, wait
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(tmp_path / "no_packages"))          # built-in strategy (instruments = from config)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ema.json").write_text(json.dumps(NCFG))
    monkeypatch.setattr(nmod, "NODES_FILE", tmp_path / "nodes.json")
    monkeypatch.setattr(nmod, "PID_DIR", tmp_path / "pids")
    monkeypatch.setattr(nmod, "CONFIG_DIR", tmp_path / "configs")
    monkeypatch.setattr(nb, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(nb, "CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(nb, "resolve_gateway", lambda: {"host": "127.0.0.1", "port": 4004, "gateway_id": "p", "mode": "paper", "account_id": "DU1"})
    seen = {}

    def launch(nid, cfg, log):
        seen.update(cfg)
        return sleeper()(nid, cfg, log)
    m = nmod.NodeManager(launcher=launch)
    m.create("lim", "ema.json")
    st = m.start("lim")
    assert seen["package_limits"]["instruments"] == [EUR] and seen["package_limits"]["max_memory_mb"] == 2048
    assert st["limits_active"] is True and st["package_limits"]["max_orders_per_hour"] == DEFAULTS["max_orders_per_hour"]
    assert json.loads((tmp_path / "pids" / "lim.pid").read_text())["package_limits"]["instruments"] == [EUR]
    m.tick()
    h = m.health("lim")
    assert h["rss_mb"] is not None and h["max_memory_mb"] == 2048
    monkeypatch.setattr(nmod, "proc_stats", lambda pid: {"mem_mb": 4096.0, "rss_mb": 900.0, "cpu_pct": 1.0, "nice": 5})
    m.tick()
    assert not m._running("lim") and "max_memory_mb" in m.status("lim")["reason"]


def test_limit_resources_sets_nice_and_rlimit():
    import resource
    import sys
    from api import node_manager as nmod
    code = ("import os, resource, sys; sys.path.insert(0, %r); from api.node_manager import _limit_resources; "
            "_limit_resources(1024, 7)(); print(os.nice(0), resource.getrlimit(resource.RLIMIT_AS)[0])") % str(ROOT)
    env = {k: v for k, v in os.environ.items() if k != "QOS_FORCE_RLIMIT_AS"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), env=env).stdout.split()
    assert int(out[0]) >= 7
    assert int(out[1]) == resource.getrlimit(resource.RLIMIT_AS)[0]      # no virtual-memory cap by default (it broke node threads on Linux)
    if sys.platform != "darwin":
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
                             env={**env, "QOS_FORCE_RLIMIT_AS": "1"}).stdout.split()
        assert int(out[1]) == 1024 * 1024 * 1024
    assert nmod.launch_cmd({"package_dir": None})[1:3] == ["run", "nautilus_runner.py"]


def _wheel(dirp: Path) -> Path:
    name, ver = "qos_testdep", "0.1.0"
    files = {f"{name}/__init__.py": "VALUE = 42\n",
             f"{name}-{ver}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: qos-testdep\nVersion: {ver}\n",
             f"{name}-{ver}.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: qos-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    rec = []
    for k, v in files.items():
        dig = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        rec.append(f"{k},sha256={dig},{len(v.encode())}")
    rec.append(f"{name}-{ver}.dist-info/RECORD,,")
    files[f"{name}-{ver}.dist-info/RECORD"] = "\n".join(rec) + "\n"
    p = dirp / f"{name}-{ver}-py3-none-any.whl"
    with zipfile.ZipFile(p, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return p


DEP = '''
import qos_testdep
from qos.sdk import Strategy


class DepUser(Strategy):
    name = "dep_user"
    version = "0.1.0"
    instruments = ["EUR/USD"]
    timeframe = "5m"
    requires = [{whl!r}]

    def on_bar(self, bar):
        self.target(+1 if (bar.close > bar.open) == (qos_testdep.VALUE == 42) else -1)
'''


def test_requires_builds_separate_venv(tmp_path, pkgdir):
    import shutil as _sh
    if not (_sh.which("uv") or Path(os.path.expanduser("~/.local/bin/uv")).exists()):
        pytest.skip("uv not installed")
    from qos import install as qi
    from qx_strategies.registry import Registry, venv_python
    from api.node_manager import launch_cmd
    whl = _wheel(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "strategy.py").write_text(DEP.format(whl=str(whl)))
    out = qi.install(str(src), bars="synthetic", days=10, api=None, log=lambda *_: None)
    dest = pkgdir / "dep_user@0.1.0"
    assert out["ref"] == "dep_user@0.1.0" and all(c["ok"] for c in out["checks"])
    py = venv_python(dest)
    assert py and Path(py).exists()
    r = subprocess.run([py, "-c", "import qos_testdep, nautilus_trader; print(qos_testdep.VALUE)"], capture_output=True, text=True)
    assert r.stdout.strip() == "42", r.stderr[-400:]
    assert importlib.util.find_spec("qos_testdep") is None                          # not in the main env
    reg = Registry()
    reg.scan(force=True)
    assert reg.packages["dep_user@0.1.0"].status == "ok"                            # inspected with the venv's python
    assert launch_cmd({"package_dir": str(dest)})[0] == py
    (src / "strategy.py").write_text(DEP.format(whl=str(whl)).replace("qos_testdep.VALUE == 42", "qos_testdep.VALUE == 41"))
    with pytest.raises(qi.InstallError, match="already installed"):
        qi.install(str(src), bars="synthetic", days=10, api=None, log=lambda *_: None)


def test_proc_stats_sums_the_process_group():
    import sys
    import time
    from api.node_manager import proc_stats
    code = "import subprocess, sys, time; c = subprocess.Popen([sys.executable, '-c', 'x = bytearray(80 * 1024 * 1024); import time; time.sleep(5)']); time.sleep(5)"
    p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        time.sleep(2.0)
        st = proc_stats(p.pid)
        assert st["processes"] >= 2 and st["rss_mb"] > 80 and st["mem_mb"] > 80, st
    finally:
        os.killpg(p.pid, 9)
