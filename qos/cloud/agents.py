"""Submit an Agent to QOS Cloud and track its status (step 36)

  qos publish <folder> [--check] [--title …] [--description …]
  qos agent status [name[@version]] · qos agent withdraw name@version
  admin: qos agent queue · qos agent approve|reject|recheck name@version [--notes …]
"""
from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path

from .client import Cloud, CloudError, get

SKIP = {"__pycache__", ".venv", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "tests", ".DS_Store"}


def pack(folder: str | Path, out: Path) -> dict:
    root = Path(folder).resolve()
    n = size = 0
    with tarfile.open(out, "w:gz") as t:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in SKIP for part in rel.parts) or p.suffix == ".pyc" or p.name == "QOS-MANIFEST.jws":
                continue
            if p.is_symlink():
                raise CloudError("bad_package", f"symlinks are not allowed: {rel}")
            if p.is_file():
                t.add(p, arcname=rel.as_posix(), recursive=False)
                n += 1
                size += p.stat().st_size
    return {"files": n, "bytes": size}


def publish(folder: str | Path, title: str = "", description: str = "", full_check: bool = False, catalog: str | None = None,
            c: Cloud | None = None, log=None, distribution: str = "compiled") -> dict:
    from qos.publish_check import run, scan
    c = c or get()
    log = log or (lambda *_: None)
    folder = Path(folder).resolve()
    sc = scan(folder)
    for i in sc["issues"]:
        log(f"   {'✕' if i['level'] == 'error' else '!'} {i['file']}:{i['line']}  {i['message']}")
    if not sc["ok"]:
        raise CloudError("scan_failed", f"{len(sc['issues'])} problem(s) found — fix them before publishing")
    name, version = sc["name"], sc["version"]
    log(f"✓ scan {name}@{version}: {sc['files']} files, {sc['bytes'] / 1e6:.2f} MB")
    if full_check:
        log("→ running the same checks as QOS Cloud (checks · backtest · walk-forward) …")
        r = run(folder, catalog, log=lambda s: log("   " + s))
        if not r["ok"]:
            raise CloudError("check_failed", f"local check failed at '{r['stage']}'" + (f": {r.get('error')}" if r.get("error") else ""))
    with tempfile.TemporaryDirectory() as td:
        arc = Path(td) / f"{name}-{version}.tar.gz"
        pack(folder, arc)
        out = c._authed("PUT", f"/agents/{name}/versions/{version}", form={"title": title, "description": description, "distribution": distribution},
                        files={"file": str(arc)})
    log(f"→ submitted {name}@{version} — QOS Cloud checks it next (qos agent status {name})")
    return out


def mine(c: Cloud | None = None) -> list[dict]:
    return (c or get())._authed("GET", "/agents/mine")["agents"]


def listed(c: Cloud | None = None) -> list[dict]:
    return (c or get())._authed("GET", "/agents")["agents"]


def queue(c: Cloud | None = None, status: str = "in_review") -> list[dict]:
    return (c or get())._authed("GET", f"/admin/agents/queue?status={status}")["queue"]


def act(ref: str, action: str, notes: str = "", c: Cloud | None = None) -> dict:
    name, _, ver = ref.partition("@")
    if not ver:
        raise CloudError("bad_ref", "use name@version")
    c = c or get()
    path = f"/agents/{name}/versions/{ver}/withdraw" if action == "withdraw" else f"/admin/agents/{name}/versions/{ver}/{action}"
    return c._authed("POST", path, json_body={"notes": notes})
