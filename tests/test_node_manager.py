import json
import subprocess
import sys
import time

import pytest

from api import nautilus_bridge as nb
from api import node_manager as nmod

CFG = {"strategy": "ema_cross", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5, "fast": 10, "slow": 30, "trade_size": "20000"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ema.json").write_text(json.dumps(CFG))
    monkeypatch.setattr(nmod, "NODES_FILE", tmp_path / "nodes.json")
    monkeypatch.setattr(nmod, "PID_DIR", tmp_path / "pids")
    monkeypatch.setattr(nmod, "CONFIG_DIR", tmp_path / "configs")
    monkeypatch.setattr(nb, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(nb, "CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(nb, "resolve_gateway", lambda: {"host": "127.0.0.1", "port": 4004, "gateway_id": "paper1", "mode": "paper", "account_id": "DU1"})
    return tmp_path


def sleeper(seconds=30):
    """Stand-in for nautilus_runner: command line has nautilus_runner.py + agent_id like the real one (used to test adopt)"""
    def launch(nid, cfg, log):
        f = open(log, "a")
        return subprocess.Popen([sys.executable, "-c", f"import time; print('ExecEngine: RUNNING', flush=True); time.sleep({seconds})",
                                 "nautilus_runner.py", json.dumps(cfg)], stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    return launch


def wait(pred, t=5.0):
    t0 = time.time()
    while time.time() - t0 < t:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_create_validates(env):
    m = nmod.NodeManager(launcher=sleeper())
    with pytest.raises(ValueError):
        m.create("bad", "ema.json", {"fast": 40, "slow": 30})
    with pytest.raises(ValueError):
        m.create("x", "nope.json")
    st = m.create("EMA EURUSD 5m", "ema.json", {"fast": "8", "junk": 1})
    assert st["id"] == "ema_eurusd_5m" and st["overrides"] == {"fast": 8} and st["state"] == "stopped"
    assert st["config"]["fast"] == 8 and st["config"]["slow"] == 30
    with pytest.raises(ValueError):
        m.create("EMA EURUSD 5m", "ema.json")


def test_start_stop_and_client_ids(env):
    m = nmod.NodeManager(launcher=sleeper())
    m.create("na", "ema.json"), m.create("nb", "ema.json")
    sa, sb = m.start("na"), m.start("nb")
    assert sa["running"] and sb["running"]
    assert {sa["client_id"], sa["client_id"] + 1}.isdisjoint({sb["client_id"], sb["client_id"] + 1})
    assert wait(lambda: m.status("na")["state"] == "running")
    assert m.stop("na")["state"] == "stopped" and not m._running("na")
    with pytest.raises(ValueError):
        m.delete("nb")
    m.stop("nb")
    m.delete("nb")
    assert "nb" not in nb._load_clients()


def test_start_blocked_when_halted(env):
    m = nmod.NodeManager(launcher=sleeper(), can_start=lambda: (False, "trading is halted"))
    m.create("na", "ema.json")
    with pytest.raises(RuntimeError, match="halted"):
        m.start("na")


def test_supervisor_backoff_then_crash(env):
    events = []
    m = nmod.NodeManager(launcher=sleeper(0), on_event=lambda e, **k: events.append(e))
    m.create("na", "ema.json")
    m.start("na")
    assert wait(lambda: not m._running("na"))
    now = time.time()
    m.tick(now)                                   # detects the stop → schedules a restart in 5s
    assert m.status("na")["state"] == "backoff" and "restarting in 5s" in m.rt["na"]["reason"]
    assert m.tick(now + 1) == []                  # not due yet
    assert m.tick(now + 6) == ["na"]
    for k in range(4):                            # keep crashing until over the threshold
        assert wait(lambda: not m._running("na"))
        now = time.time()
        m.tick(now)
        m.tick(now + nmod.BACKOFF_MAX_S + 1)
    assert wait(lambda: not m._running("na"))
    m.tick(time.time())
    assert m.status("na")["state"] == "crashed" and "node_crashed" in events
    m.start("na")                                  # user presses Start = clears the crashed state
    assert m.rt["na"]["gave_up"] is False
    m.stop("na")


def test_no_restart_when_auto_restart_off(env):
    m = nmod.NodeManager(launcher=sleeper(0))
    m.create("na", "ema.json", auto_restart=False)
    m.start("na")
    assert wait(lambda: not m._running("na"))
    assert m.tick(time.time()) == [] and m.tick(time.time() + 999) == []


def test_adopt_after_api_restart(env):
    m1 = nmod.NodeManager(launcher=sleeper())
    m1.create("na", "ema.json")
    pid = m1.start("na")["pid"]
    m2 = nmod.NodeManager(launcher=sleeper())     # new API process
    assert m2.adopt() == ["na"]
    st = m2.status("na")
    assert st["running"] and st["pid"] == pid
    m2.stop("na")
    assert wait(lambda: not nmod._pid_alive(pid) or "nautilus_runner" not in nmod._cmdline(pid))


def test_resume_desired_without_process(env):
    m1 = nmod.NodeManager(launcher=sleeper())
    m1.create("na", "ema.json")
    m1.start("na")
    m1._kill("na")                                 # simulate a reboot: process gone but desired is still True
    m2 = nmod.NodeManager(launcher=sleeper())
    assert m2.adopt() == []
    ok, failed = m2.resume()
    assert ok == ["na"] and not failed and m2._running("na")
    m2.stop("na")


def test_log_and_level_filter(env):
    m = nmod.NodeManager(launcher=sleeper())
    m.create("na", "ema.json")
    nb.log_path("na").write_text("\x1b[1m2026-10-26T10:00:00Z\x1b[0m [INFO] x\n2026-10-26T10:00:01Z [ERROR] boom\n2026-10-26T10:00:02Z [WARN] w\n")
    assert len(m.log("na")) == 3 and "\x1b" not in m.log("na")[0]
    assert m.log("na", level="error") == ["2026-10-26T10:00:01Z [ERROR] boom"]
    assert len(m.log("na", level="warn")) == 2
    h = m.health("na")
    assert h["errors"] == 1 and "boom" in h["last_error"]


def test_spawn_pins_strategy_version(env, monkeypatch):
    """a node remembers the name@version used at start (step 19), and old processes without a ref count as @builtin"""
    from qx_strategies.registry import Registry, REG as real
    reg = Registry()
    for b in real.builtins.values():
        reg.add_builtin(b.module)
    monkeypatch.setattr("qx_strategies.registry.REG", reg)
    monkeypatch.setattr("qx_strategies.REG", reg)
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(env / "none"))
    m = nmod.NodeManager(launcher=sleeper())
    m.create("EMA", "ema.json", {}, True, "ema_t")
    m.start("ema_t")
    assert m.rt["ema_t"]["strategy_ref"] == "ema_cross@builtin"
    assert m.status("ema_t")["strategy_ref"] == "ema_cross@builtin"
    assert json.loads((nmod.PID_DIR / "ema_t.pid").read_text())["strategy_ref"] == "ema_cross@builtin"
    m.stop("ema_t")
    assert m.status("ema_t")["strategy_ref"] is None and m.status("ema_t")["strategy_latest"] == "ema_cross@builtin"
