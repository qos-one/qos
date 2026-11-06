"""Operators + roles + session tokens — separate from the IBKR login
roles: admin (everything) · trader (orders/agents) · risk (risk settings, kill switch, no orders) · viewer (read-only)"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

DATA_DIR = paths.data_dir()
OPS_FILE = DATA_DIR / "operators.json"
ROLES = ("admin", "trader", "risk", "viewer", "assistant")   # assistant = LLM via MCP (limited rights + confirmation required)
IDLE_SEC = 30 * 60          # auto-lock after 30 minutes idle
MAX_SEC = 12 * 3600         # maximum session lifetime
_tokens: dict[str, dict] = {}


def public_mode() -> bool:
    """QOS_PUBLIC_TERMINAL=1: the Terminal is served on a public domain (deploy/README, option B). Then no legacy
    no-operator admin, no web bootstrap, longer PINs and a global login-failure limit."""
    return os.getenv("QOS_PUBLIC_TERMINAL", "") == "1"


def min_pin() -> int:
    return 8 if public_mode() else 4
current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("current_operator", default=None)


def _load() -> dict:
    try:
        return json.loads(OPS_FILE.read_text())
    except Exception:
        return {"operators": [], "security": {}}


def _save(doc: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OPS_FILE.write_text(json.dumps(doc, indent=2))
    try:
        os.chmod(OPS_FILE, 0o600)
    except Exception:
        pass


def _hash(secret: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 200_000).hex()


def configured() -> bool:
    return any(o.get("active", True) for o in _load()["operators"])


def list_ops() -> list[dict]:
    return [{k: v for k, v in o.items() if k not in ("pin_hash", "salt")} for o in _load()["operators"]]


def create(name: str, role: str, pin: str, created_by: str = "") -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if len(pin) < min_pin():
        raise ValueError(f"PIN must be at least {min_pin()} characters")
    doc = _load()
    oid = name.strip().lower().replace(" ", "_")
    if any(o["id"] == oid for o in doc["operators"]):
        raise ValueError(f"operator '{oid}' already exists")
    salt = secrets.token_hex(8)
    op = {"id": oid, "name": name.strip(), "role": role, "salt": salt, "pin_hash": _hash(pin, salt),
          "active": True, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "created_by": created_by}
    doc["operators"].append(op)
    _save(doc)
    return {k: v for k, v in op.items() if k not in ("pin_hash", "salt")}


def update(oid: str, role: str | None = None, active: bool | None = None, pin: str | None = None) -> dict:
    doc = _load()
    for o in doc["operators"]:
        if o["id"] == oid:
            if role is not None:
                if role not in ROLES:
                    raise ValueError("bad role")
                o["role"] = role
            if active is not None:
                o["active"] = active
                if not active:
                    for t, sess in list(_tokens.items()):
                        if sess["id"] == oid:
                            _tokens.pop(t, None)
            if pin:
                if len(pin) < min_pin():
                    raise ValueError(f"PIN must be at least {min_pin()} characters")
                o["salt"] = secrets.token_hex(8); o["pin_hash"] = _hash(pin, o["salt"])
            _save(doc)
            return {k: v for k, v in o.items() if k not in ("pin_hash", "salt")}
    raise KeyError(oid)


_fail: dict[str, list[float]] = {}
_fail_all: list[float] = []
FAIL_ALL_MAX = 20           # failed logins across all operators per 10 minutes (stops guessing by rotating operator ids)


def login(oid: str, pin: str) -> tuple[str, dict]:
    now = time.time()
    fails = [t for t in _fail.get(oid, []) if now - t < 600]
    _fail_all[:] = [t for t in _fail_all if now - t < 600]
    if len(fails) >= 5 or len(_fail_all) >= FAIL_ALL_MAX:
        raise PermissionError("too many failed attempts — wait 10 minutes")
    doc = _load()
    for o in doc["operators"]:
        if o["id"] == oid and o.get("active", True) and secrets.compare_digest(_hash(pin, o["salt"]), o["pin_hash"]):
            _fail.pop(oid, None)
            tok = secrets.token_urlsafe(32)
            _tokens[tok] = {"id": o["id"], "name": o["name"], "role": o["role"], "start": now, "last": now}
            return tok, _tokens[tok]
    _fail.setdefault(oid, []).append(now)
    _fail_all.append(now)
    raise PermissionError("invalid operator or PIN")


def logout(token: str) -> None:
    _tokens.pop(token, None)


def resolve(token: str | None) -> dict | None:
    """Return the token's operator (extends idle) or None"""
    if not token:
        return None
    sess = _tokens.get(token)
    if not sess:
        return None
    now = time.time()
    if now - sess["last"] > IDLE_SEC or now - sess["start"] > MAX_SEC:
        _tokens.pop(token, None)
        return None
    sess["last"] = now
    return sess


# ---------- live-trading confirmation code (second layer for entering Live) ----------
def set_live_code(code: str) -> None:
    if len(code) < 6:
        raise ValueError("live code must be at least 6 characters")
    doc = _load()
    salt = secrets.token_hex(8)
    doc.setdefault("security", {})["live_code"] = {"salt": salt, "hash": _hash(code, salt),
                                                   "set": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _save(doc)


def live_code_set() -> bool:
    return bool(_load().get("security", {}).get("live_code"))


def check_live_code(code: str) -> bool:
    lc = _load().get("security", {}).get("live_code")
    if not lc or not code:
        return False
    return secrets.compare_digest(_hash(code, lc["salt"]), lc["hash"])


# ---------- permissions ----------
ASSISTANT_WRITE_EXACT = ("/order/prepare", "/order/confirm")    # orders only via prepare → confirm (no POST /order, no PUT /order/{id} modify)
ASSISTANT_WRITE = ("/positions/", "/alerts", "/watchlist", "/ui", "/history/trades")   # no /setup/account, no /mode: human-only
ASSISTANT_DENY_EXACT = ("/positions/close_all",)                 # the LLM may not close all positions
ASSISTANT_DENY_SUFFIX = ("/reverse",)                             # no reversing a position in one go (/positions/{key}/reverse)
_ORDER_CANCEL = re.compile(r"^/order/\d+/cancel$")               # cancelling a working order is allowed (it only reduces risk)


def assistant_allowed(method: str, path: str) -> bool:
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    path = path.rstrip("/") or "/"
    if path in ASSISTANT_WRITE_EXACT:
        return method == "POST"
    if path == "/order" or path.startswith("/order/"):   # direct submit / modify (PUT /order/{id}) bypass the confirm step and the assistant caps
        return method == "POST" and bool(_ORDER_CANCEL.match(path))
    if path in ASSISTANT_DENY_EXACT or any(path.endswith(d) for d in ASSISTANT_DENY_SUFFIX):
        return False
    return any(path == a or path.startswith(a) for a in ASSISTANT_WRITE)


WRITE_RULES = [   # (prefix, roles allowed to write)
    ("/setup", ("admin",)), ("/operators", ("admin",)), ("/security", ("admin",)), ("/assistant", ("admin",)),
    ("/risk/kill", ("admin", "risk", "trader")),
    ("/risk", ("admin", "risk")), ("/notify", ("admin", "risk")), ("/calendar/pause", ("admin", "risk")),
    ("/ui", ("admin", "risk", "trader", "viewer")), ("/alerts", ("admin", "risk", "trader", "viewer")),
    ("/watchlist", ("admin", "risk", "trader", "viewer")), ("/history/trades", ("admin", "risk", "trader")),
]
PUBLIC = ("/health", "/operators/login", "/operators/bootstrap", "/operators/status", "/operators/logout", "/setup/status", "/ws", "/docs", "/openapi.json")


def allowed(role: str, method: str, path: str) -> bool:
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if role == "assistant":
        return assistant_allowed(method, path)
    for prefix, roles in WRITE_RULES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return role in roles
    return role in ("admin", "trader")        # orders, positions, mode, agents, sync, etc.
