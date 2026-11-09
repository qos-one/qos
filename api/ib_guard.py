"""IBKR guards: cancel orders that another client placed, and the gateway-down alert

IBKR ties an order to the clientId that sent it. The API (client 60) can only cancel its own orders: a cancel for a node's
order stays at PendingCancel or fails with 10147 "not found". So each order is cancelled by its owner:

- api     the API placed it → ib.cancelOrder
- node    a running node placed it → ask that node over Redis (cancel_order by client_order_id)
- client  the owner is not connected (node stopped, old script) → connect briefly with the owner's clientId and cancel
"""
from __future__ import annotations

import asyncio
from typing import Callable, Iterable


def plan(trades: Iterable, api_client_id: int, node_of: Callable[[object], str], running: Callable[[str], bool],
         client_order_id: Callable[[str], str]) -> list[dict]:
    """Decide who cancels each order (pure: no IBKR calls)"""
    out = []
    for tr in trades:
        o = tr.order
        cid = int(getattr(o, "clientId", 0) or 0)
        node = node_of(tr) or ""
        coid = client_order_id(getattr(o, "orderRef", "") or "")
        if cid == api_client_id:
            method = "api"
        elif node and coid and running(node):
            method = "node"
        else:
            method = "client"
        out.append({"order_id": o.orderId, "perm_id": getattr(o, "permId", 0), "client_id": cid, "node": node,
                    "client_order_id": coid, "method": method})
    return out


def by_client(items: list[dict]) -> dict[int, list[int]]:
    """Orders to cancel through a temporary connection, grouped by owner clientId"""
    g: dict[int, list[int]] = {}
    for it in items:
        if it["method"] == "client":
            g.setdefault(it["client_id"], []).append(it["order_id"])
    return g


async def cancel_as_client(host: str, port: int, client_id: int, order_ids: list[int], wait: float = 2.5,
                           ib_factory=None, timeout: float = 10.0) -> dict:
    """Connect with the owner's clientId, cancel its orders, disconnect. Fails if that clientId is still connected"""
    if ib_factory is None:
        from ib_async import IB as ib_factory
    from ib_async import Order
    ib2 = ib_factory()
    try:
        await asyncio.wait_for(ib2.connectAsync(host, port, clientId=client_id, timeout=timeout), timeout + 5)
    except Exception as e:
        return {"client_id": client_id, "ok": False, "error": f"cannot connect as client {client_id}: {e or type(e).__name__}"}
    try:
        for oid in order_ids:
            o = Order()
            o.orderId, o.clientId = oid, client_id
            ib2.cancelOrder(o)
        await asyncio.sleep(wait)
        return {"client_id": client_id, "ok": True, "order_ids": list(order_ids)}
    finally:
        try:
            ib2.disconnect()
        except Exception:
            pass


def down_alert_due(down_since: float | None, now: float, alerted: bool, threshold: float = 900.0) -> bool:
    """IB Gateway has been unreachable longer than threshold seconds and no alert went out yet"""
    return down_since is not None and not alerted and now - down_since >= threshold
