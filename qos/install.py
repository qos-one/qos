"""
qos install / uninstall / status — used by both the CLI (qos_cli.py) and tests

install(src): read files/clone → checks in a subprocess (import, manifest, network, deterministic, lookahead)
            → copy to QOS_STRATEGIES_DIR/<name>@<version>/ (+ install.json) → ask the API to load and evaluate it (backtest + walk-forward)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "qos" / "sdk" / "_check.py"
SKIP = {".git", "__pycache__", ".venv", "manifest.json", "install.json", ".DS_Store", ".pytest_cache"}
DEFAULT_API = os.getenv("QOS_API", "http://127.0.0.1:8000")


class InstallError(RuntimeError):
    def __init__(self, msg: str, result: dict | None = None):
        super().__init__(msg)
        self.result = result or {}


def strategies_dir() -> Path:
    from qx_strategies.registry import strategies_dir as sd
    return sd()


def is_git(src: str) -> bool:
    return src.startswith(("git+", "git@")) or src.endswith(".git") or (src.startswith("https://") and not Path(src).exists())


def fetch(src: str, tmp: Path) -> Path:
    """Return the folder containing strategy.py (a folder, a single .py file or a git url[#subdir])"""
    if is_git(src):
        url, _, sub = src.removeprefix("git+").partition("#")
        dest = tmp / "repo"
        p = subprocess.run(["git", "clone", "--depth", "1", url, str(dest)], capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise InstallError(f"git clone failed: {(p.stderr or p.stdout).strip()[-300:]}")
        d = dest / sub if sub else dest
    else:
        path = Path(src).expanduser().resolve()
        if path.is_file() and path.suffix == ".py":
            d = tmp / "single"
            d.mkdir()
            shutil.copy2(path, d / "strategy.py")
        else:
            d = path
    if not (d / "strategy.py").exists():
        raise InstallError(f"no strategy.py in {d}")
    return d


def static_meta(path: Path) -> dict:
    """Read name / version / requires from strategy.py without importing it (needed before creating the venv)"""
    import ast
    out = {"name": None, "version": "1.0.0", "requires": []}
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as e:
        raise InstallError(f"strategy.py has a syntax error: {e}")
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any((isinstance(b, ast.Name) and b.id == "Strategy") or (isinstance(b, ast.Attribute) and b.attr == "Strategy") for b in node.bases):
            continue
        for st in node.body:
            targets = st.targets if isinstance(st, ast.Assign) else ([st.target] if isinstance(st, ast.AnnAssign) and st.value else [])
            for t in targets:
                if isinstance(t, ast.Name) and t.id in out:
                    try:
                        out[t.id] = ast.literal_eval(st.value)
                    except ValueError:
                        raise InstallError(f"{t.id} in strategy.py must be a literal value")
    if not isinstance(out["requires"], (list, tuple)) or not all(isinstance(r, str) for r in out["requires"]):
        raise InstallError("requires must be a list of strings, e.g. ['ta==0.11.0']")
    return out


def _uv() -> str:
    from qos import paths
    c = paths.uv()
    if c != "uv" or shutil.which("uv"):
        return c
    raise InstallError("uv is not installed — needed to create the package venv for `requires`")


def build_venv(dest: Path, requires: list[str], log=print) -> str:
    """Separate venv in <package>/.venv: sees the main env's packages (via an appended .pth) + installs requires pinned to the versions the main env has"""
    import sysconfig
    uv = _uv()
    venv = dest / ".venv"
    log(f"→ creating venv for requires: {', '.join(requires)}")
    p = subprocess.run([uv, "venv", "--quiet", "--python", sys.executable, str(venv)], capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise InstallError(f"uv venv failed: {(p.stderr or p.stdout).strip()[-300:]}")
    py = str(venv / "bin" / "python")
    site = subprocess.run([py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], capture_output=True, text=True).stdout.strip()
    main_site = sysconfig.get_paths()["purelib"]
    Path(site, "_qos_main_env.pth").write_text(f"import site; site.addsitedir({main_site!r})\n{ROOT}\n")
    freeze = subprocess.run([uv, "pip", "freeze", "--python", sys.executable], capture_output=True, text=True, timeout=120).stdout
    cons = dest / ".venv" / "qos-constraints.txt"
    cons.write_text("\n".join(ln for ln in freeze.splitlines() if "==" in ln and not ln.startswith("-e")) + "\n")
    p = subprocess.run([uv, "pip", "install", "--quiet", "--python", py, "-c", str(cons), *requires], capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise InstallError(f"installing requires failed: {(p.stderr or p.stdout).strip()[-500:]}")
    log(f"   ✓ venv {venv}")
    return py


def run_checks(path: Path, bars: str = "auto", days: int = 30, catalog: str | None = None, timeout: int = 600, python: str | None = None) -> dict:
    cmd = [python or sys.executable, "-I", str(CHECK), str(path), "--bars", bars, "--days", str(days)]
    if catalog:
        cmd += ["--catalog", catalog]
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "QOS_STRATEGIES_DIR": os.environ.get("QOS_STRATEGIES_DIR", str(strategies_dir()))})
    except subprocess.TimeoutExpired:
        return {"ok": False, "checks": [{"name": "import", "ok": False, "detail": f"checks timed out after {timeout}s"}], "failed": "timeout"}
    lines = (p.stdout or "").strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "checks": [], "failed": "internal", "error": ((p.stderr or "") + (p.stdout or ""))[-800:]}


def api_call(method: str, path: str, body: dict | None = None, api: str | None = DEFAULT_API, timeout: float = 30) -> dict | None:
    """Call the QOS API (None = API not running) · sends QOS_OPERATOR_TOKEN if set"""
    if not api:
        return None
    req = urllib.request.Request(api.rstrip("/") + path, method=method, data=json.dumps(body or {}).encode() if method != "GET" else None,
                                 headers={"Content-Type": "application/json", "X-Source": "qos-cli",
                                          **({"X-Operator-Token": os.environ["QOS_OPERATOR_TOKEN"]} if os.getenv("QOS_OPERATOR_TOKEN") else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = str(e)
        raise InstallError(f"API {method} {path} → {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(src: str, *, force: bool = False, evaluate: bool = True, bars: str = "auto", days: int = 30, catalog: str | None = None,
            api: str | None = DEFAULT_API, log=print) -> dict:
    base = strategies_dir()
    with tempfile.TemporaryDirectory(prefix="qos-install-") as t:
        tmp = Path(t)
        log(f"→ source: {src}")
        d = fetch(src, tmp)
        meta = static_meta(d / "strategy.py")
        python, staged, moved = None, None, None
        if meta["requires"]:                                  # step 22: the separate venv must be at its final location (venv paths are absolute)
            sref = f"{meta['name']}@{meta['version']}"
            staged = base / sref
            if staged.exists() and staged.resolve() != d.resolve():
                if not force:
                    raise InstallError(f"{sref} is already installed — bump `version` in strategy.py, or use --force to replace it")
                nodes = _nodes_using(sref, api)
                if nodes:
                    raise InstallError(f"{sref} is used by node(s) {', '.join(nodes)} — stop them or install a new version")
                moved = staged.with_name(sref + ".replaced")
                shutil.rmtree(moved, ignore_errors=True)
                staged.rename(moved)
            if staged.resolve() == d.resolve():
                staged = None
                python = build_venv(d, meta["requires"], log) if not (d / ".venv").exists() else str(d / ".venv" / "bin" / "python")
            else:
                staged.mkdir(parents=True, exist_ok=True)
                try:
                    python = build_venv(staged, meta["requires"], log)
                except Exception:
                    _rollback(staged, moved)
                    raise
        log("→ running checks (import · manifest · network · deterministic · lookahead) …")
        res = run_checks(d / "strategy.py", bars, days, catalog, python=python)
        for c in res.get("checks", []):
            log(f"   {'✓' if c['ok'] else '✕'} {c['name']:<13} {c['detail']}")
        if not res.get("ok"):
            if staged:
                _rollback(staged, moved)
            raise InstallError(f"check '{res.get('failed')}' failed" + (f": {res['error']}" if res.get("error") else ""), res)
        for w in res.get("warnings", []):
            log(f"   ! {w}")
        m = res["manifest"]
        ref = f"{m['name']}@{m['version']}"
        dest = base / ref
        same = dest.exists() and dest.resolve() == d.resolve()
        if staged is not None and staged != dest:
            _rollback(staged, moved)
            raise InstallError(f"name/version in strategy.py must be plain literals (read {staged.name}, manifest says {ref})", res)
        if staged is not None and staged == dest:              # venv already built: copy only the code
            shutil.copytree(d, dest, ignore=lambda _d, names: [n for n in names if n in SKIP], dirs_exist_ok=True)
            if moved:
                shutil.rmtree(moved, ignore_errors=True)
            same = True
        elif dest.exists() and not same:
            if not force:
                raise InstallError(f"{ref} is already installed — bump `version` in strategy.py, or use --force to replace it", res)
            nodes = _nodes_using(ref, api)
            if nodes:
                raise InstallError(f"{ref} is used by node(s) {', '.join(nodes)} — stop them or install a new version", res)
            shutil.rmtree(dest)
        if not same:
            base.mkdir(parents=True, exist_ok=True)
            shutil.copytree(d, dest, ignore=lambda _d, names: [n for n in names if n in SKIP])
        info = {"ref": ref, "source": src, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sha256": sha256(dest / "strategy.py"),
                "checks": res["checks"], "sample": res.get("sample"), "bars": bars}
        (dest / "install.json").write_text(json.dumps(info, indent=2, default=str))
        log(f"→ installed {ref} → {dest}")
    out = {"ref": ref, "path": str(dest), "checks": res["checks"], "manifest": m, "api": None, "evaluation": None}
    if not api:                                       # called from the API itself (Terminal → Install) — the API handles reload/evaluation
        return out
    r = api_call("POST", "/strategies/packages/reload", api=api)
    if r is None:
        log("! QOS API is not running — the package will be picked up when it starts; then run: qos evaluate " + ref)
        return out
    out["api"] = r
    if evaluate:
        ev = api_call("POST", f"/strategies/packages/{ref}/evaluate", api=api)
        out["evaluation"] = ev
        log(f"→ evaluation {ev.get('status', 'queued')}: backtest 90 days + walk-forward (see qos status / the Strategies tab)")
    return out


def _rollback(staged: Path, moved: Path | None) -> None:
    shutil.rmtree(staged, ignore_errors=True)
    if moved and moved.exists():
        moved.rename(staged)


def _nodes_using(ref: str, api: str | None) -> list[str]:
    if os.getenv("QOS_USED_REFS") is not None:       # the API passes the ref → node list (no callback needed)
        used = json.loads(os.environ["QOS_USED_REFS"] or "{}")
        return used.get(ref, [])
    try:
        r = api_call("GET", "/strategies/packages", api=api)
    except InstallError:
        r = None
    if r is not None:
        for p in r.get("packages", []):
            if p["ref"] == ref:
                return p.get("nodes", [])
        return []
    return _running_nodes_from_pidfiles(ref)          # API not reachable: nodes keep running on their own — check their pid files


def _proc_alive(pid: int, group: bool) -> bool:
    """pid (or process group) still has a live, non-zombie process"""
    try:
        (os.killpg if group else os.kill)(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if not Path("/proc").is_dir():
        return True
    try:                                              # Linux: ignore zombies (exited, not yet reaped)
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return True
    col = 1 if group else 0
    rows = [f for f in (ln.split() for ln in out.splitlines()) if len(f) >= 3 and f[col] == str(pid)]
    return any(not f[2].startswith("Z") for f in rows) if rows else True


def live_pidfile_nodes(ref: str | None = None) -> list[str]:
    """Nodes (data/nodes/*.pid) whose process — or anything left in its process group — is alive · optional: only those running ref
    (nodes keep running while the API is stopped, so this works without the API)"""
    from qos import paths
    out = []
    for pf in sorted((paths.data_dir() / "nodes").glob("*.pid")):
        try:
            info = json.loads(pf.read_text())
        except Exception:
            continue
        if ref is not None and info.get("strategy_ref") != ref:
            continue
        pid, pgid = int(info.get("pid") or 0), int(info.get("pgid") or 0)
        if (pgid and _proc_alive(pgid, True)) or (pid and _proc_alive(pid, False)):
            out.append(pf.stem)
    return out


def _running_nodes_from_pidfiles(ref: str) -> list[str]:
    """Nodes whose live process (pid file in data/nodes) runs this strategy ref — fails closed when the API is down"""
    return live_pidfile_nodes(ref)
