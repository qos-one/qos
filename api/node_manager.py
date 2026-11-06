"""
Node manager — runs multiple Nautilus TradingNodes (1 node = 1 Strategy = 1 process) and keeps them running

- Node definitions live in data/nautilus_nodes.json: config file in configs/strategies + adjusted values (overrides), auto_restart, desired
- Processes are separate from the API (start_new_session) and **are not stopped when the API restarts** — on startup the API "adopts" existing processes from pid files
- Supervisor every 5 seconds: a node that should run but stopped on its own → restart with backoff (5s, 10s, 20s … ≤5 min)
  More than 5 crashes in 15 minutes → state "crashed": stop retrying, alert, wait for the user to press Start
- Health from the node log: connected / ready (RUNNING) / latest ERROR
- clientId uses the same allocator as nautilus_bridge (no clash with the API/agents/scripts)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)
from typing import Callable

from . import nautilus_bridge as nb

ROOT = nb.ROOT
NODES_FILE = paths.data_dir() / "nautilus_nodes.json"
PID_DIR = paths.data_dir() / "nodes"
CONFIG_DIR = paths.config_dir()
ID_RE = re.compile(r"^[a-z0-9_]{2,32}$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
LOG_MAX = 20 * 1024 * 1024
BACKOFF_MAX_S = 300
CRASH_LIMIT, CRASH_WINDOW_S = 5, 900
BLOCKED_RETRY_S = 30                     # first retry after a blocked (refused) restart; doubles up to BACKOFF_MAX_S
RESTARTS_KEEP = 50                       # restarts[] is bounded (only the crash window matters)
LICENCE_CHECK_S = 900                    # how often running nodes are re-checked with licence_check (if set by the server)
OVERRIDE_KEYS = {"fast": int, "slow": int, "trade_size": str, "sl_pips": float, "tp_pips": float, "allow_short": bool, "tf_minutes": int}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (s or "node")[:32]


def list_configs() -> list[dict]:
    out = []
    for f in sorted(CONFIG_DIR.glob("*.json")):
        try:
            c = json.loads(f.read_text())
        except Exception:
            continue
        out.append({"file": f.name, "strategy": c.get("strategy", ""), "instrument": c.get("instrument", ""),
                    "tf_minutes": c.get("tf_minutes"), "params": {k: c[k] for k in OVERRIDE_KEYS if k in c}})
    return out


def _to_int(v) -> int:
    """int cast that refuses to silently truncate (2.9 → error, 8.0 / "8" → 8)"""
    if isinstance(v, bool):
        return int(v)
    f = float(v)
    if f != int(f):
        raise ValueError(f"{v!r} is not a whole number")
    return int(f)


def clean_overrides(o: dict, extra: dict | None = None) -> dict:
    """extra = strategy parameters from the manifest in the config (step 21: values edited in the Terminal are node overrides)"""
    keys = {**OVERRIDE_KEYS, **(extra or {})}
    out = {}
    for k, v in (o or {}).items():
        if k in keys and v is not None and v != "":
            if keys[k] is bool:
                out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif keys[k] is int:
                out[k] = _to_int(v)
            else:
                out[k] = keys[k](v)
    return out


def param_casts(config_file: str) -> dict:
    """{key: type} of the strategy parameters this config uses (from manifest/spec)"""
    try:
        cfg = json.loads((CONFIG_DIR / config_file).read_text())
        from qx_strategies import SPECS
        params = SPECS.get(str(cfg.get("strategy", "ema_cross")), {}).get("params") or []
    except Exception:
        return {}
    casts = {"int": int, "float": float, "str": str, "bool": bool}
    return {p["key"]: casts.get(p.get("type"), str) for p in params if p.get("key")}


NODE_NICE = int(os.getenv("QOS_NODE_NICE", "5"))


def launch_cmd(cfg: dict) -> list[str]:
    """Packages with their own venv (requires) → that venv's python · others → main env (uv run)"""
    from qx_strategies.registry import venv_python
    py = venv_python(cfg.get("package_dir"))
    if py:
        return [py, "nautilus_runner.py", "--config", json.dumps(cfg)]
    return [paths.uv(), "run", "nautilus_runner.py", "--config", json.dumps(cfg)]


def _limit_resources(max_memory_mb: int, nice: int):
    """Set in the child process before exec: CPU nice · memory is enforced by the supervisor on resident memory (RSS / footprint)
    on every platform. RLIMIT_AS is opt-in only (QOS_FORCE_RLIMIT_AS=1): it caps *virtual* address space, and a Nautilus node
    (Rust runtime threads, IB client threads, glibc arenas) reserves far more virtual memory than it uses, so on Linux a 2 GB
    cap made thread creation fail ("can't start new thread") and the IB clients never connected."""
    def f():
        try:
            os.nice(nice)
        except OSError:
            pass
        if max_memory_mb and os.getenv("QOS_FORCE_RLIMIT_AS") == "1":
            import resource
            try:
                lim = max_memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
            except (ValueError, OSError):
                pass
    return f


def _darwin_footprint_mb(pid: int) -> float | None:
    """macOS: physical footprint (includes compressed memory — same number as Activity Monitor) from proc_pid_rusage
    ps RSS on macOS is far below reality when the process is idle (compressed pages are not counted)"""
    import ctypes
    global _libproc
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        class RUsageV2(ctypes.Structure):
            _fields_ = [("uuid", ctypes.c_uint8 * 16), ("v", ctypes.c_uint64 * 32)]
        buf = RUsageV2()
        if _libproc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(2), ctypes.byref(buf)) != 0:
            return None
        return buf.v[7] / (1024 * 1024)                      # ri_phys_footprint
    except Exception:
        return None


_libproc = None


def proc_stats(pid) -> dict:
    """Includes the node's whole process group (the spawned pid is the `uv run` wrapper — the real Python/Nautilus is a child in the same group)"""
    if not pid:
        return {}
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,rss=,%cpu=,nice="], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return {}
    rss, cpu, nice, n, foot = 0, 0.0, None, 0, 0.0
    for ln in out.splitlines():
        f = ln.split()
        if len(f) < 5:
            continue
        try:
            if int(f[1]) != int(pid):
                continue
            rss += int(f[2])
            fp = _darwin_footprint_mb(int(f[0])) if sys.platform == "darwin" else None
            foot += fp if fp is not None else int(f[2]) / 1024
            cpu += float(f[3])
            nice = int(f[4]) if nice is None else max(nice, int(f[4]))
            n += 1
        except ValueError:
            continue
    if not n:
        return {}
    return {"mem_mb": round(foot, 1), "rss_mb": round(rss / 1024, 1), "cpu_pct": round(cpu, 1), "nice": nice, "processes": n}


def _resolve_safe(ref: str) -> str | None:
    try:
        from qx_strategies import resolve_ref
        return resolve_ref(ref)
    except Exception:
        return None


def _summary(cfg: dict) -> str:
    try:
        from qx_strategies import summary
        return summary(cfg)
    except Exception:
        return cfg.get("strategy", "")


def _zombie(pid: int) -> bool:
    """An exited process not yet reaped by its parent still answers kill(0) — it is not running (Linux /proc, else ps)"""
    if Path("/proc").is_dir():
        try:
            st = Path(f"/proc/{pid}/stat").read_text()
            return st[st.rindex(")") + 2] in "ZX"
        except Exception:
            return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return False
    return out[:1] in ("Z", "X") if out else False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                                                     # macOS: EPERM can also mean a zombie
    return not _zombie(pid)


def _own_group(pid: int | None) -> int | None:
    """Process group of a node process — only if the process leads its own group (start_new_session), never the API's group"""
    if not pid:
        return None
    try:
        pg = os.getpgid(pid)
    except Exception:
        return None
    return pg if pg == pid and pg != os.getpgrp() else None


def _group_alive(pgid: int | None) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                                      # macOS answers EPERM for a group of only zombies
        return _group_has_live_member(pgid)
    except OSError:
        return False
    return _group_has_live_member(pgid)


def _group_has_live_member(pgid: int) -> bool:
    """kill(0) succeeds for a group whose members have all exited but are not reaped yet (zombies) — that group is not
    running. macOS may list such zombies without their group id, so: ps worked and no non-zombie member → not running."""
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=3)
    except Exception:
        return True
    if r.returncode != 0 or not r.stdout.strip():
        return True                                              # cannot tell → assume running (fail safe)
    for ln in r.stdout.splitlines():
        f = ln.split()
        if len(f) >= 3 and f[1] == str(pgid) and not f[2].startswith(("Z", "X")):
            return True
    return False


def _group_pids(pgid: int) -> list[int]:
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    pids = []
    for ln in out.splitlines():
        f = ln.split()
        if len(f) == 2 and f[1] == str(pgid) and f[0].isdigit():
            pids.append(int(f[0]))
    return pids


def _cmdline(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ""


class NodeManager:
    def __init__(self, can_start: Callable[..., tuple[bool, str]] | None = None, on_event: Callable[..., None] | None = None,
                 launcher: Callable[[str, dict, Path], subprocess.Popen] | None = None):
        self.can_start = can_start or (lambda: (True, ""))
        self.on_event = on_event or (lambda *a, **k: None)
        self.launcher = launcher or self._launch
        self.nodes: dict[str, dict] = {}
        self.rt: dict[str, dict] = {}          # runtime: pid, popen, started, restarts[], next_try, gave_up, last_exit, reason
        self._task: asyncio.Task | None = None
        self.live: Callable[[str], dict] = lambda nid: {}      # live state from the event bus (step 7) — set by the server
        self.size_multiplier: Callable[[str], float] = lambda nid: 1.0     # step 14: size per ramp-up stage on a live gateway
        # optional re-check of running nodes (e.g. QOS Store licence/revocation) → (ok, why); a failure stops the node
        self.licence_check: Callable[[str], tuple[bool, str]] | None = None
        # one lock for every state change (start/stop/restart/tick/adopt/resume/delete): the supervisor runs in a worker thread
        # while API requests run in others — without it a Stop could land in the middle of a respawn and leave a node trading
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()   # short: snapshot + write of nautilus_nodes.json (never held while killing)
        self.load()

    def _can_start(self, nid: str) -> tuple[bool, str]:
        import inspect
        try:
            takes_nid = len(inspect.signature(self.can_start).parameters) >= 1
        except (TypeError, ValueError):
            takes_nid = False
        return self.can_start(nid) if takes_nid else self.can_start()

    # ---------- persistence ----------
    def load(self) -> None:
        """nautilus_nodes.json → self.nodes · a missing file = no nodes · a corrupt/empty file is kept aside and the nodes that
        still have a live process are rebuilt from their pid files (desired=true) so adopt/Stop/kill switch still reach them"""
        raw = None
        for attempt in range(3):
            try:
                raw = NODES_FILE.read_text()
                break
            except FileNotFoundError:
                raw = None
                break
            except OSError:                                    # I/O problem ≠ corrupt content: retry, then fail loudly
                if attempt == 2:
                    raise
                time.sleep(0.2)
        nodes, rebuilt = None, False
        if raw is not None:
            try:
                doc = json.loads(raw)
                nodes = doc if isinstance(doc, dict) else None
            except ValueError:
                nodes = None
            if nodes is None:
                try:
                    NODES_FILE.replace(NODES_FILE.with_name(f"{NODES_FILE.name}.corrupt-{int(time.time())}"))
                except OSError:
                    pass
                nodes, rebuilt = self._rebuild_from_pidfiles(), True
                print(f"node manager: {NODES_FILE.name} was unreadable — rebuilt {sorted(nodes)} from pid files")
        self.nodes = nodes or {}
        for nid in self.nodes:
            self.rt.setdefault(nid, self._fresh_rt())
        if rebuilt:
            self.save()

    @staticmethod
    def _rebuild_from_pidfiles() -> dict:
        out = {}
        for pf in sorted(PID_DIR.glob("*.pid")) if PID_DIR.is_dir() else []:
            nid = pf.stem
            try:
                info = json.loads(pf.read_text())
            except Exception:
                continue
            pid, pgid = int(info.get("pid") or 0), int(info.get("pgid") or 0) or None
            if not ID_RE.match(nid) or not ((pid and _pid_alive(pid)) or _group_alive(pgid)):
                continue
            # same identity check as _adopt: the live process must really be this node's runner (pids get reused)
            cmd = _cmdline(pid) if pid and _pid_alive(pid) else ""
            if not cmd and pgid:
                cmd = " ".join(_cmdline(int(x)) for x in _group_pids(pgid))
            verified = "nautilus_runner.py" in cmd and f'"agent_id": "{nid}"' in cmd
            n = {"id": nid, "name": info.get("name") or nid, "config_file": info.get("config_file", ""),
                 "overrides": info.get("overrides") or {}, "auto_restart": False, "desired": verified,
                 "created": _now(), "rebuilt": "from pid file after nautilus_nodes.json was unreadable"
                                               + ("" if verified else " (process not verified — left stopped)")}
            for k in ("allow_live", "ramp"):
                if info.get(k) is not None:
                    n[k] = info[k]
            out[nid] = n
        return out

    def save(self) -> None:
        """Atomic write (temp file + replace) — a crash mid-write never leaves a torn nodes file
        Uses only the short _save_lock (not the node lock, which a Stop holds for up to ~18 s per node): event-loop callers
        never block behind a kill in progress"""
        with self._save_lock:
            data = None
            for _ in range(20):                                 # another thread may mutate a node dict mid-snapshot → retry
                try:
                    data = json.dumps(self.nodes, indent=2, sort_keys=True)
                    break
                except RuntimeError:
                    time.sleep(0.005)
            if data is None:
                data = json.dumps(json.loads(json.dumps(dict(self.nodes))), indent=2, sort_keys=True)
            NODES_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = NODES_FILE.with_name(f".{NODES_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            with open(tmp, "w") as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            tmp.replace(NODES_FILE)

    @staticmethod
    def _fresh_rt() -> dict:
        return {"pid": None, "pgid": None, "popen": None, "started": None, "restarts": [], "next_try": 0.0, "gave_up": False,
                "last_exit": None, "reason": "", "last_stop": None, "blocked": 0, "licence_checked": 0.0}

    # ---------- definitions ----------
    def _strategy_name(self, nid: str) -> str:
        try:
            return str(self.effective_config(nid).get("strategy", "ema_cross")).split("@")[0]
        except Exception:
            return "ema_cross"

    def effective_config(self, nid: str) -> dict:
        n = self.nodes[nid]
        cfg = json.loads((CONFIG_DIR / n["config_file"]).read_text())
        return {**cfg, **n.get("overrides", {})}

    def validate(self, config_file: str, overrides: dict) -> dict:
        if "/" in config_file or not (CONFIG_DIR / config_file).exists():
            raise ValueError(f"unknown config file '{config_file}'")
        cfg = {**json.loads((CONFIG_DIR / config_file).read_text()), **overrides}
        from qx_strategies import check_config          # validate with the real constructor (bad values, e.g. fast >= slow, error here) · packages are checked in a subprocess
        check_config(cfg, source="INTERNAL")
        return cfg

    def create(self, name: str, config_file: str, overrides: dict | None = None, auto_restart: bool = True, node_id: str = "") -> dict:
        nid = node_id or slug(name)
        if not ID_RE.match(nid):
            raise ValueError("node id must be 2–32 chars: a–z, 0–9, _")
        ov = clean_overrides(overrides or {}, param_casts(config_file))
        self.validate(config_file, ov)
        with self._lock:
            if nid in self.nodes:
                raise ValueError(f"node '{nid}' already exists")
            tags = nb.node_tags(nid)                 # trader_id + order tag form the client_order_id prefix → must be unique
            clash = next((o for o in self.nodes if nb.node_tags(o) == tags), None)
            if clash:
                raise ValueError(f"node id '{nid}' maps to the same order ids as node '{clash}' — choose another id")
            self.nodes[nid] = {"id": nid, "name": name.strip() or nid, "config_file": config_file, "overrides": ov,
                               "auto_restart": bool(auto_restart), "desired": False, "created": _now()}
            self.rt[nid] = self._fresh_rt()
            self.save()
        self.on_event("node_created", node=nid, config_file=config_file, overrides=ov)
        return self.status(nid)

    def update(self, nid: str, name: str | None = None, overrides: dict | None = None, auto_restart: bool | None = None,
               config_file: str | None = None) -> dict:
        n = self._get(nid)
        if overrides is not None or config_file is not None:
            cf = config_file or n["config_file"]
            ov = clean_overrides(overrides, param_casts(cf)) if overrides is not None else n.get("overrides", {})
            self.validate(cf, ov)
        with self._lock:
            if overrides is not None or config_file is not None:
                n["config_file"], n["overrides"] = cf, ov
            if name is not None:
                n["name"] = name.strip() or nid
            if auto_restart is not None:
                n["auto_restart"] = bool(auto_restart)
            self.save()
        self.on_event("node_updated", node=nid, overrides=n.get("overrides"), auto_restart=n["auto_restart"])
        st = self.status(nid)
        st["restart_needed"] = st["running"] and (overrides is not None or config_file is not None)
        return st

    def delete(self, nid: str) -> None:
        with self._lock:
            self._get(nid)
            if self._running(nid):
                raise ValueError("stop the node before deleting it")
            self.nodes.pop(nid)
            self.rt.pop(nid, None)
            (PID_DIR / f"{nid}.pid").unlink(missing_ok=True)
            nb.release_client_id(nid)
            self.save()
        self.on_event("node_deleted", node=nid)

    def _get(self, nid: str) -> dict:
        if nid not in self.nodes:
            raise KeyError(nid)
        return self.nodes[nid]

    # ---------- process control ----------
    def _launch(self, nid: str, cfg: dict, log: Path) -> subprocess.Popen:
        cmd = launch_cmd(cfg)
        f = open(log, "a")
        mem = int((cfg.get("package_limits") or {}).get("max_memory_mb") or 0)
        return subprocess.Popen(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT, start_new_session=True,
                                preexec_fn=_limit_resources(mem, NODE_NICE))

    @staticmethod
    def _snap(rt: dict) -> tuple:
        return rt.get("popen"), rt.get("pid"), rt.get("pgid"), rt.get("started")

    @staticmethod
    def _alive(snap: tuple) -> bool:
        """Read-only liveness of one snapshot (spawned process, adopted pid, or anything left in its process group)"""
        p, pid, pgid, _ = snap
        if p is not None:
            if p.poll() is None:
                return True
        elif pid and _pid_alive(pid):
            return True
        return _group_alive(pgid)

    def _running(self, nid: str) -> bool:
        """True while the spawned process OR anything left in its process group is alive
        (the spawned pid is the `uv run` wrapper — if it dies alone, the real Nautilus child keeps running in the same group)
        Lock-free callers (status / brief from request threads) only read: state is cleaned up (pid/pgid cleared, stale pid
        file removed) only by a caller that holds — or can take without waiting — the node lock, after re-checking that the
        process it saw dead is still the current one (a respawn may have happened in between)."""
        rt = self.rt.get(nid) or {}
        snap = self._snap(rt)
        if self._alive(snap):
            return True
        if not self._lock.acquire(blocking=False):
            return False                                        # someone else is changing this node — report, don't touch
        try:
            cur = self._snap(rt)
            if cur != snap:                                     # respawned/adopted meanwhile → judge the new process
                if self._alive(cur):
                    return True
                snap = cur
            p, pid, pgid, _ = snap
            if p is not None:
                rt["last_exit"] = p.returncode
            rt["popen"] = None
            rt["pid"] = None
            if rt.get("pid_file_live"):
                self._drop_pidfile(nid, rt, (p.pid if p is not None else pid) or pgid)
            rt["pgid"] = None
            return False
        finally:
            self._lock.release()

    @staticmethod
    def _drop_pidfile(nid: str, rt: dict, dead_pid: int | None) -> None:
        """Remove the pid file only if it still names the process that was seen dead"""
        pf = PID_DIR / f"{nid}.pid"
        try:
            info = json.loads(pf.read_text())
        except Exception:
            rt["pid_file_live"] = False
            return
        if dead_pid and dead_pid in (int(info.get("pid") or 0), int(info.get("pgid") or 0)):
            pf.unlink(missing_ok=True)
        rt["pid_file_live"] = False

    def _preflight(self, nid: str) -> dict:
        """Checks that need no process: start allowed (halt / live gates / licence) + a usable gateway & account → target"""
        ok, why = self._can_start(nid)
        if not ok:
            raise RuntimeError(why)
        target = nb.resolve_gateway()
        if target.get("error"):
            raise RuntimeError(target["error"])
        if not target["account_id"]:
            raise RuntimeError("no IBKR account selected — sign in and choose an account first")
        return target

    def _spawn(self, nid: str) -> None:
        target = self._preflight(nid)
        base = nb.allocate_client_id(nid)
        from qx_strategies.package_limits import resolve as resolve_limits
        from qx_strategies.registry import REG
        eff = self.effective_config(nid)
        entry = REG.resolve(eff.get("strategy", "ema_cross"))         # lock the strategy version at start (name@version)
        ref = entry.ref
        limits = resolve_limits(entry.manifest if entry.kind != "builtin" else None, eff.get("instrument"))
        if limits["instruments"] and eff.get("instrument") not in limits["instruments"]:
            raise RuntimeError(f"{ref} declares instruments {', '.join(limits['instruments'])} — not {eff.get('instrument')}")
        pkg_dir = str(entry.path) if entry.kind != "builtin" and entry.path else None
        cfg = {**eff, "strategy": ref, "host": target["host"], "port": target["port"], "account_id": target["account_id"],
               "client_id": base, "agent_id": nid, "package_limits": limits, "package_dir": pkg_dir}
        mult = 1.0
        if target.get("mode") == "live":                              # step 14: real money starts small per ramp-up
            from .ramp import scaled_size
            mult = float(self.size_multiplier(nid))
            lot = int((((self.nodes.get(nid) or {}).get("ramp") or {}).get("config") or {}).get("lot") or 1000)   # the ramp's own lot
            cfg["trade_size"] = scaled_size(eff.get("trade_size", "20000"), mult, lot)
        log = nb.log_path(nid)
        if log.exists() and log.stat().st_size > LOG_MAX:
            log.replace(log.with_suffix(".log.1"))
        with log.open("a") as f:
            f.write(f"\n===== start {_now()} node={nid} gateway={target['gateway_id']}({target['mode']}) port={target['port']} "
                    f"clientId={base}/{base + 1} {json.dumps(cfg)}\n")
        p = self.launcher(nid, cfg, log)
        rt = self.rt.setdefault(nid, self._fresh_rt())
        rt.update(popen=p, pid=p.pid, pgid=_own_group(p.pid), pid_file_live=True, started=time.time(), reason="", gateway=target, client_id=base, strategy_ref=ref,
                  package_limits=limits, python=launch_cmd(cfg)[0], proc={},
                  live_size={"multiplier": mult, "trade_size": cfg.get("trade_size"), "base_trade_size": eff.get("trade_size")})
        PID_DIR.mkdir(parents=True, exist_ok=True)
        (PID_DIR / f"{nid}.pid").write_text(json.dumps({"pid": p.pid, "pgid": rt["pgid"], "started": rt["started"], "gateway": target, "client_id": base,
                                                        "name": self.nodes[nid].get("name"), "config_file": self.nodes[nid].get("config_file"),
                                                        "overrides": self.nodes[nid].get("overrides") or {},
                                                        "allow_live": self.nodes[nid].get("allow_live"), "ramp": self.nodes[nid].get("ramp"),
                                                        "strategy_ref": ref, "package_limits": limits, "python": rt["python"],
                                                        "live_size": rt["live_size"]}))

    def _signal(self, nid: str, sig: int) -> None:
        """Signal the node's whole process group (stored pgid — works even after the wrapper died) or else its pid's group"""
        rt = self.rt.get(nid) or {}
        pgid = rt.get("pgid") or _own_group(rt.get("pid"))
        try:
            if pgid:
                os.killpg(pgid, sig)
            elif rt.get("pid"):
                os.kill(rt["pid"], sig)
        except Exception:
            pass

    def _kill(self, nid: str, timeout: float = 15.0) -> None:
        with self._lock:
            rt = self.rt.get(nid) or {}
            if self._running(nid):
                self._signal(nid, signal.SIGINT)                     # let Nautilus shut down cleanly (cancel orders/close positions per strategy)
                t0 = time.time()
                while time.time() - t0 < timeout and self._running(nid):
                    time.sleep(0.2)
                if self._running(nid):
                    self._signal(nid, signal.SIGKILL)
                    t0 = time.time()
                    while time.time() - t0 < 3 and self._running(nid):
                        time.sleep(0.1)
            self._running(nid)
            rt.update(pid=None, popen=None, last_stop=time.time())
            if not _group_alive(rt.get("pgid")):
                rt["pgid"] = None
            (PID_DIR / f"{nid}.pid").unlink(missing_ok=True)

    def start(self, nid: str) -> dict:
        with self._lock:
            n = self._get(nid)
            if not self._running(nid):
                self._spawn(nid)
            n["desired"] = True
            rt = self.rt[nid]
            rt.update(gave_up=False, restarts=[], next_try=0.0, blocked=0)
            self.save()
        self.on_event("node_started", node=nid, pid=rt["pid"], port=rt.get("gateway", {}).get("port"), client_id=rt.get("client_id"))
        return self.status(nid)

    def stop(self, nid: str, reason: str = "manual") -> dict:
        n = self._get(nid)
        n["desired"] = False                                     # visible at once to a respawn in flight (it re-checks after launch)
        with self._lock:
            n["desired"] = False
            self.save()
            self._kill(nid)
            self.rt[nid]["reason"] = reason
        self.on_event("node_stopped", node=nid, reason=reason)
        return self.status(nid)

    def restart(self, nid: str) -> dict:
        with self._lock:                                         # the supervisor cannot see the gap between kill and start
            self._get(nid)
            self._preflight(nid)                                 # refused (halt, live gate, no account …) → node keeps running
            self._kill(nid)
            return self.start(nid)

    def stop_all(self, reason: str) -> list[str]:
        snap = list(self.nodes.items())                          # one C-level copy — safe against concurrent create/delete
        wanted = {nid for nid, n in snap if n.get("desired")}
        for _nid, n in snap:                                     # kill switch: nothing may be (re)started from here on
            n["desired"] = False
        with self._lock:
            for n in self.nodes.values():
                n["desired"] = False
            stopped = [nid for nid in list(self.nodes) if nid in wanted or self._running(nid)]
            for nid in stopped:
                self.stop(nid, reason)
            self.save()
        return stopped

    def adopt(self) -> list[str]:
        """After the API restarts: adopt processes still running from pid files (checks the command line really is this node)"""
        adopted = []
        with self._lock:
            adopted = self._adopt()
        if adopted:
            self.on_event("nodes_adopted", nodes=adopted)
        return adopted

    def _adopt(self) -> list[str]:
        adopted = []
        for nid in self.nodes:
            pf = PID_DIR / f"{nid}.pid"
            try:
                info = json.loads(pf.read_text())
            except Exception:
                continue
            pid = int(info.get("pid", 0))
            pgid = int(info.get("pgid") or 0) or None
            cmd = _cmdline(pid) if pid and _pid_alive(pid) else ""
            if not cmd and pgid and _group_alive(pgid):
                # the wrapper is gone but its group still runs the node (orphaned child) — adopt the group so Stop can reach it
                cmd = " ".join(_cmdline(int(x)) for x in _group_pids(pgid))
            if "nautilus_runner.py" in cmd and f'"agent_id": "{nid}"' in cmd:
                self.rt[nid].update(pid=pid if _pid_alive(pid) else None, pgid=pgid or _own_group(pid), popen=None, pid_file_live=True, started=info.get("started"), gateway=info.get("gateway", {}),
                                    strategy_ref=info.get("strategy_ref") or f"{self._strategy_name(nid)}@builtin",   # processes from before step 19 use built-in strategies
                                    client_id=info.get("client_id"),
                                    package_limits=info.get("package_limits"),          # None = started before step 22 (no limits yet)
                                    python=info.get("python"), proc={}, live_size=info.get("live_size"))
                adopted.append(nid)
            else:
                pf.unlink(missing_ok=True)
        return adopted

    def resume(self) -> tuple[list[str], dict[str, str]]:
        """After adopt: start nodes that should run (desired) but have no process — e.g. after a reboot"""
        ok, failed = [], {}
        with self._lock:
            for nid, n in self.nodes.items():
                if n.get("desired") and not self._running(nid):
                    try:
                        self._spawn(nid)
                        ok.append(nid)
                    except Exception as e:
                        failed[nid] = str(e)
                        rt = self.rt[nid]
                        rt["reason"] = f"not resumed: {e}"
                        rt["next_try"] = time.time() + BLOCKED_RETRY_S
                        rt["blocked"] = rt.get("blocked", 0) + 1
                        rt["restarts"] = (rt["restarts"] + [time.time()])[-RESTARTS_KEEP:]
        return ok, failed

    # ---------- supervisor ----------
    def tick(self, now: float | None = None) -> list[str]:
        """Called periodically: restart nodes that should run but stopped on their own (returns the list started)
        · stop processes that run although the node is not desired (stop raced with a respawn, adopted after an API crash)"""
        with self._lock:
            return self._tick(now or time.time())

    def _give_up(self, nid: str, why: str) -> None:
        rt = self.rt[nid]
        rt["gave_up"] = True
        rt["reason"] = why
        self.on_event("node_crashed", node=nid, exit_code=rt["last_exit"], reason=why, notify=True)

    def _tick(self, now: float) -> list[str]:
        acted = []
        for nid in list(self.nodes):
            if self._running(nid):
                if not self.nodes[nid].get("desired"):
                    self._kill(nid)
                    self.rt[nid]["reason"] = "stopped by the supervisor: process was running although the node is stopped"
                    self.on_event("node_stopped", node=nid, reason="not desired (supervisor)", notify=True)
                    continue
                self._watch_resources(nid)
                self._check_licence(nid, now)
        for nid, n in self.nodes.items():
            if not n.get("desired") or self._running(nid):
                continue
            rt = self.rt[nid]
            if not n.get("auto_restart") or rt["gave_up"]:
                continue
            rt["restarts"] = [t for t in rt["restarts"] if now - t < CRASH_WINDOW_S][-RESTARTS_KEEP:]
            if rt["next_try"] == 0.0:                                 # just detected as stopped
                if len(rt["restarts"]) >= CRASH_LIMIT:
                    self._give_up(nid, f"crashed {CRASH_LIMIT}× in {CRASH_WINDOW_S // 60} min — press Start to retry")
                    continue
                delay = min(BACKOFF_MAX_S, 5 * 2 ** len(rt["restarts"]))
                rt["next_try"] = now + delay
                rt["reason"] = f"exited (code {rt['last_exit']}) — restarting in {delay}s"
                self.on_event("node_exited", node=nid, exit_code=rt["last_exit"], restart_in_s=delay)
                continue
            if now < rt["next_try"]:
                continue
            rt["next_try"] = 0.0
            rt["restarts"].append(now)
            try:
                self._spawn(nid)
            except Exception as e:
                # refused restart (halted, licence, no account, no clientId …) counts toward the crash limit → give up + alert
                rt["blocked"] = rt.get("blocked", 0) + 1
                if len(rt["restarts"]) >= CRASH_LIMIT:
                    self._give_up(nid, f"restart blocked {len(rt['restarts'])}× in {CRASH_WINDOW_S // 60} min: {e} — press Start to retry")
                    continue
                delay = min(BACKOFF_MAX_S, BLOCKED_RETRY_S * 2 ** (rt["blocked"] - 1))
                rt["reason"] = f"restart blocked: {e} — retrying in {delay}s"
                rt["next_try"] = now + delay
                continue
            rt["blocked"] = 0
            if not n.get("desired"):                                  # stopped while the process was being launched
                self._kill(nid)
                continue
            acted.append(nid)
            self.on_event("node_restarted", node=nid, attempt=len(rt["restarts"]), pid=rt["pid"])
        return acted

    def _check_licence(self, nid: str, now: float) -> None:
        """Every LICENCE_CHECK_S: re-run licence_check (set by the server) for a running node — a failure stops it"""
        if self.licence_check is None:
            return
        rt = self.rt[nid]
        if now - (rt.get("licence_checked") or 0.0) < LICENCE_CHECK_S:
            return
        rt["licence_checked"] = now
        try:
            ok, why = self.licence_check(nid)
        except Exception as e:
            ok, why = True, ""                                         # the check itself failed (offline) — keep running
            print("licence check:", e)
        if not ok:
            self.on_event("node_licence_invalid", node=nid, reason=why, notify=True)
            self.stop(nid, f"licence check failed: {why}")

    def _watch_resources(self, nid: str) -> None:
        """Record node memory/cpu · above max_memory_mb = stop the node (the memory limit is enforced here, on resident memory)"""
        rt = self.rt.get(nid) or {}
        st = proc_stats(rt.get("pid"))
        if not st:
            return
        rt["proc"] = {**st, "ts": time.time()}
        cap = int((rt.get("package_limits") or {}).get("max_memory_mb") or 0)
        mem = st.get("mem_mb", st["rss_mb"])
        if cap and mem > cap:
            self.on_event("node_memory_limit", node=nid, mem_mb=mem, max_memory_mb=cap, notify=True)
            self.stop(nid, f"memory {mem:.0f} MB > max_memory_mb {cap}")

    async def run(self, interval: float = 5.0) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as e:
                print("node supervisor:", e)
            await asyncio.sleep(interval)

    def start_supervisor(self) -> None:
        try:                                                           # lets notifications from worker threads reach the event loop
            from . import notify
            notify.set_loop(asyncio.get_running_loop())
        except Exception:
            pass
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    # ---------- status / health / log ----------
    def health(self, nid: str) -> dict:
        lines = self.log(nid, 400, raw=True)
        start_idx = max((i for i, ln in enumerate(lines) if ln.startswith("===== start")), default=0)
        cur = lines[start_idx:]
        h = {"ready": False, "connected": None, "last_error": "", "errors": 0, "warnings": 0, "last_log": ""}
        rt = self.rt.get(nid) or {}
        proc = rt.get("proc") or (proc_stats(rt.get("pid")) if rt.get("pid") else {})
        h.update(mem_mb=proc.get("mem_mb"), rss_mb=proc.get("rss_mb"), cpu_pct=proc.get("cpu_pct"), nice=proc.get("nice"),
                 max_memory_mb=(rt.get("package_limits") or {}).get("max_memory_mb"), python=rt.get("python"))
        for ln in cur:
            if "TradingNode: RUNNING" in ln or "Trader: RUNNING" in ln or "ExecEngine: RUNNING" in ln:
                h["ready"] = True
            if "Connected to Interactive Brokers" in ln:
                h["connected"] = True
            elif re.search(r"Disconnected|Connection lost|connection.*(closed|reset)", ln, re.I):
                h["connected"] = False
            if "[ERROR]" in ln:
                h["errors"] += 1
                h["last_error"] = ln[-240:]
            elif "[WARN]" in ln:
                h["warnings"] += 1
        if cur:
            h["last_log"] = cur[-1][-240:]
            m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", cur[-1])
            h["last_log_ts"] = m.group(1) + "Z" if m else None
        return h

    def status(self, nid: str) -> dict:
        n = self._get(nid)
        rt = self.rt.setdefault(nid, self._fresh_rt())
        running = self._running(nid)
        h = self.health(nid) if (running or rt.get("started")) else {}
        if running and not h.get("ready"):
            # the "RUNNING" line may scroll out of the log window read (long-running nodes) — a live heartbeat from the event bus confirms the node is working
            age = (self.live(nid) or {}).get("heartbeat_age_s")
            if age is not None and age <= 30:
                h["ready"] = True
        if running:
            state = "running" if h.get("ready") and h.get("connected") is not False else ("degraded" if h.get("ready") else "starting")
        elif n.get("desired"):
            state = "crashed" if rt["gave_up"] else "backoff"
        else:
            state = "stopped"
        try:
            cfg = self.effective_config(nid)
        except Exception as e:
            cfg = {"error": str(e)}
        started = rt.get("started")
        return {**n, "state": state, "running": running, "pid": rt.get("pid") if running else None,
                "uptime_s": int(time.time() - started) if running and started else None,
                "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds") if started else None,
                "restarts_15m": len([t for t in rt["restarts"] if time.time() - t < CRASH_WINDOW_S]),
                "last_exit": rt.get("last_exit"), "reason": rt.get("reason", ""),
                "client_id": rt.get("client_id") or nb._load_clients().get(nid), "gateway": rt.get("gateway"),
                "strategy_ref": rt.get("strategy_ref") if running else None,       # version this process is running
                "package_limits": rt.get("package_limits") if running else None,   # None while running = process started before step 22 (old values)
                "limits_active": bool(rt.get("package_limits")) if running else None,
                "live_size": rt.get("live_size") if running else None,          # step 14: size this process uses (ramp-up)
                "strategy_latest": _resolve_safe(cfg.get("strategy", "ema_cross")),
                "config": cfg, "summary": _summary(cfg),
                "health": h, "live": self.live(nid) if running else {}}

    def brief(self) -> dict[str, str]:
        """Lightweight status (no log reading) for /health"""
        out = {}
        for nid, n in self.nodes.items():
            rt = self.rt.setdefault(nid, self._fresh_rt())
            out[nid] = "running" if self._running(nid) else ("crashed" if rt["gave_up"] else "backoff") if n.get("desired") else "stopped"
        return out

    def list(self) -> list[dict]:
        return [self.status(nid) for nid in sorted(self.nodes)]

    def log(self, nid: str, lines: int = 200, level: str = "", raw: bool = False) -> list[str]:
        self._get(nid)
        lp = nb.log_path(nid)
        if not lp.exists():
            return []
        with lp.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 512 * 1024))
            text = f.read().decode(errors="ignore")
        out = [ANSI.sub("", ln) for ln in text.splitlines()]
        if level:
            lv = level.upper()
            want = {"ERROR": ("[ERROR]",), "WARN": ("[ERROR]", "[WARN]")}.get(lv, ())
            out = [ln for ln in out if any(w in ln for w in want) or ln.startswith("=====")]
        out = out[-lines:]
        return out if raw else [ln[:400] for ln in out]
