"""
Check an Agent before publishing on QOS Cloud (step 36) — used both by the Cloud worker and `qos publish --check` on a developer machine

  python -m qos.publish_check <package-folder> [--catalog DIR] [--days 90] [--wf-days 365] [--scan-only] [--json]

1. scan     static code check (AST): no network / process / ctypes / eval-exec / env reads / dunder escapes,
            allowed file types and sizes, no requires (marketplace v1)
2. checks   the same checks as qos install (import · manifest · network · deterministic · lookahead)
3. backtest with default parameters over `--days` days on Cloud data
4. walkforward with the package grid over `--wf-days` days
Prints JSON on the last line: {"ok", "stage", "meta", "scan", "checks", "backtest", "walkforward", "seconds"}
ok is true only when every stage passed, including the backtest (net > 0, DD ≤ cap) and walk-forward (≥ 60% positive folds) criteria
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
ALLOWED_EXT = {".py", ".json", ".toml", ".yaml", ".yml", ".md", ".txt", ".csv", ".joblib", ".parquet"}
SKIP_DIRS = {"__pycache__", ".venv", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "tests"}   # tests/ is not uploaded to the Cloud
MAX_FILES, MAX_BYTES, MAX_PY_BYTES, MAX_LITERAL = 200, 50 * 1024 * 1024, 1024 * 1024, 20_000

BANNED_MODULES = {
    "socket", "ssl", "select", "selectors", "asyncio.subprocess", "subprocess", "multiprocessing", "concurrent.futures.process",
    "ctypes", "cffi", "mmap", "urllib", "http", "requests", "httpx", "aiohttp", "websockets", "websocket", "ftplib", "smtplib",
    "poplib", "imaplib", "telnetlib", "xmlrpc", "paramiko", "importlib", "runpy", "code", "codeop", "pty", "pickle", "marshal",
    "shelve", "dill", "cloudpickle", "builtins", "sys", "gc", "inspect", "resource", "signal", "webbrowser", "ib_async",
    "ibapi", "nautilus_trader.adapters", "qos.cloud", "keyring", "tests",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint", "input", "globals", "vars", "getattr", "setattr", "delattr"}
BANNED_OS = {"system", "popen", "fork", "forkpty", "kill", "killpg", "environ", "getenv", "putenv", "unsetenv", "environb",
             "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp",
             "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp", "startfile", "chmod", "chown",
             "symlink", "link", "remove", "unlink", "rmdir", "removedirs", "rename", "replace"}
BANNED_ATTRS = {"__subclasses__", "__globals__", "__builtins__", "__code__", "__closure__", "__bases__", "__mro__", "__base__",
                "__getattribute__", "__dict__", "__loader__", "__spec__", "f_globals", "f_locals", "f_back", "gi_frame", "tb_frame"}


def _banned_module(name: str) -> str | None:
    for b in BANNED_MODULES:
        if name == b or name.startswith(b + "."):
            return b
    return None


def _issue(level: str, file: str, line: int, msg: str) -> dict:
    return {"level": level, "file": file, "line": line, "message": msg}


def scan_source(rel: str, src: str) -> list[dict]:
    out: list[dict] = []
    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError as e:
        return [_issue("error", rel, e.lineno or 0, f"syntax error: {e.msg}")]
    os_aliases = {"os"}
    for node in ast.walk(tree):
        ln = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for a in node.names:
                if b := _banned_module(a.name):
                    out.append(_issue("error", rel, ln, f"import of '{a.name}' is not allowed ({b})"))
                if a.name == "os" and a.asname:
                    os_aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and (b := _banned_module(mod)):
                out.append(_issue("error", rel, ln, f"import from '{mod}' is not allowed ({b})"))
            for a in node.names:
                if node.level == 0 and (b := _banned_module(f"{mod}.{a.name}")):
                    out.append(_issue("error", rel, ln, f"import of '{mod}.{a.name}' is not allowed ({b})"))
                if mod == "os" and a.name in BANNED_OS:
                    out.append(_issue("error", rel, ln, f"os.{a.name} is not allowed"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            out.append(_issue("error", rel, ln, f"{node.func.id}() is not allowed"))
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRS:
                out.append(_issue("error", rel, ln, f"access to {node.attr} is not allowed"))
            if isinstance(node.value, ast.Name) and node.value.id in os_aliases and node.attr in BANNED_OS:
                out.append(_issue("error", rel, ln, f"os.{node.attr} is not allowed"))
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__loader__", "__spec__"):
            out.append(_issue("error", rel, ln, f"{node.id} is not allowed"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and len(node.value) > MAX_LITERAL:
            out.append(_issue("error", rel, ln, f"literal of {len(node.value):,} characters (embedded data or obfuscated code)"))
    return out


PICKLE_OK = ("sklearn.", "numpy.", "numpy", "scipy.", "joblib.numpy_pickle", "collections.OrderedDict", "collections.defaultdict",
             "copyreg._reconstructor", "builtins.object", "builtins.set", "builtins.frozenset", "builtins.slice", "builtins.complex",
             "builtins.bytearray", "_codecs.encode", "pandas.", "lightgbm.", "xgboost.")


def pickle_globals(path: Path) -> list[str] | None:
    """module.name values that a pickle/joblib file will import when loaded (None = unreadable → needs human review)"""
    import pickletools
    import zlib
    raw = path.read_bytes()
    if raw[:1] == b"\x78" and raw[1:2] in (b"\x01", b"\x5e", b"\x9c", b"\xda"):
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            return None
    found, vals, memo = [], [], {}
    try:
        for op, arg, _pos in pickletools.genops(raw):
            n = op.name
            if n in ("SHORT_BINUNICODE", "BINUNICODE", "UNICODE", "BINUNICODE8", "SHORT_BINSTRING", "BINSTRING", "STRING"):
                vals.append(arg)
            elif n in ("BINGET", "LONG_BINGET", "GET"):
                vals.append(memo.get(int(arg)))
            elif n == "MEMOIZE":
                memo[len(memo)] = vals[-1] if vals else None
            elif n in ("BINPUT", "LONG_BINPUT", "PUT"):
                memo[int(arg)] = vals[-1] if vals else None
            elif n == "GLOBAL":
                found.append(str(arg).replace(" ", "."))
            elif n == "STACK_GLOBAL":
                found.append(f"{vals[-2]}.{vals[-1]}" if len(vals) >= 2 else "?")
            elif n == "STOP":
                break
    except Exception:
        return sorted(set(found)) or None
    return sorted(set(found))


def walk_files(root: Path) -> list[Path]:
    files = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or p.suffix == ".pyc":
            continue
        if p.is_file() or p.is_symlink():
            files.append(p)
    return files


def static_meta(root: Path) -> dict:
    from qos.install import InstallError, static_meta as sm
    try:
        return sm(root / "strategy.py")
    except InstallError as e:
        return {"error": str(e)}


def scan(root: Path, expect_name: str | None = None, expect_version: str | None = None) -> dict:
    root = Path(root)
    issues: list[dict] = []
    if not (root / "strategy.py").is_file():
        return {"ok": False, "issues": [_issue("error", "strategy.py", 0, "strategy.py is missing")], "files": 0, "bytes": 0}
    files = walk_files(root)
    total = 0
    for p in files:
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            issues.append(_issue("error", rel, 0, "symlinks are not allowed"))
            continue
        size = p.stat().st_size
        total += size
        if p.suffix.lower() not in ALLOWED_EXT:
            issues.append(_issue("error", rel, 0, f"file type {p.suffix or '(none)'} is not allowed"))
        elif p.suffix == ".py":
            if size > MAX_PY_BYTES:
                issues.append(_issue("error", rel, 0, "Python file larger than 1 MB"))
            else:
                issues.extend(scan_source(rel, p.read_text(errors="replace")))
        elif p.suffix == ".joblib":
            g = pickle_globals(p)
            if g is None:
                issues.append(_issue("warn", rel, 0, "model file could not be inspected — reviewed manually"))
            else:
                bad = [x for x in g if not x.startswith(PICKLE_OK)]
                if bad:
                    issues.append(_issue("error", rel, 0, f"model file imports {', '.join(bad[:5])} when loaded (only sklearn/numpy/scipy/pandas objects are allowed)"))
                else:
                    issues.append(_issue("warn", rel, 0, f"model file uses {len(g)} sklearn/numpy types — checked automatically"))
    if len(files) > MAX_FILES:
        issues.append(_issue("error", ".", 0, f"{len(files)} files (max {MAX_FILES})"))
    if total > MAX_BYTES:
        issues.append(_issue("error", ".", 0, f"{total / 1e6:.1f} MB (max {MAX_BYTES / 1e6:.0f} MB)"))
    meta = static_meta(root)
    if meta.get("error"):
        issues.append(_issue("error", "strategy.py", 0, meta["error"]))
    else:
        if not meta.get("name") or not NAME_RE.match(str(meta["name"])):
            issues.append(_issue("error", "strategy.py", 0, "name must be lower_snake_case, 2–32 characters"))
        if not VERSION_RE.match(str(meta.get("version"))):
            issues.append(_issue("error", "strategy.py", 0, "version must look like 1.2.3"))
        if meta.get("requires"):
            issues.append(_issue("error", "strategy.py", 0, "requires is not supported for marketplace agents yet "
                                                              "(the review sandbox has no network)"))
        if expect_name and meta.get("name") != expect_name:
            issues.append(_issue("error", "strategy.py", 0, f"name is '{meta.get('name')}' but you are publishing '{expect_name}'"))
        if expect_version and meta.get("version") != expect_version:
            issues.append(_issue("error", "strategy.py", 0, f"version is '{meta.get('version')}' but you are publishing '{expect_version}'"))
    return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues, "files": len(files), "bytes": total,
            "name": meta.get("name"), "version": meta.get("version")}


def _evaluate(pkg: Path, manifest: dict, days: int, wf_days: int, log) -> tuple[dict, dict]:
    import pandas as pd
    from qx_strategies.backtest import NoBarsError, run_backtest
    from qx_strategies.registry import REG
    from qx_strategies.walkforward import walk_forward
    REG.scan(force=True)
    entry = REG.resolve(f"{manifest['name']}@{manifest['version']}")
    if entry.status != "ok":
        raise RuntimeError(f"package cannot be loaded: {entry.error}")
    m = entry.manifest
    cfg = {"strategy": entry.ref, "strategy_id": f"QOS-{entry.name}-{entry.version}".replace("_", "-"),
           "tf_minutes": int(m.get("tf_minutes") or 5), "trade_size": "20000"}
    dd_cap = float(m.get("max_drawdown_pct") or 10.0)
    end = pd.Timestamp.now(tz="UTC").floor("min").tz_localize(None)
    start = end - pd.Timedelta(days=days)
    res, errors = None, []
    for inst in m.get("instruments") or []:
        c = {**cfg, "instrument": inst}
        try:
            res = run_backtest(c, start=str(start), end=str(end), log_level="OFF")
            cfg = c
            break
        except NoBarsError as e:                                   # try the next instrument; other errors (no USD rate) are reported
            errors.append(f"{inst}: {e}")
    if res is None:
        raise RuntimeError("no data for any instrument of the agent — " + "; ".join(errors))
    dd_pct = res["max_drawdown_usd"] / 100_000 * 100
    bt = {"ok": res["pnl_usd"] > 0 and dd_pct <= dd_cap, "instrument": cfg["instrument"], "days": days,
          "pnl_usd": round(res["pnl_usd"], 2), "trades": res["closed_positions"], "win_rate": res.get("win_rate"),
          "max_drawdown_usd": round(res["max_drawdown_usd"], 2), "max_drawdown_pct": round(dd_pct, 3), "dd_cap_pct": dd_cap,
          "from": str(res.get("from")), "to": str(res.get("to"))}
    log(f"backtest {cfg['instrument']} {days}d: pnl {bt['pnl_usd']:,.2f} · trades {bt['trades']} · DD {dd_pct:.2f}%")
    grid = m.get("grid") or {"trade_size": [cfg["trade_size"]]}
    try:
        r = walk_forward(cfg, str(end - pd.Timedelta(days=wf_days)), str(end), 90, 30, grid, workers=1, progress=lambda *_: None)
        o = r["oos"]
        share = o["positive_folds"] / o["folds"] if o["folds"] else 0.0
        wf = {"ok": share >= 0.6, "folds": o["folds"], "positive_folds": o["positive_folds"], "share": round(share, 3),
              "pnl_usd": round(o["pnl_usd"], 2), "trades": o["trades"], "max_drawdown_usd": round(o["max_drawdown_usd"], 2),
              "days": wf_days}
    except ValueError as e:
        wf = {"ok": False, "error": str(e), "days": wf_days}
    log(f"walk-forward {wf_days}d: {wf.get('positive_folds', 0)}/{wf.get('folds', 0)} positive folds")
    return bt, wf


def run(pkg: str | Path, catalog: str | None = None, days: int = 90, wf_days: int = 365, scan_only: bool = False,
        expect_name: str | None = None, expect_version: str | None = None, log=lambda *_: None) -> dict:
    t0 = time.time()
    pkg = Path(pkg).resolve()
    out: dict = {"ok": False, "stage": "scan", "meta": {}, "scan": None, "checks": None, "backtest": None, "walkforward": None}
    out["scan"] = sc = scan(pkg, expect_name, expect_version)
    log(f"scan: {'OK' if sc['ok'] else 'FAILED'} ({sc['files']} files, {len(sc['issues'])} issues)")
    if not sc["ok"] or scan_only:
        out.update(ok=sc["ok"], seconds=round(time.time() - t0, 1))
        return out
    if catalog:
        os.environ["QOS_CATALOG"] = str(catalog)
    with tempfile.TemporaryDirectory(prefix="qos-publish-") as td:
        sdir = Path(td) / "strategies"
        dest = sdir / f"{sc['name']}@{sc['version']}"
        shutil.copytree(pkg, dest, ignore=lambda _d, names: [n for n in names if n in SKIP_DIRS or n.endswith(".pyc")])
        os.environ["QOS_STRATEGIES_DIR"] = str(sdir)
        os.environ["QOS_REPORTS_DIR"] = str(Path(td) / "reports")
        from qos.install import run_checks
        out["stage"] = "checks"
        ch = run_checks(dest / "strategy.py", "catalog" if catalog else "synthetic", 30, catalog)
        out["checks"] = ch
        man = ch.get("manifest") or {}
        out["meta"] = {k: man.get(k) for k in ("name", "version", "label", "description", "instruments", "timeframe", "tf_minutes",
                                               "params", "grid", "summary", "max_drawdown_pct", "limits")}
        log(f"checks: {'OK' if ch.get('ok') else 'FAILED at ' + str(ch.get('failed'))}")
        if not ch.get("ok"):
            out["seconds"] = round(time.time() - t0, 1)
            return out
        out["stage"] = "backtest"
        try:
            out["backtest"], out["walkforward"] = _evaluate(dest, man, days, wf_days, log)
            if not out["backtest"].get("ok"):                     # evaluation ran but the agent misses the criteria
                out["stage"] = "backtest"
                out["error"] = (f"backtest criteria not met: net {out['backtest']['pnl_usd']:+,.2f} USD (needs > 0), "
                                f"max DD {out['backtest']['max_drawdown_pct']:.2f}% (cap {out['backtest']['dd_cap_pct']:g}%)")
            elif not out["walkforward"].get("ok"):
                out["stage"] = "walkforward"
                wf = out["walkforward"]
                out["error"] = wf.get("error") or (f"walk-forward criteria not met: {wf.get('positive_folds', 0)}/{wf.get('folds', 0)} "
                                                   "positive folds (needs ≥ 60%)")
            else:
                out["stage"] = "done"
                out["ok"] = True
        except Exception as e:
            out["error"] = str(e)[:500]
            log(f"evaluation failed: {e}")
    out["seconds"] = round(time.time() - t0, 1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m qos.publish_check")
    ap.add_argument("package")
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--wf-days", type=int, default=365)
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--name", default=None)
    ap.add_argument("--version", default=None)
    ap.add_argument("--json", action="store_true", help="only print the JSON result line")
    a = ap.parse_args(argv)
    log = (lambda *_: None) if a.json else (lambda s: print(s, flush=True))
    r = run(a.package, a.catalog, a.days, a.wf_days, a.scan_only, a.name, a.version, log)
    print(json.dumps(r, default=str), flush=True)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
