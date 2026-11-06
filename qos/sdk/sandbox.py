"""
Restricts file writes by strategy code (step 22) — writes are allowed only in the package's own folder and data/strategy_state

Two layers:
  scope(allowed)        the SDK adapter wraps every hook (on_start / on_bar / on_timer / on_stop) — works in both backtest and live
  install_global(allowed)  nautilus_runner installs it process-wide after building the node (checks again, even for writes outside hooks, e.g. in a thread)

Wraps builtins.open / io.open / os.open / os.remove / os.unlink / os.rename / os.replace / os.mkdir / os.rmdir / shutil.rmtree
Reads are not restricted · writing outside the allowed paths = PermissionError
Limitation: C extension code that makes syscalls directly bypasses this wrapper (full protection needs an OS-level sandbox)
"""
from __future__ import annotations

import builtins
import contextlib
import io
import os
import shutil
import threading
from pathlib import Path

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_lock = threading.RLock()
_installed = False
_global: list[str] | None = None
_scopes: list[list[str]] = []
_orig: dict = {}
blocked: list[str] = []                     # recorded attempts (last 50)


def _norm(p) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(p))))


def _allowed_now() -> list[str] | None:
    if _scopes:
        return _scopes[-1]
    return _global


def check_write(path) -> None:
    allowed = _allowed_now()
    if allowed is None or isinstance(path, int):
        return
    try:
        target = _norm(path)
    except TypeError:
        return
    if target == os.devnull or target.startswith("/dev/"):
        return
    for a in allowed:
        if target == a or target.startswith(a.rstrip(os.sep) + os.sep):
            return
    blocked.append(target)
    del blocked[:-50]
    raise PermissionError(f"QOS sandbox: strategy code may only write inside its package folder or data/strategy_state — denied: {target}")


def _is_write_mode(mode) -> bool:
    return any(c in str(mode) for c in "wax+")


def _wrap_open(orig):
    def _open(file, mode="r", *a, **k):
        if _is_write_mode(mode):
            check_write(file)
        return orig(file, mode, *a, **k)
    _open.__qos_sandbox__ = True
    return _open


def _wrap_os_open(orig):
    def _os_open(path, flags, *a, **k):
        if flags & _WRITE_FLAGS and k.get("dir_fd") is None:
            check_write(path)
        return orig(path, flags, *a, **k)
    return _os_open


def _wrap_path1(orig):
    def f(path, *a, **k):
        if k.get("dir_fd") is None:
            check_write(path)
        return orig(path, *a, **k)
    return f


def _wrap_path2(orig):
    def f(src, dst, *a, **k):
        check_write(src)
        check_write(dst)
        return orig(src, dst, *a, **k)
    return f


def _install() -> None:
    global _installed
    if _installed:
        return
    _orig.update(open=builtins.open, io_open=io.open, os_open=os.open, remove=os.remove, unlink=os.unlink, rename=os.rename,
                 replace=os.replace, mkdir=os.mkdir, rmdir=os.rmdir, rmtree=shutil.rmtree)
    builtins.open = _wrap_open(_orig["open"])
    io.open = _wrap_open(_orig["io_open"])
    os.open = _wrap_os_open(_orig["os_open"])
    os.remove, os.unlink, os.mkdir, os.rmdir = (_wrap_path1(_orig[k]) for k in ("remove", "unlink", "mkdir", "rmdir"))
    os.rename, os.replace = _wrap_path2(_orig["rename"]), _wrap_path2(_orig["replace"])
    shutil.rmtree = _wrap_path1(_orig["rmtree"])
    _installed = True


def uninstall() -> None:
    """Restore the original functions (used in tests)"""
    global _installed, _global
    with _lock:
        if not _installed:
            return
        builtins.open, io.open, os.open = _orig["open"], _orig["io_open"], _orig["os_open"]
        os.remove, os.unlink, os.rename, os.replace = _orig["remove"], _orig["unlink"], _orig["rename"], _orig["replace"]
        os.mkdir, os.rmdir, shutil.rmtree = _orig["mkdir"], _orig["rmdir"], _orig["rmtree"]
        _installed, _global = False, None
        _scopes.clear()


def install_global(allowed: list) -> None:
    global _global
    with _lock:
        _install()
        _global = [_norm(a) for a in allowed if a]


@contextlib.contextmanager
def scope(allowed: list):
    with _lock:
        _install()
        _scopes.append([_norm(a) for a in allowed if a])
    try:
        yield
    finally:
        with _lock:
            if _scopes:
                _scopes.pop()


def default_allowed(package_dir: str | Path | None, state_file: str | Path | None) -> list[str]:
    out = []
    if package_dir:
        out.append(str(package_dir))
    if state_file:
        out.append(str(Path(state_file).parent))
    return out
