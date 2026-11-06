"""Shared Nautilus node helpers: IB clientId, selected gateway, instrument names, log location (processes are managed by api.node_manager)"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = paths.logs_dir()
CLIENTS_FILE = paths.data_dir() / "nautilus_clients.json"
SESSION_FILE = paths.data_dir() / "session.json"

# ---------- IB clientId: each node uses 2 (data = base, exec = base + 1) ----------
# Nautilus range: 100–199 (even/odd pairs), no clash with the API (60) and older scripts using numbers below 100
NODE_ID_MIN, NODE_ID_MAX = 100, 198
RESERVED_IDS = {11, 12, 55, 56, 57, 58, 60, 76, 77, 96, 98, 99}   # main.py, scripts/*, catalog (58), API, close_all, test_*


def _reserved() -> set[int]:
    r = set(RESERVED_IDS)
    try:
        r.add(int(os.getenv("IB_API_CLIENT_ID", "60")))
    except ValueError:
        pass
    return r


def _load_clients() -> dict[str, int]:
    try:
        return {k: int(v) for k, v in json.loads(CLIENTS_FILE.read_text()).items()}
    except Exception:
        return {}


def _save_clients(d: dict[str, int]) -> None:
    CLIENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLIENTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True))
    tmp.replace(CLIENTS_FILE)                                   # atomic: a reader never sees a half-written file


_clients_lock = threading.Lock()


@contextlib.contextmanager
def _clients_locked():
    """Serialise read-modify-write of nautilus_clients.json: threads (API requests + supervisor) and other processes (flock)"""
    with _clients_lock:
        CLIENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        f = open(CLIENTS_FILE.with_suffix(".lock"), "a")
        try:
            try:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            f.close()                                           # closing releases the flock


def allocate_client_id(node_id: str, requested: int = 0) -> int:
    """Return a node's base clientId (exec uses base + 1) — stable across restarts because IBKR ties orders to the sending clientId
    requested > 0 = user-defined (checked against reserved ids and other nodes)"""
    with _clients_locked():
        return _allocate(node_id, requested)


def _allocate(node_id: str, requested: int) -> int:
    clients = _load_clients()
    taken = {cid for nid, base in clients.items() if nid != node_id for cid in (base, base + 1)} | _reserved()
    if requested:
        if requested in taken or requested + 1 in taken:
            raise ValueError(f"clientId {requested}/{requested + 1} is already used (reserved or another node)")
        base = requested
    elif node_id in clients and clients[node_id] not in taken and clients[node_id] + 1 not in taken:
        base = clients[node_id]
    else:
        base = next((b for b in range(NODE_ID_MIN, NODE_ID_MAX + 1, 2) if b not in taken and b + 1 not in taken), 0)
        if not base:
            raise ValueError("no free IB clientId left for Nautilus nodes")
    if clients.get(node_id) != base:
        clients[node_id] = base
        _save_clients(clients)
    return base


def release_client_id(node_id: str) -> None:
    with _clients_locked():
        clients = _load_clients()
        if clients.pop(node_id, None) is not None:
            _save_clients(clients)


# ---------- Nautilus identifiers per node ----------
TAG_LEN = 16


def node_tags(node_id: str) -> tuple[str, str]:
    """(trader_id, order_id_tag) for a node — unique per node id (they form the client_order_id prefix used for attribution)
    Ids that fit (≤ 16 chars) keep the historical values unchanged · longer ids get a short hash suffix instead of being cut,
    so 'eurusd_ema_cross_fast_a' and '…_b' no longer collide (their ids change once, at the next node start)."""
    nid = str(node_id or "node")
    tag = nid.replace("-", "_")
    alnum = "".join(ch for ch in nid.upper() if ch.isalnum())
    h = hashlib.sha1(nid.encode()).hexdigest()[:5]
    if len(tag) > TAG_LEN:
        tag = f"{tag[:TAG_LEN - 6]}_{h}"
    if len(nid) > TAG_LEN or len(alnum) > TAG_LEN:
        alnum = f"{alnum[:TAG_LEN - 5]}{h.upper()}"
    return "QX-" + (alnum or "NODE"), tag


# ---------- selected gateway (per Sign-in / gateway profile) ----------
PAPER_PORTS = {4002, 4004, 7497}          # IB defaults for paper (gateway 4002 / ibc docker 4004 / TWS 7497)


def resolve_gateway() -> dict:
    """Port and account a node must use: IB_PORT (if set) > gateway profile selected in the session > first profile > 4004
    Fails closed: when IB_PORT points elsewhere than the profile, the mode comes from the profile owning that port, else it is
    'paper' only for the standard paper ports and 'live' otherwise (so every live gate applies) · a selected profile that no
    longer exists yields 'error' (nodes refuse to start) instead of silently using another — maybe live — profile."""
    from . import gateways as gw
    try:
        sess = json.loads(SESSION_FILE.read_text())
    except Exception:
        sess = {}
    gws = gw.list_gateways()
    error = ""
    g = next((x for x in gws if x["id"] == sess.get("gateway_id")), None) if sess.get("gateway_id") else None
    if g is None:
        if sess.get("gateway_id"):
            error = f"the selected gateway profile '{sess['gateway_id']}' no longer exists — sign in / choose a gateway again"
        g = gws[0] if gws else None
    mode = (g or {}).get("mode", "paper")
    env_port = os.getenv("IB_PORT")
    if env_port:
        port = int(env_port)
        if not g or int(g.get("api_port") or 0) != port:
            owner = next((x for x in gws if int(x.get("api_port") or 0) == port), None)
            mode = owner.get("mode", "live") if owner else ("paper" if port in PAPER_PORTS else "live")
    else:
        port = int(g["api_port"]) if g else 4004
    out = {"host": "127.0.0.1", "port": port, "gateway_id": (g or {}).get("id", ""), "mode": mode,
           "account_id": sess.get("account_id") or os.getenv("IB_ACCOUNT_ID", "")}
    if error:
        out["error"] = error
    return out


def key_to_nautilus(key: str) -> str:
    mkt, sym = key.split(":", 1)
    if mkt == "FX":
        return f"{sym[:3]}/{sym[3:]}.IDEALPRO"
    if mkt == "STK":
        return f"{sym}.SMART"
    if mkt == "CRYPTO":
        return f"{sym}/USD.PAXOS"
    raise ValueError(f"unsupported market for Nautilus: {mkt}")


def log_path(agent_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"nautilus_{agent_id}.log"
