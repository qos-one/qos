"""Step 15: sleep prevention, sleep/wake detection, watchdog, pausing nodes after wake, backup/restore"""
import asyncio
import json
import sqlite3
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


# ---------- Sleep prevention ----------
class FakeProc:
    def __init__(self, *a, **k):
        self.args, self.pid, self.done = a[0], 4242, False

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.done = True

    def wait(self, timeout=None):
        return 0


def test_power_guard_holds_assertion_only_while_nodes_run():
    from api.power import PowerGuard
    spawned = []
    pg = PowerGuard(platform="darwin", spawn=lambda *a, **k: spawned.append(FakeProc(*a)) or spawned[-1])
    assert pg.update(True) and spawned[0].args == ["caffeinate", "-i", "-s"]
    assert pg.update(True) and len(spawned) == 1                     # no duplicate spawn
    assert pg.update(False) is False and spawned[0].done
    assert PowerGuard(platform="linux").update(True) is False


def test_parse_pmset():
    from api.power import parse_pmset
    out = "Battery Power:\n sleep                1\nAC Power:\n sleep                10\n standby              1\n displaysleep         10\n"
    r = parse_pmset(out)
    assert r["will_sleep"] and r["sleep_min"] == 10 and r["standby"] and "pmset -c sleep 0" in r["hint"]
    assert parse_pmset("AC Power:\n sleep                0\n")["will_sleep"] is False


# ---------- Sleep detection / restart ----------
def test_wake_watch_detects_sleep_and_downtime(tmp_path):
    from api.wake import WakeWatch
    clock = {"wall": 1_000_000.0, "mono": 50.0, "boot": 900_000.0}
    ww = WakeWatch(tmp_path / "wake.json", 120, wall=lambda: clock["wall"], mono=lambda: clock["mono"], boot=lambda: clock["boot"])
    assert ww.startup() is None
    clock["wall"] += 15
    clock["mono"] += 15
    assert ww.tick() is None
    clock["wall"] += 6 * 3600 + 15                                   # slept 6 hours: monotonic advanced only 15 seconds
    clock["mono"] += 15
    ev = ww.tick()
    assert ev["kind"] == "sleep" and ev["minutes"] == 360.0
    clock["wall"] += 3600                                             # API stopped for 1 h (machine did not restart)
    ww2 = WakeWatch(tmp_path / "wake.json", 120, wall=lambda: clock["wall"], mono=lambda: 0.0, boot=lambda: clock["boot"])
    assert ww2.startup()["kind"] == "api_down"
    clock["wall"] += 3600
    clock["boot"] = clock["wall"] - 100                               # machine restarted
    ww3 = WakeWatch(tmp_path / "wake.json", 120, wall=lambda: clock["wall"], mono=lambda: 0.0, boot=lambda: clock["boot"])
    assert ww3.startup()["kind"] == "reboot" and [e["kind"] for e in ww3.recent()] == ["sleep", "api_down", "reboot"]


# ---------- watchdog ----------
def test_watchdog_kickstarts_then_alerts_then_recovers():
    import watchdog as wd
    st, acts = {}, []
    for i in range(6):
        acts += wd.step(st, {"api": False, "redis": True, "gateway": None}, 1000 + 60 * i)
    assert [a[0] for a in acts] == ["kickstart", "alert"] and "not responding" in acts[1][1]
    acts = wd.step(st, {"api": True, "redis": True, "gateway": None}, 2000)
    assert acts[0][0] == "alert" and "back" in acts[0][1] and st["api"]["fails"] == 0
    acts = []
    for i in range(12):
        acts += wd.step(st, {"api": True, "redis": True, "gateway": False}, 3000 + 60 * i)
    assert len(acts) == 1 and "IB Gateway" in acts[0][1]              # 10 minutes, notified once


def test_watchdog_restarts_gateway_at_10_and_25_min():
    import watchdog as wd
    st, acts = {}, []
    for i in range(30):
        acts += wd.step(st, {"api": True, "redis": True, "gateway": False}, 3000 + 60 * i, restart_gateway=True)
    assert [a[0] for a in acts] == ["restart_gateway", "alert", "restart_gateway", "alert"]
    assert "10 min" in acts[1][1] and "IBKR Mobile" in acts[3][1]
    acts = wd.step(st, {"api": True, "redis": True, "gateway": True}, 9000, restart_gateway=True)
    assert acts[0][0] == "alert" and "back" in acts[0][1]


def test_ib_ok_handshake(monkeypatch):
    import struct
    import watchdog as wd

    class Sock:
        def __init__(self, reply): self.reply, self.sent = reply, b""
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): pass
        def sendall(self, b): self.sent += b
        def recv(self, n): return self.reply[:n]
    logged_in = struct.pack(">I", 22) + b"187\x0020261109 12:00:00\x00"
    monkeypatch.setattr(wd.socket, "create_connection", lambda a, timeout=0: Sock(logged_in))
    assert wd.ib_ok("127.0.0.1", 4004)
    monkeypatch.setattr(wd.socket, "create_connection", lambda a, timeout=0: Sock(b""))    # socat, nothing behind it
    assert not wd.ib_ok("127.0.0.1", 4004)


def test_watchdog_run_once_writes_state(tmp_path, monkeypatch):
    import watchdog as wd
    monkeypatch.setenv("QOS_DATA_DIR", str(tmp_path))
    (tmp_path / "gateways.json").write_text(json.dumps({"gateways": [{"id": "default", "api_port": 4999}]}))
    monkeypatch.setattr(wd, "http_ok", lambda url, timeout=5.0: False)
    monkeypatch.setattr(wd, "tcp_ok", lambda h, p, timeout=3.0: p != 4999)
    calls = []
    monkeypatch.setattr(wd.subprocess, "run", lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": 0, "stderr": ""})())
    for _ in range(3):
        doc = wd.run_once("com.test-api", 8000)
    assert calls and calls[0][:3] == ["launchctl", "kickstart", "-k"] and calls[0][3].endswith("/com.test-api")
    saved = json.loads((tmp_path / "watchdog.json").read_text())
    assert saved["gateway_port"] == 4999 and saved["checks"]["gateway"] is False and saved["state"]["api"]["fails"] == 3


# ---------- Pausing nodes after wake ----------
def test_held_node_cannot_open_new_positions():
    from qx_strategies.risk import RiskState
    s = RiskState()
    s.node_id = "n1"
    s.update({"halted": False, "blocked_nodes": {"n1": "mismatch FX:EURUSD after mac was asleep"}})
    assert s.halt_reason().startswith("held: mismatch")
    s.node_id = "n2"
    assert s.halt_reason() == ""


def test_machine_gap_holds_mismatched_nodes(tmp_path, monkeypatch):
    from api import holds, server
    monkeypatch.setenv("QOS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server.nm, "nodes", {"n1": {"desired": True}, "n2": {"desired": True}})
    audits, sent, incidents = [], [], []
    monkeypatch.setattr(server, "audit", lambda ev, **f: audits.append(ev))
    monkeypatch.setattr(server.notify, "fire", lambda msg, kind="": sent.append((kind, msg)))
    monkeypatch.setattr(server.soak, "incident", lambda n, k, d="": incidents.append((n, k)))
    monkeypatch.setattr(server.ib, "isConnected", lambda: True)
    rows = [{"key": "FX:EURUSD", "status": "mismatch", "ib_qty": 20000, "nodes": [{"node": "n1", "qty": 0}]},
            {"key": "FX:GBPUSD", "status": "ok", "ib_qty": -10000, "nodes": [{"node": "n2", "qty": -10000}]}]

    async def pos():
        return {"reconcile": {"rows": rows}}

    async def pub():
        return {}
    monkeypatch.setattr(server, "positions", pos)
    monkeypatch.setattr(server, "publish_risk", pub)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(server.asyncio, "sleep", lambda s: real_sleep(0))
    ev = {"kind": "sleep", "from": "2026-10-27T19:13:00+00:00", "to": "2026-10-28T01:25:00+00:00", "minutes": 372.0}
    r = asyncio.run(server._on_machine_gap(ev, delay=0))
    assert r["held"] == ["n1"] and holds.payload() == {"n1": "mismatch FX:EURUSD after mac was asleep"}
    assert "EURUSD +20,000" in r["open"] and ("n1", "sleep") in incidents and "machine_gap" in audits
    assert sent[-1][0] == "error" and "paused for n1" in sent[-1][1]
    out = asyncio.run(server.node_release_hold("n1"))
    assert out["released"] and holds.payload() == {}


# ---------- backup / restore ----------
@pytest.fixture()
def home(tmp_path, monkeypatch):
    for k in ("QOS_DATA_DIR", "QOS_CONFIG_DIR", "QOS_ENV_FILE", "QOS_STRATEGIES_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("QOS_HOME", str(tmp_path / "Q"))
    d = tmp_path / "Q" / "data"
    (d / "strategy_state").mkdir(parents=True)
    (d / "catalog").mkdir()
    (d / "catalog" / "big.parquet").write_bytes(b"x" * 100)
    (tmp_path / "Q" / "configs" / "strategies").mkdir(parents=True)
    (tmp_path / "Q" / "configs" / "strategies" / "a.json").write_text('{"strategy": "ema_cross"}')
    (d / "risk.json").write_text('{"default": {}}')
    (d / "strategy_state" / "n1.json").write_text('{"k": 1}')
    (tmp_path / "Q" / ".env").write_text("TELEGRAM_BOT_TOKEN=secret\n")
    con = sqlite3.connect(d / "history.db")
    con.execute("CREATE TABLE t (i INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    con.commit()
    con.close()
    return tmp_path / "Q"


def test_backup_while_writing_and_restore(home):
    from api import backup
    stop = threading.Event()

    def writer():
        con = sqlite3.connect(home / "data" / "history.db", timeout=30)
        i = 1000
        while not stop.is_set():
            con.execute("INSERT INTO t VALUES (?)", (i,))
            con.commit()
            i += 1
        con.close()
    th = threading.Thread(target=writer)
    th.start()
    try:
        r = backup.create()
    finally:
        stop.set()
        th.join()
    names = tarfile.open(r["file"]).getnames()
    assert "data/history.db" in names and "data/risk.json" in names and "data/strategy_state/n1.json" in names
    assert "configs/strategies/a.json" in names and not any("catalog" in n or ".env" in n for n in names)
    m = backup.verify(r["file"])
    assert m["files"] and not m["include_env"]
    (home / "data" / "risk.json").write_text('{"broken": true}')
    (home / "data" / "history.db").unlink()
    out = backup.restore(r["file"])
    assert json.loads((home / "data" / "risk.json").read_text()) == {"default": {}}
    n = sqlite3.connect(home / "data" / "history.db").execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert n >= 1000 and out["previous_saved_to"] and Path(out["previous_saved_to"], "data", "risk.json").exists()
    assert (home / ".env").read_text() == "TELEGRAM_BOT_TOKEN=secret\n"


def test_backup_damaged_file_is_refused(home, tmp_path):
    from api import backup
    r = backup.create()
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(r["file"]) as src, tarfile.open(bad, "w:gz") as dst:
        for ti in src.getmembers():
            data = src.extractfile(ti).read()
            if ti.name == "data/risk.json":
                data = b'{"tampered": 1}'
                ti.size = len(data)
            import io
            dst.addfile(ti, io.BytesIO(data))
    with pytest.raises(ValueError, match="damaged"):
        backup.verify(bad)


def test_backup_rotation_and_env_option(home, tmp_path):
    from api import backup
    backup.save_settings({"keep_daily": 2, "keep_weekly": 1})
    made = []
    for day in range(1, 9):                                           # Nov 1–8, 2026 (8 = Sunday)
        made.append(backup.create(now=datetime(2026, 11, day, 12, tzinfo=timezone.utc)))
    names = sorted(x["name"] for x in backup.list_backups())
    weekly = [n for n in names if n.endswith("-weekly.tar.gz")]
    assert len(weekly) == 1 and len(names) == 3
    r = backup.create(include_env=True, now=datetime(2026, 11, 9, 12, tzinfo=timezone.utc))
    assert "env/.env" in tarfile.open(r["file"]).getnames()


def test_backup_due(home):
    from api import backup
    backup.save_settings({"hour": 6})
    early = datetime(2026, 11, 2, 3, tzinfo=timezone.utc).astimezone().replace(hour=5)
    assert not backup.due(early)
    late = early.replace(hour=7)
    assert backup.due(late)
    backup.create(now=late)
    assert not backup.due(late.replace(hour=8))
