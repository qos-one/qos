"""Where the refresh token lives: macOS Keychain (default on macOS) or a 0600 file (Linux / tests).

Only the refresh token is persisted. Access tokens stay in memory, so strategy code (which can read
files under data/) never sees a usable credential on disk.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SERVICE = "qos-cloud"


class KeychainStore:
    kind = "keychain"

    def __init__(self):
        import keyring
        self._k = keyring

    def get(self, account: str) -> str | None:
        return self._k.get_password(SERVICE, account)

    def set(self, account: str, value: str) -> None:
        self._k.set_password(SERVICE, account, value)

    def delete(self, account: str) -> None:
        try:
            self._k.delete_password(SERVICE, account)
        except Exception:
            pass


class FileStore:
    kind = "file"

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)

    def get(self, account: str) -> str | None:
        return self._read().get(account)

    def set(self, account: str, value: str) -> None:
        d = self._read()
        d[account] = value
        self._write(d)

    def delete(self, account: str) -> None:
        d = self._read()
        if d.pop(account, None) is not None:
            self._write(d)


def default_store(cloud_dir: Path):
    kind = os.getenv("QOS_CLOUD_TOKEN_STORE") or ("keychain" if sys.platform == "darwin" else "file")
    if kind == "keychain":
        return KeychainStore()
    return FileStore(cloud_dir / ".tokens.json")
