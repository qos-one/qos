"""
API-side event bus: read the Redis Stream `qx:events` sent by Nautilus nodes → keep the latest in memory + forward (WebSocket / alerts)

- Startup: load the last 500 events (replay — no repeat alerts), then XREAD BLOCK from the latest id
- Redis disconnected: reconnect every 2–10 seconds and resume from the same id (no events in the stream are lost)
- Live state per node: latest heartbeat, position, latest signal, event counts by type
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, deque
from typing import Awaitable, Callable

STREAM = "qx:events"


class EventBus:
    def __init__(self, redis_url: str, on_event: Callable[[dict, bool], Awaitable[None]] | None = None,
                 stream: str = STREAM, keep: int = 2000, backlog: int = 500):
        self.url, self.stream, self.backlog = redis_url, stream, backlog
        self.on_event = on_event
        self.events: deque[dict] = deque(maxlen=keep)
        self.nodes: dict[str, dict] = {}
        self.connected = False
        self.last_id: str | None = None
        self.last_error = ""
        self.received = 0
        self._task: asyncio.Task | None = None

    # ---------- state ----------
    def ingest(self, ev: dict, replay: bool = False) -> None:
        node = ev.get("node") or "?"
        n = self.nodes.setdefault(node, {"counts": Counter(), "last_heartbeat": None, "positions": [], "last_signal": None,
                                         "last_fill": None, "realized_pnl": None, "open_orders": None, "last_event": None})
        t = ev.get("type", "")
        n["counts"][t] += 1
        n["last_event"] = ev.get("ts")
        d = ev.get("data") or {}
        if t == "heartbeat":
            n["last_heartbeat"] = ev.get("ts_ns", 0) / 1e9 if replay else time.time()
            n["positions"] = d.get("positions", [])
            n["realized_pnl"] = d.get("realized_pnl")
            n["open_orders"] = d.get("open_orders")
            n["risk"] = d.get("risk")
        elif t == "signal":
            n["last_signal"] = {"ts": ev.get("ts"), "instrument": ev.get("instrument"), **d}
        elif t == "fill":
            n["last_fill"] = {"ts": ev.get("ts"), "instrument": ev.get("instrument"), **d}
        elif t == "node" and d.get("event") == "stopping":
            n["positions"] = []
            n["last_heartbeat"] = None
        self.events.append(ev)
        self.received += 1

    def heartbeat_age(self, node: str) -> float | None:
        hb = (self.nodes.get(node) or {}).get("last_heartbeat")
        return round(time.time() - hb, 1) if hb else None

    def live(self, node: str) -> dict:
        n = self.nodes.get(node)
        if not n:
            return {}
        return {"heartbeat_age_s": self.heartbeat_age(node), "positions": n["positions"], "last_signal": n["last_signal"],
                "last_fill": n["last_fill"], "realized_pnl": n["realized_pnl"], "open_orders": n["open_orders"],
                "counts": dict(n["counts"]), "risk": n.get("risk")}

    def recent(self, node: str = "", types: str = "", limit: int = 200) -> list[dict]:
        want = {t for t in types.split(",") if t} if types else None
        out = []
        for ev in reversed(self.events):
            if node and ev.get("node") != node:
                continue
            if want and ev.get("type") not in want:
                continue
            if not want and ev.get("type") == "heartbeat":
                continue                                   # heartbeats are not shown in the normal list
            out.append(ev)
            if len(out) >= limit:
                break
        return out

    def status(self) -> dict:
        return {"connected": self.connected, "stream": self.stream, "last_id": self.last_id, "received": self.received,
                "buffered": len(self.events), "last_error": self.last_error,
                "nodes": {k: {"heartbeat_age_s": self.heartbeat_age(k), "counts": dict(v["counts"])} for k, v in self.nodes.items()}}

    # ---------- reader ----------
    async def _handle(self, entries, replay: bool) -> None:
        for eid, fields in entries:
            self.last_id = eid.decode() if isinstance(eid, bytes) else eid
            raw = fields.get(b"e") or fields.get("e")
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            ev["_id"] = self.last_id                         # Redis stream id: time the node sent it + prevents duplicate saves
            self.ingest(ev, replay)
            if self.on_event:
                try:
                    await self.on_event(ev, replay)
                except Exception as e:
                    print("event handler:", e)

    async def run(self) -> None:
        import redis.asyncio as aioredis
        backoff = 2.0
        while True:
            r = None
            try:
                r = aioredis.from_url(self.url, socket_connect_timeout=3, socket_timeout=10)
                await r.ping()
                self.connected, self.last_error, backoff = True, "", 2.0
                if self.last_id is None:
                    hist = await r.xrevrange(self.stream, count=self.backlog)
                    await self._handle(list(reversed(hist)), replay=True)
                    # empty stream: read from the very beginning, not '$' — '$' would skip events XADDed between this call and
                    # the first XREAD (and, after a Redis outage, everything sent during it). Every entry is new anyway.
                    self.last_id = self.last_id or "0-0"
                while True:
                    res = await r.xread({self.stream: self.last_id}, block=5000, count=200)
                    for _stream, entries in res or []:
                        await self._handle(entries, replay=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected, self.last_error = False, str(e)[:200]
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 2)
            finally:
                if r is not None:
                    try:
                        await r.aclose()
                    except Exception:
                        pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
