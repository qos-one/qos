import asyncio
import json
import os

import pytest

from api.event_bus import EventBus
from qx_strategies.events import RedisPublisher, envelope


def _ev(t, node="n1", **data):
    return envelope(t, node, data, "QXEmaCross-001", "EUR/USD.IDEALPRO")


def test_ingest_live_and_recent():
    b = EventBus("redis://x")
    b.ingest(_ev("signal", name="cross_up", action=1))
    b.ingest(_ev("fill", side="BUY", qty=20000, price=1.1))
    b.ingest(_ev("heartbeat", positions=[{"instrument": "EUR/USD.IDEALPRO", "side": "LONG", "qty": 20000}], open_orders=2, realized_pnl=5.0))
    b.ingest(_ev("order", node="n2", event="Rejected", reason="x"))
    live = b.live("n1")
    assert live["positions"][0]["side"] == "LONG" and live["open_orders"] == 2 and live["last_signal"]["name"] == "cross_up"
    assert live["heartbeat_age_s"] is not None and live["heartbeat_age_s"] < 2
    assert [e["type"] for e in b.recent()] == ["order", "fill", "signal"]          # newest first, heartbeats excluded
    assert [e["type"] for e in b.recent(node="n1", types="fill")] == ["fill"]
    assert b.status()["nodes"]["n2"]["counts"] == {"order": 1}


def test_stopping_clears_positions():
    b = EventBus("redis://x")
    b.ingest(_ev("heartbeat", positions=[{"qty": 1}]))
    b.ingest(_ev("node", event="stopping"))
    assert b.live("n1")["positions"] == [] and b.heartbeat_age("n1") is None


def test_handle_calls_callback_with_replay_flag():
    got = []

    async def cb(ev, replay):
        got.append((ev["type"], replay))
    b = EventBus("redis://x", on_event=cb)
    entries = [(b"1-0", {b"e": json.dumps(_ev("fill"))}), (b"2-0", {b"e": b"not json"}), (b"3-0", {b"e": json.dumps(_ev("signal"))})]
    asyncio.run(b._handle(entries, replay=True))
    assert got == [("fill", True), ("signal", True)] and b.last_id == "3-0"


def _redis_ok(url):
    try:
        import redis
        redis.Redis.from_url(url, socket_connect_timeout=0.3).ping()
        return True
    except Exception:
        return False


URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/15")


@pytest.mark.skipif(not _redis_ok(URL), reason="Redis not running")
def test_end_to_end_through_redis():
    stream = "qx:test:events"
    import redis
    redis.Redis.from_url(URL).delete(stream)
    got = []

    async def cb(ev, replay):
        got.append(ev)

    async def main():
        b = EventBus(URL, on_event=cb, stream=stream)
        b.start()
        await asyncio.sleep(0.5)
        pub = RedisPublisher(URL, stream)
        for i in range(5):
            pub.publish(_ev("fill", qty=i))
        await asyncio.sleep(1.5)
        pub.close()
        b.stop()
    asyncio.run(main())
    assert [e["data"]["qty"] for e in got] == [0, 1, 2, 3, 4]
    redis.Redis.from_url(URL).delete(stream)


def test_publisher_buffers_when_redis_down():
    pub = RedisPublisher("redis://127.0.0.1:1/0", buffer=3)        # closed port → cannot send
    for i in range(5):
        pub.publish(_ev("fill", qty=i))
    import time
    time.sleep(0.5)
    assert pub.dropped >= 2 and len(pub.q) <= 3 and not pub.connected
    pub.close(0.5)
