"""
QOS API — bridge between the Terminal and IBKR (ib_async)
instrument key = "<MARKET>:<SYMBOL>"  e.g. FX:EURUSD, STK:AAPL, CRYPTO:BTC, IDX:SPX

Run:  uv run uvicorn api.server:app --host 127.0.0.1 --port 8000
"""
import asyncio
import json
import math
import time
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from qos import __version__ as QOS_VERSION  # noqa: E402  (package version: pyproject.toml or installed metadata)
from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from ib_async import IB, Contract, Crypto, ExecutionFilter, Forex, Index, LimitOrder, MarketOrder, Order, Stock, StopOrder
from pydantic import BaseModel, ConfigDict, Field

from .universe import MARKETS, UNIVERSE, default_watchlist
from .groups import GROUPS, GROUP_BY_ID, SCANNERS, SCANNER_BY_ID, SCAN_ROWS, SCAN_MIN_PRICE, SCAN_MIN_VOLUME, scan_weight, scan_leader
from . import history
from . import attribution as attr
from . import soak
from . import holds
from . import agents_migration
from . import notify
from . import nautilus_bridge as nb
from . import alerts as alerts_mod
from . import gateways as gw
from . import operators as ops
from . import news as news_mod
from . import ib_guard

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4004"))
IB_CLIENT_ID = int(os.getenv("IB_API_CLIENT_ID", "60"))
DATA_DIR = paths.data_dir()
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
MODE_FILE = DATA_DIR / "mode.json"

# ---------- risk limits (demo) ----------
MAX_NOTIONAL_USD = float(os.getenv("MAX_NOTIONAL_USD", "150000"))       # notional per order
MAX_POSITION_NOTIONAL_USD = float(os.getenv("MAX_POSITION_NOTIONAL_USD", "400000"))

MARKET_BY_ID = {m["id"]: m for m in MARKETS}
NAME_BY_KEY = {f"{mkt}:{sym}": name for mkt, rows in UNIVERSE.items() for sym, name in rows}

ib = IB()
# IBKR sends execution times without a timezone; they are UTC — if unset, ib_async treats them as local time (Bangkok +7) and fill times end up 7 hours off
ib.TimezoneTWS = os.getenv("IB_TWS_TIMEZONE", "UTC")
_lock = asyncio.Lock()
_contracts: dict[str, Contract] = {}
_tickers: dict[str, object] = {}


# ---------- helpers ----------
def _f(x, default=0.0) -> float:
    try:
        v = float(x)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def _px(x) -> float:
    v = _f(x)
    return v if v > 0 else 0.0


def parse_key(key: str) -> tuple[str, str]:
    key = key.strip().upper()
    if ":" not in key:
        key = f"FX:{key}" if len(key) == 6 and key.isalpha() else f"STK:{key}"
    mkt, sym = key.split(":", 1)
    if mkt not in MARKET_BY_ID:
        raise HTTPException(400, f"Unknown market {mkt}")
    return mkt, sym.replace(".", " ") if mkt == "STK" else sym


def build_contract(mkt: str, sym: str) -> Contract:
    if mkt == "FX":
        return Forex(sym)
    if mkt == "STK":
        return Stock(sym, "SMART", "USD")
    if mkt == "CRYPTO":
        return Crypto(sym, "PAXOS", "USD")
    if mkt == "IDX":
        return Index(sym, "CBOE", "USD")
    raise HTTPException(400, f"Unsupported market {mkt}")


def decimals_for(mkt: str, sym: str) -> int:
    if mkt == "FX":
        return 3 if sym.endswith("JPY") else 5
    if mkt == "CRYPTO":
        return 0 if sym in ("BTC",) else 2
    return 2


def pip_for(mkt: str, sym: str) -> float:
    if mkt == "FX":
        return 0.01 if sym.endswith("JPY") else 0.0001
    return 0.01


def key_of_contract(c: Contract) -> str:
    st = c.secType
    if st == "CASH":
        return f"FX:{(c.localSymbol or c.symbol).replace('.', '')}"
    if st == "STK":
        return f"STK:{c.symbol}"
    if st == "CRYPTO":
        return f"CRYPTO:{c.symbol}"
    if st == "IND":
        return f"IDX:{c.symbol}"
    return f"{st}:{c.symbol}"


async def ensure_connected() -> None:
    if ib.isConnected():
        return
    async with _lock:
        if ib.isConnected():
            return
        await ib.connectAsync(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=20)
        ib.reqMarketDataType(3)
        _tickers.clear()


async def contract_for(key: str) -> Contract:
    mkt, sym = parse_key(key)
    k = f"{mkt}:{sym}"
    if k not in _contracts:
        c = build_contract(mkt, sym)
        res = await ib.qualifyContractsAsync(c)
        if not res or not c.conId:
            raise HTTPException(404, f"IBKR does not recognize {k}")
        _contracts[k] = c
    return _contracts[k]


async def ticker_for(key: str):
    mkt, sym = parse_key(key)
    k = f"{mkt}:{sym}"
    if k not in _tickers:
        c = await contract_for(k)
        _tickers[k] = ib.reqMktData(c, "", False, False)
        await asyncio.sleep(0.25)
    return _tickers[k]


def load_watchlist() -> list[dict]:
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text())
        except Exception:
            pass
    return default_watchlist()


def save_watchlist(items: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(json.dumps(items, indent=2))


# ---------- risk settings / daily P&L / audit ----------
RISK_FILE = DATA_DIR / "risk.json"
DAILY_FILE = DATA_DIR / "daily.json"
AUDIT_FILE = DATA_DIR / "audit.jsonl"
DEFAULT_ASSISTANT = {        # limits for the LLM assistant (via MCP) — separate from the trader
    "write_enabled": False,          # off = read-only (toggle in the Terminal)
    "live": False,                   # allow placing orders on live accounts
    "max_order_notional_usd": 25000.0,
    "max_orders_per_hour": 10,
    "allow_short": False,
    "allow_agents": True,            # may start/stop/configure agents
    "confirm_ttl_s": 90,
    "auto_confirm": False,           # off = an operator approves each assistant order in the Terminal; on = the assistant confirms its own
}
DEFAULT_RISK = {
    "allow_live": False,
    "max_daily_loss_usd": 2000.0,
    "max_order_notional_usd": MAX_NOTIONAL_USD,
    "max_position_notional_usd": MAX_POSITION_NOTIONAL_USD,
    "max_open_positions": 10,
    "halted": False,
    "halt_reason": "",
}


GLOBAL_RISK_KEYS = {"allow_live", "news_pause_minutes", "assistant", "markets_tradable"}   # settings shared by all accounts
DEFAULT_MARKETS_TRADABLE = {"CRYPTO": False}   # crypto trading is not available to every IBKR account (regional regulatory restrictions) → view-only until an admin enables it


def market_tradable(mkt: str) -> bool:
    """Whether orders can be sent in this market: universe → broker result (rejected for regulatory reasons) → admin override"""
    base = bool(MARKET_BY_ID.get(mkt, {}).get("tradable"))
    d = _risk_doc()["default"]
    if mkt in (d.get("markets_blocked_by_broker") or {}):
        return False
    ov = {**DEFAULT_MARKETS_TRADABLE, **(d.get("markets_tradable") or {})}
    return base and ov.get(mkt, True)


BROKER_BLOCK_PATTERNS = ("regulatory restrictions", "no trading permission", "not allowed to open a position", "trading permissions")


def block_market_from_broker(mkt: str, reason: str) -> None:
    """IBKR rejected for permission/regulatory reasons → make that market view-only immediately (admin can re-enable in Risk → Security)"""
    doc = _risk_doc()
    blocked = doc["default"].setdefault("markets_blocked_by_broker", {})
    if mkt not in blocked:
        blocked[mkt] = {"reason": reason[:200], "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "account": active_account()}
        doc["default"].setdefault("markets_tradable", {})[mkt] = False
        _save_risk_doc(doc)
        audit("market_blocked_by_broker", market=mkt, reason=reason[:200])
        notify.fire(f"{mkt} set to view-only: IBKR rejected orders ({reason[:120]})", "error")


def market_block_reason(mkt: str) -> str:
    if mkt == "IDX":
        return "view-only index"
    if mkt == "CRYPTO":
        return "crypto trading is not permitted on this IBKR account (Regulatory Restrictions) — view-only until an admin enables it in Risk → Security"
    return "trading disabled for this market by admin"


def load_assistant() -> dict:
    d = dict(DEFAULT_ASSISTANT)
    d.update(_risk_doc()["default"].get("assistant", {}))
    return d


def save_assistant(d: dict) -> None:
    doc = _risk_doc()
    doc["default"].setdefault("assistant", {}).update(d)
    _save_risk_doc(doc)


def _risk_doc() -> dict:
    """New risk.json format: {"default": {...}, "accounts": {"DUxxxx": {...}}} (migrated from the old file automatically)"""
    try:
        doc = json.loads(RISK_FILE.read_text())
    except Exception:
        doc = {}
    if "default" not in doc or "accounts" not in doc:
        doc = {"default": {k: v for k, v in doc.items() if k not in ("default", "accounts")}, "accounts": doc.get("accounts", {})}
    return doc


def _save_risk_doc(doc: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RISK_FILE.write_text(json.dumps(doc, indent=2))


def load_risk(account: str | None = None) -> dict:
    """Account risk settings: DEFAULT_RISK ← default (all accounts) ← that account's override"""
    doc = _risk_doc()
    acct = account if account is not None else active_account()
    d = dict(DEFAULT_RISK)
    d.update(doc["default"])
    d.update(doc["accounts"].get(acct, {}) if acct else {})
    d["account"] = acct
    return d


def save_risk(d: dict, account: str | None = None) -> None:
    doc = _risk_doc()
    acct = account if account is not None else active_account()
    d = dict(d)
    d.pop("account", None)
    for k in GLOBAL_RISK_KEYS:
        if k in d:
            doc["default"][k] = d.pop(k)
    if acct:
        doc["accounts"].setdefault(acct, {}).update(d)
    else:
        doc["default"].update(d)
    _save_risk_doc(doc)


KILL_ALL_REASON = "kill switch (all accounts)"


def halt_all_accounts(reason: str) -> None:
    doc = _risk_doc()
    for blk in (doc["default"], *doc["accounts"].values()):
        if not blk.get("halted"):                         # an existing halt (daily loss, own kill) keeps its reason, so a
            blk.update(halted=True, halt_reason=reason)   # resume of the kill-all clears only what the kill-all set
    _save_risk_doc(doc)


from .node_manager import NodeManager  # noqa: E402
from . import strategy_status as sst  # noqa: E402


def _node_can_start(nid: str | None = None) -> tuple[bool, str]:
    r = load_risk(account="")
    if load_risk().get("halted") or r.get("halted"):
        return False, "trading is halted (kill switch / daily loss)"
    if nid is not None:                                       # step 38: agents from the QOS Store must pass signature/file/revocation/permission checks
        try:
            ok, why = cloud_api.cloud_package_check(_node_ref(nid))
        except Exception as e:
            ok, why = False, f"cannot verify QOS Store agent: {e}"
        if not ok:
            return False, why
    if nb.resolve_gateway().get("mode") == "live":
        if not r.get("allow_live"):
            return False, "live trading is locked (allow_live is off)"
        if nid is not None:                                   # step 20: the node needs its own allow_live and the strategy version must have passed soak GO
            if not (nm.nodes.get(nid) or {}).get("allow_live"):
                return False, f"node {nid} is paper-only — allow it with POST /nodes/{nid}/allow_live once its strategy version is soak_go"
            ref = _node_ref(nid)
            if sst.state_of(ref or "") not in ("soak_go", "live"):
                return False, f"{ref} is '{sst.state_of(ref or '') or 'unknown'}' — live needs soak_go"
            rp = (nm.nodes.get(nid) or {}).get("ramp")
            if rp and rp.get("status") == "paused":                   # step 14: ramp paused = must resume first
                return False, f"node {nid} live ramp is paused ({rp.get('note') or 'review needed'}) — resume it with the live code"
        blocking = [c for c in _live_readiness_global() if c["level"] == "block" and not c["ok"]]
        if blocking:
            return False, "live readiness: " + "; ".join(c["detail"] for c in blocking)
    return True, ""


def _live_readiness_global() -> list[dict]:
    """Step 14: account-level conditions before real money (block = cannot start nodes on live · warn = recommendation)"""
    r = load_risk(account="")
    acct = active_account() or ""
    own = (_risk_doc().get("accounts") or {}).get(acct, {})
    out = []

    def add(name, ok, level, detail):
        out.append({"name": name, "ok": bool(ok), "level": level, "detail": detail})
    add("allow_live", r.get("allow_live"), "block", "an admin must enable 'Allow live trading' in Risk settings")
    add("live_code", ops.live_code_set(), "block", "set a live confirmation code in Risk settings → Security")
    add("daily_loss_limit", float(own.get("max_daily_loss_usd") or 0) > 0, "block",
        f"set this account's own daily loss limit in Risk settings ({acct or 'no account'}) — the shared default is not enough for live")
    add("position_limit", float(load_risk().get("max_position_notional_usd") or 0) > 0, "block", "set a max position notional in Risk settings")
    add("portfolio_limits", bool(own.get("portfolio")), "block",
        f"review and save portfolio limits for {acct or 'this account'} (Agents › Portfolio risk) — currency, correlated group, VaR, drawdown, margin")
    add("not_halted", not (load_risk().get("halted") or r.get("halted")), "block", "trading is halted — resume first")
    add("notifications", notify.configured(), "warn", "configure Telegram notifications so you hear about stops and ramp changes")
    add("admin_operator", any(o.get("role") == "admin" and o.get("active", True) for o in ops.list_ops()), "warn",
        "create an admin operator (PIN) so live changes are authenticated")
    return out


_main_loop: asyncio.AbstractEventLoop | None = None       # set in lifespan; node events arrive from the supervisor thread


def _hold_restarted_node(nid: str) -> None:
    """An auto-restarted node starts flat (filter_position_reports=True) while IBKR may still hold its old position, and the
    strategy could open a second one: hold its new entries until the operator confirms the positions (release_hold)"""
    try:
        held = {k: b[f"node:{nid}"] for k, b in history.book_positions(active_account()).items() if abs(b.get(f"node:{nid}", 0.0)) > 1e-6}
    except Exception as e:
        print("restart hold:", e)
        return
    if held and holds.add(nid, "auto-restarted while holding " + ", ".join(f"{k} {q:+,.0f}" for k, q in held.items()) + " — confirm positions"):
        audit("node_hold_restart", node=nid, positions=held)
        if _main_loop is not None:
            asyncio.run_coroutine_threadsafe(publish_risk(), _main_loop)


def _note_soak_ref(nid: str) -> None:
    """Soak evidence belongs to the strategy version the node's process runs, counted from that process's start"""
    try:
        rt = nm.rt.get(nid) or {}
        soak.note_ref(nid, rt.get("strategy_ref"), started_ns=int(rt["started"] * 1e9) if rt.get("started") else None)
    except Exception as e:
        print("soak note_ref:", e)


def _node_event(event: str, notify_user: bool = False, **fields) -> None:
    if fields.pop("notify", False):
        notify_user = True
    audit(event, **fields)
    if event in ("node_started", "node_restarted"):
        _note_soak_ref(fields.get("node", ""))
    if event == "node_restarted":
        _hold_restarted_node(fields.get("node", ""))
    if event == "node_stopped" and fields.get("node") and fields.get("reason") != "kill switch":
        _on_loop(_after_node_stop_safe(fields["node"]))       # kill switch: its global cancel covers every order
    if event in ("node_exited", "node_crashed"):
        try:
            soak.incident(fields.get("node", ""), "crash", f"{event.replace('node_', '')} · exit {fields.get('exit_code')}")
        except Exception as e:
            print("soak incident:", e)
    if notify_user or event in ("node_crashed", "nodes_resumed_failed"):
        try:
            notify.fire(f"Nautilus node {fields.get('node', '')}: {event.replace('node_', '')}"
                        + (f" (exit {fields.get('exit_code')})" if "exit_code" in fields else ""), "agent")
        except Exception as e:
            print("node notify:", e)


nm = NodeManager(can_start=_node_can_start, on_event=_node_event)
from . import ramp as ramp_mod  # noqa: E402
nm.size_multiplier = lambda nid: ramp_mod.multiplier((nm.nodes.get(nid) or {}).get("ramp"))

from .event_bus import EventBus  # noqa: E402


def _fmt_qty(q) -> str:
    try:
        return f"{float(q):,.0f}" if float(q) >= 1 else f"{float(q):g}"
    except Exception:
        return str(q)


def _attribute_node_event(ev: dict) -> None:
    """Node order / fill → history with node + strategy (idempotent, so it also runs during replay)"""
    t, d, node = ev.get("type"), ev.get("data") or {}, ev.get("node", "")
    coid = d.get("client_order_id") or ""
    if t not in ("order", "fill") or not node or not coid:
        return
    if t == "order":
        history.record_node_order(coid, node, ev.get("strategy", ""), ev.get("instrument", ""), ev.get("ts", ""))
        return
    key = attr.nautilus_to_key(ev.get("instrument"))
    if not key or not d.get("trade_id"):
        return
    vo = str(d.get("venue_order_id") or "")
    history.record_node_fill({
        "exec_id": str(d["trade_id"]), "ts": ev.get("ts") or datetime.now(timezone.utc).isoformat(), "key": key,
        "symbol": key.split(":", 1)[1], "side": "BUY" if str(d.get("side", "")).upper() in ("BUY", "BOT") else "SELL",
        "qty": _f(d.get("qty")), "price": _f(d.get("price")), "commission": _commission_usd(_f(d.get("commission")), d.get("currency")),
        "order_id": int(vo) if vo.isdigit() else None, "order_ref": coid, "account": active_account(),
        "node": node, "strategy": ev.get("strategy", ""), "instrument": ev.get("instrument", ""),
    })


from .package_guard import PackageGuard  # noqa: E402

pkg_guard = PackageGuard(lambda node: (nm.rt.get(node) or {}).get("package_limits"), rates_of=lambda: _usd_rates())


async def _package_breach(b: dict, ev: dict) -> None:
    """Package cap exceeded: audit + Terminal · API layer tells the node to cancel that order · 3 times within 10 minutes → stop the node"""
    node = b["node"]
    audit("package_limit_breach", node=node, limit=b["limit"], reason=b["reason"], source=b["source"],
          instrument=ev.get("instrument"), client_order_id=b.get("client_order_id"), count_10m=b["count_10m"])
    await ws_broadcast({"type": "package_limit", **b})
    if b["source"] == "api" and b.get("client_order_id"):
        await send_control("cancel_order", node=node, client_order_id=b["client_order_id"], reason=f"{b['limit']}: {b['reason']}")
    if b["stop"]:
        why = f"package limits breached {b['count_10m']}× in 10 min (last: {b['limit']})"
        try:
            await asyncio.to_thread(nm.stop, node, why)
        except Exception as e:
            print("package guard stop:", e)
        audit("node_stopped_package_limits", node=node, reason=why)
        notify.fire(f"Node {node} stopped: {why}", "error")


async def _on_node_event(ev: dict, replay: bool) -> None:
    """Event from a Nautilus node: record attribution + push to the Terminal immediately + notify on fill / order rejected (no notifications during replay)"""
    try:
        _attribute_node_event(ev)
    except Exception as e:
        print("attribution:", e)
    try:
        soak_rec.on_event(ev, replay)
    except Exception as e:
        print("soak:", e)
    if replay:
        return
    try:
        for b in pkg_guard.on_event(ev):
            await _package_breach(b, ev)
    except Exception as e:
        print("package guard:", e)
    if ev.get("type") == "fill" and ev.get("node"):
        _pf_state["fills"].append((time.time(), ev["node"]))
        del _pf_state["fills"][:-200]
    elif ev.get("type") == "risk" and (ev.get("data") or {}).get("event") == "portfolio_limit":
        audit("portfolio_limit_blocked", node=ev.get("node"), instrument=ev.get("instrument"), **{k: v for k, v in (ev.get("data") or {}).items()
                                                                                                if k in ("limit", "reason", "side", "qty")})
    await ws_broadcast({"type": "node_event", "event": ev})          # heartbeats too (live position updates in the Nodes tab)
    d, node, inst = ev.get("data") or {}, ev.get("node", ""), ev.get("instrument", "")
    if ev.get("type") == "fill":
        notify.fire(f"Node {node}: filled {d.get('side')} {_fmt_qty(d.get('qty'))} {inst} @ {d.get('price')}", "fill")
    elif ev.get("type") == "order" and d.get("event") in ("Rejected", "Denied"):
        notify.fire(f"Node {node}: order {d.get('event').lower()} {inst} — {d.get('reason', '')}", "error")
        audit("node_order_rejected", node=node, instrument=inst, reason=d.get("reason"), client_order_id=d.get("client_order_id"))


soak_rec = soak.Recorder()
bus = EventBus(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), on_event=_on_node_event)
nm.live = bus.live


import contextvars as _cv
_req_meta: _cv.ContextVar[dict] = _cv.ContextVar("req_meta", default={})


def audit(event: str, **fields) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    op = ops.current.get()
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
           "operator": (op or {}).get("id", "local"), "role": (op or {}).get("role", ""), "account": fields.pop("account", active_account()),
           "source": _req_meta.get().get("source", "terminal"), "request_id": _req_meta.get().get("request_id", ""), **fields}
    with AUDIT_FILE.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def read_audit(limit: int = 100) -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    lines = AUDIT_FILE.read_text().splitlines()[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return list(reversed(out))


_acct_cache: dict = {"ts": 0.0, "values": {}}


# ---------- session / login (IBKR only) ----------
SESSION_FILE = DATA_DIR / "session.json"
_setup = {"state": "idle", "message": "", "started": None}   # state: idle | starting | ready | error


def load_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except Exception:
        return {"logged_in": False, "account_id": "", "username": "", "mode": ""}


def save_session(d: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(d, indent=2))


def active_account() -> str:
    """The account the user selected (if still among managed accounts), otherwise the first account"""
    accts = ib.managedAccounts() if ib.isConnected() else []
    sel = load_session().get("account_id") or os.getenv("IB_ACCOUNT_ID", "")
    if sel and (not accts or sel in accts):
        return sel
    return accts[0] if accts else ""


def _mine(acct_of_item: str) -> bool:
    """Filter data down to the selected account (no filtering with a single account)"""
    a = active_account()
    return not a or not acct_of_item or acct_of_item == a


def active_gateway() -> dict | None:
    gid = load_session().get("gateway_id", "")
    g = gw.get(gid) if gid else None
    if g is None:
        gws = gw.list_gateways()
        g = gws[0] if gws else None
    return g


def gateway_port() -> int:
    if os.getenv("IB_PORT"):
        return int(os.getenv("IB_PORT"))
    g = active_gateway()
    return int(g["api_port"]) if g else 4004


async def connect_gateway(gid: str) -> None:
    """Switch to another gateway (paper ↔ live / different user) without restarting the container"""
    global IB_PORT
    g = gw.get(gid)
    if not g:
        raise HTTPException(404, f"unknown gateway {gid}")
    # Nautilus nodes are bound to the old gateway's port/account → stop them before switching (never move to another account, especially paper → live, on their own)
    prev = active_gateway()
    if prev and prev["id"] != gid:
        await asyncio.to_thread(nm.stop_all, f"gateway switched {prev['id']} → {gid}")
        audit("nautilus_stopped_on_gateway_switch", from_gateway=prev["id"], to_gateway=gid)
    sess = load_session()
    sess.update(gateway_id=gid, mode=g["mode"], username=_mask(g["username"]), logged_in=False, account_id="")
    save_session(sess)
    if ib.isConnected():
        ib.disconnect()
    _tickers.clear()
    _acct_cache.update(ts=0.0, values={})
    IB_PORT = int(g["api_port"])
    await ensure_connected()


# ---------- account summary (avoid duplicate subscription requests until IBKR returns error 322) ----------
_sum_state = {"ts": 0.0}


async def acct_summary(acct: str) -> list:
    """Account summary values: use what ib_async already has; if empty (after a connection hiccup) use the accountValues stream,
    and request a new subscription no more often than every 30 seconds"""
    import time as _t
    rows = [v for v in ib.wrapper.acctSummary.values() if not acct or v.account == acct]
    if rows:
        return rows
    rows = _one_row_per_tag(ib.accountValues(acct))
    if rows:
        return rows
    if _t.time() - _sum_state["ts"] > 30:
        _sum_state["ts"] = _t.time()
        try:
            await asyncio.wait_for(ib.reqAccountSummaryAsync(), 10)
        except Exception as e:
            print("account summary request failed:", e)
    return [v for v in ib.wrapper.acctSummary.values() if not acct or v.account == acct]


def _one_row_per_tag(values) -> list:
    """The accountValues stream sends UnrealizedPnL, TotalCashValue … once per currency (BASE, USD, JPY …):
    keep one row per tag — BASE, else USD, else the one without a currency — never a foreign-currency figure"""
    rank = {"BASE": 0, "USD": 1, "": 2}
    best = {}
    for v in values or []:
        r = rank.get(v.currency, 9)
        if r < 9 and (v.tag not in best or r < rank.get(best[v.tag].currency, 9)):
            best[v.tag] = v
    return list(best.values())


async def account_values() -> dict:
    """Latest account values (cached 2 seconds)"""
    import time as _t
    if _t.time() - _acct_cache["ts"] < 2.0 and _acct_cache["values"]:
        return _acct_cache["values"]
    await ensure_connected()
    acct = active_account()
    if not acct:
        return {}
    vals = {}
    for v in await acct_summary(acct):
        vals[v.tag] = v.value
    vals["account_id"] = acct
    _acct_cache.update(ts=_t.time(), values=vals)
    return vals


NY_TZ = ZoneInfo("America/New_York")


def _day_start(now: datetime | None = None) -> datetime:
    """Start of the current IBKR trading day: 17:00 New York (the FX rollover), as a UTC datetime"""
    ny = (now or datetime.now(timezone.utc)).astimezone(NY_TZ)
    start = ny.replace(hour=17, minute=0, second=0, microsecond=0)
    if ny < start:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


def _today() -> str:
    """Trading-day label (the New York date on which the day started at 17:00)"""
    return _day_start().astimezone(NY_TZ).strftime("%Y-%m-%d")


async def daily_pnl() -> dict:
    """Today's P&L = NetLiq now − NetLiq at the start of the IBKR trading day (17:00 New York).
    Baseline = the last equity row recorded in the hour before the boundary. If the account has only older equity rows (the API
    was offline at 17:00 NY) the first NetLiq seen is only provisional: baseline "unconfirmed" blocks new orders until an
    operator confirms it (POST /risk/daily_baseline) — losses made before the API started would otherwise be invisible.
    No equity history at all (new install): the first NetLiq seen ("first_seen").
    Deposits and withdrawals change NetLiq and therefore count as P&L (IBKR cash transactions are not read)."""
    vals = await account_values()
    netliq = _f(vals.get("NetLiquidation"))
    acct = vals.get("account_id") or active_account() or "_"
    today = _today()
    alld = {}
    try:
        alld = json.loads(DAILY_FILE.read_text())
    except Exception:
        pass
    if "date" in alld and "accounts" not in alld:          # migrate the old format (single account)
        alld = {"accounts": {acct: alld}}
    accounts = alld.setdefault("accounts", {})
    data = accounts.get(acct, {})
    if data.get("date") != today and netliq > 0:
        start_dt = _day_start()
        base, older = None, None
        hacct = vals.get("account_id") or active_account() or ""
        try:
            base = history.netliq_before(hacct, start_dt.isoformat(), (start_dt - timedelta(hours=DAILY_BASELINE_WINDOW_H)).isoformat())
            older = None if base else history.netliq_before(hacct, start_dt.isoformat())
        except Exception as e:
            print("daily_pnl: baseline from history failed:", e)
        # equity recorded only long before the boundary = the API was offline at 17:00 NY → the operator must confirm;
        # no equity history at all (first day of a new install) → the first NetLiq seen is the best available
        data = {"date": today, "start_netliq": base or netliq,
                "baseline": "history" if base else ("unconfirmed" if older else "first_seen")}
        if data["baseline"] == "unconfirmed":                     # once per trading day and account (the day file is written once)
            notify.fire(f"Daily-loss baseline for {acct} is unconfirmed (the API was offline at the trading-day start) — "
                        "new orders are blocked until it is confirmed in the Risk panel", "error")
        accounts[acct] = data
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DAILY_FILE.write_text(json.dumps(alld))
    start = _f(data.get("start_netliq")) or netliq
    baseline = data.get("baseline", "first_seen")          # files written before baselines were tracked: accepted
    return {"date": today, "account": acct, "start_netliq": start, "netliq": netliq, "pnl": netliq - start if netliq else 0.0,
            "baseline": baseline, "baseline_confirmed": baseline != "unconfirmed"}


DAILY_BASELINE_WINDOW_H = 1      # an equity row older than this before 17:00 NY is not a trustworthy start-of-day NetLiq


async def risk_gate(notional_usd: float, key: str) -> None:
    """Checks before every order: halted, daily loss, number of positions"""
    r = load_risk()
    if r.get("halted"):
        raise HTTPException(423, f"Trading halted: {r.get('halt_reason') or 'kill switch'}")
    d = await daily_pnl()
    limit = _f(r.get("max_daily_loss_usd"))
    if limit > 0 and _f(d.get("netliq")) <= 0:
        raise HTTPException(409, "Daily P&L unknown (no NetLiquidation from IBKR yet) — order blocked until account values arrive")
    if limit > 0 and not d.get("baseline_confirmed", True):
        raise HTTPException(409, "Daily-loss baseline unknown (the API was not running at the trading-day start) — "
                                 "confirm today's starting NetLiq in the Risk panel (POST /risk/daily_baseline)")
    if limit > 0 and d["pnl"] <= -limit:
        r["halted"] = True
        r["halt_reason"] = f"daily loss limit hit ({d['pnl']:,.0f} USD)"
        save_risk(r)
        audit("halt", reason=r["halt_reason"])
        notify.fire(f"TRADING HALTED: {r['halt_reason']}", "halt")
        raise HTTPException(423, f"Daily loss limit reached ({d['pnl']:,.0f} USD) — trading halted")
    open_keys = {key_of_contract(p.contract) for p in ib.positions() if _f(p.position) != 0 and _mine(p.account)}
    open_keys |= {key_of_contract(tr.contract) for tr in ib.openTrades() if not getattr(tr.order, "parentId", 0)
                  and _mine(getattr(tr.order, "account", "") or "")}
    if key not in open_keys and len(open_keys) >= int(r.get("max_open_positions", 10)):
        raise HTTPException(400, f"Max open positions ({int(r.get('max_open_positions'))}) reached")
    if notional_usd > _f(r.get("max_order_notional_usd")):
        raise HTTPException(400, f"Order notional {notional_usd:,.0f} USD exceeds limit {_f(r.get('max_order_notional_usd')):,.0f}")


from qx_strategies import fx  # noqa: E402

_order_lock = asyncio.Lock()          # serialises check → placeOrder so concurrent orders see each other


def _usd_rates() -> dict:
    """USD value of one unit of each currency (qx_strategies.fx format): the daily stats rates, then live USD-pair tickers"""
    prices = {}
    for k, t in list(_tickers.items()):
        mkt, _, sym = k.partition(":")
        if mkt != "FX":
            continue
        try:
            px = _px(t.marketPrice())
        except Exception:
            px = 0.0
        if px:
            prices[sym] = px
    return fx.rates_from_prices(prices, base=dict((_pf_state.get("stats") or {}).get("rates") or {}))


async def _usd_per_unit(mkt: str, sym: str, ref: float) -> float:
    """USD value of one unit (FX base currency / share / coin). For a cross pair, subscribes the USD leg once if needed.
    Raises 409 if it cannot be priced: risk checks must never treat an unknown value as 0."""
    if mkt != "FX":
        return ref
    v = fx.base_to_usd(sym, ref, _usd_rates())
    if v is None:
        base, quote = fx.split_pair(sym)
        for leg in (f"{base}USD", f"USD{base}", f"{quote}USD", f"USD{quote}"):
            if any(s == leg for s, _ in UNIVERSE.get("FX", [])):
                try:
                    await ticker_for(f"FX:{leg}")
                except Exception:
                    pass
        v = fx.base_to_usd(sym, ref, _usd_rates())
    if v is None:
        raise HTTPException(409, f"FX:{sym}: no USD rate for {sym[:3]}/{sym[3:]} — cannot check risk limits, order not sent")
    return v


def _export_usd_rates() -> None:
    """Strategy-lab / evaluate jobs run as child processes (they inherit os.environ): hand them today's USD rates as JSON
    in QOS_USD_RATES so backtests / walk-forwards of cross pairs can convert P&L (read as run_backtest's usd_rates)"""
    try:
        os.environ["QOS_USD_RATES"] = json.dumps(_usd_rates())
    except Exception as e:
        print("usd rates export:", e)


def _working_qty(k: str, exclude_order_id: int | None = None) -> tuple[float, float]:
    """Remaining qty of working ENTRY orders on key k as (buys, sells). Bracket children (parentId ≠ 0) are exits and are not counted."""
    buys = sells = 0.0
    for tr in ib.openTrades():
        o = tr.order
        if key_of_contract(tr.contract) != k or getattr(o, "parentId", 0) or o.orderId == exclude_order_id:
            continue
        if not _mine(getattr(o, "account", "") or ""):
            continue
        rem = max(0.0, _f(o.totalQuantity) - _f(getattr(tr.orderStatus, "filled", 0)))
        if o.action == "BUY":
            buys += rem
        else:
            sells += rem
    return buys, sells


def _working_legs(k: str, exclude_order_id: int | None = None) -> tuple[float, float]:
    """Remaining qty of working TP/SL legs whose entry has filled, as (buys, sells); per bracket only the larger leg counts
    (one OCA group executes once). Legs of still-working entries are covered by the entry itself."""
    working = {tr.order.orderId for tr in ib.openTrades()}
    per: dict[tuple, float] = {}
    for tr in ib.openTrades():
        o = tr.order
        pid = getattr(o, "parentId", 0)
        if not pid or pid in working or o.orderId == exclude_order_id or key_of_contract(tr.contract) != k:
            continue
        if not _mine(getattr(o, "account", "") or ""):
            continue
        rem = max(0.0, _f(o.totalQuantity) - _order_filled(tr))
        per[(pid, o.action)] = max(per.get((pid, o.action), 0.0), rem)
    return (sum(q for (_, a), q in per.items() if a == "BUY"), sum(q for (_, a), q in per.items() if a == "SELL"))


def _check_position(k: str, side: str, qty: float, usd_per_unit: float, exclude_order_id: int | None = None) -> None:
    """Worst-case resulting position (current + same-side working orders + this order) must stay within max_position_notional_usd"""
    rk = load_risk()
    cur = position_qty(k)
    buys, sells = _working_qty(k, exclude_order_id)
    leg_buys, leg_sells = _working_legs(k, exclude_order_id)      # TP/SL of filled entries also trade when they trigger
    projected = cur + buys + leg_buys + qty if side == "BUY" else cur - sells - leg_sells - qty
    max_pos = _f(rk.get("max_position_notional_usd"))
    if abs(projected) * usd_per_unit > max_pos:
        raise HTTPException(400, f"Resulting position incl. working orders {abs(projected) * usd_per_unit:,.0f} USD exceeds limit {max_pos:,.0f}")


# ---------- risk limits → Nautilus nodes via Redis (step 8) ----------
RISK_KEY, CONTROL_CHANNEL = "qx:risk", "qx:control"
_risk_redis = None
_risk_pub: dict = {"ok": None, "error": "", "ts": None}


async def _rredis():
    global _risk_redis
    if _risk_redis is None:
        import redis.asyncio as aioredis
        _risk_redis = aioredis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), socket_connect_timeout=1, socket_timeout=2)
    return _risk_redis


_cal_task: asyncio.Task | None = None


async def risk_payload() -> dict:
    """Risk settings of the selected account + account state (today's P&L, open positions) — nodes check the same way as the API risk_gate"""
    r = load_risk()
    pnl, open_keys, notional = None, set(), {}
    account_data = False
    rates = _usd_rates()
    if ib.isConnected():
        try:
            d = await daily_pnl()
            account_data = _f(d.get("netliq")) > 0
            pnl = round(d["pnl"], 2) if account_data and d.get("baseline_confirmed", True) else None   # unknown ≠ zero P&L
        except Exception:
            pass
        for p in ib.positions():
            q = _f(p.position)
            if q == 0 or not _mine(p.account):
                continue
            k = key_of_contract(p.contract)
            mkt, sym = parse_key(k)
            t = _tickers.get(k)
            px = (_px(t.marketPrice()) if t else 0.0) or _f(p.avgCost)
            upu = fx.base_to_usd(sym, px, rates) if mkt == "FX" else px
            open_keys.add(k)
            if upu is None:                       # cannot value in USD: omitted — the node values it itself and blocks if it cannot
                continue
            notional[k] = round(notional.get(k, 0.0) + abs(q) * upu, 2)
    pause = int(r.get("news_pause_minutes", 0) or 0)
    news_ccy: set[str] = set()
    cal_stale = False
    if pause:
        global _cal_task
        age = news_mod.cache_age_s()
        if (age is None or age > 1800) and (_cal_task is None or _cal_task.done()):
            _cal_task = asyncio.create_task(news_mod.calendar())    # refresh in the background (cached 30 min)
        if _cal_task is not None and not _cal_task.done():
            await asyncio.wait({_cal_task}, timeout=1.0)           # at most 1 s: a slow calendar host never holds up risk publishing
        items = news_mod._cal_cache.get("items") or []
        age = news_mod.cache_age_s()
        cal_stale = age is None or age > 6 * 3600
        for ev in news_mod.upcoming_high_impact(items, set(), pause):
            news_ccy.add(str(ev.get("country", "")).upper())
    return {"account": active_account(), "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "halted": bool(r.get("halted")), "halt_reason": r.get("halt_reason", ""),
            "news_pause_minutes": pause, "news_pause_currencies": sorted(c for c in news_ccy if c), "news_calendar_stale": cal_stale,
            "max_order_notional_usd": _f(r.get("max_order_notional_usd")), "max_position_notional_usd": _f(r.get("max_position_notional_usd")),
            "max_open_positions": int(r.get("max_open_positions") or 0), "max_daily_loss_usd": _f(r.get("max_daily_loss_usd")),
            "daily_pnl_usd": pnl, "account_data": account_data, "open_keys": sorted(open_keys), "position_notional_usd": notional, "fx_rates": rates,
            "blocked_nodes": holds.payload(), "portfolio": await _portfolio_payload()}


def _halt_for_daily_loss(p: dict) -> bool:
    """Nodes trade without going through the API risk_gate → also periodically check the whole account's daily loss"""
    lim, pnl = p.get("max_daily_loss_usd") or 0, p.get("daily_pnl_usd")
    if p.get("halted") or not lim or pnl is None or pnl > -lim:
        return False
    r = load_risk()
    r["halted"], r["halt_reason"] = True, f"daily loss limit hit ({pnl:,.0f} USD)"
    save_risk(r)
    audit("halt", reason=r["halt_reason"], source_check="risk_loop")
    notify.fire(f"TRADING HALTED: {r['halt_reason']} — Nautilus nodes switched to reduce-only", "halt")
    p["halted"], p["halt_reason"] = True, r["halt_reason"]
    return True


async def publish_risk() -> dict | None:
    try:
        p = await risk_payload()
        _halt_for_daily_loss(p)
        await (await _rredis()).set(RISK_KEY, json.dumps(p, default=str), ex=30)
        _risk_pub.update(ok=True, error="", ts=p["ts"])
        return p
    except Exception as e:
        _risk_pub.update(ok=False, error=str(e)[:200])
        return None


async def send_control(cmd: str, **fields) -> int:
    """Send a command to nodes immediately (pub/sub) — returns the number of receivers"""
    try:
        return int(await (await _rredis()).publish(CONTROL_CHANNEL, json.dumps({"cmd": cmd, "ts": datetime.now(timezone.utc).isoformat(), **fields})))
    except Exception as e:
        print("send_control:", e)
        return 0


async def risk_loop() -> None:
    i = 0
    while True:
        await publish_risk()
        try:
            await _portfolio_watch()
        except Exception as e:
            print("portfolio watch:", e)
        if i % 6 == 5:
            try:
                await _recon_watch()
            except Exception as e:
                print("reconcile:", e)
        i += 1
        await asyncio.sleep(5)


# ---------- bars cache (shared between /bars and agents) ----------
_bars_cache: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
BARS_TTL = 20.0


async def fetch_bars(key: str, bar_size: str = "1 min", duration: str = "1 D") -> list[dict]:
    mkt, sym = parse_key(key)
    ck = (f"{mkt}:{sym}", bar_size, duration)
    ts, cached = _bars_cache.get(ck, (0.0, []))
    import time as _t
    if _t.time() - ts < BARS_TTL and cached:
        return cached
    c = await contract_for(ck[0])
    what = {"FX": "MIDPOINT", "STK": "TRADES", "CRYPTO": "AGGTRADES", "IDX": "TRADES"}[mkt]
    data = await ib.reqHistoricalDataAsync(
        c, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
        whatToShow=what, useRTH=False, formatDate=2,
    )
    rows = [
        {"t": b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date),
         "o": _f(b.open), "h": _f(b.high), "l": _f(b.low), "c": _f(b.close), "v": _f(b.volume)}
        for b in data
    ]
    _bars_cache[ck] = (_t.time(), rows)
    return rows


def quote_dict(key: str) -> dict:
    mkt, sym = parse_key(key)
    t = _tickers.get(f"{mkt}:{sym}")
    if t is None:
        return {}
    bid, ask, last = _px(t.bid), _px(t.ask), _px(t.last)
    return {"bid": bid, "ask": ask, "last": last, "mid": (bid + ask) / 2 if bid and ask else last}


def _mark(t) -> float:
    """Mark price of a ticker: last → mid (only when BOTH bid and ask exist) → close; 0.0 if nothing"""
    if t is None:
        return 0.0
    bid, ask = _px(t.bid), _px(t.ask)
    return _px(t.last) or ((bid + ask) / 2 if bid and ask else 0.0) or _px(t.close)


def _usd_pair(cur: str) -> str:
    """IBKR's USD pair for a currency: EURUSD / GBPUSD / AUDUSD / NZDUSD, every other currency trades as USDxxx (USDJPY, USDCHF, …)"""
    return f"{cur}USD" if cur in ("EUR", "GBP", "AUD", "NZD") else f"USD{cur}"


_cash_warn_sent: dict[str, str] = {}      # key → trading day of the last cash-vs-books warning notification


def _fx_cash_map() -> dict[str, dict]:
    """FX exposure from the account's cash balances, per USD pair: {"FX:EURUSD": {"ccy", "cash", "qty"}, "FX:USDJPY": …}
    cash = ledger balance of the currency minus the legs of virtual CROSS positions (EUR/GBP …) that use it, so a cross
    position is not counted a second time in EURUSD / GBPUSD. qty = position in the pair's base units, whole units
    (cash residue such as 20,000.37 EUR is never an exact position); for USDxxx it is −cash / mark price (approximate)
    and None when there is no price. Currencies without balance data are absent."""
    cash: dict[str, float] = {}
    try:
        for v in ib.accountValues(active_account()):
            # IBKR sends per-currency balances as "$LEDGER-CashBalance" (some versions use "CashBalance")
            if v.tag in ("$LEDGER-CashBalance", "CashBalance") and v.currency not in ("USD", "BASE", ""):
                cash[v.currency] = _f(v.value)
        for p in ib.positions():
            c = p.contract
            if c.secType != "CASH" or not _mine(p.account) or not _f(p.position):
                continue
            base, quote = fx.split_pair(key_of_contract(c))
            if "USD" in (base, quote):
                continue
            if base in cash:
                cash[base] -= _f(p.position)
            if quote in cash:
                cash[quote] += _f(p.position) * _f(p.avgCost)        # the quote leg was paid at the entry price
    except Exception:
        return {}
    out = {}
    for cur, bal in cash.items():
        k = f"FX:{_usd_pair(cur)}"
        if k.startswith("FX:USD"):
            # The xxx balance also holds realized P&L of earlier trades, so it only approximates the position. The open
            # trades of every history book (nodes + manual) give the quantity; the cash (÷ their signed average entry, else
            # today's price) cross-checks it. Without books, or when they disagree, the cash-derived value is used.
            books_q, books_px = _open_books(active_account(), k)
            mp = _mark(_tickers.get(k))
            px = books_px if books_px and (not mp or abs(books_px / mp - 1) <= 0.1) else mp    # implausible history price: today's
            cash_q = float(round(-bal / px)) if px else None
            tol = max(1000.0, 0.02 * abs(books_q))
            warn = None
            recent = _recent_fill(active_account(), k)     # cash balance lags fills by up to ~3 min: books win meanwhile
            if (books_q or recent) and (cash_q is None or abs(books_q - cash_q) <= tol or recent
                                        or _books_match_nodes(active_account(), k)):
                qty = float(round(books_q))            # books win; the cash value only flags a difference
                if cash_q is not None and abs(cash_q - qty) > tol and not recent:
                    warn = f"cash balance implies {cash_q:+,.0f}; possible trade outside QOS"
                    if _cash_warn_sent.get(k) != _today():          # once per key and trading day
                        _cash_warn_sent[k] = _today()
                        notify.fire(f"Reconciliation warning {k}: history {qty:+,.0f}, {warn}", "error")
            else:
                qty = cash_q
            out[k] = {"ccy": cur, "cash": bal, "qty": qty, "cash_qty": cash_q, "cash_warn": warn}
            continue
        else:
            qty = float(round(bal))
        out[k] = {"ccy": cur, "cash": bal, "qty": qty}
    return out


CASH_LAG_SEC = 300                # IBKR updates CashBalance about every 3 minutes


def _recent_fill(acct: str, key: str) -> bool:
    """A fill for this instrument within CASH_LAG_SEC: the cash balance may not include it yet"""
    try:
        ts = history.last_fill_ts(acct or None, key)
    except Exception:
        return False
    return ts is not None and (datetime.now(timezone.utc) - ts).total_seconds() < CASH_LAG_SEC


def _books_match_nodes(acct: str, key: str) -> bool:
    """The nodes' history books equal their live heartbeat positions (≥ 1 node involved): then the books are trusted over
    the cash balance, which also holds realized P&L"""
    try:
        live = {nid: bus.live(nid) for nid in set(nm.nodes) | set(bus.nodes)}
        npos = {n: d["qty"] for n, d in attr.node_positions(live).get(key, {}).items()}
        hist = {b[5:]: q for b, q in (history.book_positions(acct or None).get(key) or {}).items() if b.startswith("node:") and abs(q) > 1e-9}
    except Exception:
        return False
    nodes = set(npos) | set(hist)
    return bool(nodes) and all(abs(npos.get(n, 0.0) - hist.get(n, 0.0)) <= 1e-6 + 1e-9 * abs(hist.get(n, 0.0)) for n in nodes)


def _open_books(acct: str, key: str) -> tuple[float, float | None]:
    """One instrument across all history books (nodes + manual): (net signed qty still open, average entry weighted by the
    SIGNED quantity still open). Quantities come from the summed fills (book_positions), not from each trade's opened size
    (a partly closed trade still says its full size); each book's open trade gives its entry price (average-cost method)."""
    try:
        books = {b: q for b, q in (history.book_positions(acct or None).get(key) or {}).items() if abs(q) > 1e-9}
        entry = {attr.book_of(t["source"]): _f(t["entry_price"])
                 for t in history.list_trades("all", key=key, limit=100000, account=acct or None) if t["open"]}
    except Exception:
        return 0.0, None
    net = sum(books.values())
    priced = {b: q for b, q in books.items() if entry.get(b, 0.0) > 0}
    pq = sum(priced.values())
    px = sum(q * entry[b] for b, q in priced.items()) / pq if abs(pq) > 1e-9 else None
    return net, (px if px and px > 0 else None)


FX_CASH_RESIDUE_USD = 1000.0      # cash-only FX rows below this USD value without any open book / node position are P&L residue


def _fx_residue(key: str, cash: float, acct: str) -> bool:
    """A currency balance that is leftover P&L (e.g. JPY profit of a closed USD/JPY round trip), not a position:
    no open position in any history book or live node for the pair, and worth less than FX_CASH_RESIDUE_USD"""
    try:
        if any(abs(v) > 1e-6 for v in (history.book_positions(acct).get(key) or {}).values()):
            return False
        live = {nid: bus.live(nid) for nid in set(nm.nodes) | set(bus.nodes)}
        if attr.node_positions(live).get(key):
            return False
    except Exception:
        return False
    ccy = fx.split_pair(key)[1] if key.startswith("FX:USD") else fx.split_pair(key)[0]
    r = fx.usd_rate(ccy, _usd_rates())
    if r is None and key.startswith("FX:USD"):
        mp = _mark(_tickers.get(key))
        r = 1.0 / mp if mp else None
    return r is not None and abs(cash) * r < FX_CASH_RESIDUE_USD


def _fx_cash(k: str) -> float | None:
    """Real FX exposure for XXXUSD pairs = the account's cash balance in XXX (cross-pair legs removed, whole units)
    (CASH positions from IBKR reqPositions are virtual amounts that may not match real cash, e.g. they vanish after a gateway restart and recount from zero)
    Returns None if there is no balance data for that currency, and always for USDxxx pairs (see _fx_usd_base_qty)"""
    mkt, sym = parse_key(k)
    if mkt != "FX" or not sym.endswith("USD") or sym.startswith("USD"):
        return None
    e = _fx_cash_map().get(f"FX:{sym}")
    return e["qty"] if e else None


def _fx_usd_base_qty(k: str, virtual: float) -> float | None:
    """Position for a USDxxx pair (USDJPY, USDCHF …). IBKR keeps it as a virtual CASH position that resets to zero on every
    gateway restart and then counts only the trades made since: after the nightly restart a sell that closes a +20,000 long
    shows −20,000 although the account is flat. The virtual value is trusted only while it equals the open history books
    (no restart since those trades); otherwise the cash-balance view of _fx_cash_map (books cross-checked against
    −cash / price). None = no balance data or no price: callers keep the virtual position."""
    e = _fx_cash_map().get(k)
    if not e or e.get("qty") is None:
        return None
    if virtual and abs(virtual - _open_books(active_account(), k)[0]) < 1:
        return virtual
    return e["qty"]


def position_qty(key: str) -> float:
    mkt, sym = parse_key(key)
    k = f"{mkt}:{sym}"
    virtual = sum(_f(p.position) for p in ib.positions() if key_of_contract(p.contract) == k and _mine(p.account))
    if mkt == "FX" and sym.startswith("USD"):
        cash = _fx_usd_base_qty(k, virtual)
    else:
        cash = _fx_cash(k)
    if cash is None or cash == virtual:
        return virtual
    if cash:                                                  # same rule as /positions: leftover P&L cash is not a position
        e = _fx_cash_map().get(k) or {}
        if _fx_residue(k, e.get("cash", 0.0), active_account()):
            return 0.0
    return cash


async def submit_order(*a, **kw) -> dict:
    """One order at a time: check → placeOrder is atomic for every path (manual, confirm, reverse). Not re-entrant."""
    async with _order_lock:
        return await _submit_order_impl(*a, **kw)


async def _submit_order_impl(order_ref: str, key: str, side: str, qty: float, order_type: str = "MKT",
                       price: float | None = None, tif: str = "DAY",
                       take_profit: float | None = None, stop_loss: float | None = None,
                       trail_amount: float | None = None, cash_qty: float | None = None) -> dict:
    """The only path for orders to reach IBKR; manual and agent orders go through the same risk checks
    Supports MKT / LMT / STP / TRAIL and bracket (take_profit + stop_loss as an OCA group)"""
    await ensure_connected()
    mkt, sym = parse_key(key)
    k = f"{mkt}:{sym}"
    if not market_tradable(mkt):
        raise HTTPException(400, f"{k}: {market_block_reason(mkt)}")
    for name, v in (("qty", qty), ("price", price), ("take_profit", take_profit), ("stop_loss", stop_loss),
                    ("trail_amount", trail_amount), ("cash_qty", cash_qty)):
        if v is not None and not math.isfinite(v):
            raise HTTPException(400, f"{name} must be a finite number")
    if qty <= 0:
        raise HTTPException(400, "Quantity must be positive")
    if mkt == "FX":
        if qty < 1 or abs(qty - round(qty)) > 1e-9:
            raise HTTPException(400, f"FX quantity is in units of {sym[:3]} (whole number): 0.01 lot = 1000, 0.1 lot = 10000, 1 lot = 100000 — got {qty:g}")
        if qty < 1000:
            raise HTTPException(400, f"FX quantity {qty:g} is below IBKR's practical minimum (use at least 1000 units = 0.01 lot)")
    if mkt == "STK" and abs(qty - round(qty)) > 1e-9:
        raise HTTPException(400, "Stock quantity must be a whole number of shares")
    if mkt == "CRYPTO":
        # IBKR/Paxos rules: Market must be IOC, only Limit can set TIF, no Stop/Trailing/bracket
        if order_type in ("STP", "TRAIL"):
            raise HTTPException(400, "IBKR crypto supports only Market and Limit orders (no Stop / Trailing)")
        if take_profit is not None or stop_loss is not None:
            raise HTTPException(400, "Bracket (TP/SL) is not available for crypto at IBKR — manage exits manually or with price alerts")
        if order_type == "MKT":
            tif = "IOC"
        qty = round(qty, 8)
    c = await contract_for(k)
    tick = await _min_tick(c)
    if tick:                                     # IBKR rejects prices off the tick grid: nearest tick, as in modify_order
        price, take_profit, stop_loss, trail_amount = (_to_tick(v, tick) for v in (price, take_profit, stop_loss, trail_amount))
    t = await ticker_for(k)
    ref = _px(t.ask if side == "BUY" else t.bid) or _px(t.last) or _px(t.close)
    if ref == 0.0:
        raise HTTPException(409, "No reference price (market closed?) — order not sent")

    if cash_qty and mkt == "CRYPTO":
        qty = round(cash_qty / ref, 8)          # convert cash amount to coins (used for Limit/Sell; Market buy sends cash directly)
    elif cash_qty:
        raise HTTPException(400, "cash_qty is only supported for crypto")
    usd_per_unit = await _usd_per_unit(mkt, sym, ref)
    notional = qty * usd_per_unit
    try:
        await risk_gate(notional, k)
        _check_position(k, side, qty, usd_per_unit)
    except HTTPException as e:
        audit("order_rejected", ref=order_ref, key=k, side=side, qty=qty, type=order_type, reason=e.detail)
        raise
    if order_type in ("LMT", "STP"):
        if not price:
            raise HTTPException(400, "Price is required for LMT/STP orders")
        if abs(price - ref) / ref > 0.05:
            raise HTTPException(400, "Price is more than 5% away from market — please check")
    if order_type == "TRAIL":
        if not trail_amount or trail_amount <= 0:
            raise HTTPException(400, "Trail amount is required for trailing stop")
        if trail_amount / ref > 0.05:
            raise HTTPException(400, "Trail amount is more than 5% of the price — please check")

    # check that the bracket makes sense for the direction
    entry_ref = price if order_type in ("LMT", "STP") and price else ref
    if take_profit is not None or stop_loss is not None:
        if side == "BUY":
            if take_profit is not None and take_profit <= entry_ref:
                raise HTTPException(400, "Take profit must be above entry for a BUY")
            if stop_loss is not None and stop_loss >= entry_ref:
                raise HTTPException(400, "Stop loss must be below entry for a BUY")
        else:
            if take_profit is not None and take_profit >= entry_ref:
                raise HTTPException(400, "Take profit must be below entry for a SELL")
            if stop_loss is not None and stop_loss <= entry_ref:
                raise HTTPException(400, "Stop loss must be above entry for a SELL")

    if order_type == "MKT":
        parent = MarketOrder(side, qty, tif=tif)
        if mkt == "CRYPTO" and side == "BUY":
            # IBKR/Paxos: crypto Market BUY must be specified as a cash amount (Cash Quantity), not a coin quantity
            parent = MarketOrder(side, 0, tif="IOC", cashQty=round(cash_qty if cash_qty else qty * ref, 2))
    elif order_type == "LMT":
        parent = LimitOrder(side, qty, price, tif=tif)
    elif order_type == "STP":
        parent = StopOrder(side, qty, price, tif=tif)
    else:
        parent = Order(action=side, totalQuantity=qty, orderType="TRAIL", auxPrice=trail_amount, tif=tif)
    parent.orderRef = order_ref

    children: list[Order] = []
    if take_profit is not None or stop_loss is not None:
        parent.orderId = ib.client.getReqId()
        parent.transmit = False
        exit_side = "SELL" if side == "BUY" else "BUY"
        oca = f"oca-{parent.orderId}"
        if take_profit is not None:
            tp = LimitOrder(exit_side, qty, take_profit, tif="GTC")
            tp.parentId = parent.orderId; tp.ocaGroup = oca; tp.ocaType = 1; tp.transmit = False
            tp.orderRef = order_ref
            children.append(tp)
        if stop_loss is not None:
            sl = StopOrder(exit_side, qty, stop_loss, tif="GTC")
            sl.parentId = parent.orderId; sl.ocaGroup = oca; sl.ocaType = 1; sl.transmit = False
            sl.orderRef = order_ref
            children.append(sl)
        children[-1].transmit = True        # the last one transmits the whole group

    acct = active_account()
    if acct:
        parent.account = acct
        for ch in children:
            ch.account = acct
    trade = ib.placeOrder(c, parent)
    child_trades = [ib.placeOrder(c, ch) for ch in children]
    for _ in range(20):
        await asyncio.sleep(0.2)
        if trade.isDone() or trade.orderStatus.status in ("Submitted", "PreSubmitted"):
            break
    st = trade.orderStatus
    audit("order", ref=order_ref, key=k, side=side, qty=qty, type=order_type, price=price,
          tp=take_profit, sl=stop_loss, trail=trail_amount, order_id=trade.order.orderId, status=st.status,
          avg_fill_price=_f(st.avgFillPrice))
    msg = trade.log[-1].message if trade.log else ""
    if st.status in ("Cancelled", "Inactive", "ApiCancelled"):
        await asyncio.sleep(0.5)          # wait for the IBKR reason message, which often arrives after the status
        reason = _order_errors.get(trade.order.orderId) or next((e.message for e in reversed(trade.log) if e.message), "")
        if reason:
            msg = reason
        audit("order_rejected_by_broker", ref=order_ref, key=k, side=side, qty=qty, type=order_type, order_id=trade.order.orderId, status=st.status, reason=msg)
        if any(pat in msg.lower() for pat in BROKER_BLOCK_PATTERNS):
            block_market_from_broker(mkt, msg)
    if getattr(parent, "cashQty", 0):
        msg = f"cash quantity {parent.cashQty:,.2f} USD (≈ {qty:g} {sym}) · {msg}".strip(" ·")
    return {"order_id": trade.order.orderId, "status": st.status, "filled": _f(st.filled),
            "avg_fill_price": _f(st.avgFillPrice), "message": msg,
            "child_order_ids": [ct.order.orderId for ct in child_trades]}


def load_mode() -> str:
    try:
        return json.loads(MODE_FILE.read_text()).get("mode", "manual")
    except Exception:
        return "manual"


def save_mode(mode: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODE_FILE.write_text(json.dumps({"mode": mode}))


# ---------- history recorder ----------
_comm_ccy_warned: set[str] = set()


def _commission_usd(amount: float, ccy: str | None) -> float:
    """Commission in USD (IBKR charges some in the quote currency, e.g. JPY). Unknown rate → raw value, audited once per currency"""
    ccy = (ccy or "").upper()
    if not amount or ccy in ("", "USD", "BASE"):
        return amount
    r = fx.usd_rate(ccy, _usd_rates())
    if r is None:
        if ccy not in _comm_ccy_warned:
            _comm_ccy_warned.add(ccy)
            audit("commission_unconverted", currency=ccy, amount=amount)
        return amount
    return amount * r


def _fill_rows() -> list[dict]:
    rows = []
    for f in ib.fills():
        e, cr = f.execution, f.commissionReport
        if not _mine(e.acctNumber or ""):
            continue
        key = key_of_contract(f.contract)
        rows.append({
            "exec_id": e.execId, "account": e.acctNumber or "", "ts": (e.time if e.time.tzinfo else e.time.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat() if e.time else datetime.now(timezone.utc).isoformat(),
            "key": key, "symbol": key.split(":", 1)[1], "side": "BUY" if e.side == "BOT" else "SELL",
            "qty": _f(e.shares), "price": _f(e.price),
            "commission": _commission_usd(_f(cr.commission), getattr(cr, "currency", "")) if cr and abs(_f(cr.commission)) < 1e15 else 0.0,
            "realized_pnl": _f(cr.realizedPNL) if cr and cr.realizedPNL and abs(_f(cr.realizedPNL)) < 1e15 else 0.0,
            "order_id": e.orderId, "order_ref": e.orderRef or "manual",
        })
    return rows


_seen_fills: set[str] = set()
_day_state: dict = {}


async def recorder_loop():
    """Every 10 seconds: record new fills / every 60 seconds: record equity"""
    last_eq = 0.0
    import time as _t
    while True:
        try:
            if ib.isConnected():
                rows = _fill_rows()
                new_ids = {r["exec_id"] for r in rows} - _seen_fills
                if _seen_fills and new_ids:
                    for r in rows:
                        if r["exec_id"] in new_ids and not attr.is_nautilus_ref(r["order_ref"]):   # node fills are already notified via the event bus
                            notify.fire(f"FILL {r['side']} {r['qty']:g} {r['symbol']} @ {r['price']} ({r['order_ref']})", "fill")
                _seen_fills.update(r["exec_id"] for r in rows)
                try:
                    history.set_fx_rates(_usd_rates())          # cross-pair (EUR/GBP, EUR/JPY) P&L needs USD rates
                except Exception as e:
                    print("recorder: fx rates:", e)
                history.upsert_fills(rows)
                today = _today()
                if _day_state.get("day") and _day_state["day"] != today:
                    end = _day_start()                                # the trading day that just ended: 17:00 NY → 17:00 NY
                    beg = _day_start(end - timedelta(minutes=1))
                    done = [t for t in history.list_trades("all", limit=100000, account=active_account())
                            if not t["open"] and t["exit_ts"] and t["net_pnl"] is not None
                            and beg <= datetime.fromisoformat(t["exit_ts"]) < end]
                    wins = sum(1 for t in done if t["net_pnl"] > 0)
                    notify.fire(f"Daily summary {_day_state['day']}: realized P&L {sum(t['net_pnl'] for t in done):,.2f} USD · trades {len(done)} · "
                                f"win rate {(wins / len(done) * 100) if done else 0:.0f}%", "daily")
                _day_state["day"] = today
                if _t.time() - last_eq >= 60:
                    vals = await account_values()
                    nl = _f(vals.get("NetLiquidation"))
                    if nl > 0:
                        history.insert_equity(nl, _f(vals.get("UnrealizedPnL")), _f(vals.get("RealizedPnL")), vals.get("account_id", ""))
                        last_eq = _t.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("recorder:", e)
        await asyncio.sleep(10)


# ---------- connection state, reconciliation, websocket hub ----------
_conn = {"state": "disconnected", "last_reconcile": None, "reconnects": 0, "last_error": ""}
_ws_clients: set[WebSocket] = set()
UI_FILE = DATA_DIR / "ui.json"


async def ws_broadcast(msg: dict) -> None:
    if not _ws_clients:
        return
    data = json.dumps(msg, default=str)
    dead = []
    for ws in list(_ws_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


async def reconcile() -> dict:
    """After reconnecting: pull positions/orders from the broker so they match the system"""
    await ib.reqPositionsAsync()
    await ib.reqOpenOrdersAsync()
    await ib.reqAllOpenOrdersAsync()
    _tickers.clear()
    for it in load_watchlist():
        try:
            await ticker_for(it["key"])
        except Exception:
            pass
    pos = [(key_of_contract(p.contract), _f(p.position)) for p in ib.positions() if _f(p.position) != 0 and _mine(p.account)]
    info = {"positions": len(pos), "open_orders": len(ib.openTrades()), "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _conn["last_reconcile"] = info
    audit("reconcile", **info)
    return info


GATEWAY_DOWN_ALERT_SEC = int(os.getenv("QOS_GATEWAY_DOWN_ALERT_SEC", "900"))
_gw_down = {"since": None, "alerted": False}


async def _gateway_down_check() -> None:
    """IB Gateway unreachable for 15+ min (stuck at a dialog after its nightly restart, logged out, container down) → one alert"""
    now = time.time()
    if _gw_down["since"] is None:
        _gw_down["since"] = now
    if ib_guard.down_alert_due(_gw_down["since"], now, _gw_down["alerted"], GATEWAY_DOWN_ALERT_SEC):
        _gw_down["alerted"] = True
        mins = int((now - _gw_down["since"]) // 60)
        await warn(f"IB Gateway unreachable for {mins} min — nodes cannot trade or cancel. Check the gateway "
                   f"(docker compose restart in the ibgw folder clears a stuck login dialog)", down_min=mins,
                   last_error=_conn.get("last_error", ""))


async def _gateway_back() -> None:
    if _gw_down["alerted"]:
        mins = int((time.time() - (_gw_down["since"] or time.time())) // 60)
        await warn(f"IB Gateway reachable again after {mins} min", "info", down_min=mins)
    _gw_down["since"], _gw_down["alerted"] = None, False


async def connection_watcher():
    """Check every 5 seconds: reconnect if dropped + reconcile; agents pause while disconnected"""
    was_connected = ib.isConnected()
    while True:
        try:
            if _setup["state"] == "starting":
                was_connected = False
                await asyncio.sleep(5)
                continue
            if ib.isConnected():
                await _gateway_back()
                if _conn["state"] != "connected":
                    _conn["state"] = "connected"
                    await ws_broadcast({"type": "conn", **_conn, "ib_connected": True})
                was_connected = True
                # stream empty while connected (seen after the daily gateway restart) → re-request, at most every 60 s
                import time as _t
                if not ib.accountValues() and _t.time() - _conn.get("last_refresh", 0) > 60:
                    _conn["last_refresh"] = _t.time()
                    try:
                        r = await ib_refresh()
                        print("auto ib_refresh:", r)
                    except Exception as e:
                        print("auto ib_refresh failed:", e)
            else:
                if was_connected:
                    _conn["state"] = "reconnecting"
                    audit("ib_disconnected")
                    notify.fire("IB Gateway connection lost — reconnecting", "error")
                    await ws_broadcast({"type": "conn", **_conn, "ib_connected": False})
                was_connected = False
                await _gateway_down_check()
                try:
                    await ensure_connected()
                    await _gateway_back()
                    _conn["reconnects"] += 1
                    info = await reconcile()
                    _conn["state"] = "connected"
                    _conn["last_error"] = ""
                    audit("ib_reconnected", **info)
                    notify.fire(f"IB Gateway reconnected · {info['positions']} positions · {info['open_orders']} open orders", "error")
                    await ws_broadcast({"type": "conn", **_conn, "ib_connected": True})
                except Exception as e:
                    _conn["last_error"] = str(e)[:200]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("watcher:", e)
        await asyncio.sleep(5)


async def ws_pusher():
    """Push quotes every 1 second, snapshot every 3 seconds, check price alerts"""
    n = 0
    while True:
        try:
            if _ws_clients and ib.isConnected():
                q = await quotes()
                await ws_broadcast({"type": "quotes", "quotes": q["quotes"]})
                for a in alerts_mod.check(q["quotes"]):
                    text = f"ALERT {a['key']} {a['kind']} {a['level']:g} hit at {a.get('last_price'):.5g} {a.get('note') or ''}"
                    audit("price_alert", **{k: a[k] for k in ("id", "key", "kind", "level")})
                    notify.fire(text, "order")
                    await ws_broadcast({"type": "alert", "alert": a, "text": text})
                if n % 3 == 0:
                    async def _part(name, coro, key=None, default=None):
                        try:
                            r = await coro
                            return r[key] if key else r
                        except Exception as e:
                            print(f"ws pusher: {name} failed: {e}")
                            return default
                    acct = await _part("account", account(), default=None)
                    pos = await _part("positions", positions(), default={})
                    snap = {"account": acct or {}, "account_ok": acct is not None,          # False → the Terminal shows the last values as stale
                            "positions": pos.get("positions", []), "reconcile": pos.get("reconcile"),
                            "orders": await _part("orders", orders(), "orders", []),
                            "fills": await _part("fills", fills(30), "fills", []),
                            "ib_connected": ib.isConnected(), "mode": load_mode(), "running_nodes": _running_nodes()}
                    if snap["reconcile"] is None:
                        del snap["reconcile"]                    # missing key = "keep the last known" in the Terminal (null would not parse)
                    await ws_broadcast({"type": "snapshot", **snap})
            elif _ws_clients and n % 5 == 0:                 # IB down: keep telling the Terminal instead of going silent
                await ws_broadcast({"type": "conn", **_conn, "ib_connected": False})
            n += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("ws pusher:", e)
        await asyncio.sleep(1)


def _on_exec(trade, fill):
    try:
        key = key_of_contract(fill.contract)
        asyncio.get_running_loop().create_task(ws_broadcast({"type": "fill", "key": key, "side": fill.execution.side,
            "qty": _f(fill.execution.shares), "price": _f(fill.execution.price), "ref": trade.order.orderRef or "manual"}))
    except Exception:
        pass


_order_errors: dict[int, str] = {}


def _on_ib_error(reqId, errorCode, errorString, contract):
    """Store order error messages (reqId = orderId) to show the reason; 1102 = connection restored → refresh"""
    if reqId and reqId > 0 and errorCode not in (399, 404, 2104, 2106, 2158):
        _order_errors[reqId] = f"Error {errorCode}: {errorString}"
        if len(_order_errors) > 500:
            for k in list(_order_errors)[:100]:
                _order_errors.pop(k, None)
    if errorCode == 1102:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(ib.reqPositionsAsync())
            loop.create_task(ib.reqOpenOrdersAsync())
            _acct_cache.update(ts=0.0, values={})
        except Exception:
            pass


def _on_order_status(trade):
    try:
        asyncio.get_running_loop().create_task(ws_broadcast({"type": "order_status", "order_id": trade.order.orderId,
            "status": trade.orderStatus.status, "key": key_of_contract(trade.contract)}))
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    global IB_PORT, _main_loop
    IB_PORT = gateway_port()   # port of the selected gateway profile (paper/live/different user)
    try:
        await ensure_connected()
        for it in load_watchlist():
            try:
                await ticker_for(it["key"])
            except Exception as e:
                print("warmup skip", it, e)
    except Exception as e:
        print("warning: IB not ready at startup:", e)
    if ib.isConnected() and ib.managedAccounts():
        os.environ["IB_ACCOUNT_ID"] = active_account() or ib.managedAccounts()[0]
    from dotenv import load_dotenv as _ld
    _ld(paths.env_file())
    try:   # previously rejected by IBKR for regulatory reasons (from audit) → set to view-only
        for rec in read_audit(500):
            if rec.get("event") == "order_rejected_by_broker" and any(p in str(rec.get("reason", "")).lower() for p in BROKER_BLOCK_PATTERNS):
                mk = str(rec.get("key", "")).split(":")[0]
                if mk in MARKET_BY_ID and not (_risk_doc()["default"].get("markets_manual") or {}).get(mk):
                    block_market_from_broker(mk, str(rec.get("reason")))
    except Exception as e:
        print("broker block seed:", e)
    # Nautilus nodes: adopt processes still running, then start the ones that should run (unless the account is halted)
    try:
        adopted = nm.adopt()
        resumed, failed = ([], {}) if load_risk().get("halted") else nm.resume()
        for nid in adopted + resumed:                        # adopted / resumed processes emit no node_started: record their version
            _note_soak_ref(nid)
        if adopted or resumed or failed:
            audit("nodes_resume", adopted=adopted, resumed=resumed, failed=failed)
            if resumed or failed:
                notify.fire(f"Nautilus nodes · running: {', '.join(adopted + resumed) or 'none'}"
                            + (f" · failed: {', '.join(failed)}" if failed else ""), "agent")
        print("nodes adopted:", adopted, "| resumed:", resumed, "| failed:", failed)
    except Exception as e:
        print("node resume failed:", e)
    _main_loop = asyncio.get_running_loop()
    notify.set_loop(_main_loop)
    nm.start_supervisor()
    bus.start()
    risk_task = asyncio.create_task(risk_loop())
    ramp_task = asyncio.create_task(ramp_loop())
    wake_task = asyncio.create_task(wake_loop())
    backup_task = asyncio.create_task(backup_loop())
    try:
        gap = wake_watch.startup()
        if gap:
            asyncio.create_task(_on_machine_gap(gap, delay=60))
    except Exception as e:
        print("wake startup:", e)
    soak_task = asyncio.create_task(soak_loop())
    cloud_task = asyncio.create_task(cloud_api.cloud_loop())
    feed_task = asyncio.create_task(cloud_api.feed_loop())
    rec_task = asyncio.create_task(recorder_loop())
    watch_task = asyncio.create_task(connection_watcher())
    push_task = asyncio.create_task(ws_pusher())
    heat_task = asyncio.create_task(heat_prefetch_loop())
    ib.execDetailsEvent += _on_exec
    ib.orderStatusEvent += _on_order_status
    ib.errorEvent += _on_ib_error
    _conn["state"] = "connected" if ib.isConnected() else "reconnecting"
    if ib.isConnected() and load_session().get("account_id"):
        asyncio.create_task(_safe_probe())
    _legacy = agents_migration.pending()
    if _legacy:
        print("legacy agents not migrated yet:", _legacy, "— POST /agents/migrate")
    print("mode:", load_mode(), "| nodes:", nm.brief())
    yield
    rec_task.cancel(); watch_task.cancel(); push_task.cancel(); heat_task.cancel(); bus.stop(); risk_task.cancel(); soak_task.cancel(); ramp_task.cancel()
    wake_task.cancel(); backup_task.cancel(); cloud_task.cancel(); feed_task.cancel(); power.update(False)
    if ib.isConnected():
        ib.disconnect()


app = FastAPI(title="QOS API", version=QOS_VERSION, lifespan=lifespan)
# no CORS: the Terminal calls the API from Rust (no Origin header); browser pages may not write (operator_guard)
TRUSTED_ORIGINS = {o.strip() for o in os.getenv("QOS_TRUSTED_ORIGINS", "").split(",") if o.strip()}


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# Host header names the API answers to (DNS-rebinding guard). "testserver" is Starlette's TestClient host: a single-label
# name that public DNS cannot serve, so an attacker's page cannot use it. Add LAN names / IPs with QOS_TRUSTED_HOSTS.
TRUSTED_HOSTS = {*LOCAL_HOSTS, *(h.strip().lower() for h in os.getenv("QOS_TRUSTED_HOSTS", "").split(",") if h.strip())}


def _test_hosts() -> set[str]:
    """Starlette's TestClient sends Host 'testserver': trusted only while tests run (pytest sets PYTEST_CURRENT_TEST)
    or with an explicit QOS_TEST_HOSTS=1"""
    return {"testserver"} if os.getenv("QOS_TEST_HOSTS") == "1" or os.getenv("PYTEST_CURRENT_TEST") else set()


def _local_origin(origin: str | None) -> bool:
    """No Origin (native clients), the Terminal's own webview (dioxus://… or http://dioxus.index.html), a page served
    from this computer (localhost / 127.0.0.1 / ::1) or an explicitly trusted origin — exact matches only"""
    if not origin or origin in TRUSTED_ORIGINS:
        return True
    from urllib.parse import urlsplit
    try:
        u = urlsplit(origin)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    return u.scheme == "dioxus" or host == "dioxus.index.html" or host in LOCAL_HOSTS


def _host_ok(host_header: str | None) -> bool:
    """DNS-rebinding guard: the Host header must name this computer (or a trusted host); no Host = native client"""
    if not host_header:
        return True
    h = host_header.strip().lower()
    if any(c in h for c in "@/\\ ?#"):                      # userinfo / path tricks: never a plain host[:port]
        return False
    port = ""
    if h.startswith("["):                                   # [::1]:8000
        end = h.find("]")
        if end < 0:
            return False
        h, rest = h[1:end], h[end + 1:]
        if rest and not rest.startswith(":"):
            return False
        port = rest[1:]
    elif ":" in h:
        if h.count(":") != 1:
            return False
        h, port = h.split(":")
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        return False
    return h in TRUSTED_HOSTS or h in _test_hosts()


# ---------- status / account ----------
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")


async def _redis_ping() -> str:
    """Redis status (event bus for Nautilus) — optional: if not enabled, nothing else is affected"""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5)
        try:
            return "ok" if await r.ping() else "down"
        finally:
            await r.aclose()
    except Exception:
        return "down"


def _running_nodes() -> list[str]:
    return sorted(nid for nid, st in nm.brief().items() if st == "running")


@app.get("/health")
async def health():
    r = load_risk()
    return {"ok": True, "ib_connected": ib.isConnected(), "mode": load_mode(),
            "conn": _conn, "halted": bool(r.get("halted")),
            "active_agents": _running_nodes(), "running_nodes": _running_nodes(),
            "ws_clients": len(_ws_clients), "version": app.version, "redis": await _redis_ping(),
            "nautilus": {"gateway": nb.resolve_gateway(), "managed": nm.brief(), "events": {"connected": bus.connected, "received": bus.received},
                         "client_ids": nb._load_clients()},
            "time_utc": datetime.now(timezone.utc).isoformat()}


@app.get("/account")
async def account():
    await ensure_connected()
    acct = active_account()
    if not acct:
        raise HTTPException(503, "no managed accounts")
    want = {"NetLiquidation", "AvailableFunds", "BuyingPower", "UnrealizedPnL", "RealizedPnL", "TotalCashValue"}
    out = {"account_id": acct, "currency": "USD"}
    rows = await acct_summary(acct)
    base = next((str(v.value) for v in rows if v.tag == "Currency" and str(v.value) not in ("", "BASE")), "USD")   # BASE rows: account base currency
    for v in rows:
        if v.tag in want:
            out[v.tag] = _f(v.value)
            if v.currency:
                out["currency"] = base if v.currency == "BASE" else v.currency
    return out


# ---------- markets / instruments / watchlist ----------
@app.get("/markets")
async def markets():
    return {"markets": [{**m, "tradable": market_tradable(m["id"]), "base_tradable": m["tradable"]} for m in MARKETS]}


class MarketTradableBody(BaseModel):
    market: Literal["FX", "STK", "CRYPTO"]
    tradable: bool


PROBE_SYMBOL = {"CRYPTO": "CRYPTO:BTC", "FX": "FX:EURUSD", "STK": "STK:SPY"}


async def probe_market(mkt: str) -> dict:
    """Check whether this account/country can trade this market using a what-if order (IBKR checks permissions as if real but creates no order)"""
    await ensure_connected()
    key = PROBE_SYMBOL[mkt]
    c = await contract_for(key)
    t = await ticker_for(key)
    ref = _px(t.last) or _px(t.close) or _px(t.ask) or 0.0
    qty = {"CRYPTO": 0.0001, "FX": 25000.0, "STK": 1.0}[mkt]
    # Limit far below the market: would not fill even if it slipped through (and whatIf=True never sends it)
    order = LimitOrder("BUY", qty, round(ref * 0.5, 2) if ref else 1.0, tif="DAY")
    if mkt == "CRYPTO":
        order.tif = "IOC"
    order.account = active_account() or ""
    try:
        state = await asyncio.wait_for(ib.whatIfOrderAsync(c, order), 15)
        warn = (getattr(state, "warningText", "") or "")
        ok = "reject" not in warn.lower() and "not allowed" not in warn.lower()
        return {"market": mkt, "allowed": ok, "reason": warn or "what-if accepted", "init_margin": getattr(state, "initMarginChange", "")}
    except Exception as e:
        msg = str(e)
        return {"market": mkt, "allowed": False, "reason": msg[-200:]}


async def _safe_probe():
    try:
        await asyncio.sleep(3)
        await probe_markets(None, True)
    except Exception as e:
        print("markets probe:", e)


@app.post("/risk/markets/probe")
async def probe_markets(market: str | None = None, apply: bool = True, force: bool = False):
    """Check trading permissions with IBKR (default: all markets) and set markets_tradable automatically
    - allowed → tradable as normal (unless an admin disabled it first)
    - not allowed (e.g. Regulatory Restrictions) → view-only"""
    targets = [market] if market else list(PROBE_SYMBOL)
    results = []
    doc = _risk_doc()
    ov = doc["default"].setdefault("markets_tradable", {})
    manual = doc["default"].setdefault("markets_manual", {})     # markets set manually by an admin are not overwritten by the probe
    for m in targets:
        r = await probe_market(m)
        results.append(r)
        blocked = doc["default"].get("markets_blocked_by_broker") or {}
        if m in blocked:
            r["allowed"] = False
            r["reason"] = f"blocked by IBKR on {blocked[m].get('ts', '')[:10]}: {blocked[m].get('reason', '')}"
        if apply and (force or not manual.get(m)):
            ov[m] = bool(r["allowed"])
            if force:
                manual.pop(m, None)
    doc["default"]["markets_probe"] = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "account": active_account(), "results": results}
    _save_risk_doc(doc)
    audit("markets_probe", results={r["market"]: r["allowed"] for r in results})
    return {"results": results, "markets": {m["id"]: market_tradable(m["id"]) for m in MARKETS}}


@app.put("/risk/markets")
async def put_market_tradable(body: MarketTradableBody):
    """Admin enables/disables order sending for a market (e.g. enable crypto once IBKR allows it)"""
    doc = _risk_doc()
    doc["default"].setdefault("markets_tradable", {})[body.market] = body.tradable
    doc["default"].setdefault("markets_manual", {})[body.market] = True
    if body.tradable:
        (doc["default"].get("markets_blocked_by_broker") or {}).pop(body.market, None)
    _save_risk_doc(doc)
    audit("market_tradable", market=body.market, tradable=body.tradable)
    return {"markets": {m["id"]: market_tradable(m["id"]) for m in MARKETS}}


def _inst(mkt: str, sym: str, name: str | None = None) -> dict:
    m = MARKET_BY_ID[mkt]
    return {
        "key": f"{mkt}:{sym}", "market": mkt, "symbol": sym,
        "name": name or NAME_BY_KEY.get(f"{mkt}:{sym}", sym),
        "exchange": m["exchange"], "currency": "USD",
        "tradable": market_tradable(mkt), "unit": m["unit"], "decimals": decimals_for(mkt, sym),
    }


@app.get("/instruments")
async def instruments(market: str = "STK", q: str = "", limit: int = 200):
    mkt = market.upper()
    if mkt not in UNIVERSE:
        raise HTTPException(400, f"Unknown market {mkt}")
    ql = q.strip().lower()
    rows = [_inst(mkt, s, n) for s, n in UNIVERSE[mkt] if not ql or ql in s.lower() or ql in n.lower()]

    # search IBKR as well when a search term is typed and it is a stock
    if ql and mkt == "STK" and len(ql) >= 1 and ib.isConnected():
        try:
            seen = {r["symbol"] for r in rows}
            for d in await ib.reqMatchingSymbolsAsync(q.strip()):
                c = d.contract
                if c.secType == "STK" and c.currency == "USD" and c.symbol not in seen:
                    rows.append(_inst("STK", c.symbol, d.derivativeSecTypes and c.symbol or c.symbol))
                    seen.add(c.symbol)
        except Exception as e:
            print("reqMatchingSymbols:", e)
    return {"market": mkt, "instruments": rows[:limit]}


@app.get("/watchlist")
async def get_watchlist():
    out = []
    for it in load_watchlist():
        mkt, sym = parse_key(it["key"])
        out.append(_inst(mkt, sym))
    return {"items": out}


class WatchlistBody(BaseModel):
    keys: list[str]


@app.put("/watchlist")
async def put_watchlist(body: WatchlistBody):
    items, seen = [], set()
    for k in body.keys:
        mkt, sym = parse_key(k)
        key = f"{mkt}:{sym}"
        if key in seen:
            continue
        seen.add(key)
        items.append({"key": key})
    save_watchlist(items)
    return await get_watchlist()


@app.post("/watchlist/{key:path}")
async def add_watch(key: str):
    """Add a symbol (checks with IBKR first that it exists)"""
    await ensure_connected()
    mkt, sym = parse_key(key)
    await contract_for(f"{mkt}:{sym}")
    items = load_watchlist()
    if not any(i["key"] == f"{mkt}:{sym}" for i in items):
        items.append({"key": f"{mkt}:{sym}"})
        save_watchlist(items)
    return await get_watchlist()


@app.delete("/watchlist/{key:path}")
async def remove_watch(key: str):
    mkt, sym = parse_key(key)
    items = [i for i in load_watchlist() if i["key"] != f"{mkt}:{sym}"]
    save_watchlist(items)
    return await get_watchlist()


# ---------- market data ----------
@app.get("/quotes")
async def quotes(keys: str = ""):
    await ensure_connected()
    wanted = [k for k in keys.split(",") if k.strip()] or [i["key"] for i in load_watchlist()]
    rows = []
    for k in wanted:
        try:
            mkt, sym = parse_key(k)
            t = await ticker_for(k)
        except HTTPException:
            continue
        bid, ask, last = _px(t.bid), _px(t.ask), _px(t.last)
        mid = (bid + ask) / 2 if bid and ask else last
        close = _px(t.close)
        pip = pip_for(mkt, sym)
        rows.append(
            {
                "key": f"{mkt}:{sym}", "market": mkt, "symbol": sym,
                "name": NAME_BY_KEY.get(f"{mkt}:{sym}", sym),
                "bid": bid, "ask": ask, "last": last, "mid": mid,
                "spread_pip": (ask - bid) / pip if bid and ask else 0.0,
                "prev_close": close,
                "change_pct": ((mid - close) / close * 100) if close and mid else 0.0,
                "decimals": decimals_for(mkt, sym),
                "tradable": market_tradable(mkt),
                "unit": MARKET_BY_ID[mkt]["unit"],
                "time": t.time.isoformat() if t.time else None,
            }
        )
    return {"as_of": datetime.now(timezone.utc).isoformat(), "quotes": rows}


@app.get("/bars")
async def bars(key: str = "FX:EURUSD", bar_size: str = "1 min", duration: str = "1 D"):
    await ensure_connected()
    mkt, sym = parse_key(key)
    return {"key": f"{mkt}:{sym}", "bar_size": bar_size, "decimals": decimals_for(mkt, sym),
            "bars": await fetch_bars(f"{mkt}:{sym}", bar_size, duration)}


# ---------- market overview (market groups) and heatmap ----------
_heat_cache: dict[str, tuple[float, list[dict]]] = {}
_heat_missing: dict[str, list[str]] = {}
HEAT_TTL = 150.0                 # a snapshot this fresh is served as is
HEAT_PREFETCH_EVERY = 120.0      # while a Terminal is open, every heatmap is rebuilt in the background this often
HEAT_PREFETCH_GAP = 1.0          # pause between groups (market data lines and IBKR pacing)


def _ticker_row(key: str, t) -> dict:
    mkt, sym = parse_key(key)
    last = _px(t.last) or ((_px(t.bid) + _px(t.ask)) / 2 if _px(t.bid) and _px(t.ask) else 0.0) or _px(t.close)
    close = _px(t.close)
    return {"key": f"{mkt}:{sym}", "symbol": sym, "last": last, "prev_close": close,
            "change_pct": ((last - close) / close * 100) if close and last else 0.0,
            "decimals": decimals_for(mkt, sym)}


async def _proxy_quote(key: str | None) -> dict | None:
    if not key:
        return None
    try:
        t = await ticker_for(key)
        return _ticker_row(key, t)
    except Exception:
        return None


@app.get("/market")
async def market():
    """Market group cards: index/ETF price representing each group"""
    await ensure_connected()
    rows = []
    for g in GROUPS:
        q = await _proxy_quote(g["proxy"])
        if (q is None or q["last"] == 0.0) and g.get("alt_proxy"):
            q = await _proxy_quote(g["alt_proxy"]) or q
        q = q or {"key": g["proxy"], "symbol": g["proxy"].split(":")[1], "last": 0.0, "prev_close": 0.0, "change_pct": 0.0, "decimals": 2}
        rows.append({"id": g["id"], "name": g["name"], "subtitle": g["subtitle"], "members": len(g["members"]), **q})
    for s in SCANNERS:                      # scanner cards show the leader from the last scan; scanning happens in /heatmap only
        _, cached = _heat_cache.get(s["id"], (0.0, []))
        lead = scan_leader(cached, s["scan_code"])
        q = ({k: lead[k] for k in ("key", "symbol", "last", "prev_close", "change_pct", "decimals")} if lead else
             {"key": "", "symbol": "", "last": 0.0, "prev_close": 0.0, "change_pct": 0.0, "decimals": 2})
        rows.append({"id": s["id"], "name": s["name"], "subtitle": s["subtitle"], "members": len(cached) or SCAN_ROWS, **q})
    return {"as_of": datetime.now(timezone.utc).isoformat(), "items": rows}


@app.get("/heatmap")
async def heatmap(group: str):
    """Price snapshot of all group members for the heatmap. Fresh for 150 s (kept fresh by heat_prefetch_loop while a Terminal is open); up to 15 min old it is returned at once and
    refreshed in the background, so opening a heatmap a second time does not wait for IBKR"""
    await ensure_connected()
    gid = group.upper()
    g = GROUP_BY_ID.get(gid) or SCANNER_BY_ID.get(gid)
    if not g:
        raise HTTPException(404, f"Unknown group {group}")
    ts, cached = _heat_cache.get(g["id"], (0.0, []))
    age = time.time() - ts
    if cached and age < HEAT_TTL:
        return _heat_reply(g, cached, age, cached=True)
    if cached and age < HEAT_STALE_MAX:
        _heat_task(g)
        return _heat_reply(g, cached, age, cached=True, refreshing=True)
    items = await asyncio.shield(_heat_task(g))
    return _heat_reply(g, items, 0.0, cached=False)


HEAT_STALE_MAX = 900.0
_heat_tasks: dict[str, asyncio.Future] = {}


_heat_failed: dict[str, float] = {}              # last failed prefetch per group: retried after HEAT_PREFETCH_EVERY, not every loop


def heat_due(now: float, cache: dict, ids: list[str], every: float = HEAT_PREFETCH_EVERY, failed: dict | None = None) -> list[str]:
    """Groups whose snapshot is missing or older than `every` (and did not just fail), oldest first"""
    failed = failed or {}
    ages = [(now - max(cache.get(i, (0.0, []))[0], failed.get(i, 0.0)), i) for i in ids]
    return [i for age, i in sorted(ages, reverse=True) if age >= every]


async def heat_prefetch_loop() -> None:
    """Keep every heatmap ready while a Terminal is connected (live stream open): groups are rebuilt one at a time in
    the background, so opening a heatmap shows a snapshot at once. Nothing runs while no Terminal is open"""
    await asyncio.sleep(20)                                   # let the API connect and reconcile first
    while True:
        try:
            if _ws_clients and ib.isConnected():
                groups = {g["id"]: g for g in [*GROUPS, *SCANNERS]}
                for gid in heat_due(time.time(), _heat_cache, list(groups), failed=_heat_failed):
                    if not _ws_clients or not ib.isConnected():
                        break
                    try:
                        await asyncio.shield(_heat_task(groups[gid]))
                    except Exception as e:
                        _heat_failed[gid] = time.time()
                        print(f"heatmap prefetch {gid}: {getattr(e, 'detail', e)}")
                    await asyncio.sleep(HEAT_PREFETCH_GAP)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("heatmap prefetch:", e)
        await asyncio.sleep(10)


def _heat_reply(g: dict, items: list[dict], age: float, cached: bool, refreshing: bool = False) -> dict:
    return {"group": g["id"], "name": g["name"], "cached": cached, "items": items, "breadth": _breadth(items),
            "missing": _heat_missing.get(g["id"], []), "age_s": round(age, 1), "refreshing": refreshing}


def _heat_task(g: dict) -> asyncio.Future:
    """One snapshot build per group at a time: a second request (or a background refresh) joins the one in flight"""
    t = _heat_tasks.get(g["id"])
    if t is None or t.done():
        t = asyncio.ensure_future(_heat_build(g))
        t.add_done_callback(lambda f: f.cancelled() or f.exception())       # a failed background refresh is not "never retrieved"
        _heat_tasks[g["id"]] = t
    return t


async def _heat_build(g: dict) -> list[dict]:
    if "scan_code" in g:
        contracts, meta = await _scan(g["scan_code"])
        missing: list[str] = []
    else:
        contracts, meta, missing = await _group_contracts(g)
    items = await _snapshot(contracts, meta)
    if "scan_code" in g:
        for r in items:
            r["weight"] = scan_weight(g["scan_code"], int(r["weight"]), r["change_pct"])
    items.sort(key=lambda r: -r["weight"])
    _heat_cache[g["id"]] = (time.time(), items)
    _heat_missing[g["id"]] = missing                # members IBKR does not recognise (renamed / delisted) — shown, not hidden
    return items


async def _group_contracts(g: dict) -> tuple[list, list, list[str]]:
    """Contracts of a fixed group: members not yet known are qualified in one batch (in parallel), not one by one"""
    def key_of(sym: str) -> str:                 # a member may name its own market, e.g. "IDX:VIX" in a group of stocks
        return sym if ":" in sym else f"{g['market']}:{sym}"
    todo = [(key_of(sym), build_contract(*parse_key(key_of(sym)))) for sym, _, _ in g["members"] if key_of(sym) not in _contracts]
    if todo:
        try:
            await asyncio.wait_for(ib.qualifyContractsAsync(*[c for _, c in todo]), 20)
        except Exception as e:
            print(f"heatmap {g['id']}: qualify: {e or type(e).__name__}")
        for key, c in todo:
            if c.conId:
                _contracts[key] = c
    contracts, meta, missing = [], [], []
    for sym, name, w in g["members"]:
        key = key_of(sym)
        sym = parse_key(key)[1]
        c = _contracts.get(key)
        if c is None:
            missing.append(sym)
            continue
        contracts.append(c)
        meta.append((key, sym, name, w))
    if missing:
        print(f"heatmap {g['id']}: not recognised by IBKR: {', '.join(missing)}")
    return contracts, meta, missing


async def _scan(scan_code: str) -> tuple[list, list]:
    """Whole-market US stock scan by IBKR (top % gainers / losers / most active), price and volume filtered.
    Returns contracts + (key, symbol, name, rank) like a fixed group's members"""
    from ib_async import ScannerSubscription
    sub = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR", scanCode=scan_code, numberOfRows=SCAN_ROWS,
                              abovePrice=SCAN_MIN_PRICE, aboveVolume=SCAN_MIN_VOLUME)
    try:
        rows = await asyncio.wait_for(ib.reqScannerDataAsync(sub), 20)
    except asyncio.TimeoutError:
        raise HTTPException(504, "IBKR market scanner did not answer within 20 s")
    if not rows:
        raise HTTPException(502, "IBKR market scanner returned no stocks (market data permissions, or try again in a minute)")
    contracts, meta = [], []
    for r in rows:
        c = getattr(r.contractDetails, "contract", None)
        if c is None or c.secType != "STK" or not c.conId:
            continue
        c.exchange = "SMART"                     # scanner contracts carry the listing exchange; quotes come from SMART
        key = f"STK:{c.symbol}"
        name = NAME_BY_KEY.get(key) or getattr(r.contractDetails, "longName", "") or c.symbol
        contracts.append(c)
        meta.append((key, c.symbol, name, int(r.rank)))
    return contracts, meta


async def _snapshot(contracts: list, meta: list) -> list[dict]:
    """One price snapshot per contract: temporary market data subscriptions in batches (stay within the quota)"""
    items = []
    for i in range(0, len(contracts), 45):          # temporary subscriptions in batches (stay within market data quota)
        chunk = contracts[i:i + 45]
        chunk_meta = meta[i:i + 45]
        tickers = []
        for (key, _, _, _), c in zip(chunk_meta, chunk):
            k = key
            if k in _tickers:                        # stream already exists (in the watchlist), reuse it
                tickers.append((_tickers[k], False))
            else:
                tickers.append((ib.reqMktData(c, "", False, False), True))
        await _wait_prices([t for t, _ in tickers])
        for (key, sym, name, w), (t, temp) in zip(chunk_meta, tickers):
            row = _ticker_row(key, t)
            row.update({"name": name, "weight": w})
            items.append(row)
            if temp:
                try:
                    ib.cancelMktData(t.contract)
                except Exception:
                    pass
    return items


def _has_price(t) -> bool:
    return bool((_px(t.last) or (_px(t.bid) and _px(t.ask))) and _px(t.close))


async def _wait_prices(tickers: list, max_wait: float = 2.5) -> None:
    """Wait until every ticker has a price and a previous close, at most max_wait seconds (was a fixed 2.5 s)"""
    deadline = time.monotonic() + max_wait
    await asyncio.sleep(0.2)
    while time.monotonic() < deadline and not all(_has_price(t) for t in tickers):
        await asyncio.sleep(0.1)


def _breadth(items: list[dict]) -> dict:
    return {"up": sum(1 for r in items if (r.get("change_pct") or 0) > 0), "down": sum(1 for r in items if (r.get("change_pct") or 0) < 0),
            "total": len(items)}


# ---------- portfolio ----------
def _position_row(key: str, mkt: str, sym: str, sec_type: str, ccy: str | None, qty: float, avg: float, mp: float,
                  mv_local: float, upnl_local: float, rpnl_local: float, rates: dict) -> dict:
    """/positions row with money in USD: P&L and market value arrive in the instrument's currency (the QUOTE currency for
    FX — JPY for USDJPY / EURJPY, GBP for EURGBP). Converted with qx_strategies.fx; when that is impossible (cross pair
    without a USD rate) the local values are kept and "usd" is False, so the Terminal can label them"""
    if mkt == "FX":
        ccy = fx.split_pair(sym)[1]
        upnl = fx.pnl_to_usd(sym, upnl_local, mp, rates)
        rpnl = fx.pnl_to_usd(sym, rpnl_local, mp, rates)
        n = fx.notional_usd(sym, qty, mp, rates)
        mv = None if n is None else (n if qty >= 0 else -n)
    else:
        ccy = ccy or "USD"
        r = fx.usd_rate(ccy, rates)
        upnl, rpnl, mv = ((upnl_local * r, rpnl_local * r, mv_local * r) if r is not None else (None, None, None))
    ok = upnl is not None and rpnl is not None and mv is not None
    return {"key": key, "symbol": sym, "market": mkt, "sec_type": sec_type,
            "position": qty, "avg_cost": avg, "market_price": mp,
            "market_value": mv if ok else mv_local, "unrealized_pnl": upnl if ok else upnl_local, "realized_pnl": rpnl if ok else rpnl_local,
            "currency": ccy, "usd": ok, "market_value_local": mv_local, "unrealized_pnl_local": upnl_local,
            **({} if ok or ccy == "USD" else {"pnl_ccy": ccy}),
            "decimals": decimals_for(mkt, sym) if mkt in MARKET_BY_ID else 2}


@app.get("/positions")
async def positions():
    """Positions from the ib_async subscription (reqPositions on connect + updates via positionEvent)
    + price/P&L from the portfolio stream if available, otherwise computed from the ticker — tolerant of connection hiccups"""
    await ensure_connected()
    pos_list = list(ib.positions())
    if not pos_list:
        try:
            pos_list = list(await asyncio.wait_for(ib.reqPositionsAsync(), 5) or [])
        except Exception as e:
            print("positions: reqPositions failed:", e)
            pos_list = list(ib.positions())
    port = {}
    try:
        for item in ib.portfolio():
            if _f(item.position) != 0 and _mine(getattr(item, "account", "")):
                port[key_of_contract(item.contract)] = item
    except Exception as e:
        print("positions: portfolio read failed:", e)
    rows = []
    seen = set()
    rates = _usd_rates()
    acct = active_account()
    cash_map = _fx_cash_map()
    for p in pos_list:
        try:
            if _f(p.position) == 0 or not _mine(p.account):
                continue
            key = key_of_contract(p.contract)
            if key in seen:
                continue
            seen.add(key)
            mkt, sym = key.split(":", 1)
            item = port.get(key)
            qty = _f(p.position)
            avg = _f(p.avgCost)
            cash = None
            if p.contract.secType == "CASH":
                cash = _fx_usd_base_qty(key, qty) if sym.startswith("USD") else _fx_cash(key)
            from_cash = cash is not None and abs(cash - qty) >= 1
            if from_cash:                                          # use the real cash balance instead of the IBKR virtual position
                qty, item = cash, None
                e = cash_map.get(key) or {}
                if qty == 0 or _fx_residue(key, e.get("cash", 0.0), acct):
                    continue
                try:
                    avg = history.open_cost(acct, key) or avg       # open trade of the shared manual/agent book
                except Exception:
                    pass
            if item is not None:
                mp, mv, upnl, rpnl = _f(item.marketPrice), _f(item.marketValue), _f(item.unrealizedPNL), _f(item.realizedPNL)
            else:
                mp = _mark(_tickers.get(key))
                mv = qty * mp
                upnl = (mp - avg) * qty if mp else 0.0
                rpnl = 0.0
            row = _position_row(key, mkt, sym, p.contract.secType, p.contract.currency, qty, avg, mp, mv, upnl, rpnl, rates)
            if from_cash:
                e = cash_map.get(key) or {}
                row["origin"], row["ib_virtual_qty"] = "cash_balance", _f(p.position)
                if e.get("cash_qty") is not None and e["cash_qty"] != qty:
                    row["cash_qty"] = e["cash_qty"]
                if e.get("cash_warn"):
                    row["cash_warn"] = e["cash_warn"]
            rows.append(row)
        except Exception as e:
            print("positions: skip", getattr(p.contract, "localSymbol", "?"), e)
            audit("positions_row_error", symbol=getattr(p.contract, "localSymbol", "?"), error=str(e)[:200])
    # FX fallback: IBKR stores FX holdings as per-currency cash balances (CashBalance) — if reqPositions does not report them (can happen after a gateway restart)
    # build rows from non-zero balances (EUR → FX:EURUSD, JPY → FX:USDJPY …), using the average cost from our trade history
    try:
        for key, e in cash_map.items():
            if abs(e["cash"]) < 1 or key in seen or _fx_residue(key, e["cash"], acct):
                continue
            seen.add(key)
            sym = key.split(":", 1)[1]
            if e["qty"] is None:                     # USDxxx without a price: show the balance, the position cannot be sized
                rows.append({"key": key, "symbol": sym, "market": "FX", "sec_type": "CASH", "position": 0.0, "avg_cost": 0.0,
                             "market_price": 0.0, "market_value": 0.0, "unrealized_pnl": 0.0, "realized_pnl": 0.0,
                             "currency": fx.split_pair(sym)[1], "usd": False, "decimals": decimals_for("FX", sym),
                             "origin": "cash_balance", "unpriced": True, "cash_balance": e["cash"], "cash_currency": e["ccy"]})
                continue
            if e["qty"] == 0:
                continue
            qty, mp = e["qty"], _mark(_tickers.get(key))
            avg = 0.0
            try:
                avg = history.open_cost(acct, key) or 0.0
            except Exception:
                pass
            row = _position_row(key, "FX", sym, "CASH", fx.split_pair(sym)[1], qty, avg, mp, qty * mp,
                                (mp - avg) * qty if mp and avg else 0.0, 0.0, rates)
            row["origin"] = "cash_balance"
            if e.get("cash_qty") is not None and e["cash_qty"] != qty:
                row["cash_qty"] = e["cash_qty"]                  # cash balance disagrees with the books (shown, not used)
            if e.get("cash_warn"):
                row["cash_warn"] = e["cash_warn"]                # → reconcile 'warn' (see _reconcile_positions)
            rows.append(row)
    except Exception as e:
        print("positions: cash fallback failed:", e)
    recon = None
    try:
        recon = _reconcile_positions(rows)
    except Exception as e:
        print("positions: attribution failed:", e)
    return {"as_of": datetime.now(timezone.utc).isoformat(), "positions": rows, "reconcile": recon}


def _reconcile_positions(rows: list[dict]) -> dict:
    """Compare IBKR positions with nodes (heartbeat) + manual/agent books from history, then fill in source for each row"""
    acct = active_account()
    live = {nid: bus.live(nid) for nid in set(nm.nodes) | set(bus.nodes)}
    desired = {nid for nid, n in nm.nodes.items() if n.get("desired")}
    rep = attr.reconcile({r["key"]: _f(r["position"]) for r in rows}, live, history.book_positions(acct), desired)
    warns = {r["key"]: r["cash_warn"] for r in rows if r.get("cash_warn")}
    for rr in rep["rows"]:                     # a cash balance far from the books (trade outside QOS?) is never silently 'ok'
        if rr["key"] in warns:
            rr["note"] += f" · {warns[rr['key']]}"
            if rr["status"] == "ok":
                rr["status"] = "warn"
    if warns and rep["status"] == "ok":
        rep["status"] = "warn"
    attr.annotate(rows, rep, history.open_sources(acct))
    return rep


_recon_state: dict = {"bad": {}, "alerted": set(), "last": None}
RECON_CONFIRM_ROUNDS = 7          # 7 rounds × 30 s ≈ 210 s: longer than IBKR's ~3-minute CashBalance update cycle


async def _recon_watch() -> None:
    """Every 30 seconds: mismatch / orphan seen RECON_CONFIRM_ROUNDS rounds in a row (cash balances and fills arrive out of sync) → audit + alert · notify again when resolved"""
    if not ib.isConnected() or not (nm.nodes or bus.nodes):
        return
    rep = (await positions()).get("reconcile") or {}
    _recon_state["last"] = rep
    bad = {r["key"]: r for r in rep.get("rows", []) if r["status"] in attr.ALERT_STATUSES}
    for k, r in bad.items():
        n = _recon_state["bad"].get(k, 0) + 1
        _recon_state["bad"][k] = n
        if n == RECON_CONFIRM_ROUNDS and k not in _recon_state["alerted"]:
            _recon_state["alerted"].add(k)
            audit("reconcile_mismatch", key=k, status=r["status"], ib_qty=r["ib_qty"], expected=r["expected"], note=r["note"])
            for nd in r.get("nodes") or []:
                soak.incident(nd["node"], "reconcile", f"{r['status']} {k}: {r['note']}")
            notify.fire(f"Reconciliation {r['status']}: {k} — {r['note']}", "error")
    for k in list(_recon_state["bad"]):
        if k not in bad:
            del _recon_state["bad"][k]
            if k in _recon_state["alerted"]:
                _recon_state["alerted"].discard(k)
                audit("reconcile_resolved", key=k)
                notify.fire(f"Reconciliation OK again: {k}", "agent")


async def ib_refresh() -> dict:
    """Re-request all account/positions/orders/executions data from IBKR (used when streams are empty after a gateway restart)"""
    await ensure_connected()
    acct = active_account() or (ib.managedAccounts()[0] if ib.managedAccounts() else "")
    out = {}
    for name, coro in [("account_updates", ib.reqAccountUpdatesAsync(acct)), ("positions", ib.reqPositionsAsync()),
                       ("open_orders", ib.reqAllOpenOrdersAsync()), ("executions", ib.reqExecutionsAsync())]:
        try:
            await asyncio.wait_for(coro, 15)
            out[name] = "ok"
        except Exception as e:
            out[name] = f"error: {e}"
    _acct_cache.update(ts=0.0, values={})
    out["account_values"] = len(ib.accountValues()); out["positions_n"] = len(ib.positions()); out["fills_n"] = len(ib.fills())
    audit("ib_refresh", **out)
    return out


@app.post("/ib/refresh")
async def ib_refresh_ep():
    return await ib_refresh()


@app.get("/catalog/status")
async def catalog_status():
    """Nautilus data catalog: available data range per series and the latest update result"""
    from . import catalog as cat
    return await asyncio.to_thread(cat.status)


# ---------- Nautilus node manager (step 6) ----------
class NodeCreate(BaseModel):
    name: str
    config_file: str
    overrides: dict = {}
    auto_restart: bool = True
    node_id: str = ""


class NodeUpdate(BaseModel):
    name: str | None = None
    config_file: str | None = None
    overrides: dict | None = None
    auto_restart: bool | None = None


def _node_call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except KeyError:
        raise HTTPException(404, "node not found")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.get("/nodes")
async def nodes_list():
    from .node_manager import list_configs
    return {"nodes": await asyncio.to_thread(nm.list), "configs": list_configs(), "gateway": nb.resolve_gateway()}


@app.post("/nodes")
async def nodes_create(body: NodeCreate):
    return await asyncio.to_thread(_node_call, nm.create, body.name, body.config_file, body.overrides, body.auto_restart, body.node_id)


@app.put("/nodes/{nid}")
async def nodes_update(nid: str, body: NodeUpdate):
    return await asyncio.to_thread(_node_call, nm.update, nid, body.name, body.overrides, body.auto_restart, body.config_file)


@app.delete("/nodes/{nid}")
async def nodes_delete(nid: str):
    await asyncio.to_thread(_node_call, nm.delete, nid)
    return {"ok": True}


@app.post("/nodes/{nid}/start")
async def nodes_start(nid: str):
    return await asyncio.to_thread(_node_call, nm.start, nid)


async def _node_orders(nid: str) -> list:
    if not ib.isConnected():
        return []
    return [tr for tr in await _open_trades_fresh() if _live(tr) and _node_of_trade(tr) == nid]


def _on_loop(coro) -> None:
    """Run a coroutine on the API loop from sync code (event loop or worker thread)"""
    try:
        asyncio.get_running_loop().create_task(coro)
        return
    except RuntimeError:
        pass
    loop = _main_loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)
    else:
        coro.close()


async def _after_node_stop_safe(nid: str) -> None:
    try:
        await _after_node_stop(nid)
    except Exception as e:
        print(f"after node stop {nid}:", e)


async def _after_node_stop(nid: str) -> list[dict]:
    """A stopping node cancels its own orders. When that fails (seen: 10147 "order not found" for an order placed before a
    restart) they stay working at IBKR and fill later with no node managing them. Cancel the leftovers as the node's
    clientId (free now) and alert if any are still working"""
    await asyncio.sleep(2.0)
    trades = await _node_orders(nid)
    if not trades:
        return []
    items = await _cancel_trades(trades, f"node {nid} stopped with working orders")
    left = await _verify_cancelled(items, 2.5)
    if left:
        await warn(f"Node {nid} stopped but {len(left)} order(s) are still working at IBKR: "
                   + ", ".join(f"#{o['order_id']} {o['key']} ({o['status']})" for o in left[:5])
                   + ". Cancel them in Orders or use the Kill switch", node=nid, orders=left)
    else:
        await warn(f"Node {nid} left {len(items)} order(s) working when it stopped — cancelled by the API", "info", node=nid)
    return left


@app.post("/nodes/{nid}/stop")
async def nodes_stop(nid: str):
    return await asyncio.to_thread(_node_call, nm.stop, nid, "manual")


@app.post("/nodes/{nid}/restart")
async def nodes_restart(nid: str):
    return await asyncio.to_thread(_node_call, nm.restart, nid)


@app.get("/nodes/{nid}/log")
async def nodes_log(nid: str, lines: int = 300, level: str = ""):
    return {"lines": await asyncio.to_thread(_node_call, nm.log, nid, min(max(lines, 10), 2000), level)}


def _node_ref(nid: str) -> str | None:
    """Strategy version used by the node: the one locked at start (if running) or the one it would get if started now"""
    rt = nm.rt.get(nid) or {}
    ref = rt.get("strategy_ref") if nm._running(nid) else None
    if ref:
        return ref
    try:
        from qx_strategies import resolve_ref
        return resolve_ref(nm.effective_config(nid).get("strategy", "ema_cross"))
    except Exception:
        return None


def _package_usage() -> dict[str, list[str]]:
    using: dict[str, list[str]] = {}
    for nid in nm.nodes:
        ref = _node_ref(nid)
        if ref:
            using.setdefault(ref, []).append(nid)
    return using


def _package_facts(using: dict[str, list[str]] | None = None) -> dict[str, dict]:
    """Facts for status promotion: soak GO / allow_live of the nodes using each version"""
    from qx_strategies.registry import REG
    using = using if using is not None else _package_usage()
    facts = {}
    for e in REG.entries():
        if e.status != "ok":
            continue
        nodes = using.get(e.ref, [])
        go = False
        for nid in nodes:
            try:
                if nm._running(nid) and soak.soak_ref(nid) is None:    # only a never-recorded running node (normally recorded at
                    _note_soak_ref(nid)                                 # start / adoption); idempotent, counted from its process start
                go = go or soak.report(nid, ref=e.ref).get("verdict") == "GO"     # soak evidence counts for its own version only
            except Exception:
                pass
        facts[e.ref] = {"builtin": e.kind == "builtin", "soak_go": go,
                        "live": go and any((nm.nodes.get(n) or {}).get("allow_live") for n in nodes)}
    return facts


@app.get("/strategies/packages")
async def strategies_packages():
    """All strategy packages (built-in + QOS_STRATEGIES_DIR): version, manifest, load status, nodes using it, lifecycle status (step 20)"""
    from qx_strategies.registry import REG, strategies_dir
    REG.scan(force=True)
    using = await asyncio.to_thread(_package_usage)
    status = await asyncio.to_thread(lambda: sst.refresh(_package_facts(using)))
    items = []
    for e in REG.entries():
        st = status.get(e.ref)
        if st is not None:
            st = {**st, "evaluating": st.get("evaluating") or e.ref in _eval_queue, "history": (st.get("history") or [])[-10:]}
        nodes = using.get(e.ref, [])
        items.append({"ref": e.ref, "name": e.name, "version": e.version, "kind": e.kind, "status": e.status, "error": e.error,
                      "path": str(e.path) if e.path else None, "manifest": e.manifest if e.status == "ok" else {},
                      "nodes": nodes, "latest": _latest_ref(e.name) == e.ref, "lifecycle": st,
                      "node_info": [{"id": n, "name": (nm.nodes.get(n) or {}).get("name", n), "running": nm._running(n),
                                     "allow_live": bool((nm.nodes.get(n) or {}).get("allow_live")),
                                     "limits_active": bool((nm.rt.get(n) or {}).get("package_limits")) if nm._running(n) else None,
                                     "overrides": (nm.nodes.get(n) or {}).get("overrides", {})} for n in nodes]})
    pkg_jobs = [j for j in lab.list() if j.get("kind") in ("install", "evaluate")][:10]
    run = lab.running()
    return {"dir": str(strategies_dir()), "states": sst.STATES, "queue": list(_eval_queue), "packages": items,
            "instruments": lab_mod.INSTRUMENTS, "timeframes": lab_mod.TIMEFRAMES, "jobs": pkg_jobs,
            "running": lab.public(run) if run else None,
            "live_gateway": nb.resolve_gateway().get("mode") == "live"}


def _package_entry(ref: str):
    from qx_strategies.registry import REG
    REG.scan(force=True)
    try:
        return REG.resolve(ref)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/strategies/packages/reload")
async def strategies_packages_reload():
    """After qos install: rescan the package folders and start recording installed status for newly added packages"""
    from qx_strategies.registry import REG
    REG.scan(force=True)
    new, errors = [], []
    for e in REG.entries():
        if e.kind == "builtin":
            continue
        if e.status != "ok":
            errors.append({"ref": e.ref, "error": e.error})
            continue
        if sst.state_of(e.ref) is None:
            src = None
            try:
                src = json.loads((Path(e.path) / "install.json").read_text()).get("source")
            except Exception:
                pass
            sst.ensure(e.ref, source=src)
            new.append(e.ref)
            audit("strategy_package_installed", ref=e.ref, source=src)
    return {"new": new, "errors": errors}


_eval_queue: list[str] = []
_eval_opts: dict[str, dict] = {}


def _eval_done(job: dict) -> None:
    ref, r = job.get("package"), job.get("result")
    if r:
        e = sst.set_result(ref, **{k: r[k] for k in ("backtest", "walkforward") if k in r})
        audit("strategy_package_evaluated", ref=ref, state=e.get("state"), backtest=r.get("backtest"), walkforward=r.get("walkforward"))
    else:
        why = "cancelled" if job["status"] == "cancelled" else (job["log"][-1] if job.get("log") else f"exit {job.get('exit_code')}")
        sst.set_evaluating(ref, False, error=why[:300])


async def _eval_pump(_job: dict | None = None) -> None:
    """Evaluate one package at a time, queued after other Strategy lab jobs"""
    while _eval_queue and not lab.running():
        ref = _eval_queue[0]
        o = _eval_opts.get(ref) or {}
        from qx_strategies.registry import REG, venv_python
        try:
            py = venv_python(REG.resolve(ref).path)              # step 22: packages with requires are evaluated in their own venv
        except Exception:
            py = None
        cmd = [py, "scripts/evaluate_package.py", ref] if py else [lab.uv(), "run", "scripts/evaluate_package.py", ref]
        for k, flag in (("only", "--only"), ("days", "--days"), ("wf_days", "--wf-days"), ("train", "--train"), ("test", "--test")):
            if o.get(k):
                cmd += [flag, str(o[k])]
        try:
            _export_usd_rates()
            await lab.start_cmd("evaluate", cmd, {"package": ref, "options": {"days": 90, "wf_days": 365, **o}}, on_done=_eval_done)
            _eval_queue.pop(0)
            _eval_opts.pop(ref, None)
            return
        except ValueError:
            return
        except Exception as e:
            _eval_queue.pop(0)
            sst.set_evaluating(ref, False, error=str(e)[:300])


class PackageEval(BaseModel):
    only: Literal["backtest", "walkforward"] | None = None
    days: int | None = Field(None, ge=7, le=1500)
    wf_days: int | None = Field(None, ge=60, le=3000)
    train: int | None = Field(None, ge=14, le=730)
    test: int | None = Field(None, ge=7, le=365)


@app.post("/strategies/packages/{ref}/evaluate")
async def strategies_packages_evaluate(ref: str, body: PackageEval | None = None):
    """90-day backtest + walk-forward over the manifest grid as a background job (Strategy lab) → results tied to name@version
    only = run just backtest or walk-forward (Terminal) · the part not run keeps its previous result"""
    e = _package_entry(ref)
    if e.kind == "builtin":
        raise HTTPException(400, "builtin strategies are evaluated from their configs (Strategies tab)")
    if e.status != "ok":
        raise HTTPException(400, f"{e.ref} cannot be loaded: {e.error}")
    sst.ensure(e.ref)
    running = lab.running()
    if running and running.get("package") == e.ref:
        return {"ref": e.ref, "status": "running", "job": running["id"]}
    if e.ref not in _eval_queue:
        _eval_queue.append(e.ref)
    _eval_opts[e.ref] = (body or PackageEval()).model_dump(exclude_none=True)
    sst.set_evaluating(e.ref, True)
    await _eval_pump()
    cur = lab.running()
    audit("strategy_package_evaluate", ref=e.ref)
    if cur and cur.get("package") == e.ref:
        return {"ref": e.ref, "status": "running", "job": cur["id"]}
    return {"ref": e.ref, "status": "queued", "position": _eval_queue.index(e.ref) + 1 if e.ref in _eval_queue else 0}


@app.delete("/strategies/packages/{ref}")
async def strategies_packages_delete(ref: str):
    """qos uninstall — refused (409) if any node uses this version (including nodes that reference the name only and resolve to this version)"""
    import shutil
    from qx_strategies.registry import REG
    if "@" not in ref:
        raise HTTPException(400, "give the exact version: name@version")
    e = _package_entry(ref)
    if e.kind == "builtin":
        raise HTTPException(400, "builtin strategies cannot be uninstalled")
    users = (await asyncio.to_thread(_package_usage)).get(e.ref, [])
    if users:
        raise HTTPException(409, f"{e.ref} is used by node(s) {', '.join(users)} — delete those nodes or point them at another version first")
    running = lab.running()
    if running and running.get("package") == e.ref:
        raise HTTPException(409, f"{e.ref} is being evaluated — cancel job {running['id']} first")
    if e.ref in _eval_queue:
        _eval_queue.remove(e.ref)
    await asyncio.to_thread(shutil.rmtree, e.path)
    REG.scan(force=True)
    sst.drop(e.ref)
    audit("strategy_package_uninstalled", ref=e.ref)
    return {"ok": True, "ref": e.ref}


class PackageInstall(BaseModel):
    source: str
    force: bool = False


def _install_done(job: dict) -> None:
    r = job.get("result")
    if not r or not r.get("ref"):
        return
    from qx_strategies.registry import REG
    ref = r["ref"]
    job["package"] = ref
    REG.scan(force=True)
    if job.get("force"):
        sst.reset(ref, source=job.get("source"))
    else:
        sst.ensure(ref, source=job.get("source"))
    audit("strategy_package_installed", ref=ref, source=job.get("source"), via="terminal")
    if ref not in _eval_queue:                       # the Lab on_finish starts evaluation right away
        _eval_queue.append(ref)
    sst.set_evaluating(ref, True)


@app.post("/strategies/packages/install")
async def strategies_packages_install(body: PackageInstall):
    """Terminal → Install: run qos install as a Strategy lab job (live log in the job), then evaluate automatically"""
    src = body.source.strip()
    if not src:
        raise HTTPException(400, "give a folder with strategy.py, a strategy.py file, or a git url")
    if src.startswith("-"):
        raise HTTPException(400, "invalid source")
    if lab.running():
        raise HTTPException(409, "another Strategy lab job is running — wait for it or cancel it")
    used = await asyncio.to_thread(_package_usage)
    cmd = [lab.uv(), "run", "qos_cli.py", "--api", "", "install", os.path.expanduser(src), "--no-eval", "--result-line"]
    if body.force:
        cmd.append("--force")
    os.environ["QOS_USED_REFS"] = json.dumps(used)          # qos install refuses --force over a version in use by a node
    try:
        job = await lab.start_cmd("install", cmd, {"package": "", "source": src, "force": body.force},
                                  operator=(ops.current.get() or {}).get("id", ""), on_done=_install_done)
    except ValueError as e:
        raise HTTPException(409, str(e))
    finally:
        os.environ.pop("QOS_USED_REFS", None)
    audit("strategy_package_install_started", source=src, force=body.force, job=job["id"])
    return job


class PackageDeploy(BaseModel):
    name: str = ""
    instrument: str
    tf_minutes: int | None = None
    overrides: dict = {}
    auto_restart: bool = True
    start: bool = False


def _package_config_file(e, instrument: str, tf: int) -> str:
    """Base config per (package@version, instrument, timeframe) — user edits are stored as node overrides, the package is not modified"""
    sym = instrument.split(".")[0].replace("/", "").lower()
    ver = e.version.replace(".", "_") if e.kind != "builtin" else "builtin"
    file = f"pkg_{e.name}_{ver}_{sym}_{tf}m"[:55] + ".json"
    path = lab_mod.CONFIG_DIR / file
    if not path.exists():
        camel = "".join(w.title() for w in e.name.split("_"))
        tag = ("V" + e.version.replace(".", "") if e.kind != "builtin" else "B") + sym.upper() + f"{tf}M"
        cfg = {"strategy": e.ref, "strategy_id": f"QX{camel}-{tag}"[:60], "instrument": instrument, "tf_minutes": tf,
               "trade_size": "20000", "sl_pips": 0.0, "tp_pips": 0.0, "allow_short": True}
        lab_mod.validate(cfg)
        lab_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2) + "\n")
    return file


@app.post("/strategies/packages/{ref}/deploy")
async def strategies_packages_deploy(ref: str, body: PackageDeploy):
    """Packages can be deployed as nodes from the installed status (paper) · on a live gateway the node needs allow_live before starting"""
    e = _package_entry(ref)
    if e.status != "ok":
        raise HTTPException(400, f"{e.ref} cannot be loaded: {e.error}")
    m = e.manifest or {}
    tf = int(m.get("tf_minutes") or body.tf_minutes or 5)
    if body.instrument not in lab_mod.INSTRUMENTS:
        raise HTTPException(400, f"instrument must be one of {', '.join(lab_mod.INSTRUMENTS)} (what the data catalog has)")
    if m.get("instruments") and body.instrument not in m["instruments"]:
        raise HTTPException(400, f"{e.ref} trades {', '.join(m['instruments'])}")
    try:
        file = await asyncio.to_thread(_package_config_file, e, body.instrument, tf)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    sym = body.instrument.split(".")[0].replace("/", "").lower()
    name = body.name.strip() or f"{e.name}_{sym}"
    st = await asyncio.to_thread(_node_call, nm.create, name, file, body.overrides, body.auto_restart, "")
    audit("strategy_package_deployed", ref=e.ref, node=st["id"], config_file=file, overrides=st.get("overrides"), start=body.start)
    if body.start:
        st = await asyncio.to_thread(_node_call, nm.start, st["id"])
    return {"node": st, "config_file": file}


class AllowLive(BaseModel):
    enabled: bool = True
    live_code: str | None = None


@app.post("/nodes/{nid}/allow_live")
async def nodes_allow_live(nid: str, body: AllowLive | None = None):
    """Step 20: allow a node to run on a live gateway — only when the node's strategy version is in soak_go status (409 otherwise)
    (ramp-up / real-money use belongs to step 14)"""
    body = body or AllowLive()
    if nid not in nm.nodes:
        raise HTTPException(404, f"no node '{nid}'")
    ref = await asyncio.to_thread(_node_ref, nid)
    state = None
    if body.enabled:
        doc = await asyncio.to_thread(lambda: sst.refresh(_package_facts()))
        st = doc.get(ref or "") or {}
        state = st.get("state")
        if state not in ("soak_go", "live"):
            audit("node_allow_live_refused", node=nid, ref=ref, state=state)
            raise HTTPException(409, f"{ref} is '{state or 'unknown'}' — allow_live needs soak_go"
                                     + (f" ({st['note']})" if st.get("note") else ""))
        require_live_ok(body.live_code, target=f"allow_live:{nid}")          # step 14: live confirmation code
        if not nm.nodes[nid].get("ramp"):
            nm.nodes[nid]["ramp"] = ramp_mod.new_ramp()                     # always start at the smallest size
    nm.nodes[nid]["allow_live"] = body.enabled
    await asyncio.to_thread(nm.save)
    audit("node_allow_live", node=nid, ref=ref, enabled=body.enabled, operator=(ops.current.get() or {}).get("id", ""))
    if body.enabled:
        await asyncio.to_thread(lambda: sst.refresh(_package_facts()))
    return {"node": nid, "allow_live": body.enabled, "strategy_ref": ref, "state": sst.state_of(ref or "")}


def _latest_ref(name: str) -> str | None:
    try:
        from qx_strategies import resolve_ref
        return resolve_ref(name)
    except Exception:
        return None


@app.get("/nodes/{nid}/summary")
async def node_summary(nid: str, range: str = "all", tz: str | None = None):
    """Everything about one node for the detail page: live status, real performance on the selected account, latest backtest/parity/walk-forward of the config, soak, recent activity"""
    st = await asyncio.to_thread(_node_call, nm.status, nid)
    acct, src = active_account(), f"node:{nid}"
    stats = await asyncio.to_thread(history.stats, range, acct, src, tz)
    if stats.get("profit_factor") == float("inf"):
        stats["profit_factor"] = 999.0
    trades = await asyncio.to_thread(history.list_trades, range, None, src, 15, acct, tz)
    curve = (await asyncio.to_thread(history.pnl_curves_by_source, range, acct, tz)).get(src, [])
    if len(curve) > 300:
        step = len(curve) // 300 + 1
        curve = curve[::step] + [curve[-1]]
    from . import perf as _perf
    closed = await asyncio.to_thread(history.list_trades, range, None, src, 100000, acct, tz)
    live_perf = _perf.perf_block([(t["exit_ts"], t["net_pnl"]) for t in closed if not t["open"] and t["exit_ts"] and t["net_pnl"] is not None],
                                 stats.get("equity_start") or 100_000.0)
    research: dict = {}
    try:
        cfgs = await asyncio.to_thread(lab_mod.list_configs, nm.nodes)
        mine = next((c for c in cfgs if c["file"] == st.get("config_file")), None)
        research = dict((mine or {}).get("latest") or {})
        bt = research.get("backtest")
        if bt and bt.get("file"):
            from qx_strategies import reports as _rep
            doc = await asyncio.to_thread(_rep.load_report, "backtest", bt["file"], 100000)
            bt_base = float(doc.get("balance_usd") or 0) - float(doc.get("pnl_usd") or 0)
            research["backtest_perf"] = _perf.perf_block([(x.get("closed"), x.get("pnl") or 0.0) for x in doc.get("trades") or []], bt_base)
            eq = doc.get("equity") or []
            if len(eq) > 200:
                eq = eq[:: len(eq) // 200 + 1] + [eq[-1]]
            research["backtest"] = {**bt, **{k: doc.get(k) for k in ("from", "to", "bars", "pnl_usd", "return_pct", "win_rate", "avg_win",
                                                                     "avg_loss", "max_drawdown_usd", "closed_positions")}, "equity": eq}
    except Exception as e:
        research["error"] = str(e)[:200]
    expected = None
    try:
        con = soak.connect()
        par = soak.last_parity(con, nid)
        con.close()
        if par and par.get("backtest_trades") is not None:
            expected = {"from": par.get("from"), "to": par.get("to"), "ts": par.get("ts"),
                        **_perf.perf_block([tuple(x) for x in par["backtest_trades"]], live_perf["base"])}
    except Exception as e:
        print("node summary expected:", e)
    pkg = {}
    try:
        ref = st.get("strategy_ref") or st.get("strategy_latest") or ""
        lc = sst.load().get(ref) or {}
        state = lc.get("state")
        note = sst.derive(lc, state in ("soak_go", "live"), state == "live")[1] if lc else ""
        pkg = {"ref": ref, "state": state, "note": note}
    except Exception as e:
        print("node summary package:", e)
    ramp_view = None
    try:
        if nm.nodes.get(nid, {}).get("ramp"):
            ramp_view = await asyncio.to_thread(_ramp_view, nid)
    except Exception as e:
        print("node summary ramp:", e)
    return {"node": st, "account": acct, "range": range, "stats": stats, "trades": trades, "curve": curve, "live_perf": live_perf,
            "expected_perf": expected, "package": pkg, "ramp": ramp_view,
            "research": research, "soak": await asyncio.to_thread(soak.report, nid),
            "events": bus.recent(nid, "signal,fill,order,risk", 15)}


# ---------- Strategy lab (step 9): config, backtest/parity/walk-forward, deploy ----------
from . import strategy_lab as lab_mod  # noqa: E402

lab = lab_mod.Lab()
lab.on_finish.append(_eval_pump)
try:
    sst.reset_evaluating()
except Exception as _e:
    print("strategy status:", _e)


class StrategySave(BaseModel):
    config: dict
    overwrite: bool = False


class StrategyRun(BaseModel):
    kind: str
    days: int | None = None
    start: str | None = None
    end: str | None = None
    train: int | None = None
    test: int | None = None
    grid: dict | None = None


class StrategyDeploy(BaseModel):
    name: str = ""
    auto_restart: bool = True
    start: bool = False
    force: bool = False


def _lab_call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except KeyError:
        raise HTTPException(404, "not found")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.get("/strategies")
async def strategies_list():
    return {"configs": await asyncio.to_thread(lab_mod.list_configs, nm.nodes), "jobs": lab.list(),
            "running": lab.running() and lab.public(lab.running()),
            "instruments": lab_mod.INSTRUMENTS, "timeframes": lab_mod.TIMEFRAMES, "default_days": lab_mod.DEFAULT_DAYS,
            "specs": lab_mod.specs()}


@app.put("/strategies/{file}")
async def strategies_save(file: str, body: StrategySave):
    cfg = await asyncio.to_thread(_lab_call, lab_mod.save_config, file, body.config, body.overwrite)
    audit("strategy_config_saved", file=file, config=cfg, overwrite=body.overwrite)
    return {"file": file, "config": cfg}


@app.delete("/strategies/{file}")
async def strategies_delete(file: str):
    _lab_call(lab_mod.delete_config, file, nm.nodes)
    audit("strategy_config_deleted", file=file)
    return {"ok": True}


@app.post("/strategies/{file}/run")
async def strategies_run(file: str, body: StrategyRun):
    try:
        _export_usd_rates()
        job = await lab.start(body.kind, file, body.model_dump(exclude_none=True, exclude={"kind"}),
                              operator=(ops.current.get() or {}).get("id", ""))
    except KeyError:
        raise HTTPException(404, "config not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit("strategy_job_started", file=file, kind=body.kind, job=job["id"])
    return job


@app.get("/strategies/jobs/{jid}")
async def strategies_job(jid: str):
    j = lab.jobs.get(jid)
    if not j:
        raise HTTPException(404, "job not found")
    return lab.public(j, log=True)


@app.post("/strategies/jobs/{jid}/cancel")
async def strategies_job_cancel(jid: str):
    return _lab_call(lab.cancel, jid)


@app.get("/strategies/{file}/deploy_check")
async def strategies_deploy_check(file: str):
    return {"warnings": await asyncio.to_thread(_lab_call, lab_mod.deploy_checks, file)}


@app.post("/strategies/{file}/deploy")
async def strategies_deploy(file: str, body: StrategyDeploy):
    warnings = await asyncio.to_thread(_lab_call, lab_mod.deploy_checks, file)
    if warnings and not body.force:
        raise HTTPException(409, "Review before deploying: " + " ".join(warnings))
    name = body.name.strip() or file.removesuffix(".json")
    st = await asyncio.to_thread(_node_call, nm.create, name, file, {}, body.auto_restart, "")
    audit("strategy_deployed", file=file, node=st["id"], warnings=warnings, start=body.start)
    if body.start:
        st = await asyncio.to_thread(_node_call, nm.start, st["id"])
    return {"node": st, "warnings": warnings}


# ---------- event pipeline from Nautilus nodes (step 7) ----------
@app.get("/events")
async def events_recent(node: str = "", types: str = "", limit: int = 200):
    """Latest events from nodes (newest first) · types e.g. fill,order,position,signal"""
    return {"events": bus.recent(node, types, min(max(limit, 1), 2000)), "status": bus.status()}


@app.get("/risk/nodes")
async def risk_nodes():
    """What nodes see: latest payload sent to Redis + each node's risk status from heartbeat"""
    return {"published": _risk_pub, "payload": await risk_payload(),
            "nodes": {nid: (bus.live(nid).get("risk") or {}) for nid in nm.nodes}}


@app.get("/events/status")
async def events_status():
    return bus.status()


@app.get("/backtests")
async def backtests_list(limit: int = 100):
    """Backtest / parity / walk-forward reports (newest first) from reports/"""
    from qx_strategies import reports
    return {"items": await asyncio.to_thread(reports.list_reports, limit)}


@app.get("/backtests/{kind}/{file}")
async def backtests_get(kind: str, file: str):
    from qx_strategies import reports
    try:
        return await asyncio.to_thread(reports.load_report, kind, file)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "report not found")


@app.get("/debug/ib")
async def debug_ib():
    """Raw ib_async data for diagnostics (positions / portfolio / cash balances / fills)"""
    await ensure_connected()
    try:
        pos = await asyncio.wait_for(ib.reqPositionsAsync(), 5)
    except Exception as e:
        pos = f"error: {e}"
    return {
        "managed_accounts": ib.managedAccounts(), "active_account": active_account(),
        "positions": [(p.account, p.contract.localSymbol or p.contract.symbol, p.contract.secType, _f(p.position), _f(p.avgCost)) for p in (pos if isinstance(pos, list) else ib.positions())],
        "portfolio": [(i.account, i.contract.localSymbol or i.contract.symbol, _f(i.position), _f(i.marketValue)) for i in ib.portfolio()],
        "cash": [(v.account, v.tag, v.currency, v.value) for v in ib.accountValues() if "Cash" in v.tag or (v.currency not in ("USD", "BASE", "") and v.tag in ("CashBalance", "TotalCashBalance", "NetLiquidationByCurrency"))],
        "non_usd_values": [(v.tag, v.currency, v.value) for v in ib.accountValues() if v.currency not in ("USD", "BASE", "")][:40],
        "tags_sample": sorted({v.tag for v in ib.accountValues()})[:60],
        "fills_today": len(ib.fills()), "open_trades": len(ib.openTrades()),
    }


@app.get("/orders")
async def orders():
    await ensure_connected()
    rows = []
    for tr in ib.openTrades():
        o, st, c = tr.order, tr.orderStatus, tr.contract
        if not _mine(o.account or ""):
            continue
        key = key_of_contract(c)
        nd = history.node_for(attr.client_order_id(o.orderRef)) or ("", "")
        rows.append(
            {
                "source": attr.classify(o.orderRef, nd[0]), "node": nd[0], "strategy": nd[1],
                "order_id": o.orderId, "key": key, "symbol": key.split(":", 1)[1], "side": o.action, "type": o.orderType,
                "qty": _f(o.totalQuantity),
                "limit_price": _f(o.lmtPrice) if o.orderType == "LMT" else None,
                "stop_price": _f(o.auxPrice) if o.orderType in ("STP", "TRAIL") else None,
                "parent_id": o.parentId or None, "oca_group": o.ocaGroup or None, "tif": o.tif,
                "ref": o.orderRef or "",
                "status": st.status, "filled": _f(st.filled), "remaining": _f(st.remaining),
            }
        )
    return {"orders": rows}


def _fill_source(order_ref: str | None) -> dict:
    nd = history.node_for(attr.client_order_id(order_ref)) or ("", "")
    return {"source": attr.classify(order_ref, nd[0]), "node": nd[0], "strategy": nd[1]}


@app.get("/reconcile")
async def reconcile_report():
    """Positions per instrument: IBKR vs nodes (heartbeat) + manual/agent (fill history) · status ok/external/untracked/stale/orphan/mismatch"""
    rep = (await positions()).get("reconcile") or {}
    return {**rep, "alerting": sorted(_recon_state["alerted"])}


# ---------- live readiness + ramp-up (step 14) ----------
def _ramp_metrics(nid: str, since_iso: str) -> dict:
    """Real performance of the node on the selected account since the current stage began (from local history + soak)"""
    acct = active_account()
    trades = [t for t in history.list_trades("all", None, f"node:{nid}", 100000, acct)
              if not t["open"] and (t.get("exit_ts") or "") >= since_iso]
    trades.sort(key=lambda t: t.get("exit_ts") or "")
    unknown = sum(1 for t in trades if t["net_pnl"] is None)          # cross pair without a USD rate: left out, counted
    pnls = [float(t["net_pnl"]) for t in trades if t["net_pnl"] is not None]
    since_ns = int(datetime.fromisoformat(since_iso).timestamp() * 1e9)
    con = soak.connect()
    try:
        inc = {k: n for k, n in con.execute("SELECT kind, COUNT(*) FROM incidents WHERE node=? AND ts_ns>=? GROUP BY kind", (nid, since_ns))}
        rej = con.execute("SELECT COUNT(*) FROM events WHERE node=? AND type='order' AND ts_ns>=? AND data LIKE '%\"event\": \"Rejected\"%'",
                          (nid, since_ns)).fetchone()[0]
        par = soak.last_parity(con, nid)
    finally:
        con.close()
    match = None
    if par and (par.get("ts") or "") >= since_iso:
        match = (par.get("live_vs_backtest") or {}).get("match_rate")
    return {"net_usd": round(sum(pnls), 2), "max_dd_usd": ramp_mod.drawdown(pnls), "trades": len(pnls), "unknown_pnl_trades": unknown,
            "crashes": int(inc.get("crash", 0)), "reconcile": int(inc.get("reconcile", 0)), "rejects": int(rej), "match": match}


def _ramp_view(nid: str) -> dict:
    n = nm.nodes[nid]
    rp = n.get("ramp")
    if not rp:
        return {"node": nid, "ramp": None, "allow_live": bool(n.get("allow_live"))}
    m = _ramp_metrics(nid, rp["stage_started"])
    ev = ramp_mod.evaluate(rp, m)
    base = nm.effective_config(nid).get("trade_size", "20000")
    rt = nm.rt.get(nid) or {}
    mult, lot = ramp_mod.multiplier(rp), rp["config"]["lot"]
    why = ramp_mod.size_problem(base, mult, lot)
    return {"node": nid, "allow_live": bool(n.get("allow_live")), "ramp": rp, "multiplier": mult, "size_problem": why,
            "base_trade_size": base, "live_trade_size": None if why else ramp_mod.scaled_size(base, mult, lot),
            "running_size": (rt.get("live_size") if nm._running(nid) else None), "cap_usd": ramp_mod.stage_cap(rp),
            "days": ramp_mod.business_days(rp["stage_started"]), "metrics": m, "evaluation": ev,
            "restart_pending": bool(rt.get("restart_when_flat"))}


async def _ramp_change(nid: str, action: str, by: str, why: str) -> bool:
    rp = nm.nodes[nid]["ramp"]
    changed = ramp_mod.apply(rp, action, by=by, why=why)
    await asyncio.to_thread(nm.save)                       # file write off the event loop
    audit("live_ramp", node=nid, action=action, stage=rp["stage"], status=rp["status"], multiplier=ramp_mod.multiplier(rp), by=by, why=why)
    if changed and nm._running(nid):
        nm.rt.setdefault(nid, {})["restart_when_flat"] = True              # new size takes effect once the node has no position
    return changed


def _is_flat(nid: str) -> bool:
    lv = nm.live(nid) or {}
    return not lv.get("positions") and not lv.get("open_orders")


async def ramp_loop(interval: float = 600.0) -> None:
    """Every 10 minutes: evaluate ramp for nodes running on live · auto step-back/stop · advance per auto_advance · restart when flat"""
    while True:
        await asyncio.sleep(interval)
        try:
            await ramp_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("ramp loop:", e)


async def ramp_tick() -> list[dict]:
    acted = []
    live_gw = nb.resolve_gateway().get("mode") == "live"
    for nid in list(nm.nodes):
        n = nm.nodes[nid]
        rp, rt = n.get("ramp"), nm.rt.get(nid) or {}
        if rt.get("restart_when_flat") and nm._running(nid) and _is_flat(nid):
            rt["restart_when_flat"] = False
            await asyncio.to_thread(_node_call, nm.restart, nid)
            audit("live_ramp_restart", node=nid, multiplier=ramp_mod.multiplier(rp))
            acted.append({"node": nid, "action": "restart"})
        if not (live_gw and rp and n.get("allow_live") and rp.get("status") == "active" and nm._running(nid)):
            continue
        view = await asyncio.to_thread(_ramp_view, nid)
        ev = view["evaluation"]
        act, why = ev["action"], "; ".join(ev["reasons"])
        if act == "pause":
            await _ramp_change(nid, "pause", "auto", why)
            await asyncio.to_thread(_node_call, nm.stop, nid, f"live ramp paused: {why}")
            notify.fire(f"Node {nid}: live ramp paused and node stopped — {why}", "error")
        elif act == "demote":
            if rp["stage"] == 0:
                await _ramp_change(nid, "pause", "auto", why)
                await asyncio.to_thread(_node_call, nm.stop, nid, f"live ramp paused: {why}")
            else:
                await _ramp_change(nid, "demote", "auto", why)
            notify.fire(f"Node {nid}: live ramp stepped down to {ramp_mod.multiplier(rp):.0%} — {why}", "error")
        elif act == "advance":
            if rp["config"]["auto_advance"]:
                await _ramp_change(nid, "advance", "auto", why)
                notify.fire(f"Node {nid}: live ramp advanced to {ramp_mod.multiplier(rp):.0%} size", "agent")
            elif not rp.get("ready"):
                rp["ready"] = True
                await asyncio.to_thread(nm.save)
                notify.fire(f"Node {nid}: ready to step up from {ramp_mod.multiplier(rp):.0%} — approve in the Terminal", "agent")
        else:
            continue
        acted.append({"node": nid, "action": act, "why": why})
    return acted


@app.get("/live/readiness")
async def live_readiness(node: str = ""):
    """Step 14: conditions before real money — account level + node level (if given)"""
    checks = await asyncio.to_thread(_live_readiness_global)
    g = nb.resolve_gateway()
    acct = active_account() or ""
    checks.append({"name": "live_gateway", "ok": g.get("mode") == "live", "level": "info", "detail": f"gateway mode is {g.get('mode') or 'unknown'}"})
    checks.append({"name": "live_account", "ok": bool(acct) and not acct.upper().startswith("D"), "level": "info",
                   "detail": f"account {acct or '—'} ({'paper' if acct.upper().startswith('D') else 'live' if acct else 'none'})"})
    if node:
        if node not in nm.nodes:
            raise HTTPException(404, f"no node '{node}'")
        n = nm.nodes[node]
        ref = await asyncio.to_thread(_node_ref, node)
        state = sst.state_of(ref or "")
        rp = n.get("ramp") or {}
        checks += [{"name": "node_allow_live", "ok": bool(n.get("allow_live")), "level": "block", "detail": f"Allow live for {node} (Strategies tab)"},
                   {"name": "soak_go", "ok": state in ("soak_go", "live"), "level": "block", "detail": f"{ref} is '{state or 'unknown'}' — needs soak_go"},
                   {"name": "ramp_active", "ok": bool(rp) and rp.get("status") == "active", "level": "block",
                    "detail": "live ramp is paused — resume with the live code" if rp else "no live ramp yet (created by Allow live)"}]
        if rp:
            try:
                why = ramp_mod.size_problem(nm.effective_config(node).get("trade_size", "20000"), ramp_mod.multiplier(rp), rp["config"]["lot"])
            except Exception:                                         # config unreadable: the node cannot start anyway (start reports it)
                why = None
            else:
                checks.append({"name": "ramp_size", "ok": not why, "level": "block", "detail": why or "stage size is at least 1 lot"})
    blocked = [c["name"] for c in checks if c["level"] == "block" and not c["ok"]]
    return {"ready": not blocked, "blocked": blocked, "checks": checks}


@app.get("/nodes/{nid}/ramp")
async def node_ramp(nid: str):
    if nid not in nm.nodes:
        raise HTTPException(404, f"no node '{nid}'")
    return await asyncio.to_thread(_ramp_view, nid)


class RampConfig(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    stages: list[float] | None = None
    min_days: int | None = None
    min_trades: int | None = None
    budget_usd: float | None = None
    max_crashes: int | None = None
    max_rejects: int | None = None
    max_reconcile: int | None = None
    min_match: float | None = None
    auto_advance: bool | None = None


def _admin_only(what: str) -> None:
    op = ops.current.get()
    if ops.list_ops() and (op or {}).get("role") != "admin":
        raise HTTPException(403, f"only an admin may {what}")


@app.put("/nodes/{nid}/ramp")
async def node_ramp_config(nid: str, body: RampConfig):
    if nid not in nm.nodes or not nm.nodes[nid].get("ramp"):
        raise HTTPException(404, f"node '{nid}' has no live ramp (Allow live creates it)")
    _admin_only("change the live ramp")
    rp = nm.nodes[nid]["ramp"]
    try:
        cfg = ramp_mod.clean_config({**rp["config"], **body.model_dump(exclude_none=True)})
    except ValueError as e:
        raise HTTPException(400, str(e))
    rp["config"] = cfg
    rp["stage"] = min(rp["stage"], len(cfg["stages"]) - 1)
    await asyncio.to_thread(nm.save)
    audit("live_ramp_config", node=nid, config=cfg)
    return await asyncio.to_thread(_ramp_view, nid)


class RampAction(BaseModel):
    live_code: str | None = None
    force: bool = False
    reason: str = ""


@app.post("/nodes/{nid}/ramp/{action}")
async def node_ramp_action(nid: str, action: Literal["advance", "demote", "resume", "reset"], body: RampAction | None = None):
    """advance: must meet the criteria (force only for admin + live code) · demote: always allowed · resume/reset: live code required"""
    body = body or RampAction()
    if nid not in nm.nodes or not nm.nodes[nid].get("ramp"):
        raise HTTPException(404, f"node '{nid}' has no live ramp (Allow live creates it)")
    who = (ops.current.get() or {}).get("id", "") or "operator"
    if action == "advance":
        view = await asyncio.to_thread(_ramp_view, nid)
        if view["evaluation"]["action"] != "advance":
            if not body.force:
                raise HTTPException(409, "not ready to step up — " + "; ".join(view["evaluation"]["reasons"]))
            _admin_only("force a ramp step")
            require_live_ok(body.live_code, target=f"ramp_force:{nid}")
    elif action in ("resume", "reset"):
        require_live_ok(body.live_code, target=f"ramp_{action}:{nid}")
    changed = await _ramp_change(nid, action, who, body.reason or action)
    return {**(await asyncio.to_thread(_ramp_view, nid)), "changed": changed}


# ---------- portfolio risk (step 16) ----------
from qx_strategies import portfolio as pf  # noqa: E402

_pf_state: dict = {"stats": None, "stats_loading": False, "last": None, "prev": None, "alerts": {}, "fills": []}


def _pf_stats_file() -> Path:
    return DATA_DIR / "portfolio_stats.json"


def _pf_limits() -> dict:
    acct = active_account() or ""
    own = ((_risk_doc().get("accounts") or {}).get(acct) or {}).get("portfolio")
    return pf.clean_limits(own or (_risk_doc().get("default") or {}).get("portfolio"))


def _build_pf_stats() -> dict:
    """Daily returns of each currency vs USD from the data catalog → correlated groups + data for VaR (once a day)"""
    from qx_strategies.backtest import load_bars
    from .catalog import DEFAULT_SYMBOLS
    lim = _pf_limits()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(lim["lookback_days"] * 1.6) + 7)
    syms = set(DEFAULT_SYMBOLS)
    for nid in nm.nodes:
        try:
            syms.add(nm.effective_config(nid)["instrument"].split(".")[0].replace("/", ""))
        except Exception:
            pass
    closes, last = {}, {}
    for sym in sorted(syms):
        inst = f"{sym[:3]}/{sym[3:]}.IDEALPRO"
        try:
            bars = load_bars({"instrument": inst, "tf_minutes": 5}, start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            bars = []
        daily = {}
        for b in bars:
            daily[datetime.fromtimestamp(b.ts_event / 1e9, timezone.utc).date().isoformat()] = float(b.close)
        if daily:
            closes[sym] = sorted(daily.items())
            last[sym] = closes[sym][-1][1]
    dates, rets = pf.currency_returns(closes)
    n = int(lim["lookback_days"])
    rets = {c: r[-n:] for c, r in rets.items()}
    clusters, signs, cm = pf.clusters_from(rets, lim["corr_threshold"])
    rates = {"USD": 1.0}
    for sym, px in last.items():
        base, quote = sym[:3], sym[3:]
        if quote == "USD":
            rates[base] = px
        elif base == "USD" and px:
            rates[quote] = 1 / px
    doc = {"asof": end.isoformat(timespec="seconds"), "days": len(dates[-n:]), "dates": dates[-n:], "returns": rets,
           "clusters": clusters, "signs": signs, "corr": cm, "rates": rates, "threshold": lim["corr_threshold"]}
    _pf_stats_file().write_text(json.dumps(doc))
    return doc


def _pf_stats() -> dict:
    st = _pf_state["stats"]
    if st is None:
        try:
            st = json.loads(_pf_stats_file().read_text())
        except Exception:
            st = {}
        _pf_state["stats"] = st
    stale = not st.get("asof") or datetime.fromisoformat(st["asof"]).date() < datetime.now(timezone.utc).date()
    if stale and not _pf_state["stats_loading"]:
        _pf_state["stats_loading"] = True

        async def _refresh():
            try:
                _pf_state["stats"] = await asyncio.to_thread(_build_pf_stats)
            except Exception as e:
                print("portfolio stats:", e)
            finally:
                _pf_state["stats_loading"] = False
        try:
            asyncio.get_running_loop().create_task(_refresh())
        except RuntimeError:
            _pf_state["stats_loading"] = False
    return st


def _fx_rates(stats: dict) -> dict:
    rates = dict(stats.get("rates") or {"USD": 1.0})
    for k, t in list(_tickers.items()):
        mkt, sym = parse_key(k)
        if mkt != "FX" or len(sym) != 6:
            continue
        try:
            px = _px(t.marketPrice())
        except Exception:
            px = 0.0
        if px > 0:
            base, quote = sym[:3], sym[3:]
            if quote == "USD":
                rates[base] = px
            elif base == "USD":
                rates[quote] = 1 / px
    return rates


def _pf_equity_file() -> Path:
    return DATA_DIR / "portfolio_equity.json"


def _pf_drawdown(netliq: float) -> dict:
    acct = active_account() or "_"
    try:
        doc = json.loads(_pf_equity_file().read_text())
    except Exception:
        doc = {}
    e = doc.get(acct) or {}
    if not e.get("peak") or netliq > e["peak"]:
        e = {"peak": netliq, "peak_ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        doc[acct] = e
        _pf_equity_file().write_text(json.dumps(doc))
    return {**e, "dd_pct": round(max(0.0, (e["peak"] - netliq) / e["peak"] * 100), 3) if e["peak"] else 0.0}


def _pf_reset_peak() -> None:
    acct = active_account() or "_"
    try:
        doc = json.loads(_pf_equity_file().read_text())
    except Exception:
        doc = {}
    doc.pop(acct, None)
    _pf_equity_file().write_text(json.dumps(doc))


async def _portfolio_snapshot() -> dict | None:
    """Overview from real cash balances per currency in the account (not virtual positions) + NetLiq / ExcessLiquidity"""
    if not ib.isConnected() or not active_account():
        return None
    vals = await account_values()
    netliq, excess = _f(vals.get("NetLiquidation")), _f(vals.get("ExcessLiquidity"))
    if netliq <= 0:
        return None
    bal, ledger = {}, {}
    for v in ib.accountValues(active_account()):
        if v.currency in ("USD", "BASE", ""):
            continue
        if v.tag == "$LEDGER-CashBalance":
            ledger[v.currency] = _f(v.value)
        elif v.tag == "CashBalance":
            bal[v.currency] = _f(v.value)
    bal.update(ledger)
    stats = _pf_stats()
    rates = _fx_rates(stats)
    exp = {c: round(q * rates[c], 2) for c, q in bal.items() if abs(q) >= 1 and rates.get(c)}
    limits = _pf_limits()
    lim = pf.limits_usd(limits, netliq)
    m = pf.metrics(exp, stats)
    dd = _pf_drawdown(netliq)
    util = {k: (round(m[k] / lim[k] * 100, 1) if m.get(k) is not None and lim.get(k) else None) for k in pf.METRICS}
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "account": active_account(), "netliq": netliq,
            "excess_liquidity": excess, "excess_pct": round(excess / netliq * 100, 2), "exposure": exp, "exposure_all": pf.with_usd(exp),
            "unpriced": sorted(c for c in bal if abs(bal[c]) >= 1 and not rates.get(c)), "rates": rates, "metrics": m, "limits": limits,
            "limits_usd": {k: round(v, 2) for k, v in lim.items()}, "utilization": util, "drawdown": dd,
            "stats": {"asof": stats.get("asof"), "days": stats.get("days"), "clusters": stats.get("clusters") or [], "corr": stats.get("corr") or {},
                      "signs": stats.get("signs") or {}}, "_returns": stats.get("returns") or {}}


async def _portfolio_payload() -> dict | None:
    try:
        snap = await _portfolio_snapshot()
    except Exception as e:
        print("portfolio snapshot:", e)
        snap = None
    _pf_state["prev"], _pf_state["last"] = _pf_state["last"], snap
    if not snap:
        return None
    return {"netliq": snap["netliq"], "exposure": snap["exposure"], "rates": snap["rates"], "clusters": snap["stats"]["clusters"],
            "signs": snap["stats"]["signs"], "returns": snap["_returns"], "limits_usd": snap["limits_usd"]}


async def _portfolio_watch() -> list[str]:
    """After sending the payload: alert when cap usage ≥ alert_pct · drawdown / margin exceeded = whole account reduce-only
    · API layer: any cap exceeded and worsened after a node fill → pause that node"""
    snap, prev = _pf_state["last"], _pf_state["prev"]
    if not snap:
        return []
    acted, lim = [], snap["limits"]
    today = datetime.now(timezone.utc).date().isoformat()
    for k, u in snap["utilization"].items():
        if u is not None and u >= lim["alert_pct"] and _pf_state["alerts"].get(k) != today:
            _pf_state["alerts"][k] = today
            notify.fire(f"Portfolio {k} at {u:.0f}% of its limit ({snap['metrics'][k]:,.0f} / {snap['limits_usd'][k]:,.0f} USD)", "agent")
            acted.append(f"alert:{k}")
    r = load_risk()
    if not r.get("halted"):
        why = ""
        if snap["drawdown"]["dd_pct"] >= lim["max_dd_pct"]:
            why = f"portfolio drawdown {snap['drawdown']['dd_pct']:.2f}% ≥ {lim['max_dd_pct']:g}% (peak {snap['drawdown']['peak']:,.0f})"
        elif snap["excess_pct"] < lim["min_excess_liq_pct"]:
            why = f"excess liquidity {snap['excess_pct']:.1f}% < {lim['min_excess_liq_pct']:g}% of NetLiq"
        if why:
            r["halted"], r["halt_reason"] = True, why
            save_risk(r)
            audit("halt", reason=why, source_check="portfolio")
            notify.fire(f"TRADING HALTED: {why} — all nodes reduce-only until an admin resumes", "halt")
            await publish_risk()
            acted.append("halt")
    if prev:
        since = time.time() - 30
        for k in pf.METRICS:
            now_v, before = snap["metrics"].get(k), prev["metrics"].get(k)
            if now_v is None or before is None or not snap["limits_usd"].get(k):
                continue
            if now_v > snap["limits_usd"][k] and now_v > before + 1:
                nodes = sorted({n for t, n in _pf_state["fills"] if t >= since})
                for n in nodes:
                    if holds.add(n, f"portfolio {k} over its limit after this node's fill ({now_v:,.0f} > {snap['limits_usd'][k]:,.0f} USD)"):
                        acted.append(f"hold:{n}")
                audit("portfolio_limit_exceeded", metric=k, value=now_v, limit=snap["limits_usd"][k], nodes=nodes)
                notify.fire(f"Portfolio {k} over limit: {now_v:,.0f} > {snap['limits_usd'][k]:,.0f} USD"
                            + (f" — new entries paused for {', '.join(nodes)}" if nodes else " (not from a node fill)"), "error")
        if any(a.startswith("hold:") for a in acted):
            await publish_risk()
    return acted


@app.get("/portfolio/risk")
async def portfolio_risk():
    snap = await _portfolio_snapshot()
    if not snap:
        return {"available": False, "reason": "IB not connected or no account selected", "limits": _pf_limits()}
    snap.pop("_returns", None)
    return {"available": True, **snap}


class PortfolioLimits(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    max_gross_x: float | None = None
    max_currency_pct: float | None = None
    max_cluster_pct: float | None = None
    max_var_pct: float | None = None
    max_dd_pct: float | None = None
    min_excess_liq_pct: float | None = None
    alert_pct: float | None = None
    corr_threshold: float | None = None
    lookback_days: float | None = None


@app.put("/portfolio/limits")
async def portfolio_limits(body: PortfolioLimits):
    _admin_only("change portfolio limits")
    try:
        lim = pf.clean_limits({**_pf_limits(), **body.model_dump(exclude_none=True)})
    except ValueError as e:
        raise HTTPException(400, str(e))
    acct = active_account() or ""
    doc = _risk_doc()
    if acct:
        doc["accounts"].setdefault(acct, {})["portfolio"] = lim
    else:
        doc["default"]["portfolio"] = lim
    _save_risk_doc(doc)
    audit("portfolio_limits", account=acct, **lim)
    if body.corr_threshold is not None or body.lookback_days is not None:
        _pf_state["stats"] = {}
    await publish_risk()
    return lim


@app.post("/portfolio/stats/refresh")
async def portfolio_stats_refresh():
    _pf_state["stats"] = await asyncio.to_thread(_build_pf_stats)
    st = _pf_state["stats"]
    return {"asof": st["asof"], "days": st["days"], "clusters": st["clusters"], "corr": st["corr"]}


# ---------- watchdog / sleep / backup (step 15) ----------
from . import backup as backup_mod  # noqa: E402
from .power import PowerGuard  # noqa: E402
from .wake import WakeWatch  # noqa: E402

power = PowerGuard()
wake_watch = WakeWatch(paths.data_dir() / "wake.json")
_backup_state: dict = {"running": False, "last_error": "", "last": None}


def _any_node_running() -> bool:
    return any(nm._running(n) for n in list(nm.nodes))


async def wake_loop(interval: float = 15.0) -> None:
    """Every 15 seconds: prevent sleep while nodes are running · detect a recent sleep"""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(power.update, _any_node_running())
            ev = wake_watch.tick()
            if ev:
                asyncio.create_task(_on_machine_gap(ev, delay=45))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("wake loop:", e)


async def _post_gap_reconcile(tries: int = 2, wait: float = 30.0) -> list[dict]:
    """Positions mismatched after the machine comes back (must be seen 2 rounds — heartbeat may not have arrived yet)"""
    bad: dict = {}
    for i in range(tries):
        if i:
            await asyncio.sleep(wait)
        rep = (await positions()).get("reconcile") or {}
        now_bad = {r["key"]: r for r in rep.get("rows", []) if r["status"] in attr.ALERT_STATUSES}
        bad = now_bad if i == 0 else {k: r for k, r in now_bad.items() if k in bad}
        if not bad:
            return []
    return list(bad.values())


async def _on_machine_gap(ev: dict, delay: float = 45.0) -> dict:
    """Machine sleep / restart / API stopped → audit + soak incident + Telegram with positions · position mismatch = pause affected nodes"""
    what = {"sleep": "Mac was asleep", "reboot": "Mac restarted", "api_down": "QOS API was not running"}.get(ev["kind"], ev["kind"])
    audit("machine_gap", **ev)
    for nid, n in list(nm.nodes.items()):
        if n.get("desired"):
            try:
                soak.incident(nid, ev["kind"], f"{what} {ev['minutes']} min ({ev['from']} → {ev['to']})")
            except Exception as e:
                print("gap incident:", e)
    await asyncio.sleep(delay)
    held, open_txt = [], "unknown (IB not connected)"
    for _ in range(8):
        if ib.isConnected():
            break
        await asyncio.sleep(15)
    if ib.isConnected():
        try:
            pos = await positions()
            rows = [r for r in (pos.get("reconcile") or {}).get("rows", []) if abs(r.get("ib_qty") or 0) > 0]
            open_txt = ", ".join(f"{r['key'].split(':')[-1]} {r['ib_qty']:+,.0f}" for r in rows) or "none"
            for r in await _post_gap_reconcile():
                for nd in r.get("nodes") or []:
                    if holds.add(nd["node"], f"{r['status']} {r['key']} after {what.lower()}"):
                        held.append(nd["node"])
        except Exception as e:
            open_txt = f"check failed: {e}"
    if held:
        await publish_risk()
        audit("nodes_held", nodes=held, reason=ev["kind"])
    notify.fire(f"{what} for {ev['minutes']:.0f} min ({ev['from'][11:16]}–{ev['to'][11:16]} UTC). Open positions: {open_txt}."
                + (f" New entries paused for {', '.join(held)} until you confirm positions (Agents › System)." if held else ""),
                "error" if held else "agent")
    return {"event": ev, "open": open_txt, "held": held}


async def backup_loop(interval: float = 1800.0) -> None:
    """Every 30 minutes: at the configured hour (default 06:00), if there is no backup today → back up"""
    while True:
        await asyncio.sleep(interval)
        try:
            if backup_mod.due() and not _backup_state["running"]:
                await _run_backup("schedule")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("backup loop:", e)


async def _run_backup(by: str) -> dict:
    _backup_state["running"] = True
    try:
        r = await asyncio.to_thread(backup_mod.create)
        _backup_state.update(last=r, last_error="")
        audit("backup", file=r["file"], files=r["files"], bytes=r["bytes"], removed=r["removed"], by=by)
        return r
    except Exception as e:
        _backup_state["last_error"] = str(e)[:300]
        audit("backup_failed", error=str(e)[:300], by=by)
        notify.fire(f"QOS backup failed: {e}", "error")
        raise
    finally:
        _backup_state["running"] = False


@app.get("/system/health")
async def system_health():
    """Step 15: sleep prevention, sleep/wake events, watchdog, backup, paused nodes"""
    try:
        wd = json.loads((paths.data_dir() / "watchdog.json").read_text())
        wd["age_s"] = round(time.time() - float(wd.get("ts", 0)))
    except Exception:
        wd = None
    return {"power": await asyncio.to_thread(power.status), "nodes_running": _any_node_running(), "events": wake_watch.recent(),
            "watchdog": wd, "backup": {"settings": backup_mod.load_settings(), "last": backup_mod.last_backup(), **_backup_state},
            "holds": holds.load()}


@app.get("/system/backup")
async def system_backup_list():
    return {"settings": backup_mod.load_settings(), "backups": await asyncio.to_thread(backup_mod.list_backups), **_backup_state}


class BackupSettings(BaseModel):
    dir: str | None = None
    hour: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    include_env: bool | None = None


@app.put("/system/backup")
async def system_backup_settings(body: BackupSettings):
    _admin_only("change backup settings")
    try:
        s = backup_mod.save_settings(body.model_dump(exclude_none=True))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    audit("backup_settings", **s)
    return s


@app.post("/system/backup/run")
async def system_backup_run():
    if _backup_state["running"]:
        raise HTTPException(409, "a backup is already running")
    try:
        return await _run_backup((ops.current.get() or {}).get("id", "") or "operator")
    except Exception as e:
        raise HTTPException(500, f"backup failed: {e}")


from . import cloud_api  # noqa: E402  (step 34: QOS Cloud connector)
app.include_router(cloud_api.router)


@app.get("/system/holds")
async def system_holds():
    return holds.load()


@app.post("/nodes/{nid}/release_hold")
async def node_release_hold(nid: str):
    """Operator has checked positions → let the node open new positions again"""
    if not holds.release(nid):
        raise HTTPException(404, f"node '{nid}' is not on hold")
    await publish_risk()
    audit("node_hold_released", node=nid, operator=(ops.current.get() or {}).get("id", ""))
    return {"node": nid, "released": True}


# ---------- paper soak + shadow parity (step 12) ----------
_soak_jobs: dict[str, str] = {}


async def _soak_parity(nid: str) -> dict:
    if nid not in nm.nodes:
        raise HTTPException(404, f"unknown node '{nid}'")
    if _soak_jobs.get(nid) == "running":
        raise HTTPException(409, f"parity for '{nid}' is already running")
    _soak_jobs[nid] = "running"
    try:
        res = await asyncio.to_thread(soak.run_parity, nid, nm.effective_config(nid))
        _soak_jobs[nid] = "done"
        return res
    except ValueError as e:
        _soak_jobs[nid] = f"error: {e}"
        raise HTTPException(400, str(e))
    except Exception as e:
        _soak_jobs[nid] = f"error: {e}"
        raise HTTPException(500, f"parity failed: {e}")


async def soak_loop() -> None:
    """Every 30 minutes: after 06:00 (catalog finishes at 05:20) run parity once a day per node with a heartbeat in the last 2 days"""
    while True:
        await asyncio.sleep(1800)
        try:
            if datetime.now().hour < 6:
                continue
            today = datetime.now().date().isoformat()
            for nid in list(nm.nodes):
                rep_ = await asyncio.to_thread(soak.report, nid)
                last = rep_.get("parity_ts")
                if not rep_.get("since") or (last and datetime.fromisoformat(last).astimezone().date().isoformat() == today):
                    continue
                if not any(d.get("alive_min") for d in (await asyncio.to_thread(soak.report, nid, True)).get("daily", [])[-2:]):
                    continue
                try:
                    r = await _soak_parity(nid)
                    lvb = r.get("live_vs_backtest") or {}
                    audit("soak_parity", node=nid, match_rate=lvb.get("match_rate"), divergences=lvb.get("divergences"))
                except HTTPException as e:
                    print("soak parity", nid, e.detail)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("soak loop:", e)


@app.get("/soak")
async def soak_overview():
    """GO / NO-GO / PENDING summary per node"""
    nodes = [await asyncio.to_thread(soak.report, nid) for nid in sorted(nm.nodes)]
    return {"criteria": soak.criteria(), "nodes": nodes, "jobs": _soak_jobs}


@app.get("/soak/{nid}")
async def soak_detail(nid: str):
    if nid not in nm.nodes:
        raise HTTPException(404, f"unknown node '{nid}'")
    return {**(await asyncio.to_thread(soak.report, nid, True)), "job": _soak_jobs.get(nid, "")}


@app.post("/soak/{nid}/parity")
async def soak_run_parity(nid: str):
    res = await _soak_parity(nid)
    lvb = res.get("live_vs_backtest") or {}
    audit("soak_parity", node=nid, match_rate=lvb.get("match_rate"), divergences=lvb.get("divergences"))
    return res


@app.post("/soak/{nid}/reset")
async def soak_reset(nid: str):
    if nid not in nm.nodes:
        raise HTTPException(404, f"unknown node '{nid}'")
    start = soak.reset(nid)
    audit("soak_reset", node=nid, start=start)
    return {"node": nid, "since": start}


def _ib_num(x) -> float | None:
    """IBKR sends 1.797e308 (UNSET_DOUBLE) for values it has not computed yet → None, never a number"""
    v = _f(x)
    return v if abs(v) < 1e15 else None


@app.get("/fills")
async def fills(limit: int = 50, days: int = 7):
    """Executions: live from IBKR (current trading day only, New York time) combined with our database history for the last N days
    so the Fills tab is not empty after IBKR rolls over the day"""
    await ensure_connected()
    rows = []
    seen = set()
    try:
        live = await asyncio.wait_for(ib.reqExecutionsAsync(ExecutionFilter()), 10)
    except Exception as e:
        print("fills: reqExecutions failed:", e)
        live = ib.fills()
    for f in live:
        e, cr = f.execution, f.commissionReport
        if not _mine(e.acctNumber or ""):
            continue
        key = key_of_contract(f.contract)
        seen.add(e.execId)
        rows.append({
            "time": ((e.time if e.time.tzinfo else e.time.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()) if e.time else None,
            "key": key, "symbol": key.split(":", 1)[1], "side": e.side,
            "qty": _f(e.shares), "price": _f(e.price),
            "commission": _ib_num(cr.commission) if cr else None,
            "realized_pnl": _ib_num(cr.realizedPNL) if cr and cr.realizedPNL else None,
            "order_id": e.orderId, "order_ref": (f.execution.orderRef or "manual"), "origin": "ibkr",
            **_fill_source(e.orderRef),
        })
    # fill in from the database (fills saved by the recorder) for the last `days` days
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        con = history.connect()
        q = "SELECT * FROM fills WHERE ts >= ?" + (" AND account = ?" if active_account() else "") + " ORDER BY ts DESC LIMIT ?"
        args = [since] + ([active_account()] if active_account() else []) + [limit * 3]
        for r in con.execute(q, args):
            if r["exec_id"] in seen:
                continue
            seen.add(r["exec_id"])
            rows.append({"time": r["ts"], "key": r["key"], "symbol": r["symbol"], "side": "BOT" if r["side"] == "BUY" else "SLD",
                         "qty": r["qty"], "price": r["price"], "commission": r["commission"], "realized_pnl": r["realized_pnl"],
                         "order_id": r["order_id"], "order_ref": r["order_ref"] or "manual", "origin": "history",
                         "source": r["source"] or attr.classify(r["order_ref"], r["node"]), "node": r["node"], "strategy": r["strategy"]})
        con.close()
    except Exception as e:
        print("fills: history merge failed:", e)
    rows.sort(key=lambda r: r["time"] or "", reverse=True)
    return {"fills": rows[:limit]}


# ---------- trading ----------
class OrderRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    key: str
    side: Literal["BUY", "SELL"]
    qty: float = Field(gt=0)
    order_type: Literal["MKT", "LMT", "STP", "TRAIL"] = "MKT"
    price: float | None = None
    tif: Literal["DAY", "GTC"] = "DAY"
    take_profit: float | None = None
    stop_loss: float | None = None
    trail_amount: float | None = None
    cash_qty: float | None = Field(default=None, gt=0)   # crypto: USD cash amount (used instead of qty)


async def _current_qty(key: str) -> float:
    return position_qty(key)


@app.post("/order")
async def place_order(req: OrderRequest):
    if load_mode() != "manual":
        raise HTTPException(409, "Manual trading is disabled while in Agent mode (nodes trade automatically)")
    return await submit_order("manual", req.key, req.side, req.qty, req.order_type, req.price, req.tif,
                              req.take_profit, req.stop_loss, req.trail_amount, cash_qty=req.cash_qty)


# ---------- two-step orders (used by the AI assistant via MCP — prepare → confirm) ----------
_tickets: dict[str, dict] = {}
_assistant_orders: list[float] = []


def _assistant_rate_ok() -> bool:
    import time as _t
    now = _t.time()
    _assistant_orders[:] = [t for t in _assistant_orders if now - t < 3600]
    pending = sum(1 for t in _tickets.values() if t.get("role") == "assistant" and t["expires"] > now)
    return len(_assistant_orders) + pending < int(load_assistant()["max_orders_per_hour"])


@app.post("/order/prepare")
async def prepare_order(req: OrderRequest):
    """Check everything as if sending for real (reference price, notional, risk gate, assistant limits) but do not send → return a ticket to confirm"""
    import secrets as _sec
    import time as _t
    if load_mode() != "manual":
        raise HTTPException(409, "Manual trading is disabled while in Agent mode (nodes trade automatically)")
    await ensure_connected()
    mkt, sym = parse_key(req.key)
    k = f"{mkt}:{sym}"
    if not market_tradable(mkt):
        raise HTTPException(400, f"{k}: {market_block_reason(mkt)}")
    t = await ticker_for(k)
    ref = _px(t.ask if req.side == "BUY" else t.bid) or _px(t.last) or _px(t.close)
    if ref == 0.0:
        raise HTTPException(409, "No reference price (market closed?)")
    usd_per_unit = await _usd_per_unit(mkt, sym, ref)
    notional = req.qty * usd_per_unit
    await risk_gate(notional, k)
    _check_position(k, req.side, req.qty, usd_per_unit)
    cur = position_qty(k)
    projected = cur + (req.qty if req.side == "BUY" else -req.qty)
    op = ops.current.get() or {}
    checks = ["risk gate ok"]
    if op.get("role") == "assistant":
        a = load_assistant()
        if notional > _f(a["max_order_notional_usd"]):
            raise HTTPException(400, f"Assistant limit: order notional {notional:,.0f} USD exceeds {_f(a['max_order_notional_usd']):,.0f}")
        # worst case incl. working sell orders and unconfirmed assistant tickets: two tickets may not add up to a short
        _, sells = _working_qty(k)
        pend = sum(t["req"]["qty"] for t in _tickets.values() if t["key"] == k and t["account"] == active_account()
                   and t["req"]["side"] == "SELL" and t["expires"] > _t.time())
        lowest = cur - sells - pend - (req.qty if req.side == "SELL" else 0.0)
        if lowest < 0 and not a["allow_short"]:
            raise HTTPException(400, "Assistant limit: short positions are not allowed (incl. working orders and pending tickets)")
        if not _assistant_rate_ok():
            raise HTTPException(429, f"Assistant limit: more than {a['max_orders_per_hour']} orders in the last hour")
        checks.append("assistant limits ok")
    token = _sec.token_urlsafe(12)
    ttl = int(load_assistant()["confirm_ttl_s"])
    _tickets[token] = {"req": req.model_dump(), "key": k, "ref": ref, "notional": notional, "expires": _t.time() + ttl,
                       "operator": op.get("id", "local"), "role": op.get("role", ""), "account": active_account()}
    audit("order_prepared", token=token, key=k, side=req.side, qty=req.qty, type=req.order_type, notional=round(notional, 2))
    return {"token": token, "expires_in_s": ttl, "account": active_account(), "key": k, "side": req.side, "qty": req.qty,
            "order_type": req.order_type, "price": req.price, "reference_price": ref, "notional_usd": round(notional, 2),
            "position_now": cur, "position_after": projected, "take_profit": req.take_profit, "stop_loss": req.stop_loss,
            "checks": checks, "message": f"Ready: {req.side} {req.qty:g} {k} {req.order_type} ≈ {notional:,.0f} USD on {active_account()}. Call confirm with this token within {ttl}s."}


class ConfirmBody(BaseModel):
    token: str


APPROVAL_TTL_S = 300                         # how long an assistant order waits for the operator (auto-confirm off)
_ticket_done: dict[str, dict] = {}           # token → outcome of an approval (kept 15 min so the assistant can ask what happened)


async def _await_approval(token: str, tk: dict) -> dict:
    """Auto-confirm is off: keep the assistant's ticket and ask the operator in the Terminal"""
    import time as _t
    now = _t.time()
    if not tk.get("awaiting"):
        tk["awaiting"] = True
        tk["expires"] = now + APPROVAL_TTL_S
        r = tk["req"]
        audit("order_awaiting_approval", token=token, key=tk["key"], side=r["side"], qty=r["qty"], notional=round(tk["notional"], 2))
        text = f"AI assistant asks to {r['side']} {r['qty']:g} {tk['key']} {r['order_type']} ≈ {tk['notional']:,.0f} USD — approve or reject in the Terminal"
        notify.fire(text, "order")
        await ws_broadcast({"type": "assistant_ticket", "text": text, "ticket": _ticket_view(token, tk)})
    return {"status": "awaiting_approval", "token": token, "expires_in_s": max(0, int(tk["expires"] - now)),
            "message": "Not sent yet: AI auto-confirm is off, so the operator must approve this order in the Terminal. "
                       "Tell the user, then call confirm_order with the same token again to see the outcome."}


@app.post("/order/confirm")
async def confirm_order(body: ConfirmBody):
    import time as _t
    op0 = ops.current.get() or {}
    for k in [k for k, v in _ticket_done.items() if _t.time() - v["ts"] > 900]:
        _ticket_done.pop(k, None)
    if op0.get("role") == "assistant":
        done = _ticket_done.get(body.token)
        if done:                                          # asked again after the operator decided
            return {"approval": done["status"], "status": done["status"], **(done.get("result") or {})}
        held = _tickets.get(body.token)
        if held and held.get("role") == "assistant" and _t.time() <= held["expires"] and not load_assistant()["auto_confirm"]:
            return await _await_approval(body.token, held)
    tk = _tickets.pop(body.token, None)
    if not tk:
        raise HTTPException(404, "unknown or already-used order token — call /order/prepare again")
    if _t.time() > tk["expires"]:
        audit("order_confirm_expired", token=body.token, key=tk["key"])
        raise HTTPException(410, "order token expired — prepare again")
    if tk["account"] != active_account():
        raise HTTPException(409, "active account changed since the order was prepared")
    if load_mode() != "manual":
        raise HTTPException(409, "Manual trading is disabled while in Agent mode — the prepared order was not sent")
    op = ops.current.get() or {}
    if op.get("role") == "assistant" and tk.get("role") != "assistant":
        raise HTTPException(403, "the assistant may only confirm tickets it prepared")
    if op.get("role") == "assistant":
        _assistant_orders.append(_t.time())
    r = tk["req"]
    ref = "assistant" if op.get("role") == "assistant" else "manual"
    res = await submit_order(ref, r["key"], r["side"], r["qty"], r["order_type"], r["price"], r["tif"], r["take_profit"], r["stop_loss"], r["trail_amount"], cash_qty=r.get("cash_qty"))
    audit("order_confirmed", token=body.token, key=tk["key"], side=r["side"], qty=r["qty"], order_id=res.get("order_id"))
    if op.get("role") == "assistant":
        text = f"AI assistant order: {r['side']} {r['qty']:g} {tk['key']} {r['order_type']} → {res.get('status')}"
        notify.fire(text, "order")
        await ws_broadcast({"type": "assistant_action", "text": text})
    return res


# ---------- assistant (LLM via MCP) settings ----------
class AssistantBody(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    write_enabled: bool | None = None
    live: bool | None = None
    max_order_notional_usd: float | None = Field(default=None, gt=0)
    max_orders_per_hour: int | None = Field(default=None, ge=1, le=500)
    allow_short: bool | None = None
    allow_agents: bool | None = None
    confirm_ttl_s: int | None = Field(default=None, ge=15, le=600)
    auto_confirm: bool | None = None


@app.get("/assistant")
async def get_assistant():
    import time as _t
    a = load_assistant()
    a["orders_last_hour"] = len([t for t in _assistant_orders if _t.time() - t < 3600])
    a["operator_exists"] = any(o["role"] == "assistant" and o.get("active", True) for o in ops.list_ops())
    return a


@app.put("/assistant")
async def put_assistant(body: AssistantBody):
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if upd.get("live") and not load_risk(account="").get("allow_live"):
        raise HTTPException(403, "Enable 'Allow live trading' first")
    save_assistant(upd)
    audit("assistant_settings", **upd)
    if "write_enabled" in upd or "auto_confirm" in upd:
        a = load_assistant()
        state = ("read-only" if not a["write_enabled"] else
                 "may send orders on its own (auto-confirm ON)" if a["auto_confirm"] else "orders need your approval (ask first)")
        notify.fire(f"AI assistant: {state}", "order")
        await ws_broadcast({"type": "assistant_action", "text": f"AI assistant: {state}"})
    return await get_assistant()


def _ticket_view(token: str, tk: dict) -> dict:
    import time as _t
    r = tk["req"]
    return {"token": token, "key": tk["key"], "side": r["side"], "qty": r["qty"], "order_type": r["order_type"], "price": r.get("price"),
            "reference_price": tk["ref"], "notional_usd": round(tk["notional"], 2), "account": tk["account"],
            "operator": tk.get("operator", ""), "expires_in_s": max(0, int(tk["expires"] - _t.time()))}


@app.get("/assistant/tickets")
async def assistant_tickets():
    """Assistant orders waiting for an operator's approval (auto-confirm off)"""
    import time as _t
    now = _t.time()
    return {"tickets": [_ticket_view(k, t) for k, t in _tickets.items() if t.get("awaiting") and t["expires"] > now]}


@app.post("/assistant/tickets/{token}/approve")
async def assistant_ticket_approve(token: str):
    """An operator (admin) approves an assistant order: it is sent now, through the same checks as any confirm"""
    import time as _t
    tk = _tickets.get(token)
    if not tk or not tk.get("awaiting"):
        raise HTTPException(404, "no assistant order is waiting under this token")
    _tickets.pop(token, None)
    if _t.time() > tk["expires"]:
        _ticket_done[token] = {"status": "expired", "ts": _t.time()}
        raise HTTPException(410, "the approval window has passed — the assistant must prepare the order again")
    if tk["account"] != active_account():
        raise HTTPException(409, "active account changed since the order was prepared")
    if load_mode() != "manual":
        raise HTTPException(409, "Manual trading is disabled while in Agent mode — switch to Manual, then approve")
    a = load_assistant()
    if not a["write_enabled"]:
        raise HTTPException(403, "AI assistant is in read-only mode")
    if tk["account"] and not tk["account"].startswith("D") and not a["live"]:
        raise HTTPException(403, "AI assistant may only act on paper accounts")
    r = tk["req"]
    _assistant_orders.append(_t.time())
    res = await submit_order("assistant", r["key"], r["side"], r["qty"], r["order_type"], r["price"], r["tif"], r["take_profit"], r["stop_loss"], r["trail_amount"], cash_qty=r.get("cash_qty"))
    audit("order_approved", token=token, key=tk["key"], side=r["side"], qty=r["qty"], order_id=res.get("order_id"), prepared_by=tk.get("operator", ""))
    _ticket_done[token] = {"status": "approved", "ts": _t.time(), "result": res}
    text = f"AI assistant order approved: {r['side']} {r['qty']:g} {tk['key']} {r['order_type']} → {res.get('status')}"
    notify.fire(text, "order")
    await ws_broadcast({"type": "assistant_action", "text": text})
    return res


@app.post("/assistant/tickets/{token}/reject")
async def assistant_ticket_reject(token: str):
    import time as _t
    tk = _tickets.get(token)
    if not tk or not tk.get("awaiting"):
        raise HTTPException(404, "no assistant order is waiting under this token")
    _tickets.pop(token, None)
    _ticket_done[token] = {"status": "rejected", "ts": _t.time()}
    audit("order_rejected_by_operator", token=token, key=tk["key"], side=tk["req"]["side"], qty=tk["req"]["qty"])
    await ws_broadcast({"type": "assistant_action", "text": f"AI assistant order rejected: {tk['req']['side']} {tk['req']['qty']:g} {tk['key']}"})
    return {"token": token, "status": "rejected"}


class ModifyRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    qty: float | None = Field(default=None, gt=0)
    price: float | None = Field(default=None, gt=0)


def _to_tick(v: float | None, tick: float) -> float | None:
    """Nearest multiple of the contract's minimum tick (None stays None)"""
    return None if v is None else round(round(v / tick) * tick, 10)


def _order_filled(tr) -> float:
    """Filled quantity from the order STATUS (ib_async's Order.filledQuantity defaults to UNSET_DOUBLE 1.8e308)"""
    v = _f(getattr(tr.orderStatus, "filled", 0))
    return v if 0 <= v < 1e15 else 0.0


def _check_leg_qty(tr, k: str, new_qty: float) -> None:
    """A TP/SL leg (parentId ≠ 0) only reduces a position; raising its quantity must never let it open a new one:
    · entry still working → leg ≤ the entry's total quantity
    · entry filled → leg ≤ the entry's filled quantity, and the legs protecting one side (SELL legs a long, BUY legs a short)
      may together not exceed that side's position — per bracket the larger of its TP/SL counts (only one of them executes)"""
    o = tr.order
    pid = o.parentId
    working = {x.order.orderId: x for x in ib.openTrades()}
    if pid in working:
        cap = _f(working[pid].order.totalQuantity)
        if new_qty > cap + 1e-9:
            raise HTTPException(400, f"TP/SL quantity {new_qty:,.0f} exceeds its entry order ({cap:,.0f}) — it would open a new position")
        return
    parent = next((x for x in ib.trades() if x.order.orderId == pid), None)
    if parent is not None:
        cap = _order_filled(parent) or _f(parent.order.totalQuantity)
        if new_qty > cap + 1e-9:
            raise HTTPException(400, f"TP/SL quantity {new_qty:,.0f} exceeds its filled entry ({cap:,.0f}) — it would open a new position")
    pos = position_qty(k)
    side_pos = max(pos, 0.0) if o.action == "SELL" else max(-pos, 0.0)
    per_bracket: dict = {}
    for x in working.values():
        xo = x.order
        if xo.action != o.action or key_of_contract(x.contract) != k or not _mine(getattr(xo, "account", "") or ""):
            continue
        xp = getattr(xo, "parentId", 0) or 0
        if xp and xp in working:
            continue                                            # legs of still-working entries are bounded by their entry above
        q = new_qty if xo.orderId == o.orderId else max(0.0, _f(xo.totalQuantity) - _order_filled(x))
        group = xp or ("own", xo.orderId)                       # standalone same-side exit orders count on their own
        per_bracket[group] = max(per_bracket.get(group, 0.0), q)
    total = sum(per_bracket.values())
    if total > side_pos + 1e-9:
        raise HTTPException(400, f"TP/SL legs would total {total:,.0f} against a {side_pos:,.0f} position — they would open a new position")


_min_ticks: dict[int, float] = {}


async def _min_tick(c: Contract) -> float | None:
    """Contract's minimum price increment from IBKR (cached per conId); None if unknown"""
    cid = getattr(c, "conId", 0) or 0
    if cid in _min_ticks:
        return _min_ticks[cid]
    try:
        cds = await asyncio.wait_for(ib.reqContractDetailsAsync(c), 5)
        tick = _f(cds[0].minTick) if cds else 0.0
    except Exception:
        tick = 0.0
    if tick > 0 and cid:
        _min_ticks[cid] = tick
    return tick or None


@app.put("/order/{order_id}")
async def modify_order(order_id: int, req: ModifyRequest):
    await ensure_connected()
    for tr in ib.openTrades():
        if tr.order.orderId != order_id:
            continue
        o = tr.order
        if not _mine(getattr(o, "account", "") or ""):              # multi-account login: only the selected account's orders
            continue
        k = key_of_contract(tr.contract)
        mkt, sym = parse_key(k)
        if load_mode() != "manual":                                 # Agent mode: no manual changes to any working order
            raise HTTPException(409, f"Order {order_id} cannot be modified in Agent mode — switch to Manual or cancel it")
        if req.price is not None:
            tick = await _min_tick(tr.contract)
            if tick:                                                   # IBKR rejects prices off the tick grid
                req.price = _to_tick(req.price, tick)
        async with _order_lock:
            r = load_risk()
            if r.get("halted"):
                raise HTTPException(423, f"Trading halted: {r.get('halt_reason') or 'kill switch'} — cancel instead of modifying")
            if not market_tradable(mkt):
                raise HTTPException(400, f"{k}: {market_block_reason(mkt)}")
            parent_id = getattr(o, "parentId", 0) or 0
            t = await ticker_for(k)
            ref = _px(t.ask if o.action == "BUY" else t.bid) or _px(t.last) or _px(t.close)
            if ref == 0.0:
                raise HTTPException(409, "No reference price — order not modified")
            new_qty = req.qty if req.qty is not None else _f(o.totalQuantity)
            if req.qty is not None and parent_id:
                _check_leg_qty(tr, k, new_qty)
            elif req.qty is not None:
                upu = await _usd_per_unit(mkt, sym, ref)
                await risk_gate(new_qty * upu, k)
                _check_position(k, o.action, new_qty, upu, exclude_order_id=order_id)
            if req.price is not None and o.orderType in ("LMT", "STP", "STP LMT") and abs(req.price - ref) / ref > 0.05:
                raise HTTPException(400, "Price is more than 5% away from market — please check")
            if req.price is not None and o.orderType == "TRAIL" and req.price / ref > 0.05:
                raise HTTPException(400, "Trail amount is more than 5% of the price — please check")
            if req.qty is not None:
                o.totalQuantity = new_qty
            if req.price is not None:
                if o.orderType == "LMT":
                    o.lmtPrice = req.price
                elif o.orderType in ("STP", "TRAIL", "STP LMT"):
                    o.auxPrice = req.price
                else:
                    raise HTTPException(400, f"Cannot set price on {o.orderType} order")
            ib.placeOrder(tr.contract, o)
        audit("modify", order_id=order_id, qty=req.qty, price=req.price)
        await asyncio.sleep(0.5)
        return {"order_id": order_id, "status": tr.orderStatus.status}
    raise HTTPException(404, f"No open order {order_id}")


def _node_of_trade(tr) -> str:
    """Node that placed an order: from its Nautilus orderRef, else from the node's clientId (exec = base + 1)"""
    coid = attr.client_order_id(getattr(tr.order, "orderRef", "") or "")
    if coid:
        try:
            nd = history.node_for(coid)
            if nd and nd[0]:
                return nd[0]
        except Exception:
            pass
    cid = int(getattr(tr.order, "clientId", 0) or 0)
    for nid, base in nb._load_clients().items():
        if cid in (base, base + 1):
            return nid
    return ""


def _live(tr) -> bool:
    done = getattr(tr, "isDone", None)
    return not (done() if callable(done) else False)


async def _open_trades_fresh() -> list:
    """Every open order at IBKR right now, all clients. ib.openTrades() keeps other clients' orders that were already
    cancelled elsewhere (not kept in sync), so checks after a cancel use this"""
    try:
        return list(await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), 10))
    except Exception as e:
        print("reqAllOpenOrders:", e)
        return [t for t in ib.openTrades()]


async def _cancel_trades(trades: list, reason: str) -> list[dict]:
    """Cancel each order through its owner (see api/ib_guard.py): own orders here, a running node's orders by the node,
    the rest by connecting briefly with the owner's clientId"""
    items = ib_guard.plan(trades, IB_CLIENT_ID, _node_of_trade, nm._running, attr.client_order_id)
    by_id = {tr.order.orderId: tr for tr in trades}
    for it in items:
        if it["method"] == "api":
            ib.cancelOrder(by_id[it["order_id"]].order)
        elif it["method"] == "node":
            n = await send_control("cancel_order", node=it["node"], client_order_id=it["client_order_id"], reason=reason)
            if not n:
                it["method"] = "client"                         # nobody listening on Redis → try as the owner client
    for cid, oids in ib_guard.by_client(items).items():
        res = await ib_guard.cancel_as_client(IB_HOST, IB_PORT, cid, oids)
        for it in items:
            if it["method"] == "client" and it["client_id"] == cid and not res["ok"]:
                it["error"] = res["error"]
    audit("cancel_routed", reason=reason, orders=[{k: it[k] for k in ("order_id", "client_id", "node", "method")} for it in items])
    return items


async def _verify_cancelled(items: list[dict], wait: float = 2.0) -> list[dict]:
    """Orders from items still working at IBKR after the cancel"""
    if not items:
        return []
    await asyncio.sleep(wait)
    ids = {it["order_id"] for it in items}
    left = []
    for tr in await _open_trades_fresh():
        if tr.order.orderId in ids and tr.orderStatus.status not in ("Cancelled", "ApiCancelled", "Filled", "Inactive"):
            left.append({"order_id": tr.order.orderId, "client_id": tr.order.clientId, "key": key_of_contract(tr.contract),
                         "status": tr.orderStatus.status})
    return left


async def warn(text: str, event: str = "error", **fields) -> None:
    """Problem the operator must see: Terminal (notification + toast), Telegram/LINE when configured, audit"""
    audit("warning", text=text, **fields)
    notify.fire(text, event)
    await ws_broadcast({"type": "warning", "text": text, "event": event, **fields})


@app.post("/order/{order_id}/cancel")
async def cancel_order(order_id: int):
    await ensure_connected()

    def find(trades):
        return next((tr for tr in trades if tr.order.orderId == order_id and _live(tr)
                     and _mine(getattr(tr.order, "account", "") or "")), None)       # selected account only
    tr = find(ib.openTrades()) or find(await _open_trades_fresh())
    if tr is None:
        raise HTTPException(404, f"No open order {order_id}")
    items = await _cancel_trades([tr], "manual cancel")
    it = items[0]
    audit("cancel", order_id=order_id, key=key_of_contract(tr.contract), method=it["method"], node=it["node"])
    left = await _verify_cancelled(items, 1.0 if it["method"] == "api" else 2.5)
    if left:
        why = it.get("error") or (f"node {it['node']} did not cancel it" if it["method"] == "node" else "IBKR has not confirmed")
        await warn(f"Order {order_id} is still working at IBKR ({left[0]['status']}): {why}. Retry, or use the Kill switch "
                   f"to cancel every order", order_id=order_id)
        return {"order_id": order_id, "status": left[0]["status"], "method": it["method"], "still_working": True, "error": why}
    return {"order_id": order_id, "status": "Cancelled", "method": it["method"], "still_working": False}


def _close_order(k: str, qty: float, ref: str) -> MarketOrder:
    """Market order that flattens `qty` of key k: whole units for FX, 8 decimals and IOC for crypto (IBKR/Paxos rejects
    crypto market DAY orders), on the active account (multi-account logins)"""
    qty = float(round(qty)) if k.startswith("FX:") else round(qty, 8) if k.startswith("CRYPTO:") else qty
    o = MarketOrder("SELL" if qty > 0 else "BUY", abs(qty), tif="IOC" if k.startswith("CRYPTO:") else "DAY")
    o.orderRef = ref
    acct = active_account()
    if acct:
        o.account = acct
    return o


FLATTEN_REFS = ("manual:close", "manual:reverse")       # orderRef prefixes of close / close_all / reverse orders
_flattening: set[str] = set()                           # keys whose close/reverse request is between check and placeOrder


def _pending_flatten(k: str | None = None) -> list[int]:
    """Working close / close-all / reverse orders (not filled, not cancelled) on the selected account, for key k or all keys.
    A second Close sent before the first one fills would flip the position (two terminals, a double click, or an order
    that sits in PreSubmitted for minutes)."""
    out = []
    for tr in ib.openTrades():
        o = tr.order
        if not (getattr(o, "orderRef", "") or "").startswith(FLATTEN_REFS) or not _mine(getattr(o, "account", "") or ""):
            continue
        done = getattr(tr, "isDone", None)
        if (done() if callable(done) else False) or (k is not None and key_of_contract(tr.contract) != k):
            continue
        out.append(o.orderId)
    return out


def _claim_flatten(k: str) -> None:
    """Refuse a close/reverse while one is already working for k; otherwise mark k as in flight (call release later).
    No await between the check and the claim, so two simultaneous requests cannot both pass."""
    ids = _pending_flatten(k)
    if ids or k in _flattening or "*" in _flattening:
        audit("close_refused_pending", key=k, order_ids=ids)
        raise HTTPException(409, f"A close order for {k} is already working (order {', '.join(map(str, ids)) or 'being sent'}). "
                                 "Wait for it to fill, or cancel it in Orders first.")
    _flattening.add(k)


def _bracket_order_ids(k: str) -> list[int]:
    """Working TP/SL legs of key k on the selected account plus the still-working entries of their brackets"""
    trades = [tr for tr in ib.openTrades() if key_of_contract(tr.contract) == k and _mine(getattr(tr.order, "account", "") or "")]
    parents = {tr.order.parentId for tr in trades if getattr(tr.order, "parentId", 0)}
    return [tr.order.orderId for tr in trades if getattr(tr.order, "parentId", 0) or tr.order.orderId in parents]


def _cancel_bracket_legs(k: str, only: set[int] | None = None) -> list[int]:
    """Cancel the working TP/SL legs (parentId ≠ 0) of key k on the selected account — and, when a leg's entry order is
    still working, that entry too (the whole bracket): an entry left without its stop would fill unprotected.
    Returns the cancelled order ids."""
    ids = set(_bracket_order_ids(k))
    if only is not None:
        ids &= only                             # only: the orders that existed before (a reverse keeps newer protection)
    out = []
    for tr in ib.openTrades():
        if tr.order.orderId in ids:
            ib.cancelOrder(tr.order)
            out.append(tr.order.orderId)
    return out


def _node_owners(k: str) -> list[str]:
    """Running nodes whose live heartbeat holds a position in key k"""
    live = {nid: bus.live(nid) for nid in _running_nodes()}
    return sorted(attr.node_positions(live).get(k, {}))


@app.post("/positions/{key:path}/close")
async def close_position(key: str, force: bool = False):
    """Close one position at market (allowed in Agent mode and while halted: it only reduces risk).
    A position held by a running node needs force=true — the node would otherwise re-open or double-close it"""
    if force and (ops.current.get() or {}).get("role") == "assistant":
        raise HTTPException(403, "the assistant may not force-close a position held by a node")
    await ensure_connected()
    mkt, sym = parse_key(key)
    k = f"{mkt}:{sym}"
    owners = _node_owners(k)
    if owners and not force:
        raise HTTPException(409, f"{k} is held by running node {', '.join(owners)} — stop the node first (it closes the positions it opened while the market is open), or close with force=true")
    _claim_flatten(k)
    try:
        c = await contract_for(k)
        qty = await _current_qty(k)
        if mkt == "FX":
            qty = float(round(qty))
        if qty == 0:
            raise HTTPException(404, f"No position in {k}")
        o = _close_order(k, qty, "manual:close")
        legs = _cancel_bracket_legs(k)                 # orphaned TP/SL legs would open a NEW position when touched
        audit("close_position", key=k, qty=qty, mode=load_mode(), force=force, nodes=owners, cancelled_legs=legs)
        trade = ib.placeOrder(c, o)
    finally:
        _flattening.discard(k)
    for _ in range(20):
        await asyncio.sleep(0.2)
        if trade.isDone():
            break
    return {"order_id": trade.order.orderId, "status": trade.orderStatus.status, "avg_fill_price": _f(trade.orderStatus.avgFillPrice)}


# ---------- trading mode & agents ----------
class ModeBody(BaseModel):
    mode: Literal["manual", "agent"]


@app.get("/mode")
async def get_mode():
    return {"mode": load_mode()}


@app.put("/mode")
async def set_mode(body: ModeBody):
    """manual = orders can be placed by hand · agent = manual orders locked (nodes trade per their own Start/Stop state in both modes)"""
    save_mode(body.mode)
    running = _running_nodes()
    audit("mode", mode=body.mode, nodes_running=running)
    return {"mode": body.mode, "agents_running": running, "nodes_running": running}


# ---------- legacy agents → Nautilus nodes (step 13) ----------
@app.get("/agents")
async def list_agents_legacy():
    """Legacy agents have been replaced by Nautilus nodes — use /nodes · returns any not yet migrated"""
    return {"mode": load_mode(), "agents": [], "migrated_to": "/nodes", "pending_migration": agents_migration.pending()}


@app.post("/reconcile/adjust")
async def reconcile_adjust(key: str):
    """Adjust the manual/agent books in an instrument's history to match IBKR (after subtracting node positions) — fixes mismatch from incomplete fill history"""
    acct = active_account()
    rows = (await positions())["positions"]
    mkt, sym = parse_key(key)
    k = f"{mkt}:{sym}"
    if any(r["key"] == k and r.get("unpriced") for r in rows):
        raise HTTPException(409, f"{k}: the IBKR position cannot be sized (no price for the cash balance) — wait for a price, then adjust")
    ib_q = sum(_f(r["position"]) for r in rows if r["key"] == k)
    node_q = _node_qty(acct, k)
    avg = next((_f(r["avg_cost"]) for r in rows if r["key"] == k), 0.0) or None     # never book the adjustment at price 0
    d = await asyncio.to_thread(history.adjust_book, acct, k, ib_q - node_q, "manual reconcile", avg)
    audit("history_adjusted", key=k, target=ib_q - node_q, delta=d, reason="manual reconcile")
    return {"key": k, "ibkr": ib_q, "nodes": node_q, "adjusted_by": d}


def _node_qty(acct: str, k: str) -> float:
    """What the nodes hold in key k: the live heartbeat of nodes that are fresh, fill history for stale / stopped nodes"""
    live = {nid: bus.live(nid) for nid in set(nm.nodes) | set(bus.nodes)}
    npos = attr.node_positions(live).get(k, {})
    hist = {b[5:]: v for b, v in (history.book_positions(acct).get(k) or {}).items() if b.startswith("node:")}
    q = 0.0
    for nid in set(npos) | set(hist):
        age = (live.get(nid) or {}).get("heartbeat_age_s")
        fresh = age is not None and age <= attr.HEARTBEAT_STALE_S
        q += npos.get(nid, {}).get("qty", 0.0) if fresh else hist.get(nid, 0.0)
    return q


@app.post("/agents/migrate")
async def migrate_agents(dry_run: bool = True, force: bool = False, start: bool = True):
    """Migrate data/agents.json → configs/strategies/*.json + node · dry_run=true shows the plan only
    start=true starts nodes for agents that were active · force=true skips the check for open positions of the old agent"""
    acct = active_account()
    ib_pos, ib_avg = None, {}
    if ib.isConnected():
        ib_rows = (await positions())["positions"]
        ib_pos = {r["key"]: _f(r["position"]) for r in ib_rows}
        ib_avg = {r["key"]: _f(r["avg_cost"]) for r in ib_rows}
    plan = agents_migration.plan(nm, history.book_positions(acct), history.open_sources(acct), ib_pos)
    plan["ibkr_positions_checked"] = ib_pos is not None
    if dry_run:
        return plan

    def _align(key: str, target: float) -> float:
        d = history.adjust_book(acct, key, target, "agents migration", ib_avg.get(key) or None)
        audit("history_adjusted", key=key, target=target, delta=d, reason="agents migration")
        return d
    try:
        res = await asyncio.to_thread(agents_migration.apply, nm, plan, force, start and not load_risk().get("halted"), _align)
    except ValueError as e:
        raise HTTPException(409, str(e))
    audit("agents_migrated", created=[r["node"] for r in res["created"]], started=res["started"], skipped=[r["agent"] for r in res["skipped"]])
    return res


@app.post("/positions/close_all")
async def close_all_positions():
    """Cancel all open orders, then close every position with market orders"""
    await ensure_connected()
    pending = _pending_flatten()
    if pending or _flattening:
        audit("close_refused_pending", key="ALL", order_ids=pending)
        raise HTTPException(409, f"Close orders are already working (order {', '.join(map(str, pending)) or 'being sent'}). "
                                 "Wait for them to fill, or cancel them in Orders first.")
    _flattening.add("*")
    try:
        return await _close_all_impl()
    finally:
        _flattening.discard("*")


async def _close_all_impl() -> dict:
    audit("close_all", account=active_account())
    open_now = [tr for tr in await _open_trades_fresh() if _live(tr)
                and _mine(getattr(tr.order, "account", "") or "")]   # multi-account login: the selected account only
    await _cancel_trades(open_now, "close all")
    await asyncio.sleep(0.5)
    results = []
    for p in (await positions())["positions"]:              # includes FX from cash balances (when reqPositions does not report them)
        key = p["key"]
        qty = _f(p["position"])
        if p.get("unpriced"):                                 # USDxxx cash balance without a price: subscribe once and size it
            try:
                t = await ticker_for(key)
                await asyncio.sleep(1.5)
                mp = _mark(t)
            except Exception:
                mp = 0.0
            if not mp:                                        # cannot be sized → reported, never skipped silently
                why = f"skipped: no {key[3:6]}.{key[6:]} price (cash {p.get('cash_balance', 0):,.0f} {p.get('cash_currency', '')})"
                results.append({"key": key, "qty": 0.0, "order_id": None, "status": why})
                notify.fire(f"Close all: {key} NOT closed — {why}", "error")
                continue
            qty = -_f(p.get("cash_balance")) / mp
        if key.startswith("FX:"):
            qty = float(round(qty))
        if qty == 0:
            continue
        try:
            c = await contract_for(key)
        except Exception as e:
            results.append({"key": key, "qty": qty, "order_id": None, "status": f"skipped: {getattr(e, 'detail', e)}"})
            continue
        tr = ib.placeOrder(c, _close_order(key, qty, "manual:close_all"))
        results.append({"key": key, "qty": qty, "order_id": tr.order.orderId})
    await asyncio.sleep(2.0)
    for r in results:
        for tr in ib.trades():
            if r["order_id"] is not None and tr.order.orderId == r["order_id"]:
                r["status"] = tr.orderStatus.status
                r["avg_fill_price"] = _f(tr.orderStatus.avgFillPrice)
    return {"closed": results}


@app.post("/positions/{key:path}/reverse")
async def reverse_position(key: str):
    """Reverse a position: send an order of 2x the size in the opposite direction (opens a new position → Manual mode only)"""
    if load_mode() != "manual":
        raise HTTPException(409, "Manual trading is disabled while in Agent mode — close the position instead")
    await ensure_connected()
    mkt, sym = parse_key(key)
    k = f"{mkt}:{sym}"
    qty = position_qty(k)
    if mkt == "FX":
        qty = float(round(qty))
    if qty == 0:
        raise HTTPException(404, f"No position in {k}")
    side = "SELL" if qty > 0 else "BUY"
    old = set(_bracket_order_ids(k))                    # the old position's TP/SL (and working bracket entries)
    _claim_flatten(k)
    try:
        res = await submit_order("manual:reverse", k, side, abs(qty) * 2)      # a rejected reverse keeps the old protection
    finally:
        _flattening.discard(k)
    if res.get("status") in ORDER_LIVE_STATUSES:
        res["cancelled_legs"] = _cancel_bracket_legs(k, only=old)      # they would act on the new side
        audit("reverse_legs_cancelled", key=k, order_ids=res["cancelled_legs"])
    elif old and res.get("status") not in ORDER_DEAD_STATUSES:     # not confirmed yet (PendingSubmit …): never cancel on unknown
        res["warning"] = "old TP/SL orders are kept until IBKR accepts the reverse order, then cancelled"
        asyncio.create_task(_cancel_legs_when_live(k, res.get("order_id"), old))
    return res


ORDER_LIVE_STATUSES = ("Submitted", "PreSubmitted", "Filled")
ORDER_DEAD_STATUSES = ("Cancelled", "Inactive", "ApiCancelled")


async def _cancel_legs_when_live(k: str, order_id: int | None, old: set[int], timeout_s: float = 120.0) -> None:
    """Reverse order still pending: cancel the old bracket orders once IBKR reports it live; keep them if it dies or times out"""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        tr = next((t for t in ib.trades() if t.order.orderId == order_id), None)
        st = tr.orderStatus.status if tr is not None else ""
        if st in ORDER_LIVE_STATUSES:
            ids = _cancel_bracket_legs(k, only=old)
            audit("reverse_legs_cancelled", key=k, order_ids=ids, delayed=True)
            return
        if st in ORDER_DEAD_STATUSES:
            audit("reverse_legs_kept", key=k, order_id=order_id, status=st)
            return
        await asyncio.sleep(1.0)
    audit("reverse_legs_kept", key=k, order_id=order_id, status="timeout")
    notify.fire(f"Reverse {k}: order {order_id} not confirmed by IBKR — old TP/SL orders were kept, check them", "error")


# ---------- risk panel / kill switch / audit ----------
@app.get("/risk")
async def get_risk():
    r = load_risk()
    r["assistant"] = await get_assistant()
    vals = await account_values() if ib.isConnected() else {}
    d = await daily_pnl() if ib.isConnected() else {"pnl": 0.0, "start_netliq": 0.0, "netliq": 0.0, "date": _today()}
    exposure = []
    gross, partial = 0.0, False
    rates = _usd_rates()
    for p in ib.portfolio() if ib.isConnected() else []:
        if _f(p.position) == 0 or not _mine(getattr(p, "account", "") or ""):
            continue
        key = key_of_contract(p.contract)
        mkt, sym = key.split(":", 1)
        row = _position_row(key, mkt, sym, p.contract.secType, p.contract.currency, _f(p.position), 0.0, _f(p.marketPrice),
                            _f(p.marketValue), _f(p.unrealizedPNL), 0.0, rates)          # IBKR values are in the contract currency
        if row["usd"]:
            gross += abs(row["market_value"])
        else:
            partial = True                                                         # cannot value in USD: left out, flagged
        exposure.append({"key": key, "symbol": sym, "qty": _f(p.position), "market_value": row["market_value"],
                         "unrealized_pnl": row["unrealized_pnl"], "usd": row["usd"], "currency": row["currency"]})
    exposure.sort(key=lambda e: -abs(e["market_value"]))
    netliq = _f(vals.get("NetLiquidation"))
    limit = _f(r.get("max_daily_loss_usd"))
    return {
        "settings": {**{k: r[k] for k in ("max_daily_loss_usd", "max_order_notional_usd", "max_position_notional_usd", "max_open_positions")}, "allow_live": bool(r.get("allow_live"))},
        "assistant": r["assistant"], "account_id": r.get("account", ""),
        "markets_tradable": {m["id"]: market_tradable(m["id"]) for m in MARKETS},
        "markets_probe": _risk_doc()["default"].get("markets_probe", {}),
        "halted": bool(r.get("halted")), "halt_reason": r.get("halt_reason", ""),
        "daily": {**d, "limit": limit, "used_pct": (min(1.0, max(0.0, -d["pnl"] / limit)) * 100) if limit > 0 else 0.0},
        "account": {
            "net_liquidation": netliq,
            "init_margin": _f(vals.get("InitMarginReq")), "maint_margin": _f(vals.get("MaintMarginReq")),
            "excess_liquidity": _f(vals.get("ExcessLiquidity")), "available_funds": _f(vals.get("AvailableFunds")),
            "buying_power": _f(vals.get("BuyingPower")), "gross_exposure": gross, "gross_exposure_partial": partial,
            "leverage": (gross / netliq) if netliq > 0 else 0.0,
        },
        "exposure": exposure,
        "open_positions": len(exposure),
        "working_orders": len(ib.openTrades()) if ib.isConnected() else 0,
        "active_agents": _running_nodes(), "running_nodes": _running_nodes(),
        "mode": load_mode(),
    }


class RiskSettings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    max_daily_loss_usd: float = Field(ge=0)
    max_order_notional_usd: float = Field(gt=0)
    max_position_notional_usd: float = Field(gt=0)
    max_open_positions: int = Field(ge=1, le=100)
    allow_live: bool | None = None          # shared by all accounts (admin only)


@app.put("/risk")
async def put_risk(body: RiskSettings):
    r = load_risk()
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if "allow_live" in upd and (ops.current.get() or {}).get("role") != "admin":
        raise HTTPException(403, "only an admin may change 'Allow live trading'")
    r.update(upd)
    save_risk(r)
    audit("risk_settings", **upd)
    await publish_risk()                       # nodes get the new values immediately (no waiting for the 5-second cycle)
    return await get_risk()


@app.post("/risk/kill")
async def kill_switch(all_accounts: bool = True):
    """Stop everything: lock every account (all_accounts=false locks the selected account only) → nodes kill → stop nodes →
    cancel orders → close positions. Switching to another account afterwards does not re-enable trading."""
    audit("kill_switch", account=active_account(), all_accounts=all_accounts)
    if all_accounts:
        halt_all_accounts(KILL_ALL_REASON)
    notify.fire("KILL SWITCH activated: stopping nodes, cancelling orders, closing positions", "kill")
    # 1) lock first, then tell every Nautilus node via Redis: cancel orders + close positions within ~1 second
    r = load_risk()
    r["halted"] = True
    # no account selected: this save writes the shared default — keep the kill-all reason so its resume clears it
    r["halt_reason"] = KILL_ALL_REASON if all_accounts and not active_account() else "kill switch"
    save_risk(r)
    nodes_notified = await send_control("kill", reason="kill switch", node="*")     # pub/sub first: nodes react within ~1 s
    await publish_risk()
    # 2) stop node processes (nodes also close their own positions when stopping)
    await asyncio.to_thread(nm.stop_all, "kill switch")
    save_mode("manual")
    # 2b) cancel every order at IBKR whoever placed it — the API alone cannot cancel a node's orders (IBKR ties orders
    # to the sending clientId). Global cancel covers every account on this login, so only for a kill on all accounts
    if ib.isConnected():
        if all_accounts:
            ib.reqGlobalCancel()
            audit("global_cancel", reason="kill switch")
        else:
            await _cancel_trades([tr for tr in await _open_trades_fresh() if _live(tr)
                                  and _mine(getattr(tr.order, "account", "") or "")], "kill switch")
        await asyncio.sleep(1.0)
    # 3) close remaining positions across the account (close_all_positions bypasses risk_gate)
    closed = []
    if ib.isConnected():
        res = await close_all_positions()
        closed = res.get("closed", [])
    left = []
    if ib.isConnected():
        left = [{"order_id": tr.order.orderId, "key": key_of_contract(tr.contract), "status": tr.orderStatus.status}
                for tr in await _open_trades_fresh() if _live(tr)
                and not (getattr(tr.order, "orderRef", "") or "").startswith("manual:close")]
        if left:
            await warn(f"Kill switch: {len(left)} order(s) still working at IBKR after the cancel: "
                       + ", ".join(f"#{o['order_id']} {o['key']}" for o in left[:5]), "kill", orders=left)
    audit("kill_switch_done", nodes_notified=nodes_notified, closed=len(closed), orders_left=len(left))
    return {"halted": True, "closed": closed, "nodes_notified": nodes_notified, "orders_left": left}


@app.post("/risk/resume")
async def resume_trading():
    r = load_risk()
    if str(r.get("halt_reason", "")).startswith("portfolio drawdown"):
        _pf_reset_peak()                                     # step 16: resume = restart drawdown tracking from current equity
    r["halted"] = False
    r["halt_reason"] = ""
    save_risk(r)
    # the all-accounts kill switch also halted the shared default (node starts read it) and every account override
    doc = _risk_doc()
    cleared = [a for a, v in doc["accounts"].items() if v.get("halted") and v.get("halt_reason") == KILL_ALL_REASON]
    for a in cleared:
        doc["accounts"][a].update(halted=False, halt_reason="")
    if doc["default"].get("halted") and doc["default"].get("halt_reason") == KILL_ALL_REASON:
        doc["default"].update(halted=False, halt_reason="")
        cleared.append("default")
    if cleared:
        _save_risk_doc(doc)
    audit("resume", cleared=cleared)
    await publish_risk()
    return {"halted": False}


class DailyBaselineBody(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    netliq: float | None = Field(default=None, gt=0)     # omitted = accept the current NetLiq as today's start


@app.post("/risk/daily_baseline")
async def confirm_daily_baseline(body: DailyBaselineBody | None = None):
    """Operator confirms today's daily-loss baseline (NetLiq at the trading-day start) when the API could not record it"""
    d = await daily_pnl()
    if d.get("baseline_confirmed", True):
        raise HTTPException(409, f"Today's daily-loss baseline is already set ({d.get('baseline')}) — it can only be confirmed while unconfirmed")
    vals = await account_values()
    netliq = _f(vals.get("NetLiquidation"))
    start = (body.netliq if body and body.netliq else None) or netliq
    if start <= 0:
        raise HTTPException(409, "No NetLiquidation from IBKR yet — cannot confirm a baseline")
    if start < netliq - 1e-6:                    # a start below today's NetLiq would hide a loss already made
        raise HTTPException(400, f"Baseline {start:,.2f} is below the current NetLiq {netliq:,.2f} — it may not hide today's loss")
    acct = vals.get("account_id") or active_account() or "_"
    try:
        alld = json.loads(DAILY_FILE.read_text())
    except Exception:
        alld = {}
    alld.setdefault("accounts", {})[acct] = {"date": _today(), "start_netliq": start, "baseline": "operator",
                                             "by": (ops.current.get() or {}).get("id", "local")}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_FILE.write_text(json.dumps(alld))
    audit("daily_baseline_confirmed", account=acct, start_netliq=start, netliq=netliq)
    await publish_risk()
    return await daily_pnl()


@app.get("/audit")
async def get_audit(limit: int = 100):
    return {"items": read_audit(min(limit, 500))}


# ---------- history / performance ----------
def _hist_acct(account: str | None) -> str | None:
    """account=ALL → all accounts, omitted → the selected account"""
    if account == "ALL":
        return None
    return account or active_account() or None


@app.post("/history/claim_unassigned")
async def history_claim(account: str | None = None):
    """Assign an account to old pre-migration data (only if this user has a single account, or the account is given explicitly)"""
    acct = account or active_account()
    accts = ib.managedAccounts() if ib.isConnected() else []
    if not account and len(accts) > 1:
        raise HTTPException(400, f"Multiple accounts {accts}: pass ?account= explicitly")
    n = history.claim_unassigned(acct)
    audit("history_claim_unassigned", account=acct, fills=n)
    return {"account": acct, "fills_assigned": n}


@app.get("/history/trades")
async def history_trades(range: str = "all", key: str | None = None, source: str | None = None, limit: int = 500, account: str | None = None,
                         tz: str | None = None):
    """tz (IANA name, e.g. Asia/Bangkok): 'today' / 'week' start at the viewer's local midnight · omitted = UTC"""
    return {"trades": history.list_trades(range, key, source, limit, account=_hist_acct(account), tz=tz)}


@app.get("/history/equity")
async def history_equity(range: str = "all", account: str | None = None, tz: str | None = None):
    pts = history.list_equity(range, account=_hist_acct(account), tz=tz)
    return {"points": pts, "partial": any(p.get("partial") for p in pts)}     # some account had no data for part of the curve


@app.get("/history/stats")
async def history_stats(range: str = "all", account: str | None = None, source: str = "", tz: str | None = None):
    st = history.stats(range, _hist_acct(account), source or None, tz)
    if st["profit_factor"] == float("inf"):
        st["profit_factor"] = 999.0
    return st


class JournalBody(BaseModel):
    note: str = ""
    tags: str = ""
    rating: int = Field(ge=0, le=5, default=0)


@app.put("/history/trades/{first_exec}/journal")
async def history_journal(first_exec: str, body: JournalBody):
    history.set_journal(first_exec, body.note, body.tags, body.rating)
    audit("journal", trade=first_exec, tags=body.tags, rating=body.rating)
    return {"ok": True}


@app.get("/history/export")
async def history_export(what: str = "trades", range: str = "all", account: str | None = None, tz: str | None = None):
    """account omitted / '' = the selected account, ALL = every account"""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(history.export_csv(what, range, _hist_acct(account), tz), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{what}_{range}.csv"'})


@app.post("/history/sync_databricks")
async def history_sync_databricks():
    """Upload trades / fills / equity to a Databricks volume, then COPY INTO the quantx_<name> tables"""
    from dotenv import load_dotenv
    load_dotenv(paths.env_file())
    host, token = os.getenv("DATABRICKS_HOST"), os.getenv("DATABRICKS_TOKEN")
    if not host or not token:
        raise HTTPException(400, "DATABRICKS_HOST / DATABRICKS_TOKEN not set in .env")
    try:
        import io as _io
        import pandas as pd
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.catalog import VolumeType
        from databricks.sdk.service.sql import StatementState
    except ImportError as e:
        raise HTTPException(500, f"missing package: {e}")
    w = WorkspaceClient(host=host, token=token)
    cat, sch, vol = "workspace", "default", "quantx"
    try:
        w.volumes.read(f"{cat}.{sch}.{vol}")
    except Exception:
        w.volumes.create(catalog_name=cat, schema_name=sch, name=vol, volume_type=VolumeType.MANAGED)
    whs = list(w.warehouses.list())
    if not whs:
        raise HTTPException(500, "no SQL warehouse")
    wid = os.getenv("DATABRICKS_WAREHOUSE_ID") or whs[0].id

    def run_sql(sql: str):
        r = w.statement_execution.execute_statement(warehouse_id=wid, statement=sql, wait_timeout="50s")
        import time as _t
        t0 = _t.time()
        while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
            if _t.time() - t0 > 300:
                raise HTTPException(504, "SQL timeout")
            _t.sleep(2)
            r = w.statement_execution.get_statement(r.statement_id)
        if r.status.state != StatementState.SUCCEEDED:
            raise HTTPException(500, f"SQL failed: {r.status.error}")
        return r

    out = {}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    con = history.connect()
    for name, query in (("trades", "SELECT * FROM trades"), ("fills", "SELECT * FROM fills"), ("equity", "SELECT * FROM equity")):
        df = pd.read_sql_query(query, con)
        if df.empty:
            out[name] = 0
            continue
        buf = _io.BytesIO(); df.to_parquet(buf, index=False); buf.seek(0)
        rdir = f"/Volumes/{cat}/{sch}/{vol}/{name}"
        w.files.create_directory(rdir)
        w.files.upload(f"{rdir}/{name}_{stamp}.parquet", buf, overwrite=True)
        # replace the whole table with the latest snapshot (idempotent)
        run_sql(f"CREATE OR REPLACE TABLE {cat}.{sch}.quantx_{name} AS SELECT * FROM parquet.`{rdir}/{name}_{stamp}.parquet`")
        out[name] = int(len(df))
    con.close()
    audit("databricks_sync", **out)
    return {"synced": out, "tables": [f"{cat}.{sch}.quantx_{n}" for n in out]}


# ---------- notifications ----------
@app.get("/notify")
async def get_notify():
    cfg = notify.load()
    cfg["configured"] = notify.configured()
    return cfg


class NotifyBody(BaseModel):
    enabled: bool = True
    events: dict[str, bool] = {}


@app.put("/notify")
async def put_notify(body: NotifyBody):
    cfg = notify.load()
    cfg["enabled"] = body.enabled
    cfg["events"].update(body.events)
    notify.save(cfg)
    audit("notify_settings", enabled=body.enabled)
    cfg["configured"] = notify.configured()
    return cfg


@app.post("/notify/test")
async def notify_test():
    if not notify.configured():
        raise HTTPException(400, "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in bot/.env, then restart the API")
    ok = await notify.send("Test message from QOS Terminal", "info")
    if not ok:
        raise HTTPException(502, "Telegram did not accept the message (check token / chat id)")
    return {"ok": True}


# ---------- websocket ----------
OP_COOKIE = "qos_op"        # operator token cookie set by the web Terminal (HttpOnly, Secure, SameSite=Strict)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not _local_origin(ws.headers.get("origin")) or not _host_ok(ws.headers.get("host")):              # browsers do not apply CORS to WebSockets: refuse other sites
        audit("forbidden_origin", origin=str(ws.headers.get("origin"))[:100], method="WS", path="/ws")
        await ws.close(code=1008)
        return
    if ops.public_mode():                          # public Terminal: the live stream needs a signed-in operator
        tok = ws.query_params.get("op_token") or ws.cookies.get(OP_COOKIE) or ws.headers.get("x-operator-token")
        if ops.resolve(tok) is None:
            audit("ws_refused_no_operator", path="/ws")
            await ws.close(code=1008)
            return
    await ws.accept()
    _ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "conn", **_conn, "ib_connected": ib.isConnected()}, default=str))
        while True:
            await ws.receive_text()   # ping from the client
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)


# ---------- price alerts ----------
class AlertBody(BaseModel):
    key: str
    kind: Literal["above", "below", "change_pct"]
    level: float
    note: str = ""
    repeat: bool = False


@app.get("/alerts")
async def list_alerts():
    return {"alerts": alerts_mod.load()}


@app.post("/alerts")
async def create_alert(body: AlertBody):
    mkt, sym = parse_key(body.key)
    a = alerts_mod.add(f"{mkt}:{sym}", body.kind, body.level, body.note, body.repeat)
    audit("alert_add", **{k: a[k] for k in ("id", "key", "kind", "level")})
    return a


@app.delete("/alerts/{alert_id}")
async def delete_alert(alert_id: str):
    alerts_mod.remove(alert_id)
    return {"ok": True}


# ---------- news & calendar ----------
@app.get("/news")
async def get_news(key: str = "FX:EURUSD", limit: int = 20):
    await ensure_connected()
    c = await contract_for(key)
    return {"key": key, "items": await news_mod.ibkr_news(ib, c, limit)}


@app.get("/news/article")
async def get_article(provider: str, article_id: str):
    await ensure_connected()
    return {"text": await news_mod.ibkr_article(ib, provider, article_id)}


@app.get("/calendar")
async def get_calendar(force: bool = False):
    items = await news_mod.calendar(force)
    r = load_risk()
    age = news_mod.cache_age_s()
    return {"items": items, "news_pause_minutes": int(r.get("news_pause_minutes", 0) or 0),
            "calendar_stale": age is None or age > 6 * 3600}


class NewsPauseBody(BaseModel):
    minutes: int = Field(ge=0, le=240)


@app.put("/calendar/pause")
async def set_news_pause(body: NewsPauseBody):
    r = load_risk()
    r["news_pause_minutes"] = body.minutes
    save_risk(r)
    audit("news_pause", minutes=body.minutes)
    return {"news_pause_minutes": body.minutes}


# ---------- UI settings (layout, etc.) ----------
@app.get("/ui")
async def get_ui():
    try:
        return json.loads(UI_FILE.read_text())
    except Exception:
        return {}


@app.put("/ui")
async def put_ui(body: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UI_FILE.write_text(json.dumps(body, indent=2))
    return body


# ---------- operators (who) + permissions ----------
@app.middleware("http")
async def operator_guard(request, call_next):
    path = request.url.path
    if not _host_ok(request.headers.get("host")):
        from fastapi.responses import JSONResponse
        audit("forbidden_host", host=str(request.headers.get("host"))[:100], method=request.method, path=path)
        return JSONResponse({"detail": "unknown Host header refused (set QOS_TRUSTED_HOSTS to allow it)"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and request.method not in ("GET", "HEAD", "OPTIONS") and origin not in TRUSTED_ORIGINS:
        from fastapi.responses import JSONResponse
        audit("forbidden_origin", origin=origin[:100], method=request.method, path=path)
        return JSONResponse({"detail": "cross-origin write refused"}, status_code=403)
    token = request.headers.get("x-operator-token") or request.query_params.get("op_token")
    op = ops.resolve(token)
    if op is None and not ops.configured():
        if ops.public_mode() and path not in ops.PUBLIC:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "no operator configured: run `qos operator add <name> --role admin` on the server"}, status_code=503)
        if not ops.public_mode():
            # no operators set yet → legacy mode (personal machine): the Terminal and CLI are the local admin, but an AI
            # never is. The MCP server marks its requests, so they get the assistant role and every assistant check.
            # A forged marker can only lower the caller's rights.
            from_mcp = (request.headers.get("x-qos-client", "").strip().lower() == "mcp"
                        or request.headers.get("x-source", "").strip().lower().startswith("mcp"))
            op = ({"id": "local-ai", "name": "Local AI assistant", "role": "assistant"} if from_mcp
                  else {"id": "local", "name": "Local user", "role": "admin"})
    ops.current.set(op)
    _req_meta.set({"source": request.headers.get("x-source", "terminal")[:40], "request_id": request.headers.get("x-request-id", "")[:64]})
    if path in ops.PUBLIC or path.startswith("/docs"):
        return await call_next(request)
    if op and op["role"] == "assistant" and request.method not in ("GET", "HEAD", "OPTIONS"):
        from fastapi.responses import JSONResponse
        a = load_assistant()
        if not a["write_enabled"]:
            audit("assistant_blocked", reason="write_disabled", path=path)
            return JSONResponse({"detail": "AI assistant is in read-only mode. Ask an admin to enable trading for the assistant on the Terminal."}, status_code=403)
        acct = active_account()
        if acct and not acct.startswith("D") and not a["live"]:
            audit("assistant_blocked", reason="live_not_allowed", path=path)
            return JSONResponse({"detail": "AI assistant may only act on paper accounts (admin can enable live)."}, status_code=403)
    if op is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "operator sign-in required"}, status_code=401)
    if not ops.allowed(op["role"], request.method, path):
        from fastapi.responses import JSONResponse
        audit("forbidden", method=request.method, path=path)
        return JSONResponse({"detail": f"role '{op['role']}' may not {request.method} {path}"}, status_code=403)
    return await call_next(request)


class OpLoginBody(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    pin: str = Field(min_length=1, max_length=64)


class OpCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    role: Literal["admin", "trader", "risk", "viewer"] = "trader"
    pin: str = Field(min_length=4, max_length=64)


class OpUpdateBody(BaseModel):
    role: Literal["admin", "trader", "risk", "viewer"] | None = None
    active: bool | None = None
    pin: str | None = Field(default=None, min_length=4, max_length=64)


@app.get("/operators/status")
async def operators_status(request: Request):
    op = ops.resolve(request.headers.get("x-operator-token"))
    return {"configured": ops.configured(), "operator": op and {k: op[k] for k in ("id", "name", "role")},
            "idle_sec": ops.IDLE_SEC, "live_code_set": ops.live_code_set(),
            "operators": [{"id": o["id"], "name": o["name"], "role": o["role"]} for o in ops.list_ops() if o.get("active", True)]}


@app.post("/operators/bootstrap")
async def operators_bootstrap(body: OpCreateBody):
    """Create the first admin (only allowed while no operator exists)"""
    if ops.configured():
        raise HTTPException(409, "operators already configured")
    if ops.public_mode():
        raise HTTPException(403, "public Terminal: create the first admin on the server with `qos operator add <name> --role admin`")
    op = ops.create(body.name, "admin", body.pin, created_by="bootstrap")
    audit("operator_bootstrap", operator_id=op["id"])
    tok, sess = ops.login(op["id"], body.pin)
    return {"token": tok, "operator": {k: sess[k] for k in ("id", "name", "role")}}


@app.post("/operators/login")
async def operators_login(body: OpLoginBody):
    try:
        tok, sess = ops.login(body.id, body.pin)
    except PermissionError as e:
        audit("operator_login_failed", operator_id=body.id)
        raise HTTPException(401, str(e))
    audit("operator_login", operator_id=sess["id"], role=sess["role"])
    return {"token": tok, "operator": {k: sess[k] for k in ("id", "name", "role")}}


@app.post("/operators/logout")
async def operators_logout(request: Request):
    tok = request.headers.get("x-operator-token")
    op = ops.resolve(tok)
    if op:
        audit("operator_logout", operator_id=op["id"])
    ops.logout(tok or "")
    return {"ok": True}


@app.get("/operators")
async def operators_list():
    return {"operators": ops.list_ops(), "roles": list(ops.ROLES)}


@app.post("/operators")
async def operators_create(body: OpCreateBody):
    try:
        op = ops.create(body.name, body.role, body.pin, created_by=(ops.current.get() or {}).get("id", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit("operator_create", operator_id=op["id"], new_role=op["role"])
    return op


@app.put("/operators/{oid}")
async def operators_update(oid: str, body: OpUpdateBody):
    me = ops.current.get() or {}
    if oid == me.get("id") and body.active is False:
        raise HTTPException(400, "cannot deactivate yourself")
    try:
        op = ops.update(oid, body.role, body.active, body.pin)
    except KeyError:
        raise HTTPException(404, "unknown operator")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit("operator_update", operator_id=oid, **{k: v for k, v in body.model_dump().items() if v is not None and k != "pin"}, pin_changed=bool(body.pin))
    return op


class LiveCodeBody(BaseModel):
    code: str = Field(min_length=6, max_length=64)


@app.put("/security/live_code")
async def set_live_code(body: LiveCodeBody):
    ops.set_live_code(body.code)
    audit("live_code_set")
    return {"live_code_set": True}


def require_live_ok(live_code: str | None, target: str) -> None:
    """Live access lock: allow_live in risk settings + live confirmation code"""
    if not load_risk(account="").get("allow_live"):
        audit("live_blocked", reason="allow_live=false", target=target)
        raise HTTPException(403, "Live trading is disabled. An admin must enable 'Allow live trading' in Risk settings first.")
    if not ops.live_code_set():
        raise HTTPException(403, "No live confirmation code is set. Set one in Risk settings → Security first.")
    if not ops.check_live_code(live_code or ""):
        audit("live_blocked", reason="bad_live_code", target=target)
        raise HTTPException(403, "Live confirmation code is incorrect.")


# ---------- setup / login (Interactive Brokers only) ----------
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    mode: Literal["paper", "live"] = "paper"
    live_code: str | None = None


class AccountBody(BaseModel):
    account_id: str = Field(min_length=1, max_length=32)


class ConnectBody(BaseModel):
    gateway_id: str
    live_code: str | None = None


def _mask(u: str) -> str:
    return u if len(u) <= 3 else u[:2] + "•" * (len(u) - 3) + u[-1]


_acct_list_cache: dict = {"ts": 0.0, "rows": []}
_gw_run_cache: dict = {"ts": 0.0, "rows": {}}


async def _gateway_rows() -> list[dict]:
    import time as _t
    rows = []
    gws = gw.list_gateways()
    if _t.time() - _gw_run_cache["ts"] > 10:
        _gw_run_cache["rows"] = {g["id"]: await asyncio.to_thread(gw.running, g["id"]) for g in gws}
        _gw_run_cache["ts"] = _t.time()
    act = active_gateway()
    for g in gws:
        rows.append({"id": g["id"], "name": g["name"], "username_masked": _mask(g["username"]), "mode": g["mode"], "api_port": g["api_port"],
                     "vnc_port": g["vnc_port"], "running": _gw_run_cache["rows"].get(g["id"], False), "active": bool(act and act["id"] == g["id"]),
                     "last_login": g.get("last_login", "")})
    return rows


@app.get("/setup/status")
async def setup_status():
    sess = load_session()
    g = active_gateway()
    accts = ib.managedAccounts() if ib.isConnected() else []
    summaries = _acct_list_cache["rows"]
    import time as _t
    if ib.isConnected() and accts and (_t.time() - _acct_list_cache["ts"] > 30 or [r["account_id"] for r in summaries] != accts):
        summaries = []
        for a in accts:
            row = {"account_id": a, "net_liquidation": 0.0, "currency": "USD", "kind": "paper" if a.startswith("D") else "live"}
            try:
                for v in await acct_summary(a):
                    if v.tag == "NetLiquidation":
                        row["net_liquidation"] = _f(v.value); row["currency"] = v.currency or "USD"
            except Exception:
                pass
            summaries.append(row)
        _acct_list_cache.update(ts=_t.time(), rows=summaries)
    return {
        "logged_in": bool(sess.get("logged_in")) and bool(sess.get("account_id")) and _setup["state"] != "starting",
        "ib_connected": ib.isConnected(),
        "gateway_running": True if _setup["state"] == "starting" else ib.isConnected(),
        "configured": g is not None,
        "username_masked": _mask(g["username"]) if g else "",
        "mode": g["mode"] if g else "paper",
        "gateway_id": g["id"] if g else "",
        "gateways": await _gateway_rows(),
        "accounts": summaries,
        "selected_account": sess.get("account_id", ""),
        "setup": _setup,
        "broker": "Interactive Brokers",
        "live_allowed": bool(load_risk(account="").get("allow_live")),
        "live_code_set": ops.live_code_set(),
    }


async def _start_gateway_task(gid: str):
    """Start the profile's container → wait for the API to be ready (up to ~3 minutes)"""
    _setup.update(state="starting", message="Starting IB Gateway…", started=datetime.now(timezone.utc).isoformat(), gateway_id=gid)
    try:
        code, out = await asyncio.to_thread(gw.start, gid)
        if code != 0:
            _setup.update(state="error", message=out[-400:] or "docker compose failed")
            return
        _gw_run_cache["ts"] = 0.0
        _setup["message"] = "Gateway container up — logging in to IBKR (this takes 30–90 s)…"
        for i in range(36):
            await asyncio.sleep(5)
            try:
                await connect_gateway(gid)
                if ib.managedAccounts():
                    g = gw.get(gid) or {}
                    _setup.update(state="ready", message=f"Connected · {len(ib.managedAccounts())} account(s)")
                    audit("login", username=_mask(g.get("username", "")), mode=g.get("mode", ""), gateway_id=gid)
                    return
            except Exception as e:
                _setup["message"] = f"Waiting for gateway login… ({(i + 1) * 5}s) {str(e)[:80]}"
        _setup.update(state="error", message="Gateway did not come up in time. Check credentials / 2FA on your IBKR mobile app, then retry.")
    except Exception as e:
        _setup.update(state="error", message=str(e)[:300])


@app.post("/setup/login")
async def setup_login(body: LoginBody):
    if _setup["state"] == "starting":
        raise HTTPException(409, "a gateway is already starting")
    if body.mode == "live":
        require_live_ok(body.live_code, target=f"login:{_mask(body.username)}")
    g = gw.upsert(body.username, body.password, body.mode)
    sess = load_session()
    sess.update(logged_in=False, account_id="", username=_mask(body.username), mode=body.mode, gateway_id=g["id"])
    save_session(sess)
    asyncio.create_task(_start_gateway_task(g["id"]))
    return {"ok": True, "gateway_id": g["id"], "setup": _setup}


@app.post("/setup/connect")
async def setup_connect(body: ConnectBody):
    """Switch to an already running gateway (no password needed)"""
    g = gw.get(body.gateway_id)
    if not g:
        raise HTTPException(404, "unknown gateway")
    if g["mode"] == "live":
        require_live_ok(body.live_code, target=f"connect:{g['id']}")
    if not await asyncio.to_thread(gw.running, g["id"]):
        if not gw.read_env(Path(g["dir"]) / ".env").get("TWS_PASSWORD"):
            raise HTTPException(409, "gateway is stopped and its password was cleared — sign in again with your password")
        asyncio.create_task(_start_gateway_task(g["id"]))
        return {"ok": True, "starting": True, "setup": _setup}
    try:
        await connect_gateway(g["id"])
    except Exception as e:
        raise HTTPException(503, f"could not connect to gateway {g['id']}: {e}")
    audit("gateway_switch", gateway_id=g["id"], mode=g["mode"])
    return {"ok": True, "starting": False, "accounts": ib.managedAccounts()}


@app.post("/setup/gateways/{gid}/stop")
async def setup_gateway_stop(gid: str, forget_password: bool = True):
    act = active_gateway()
    if act and act["id"] == gid and ib.isConnected():
        try:
            await asyncio.to_thread(nm.stop_all, "gateway stopped")
        except Exception:
            pass
        ib.disconnect()
    code, out = await asyncio.to_thread(gw.stop, gid, forget_password)
    _gw_run_cache["ts"] = 0.0
    audit("gateway_stop", gateway_id=gid)
    if code != 0:
        raise HTTPException(500, out[-300:])
    return {"ok": True}


@app.delete("/setup/gateways/{gid}")
async def setup_gateway_remove(gid: str):
    await asyncio.to_thread(gw.remove, gid)
    _gw_run_cache["ts"] = 0.0
    audit("gateway_remove", gateway_id=gid)
    return {"ok": True}


@app.post("/setup/account")
async def setup_account(body: AccountBody):
    await ensure_connected()
    accts = ib.managedAccounts()
    if body.account_id not in accts:
        raise HTTPException(400, f"{body.account_id} is not one of your IBKR accounts: {accts}")
    g = active_gateway() or {}
    if g.get("mode") == "live" or not body.account_id.startswith("D"):
        if not load_risk(account="").get("allow_live"):
            raise HTTPException(403, "Live trading is disabled in Risk settings.")
    sess = load_session()
    sess.update(logged_in=True, account_id=body.account_id)
    save_session(sess)
    os.environ["IB_ACCOUNT_ID"] = body.account_id
    _acct_cache.update(ts=0.0, values={})
    audit("account_selected", account_id=body.account_id)
    asyncio.create_task(_safe_probe())
    return {"ok": True, "account_id": body.account_id}


@app.post("/setup/logout")
async def setup_logout(stop_gateway: bool = True):
    sess = load_session()
    audit("logout", account_id=sess.get("account_id", ""))
    save_session({"logged_in": False, "account_id": "", "username": sess.get("username", ""), "mode": sess.get("mode", ""), "gateway_id": sess.get("gateway_id", "")})
    os.environ.pop("IB_ACCOUNT_ID", None)
    if stop_gateway:
        try:
            await asyncio.to_thread(nm.stop_all, "logout")
        except Exception:
            pass
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass
        if sess.get("gateway_id"):
            await asyncio.to_thread(gw.stop, sess["gateway_id"], True)
            _gw_run_cache["ts"] = 0.0
        _setup.update(state="idle", message="Signed out")
    return {"ok": True}
