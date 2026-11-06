"""Hold new entries of a node until an operator confirms (step 15: positions mismatch after sleep/restart) — data/holds.json"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from qos import paths


def _file():
    return paths.data_dir() / "holds.json"


def load() -> dict:
    try:
        return json.loads(_file().read_text())
    except Exception:
        return {}


def _save(doc: dict) -> None:
    _file().parent.mkdir(parents=True, exist_ok=True)
    _file().write_text(json.dumps(doc, indent=1))


def add(node: str, reason: str) -> bool:
    doc = load()
    if node in doc:
        return False
    doc[node] = {"reason": reason, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _save(doc)
    return True


def release(node: str) -> bool:
    doc = load()
    if doc.pop(node, None) is None:
        return False
    _save(doc)
    return True


def payload() -> dict:
    """For qx:risk → node: {node: reason}"""
    return {k: v["reason"] for k, v in load().items()}
