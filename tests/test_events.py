from collections import Counter

from qx_strategies.backtest import run_backtest
from qx_strategies.events import QXEventBridge, QXEventBridgeConfig, envelope
from test_qx_strategies import CFG, _sine_bars


class FakePub:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)

    def close(self):
        pass


def _run(cfg):
    pub = FakePub()
    bridge = QXEventBridge(QXEventBridgeConfig(node_id="test_node", heartbeat_secs=60), publisher=pub)
    res = run_backtest(cfg, bars=_sine_bars(), log_level="OFF", actors=[bridge])
    return res, pub.events


def test_bridge_emits_all_event_types():
    res, ev = _run({**CFG, "sl_pips": 10, "tp_pips": 20})
    c = Counter(e["type"] for e in ev)
    assert c["node"] >= 1 and c["signal"] == res["signals"]
    assert c["fill"] >= res["closed_positions"]                 # every position has at least an opening fill
    assert c["position"] >= 2 * res["closed_positions"]         # Opened + Closed
    assert c["order"] > c["fill"] and c["heartbeat"] >= 1
    assert all(e["v"] == 1 and e["node"] == "test_node" for e in ev)


def test_event_contents():
    _, ev = _run(CFG)
    fill = next(e for e in ev if e["type"] == "fill")
    assert fill["strategy"].startswith("QXEmaCross") and fill["instrument"] == "EUR/USD.IDEALPRO"
    assert fill["data"]["side"] in ("BUY", "SELL") and fill["data"]["qty"] == 20000 and fill["data"]["price"] > 1
    sig = next(e for e in ev if e["type"] == "signal")
    assert sig["data"]["name"] in ("cross_up", "cross_down") and sig["data"]["action"] in (1, -1)
    closed = next(e for e in ev if e["type"] == "position" and e["data"]["event"] == "Closed")
    assert "realized_pnl" in closed["data"]
    rej = [e for e in ev if e["type"] == "order" and e["data"].get("event") == "Rejected"]
    assert rej == []
    hb = next(e for e in ev if e["type"] == "heartbeat")
    assert "positions" in hb["data"] and "open_orders" in hb["data"]


def test_envelope_drops_empty():
    e = envelope("fill", "n1", {"a": 1, "b": None, "c": ""}, ts_ns=1_700_000_000_000_000_000)
    assert e["data"] == {"a": 1} and e["ts"].startswith("2023-11-14T22:13:20")
