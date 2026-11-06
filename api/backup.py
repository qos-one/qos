"""
Backup / restore of QOS data (step 15)

Backed up: data/*.db (SQLite backup API — safe while the API is writing) · data/*.json, *.jsonl · data/strategy_state/ · configs/
Not backed up (rebuildable / large / temporary): data/catalog, data/logs, data/nodes, data/ticks · .env (optional: include_env)
File: <dir>/qos-backup-YYYYmmdd-HHMMSS[-weekly].tar.gz with manifest.json (sha256 of every file)
Rotation: keep the latest keep_daily daily backups · Sunday (weekly) backups keep the latest keep_weekly
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from qos import paths

SKIP_DIRS = {"catalog", "logs", "nodes", "ticks"}
DATA_EXT = {".json", ".jsonl", ".db"}
DEFAULTS = {"dir": "", "hour": 6, "keep_daily": 14, "keep_weekly": 8, "include_env": False}


def settings_file() -> Path:
    return paths.data_dir() / "backup.json"


def load_settings() -> dict:
    try:
        s = {**DEFAULTS, **json.loads(settings_file().read_text())}
    except Exception:
        s = dict(DEFAULTS)
    s["dir"] = s["dir"] or str(paths.home() / "backups")
    return s


def save_settings(upd: dict) -> dict:
    s = {**load_settings(), **{k: v for k, v in upd.items() if k in DEFAULTS and v is not None}}
    if not 0 <= int(s["hour"]) <= 23 or int(s["keep_daily"]) < 1 or int(s["keep_weekly"]) < 0:
        raise ValueError("hour 0–23, keep_daily ≥ 1, keep_weekly ≥ 0")
    settings_file().parent.mkdir(parents=True, exist_ok=True)
    settings_file().write_text(json.dumps(s, indent=2))
    return s


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sqlite_bytes(path: Path) -> bytes:
    """Consistent SQLite copy even while the API is writing (sqlite3 backup API)"""
    with tempfile.TemporaryDirectory() as t:
        out = Path(t) / "copy.db"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return out.read_bytes()


def collect(include_env: bool = False) -> dict[str, bytes]:
    """{name in the backup file: file content}"""
    files: dict[str, bytes] = {}
    data = paths.data_dir()
    if data.is_dir():
        for f in sorted(data.iterdir()):
            if f.is_file() and f.suffix in DATA_EXT and not f.name.endswith(".tmp"):
                files[f"data/{f.name}"] = _sqlite_bytes(f) if f.suffix == ".db" else f.read_bytes()
        st = paths.state_dir()
        if st.is_dir():
            for f in sorted(st.rglob("*")):
                if f.is_file():
                    files[f"data/strategy_state/{f.relative_to(st)}"] = f.read_bytes()
    cfg = paths.config_dir()
    if cfg.is_dir():
        for f in sorted(cfg.rglob("*.json")):
            files[f"configs/strategies/{f.relative_to(cfg)}"] = f.read_bytes()
    env = paths.env_file()
    if include_env and env.exists():
        files["env/.env"] = env.read_bytes()
    return files


def create(dest: str | Path | None = None, include_env: bool | None = None, now: datetime | None = None) -> dict:
    s = load_settings()
    dest = Path(dest or s["dir"]).expanduser()
    include_env = s["include_env"] if include_env is None else include_env
    now = now or datetime.now(timezone.utc)
    weekly = now.astimezone().weekday() == 6
    name = f"qos-backup-{now.astimezone():%Y%m%d-%H%M%S}{'-weekly' if weekly else ''}.tar.gz"
    files = collect(include_env)
    manifest = {"created": now.isoformat(timespec="seconds"), "layout": paths.summary()["layout"], "home": str(paths.home()),
                "include_env": bool(include_env), "files": {k: _sha(v) for k, v in files.items()}}
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / (name + ".partial")
    with tarfile.open(tmp, "w:gz") as tar:
        for k, v in [("manifest.json", json.dumps(manifest, indent=1).encode())] + list(files.items()):
            ti = tarfile.TarInfo(k)
            ti.size, ti.mtime, ti.mode = len(v), int(time.time()), 0o600
            tar.addfile(ti, io.BytesIO(v))
    os.chmod(tmp, 0o600)
    tmp.replace(dest / name)
    removed = rotate(dest, s["keep_daily"], s["keep_weekly"])
    return {"file": str(dest / name), "files": len(files), "bytes": (dest / name).stat().st_size, "weekly": weekly, "removed": removed}


def list_backups(dest: str | Path | None = None) -> list[dict]:
    d = Path(dest or load_settings()["dir"]).expanduser()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("qos-backup-*.tar.gz"), key=lambda f: f.name, reverse=True):
        try:                                                  # time from the file name (local time) — independent of mtime, which changes on copy
            created = datetime.strptime(f.name[11:26], "%Y%m%d-%H%M%S").astimezone().astimezone(timezone.utc)
        except ValueError:
            created = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        out.append({"file": str(f), "name": f.name, "bytes": f.stat().st_size, "weekly": f.name.endswith("-weekly.tar.gz"),
                    "created": created.isoformat(timespec="seconds")})
    return out


def rotate(dest: Path, keep_daily: int, keep_weekly: int) -> list[str]:
    items = sorted(dest.glob("qos-backup-*.tar.gz"), key=lambda f: f.name, reverse=True)
    weekly = [f for f in items if f.name.endswith("-weekly.tar.gz")]
    daily = [f for f in items if f not in weekly]
    gone = daily[keep_daily:] + weekly[keep_weekly:]
    for f in gone:
        f.unlink(missing_ok=True)
    return [f.name for f in gone]


def verify(path: str | Path) -> dict:
    """Verify the sha256 of every file against the manifest · returns the manifest"""
    with tarfile.open(path, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
        for k, h in manifest["files"].items():
            m = tar.extractfile(k)
            if m is None or _sha(m.read()) != h:
                raise ValueError(f"backup is damaged: {k} does not match its checksum")
    return manifest


class RestoreRefused(RuntimeError):
    pass


KEEP_CURRENT = {"data/nautilus_clients.json"}     # IB clientIds belong to this machine's live sessions — never rolled back


def restore(path: str | Path, include_env: bool = False) -> dict:
    """Restore data from a backup · replaced current files move to data/.pre-restore-<time>/ (not deleted)
    Refused while any node process (data/nodes/*.pid) is alive: stop every node in the Terminal first, then stop the
    watchdog and the API (node processes keep running when the API stops, and a restored node list would not match them)."""
    from qos.install import live_pidfile_nodes
    live = live_pidfile_nodes()
    if live:
        raise RestoreRefused(f"node(s) still running: {', '.join(live)} — stop them in the Terminal (Nodes › Stop) before restoring "
                             "(they keep running when the API stops)")
    manifest = verify(path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = paths.data_dir() / f".pre-restore-{stamp}"
    targets = {"data/": paths.data_dir(), "configs/strategies/": paths.config_dir()}
    restored, skipped = [], []
    with tarfile.open(path, "r:gz") as tar:
        for k in manifest["files"]:
            if k == "env/.env":
                if not include_env:
                    continue
                dst = paths.env_file()
            else:
                prefix = next((p for p in targets if k.startswith(p)), None)
                dst = _safe_target(targets[prefix], k[len(prefix):]) if prefix else None
                if dst is None:
                    skipped.append(k)                             # absolute path / '..' / anything resolving outside the target
                    continue
                if k in KEEP_CURRENT and dst.exists():
                    skipped.append(k)
                    continue
            if dst.exists():
                bk = keep / k
                bk.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), bk)
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = tar.extractfile(k).read()
            if k == "data/nautilus_nodes.json":
                data = _nodes_not_desired(data)
            dst.write_bytes(data)
            restored.append(k)
    return {"restored": len(restored), "skipped": skipped, "previous_saved_to": str(keep) if keep.exists() else None,
            "created": manifest["created"]}


def _safe_target(base: Path, rel: str) -> Path | None:
    """base/rel only if it stays inside base (a crafted archive must not write anywhere else)"""
    if not rel or rel.startswith(("/", "\\")) or Path(rel).is_absolute() or ".." in Path(rel).parts:
        return None
    dst = base / rel
    try:
        dst.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return dst


def _nodes_not_desired(raw: bytes) -> bytes:
    """Restored nodes come back stopped (desired=false) — the API must not start strategies that ran when the backup was taken"""
    try:
        doc = json.loads(raw)
        for n in doc.values():
            if isinstance(n, dict):
                n["desired"] = False
        return json.dumps(doc, indent=2, sort_keys=True).encode()
    except Exception:
        return raw


def last_backup() -> dict | None:
    b = list_backups()
    return b[0] if b else None


def due(now: datetime | None = None) -> bool:
    """The daily backup is due and none exists for today yet"""
    s = load_settings()
    now = (now or datetime.now(timezone.utc)).astimezone()
    if now.hour < int(s["hour"]):
        return False
    last = last_backup()
    return not last or datetime.fromisoformat(last["created"]).astimezone().date() < now.date()
