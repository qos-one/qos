"""Compile an approved Agent into a native module with Cython and upload it to QOS Cloud (step 39, admin)

  qos agent build <name@version> [--no-upload] [--out DIR]

Run on a machine matching the buyer's machine (e.g. Mac Apple Silicon + Python 3.13 → cp313-darwin-arm64)
Build layout:
  strategy.py                         small shim with name/version (the registry can read it statically) that subclasses the compiled class
  _agent_impl.<EXT_SUFFIX>            the original strategy.py, compiled
  <module>.<EXT_SUFFIX>               other .py files, compiled (loaded with qos.sdk.sibling as before)
  data/model files                    copied as is
Limitations: code that uses inspect.getsource / modifies __file__ itself may not work after compiling · compiling makes the code much harder to read, but is not 100% protection
"""
from __future__ import annotations

import ast
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path

from .client import Cloud, CloudError, get

CYTHON = "cython>=3.1,<4"
SKIP = {"__pycache__", ".venv", ".git", "tests", ".pytest_cache", "build"}


def platform_tag() -> str:
    mach = platform.machine().lower()
    mach = {"amd64": "x86_64"}.get(mach, mach)
    if sys.platform == "darwin" and mach == "aarch64":
        mach = "arm64"
    return f"cp{sys.version_info.major}{sys.version_info.minor}-{sys.platform}-{mach}"


def _strategy_class(src: str) -> str:
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and any((isinstance(b, ast.Name) and b.id == "Strategy")
                                                  or (isinstance(b, ast.Attribute) and b.attr == "Strategy") for b in node.bases):
            return node.name
    raise CloudError("bad_package", "strategy.py has no class that subclasses Strategy")


SHIM = '''"""Compiled QOS agent {name}@{version} — built for {tag}. The source code is not included."""
from qos.sdk import Strategy, sibling

_impl = sibling(__file__, "_agent_impl")


class {cls}(_impl.{cls}, Strategy):
    __doc__ = _impl.{cls}.__doc__
    name = {name!r}
    version = {version!r}
    requires = []
'''


def compile_package(src: str | Path, out: str | Path, log=None) -> dict:
    """Copy src → out, compile every .py (strategy.py → _agent_impl) and write the strategy.py shim"""
    from qos.install import static_meta
    log = log or (lambda *_: None)
    src, out = Path(src).resolve(), Path(out).resolve()
    meta = static_meta(src / "strategy.py")
    cls = _strategy_class((src / "strategy.py").read_text())
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out, ignore=lambda _d, names: [n for n in names if n in SKIP or n.endswith((".pyc", ".c", ".so", ".pyd"))
                                                        or n == "QOS-MANIFEST.jws" or n == "cloud.json"])
    (out / "strategy.py").rename(out / "_agent_impl.py")
    mods = sorted(p for p in out.glob("*.py"))
    if any(p for p in out.rglob("*.py") if p.parent != out):
        raise CloudError("bad_package", "Python files in sub-folders are not supported for compiled agents yet")
    log(f"→ compiling {len(mods)} module(s) with Cython for {platform_tag()} …")
    cmd = ["uvx", "--python", sys.executable, "--with", "setuptools", "--from", CYTHON, "cythonize", "-i", "-3", "-q",
           *[p.name for p in mods]]
    p = subprocess.run(cmd, cwd=out, capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        raise CloudError("compile_failed", (p.stderr or p.stdout)[-1500:])
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    for m in mods:
        if not (out / f"{m.stem}{suffix}").exists():
            raise CloudError("compile_failed", f"{m.name} did not produce {m.stem}{suffix}")
        m.unlink()
        (out / f"{m.stem}.c").unlink(missing_ok=True)
    shutil.rmtree(out / "build", ignore_errors=True)
    (out / "strategy.py").write_text(SHIM.format(name=meta["name"], version=meta["version"], cls=cls, tag=platform_tag()))
    log(f"   ✓ {', '.join(f'{m.stem}{suffix}' for m in mods)}")
    return {"name": meta["name"], "version": meta["version"], "platform": platform_tag(), "modules": [m.stem for m in mods]}


def smoke(pkg: str | Path, log=None) -> dict:
    """Check the build works: importable through the registry and passes the qos install checks on synthetic bars"""
    from qos.install import run_checks
    log = log or (lambda *_: None)
    r = run_checks(Path(pkg) / "strategy.py", "synthetic", 10)
    for c in r.get("checks", []):
        log(f"   {'✓' if c['ok'] else '✕'} {c['name']:<13} {c['detail']}")
    if not r.get("ok"):
        raise CloudError("smoke_failed", f"compiled build failed check '{r.get('failed')}'" + (f": {r.get('error')}" if r.get("error") else ""))
    return r


def build(ref: str, c: Cloud | None = None, upload: bool = True, out: str | Path | None = None, log=print) -> dict:
    from .store import download
    name, _, ver = ref.partition("@")
    if not ver:
        raise CloudError("bad_ref", "use name@version")
    c = c or get()
    with tempfile.TemporaryDirectory(prefix="qos-build-") as td:
        src = download(name, ver, c, log, source=True, dest=Path(td) / "src")
        dest = Path(out) if out else Path(td) / "build"
        info = compile_package(src, dest, log)
        log("→ smoke test (import · manifest · network · deterministic · lookahead) …")
        smoke(dest, log)
        if not upload:
            log(f"build ready in {dest} (not uploaded)")
            return info
        arc = Path(td) / f"{name}-{ver}-{info['platform']}.tar.gz"
        with tarfile.open(arc, "w:gz") as t:
            for p in sorted(dest.rglob("*")):
                if p.is_file():
                    t.add(p, arcname=p.relative_to(dest).as_posix(), recursive=False)
        r = c._authed("PUT", f"/admin/agents/{name}/versions/{ver}/builds/{info['platform']}", form={}, files={"file": str(arc)})
        log(f"→ uploaded {name}@{ver} for {info['platform']} ({r['files']} files) — QOS Cloud signed it")
        return {**info, **r}
