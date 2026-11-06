"""
Attribution / reconciliation (step 11)

source of a fill / order / trade / position:
  manual · agent:<id> · node:<node_id> (Nautilus node) · nautilus (Nautilus orders not yet matched to a node)

Nautilus sends orderRef = "<client_order_id>:<IB order id>", e.g. O-20261027-093000-001-000-1:1234
→ match client_order_id to a node from Redis events (node_orders table in history.db)
and node fill events carry trade_id = IB execId → recorded in history even though the API cannot see other clientIds' executions

reconcile: per instrument  position at IBKR  ==  Σ node positions (live heartbeat)  +  manual/agent share from fill history
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

NAUTILUS_REF = re.compile(r"^O-\d{8}-\d{6}-")
HEARTBEAT_STALE_S = 30.0
ALERT_STATUSES = ("mismatch", "orphan")


def is_nautilus_ref(ref: str | None) -> bool:
    return bool(ref and NAUTILUS_REF.match(ref))


def client_order_id(ref: str | None) -> str:
    """'O-…-1:1234' → 'O-…-1' · any other orderRef → ''"""
    if not is_nautilus_ref(ref):
        return ""
    head, sep, tail = ref.rpartition(":")
    return head if sep and tail.isdigit() else ref


def classify(ref: str | None, node: str = "") -> str:
    if node:
        return f"node:{node}"
    ref = (ref or "").strip()
    if not ref or ref == "manual":
        return "manual"
    if is_nautilus_ref(ref):
        return "nautilus"
    return ref


def book_of(source: str | None) -> str:
    """Each node's position in its own book · manual / agent / nautilus with unknown node use the shared book ''"""
    return source if source and source.startswith("node:") else ""


def nautilus_to_key(instrument: str | None) -> str:
    sym, _, venue = (instrument or "").rpartition(".")
    if not sym:
        return ""
    if venue == "IDEALPRO":
        return "FX:" + sym.replace("/", "")
    if venue == "PAXOS":
        return "CRYPTO:" + sym.split("/")[0]
    return "STK:" + sym


def tol(q: float) -> float:
    return 1e-6 + 1e-9 * abs(q)


def _fmt(q: float) -> str:
    return f"{q:+,.0f}" if abs(q) >= 1 else f"{q:+g}"


def _fresh(lv: dict | None) -> bool:
    age = (lv or {}).get("heartbeat_age_s")
    return age is not None and age <= HEARTBEAT_STALE_S


def node_positions(live: dict[str, dict]) -> dict[str, dict[str, dict]]:
    """{key: {node: {qty (signed), strategy}}} from each node's latest heartbeat"""
    out: dict[str, dict[str, dict]] = {}
    for node, lv in live.items():
        for p in (lv or {}).get("positions") or []:
            k = nautilus_to_key(p.get("instrument"))
            q = float(p.get("qty") or 0) * (-1 if str(p.get("side", "")).upper() == "SHORT" else 1)
            if not k or abs(q) <= tol(q):
                continue
            d = out.setdefault(k, {}).setdefault(node, {"qty": 0.0, "strategy": p.get("strategy", "")})
            d["qty"] += q
    return out


def reconcile(ib_qty: dict[str, float], live: dict[str, dict], books: dict[str, dict[str, float]],
              desired: set[str] | None = None) -> dict:
    """
    ib_qty  {key: qty at IBKR}
    live    {node: bus.live(node)}  (heartbeat_age_s, positions)
    books   {key: {book: net qty from fill history}}  book '' = manual/agent, 'node:<id>' = node
    desired nodes that should be running (not live = stale, not running = orphan)

    status per instrument: ok · external (no node involved) · untracked (no node involved and history incomplete)
                           stale · orphan · mismatch
    """
    desired = desired or set()
    npos = node_positions(live)
    keys = set(ib_qty) | set(npos)
    for k, b in books.items():
        if any(abs(v) > tol(v) for v in b.values()):      # node books and the manual/agent book (drift while IBKR is flat)
            keys.add(k)
    rows = []
    for k in sorted(keys):
        ib = float(ib_qty.get(k, 0.0))
        b = books.get(k, {})
        live_k = npos.get(k, {})
        hist_nodes = {bk[5:]: q for bk, q in b.items() if bk.startswith("node:") and abs(q) > tol(q)}
        nodes, flags = [], set()
        for node in sorted(set(live_k) | set(hist_nodes)):
            hq = hist_nodes.get(node, 0.0)
            if _fresh(live.get(node)):
                q = live_k.get(node, {}).get("qty", 0.0)
                state = "live"
                if abs(q - hq) > tol(q):
                    flags.add("history")
            else:
                q = live_k.get(node, {}).get("qty", hq) if node in live_k else hq
                state = "stale" if node in desired else "stopped"
                flags.add("stale" if node in desired else "orphan")
            nodes.append({"node": node, "strategy": live_k.get(node, {}).get("strategy", ""), "qty": q,
                          "history_qty": hq, "state": state})
        other = float(b.get("", 0.0))
        node_sum = sum(n["qty"] for n in nodes)
        expected = node_sum + other
        diff = ib - expected
        # a node counts as involved if either its live position or its fill history holds something
        # (e.g. a restarted node reports flat while its history and IBKR still hold the position)
        involved = any(abs(n["qty"]) > tol(n["qty"]) or abs(n["history_qty"]) > tol(n["history_qty"]) for n in nodes)
        if not involved:
            status = "external" if abs(diff) <= tol(diff) else "untracked"
        elif abs(diff) > tol(diff) or "history" in flags:
            status = "mismatch"
        elif "orphan" in flags:
            status = "orphan"
        elif "stale" in flags:
            status = "stale"
        else:
            status = "ok"
        parts = [f"IBKR {_fmt(ib)}"] + [f"{n['node']} {_fmt(n['qty'])}" for n in nodes if abs(n["qty"]) > tol(n["qty"])]
        if abs(other) > tol(other):
            parts.append(f"manual/agent {_fmt(other)}")
        note = " · ".join(parts)
        if abs(diff) > tol(diff):
            note += f" · diff {_fmt(diff)}"
        if status == "orphan":
            note += " · node stopped but still holds a position"
        elif status == "stale":
            note += " · node heartbeat is stale"
        if "history" in flags:
            note += " · node live position and fill history disagree"
        drift = abs(ib) <= tol(ib) and abs(other) > tol(other) and not involved
        if drift:
            note += " · manual/agent history holds a position but IBKR is flat"
        rows.append({"key": k, "ib_qty": ib, "nodes": nodes, "other_qty": other, "node_qty": node_sum,
                     "expected": expected, "diff": diff, "status": status, "note": note, "drift": drift})
    issues = [r for r in rows if r["status"] in ALERT_STATUSES]
    warn = [r for r in rows if r["status"] == "stale" or r["drift"]]
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "mismatch" if issues else ("warn" if warn else "ok"),
            "checked": len(rows), "node_keys": sum(1 for r in rows if r["nodes"]),
            "issues": len(issues), "rows": rows}


def annotate(positions: list[dict], report: dict, open_sources: dict[str, str] | None = None) -> None:
    """Fill in source / sources / strategy / recon / recon_note on IBKR position rows"""
    open_sources = open_sources or {}
    by_key = {r["key"]: r for r in report.get("rows", [])}
    for p in positions:
        r = by_key.get(p.get("key", ""))
        srcs = []
        if r:
            for n in r["nodes"]:
                if abs(n["qty"]) > tol(n["qty"]):
                    srcs.append({"source": f"node:{n['node']}", "strategy": n["strategy"], "qty": n["qty"]})
            if abs(r["other_qty"]) > tol(r["other_qty"]):
                srcs.append({"source": open_sources.get(p["key"], "manual"), "strategy": "", "qty": r["other_qty"]})
            if abs(r["diff"]) > tol(r["diff"]):
                srcs.append({"source": "untracked", "strategy": "", "qty": r["diff"]})
        if not srcs:
            srcs = [{"source": open_sources.get(p.get("key", ""), "manual"), "strategy": "", "qty": p.get("position", 0.0)}]
        p["sources"] = srcs
        p["source"] = srcs[0]["source"] if len(srcs) == 1 else "mixed"
        p["strategy"] = srcs[0]["strategy"] if len(srcs) == 1 else ""
        p["recon"] = r["status"] if r else ""
        p["recon_note"] = r["note"] if r else ""
