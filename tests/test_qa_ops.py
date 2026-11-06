"""QA audit (ops / nodes) — regression tests: each test asserts the corrected behaviour of one finding"""
import asyncio
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from api import gateways as gw
from api import nautilus_bridge as nb
from api import node_manager as nmod

ROOT = Path(__file__).resolve().parent.parent
CFG = {"strategy": "ema_cross", "instrument": "EUR/USD.IDEALPRO", "tf_minutes": 5, "fast": 10, "slow": 30, "trade_size": "20000"}
_groups: list[int] = []


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "ema.json").write_text(json.dumps(CFG))
    monkeypatch.setattr(nmod, "NODES_FILE", tmp_path / "nodes.json")
    monkeypatch.setattr(nmod, "PID_DIR", tmp_path / "pids")
    monkeypatch.setattr(nmod, "CONFIG_DIR", tmp_path / "configs")
    monkeypatch.setattr(nb, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(nb, "CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(nb, "resolve_gateway", lambda: {"host": "127.0.0.1", "port": 4004, "gateway_id": "p", "mode": "paper", "account_id": "DU1"})
    yield tmp_path
    for pg in _groups:
        try:
            os.killpg(pg, signal.SIGKILL)
        except Exception:
            pass
    _groups.clear()


@pytest.fixture
def gw_files(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(gw, "GW_FILE", tmp_path / "gateways.json")
    monkeypatch.setattr(gw, "IBGW_ROOT", tmp_path / "ibgw")
    monkeypatch.delenv("IB_PORT", raising=False)
    return tmp_path


def sleeper(seconds=60):
    def launch(nid, cfg, log):
        p = subprocess.Popen([sys.executable, "-c", f"import time; print('ExecEngine: RUNNING', flush=True); time.sleep({seconds})",
                              "nautilus_runner.py", json.dumps(cfg)], stdout=open(log, "a"), stderr=subprocess.STDOUT, start_new_session=True)
        _groups.append(p.pid)
        return p
    return launch


class DeadPopen:
    n = 800000

    def __init__(self):
        DeadPopen.n += 1
        self.pid, self.returncode = DeadPopen.n, 1

    def poll(self):
        return 1


def wait(pred, t=10.0):
    t0 = time.time()
    while time.time() - t0 < t:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ---------- node manager ----------
def test_stop_during_respawn_wins(env):
    entered, go = threading.Event(), threading.Event()
    real = sleeper()

    def slow(nid, cfg, log):
        entered.set()
        go.wait(5)
        return real(nid, cfg, log)
    m = nmod.NodeManager(launcher=slow)
    m.create("n1", "ema.json")
    m.nodes["n1"]["desired"] = True
    m.rt["n1"]["next_try"] = 1.0
    t = threading.Thread(target=m.tick, kwargs={"now": time.time()})
    t.start()
    assert entered.wait(5)
    ts = threading.Thread(target=m.stop, args=("n1",))
    ts.start()
    time.sleep(0.2)
    go.set()
    t.join(10)
    ts.join(20)
    assert not m._running("n1") and m.nodes["n1"]["desired"] is False


def test_supervisor_stops_running_but_undesired_node(env):
    events = []
    m = nmod.NodeManager(launcher=sleeper(), on_event=lambda e, **k: events.append(e))
    m.create("n2", "ema.json")
    m.start("n2")
    m.nodes["n2"]["desired"] = False             # e.g. adopted after an API crash between save and kill
    m.tick()
    assert not m._running("n2") and "node_stopped" in events


def test_orphaned_child_tracked_by_process_group(env):
    def wrapper(nid, cfg, log):
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); sys.exit(1)"
        p = subprocess.Popen([sys.executable, "-c", code, "nautilus_runner.py"], start_new_session=True)
        _groups.append(p.pid)
        return p
    m = nmod.NodeManager(launcher=wrapper)
    m.create("n3", "ema.json")
    m.start("n3")
    pg = m.rt["n3"]["pgid"]
    assert pg == m.rt["n3"]["pid"]
    assert wait(lambda: m.rt["n3"]["popen"] is None or m.rt["n3"]["popen"].poll() is not None)
    assert m._running("n3")                       # the child still runs → no second node is started
    now = time.time()
    assert m.tick(now) == [] and m.tick(now + 10) == []
    m.stop("n3")
    assert wait(lambda: not nmod._group_alive(pg))


def test_own_group_never_returns_the_api_group():
    assert nmod._own_group(os.getpid()) is None or os.getpgrp() != os.getpid()


def test_blocked_restart_gives_up_and_alerts(env):
    ok = {"v": True}
    events = []
    m = nmod.NodeManager(can_start=lambda: (ok["v"], "licence expired"), launcher=lambda *a: DeadPopen(),
                         on_event=lambda e, **k: events.append(e))
    m.create("n4", "ema.json")
    m.start("n4")
    ok["v"] = False
    t = 1_000_000.0
    for _ in range(300):
        t += 30
        m.tick(t)
    rt = m.rt["n4"]
    assert rt["gave_up"] and events.count("node_crashed") == 1 and "restart blocked" in rt["reason"]
    assert len(rt["restarts"]) <= nmod.RESTARTS_KEEP


def test_crash_backoff_unchanged(env):
    m = nmod.NodeManager(launcher=lambda *a: DeadPopen())
    m.create("n5", "ema.json")
    m.start("n5")
    t, delays = 2_000_000.0, []
    for _ in range(40):
        t += 1
        m.tick(t)
        rt = m.rt["n5"]
        if rt["gave_up"]:
            break
        if rt["next_try"] and (not delays or delays[-1][0] != rt["next_try"]):
            delays.append((rt["next_try"], round(rt["next_try"] - t)))
            t = rt["next_try"] - 1
    assert [d for _, d in delays] == [5, 10, 20, 40, 80] and m.rt["n5"]["gave_up"]


def test_restart_not_seen_as_crash(env):
    events = []
    m = nmod.NodeManager(launcher=sleeper(), on_event=lambda e, **k: events.append(e))
    m.create("n6", "ema.json")
    m.start("n6")
    t = threading.Thread(target=m.restart, args=("n6",))
    t.start()
    time.sleep(0.05)
    m.tick()
    t.join(20)
    assert "node_exited" not in events and m._running("n6")
    m.stop("n6")


def test_licence_check_stops_running_node(env):
    m = nmod.NodeManager(launcher=sleeper())
    m.create("n7", "ema.json")
    m.start("n7")
    m.licence_check = lambda nid: (False, "licence expired")
    m.tick()
    assert not m._running("n7") and m.nodes["n7"]["desired"] is False and "licence" in m.rt["n7"]["reason"]


def test_spawn_refuses_gateway_error(env, monkeypatch):
    monkeypatch.setattr(nb, "resolve_gateway", lambda: {"host": "h", "port": 1, "gateway_id": "", "mode": "paper", "account_id": "DU1",
                                                        "error": "the selected gateway profile 'x' no longer exists"})
    m = nmod.NodeManager(launcher=sleeper())
    m.create("n8", "ema.json")
    with pytest.raises(RuntimeError, match="no longer exists"):
        m.start("n8")


def test_overrides_no_silent_truncation():
    with pytest.raises(ValueError):
        nmod.clean_overrides({"fast": 2.9})
    assert nmod.clean_overrides({"fast": 8.0, "slow": "30"}) == {"fast": 8, "slow": 30}


# ---------- notifications from worker threads ----------
def test_notify_from_worker_thread_is_delivered(monkeypatch):
    from api import notify
    sent = []

    async def fake_send(text, event="info"):
        sent.append(text)
        return True
    monkeypatch.setattr(notify, "send", fake_send)

    async def main():
        notify.set_loop(asyncio.get_running_loop())
        await asyncio.to_thread(notify.fire, "from thread", "agent")
        await asyncio.sleep(0.05)
    asyncio.run(main())
    notify.set_loop(None)
    th = threading.Thread(target=notify.fire, args=("no loop at all", "agent"))     # no API loop known → own thread
    th.start()
    th.join()
    assert wait(lambda: "no loop at all" in sent, 3) and "from thread" in sent


# ---------- nautilus_bridge ----------
def test_node_tags_unique_and_stable_for_short_ids():
    assert nb.node_tags("ema_eur_1") == ("QX-EMAEUR1", "ema_eur_1")
    a, b = nb.node_tags("eurusd_ema_cross_fast_a"), nb.node_tags("eurusd_ema_cross_fast_b")
    assert a != b and a[1] != b[1] and len(a[1]) <= 16 and len(a[0]) <= 19
    assert "node_tags(" in (ROOT / "nautilus_runner.py").read_text()


def test_client_id_allocation_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, "CLIENTS_FILE", tmp_path / "clients.json")
    real, barrier = nb._load_clients, threading.Barrier(2)

    def slow():
        d = real()
        try:
            barrier.wait(1)
        except threading.BrokenBarrierError:
            pass
        return d
    monkeypatch.setattr(nb, "_load_clients", slow)
    out = {}
    ts = [threading.Thread(target=lambda n=n: out.__setitem__(n, nb.allocate_client_id(n))) for n in ("x", "y")]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert sorted(out.values()) == [100, 102]


def test_ib_port_mismatch_fails_closed(gw_files, monkeypatch):
    gw._save({"gateways": [{"id": "u-paper", "mode": "paper", "api_port": 4010, "username": "u", "dir": "/x"},
                           {"id": "u-live", "mode": "live", "api_port": 4011, "username": "u", "dir": "/y"}]})
    nb.SESSION_FILE.write_text(json.dumps({"gateway_id": "u-paper", "account_id": "DU1"}))
    assert nb.resolve_gateway()["mode"] == "paper"
    monkeypatch.setenv("IB_PORT", "4010")
    assert nb.resolve_gateway()["mode"] == "paper"
    monkeypatch.setenv("IB_PORT", "4011")                          # another profile's port → that profile's mode
    assert nb.resolve_gateway()["mode"] == "live"
    monkeypatch.setenv("IB_PORT", "4003")                          # unknown port → live (every live gate applies)
    assert nb.resolve_gateway()["mode"] == "live"
    monkeypatch.setenv("IB_PORT", "4002")
    assert nb.resolve_gateway()["mode"] == "paper"


def test_missing_selected_gateway_is_an_error(gw_files):
    gw._save({"gateways": [{"id": "u-live", "mode": "live", "api_port": 4011, "username": "u", "dir": "/y"}]})
    nb.SESSION_FILE.write_text(json.dumps({"gateway_id": "deleted", "account_id": "DU1"}))
    assert "no longer exists" in nb.resolve_gateway()["error"]


# ---------- alerts ----------
def test_repeating_alert_cooldown_and_none_change(tmp_path, monkeypatch):
    from api import alerts
    monkeypatch.setattr(alerts, "FILE", tmp_path / "alerts.json")
    alerts.add("FX:EURUSD", "above", 1.10, repeat=True)
    q = [{"key": "FX:EURUSD", "mid": 1.105, "change_pct": None}]
    assert [len(alerts.check(q, now=100.0 + i)) for i in range(4)] == [1, 0, 0, 0]
    alerts.check([{"key": "FX:EURUSD", "mid": 1.09}], now=100.0 + alerts.REPEAT_COOLDOWN_S)
    assert len(alerts.check(q, now=101.0 + alerts.REPEAT_COOLDOWN_S)) == 1
    alerts.add("FX:USDJPY", "change_pct", 1.0)
    assert alerts.check([{"key": "FX:USDJPY", "mid": 150.0, "change_pct": None}], now=1.0) == []


# ---------- backup ----------
def _tar(files: dict[str, bytes], path: Path) -> Path:
    man = {"created": "2026-10-30T00:00:00+00:00", "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
    with tarfile.open(path, "w:gz") as tar:
        for k, v in [("manifest.json", json.dumps(man).encode())] + list(files.items()):
            ti = tarfile.TarInfo(k)
            ti.size = len(v)
            tar.addfile(ti, io.BytesIO(v))
    return path


def test_restore_rejects_paths_outside_target_and_stops_nodes(tmp_path, monkeypatch):
    from api import backup
    monkeypatch.setenv("QOS_HOME", str(tmp_path / "Q"))
    for k in ("QOS_DATA_DIR", "QOS_CONFIG_DIR", "QOS_ENV_FILE"):
        monkeypatch.delenv(k, raising=False)
    victim = tmp_path / "victim.txt"
    nodes = {"n1": {"id": "n1", "desired": True, "config_file": "a.json"}}
    r = backup.restore(_tar({f"data/{victim}": b"x", "data/../up.txt": b"x",
                             "data/nautilus_nodes.json": json.dumps(nodes).encode()}, tmp_path / "b.tar.gz"))
    assert not victim.exists() and not (tmp_path / "Q" / "up.txt").exists() and len(r["skipped"]) == 2
    assert json.loads((tmp_path / "Q" / "data" / "nautilus_nodes.json").read_text())["n1"]["desired"] is False


# ---------- watchdog ----------
def test_watchdog_reads_ib_port_from_env_file(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import watchdog
    monkeypatch.setenv("QOS_HOME", str(tmp_path / "Q"))
    monkeypatch.delenv("QOS_ENV_FILE", raising=False)
    monkeypatch.delenv("IB_PORT", raising=False)
    (tmp_path / "Q").mkdir()
    (tmp_path / "Q" / ".env").write_text("IB_PORT=4003\n")
    assert watchdog.gateway_port() == 4003


# ---------- event bus ----------
def test_event_bus_resumes_from_start_of_empty_stream(monkeypatch):
    import redis.asyncio as aioredis
    from api.event_bus import EventBus
    ids = []

    class R:
        async def ping(self):
            return True

        async def xrevrange(self, stream, count):
            return []

        async def xread(self, streams, block, count):
            ids.append(list(streams.values())[0])
            raise asyncio.CancelledError

        async def aclose(self):
            pass
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: R())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(EventBus("redis://x").run())
    assert ids == ["0-0"]


# ---------- catalog ----------
def test_gap_report_friday_outage_reported_weekend_not():
    from api import catalog
    def frame(*ts):
        return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=pd.to_datetime(list(ts), utc=True))
    g = catalog.gap_report(frame("2026-10-30 10:00", "2026-11-01 22:00"))
    assert len(g) == 1 and 600 < g[0]["open_minutes"] < 720
    assert catalog.gap_report(frame("2026-10-30 21:00", "2026-11-01 22:01")) == []   # winter close Fri 22:00 → Sun 22:00
    assert catalog.gap_report(frame("2026-07-31 21:00", "2026-08-02 21:01")) == []   # summer close Fri 21:00 → Sun 21:00


# ---------- qos install / store ----------
def test_install_nodes_using_falls_back_to_pidfiles(tmp_path, monkeypatch):
    from qos import install
    monkeypatch.setenv("QOS_HOME", str(tmp_path / "Q"))
    monkeypatch.delenv("QOS_DATA_DIR", raising=False)
    monkeypatch.delenv("QOS_USED_REFS", raising=False)
    d = tmp_path / "Q" / "data" / "nodes"
    d.mkdir(parents=True)
    (d / "n1.pid").write_text(json.dumps({"pid": os.getpid(), "strategy_ref": "demo@1.0.0"}))
    (d / "n2.pid").write_text(json.dumps({"pid": 999999, "strategy_ref": "demo@1.0.0"}))
    monkeypatch.setattr(install, "api_call", lambda *a, **k: None)              # API down
    assert install._nodes_using("demo@1.0.0", "http://127.0.0.1:1") == ["n1"]


def test_store_licence_requires_device_id(tmp_path):
    from qos.cloud import store

    class C:
        dir = tmp_path

        def _state(self):
            return {}

        def trust(self):
            raise AssertionError("must not be reached without a device id")
    assert store.licence_ok("x", C()) is False


def test_event_bridge_cancels_heartbeat_before_stopping():
    src = (ROOT / "qx_strategies" / "events.py").read_text()
    body = src[src.index("    def on_stop(self) -> None:"):src.index("    # ---------- handlers ----------")]
    assert body.index("cancel_timer") < body.index('"stopping"')


# ---------- round 2 (re-QA) ----------
def test_restore_refused_while_node_process_alive(tmp_path, monkeypatch):
    from api import backup
    monkeypatch.setenv("QOS_HOME", str(tmp_path / "Q"))
    for k in ("QOS_DATA_DIR", "QOS_CONFIG_DIR", "QOS_ENV_FILE"):
        monkeypatch.delenv(k, raising=False)
    d = tmp_path / "Q" / "data" / "nodes"
    d.mkdir(parents=True)
    arc = _tar({"data/nautilus_clients.json": b'{"n1": 140}'}, tmp_path / "b.tar.gz")
    (d / "n1.pid").write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(backup.RestoreRefused, match="n1"):
        backup.restore(arc)
    (d / "n1.pid").unlink()
    (tmp_path / "Q" / "data" / "nautilus_clients.json").write_text('{"n1": 100}')
    r = backup.restore(arc)                                       # no live node → restore, but clientIds are kept
    assert "data/nautilus_clients.json" in r["skipped"]
    assert json.loads((tmp_path / "Q" / "data" / "nautilus_clients.json").read_text()) == {"n1": 100}


def test_nodes_file_atomic_and_rebuilt_from_pidfiles(env):
    m = nmod.NodeManager(launcher=sleeper())
    m.create("na", "ema.json")
    m.start("na")
    pg = m.rt["na"]["pgid"]
    assert not list(nmod.NODES_FILE.parent.glob(".nodes.json.*.tmp"))
    nmod.NODES_FILE.write_text(nmod.NODES_FILE.read_text()[:20])     # torn file
    m2 = nmod.NodeManager(launcher=sleeper())
    assert m2.nodes["na"]["desired"] is True and m2.nodes["na"]["config_file"] == "ema.json"
    assert list(nmod.NODES_FILE.parent.glob("nodes.json.corrupt-*"))
    assert m2.adopt() == ["na"]
    m2.stop_all("kill switch")
    assert wait(lambda: not nmod._group_alive(pg))


def test_refused_restart_keeps_node_running(env):
    ok = {"v": True}
    events = []
    m = nmod.NodeManager(can_start=lambda: (ok["v"], "trading is halted"), launcher=sleeper(), on_event=lambda e, **k: events.append(e))
    m.create("nb", "ema.json")
    m.start("nb")
    ok["v"] = False
    with pytest.raises(RuntimeError, match="halted"):
        m.restart("nb")
    m.tick()
    assert m._running("nb") and "node_exited" not in events
    m.stop("nb")


def test_create_refuses_colliding_order_tags(env):
    a, b = "eurusd_ema_cross_333", "eurusd_ema_cross_1245"
    assert nb.node_tags(a) == nb.node_tags(b)
    m = nmod.NodeManager(launcher=sleeper())
    m.create(a, "ema.json", node_id=a)
    with pytest.raises(ValueError, match="same order ids"):
        m.create(b, "ema.json", node_id=b)


def test_install_pidfile_fallback_sees_orphaned_group(tmp_path, monkeypatch):
    from qos import install
    monkeypatch.setenv("QOS_HOME", str(tmp_path / "Q"))
    monkeypatch.delenv("QOS_DATA_DIR", raising=False)
    d = tmp_path / "Q" / "data" / "nodes"
    d.mkdir(parents=True)
    code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); sys.exit(0)"
    p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    _groups.append(p.pid)
    p.wait()
    (d / "orph.pid").write_text(json.dumps({"pid": p.pid, "pgid": p.pid, "strategy_ref": "pkg@1.0.0"}))
    assert install._running_nodes_from_pidfiles("pkg@1.0.0") == ["orph"]
    os.killpg(p.pid, signal.SIGKILL)


def test_stop_all_safe_against_concurrent_create(env):
    m = nmod.NodeManager(launcher=lambda *a: DeadPopen())
    for i in range(5):
        m.create(f"c{i}", "ema.json")
    errs = []

    def creator():
        try:
            for i in range(5, 40):
                m.create(f"c{i}", "ema.json")
        except Exception as e:                  # pragma: no cover
            errs.append(e)
    t = threading.Thread(target=creator)
    t.start()
    for _ in range(20):
        m.stop_all("kill switch")
    t.join(30)
    assert errs == [] and json.loads(nmod.NODES_FILE.read_text()).keys() == m.nodes.keys()


# ---------- round 3 ----------
def test_rebuild_verifies_cmdline_and_carries_ramp(env):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    _groups.append(other.pid)
    m = nmod.NodeManager(launcher=sleeper())
    m.create("live1", "ema.json")
    m.nodes["live1"].update(allow_live=True, ramp={"stage": 1, "status": "active"})
    m.start("live1")
    (nmod.PID_DIR / "stale1.pid").write_text(json.dumps({"pid": other.pid, "pgid": other.pid, "config_file": "ema.json"}))
    nmod.NODES_FILE.write_text("{")
    spawned = []
    real = sleeper()
    m2 = nmod.NodeManager(launcher=lambda nid, cfg, log: spawned.append(nid) or real(nid, cfg, log))
    assert m2.nodes["live1"]["desired"] is True and m2.nodes["live1"]["ramp"] == {"stage": 1, "status": "active"}
    assert m2.nodes["live1"]["allow_live"] is True and m2.nodes["stale1"]["desired"] is False
    m2.adopt()
    m2.resume()
    m2.tick()
    assert spawned == []
    m2.stop("live1")


def test_pidfile_removed_when_process_ends(env):
    m = nmod.NodeManager(launcher=sleeper(seconds=0.5))
    m.create("short", "ema.json")
    m.start("short")
    assert (nmod.PID_DIR / "short.pid").exists()
    assert wait(lambda: not m._running("short"))
    assert not (nmod.PID_DIR / "short.pid").exists()


def test_nodes_file_io_error_is_not_treated_as_corrupt(env, monkeypatch):
    nmod.NODES_FILE.write_text(json.dumps({"x1": {"id": "x1", "config_file": "ema.json", "desired": True}}))
    real = Path.read_text

    def boom(self, *a, **k):
        if self == nmod.NODES_FILE:
            raise PermissionError("EACCES")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(OSError):
        nmod.NodeManager()
    monkeypatch.setattr(Path, "read_text", real)
    assert json.loads(nmod.NODES_FILE.read_text())["x1"]["desired"] is True
    assert not list(nmod.NODES_FILE.parent.glob("nodes.json.corrupt-*"))


def test_save_not_blocked_by_stop_in_progress(env):
    body = "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)"

    def stubborn(nid, cfg, log):
        p = subprocess.Popen([sys.executable, "-c", body, "nautilus_runner.py"], start_new_session=True)
        _groups.append(p.pid)
        return p
    m = nmod.NodeManager(launcher=stubborn)
    m.create("slow", "ema.json")
    m.start("slow")
    time.sleep(0.8)
    th = threading.Thread(target=m.stop, args=("slow",))
    th.start()
    time.sleep(0.3)
    t0 = time.time()
    m.save()
    took = time.time() - t0
    th.join(30)
    assert took < 1.0 and not m._running("slow")


def test_lockfree_liveness_check_never_deletes_respawned_pidfile(env, monkeypatch):
    """status()/brief() call _running() without the node lock; a respawn that happens during that check must keep its
    pid file and pgid (only the process seen dead may lose its pid file)"""
    m = nmod.NodeManager(launcher=sleeper())
    m.create("rr", "ema.json")
    m.start("rr")
    old = m.rt["rr"]["pgid"]
    os.killpg(old, signal.SIGKILL)
    m.rt["rr"]["popen"].wait()
    assert wait(lambda: not nmod._group_alive(old))
    real, fired = nmod._group_alive, []

    def interleave(pg):
        if pg == old and not fired:
            fired.append(1)
            m._spawn("rr")
            return False
        return real(pg)
    monkeypatch.setattr(nmod, "_group_alive", interleave)
    assert m._running("rr") is True
    monkeypatch.setattr(nmod, "_group_alive", real)
    assert fired and (nmod.PID_DIR / "rr.pid").exists() and real(m.rt["rr"]["pgid"])
    # a lock-free check while another thread holds the lock only reads
    m2_pid = m.rt["rr"]["pid"]
    holder = threading.Thread(target=lambda: (m._lock.acquire(), time.sleep(1.0), m._lock.release()))
    holder.start()
    time.sleep(0.1)
    os.killpg(m.rt["rr"]["pgid"], signal.SIGKILL)
    m.rt["rr"]["popen"].wait()
    time.sleep(0.2)
    assert m._running("rr") is False and (nmod.PID_DIR / "rr.pid").exists() and m.rt["rr"]["pid"] == m2_pid
    holder.join()
    assert m._running("rr") is False and not (nmod.PID_DIR / "rr.pid").exists()     # cleaned once the lock is free


def test_drop_pidfile_only_for_the_dead_pid(env):
    nmod.PID_DIR.mkdir(parents=True, exist_ok=True)
    pf = nmod.PID_DIR / "zz.pid"
    pf.write_text(json.dumps({"pid": 222, "pgid": 222}))
    rt = {"pid_file_live": True}
    nmod.NodeManager._drop_pidfile("zz", rt, 111)
    assert pf.exists()
    nmod.NodeManager._drop_pidfile("zz", {"pid_file_live": True}, 222)
    assert not pf.exists()
