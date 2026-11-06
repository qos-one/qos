"""
Strategy lab — manage configs in configs/strategies and run backtest / parity / walk-forward from the Terminal

- Jobs run as separate processes (existing scripts/*.py) one at a time → the API/trading is not slowed · results saved in reports/ (Backtests tab)
- Progress is read from the script's stdout (walk-forward: "fold k/n")
- Deploy: create a Nautilus node from a config (via NodeManager) with a warning if never backtested / parity failed
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

from qx_strategies import reports

ROOT = Path(__file__).resolve().parent.parent
from . import catalog as _cat  # noqa: E402
CONFIG_DIR = paths.config_dir()
FILE_RE = re.compile(r"^[a-z0-9_]{2,60}\.json$")
BASE_KEYS = {"strategy": str, "strategy_id": str, "instrument": str, "tf_minutes": int,
             "trade_size": str, "sl_pips": float, "tp_pips": float, "allow_short": bool}
CASTS = {"int": int, "float": float, "str": str, "bool": bool}
INSTRUMENTS = [_cat.instrument_id_str(s) for s in _cat.DEFAULT_SYMBOLS]      # matches the Data catalog (step 3)
TIMEFRAMES = [1, 5, 15, 30, 60]          # 1m/5m straight from the catalog · the rest built from 1m bars
SCRIPTS = {"backtest": "scripts/backtest.py", "parity": "scripts/parity.py", "walkforward": "scripts/walkforward.py"}
DEFAULT_DAYS = {"backtest": 90, "parity": 60, "walkforward": 365}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(file: str) -> Path:
    if not FILE_RE.match(file or ""):
        raise ValueError("file name must be 2–60 chars of a–z, 0–9, _ and end with .json")
    return CONFIG_DIR / file


def edit_keys(strategy: str) -> dict:
    from qx_strategies import SPECS
    if strategy not in SPECS:
        raise ValueError(f"unknown strategy '{strategy}' (have: {', '.join(SPECS)})")
    return {**BASE_KEYS, **{p["key"]: CASTS[p["type"]] for p in SPECS[strategy]["params"]}}


def specs() -> dict:
    from qx_strategies import SPECS
    return {k: {"label": v["label"], "doc": v["doc"], "params": v["params"]} for k, v in SPECS.items()}


def clean_config(cfg: dict) -> dict:
    out = {}
    for k, typ in edit_keys(str(cfg.get("strategy") or "ema_cross")).items():
        if k in cfg and cfg[k] is not None and cfg[k] != "":
            out[k] = typ(cfg[k]) if typ is not bool else bool(cfg[k])
    if "trade_size" in out:
        out["trade_size"] = str(int(float(out["trade_size"])))
    out.setdefault("strategy", "ema_cross")
    if out.get("instrument") not in INSTRUMENTS:
        raise ValueError(f"instrument must be one of {', '.join(INSTRUMENTS)} (what the data catalog has)")
    if out.get("tf_minutes") not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {TIMEFRAMES} minutes")
    if float(out.get("trade_size", 0)) < 1000:
        raise ValueError("trade size must be at least 1,000 units for FX")
    return out


def validate(cfg: dict) -> None:
    from qx_strategies import check_config
    check_config(cfg, source="EXTERNAL")


def _latest_reports(items: list[dict]) -> dict[str, dict]:
    """Latest report per (config_file, kind) — old reports without config_file are matched by strategy_id"""
    out: dict[str, dict] = {}
    for r in items:                                        # already newest first
        for key in (f"file:{r.get('config_file')}:{r['kind']}", f"sid:{r.get('strategy_id')}:{r['kind']}"):
            out.setdefault(key, r)
    return out


def list_configs(nodes: dict[str, dict] | None = None) -> list[dict]:
    rep = _latest_reports(reports.list_reports(300))
    used: dict[str, list[str]] = {}
    for nid, n in (nodes or {}).items():
        used.setdefault(n.get("config_file", ""), []).append(nid)
    out = []
    for f in sorted(CONFIG_DIR.glob("*.json")):
        try:
            cfg = json.loads(f.read_text())
        except Exception as e:
            out.append({"file": f.name, "error": str(e)[:120], "config": {}})
            continue
        latest = {}
        for kind in reports.KINDS:
            r = rep.get(f"file:{f.name}:{kind}") or rep.get(f"sid:{cfg.get('strategy_id')}:{kind}")
            if r:
                latest[kind] = {k: r.get(k) for k in ("file", "created", "label", "pnl", "trades", "passed", "score")}
        from qx_strategies import SPECS, summary
        out.append({"file": f.name, "config": cfg, "latest": latest, "used_by": used.get(f.name, []),
                    "summary": summary(cfg), "label": SPECS.get(cfg.get("strategy", ""), {}).get("label", cfg.get("strategy", "")),
                    "modified": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return out


def save_config(file: str, cfg: dict, overwrite: bool) -> dict:
    p = _path(file)
    if p.exists() and not overwrite:
        raise ValueError(f"{file} already exists")
    c = clean_config(cfg)
    taken = set()
    for other in CONFIG_DIR.glob("*.json"):
        if other.name != file:
            try:
                taken.add(json.loads(other.read_text()).get("strategy_id"))
            except Exception:
                pass
    if not c.get("strategy_id") or c["strategy_id"] in taken:       # Save as → new ID so reports do not mix
        camel = "".join(w.title() for w in c["strategy"].split("@")[0].split("_"))            # ema_cross → QXEmaCross (same as the old class name)
        c["strategy_id"] = "QX" + camel + "-" + p.stem.upper().replace("_", "-")[:30]
    validate(c)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(c, indent=2) + "\n")
    return c


def delete_config(file: str, nodes: dict[str, dict]) -> None:
    p = _path(file)
    users = [nid for nid, n in nodes.items() if n.get("config_file") == file]
    if users:
        raise ValueError(f"used by node(s): {', '.join(users)} — delete those nodes first")
    if not p.exists():
        raise KeyError(file)
    p.unlink()


def deploy_checks(file: str) -> list[str]:
    """Warnings before deploy (not blocking if the user confirms)"""
    cfg = json.loads(_path(file).read_text())
    rep = _latest_reports(reports.list_reports(300))
    get = lambda kind: rep.get(f"file:{file}:{kind}") or rep.get(f"sid:{cfg.get('strategy_id')}:{kind}")   # noqa: E731
    warn = []
    bt, par, wf = get("backtest"), get("parity"), get("walkforward")
    mtime = datetime.fromtimestamp(_path(file).stat().st_mtime).isoformat(timespec="seconds")
    if not bt:
        warn.append("No backtest yet for this config.")
    elif (bt.get("created") or "") < mtime:
        warn.append("Config changed after the last backtest.")
    if bt and (bt.get("pnl") or 0) < 0:
        warn.append(f"Last backtest lost money ({bt.get('pnl'):,.2f} USD).")
    if par and par.get("passed") is False:
        warn.append(f"Last parity check failed ({par.get('label')}).")
    if wf and (wf.get("pnl") or 0) < 0:
        warn.append(f"Walk-forward out-of-sample result is negative ({wf.get('pnl'):,.2f} USD).")
    return warn


# ---------- jobs ----------
class Lab:
    def __init__(self, history: int = 30):
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.history = history
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self.on_finish: list = []              # callback(job) after any job ends (drives the package evaluation queue)

    def running(self) -> dict | None:
        return next((self.jobs[j] for j in reversed(self.order) if self.jobs[j]["status"] == "running"), None)

    def list(self) -> list[dict]:
        return [self.public(self.jobs[j]) for j in reversed(self.order)]

    @staticmethod
    def public(j: dict, log: bool = False) -> dict:
        d = {k: v for k, v in j.items() if k != "log"}
        d["last_line"] = j["log"][-1] if j["log"] else ""
        if log:
            d["log"] = j["log"][-200:]
        return d

    @staticmethod
    def uv() -> str:
        return paths.uv()

    def build_cmd(self, kind: str, file: str, opts: dict) -> list[str]:
        if kind not in SCRIPTS:
            raise ValueError("kind must be backtest, parity or walkforward")
        _path(file)
        cmd = [paths.uv(), "run", SCRIPTS[kind], str(CONFIG_DIR / file)]
        if opts.get("start"):
            cmd += ["--start", str(opts["start"])]
            if opts.get("end"):
                cmd += ["--end", str(opts["end"])]
        else:
            cmd += ["--days", str(int(opts.get("days") or DEFAULT_DAYS[kind]))]
        if kind == "walkforward":
            cmd += ["--train", str(int(opts.get("train") or 90)), "--test", str(int(opts.get("test") or 30))]
            if opts.get("grid"):
                cmd += ["--grid", json.dumps(opts["grid"]) if not isinstance(opts["grid"], str) else opts["grid"]]
        return cmd

    async def start(self, kind: str, file: str, opts: dict, operator: str = "") -> dict:
        if self.running():
            raise ValueError("another job is still running — wait for it or cancel it")
        if not _path(file).exists():
            raise KeyError(file)
        return await self._run(kind, self.build_cmd(kind, file, opts), {"config_file": file, "options": opts}, operator)

    async def start_cmd(self, kind: str, cmd: list[str], meta: dict, operator: str = "", on_done=None) -> dict:
        """Job from any command (e.g. package evaluate) · 'RESULT {json}' line → job['result'] · on_done(job) when finished"""
        if self.running():
            raise ValueError("another job is still running — wait for it or cancel it")
        return await self._run(kind, cmd, {"config_file": "", "options": {}, **meta}, operator, on_done)

    async def _run(self, kind: str, cmd: list[str], meta: dict, operator: str, on_done=None) -> dict:
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, **meta, "status": "running", "started": _now(), "finished": None, "progress": None,
               "report": None, "result": None, "summary": "", "exit_code": None, "operator": operator, "log": []}
        self.jobs[jid] = job
        self.order.append(jid)
        for old in self.order[:-self.history]:
            self.jobs.pop(old, None)
        self.order = self.order[-self.history:]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(ROOT), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                                                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self._procs[jid] = proc
        asyncio.create_task(self._watch(job, proc, on_done))
        return self.public(job)

    async def _watch(self, job: dict, proc, on_done=None) -> None:
        t0 = time.time()
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore").rstrip()
            if not line:
                continue
            if line.startswith("RESULT "):
                try:
                    job["result"] = json.loads(line[7:])
                except json.JSONDecodeError:
                    pass
                continue
            job["log"].append(line[:400])
            job["log"] = job["log"][-400:]
            m = re.search(r"fold (\d+)/(\d+)", line)
            if m:
                job["progress"] = round(int(m.group(1)) / int(m.group(2)), 3)
            m = re.search(r"saved → reports/(\w+)/(\S+\.json)", line)
            if m:
                kind = {v: k for k, v in reports.KINDS.items()}.get(m.group(1), job["kind"])
                job["report"] = {"kind": kind, "file": m.group(2)}
            if re.match(r"^(pnl_usd|match rate|out-of-sample)\b", line):
                job["summary"] = re.sub(r"\s+", " ", line)
        rc = await proc.wait()
        self._procs.pop(job["id"], None)
        job["exit_code"] = rc
        job["finished"] = _now()
        job["duration_s"] = round(time.time() - t0, 1)
        if job["status"] != "cancelled":
            ok = bool(job["report"] or job["result"])
            job["status"] = "done" if ok else "failed"      # parity failed = exit 1 but a report exists → done
            job["progress"] = 1.0 if ok else job["progress"]
        for cb in ([on_done] if on_done else []) + list(self.on_finish):
            try:
                r = cb(job)
                if asyncio.iscoroutine(r):
                    await r
            except Exception as e:
                print("lab on_finish:", e)

    def cancel(self, jid: str) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        proc = self._procs.get(jid)
        if proc and job["status"] == "running":
            job["status"] = "cancelled"
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
        return self.public(job)
