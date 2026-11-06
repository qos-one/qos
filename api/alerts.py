"""Price alerts: stored in data/alerts.json and checked against quotes every second"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

FILE = paths.data_dir() / "alerts.json"


def load() -> list[dict]:
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return []


def save(items: list[dict]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(items, indent=2))


def add(key: str, kind: str, level: float, note: str = "", repeat: bool = False) -> dict:
    """kind: above | below | change_pct (|daily change| >= level)"""
    items = load()
    a = {"id": f"a{int(time.time() * 1000)}{secrets.token_hex(2)}", "key": key, "kind": kind, "level": float(level), "note": note,
         "repeat": repeat, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "triggered": None, "armed": True}
    items.append(a)
    save(items)
    return a


def remove(alert_id: str) -> None:
    save([a for a in load() if a["id"] != alert_id])


REPEAT_COOLDOWN_S = 300          # a repeating alert fires at most once per cool-down …
REARM_BAND = 0.001               # … and only after the price has left the level by this fraction (hysteresis)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v                 # NaN → None


def _hit(kind: str, level: float, price: float, change: float | None) -> bool:
    if kind == "above":
        return price >= level
    if kind == "below":
        return price <= level
    if kind == "change_pct":
        return change is not None and abs(change) >= level
    return False


def _left_level(kind: str, level: float, price: float, change: float | None) -> bool:
    if kind == "above":
        return price < level * (1 - REARM_BAND)
    if kind == "below":
        return price > level * (1 + REARM_BAND)
    if kind == "change_pct":
        return change is not None and abs(change) < level * 0.9
    return False


def check(quotes: list[dict], now: float | None = None) -> list[dict]:
    """Return alerts that just triggered (and save their state)
    one-shot: fires once, then disarmed · repeat: disarmed after firing, re-armed once the price has left the level AND the
    cool-down has passed (no message every second while the price stays beyond the level)"""
    items = load()
    if not items:
        return []
    now = time.time() if now is None else now
    by_key = {q.get("key"): q for q in quotes}
    fired = []
    changed = False
    for a in items:
        q = by_key.get(a.get("key"))
        if not q:
            continue
        price = _num(q.get("mid")) or _num(q.get("last")) or 0.0
        if not price:
            continue
        change = _num(q.get("change_pct"))
        level = float(a.get("level") or 0.0)
        if not a.get("armed", True):
            if a.get("repeat") and now - float(a.get("fired_at") or 0.0) >= REPEAT_COOLDOWN_S \
                    and _left_level(a["kind"], level, price, change):
                a["armed"] = True
                changed = True
            continue
        if _hit(a["kind"], level, price, change):
            a["triggered"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            a["fired_at"] = now
            a["armed"] = False
            a["last_price"] = price
            fired.append(dict(a))
            changed = True
    if changed:
        save(items)
    return fired
