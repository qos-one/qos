"""Data from the QOS Cloud data store (step 35)

  qos data list · qos data pull fx-1m [--version V] · qos data remove fx-1m
  catalog.bars("EUR/USD", "5m", source="cloud:fx-1m")       # use in backtest / SDK

Dataset = a Nautilus catalog folder whose manifest is signed by the Cloud · downloaded to data/cloud/datasets/<slug>/<version>
Every file hash is checked before use · usable per entitlement (offline within the grace period) · withdrawn versions cannot be used
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from .client import Cloud, CloudError, get
from .verify import MANIFEST_FILE, VerifyError, verify_manifest


class DataAccessError(CloudError):
    pass


def root(c: Cloud) -> Path:
    return c.dir / "datasets"


def _index(c: Cloud) -> dict:
    try:
        return json.loads((root(c) / "index.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_index(c: Cloud, idx: dict) -> None:
    root(c).mkdir(parents=True, exist_ok=True)
    tmp = root(c) / "index.json.tmp"
    tmp.write_text(json.dumps(idx, indent=2))
    os.replace(tmp, root(c) / "index.json")


def _state(c: Cloud, e: dict) -> tuple[str, str]:
    if c.revoked("dataset", f"{e['slug']}@{e['version']}"):
        return "withdrawn", "this version was withdrawn by QOS — pull a newer one"
    ent = c.entitlement()
    if e.get("sku") and not c.entitled(e["sku"]) and not (ent and ent.get("role") == "admin"):
        return "locked", (f"needs {e['sku']}" if c.cloud_features() else "QOS Cloud sign-in expired — sync or sign in")
    return "ok", ""


def local(c: Cloud | None = None) -> list[dict]:
    c = c or get()
    out = []
    for slug, e in sorted(_index(c).items()):
        state, why = _state(c, e)
        out.append({**e, "state": state, "reason": why})
    return out


def remote(c: Cloud | None = None) -> list[dict]:
    return (c or get())._authed("GET", "/datasets")["datasets"]


def overview(c: Cloud | None = None) -> dict:
    """Merge the Cloud listing with what is on this machine (local items stay visible when the Cloud is down)"""
    c = c or get()
    loc = {e["slug"]: e for e in local(c)}
    err, rem = "", []
    try:
        rem = remote(c)
    except CloudError as e:
        err = e.message
    rows = []
    for d in rem:
        e = loc.pop(d["slug"], None)
        latest = (d.get("latest") or {}).get("version")
        rows.append({**d, "local": e, "update": bool(e and latest and e["version"] != latest)})
    for e in loc.values():
        rows.append({"slug": e["slug"], "title": e.get("title", ""), "sku": e.get("sku"), "entitled": None, "latest": None,
                     "local": e, "update": False})
    return {"datasets": rows, "error": err}


def pull(slug: str, version: str | None = None, c: Cloud | None = None, keep: int = 1, log=None) -> dict:
    c = c or get()
    log = log or (lambda *_: None)
    d = c._authed("POST", f"/datasets/{slug}/download", json_body={"version": version})
    ver, token = d["version"], d["manifest"]
    trust = c.trust()
    claims = verify_manifest(trust, token)
    if claims.get("kind") != "dataset" or claims.get("name") != slug or claims.get("version") != ver:
        raise DataAccessError("bad_manifest", "manifest does not match the requested dataset")
    if c.revoked("dataset", f"{slug}@{ver}"):
        raise DataAccessError("withdrawn", f"{slug}@{ver} was withdrawn by QOS")
    want = {f["path"]: f for f in claims["files"]}
    dest = root(c) / slug / ver
    if dest.is_dir():
        try:
            verify_manifest(trust, (dest / MANIFEST_FILE).read_text().strip(), dest)
            log(f"{slug}@{ver} already up to date")
            return _record(c, slug, ver, d, claims, dest, keep)
        except (OSError, VerifyError):
            shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{ver}.", dir=dest.parent))
    try:
        total = sum(f["size"] for f in claims["files"])
        done = 0
        for f in d["files"]:
            m = want.get(f["path"])
            if m is None:
                raise DataAccessError("bad_manifest", f"unexpected file {f['path']}")
            out = tmp / m["path"]
            out.parent.mkdir(parents=True, exist_ok=True)
            n, sha = c.fetch(f["url"], out, m["size"])
            if n != m["size"] or sha != m["sha256"]:
                raise DataAccessError("corrupt", f"{m['path']} failed its checksum")
            done += n
            log(f"  {done / 1e6:,.1f} / {total / 1e6:,.1f} MB  {m['path']}")
        (tmp / MANIFEST_FILE).write_text(token + "\n")
        verify_manifest(trust, token, tmp)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log(f"{slug}@{ver} ready ({len(want)} files, {total / 1e6:,.1f} MB)")
    return _record(c, slug, ver, d, claims, dest, keep)


def _record(c: Cloud, slug: str, ver: str, d: dict, claims: dict, dest: Path, keep: int) -> dict:
    idx = _index(c)
    e = {"slug": slug, "version": ver, "sku": d.get("sku"), "title": d.get("title", ""), "path": str(dest),
         "files": len(claims["files"]), "bytes": sum(f["size"] for f in claims["files"]), "pulled_at": int(time.time())}
    idx[slug] = e
    _save_index(c, idx)
    olds = sorted((p for p in (root(c) / slug).iterdir() if p.is_dir() and p.name != ver and not p.name.startswith(".")),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for p in olds[max(0, keep - 1):]:
        shutil.rmtree(p, ignore_errors=True)
    return e


def resolve(slug: str, c: Cloud | None = None) -> Path:
    """Catalog folder of a downloaded dataset — checks signature, entitlement and withdrawal"""
    c = c or get()
    e = _index(c).get(slug)
    if not e:
        raise DataAccessError("not_pulled", f"dataset '{slug}' is not on this computer — run: qos data pull {slug}")
    p = Path(e["path"])
    try:
        claims = verify_manifest(c.trust(), (p / MANIFEST_FILE).read_text().strip())
    except (OSError, VerifyError) as err:
        raise DataAccessError("bad_manifest", f"{slug}: signature check failed ({err}) — pull it again") from None
    if claims.get("name") != slug:
        raise DataAccessError("bad_manifest", f"{slug}: manifest belongs to {claims.get('name')}")
    state, why = _state(c, e)
    if state != "ok":
        raise DataAccessError(state, f"{slug}@{e['version']}: {why}")
    return p


def check(slug: str, c: Cloud | None = None) -> dict:
    """Check every file hash (slower than resolve)"""
    c = c or get()
    p = resolve(slug, c)
    claims = verify_manifest(c.trust(), (p / MANIFEST_FILE).read_text().strip(), p)
    return {"slug": slug, "version": claims["version"], "files": len(claims["files"]), "ok": True}


def remove(slug: str, c: Cloud | None = None) -> bool:
    c = c or get()
    idx = _index(c)
    gone = idx.pop(slug, None) is not None
    shutil.rmtree(root(c) / slug, ignore_errors=True)
    _save_index(c, idx)
    return gone


def pack(catalog_dir: str | Path, out: str | Path, match: list[str] | None = None) -> dict:
    """Build a tar.gz from part of a Nautilus catalog for publishing (bars matching the pattern + their instruments)"""
    cat = Path(catalog_dir)
    pats = match or ["*"]
    bars = sorted(p for p in (cat / "data" / "bar").glob("*") if p.is_dir() and any(fnmatch.fnmatch(p.name, m) for m in pats))
    if not bars:
        raise ValueError(f"no bar folders in {cat / 'data' / 'bar'} match {', '.join(pats)}")
    insts = sorted({b.name.split("-")[0] for b in bars})
    dirs = bars + [cat / "data" / "currency_pair" / i for i in insts if (cat / "data" / "currency_pair" / i).is_dir()]
    n = size = 0
    with tarfile.open(out, "w:gz") as t:
        for d in dirs:
            for f in sorted(d.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    t.add(f, arcname=f.relative_to(cat).as_posix(), recursive=False)
                    n += 1
                    size += f.stat().st_size
    return {"file": str(out), "files": n, "bytes": size, "bars": [b.name for b in bars], "instruments": insts}


def publish(slug: str, version: str, catalog_dir: str | Path, match: list[str] | None = None, title: str = "",
            sku: str | None = None, description: str = "", notes: str = "", draft: bool = False,
            c: Cloud | None = None, log=None) -> dict:
    """admin: pack part of the catalog and upload it as a new version (the Cloud signs the manifest)"""
    c = c or get()
    log = log or (lambda *_: None)
    with tempfile.TemporaryDirectory() as td:
        arc = Path(td) / f"{slug}-{version}.tar.gz"
        info = pack(catalog_dir, arc, match)
        log(f"packed {info['files']} files ({info['bytes'] / 1e6:,.1f} MB): {', '.join(info['bars'])}")
        fields = {"title": title, "description": description, "notes": notes, "draft": "true" if draft else "false"}
        if sku is not None:
            fields["sku"] = sku
        out = c._authed("PUT", f"/admin/datasets/{slug}/versions/{version}", form=fields, files={"file": str(arc)})
    log(f"published {slug}@{version}: {out['files']} files, status {out['status']}")
    return out
