"""
Layer 2 of package limits (step 22) — the API checks every node's order/fill events against the same limits the node got at start

- order → node: from Redis events (client_order_id ↔ orderRef at IBKR = "<client_order_id>:<IB order id>")
- node → package: package_limits that node_manager passes to the node at spawn (nodes started before step 22 have none → skipped)
- Checks: declared instruments · entry orders per hour · notional after the order (USD, crosses via rates) · flips per hour
  (every non-reduce-only order type: MARKET, LIMIT, STOP — a package may submit orders directly without _enter)
  (1 order / 5% tolerance because the API's clock and prices differ slightly from the node's — layer 1 in the node is the main enforcement)
- Exceeded → send cancel_order to the node (via qx:control) + event + audit · together with the node's own blocks, 3 times in 10 minutes → stop the node

Limitation: nodes connect to IBKR directly with their own clientId, so the API cannot cancel orders at IBKR itself (the node must cancel)
and market orders usually fill before the cancel arrives → the real pre-broker decision is in layer 1 · a credential proxy is needed later
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable

from qx_strategies import fx
from qx_strategies.package_limits import TRIP_COUNT, TRIP_WINDOW_NS, usd_notional

HOUR_NS = 3600 * 1_000_000_000


class PackageGuard:
    def __init__(self, limits_of: Callable[[str], dict | None], rates_of: Callable[[], dict] | None = None):
        self.limits_of = limits_of
        self.rates_of = rates_of            # USD rates from the API (optional) · fallback: rates from prices seen on XXX/USD · USD/XXX instruments
        self.orders: dict[str, deque] = defaultdict(deque)
        self.flips: dict[str, deque] = defaultdict(deque)
        self.breaches: dict[str, deque] = defaultdict(deque)
        self.net: dict[tuple[str, str], float] = {}
        self.px: dict[str, float] = {}
        self.seen: set[str] = set()
        self.stopped: set[str] = set()
        self.log: deque = deque(maxlen=200)

    def reset_node(self, node: str) -> None:
        for d in (self.orders, self.flips, self.breaches):
            d.pop(node, None)
        for k in [k for k in self.net if k[0] == node]:
            self.net.pop(k, None)
        self.stopped.discard(node)

    @staticmethod
    def _trim(q: deque, now: int, window: int) -> None:
        while q and now - q[0] >= window:
            q.popleft()

    def on_event(self, ev: dict) -> list[dict]:
        node, t, d = ev.get("node", ""), ev.get("type"), ev.get("data") or {}
        inst = ev.get("instrument", "")
        ts = int(ev.get("ts_ns") or time.time_ns())
        if t == "node" and d.get("event") == "started":
            self.reset_node(node)
            return []
        L = self.limits_of(node)
        if not L:
            return []
        out: list[dict] = []
        if t == "signal" and d.get("close"):
            self.px[inst] = float(d["close"])
        elif t == "risk" and d.get("event") == "package_limit":
            out.append(self._breach(node, d.get("limit", ""), d.get("reason", ""), ts, source="node"))
        elif t == "fill":
            q = float(d.get("qty") or 0) * (1 if str(d.get("side", "")).upper() in ("BUY", "BOT") else -1)
            if d.get("price"):
                self.px[inst] = float(d["price"])
            self.net[(node, inst)] = self.net.get((node, inst), 0.0) + q
        elif t == "order" and d.get("event") == "Submitted":
            coid = d.get("client_order_id") or ""
            if coid in self.seen:
                return []
            self.seen.add(coid)
            if len(self.seen) > 50_000:
                self.seen.clear()
            allowed = L.get("instruments") or []
            if allowed and inst not in allowed:
                out.append(self._breach(node, "instruments", f"order on {inst} — package declares {', '.join(allowed)}", ts, coid))
                return out
            entry = not d.get("reduce_only")         # every order type: a package can submit LIMIT / STOP entries directly (bypassing _enter)
            if not entry:
                return out
            side = 1 if str(d.get("side", "")).upper() in ("BUY", "BOT") else -1
            qty = float(d.get("qty") or 0)
            net = self.net.get((node, inst), 0.0)
            flip = net != 0 and (net > 0) != (side > 0)
            oq = self.orders[node]
            self._trim(oq, ts, HOUR_NS)
            oq.append(ts)
            cap = int(L.get("max_orders_per_hour") or 0)
            if cap and len(oq) > cap + 1:
                out.append(self._breach(node, "max_orders_per_hour", f"{len(oq)} entry orders in the last hour > {cap}", ts, coid))
            if flip:
                fq = self.flips[node]
                self._trim(fq, ts, HOUR_NS)
                fq.append(ts)
                cap = L.get("max_flips_per_hour")
                if cap is not None and len(fq) > int(cap) + 1:
                    out.append(self._breach(node, "max_flips_per_hour", f"{len(fq)} side flips in the last hour > {cap}", ts, coid))
            px = float(d.get("price") or d.get("trigger_price") or self.px.get(inst) or 0)
            cap = float(L.get("max_notional_usd") or 0)
            if px and cap:
                notional = usd_notional(inst, (0.0 if flip else abs(net)) + qty, px, self._rates())
                if notional is None:                 # cannot price (cross without a USD rate) — layer 1 in the node blocks; log it here
                    self.log.append({"node": node, "limit": "rate", "reason": f"cannot value {inst} in USD", "client_order_id": coid, "ts_ns": ts})
                elif notional > cap * 1.05:
                    out.append(self._breach(node, "max_notional_usd", f"position would be {notional:,.0f} USD > {cap:,.0f}", ts, coid))
        return out

    def _rates(self) -> dict:
        rates = fx.rates_from_prices({k.split(".")[0]: v for k, v in self.px.items()})
        if self.rates_of is not None:
            try:
                rates.update({k: v for k, v in (self.rates_of() or {}).items() if v})
            except Exception:
                pass
        return rates

    def _breach(self, node: str, kind: str, reason: str, ts: int, coid: str | None = None, source: str = "api") -> dict:
        q = self.breaches[node]
        while q and ts - q[0] > TRIP_WINDOW_NS:
            q.popleft()
        q.append(ts)
        stop = len(q) >= TRIP_COUNT and node not in self.stopped
        if stop:
            self.stopped.add(node)
        b = {"node": node, "limit": kind, "reason": reason, "client_order_id": coid, "source": source, "count_10m": len(q), "stop": stop}
        self.log.append({**b, "ts_ns": ts})
        return b
