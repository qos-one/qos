"""Local admin token for a personal machine that has no operator yet.

The engine creates a random token at install and on first start, mode 0600, under the
QOS data directory (or QOS_LOCAL_TOKEN_FILE). The Terminal and the local CLI read that
file and send it as X-QOS-Local-Token. The value is not an environment variable and is
not written to .env, so the MCP server is not handed it.

Once an operator exists, sign-in is the operator PIN. This token does not replace it.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path

from qos import paths

HEADER = "X-QOS-Local-Token"
_FILE = "local_token"


def token_path() -> Path:
    override = os.getenv("QOS_LOCAL_TOKEN_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return paths.data_dir() / _FILE


def _mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _tighten(path: Path) -> None:
    if _mode(path) == 0o600:
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_token(path: Path | None = None) -> str | None:
    """Return the token, or None when the file is missing or empty. Does not create it."""
    p = path or token_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    token = raw.strip()
    if not token or any(c in token for c in "\r\n\x00"):
        return None
    _tighten(p)
    return token


def ensure_local_token(path: Path | None = None) -> str:
    """Create the token if this install does not have one. Never rotates an existing token."""
    p = path or token_path()
    current = read_token(p)
    if current:
        return current
    p.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    payload = (token + "\n").encode("ascii")
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        current = read_token(p)
        if current:
            return current
        fd = os.open(p, os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    _tighten(p)
    return read_token(p) or token


def matches(presented: str | None, path: Path | None = None) -> bool:
    """Constant-time check. Missing file, empty header, and a wrong value all fail."""
    expected = read_token(path) or ""
    if isinstance(presented, str) and len(presented) <= 256:
        got = presented.strip()
    else:
        got = ""
    ok = secrets.compare_digest(hashlib.sha256(got.encode()).digest(), hashlib.sha256(expected.encode()).digest())
    return bool(expected) and bool(got) and ok


def client_headers() -> dict[str, str]:
    """Header a local CLI sends. Empty when this machine has no token file yet."""
    token = read_token()
    return {HEADER: token} if token else {}
