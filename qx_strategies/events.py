"""
Event pipeline from the Nautilus node → Redis Stream `qx:events` (the API reads it and forwards to the Terminal in real time)

- QXEventBridge (Actor) listens on the node's message bus: order / fill / position / Strategy signals + a heartbeat every 5 seconds
- sent via RedisPublisher (separate thread + queue) → Nautilus handlers never block even if Redis is slow/down
  Redis down: up to 5,000 events are queued and sent when it returns (beyond that the oldest are dropped and counted as dropped)
- event format (v1): {"v":1,"type","node","strategy","instrument","ts","ts_ns","data":{...}}
  type: order | fill | position | signal | heartbeat | node
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig

STREAM = "qx:events"
SCHEMA_VERSION = 1


def iso_ns(ns: int | None) -> str:
    if not ns:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat(timespec="milliseconds")


def _num(x):
    if x is None or x == "":
        return None
    try:
        return float(str(x).split(" ")[0])          # Money, e.g. "12.34 USD" → 12.34
    except ValueError:
        return None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def envelope(etype: str, node: str, data: dict, strategy: str = "", instrument: str = "", ts_ns: int | None = None) -> dict:
    return {"v": SCHEMA_VERSION, "type": etype, "node": node, "strategy": strategy, "instrument": instrument,
            "ts": iso_ns(ts_ns), "ts_ns": ts_ns or time.time_ns(), "data": _clean(data)}


def order_event_data(ev, order=None) -> tuple[str, dict]:
    """OrderFilled → ("fill", …) · other OrderXxx → ("order", …) — fills in side/qty/price from the cached order if available"""
    name = type(ev).__name__
    try:
        d = type(ev).to_dict(ev)
    except Exception:
        d = {}
    base = {"client_order_id": d.get("client_order_id") or str(getattr(ev, "client_order_id", "")),
            "venue_order_id": d.get("venue_order_id")}
    if order is not None:
        base.update(side=str(order.side_string()) if hasattr(order, "side_string") else None,
                    qty=_num(order.quantity), order_type=order.type_string() if hasattr(order, "type_string") else None,
                    price=_num(getattr(order, "price", None)) if getattr(order, "has_price", False) else None,
                    trigger_price=_num(getattr(order, "trigger_price", None)) if getattr(order, "has_trigger_price", False) else None,
                    status=order.status_string() if hasattr(order, "status_string") else None,
                    reduce_only=bool(getattr(order, "is_reduce_only", False)) or None)
    if name == "OrderFilled":
        return "fill", {**base, "side": d.get("order_side") or base.get("side"), "qty": _num(d.get("last_qty")),
                        "price": _num(d.get("last_px")), "commission": _num(d.get("commission")),
                        "currency": (str(d.get("commission", "")).split(" ") + [""])[1] or None,
                        "liquidity": d.get("liquidity_side"), "trade_id": d.get("trade_id"), "position_id": d.get("position_id"),
                        "order_type": d.get("order_type") or base.get("order_type")}
    return "order", {**base, "event": name.removeprefix("Order"), "side": d.get("order_side") or d.get("side") or base.get("side"),
                     "qty": _num(d.get("quantity")) or base.get("qty"), "reason": d.get("reason")}


def position_event_data(ev) -> dict:
    try:
        d = type(ev).to_dict(ev)
    except Exception:
        d = {}
    return {"event": type(ev).__name__.removeprefix("Position"), "position_id": d.get("position_id"),
            "side": d.get("side"), "entry": d.get("entry"), "qty": _num(d.get("quantity")), "peak_qty": _num(d.get("peak_qty")),
            "avg_px_open": _num(d.get("avg_px_open")), "avg_px_close": _num(d.get("avg_px_close")),
            "realized_pnl": _num(d.get("realized_pnl")), "unrealized_pnl": _num(d.get("unrealized_pnl")),
            "last_px": _num(d.get("last_px")), "duration_s": (int(d["duration_ns"]) // 1_000_000_000) if d.get("duration_ns") else None}


def _risk_summary() -> dict:
    from .risk import STATE
    return {"trading_state": STATE.trading_state, "synced": STATE.limits is not None, "age_s": STATE.age(),
            "halt_reason": STATE.halt_reason(), "killed": STATE.killed, "blocked": dict(STATE.blocked)}


class RedisPublisher:
    """Publish events to a Redis Stream from a separate thread (XADD + MAXLEN ≈ 20,000)"""

    def __init__(self, url: str, stream: str = STREAM, maxlen: int = 20_000, buffer: int = 5_000):
        self.url, self.stream, self.maxlen = url, stream, maxlen
        self.q: deque[str] = deque(maxlen=buffer)
        self.cv = threading.Condition()
        self.sent = self.dropped = 0
        self.connected = False
        self.last_error = ""
        self._stop = False
        self._t = threading.Thread(target=self._run, name="qx-redis-publisher", daemon=True)
        self._t.start()

    def publish(self, event: dict) -> None:
        with self.cv:
            if len(self.q) == self.q.maxlen:
                self.dropped += 1
            self.q.append(json.dumps(event, default=str))
            self.cv.notify()

    def _run(self) -> None:
        import redis
        r, backoff = None, 1.0
        while True:
            with self.cv:
                while not self.q and not self._stop:
                    self.cv.wait(1.0)
                if self._stop and not self.q:
                    return
                batch = [self.q.popleft() for _ in range(min(200, len(self.q)))]
            try:
                if r is None:
                    r = redis.Redis.from_url(self.url, socket_timeout=3, socket_connect_timeout=3)
                pipe = r.pipeline(transaction=False)
                for e in batch:
                    pipe.xadd(self.stream, {"e": e}, maxlen=self.maxlen, approximate=True)
                pipe.execute()
                self.sent += len(batch)
                if not self.connected:
                    print(f"qx-events: connected to Redis {self.url} → stream {self.stream} (sent {self.sent})", flush=True)
                self.connected, backoff, self.last_error = True, 1.0, ""
            except Exception as ex:
                with self.cv:
                    self.q.extendleft(reversed(batch))           # put back in the queue (original order)
                if self.connected or not self.last_error:
                    print(f"qx-events: Redis error ({self.url}): {ex!r} — retrying, {len(self.q)} queued", flush=True)
                self.connected, self.last_error, r = False, str(ex)[:200], None
                if self._stop:
                    return
                time.sleep(backoff)
                backoff = min(10.0, backoff * 2)

    def close(self, timeout: float = 3.0) -> None:
        with self.cv:
            self._stop = True
            self.cv.notify()
        self._t.join(timeout)


class QXEventBridgeConfig(ActorConfig, frozen=True):
    node_id: str
    redis_url: str = "redis://127.0.0.1:6379/0"
    stream: str = STREAM
    heartbeat_secs: float = 5.0


class QXEventBridge(Actor):
    def __init__(self, config: QXEventBridgeConfig, publisher=None) -> None:
        super().__init__(config)
        self.node = config.node_id
        self.pub = publisher or RedisPublisher(config.redis_url, config.stream)
        self.counts: Counter = Counter()
        self._t0 = time.time()

    # ---------- lifecycle ----------
    def on_start(self) -> None:
        self.msgbus.subscribe(topic="events.order.*", handler=self._on_order)
        self.msgbus.subscribe(topic="events.position.*", handler=self._on_position)
        self.msgbus.subscribe(topic="qx.signal.*", handler=self._on_signal)
        self.msgbus.subscribe(topic="qx.risk.*", handler=self._on_risk)
        self.clock.set_timer("qx_heartbeat", timedelta(seconds=self.config.heartbeat_secs), callback=self._on_heartbeat)
        self._emit("node", {"event": "started"})

    def on_stop(self) -> None:
        try:                                              # cancel first: a heartbeat after 'stopping' would make the node look alive again
            self.clock.cancel_timer("qx_heartbeat")
        except Exception:
            pass
        self._emit("node", {"event": "stopping", "counts": dict(self.counts)})
        if hasattr(self.pub, "close"):
            self.pub.close()

    # ---------- handlers ----------
    def _emit(self, etype: str, data: dict, strategy: str = "", instrument: str = "", ts_ns: int | None = None) -> None:
        self.counts[etype] += 1
        ts_ns = ts_ns or self.clock.timestamp_ns()          # the node's clock (live = real time, backtest = simulated time)
        self.pub.publish(envelope(etype, self.node, data, strategy, instrument, ts_ns))

    def _on_order(self, ev) -> None:
        order = None
        try:
            order = self.cache.order(ev.client_order_id)
        except Exception:
            pass
        etype, data = order_event_data(ev, order)
        self._emit(etype, data, str(ev.strategy_id), str(ev.instrument_id), ev.ts_event)

    def _on_position(self, ev) -> None:
        self._emit("position", position_event_data(ev), str(ev.strategy_id), str(ev.instrument_id), ev.ts_event)

    def _on_signal(self, msg) -> None:
        if isinstance(msg, dict):
            self._emit("signal", {k: v for k, v in msg.items() if k not in ("strategy", "instrument", "ts_ns")},
                       msg.get("strategy", ""), msg.get("instrument", ""), msg.get("ts_ns"))

    def _on_risk(self, msg) -> None:
        if isinstance(msg, dict):
            self._emit("risk", {k: v for k, v in msg.items() if k not in ("instrument", "ts_ns")}, "", msg.get("instrument", ""), msg.get("ts_ns"))

    def _on_heartbeat(self, _event=None) -> None:
        positions = []
        for p in self.cache.positions_open():
            upnl = None
            try:
                m = self.portfolio.unrealized_pnl(p.instrument_id)
                upnl = float(m.as_double()) if m is not None else None
            except Exception:
                pass
            positions.append(_clean({"instrument": str(p.instrument_id), "strategy": str(p.strategy_id), "side": p.side_string() if hasattr(p, "side_string") else str(p.side),
                                     "qty": float(p.quantity), "avg_px": float(p.avg_px_open), "unrealized_pnl": upnl}))
        realized = 0.0
        for p in self.cache.positions():
            try:
                realized += float(p.realized_pnl.as_double()) if p.realized_pnl is not None else 0.0
            except Exception:
                pass
        self._emit("heartbeat", {"uptime_s": int(time.time() - self._t0), "positions": positions,
                                 "open_orders": len(self.cache.orders_open()), "realized_pnl": round(realized, 2),
                                 "counts": dict(self.counts), "redis_dropped": getattr(self.pub, "dropped", 0),
                                 "risk": _risk_summary()})
