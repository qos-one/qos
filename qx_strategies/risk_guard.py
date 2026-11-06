"""
QXRiskGuard — Actor in the Nautilus node that applies the API's risk limits and stops immediately on kill switch

- a separate thread reads Redis: key `qx:risk` every 2 seconds + subscribes to `qx:control` (kill arrives within ~1 second)
- a 1-second timer on the node's event loop (thread-safe): loads the latest values into STATE and sets the RiskEngine TradingState
    trading allowed                    → ACTIVE
    halt / daily loss limit hit / kill → REDUCING while positions remain (close only) → HALTED once flat
- kill: cancel open orders + close every position of every Strategy in the node immediately (latched until the node restarts)
- API down (values older than 60 seconds): keep using the last limits and report stale in the heartbeat
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import timedelta

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.enums import TradingState

from .risk import STATE

RISK_KEY = "qx:risk"
CONTROL_CHANNEL = "qx:control"


class RedisRiskSource:
    """Read qx:risk + commands from qx:control in a separate thread — the node's event loop never waits on Redis"""

    def __init__(self, url: str, poll_secs: float = 2.0):
        self.url, self.poll = url, poll_secs
        self._latest: dict | None = None
        self._cmds: deque[dict] = deque(maxlen=50)
        self._stop = False
        self.connected = False
        threading.Thread(target=self._run, name="qx-risk-source", daemon=True).start()

    def latest(self) -> dict | None:
        return self._latest

    def take_command(self) -> dict | None:
        return self._cmds.popleft() if self._cmds else None

    def _run(self) -> None:
        import redis
        while not self._stop:
            try:
                r = redis.Redis.from_url(self.url, socket_timeout=3, socket_connect_timeout=3)
                ps = r.pubsub(ignore_subscribe_messages=True)
                ps.subscribe(CONTROL_CHANNEL)
                last = 0.0
                while not self._stop:
                    if time.time() - last >= self.poll:
                        raw = r.get(RISK_KEY)
                        if raw:
                            self._latest = json.loads(raw)
                        last = time.time()
                        self.connected = True
                    m = ps.get_message(timeout=0.5)
                    if m and m.get("type") == "message":
                        try:
                            self._cmds.append(json.loads(m["data"]))
                        except Exception:
                            pass
            except Exception:
                self.connected = False
                time.sleep(2.0)

    def close(self) -> None:
        self._stop = True


class QXRiskGuardConfig(ActorConfig, frozen=True):
    node_id: str
    redis_url: str = "redis://127.0.0.1:6379/0"
    stale_secs: float = 60.0
    tick_secs: float = 1.0


class QXRiskGuard(Actor):
    def __init__(self, config: QXRiskGuardConfig, source=None, risk_engine=None, strategies=None) -> None:
        super().__init__(config)
        self.source = source if source is not None else RedisRiskSource(config.redis_url)
        self.risk_engine = risk_engine          # set by nautilus_runner after node.build()
        self.strategies = list(strategies or [])
        self._stale_reported = False
        self._last_reason = None

    def on_start(self) -> None:
        STATE.require_sync = True               # live: no values from the API yet = no new entries
        STATE.node_id = self.config.node_id
        self.clock.set_timer("qx_risk_guard", timedelta(seconds=self.config.tick_secs), callback=self._tick)
        self._tick()

    def on_stop(self) -> None:
        try:
            self.clock.cancel_timer("qx_risk_guard")
        except Exception:
            pass
        if hasattr(self.source, "close"):
            self.source.close()

    # ---------- core ----------
    def _emit(self, event: str, **data) -> None:
        self.msgbus.publish(topic=f"qx.risk.{self.config.node_id}", msg={"event": event, "ts_ns": self.clock.timestamp_ns(), **data})

    def _tick(self, _ev=None) -> None:
        while True:
            cmd = self.source.take_command()
            if not cmd:
                break
            target = cmd.get("node", "*")
            if cmd.get("cmd") == "kill" and target in ("*", self.config.node_id):
                self.kill(cmd.get("reason", "kill switch"))
            elif cmd.get("cmd") == "cancel_order" and target == self.config.node_id:
                self.cancel_order(cmd.get("client_order_id", ""), cmd.get("reason", "package limit"))
        payload = self.source.latest()
        if payload and payload is not STATE.limits:
            STATE.update(payload)
        age = STATE.age()
        if STATE.limits and age is not None and age > self.config.stale_secs and not self._stale_reported:
            self._stale_reported = True
            self._emit("stale", reason=f"no risk update from API for {age:.0f}s — using last limits")
        elif age is not None and age <= self.config.stale_secs:
            self._stale_reported = False
        reason = STATE.halt_reason()
        if reason != self._last_reason:
            if reason and reason != "waiting for risk limits from API":
                self._emit("halt", reason=reason)
            elif not reason and self._last_reason:
                self._emit("resume", reason="trading allowed")
            self._last_reason = reason
        if reason == "waiting for risk limits from API":
            self._set_state(TradingState.ACTIVE)          # can still close/manage positions, but check_entry blocks new entries
        elif reason:
            self._set_state(TradingState.REDUCING if self._has_exposure() else TradingState.HALTED)
        else:
            self._set_state(TradingState.ACTIVE)

    def _has_exposure(self) -> bool:
        return bool(self.cache.positions_open()) or bool(self.cache.orders_open())

    def _set_state(self, state) -> None:
        name = state.name
        if STATE.trading_state == name:
            return
        STATE.trading_state = name
        if self.risk_engine is not None:
            self.risk_engine.set_trading_state(state)
        self._emit("trading_state", state=name)

    def cancel_order(self, client_order_id: str, reason: str) -> bool:
        """The API (layer 2 of package limits) cancels an order over the limit — only effective for orders not yet filled"""
        from nautilus_trader.model.identifiers import ClientOrderId
        try:
            order = self.cache.order(ClientOrderId(client_order_id))
        except Exception:
            order = None
        if order is None or not order.is_open:
            self._emit("cancel_skipped", client_order_id=client_order_id, reason=reason)
            return False
        for s in self.strategies:
            if s.id == order.strategy_id:
                s.cancel_order(order)
                self.log.warning(f"order {client_order_id} cancelled by API: {reason}")
                self._emit("cancel_order", client_order_id=client_order_id, reason=reason)
                return True
        return False

    def kill(self, reason: str) -> None:
        if STATE.killed:
            return
        STATE.killed, STATE.kill_reason = True, reason
        self._set_state(TradingState.REDUCING)
        closed = 0
        for s in self.strategies:
            try:
                iid = s.config.instrument_id
                s.cancel_all_orders(iid)
                if not self.portfolio.is_flat(iid):
                    s.close_all_positions(iid)
                    closed += 1
            except Exception as e:
                self.log.error(f"kill: {s.id}: {e}")
        self.log.warning(f"KILL SWITCH: {reason} — orders cancelled, {closed} position(s) closing")
        self._emit("kill", reason=reason, positions_closing=closed)
