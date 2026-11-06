"""QOS Cloud Agent store in the Terminal (step 38)

  qos store list · qos store show <agent> · qos store install <agent> [--version V]

Install = download files whose manifest the Cloud signed → check every file hash → regular `qos install` (checks + evaluation)
Packages from the Cloud carry cloud.json · before a node starts it checks the signature, unmodified files, not withdrawn, and entitlement (if it has a sku)
Versions withdrawn by the Cloud → the API stops nodes using them (see api/cloud_api.py)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .client import Cloud, CloudError, get
from .verify import MANIFEST_FILE, VerifyError, verify_license, verify_manifest

ORIGIN = "cloud.json"
IGNORED = {".venv", "__pycache__", ".pytest_cache"}
EXTRA_OK = {ORIGIN, MANIFEST_FILE, "install.json", "manifest.json", ".DS_Store"}


def _strategies_dir() -> Path:
    from qos import paths
    return paths.strategies_dir()


def version_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (-1,)


def installed(base: Path | None = None) -> dict[str, list[dict]]:
    """Installed Cloud packages {slug: [{ref, version, path, sku, installed_at}]} (newest version first)"""
    out: dict[str, list[dict]] = {}
    base = base or _strategies_dir()
    if not base.exists():
        return out
    for p in base.iterdir():
        o = p / ORIGIN
        if p.is_dir() and o.is_file():
            try:
                d = json.loads(o.read_text())
            except ValueError:
                continue
            out.setdefault(d.get("slug", p.name.split("@")[0]), []).append(
                {"ref": p.name, "version": d.get("version"), "path": str(p), "sku": d.get("sku"), "installed_at": d.get("installed_at")})
    for v in out.values():
        v.sort(key=lambda x: version_key(x["version"] or "0"), reverse=True)
    return out


def overview(c: Cloud | None = None, base: Path | None = None) -> dict:
    c = c or get()
    inst = installed(base)
    err, rows = "", []
    try:
        listed = c._authed("GET", "/agents")["agents"]
    except CloudError as e:
        listed, err = [], e.message
    seen = set()
    for a in listed:
        seen.add(a["slug"])
        mine = inst.get(a["slug"], [])
        have = mine[0]["version"] if mine else None
        rows.append({**a, "installed": [m["version"] for m in mine], "update": bool(have and version_key(a["version"]) > version_key(have)),
                     "revoked": c.revoked("agent", f"{a['slug']}@{a['version']}"),
                     "owned": bool(a.get("prices")) and (c.entitled(f"agent.{a['slug']}") or licence_ok(a["slug"], c))})
    for slug, mine in inst.items():
        if slug not in seen:
            rows.append({"slug": slug, "title": slug, "version": mine[0]["version"], "installed": [m["version"] for m in mine],
                         "update": False, "unlisted": True,
                         "revoked": any(c.revoked("agent", f"{slug}@{m['version']}") for m in mine)})
    from .build import platform_tag
    return {"agents": rows, "error": err, "platform": platform_tag()}


def detail(slug: str, c: Cloud | None = None) -> dict:
    c = c or get()
    return c._authed("GET", f"/agents/{slug}/forward")


def licence_path(c: Cloud, slug: str) -> Path:
    return c.dir / "licenses" / f"{slug}.jws"


def fetch_licence(slug: str, c: Cloud | None = None) -> bool:
    """Request a machine-bound licence for a paid Agent · returns True on success (free Agents return False)"""
    c = c or get()
    r = c._authed("POST", f"/agents/{slug}/license")
    if not r.get("license"):
        return False
    verify_license(c.trust(), r["license"], c._state().get("device_id") or "", slug)
    p = licence_path(c, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(r["license"])
    return True


def refresh_licences(c: Cloud | None = None, base: Path | None = None) -> dict:
    """After sync: renew licences of installed paid Agents (on failure the current licence is used until it expires)"""
    c = c or get()
    out = {}
    for slug, ms in installed(base).items():
        if any(m.get("sku") for m in ms):
            try:
                out[slug] = fetch_licence(slug, c)
            except (CloudError, VerifyError) as e:
                out[slug] = getattr(e, "message", str(e))
    return out


def licence_ok(slug: str, c: Cloud) -> bool:
    """Valid (signature, expiry, device, agent) licence on disk · no device id = not bound to this machine = not ok"""
    device = c._state().get("device_id") or ""
    if not device:
        return False
    try:
        verify_license(c.trust(), licence_path(c, slug).read_text().strip(), device, slug)
        return True
    except (OSError, VerifyError):
        return False


def download(slug: str, version: str | None = None, c: Cloud | None = None, log=None, source: bool = False,
             dest: Path | None = None) -> Path:
    """Download + check manifest → folder data/cloud/agents/<slug>/<version> with cloud.json
    Normally gets the build compiled for this machine · source=True for the owning developer / admin (used when compiling)"""
    from .build import platform_tag
    c = c or get()
    log = log or (lambda *_: None)
    try:
        d = c._authed("POST", f"/agents/{slug}/download", json_body={"version": version, "platform": platform_tag(), "source": source})
    except CloudError as e:
        if e.code == "no_build":
            raise CloudError("no_build", f"{slug} is not built for {platform_tag()} yet — ask QOS to build it (qos agent build)") from None
        raise
    ver, token = d["version"], d["manifest"]
    trust = c.trust()
    claims = verify_manifest(trust, token)
    if claims.get("kind") != "agent" or claims.get("name") != slug or claims.get("version") != ver:
        raise CloudError("bad_manifest", "manifest does not match the requested agent")
    if c.revoked("agent", f"{slug}@{ver}"):
        raise CloudError("withdrawn", f"{slug}@{ver} was withdrawn by QOS")
    if d.get("kind") == "compiled" and (claims.get("meta") or {}).get("build") != platform_tag():
        raise CloudError("bad_manifest", "the build is for another platform")
    want = {f["path"]: f for f in claims["files"]}
    dest = dest or c.dir / "agents" / slug / ver
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{ver}.", dir=dest.parent))
    try:
        for f in d["files"]:
            m = want.get(f["path"])
            if m is None:
                raise CloudError("bad_manifest", f"unexpected file {f['path']}")
            out = tmp / m["path"]
            out.parent.mkdir(parents=True, exist_ok=True)
            n, sha = c.fetch(f["url"], out, m["size"])
            if n != m["size"] or sha != m["sha256"]:
                raise CloudError("corrupt", f"{m['path']} failed its checksum")
        (tmp / MANIFEST_FILE).write_text(token + "\n")
        verify_manifest(trust, token, tmp)
        (tmp / ORIGIN).write_text(json.dumps({"source": "qos-cloud", "url": c.url, "slug": slug, "version": ver, "sku": d.get("sku"),
                                              "title": d.get("title"), "kind": d.get("kind"), "platform": d.get("platform"),
                                              "installed_at": int(time.time())}, indent=2))
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if d.get("sku") and not source:
        fetch_licence(slug, c)
    log(f"downloaded {slug}@{ver} ({d.get('kind', 'source')}, {len(want)} files) — signature and checksums OK")
    return dest


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check_installed(path: str | Path, c: Cloud | None = None) -> tuple[bool, str]:
    """Used before a node starts: signature, files match the manifest (no extra .py), not withdrawn, entitlement per sku"""
    c = c or get()
    path = Path(path)
    try:
        origin = json.loads((path / ORIGIN).read_text())
        claims = verify_manifest(c.trust(), (path / MANIFEST_FILE).read_text().strip())
    except (OSError, ValueError, VerifyError) as e:
        return False, f"QOS Cloud agent signature check failed ({e}) — reinstall it from the Store"
    slug, ver = claims.get("name"), claims.get("version")
    if (origin.get("slug"), origin.get("version")) != (slug, ver):
        return False, "QOS Cloud agent files do not match their manifest — reinstall it from the Store"
    listed = {f["path"]: f for f in claims.get("files", [])}
    for rel, f in listed.items():
        p = path / rel
        if not p.is_file() or p.stat().st_size != f["size"] or _sha(p) != f["sha256"]:
            return False, f"{slug}@{ver}: {rel} was modified — reinstall it from the Store"
    for p in path.rglob("*"):
        rel = p.relative_to(path)
        if p.is_file() and not any(x in IGNORED for x in rel.parts) and rel.as_posix() not in listed and rel.name not in EXTRA_OK:
            return False, f"{slug}@{ver}: unexpected file {rel.as_posix()} — reinstall it from the Store"
    if c.revoked("agent", f"{slug}@{ver}"):
        return False, f"{slug}@{ver} was withdrawn by QOS — install a newer version"
    sku = origin.get("sku")
    if sku and not licence_ok(slug, c) and not c.entitled(sku):
        ent = c.entitlement()
        if not (ent and ent.get("role") == "admin"):
            return False, (f"{slug}: needs {sku}" if c.cloud_features() else f"{slug}: QOS Cloud sign-in expired — sync or sign in")
    return True, ""


def is_cloud_package(path: str | Path | None) -> bool:
    return bool(path) and (Path(path) / ORIGIN).is_file()


def install(slug: str, version: str | None = None, c: Cloud | None = None, log=print) -> dict:
    """CLI (no API): download, then qos install"""
    from qos.install import DEFAULT_API, install as qos_install
    src = download(slug, version, c, log)
    return qos_install(str(src), api=DEFAULT_API, log=log)
