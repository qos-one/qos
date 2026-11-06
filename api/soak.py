"""
Paper soak + shadow parity (step 12) — evidence that a node is ready before cutting over the old system (step 13) and before using a live account (step 14)

Collected from the API event bus into data/soak.db:
- events   signal / order / fill / position / risk / node of every node (id = Redis stream id → replay without duplicates)
- alive    minutes with a heartbeat received (uptime per day + used to drop periods when the node was not running from comparisons)
- incidents heartbeat missing > 30 seconds · node crash · reconcile mismatch/orphan
- parity   latest comparison result per node

Measurements:
- Live vs backtest: the node's live decisions vs the same Strategy (same config) on Data catalog bars for the same period
- Shadow old agent: the old agent (no orders sent) on the same bars, comparing entry signals only (the old agent has no SL/TP — supporting info, not a criterion)
- Execution: fill slippage vs the close of the signal bar (pips, + = unfavorable) and signal → fill latency
- Stability: broker rejects, restarts/crashes, heartbeat gaps, reconcile
→ GO / NO-GO / PENDING verdict per criteria (adjustable in data/soak.json → "criteria")
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

ROOT = Path(__file__).resolve().parent.parent
DB_FILE = paths.data_dir() / "soak.db"
STATE_FILE = paths.data_dir() / "soak.json"
GAP_S = 30.0
LIVE_BAR_TS_OFFSET_BARS = 1     # live (INTERNAL) signals carry the bar OPEN time, catalog bars the CLOSE time → shift live by one bar before matching
KEEP_TYPES = ("signal", "order", "fill", "position", "risk", "node")

CRITERIA = {
    "min_days": 5,                      # days the node ran at least min_alive_minutes
    "min_alive_minutes": 60,
    "min_decision_match": 0.98,         # live vs backtest
    "min_actions": 3,                   # fewer than this = not enough data (PENDING)
    "max_avg_slippage_pips": 1.0,
    "max_p95_latency_s": 5.0,
    "min_executions": 3,
    "max_rejects": 0,
    "max_crashes": 0,
    "max_heartbeat_gaps": 2,
    "max_reconcile_incidents": 0,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, emit_ns INTEGER, ts TEXT, ts_ns INTEGER, day TEXT, node TEXT, type TEXT,
  strategy TEXT, instrument TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS events_node ON events(node, type, ts_ns);
CREATE TABLE IF NOT EXISTS alive (node TEXT NOT NULL, minute INTEGER NOT NULL, PRIMARY KEY (node, minute));
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ts_ns INTEGER, day TEXT, node TEXT, kind TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS incidents_node ON incidents(node, ts_ns);
CREATE TABLE IF NOT EXISTS parity (node TEXT PRIMARY KEY, ts TEXT, result TEXT);
"""


def connect() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _day(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).strftime("%Y-%m-%d")


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat(timespec="seconds")


def _stream_ns(eid: str | None) -> int | None:
    try:
        return int(str(eid).split("-")[0]) * 1_000_000
    except (TypeError, ValueError):
        return None


# ---------- state (criteria + soak start per node) ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2, sort_keys=True))


def criteria() -> dict:
    return {**CRITERIA, **(load_state().get("criteria") or {})}


def reset(node: str, now_ns: int | None = None) -> str:
    """Restart the soak count (after fixing an issue) — old data is kept but not counted in the report"""
    st = load_state()
    st.setdefault("start", {})[node] = _iso(now_ns or time.time_ns())
    save_state(st)
    return st["start"][node]


def note_ref(node: str, ref: str | None, now_ns: int | None = None, started_ns: int | None = None) -> bool:
    """Record the strategy version a node runs — evidence never carries over to another version. Returns True when the
    soak count was restarted.
    started_ns: when the node's current process (the one running `ref`) started. Without a recorded version nothing proves
    which version produced older evidence, so only evidence from that process start on is kept (no started_ns → a fresh
    soak from now). A different version than the recorded one restarts the soak at started_ns (else now).
    Idempotent: calling it again for the same version changes nothing (safe from read-only endpoints)."""
    if not ref:
        return False
    st = load_state()
    refs = st.setdefault("refs", {})
    old = (refs.get(node) or {}).get("ref")
    if old == ref:
        return False
    now = now_ns or time.time_ns()
    keep_from = int(started_ns) if started_ns else now
    cur = start_ns(node)
    restart = cur is None or cur < keep_from or old is not None
    refs[node] = {"ref": ref, "since": _iso(keep_from)}
    if restart:
        st.setdefault("start", {})[node] = _iso(keep_from)
    save_state(st)
    return restart and cur is not None


def soak_ref(node: str) -> str | None:
    """Strategy version the node's current soak evidence belongs to (None = not recorded yet)"""
    return ((load_state().get("refs") or {}).get(node) or {}).get("ref")


def start_ns(node: str, con: sqlite3.Connection | None = None) -> int | None:
    s = (load_state().get("start") or {}).get(node)
    if s:
        return int(datetime.fromisoformat(s).timestamp() * 1e9)
    own = con is None
    con = con or connect()
    try:
        r = con.execute("SELECT MIN(minute) FROM alive WHERE node = ?", (node,)).fetchone()
        e = con.execute("SELECT MIN(ts_ns) FROM events WHERE node = ?", (node,)).fetchone()
    finally:
        if own:
            con.close()
    cands = [x for x in ((r[0] * 60_000_000_000) if r and r[0] is not None else None, e[0] if e else None) if x]
    return min(cands) if cands else None


# ---------- ingest ----------
class Recorder:
    """Called by the API event bus for every event (replay too — inserts deduplicated by stream id)"""

    def __init__(self):
        self.last_hb: dict[str, float] = {}
        self.last_min: dict[str, int] = {}
        self.stopped: set[str] = set()

    def on_event(self, ev: dict, replay: bool = False, now: float | None = None) -> None:
        t, node = ev.get("type"), ev.get("node") or ""
        if not node:
            return
        if t == "heartbeat":
            if not replay:
                self._heartbeat(node, now or time.time())
            return
        if t not in KEEP_TYPES:
            return
        ts_ns = int(ev.get("ts_ns") or time.time_ns())
        eid = ev.get("_id") or f"{node}:{t}:{ts_ns}:{(ev.get('data') or {}).get('client_order_id', '')}:{(ev.get('data') or {}).get('event', '')}"
        con = connect()
        con.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (eid, _stream_ns(ev.get("_id")) or ts_ns, ev.get("ts") or _iso(ts_ns), ts_ns, _day(ts_ns), node, t,
                     ev.get("strategy", ""), ev.get("instrument", ""), json.dumps(ev.get("data") or {}, default=str)))
        con.commit()
        con.close()
        if t == "node" and not replay:
            e = (ev.get("data") or {}).get("event")
            if e == "stopping":
                self.stopped.add(node)
                self.last_hb.pop(node, None)
            elif e == "started":
                self.stopped.discard(node)

    def _heartbeat(self, node: str, now: float) -> None:
        last = self.last_hb.get(node)
        if last is not None and now - last > GAP_S and node not in self.stopped:
            incident(node, "heartbeat_gap", f"no heartbeat for {now - last:.0f}s", int(now * 1e9))
        self.last_hb[node] = now
        self.stopped.discard(node)
        m = int(now // 60)
        if self.last_min.get(node) != m:
            self.last_min[node] = m
            con = connect()
            con.execute("INSERT OR IGNORE INTO alive VALUES (?,?)", (node, m))
            con.commit()
            con.close()


def incident(node: str, kind: str, detail: str = "", ts_ns: int | None = None) -> None:
    ts_ns = ts_ns or time.time_ns()
    con = connect()
    con.execute("INSERT INTO incidents (ts, ts_ns, day, node, kind, detail) VALUES (?,?,?,?,?,?)",
                (_iso(ts_ns), ts_ns, _day(ts_ns), node, kind, detail[:500]))
    con.commit()
    con.close()


# ---------- metrics ----------
def _events(con, node: str, etype: str, since: int) -> list[dict]:
    rows = con.execute("SELECT * FROM events WHERE node = ? AND type = ? AND ts_ns >= ? ORDER BY ts_ns, emit_ns", (node, etype, since)).fetchall()
    return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]


def pip_size(instrument: str) -> float:
    sym = (instrument or "").split(".")[0]
    if "/" in sym:
        return 0.01 if sym.endswith("JPY") else 0.0001
    return 0.01


def live_actions(con, node: str, since: int) -> list[tuple[int, int]]:
    return [(s["ts_ns"], int(s["data"]["action"])) for s in _events(con, node, "signal", since) if s["data"].get("action") is not None]


def executions(con, node: str, since: int, window_s: float = 120.0) -> list[dict]:
    """Match signals (with an action) to the first fill after the signal (including partial fills of the same order)"""
    sigs = [s for s in _events(con, node, "signal", since) if s["data"].get("action") is not None]
    fills = _events(con, node, "fill", since)
    used: set[str] = set()
    out = []
    for s in sigs:
        t0 = s["emit_ns"] or s["ts_ns"]
        first = next((f for f in fills if f["id"] not in used and f["instrument"] == s["instrument"]
                      and s["ts_ns"] <= f["ts_ns"] <= t0 + window_s * 1e9), None)
        if first is None:
            continue
        coid = first["data"].get("client_order_id")
        same = [f for f in fills if f["id"] not in used and f["data"].get("client_order_id") == coid] if coid else [first]
        used.update(f["id"] for f in same)
        qty = sum(float(f["data"].get("qty") or 0) for f in same) or 1.0
        px = sum(float(f["data"].get("price") or 0) * float(f["data"].get("qty") or 0) for f in same) / qty
        side = str(first["data"].get("side", "")).upper()
        d = 1 if side in ("BUY", "BOT") else -1
        close = float(s["data"].get("close") or 0)
        pip = pip_size(s["instrument"])
        out.append({"ts": s["ts"], "day": s["day"], "instrument": s["instrument"], "signal": s["data"].get("name"),
                    "side": "BUY" if d > 0 else "SELL", "qty": qty, "close": close, "fill_px": px,
                    "slippage_pips": round((px - close) * d / pip, 3) if close else None,
                    "latency_s": round(max(0.0, (first["ts_ns"] - t0) / 1e9), 3)})
    return out


def _p95(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def daily(con, node: str, since: int, ex: list[dict] | None = None) -> list[dict]:
    ex = ex if ex is not None else executions(con, node, since)
    days: dict[str, dict] = {}

    def d(day: str) -> dict:
        return days.setdefault(day, {"day": day, "alive_min": 0, "signals": 0, "actions": 0, "fills": 0, "rejects": 0, "denied": 0,
                                     "gaps": 0, "crashes": 0, "reconcile": 0, "slippage": [], "latency": []})
    for r in con.execute("SELECT minute FROM alive WHERE node = ? AND minute >= ?", (node, since // 60_000_000_000)):
        d(_day(r[0] * 60_000_000_000))["alive_min"] += 1
    for r in con.execute("SELECT day, type, data FROM events WHERE node = ? AND ts_ns >= ?", (node, since)):
        x, data = d(r["day"]), json.loads(r["data"] or "{}")
        if r["type"] == "signal":
            x["signals"] += 1
            x["actions"] += data.get("action") is not None
        elif r["type"] == "fill":
            x["fills"] += 1
        elif r["type"] == "order" and data.get("event") == "Rejected":
            x["rejects"] += 1
        elif r["type"] == "order" and data.get("event") == "Denied":
            x["denied"] += 1
    for r in con.execute("SELECT day, kind FROM incidents WHERE node = ? AND ts_ns >= ?", (node, since)):
        k = {"heartbeat_gap": "gaps", "crash": "crashes", "reconcile": "reconcile"}.get(r["kind"])
        if k:
            d(r["day"])[k] += 1
    for e in ex:
        if e["slippage_pips"] is not None:
            d(e["day"])["slippage"].append(e["slippage_pips"])
        d(e["day"])["latency"].append(e["latency_s"])
    out = []
    for day in sorted(days):
        x = days[day]
        s, l = x.pop("slippage"), x.pop("latency")
        x["avg_slippage_pips"] = round(statistics.mean(s), 3) if s else None
        x["p95_latency_s"] = _p95(l)
        out.append(x)
    return out


# ---------- parity (live vs backtest + old shadow agent) ----------
def match_actions(live: list[tuple[int, int]], expected: list[tuple[int, int]], tol_ns: int, max_rows: int = 30) -> dict:
    """Match (bar time, value) allowing ±tol time drift (live bars are built from quotes, catalog bars from IBKR historical)"""
    left = sorted(live)
    used = [False] * len(left)
    matched, offsets, miss_exp = 0, [], []
    for t, v in sorted(expected):
        j = next((i for i, (lt, lv) in enumerate(left) if not used[i] and lv == v and abs(lt - t) <= tol_ns), None)
        if j is None:
            miss_exp.append((t, v))
            continue
        used[j] = True
        matched += 1
        offsets.append(left[j][0] - t)
    extra = [left[i] for i in range(len(left)) if not used[i]]
    rows = sorted([{"ts": _iso(t), "expected": v, "live": None} for t, v in miss_exp] +
                  [{"ts": _iso(t), "expected": None, "live": v} for t, v in extra], key=lambda r: r["ts"])
    denom = max(len(left), len(expected))
    return {"live_actions": len(left), "expected_actions": len(expected), "matched": matched,
            "match_rate": round(matched / denom, 4) if denom else None,
            "median_offset_s": round(statistics.median(offsets) / 1e9, 1) if offsets else None,
            "divergences": len(rows), "divergence_rows": rows[:max_rows]}


def run_parity(node: str, cfg: dict, since: int | None = None, until: int | None = None, warmup_days: float = 3,
               catalog_dir=None) -> dict:
    """Requires bars in the Data catalog (built every morning at 05:20) — compares up to the latest bar in the catalog"""
    from qx_strategies.backtest import load_bars, run_backtest
    from qx_strategies.parity import PAIRS, replay_agent

    con = connect()
    try:
        since = since or start_ns(node, con)
        if not since:
            raise ValueError(f"no soak data for node '{node}' yet")
        until = until or time.time_ns()
        alive = {r[0] for r in con.execute("SELECT minute FROM alive WHERE node = ? AND minute >= ?", (node, since // 60_000_000_000))}
        live_all = live_actions(con, node, since)
    finally:
        con.close()
    tf_ns = int(cfg.get("tf_minutes", 5)) * 60_000_000_000
    live_all = [(t + LIVE_BAR_TS_OFFSET_BARS * tf_ns, v) for t, v in live_all]     # bar open → bar close (same stamp as catalog bars)
    tol_ns = tf_ns // 2                                                            # a one-bar-late live action is NOT a match
    s = datetime.fromtimestamp(since / 1e9, timezone.utc) - timedelta(days=warmup_days)
    e = datetime.fromtimestamp(until / 1e9, timezone.utc)
    bars = load_bars(cfg, s.isoformat(), e.isoformat(), catalog_dir)
    if not bars:
        raise ValueError("no catalog bars for this period — build the catalog first")
    last_bar = bars[-1].ts_event

    def in_window(t: int) -> bool:
        m = t // 60_000_000_000
        return since <= t <= last_bar and (m in alive or m - 1 in alive or m - 2 in alive)
    acts: list = []
    bt = run_backtest(cfg, bars=bars, log_level="OFF", _collect_actions=acts, catalog_dir=catalog_dir)   # cross pairs: USD rate bars from the same catalog
    expected = [(t, int(v)) for t, v in acts if in_window(t)]
    live = [(t, v) for t, v in live_all if t <= last_bar]
    lo_iso, hi_iso = _iso(since), _iso(last_bar)
    bt_trades = [[t["closed"], t["pnl"]] for t in bt.get("trades") or [] if t.get("closed") and lo_iso <= t["closed"] <= hi_iso][-5000:]
    res = {"node": node, "ts": _iso(time.time_ns()), "from": _iso(since), "to": _iso(last_bar), "bars": len(bars),
           "backtest_trades": bt_trades,
           "tf_minutes": int(cfg.get("tf_minutes", 5)), "alive_minutes": len(alive),
           "live_vs_backtest": match_actions(live, expected, tol_ns), "backtest_pnl_usd": bt.get("pnl_usd"), "shadow": None}
    name = cfg.get("strategy", "")
    if name in PAIRS:
        try:
            old = replay_agent(name, {**cfg, "sl_pips": 0, "tp_pips": 0}, bars)
            o = [(t, int(v)) for t, v in old["actions"] if in_window(t) and v != 0]
            res["shadow"] = {"agent": PAIRS[name]["agent"][1], **match_actions([(t, v) for t, v in live if v != 0], o, tol_ns),
                             "note": "entries only — the legacy agent has no SL/TP"}
        except Exception as ex:
            res["shadow"] = {"error": str(ex)[:200]}
    con = connect()
    con.execute("INSERT OR REPLACE INTO parity VALUES (?,?,?)", (node, res["ts"], json.dumps(res, default=str)))
    con.commit()
    con.close()
    return res


def last_parity(con, node: str) -> dict | None:
    r = con.execute("SELECT result FROM parity WHERE node = ?", (node,)).fetchone()
    return json.loads(r[0]) if r else None


# ---------- verdict ----------
def _check(name: str, value, ok: bool | None, target: str, detail: str = "") -> dict:
    return {"name": name, "value": value, "status": "pending" if ok is None else ("pass" if ok else "fail"),
            "target": target, "detail": detail}


def report(node: str, full: bool = False, ref: str | None = None) -> dict:
    """ref: judge the soak for this strategy version only — evidence recorded for another (or an unrecorded) version → PENDING"""
    c = criteria()
    if ref is not None:
        have = soak_ref(node)
        if have != ref:
            return {"node": node, "verdict": "PENDING", "since": None, "days": 0, "checks": [], "strategy_ref": have,
                    "note": f"soak evidence is for {have}, not {ref}" if have else f"no soak recorded for {ref} on this node yet"}
    con = connect()
    try:
        since = start_ns(node, con)
        if not since:
            return {"node": node, "verdict": "PENDING", "since": None, "days": 0, "checks": [], "note": "no data yet"}
        ex = executions(con, node, since)
        days = daily(con, node, since, ex)
        par = last_parity(con, node)
        inc = [dict(r) for r in con.execute("SELECT ts, kind, detail FROM incidents WHERE node = ? AND ts_ns >= ? ORDER BY ts_ns DESC LIMIT 50", (node, since))]
    finally:
        con.close()
    run_days = sum(1 for d in days if d["alive_min"] >= c["min_alive_minutes"])
    slips = [e["slippage_pips"] for e in ex if e["slippage_pips"] is not None]
    lats = [e["latency_s"] for e in ex]
    tot = {k: sum(d[k] for d in days) for k in ("signals", "actions", "fills", "rejects", "denied", "gaps", "crashes", "reconcile")}
    lvb = (par or {}).get("live_vs_backtest") or {}
    stale_par = par is not None and par.get("from") != _iso(since)
    n_act = max(lvb.get("live_actions") or 0, lvb.get("expected_actions") or 0)
    avg_slip = round(statistics.mean(slips), 3) if slips else None
    abs_slip = round(statistics.mean(abs(x) for x in slips), 3) if slips else None      # gate: +5/−5 must not average out to 0
    p95 = _p95(lats)
    checks = [
        _check("Running days", run_days, True if run_days >= c["min_days"] else None, f"≥ {c['min_days']} days",
               f"days with ≥ {c['min_alive_minutes']} min of heartbeats"),
        _check("Decisions: live vs backtest", lvb.get("match_rate"),
               None if (not par or stale_par or n_act < c["min_actions"] or lvb.get("match_rate") is None) else lvb["match_rate"] >= c["min_decision_match"],
               f"≥ {c['min_decision_match']:.0%}",
               "run parity" if not par else ("re-run parity (soak was reset)" if stale_par else f"{lvb.get('matched')}/{n_act} matched · up to {par.get('to', '')[:16]}")),
        _check("Avg slippage (pips)", abs_slip, None if len(slips) < c["min_executions"] else abs_slip <= c["max_avg_slippage_pips"],
               f"≤ {c['max_avg_slippage_pips']}", f"mean |slippage| of {len(slips)} executions vs signal bar close · signed mean {avg_slip}"),
        _check("p95 latency (s)", p95, None if len(lats) < c["min_executions"] else p95 <= c["max_p95_latency_s"],
               f"≤ {c['max_p95_latency_s']}", "signal → first fill"),
        _check("Broker rejects", tot["rejects"], tot["rejects"] <= c["max_rejects"], f"≤ {c['max_rejects']}", f"{tot['denied']} denied by risk (info)"),
        _check("Crashes", tot["crashes"], tot["crashes"] <= c["max_crashes"], f"≤ {c['max_crashes']}"),
        _check("Heartbeat gaps", tot["gaps"], tot["gaps"] <= c["max_heartbeat_gaps"], f"≤ {c['max_heartbeat_gaps']}", f"> {GAP_S:.0f}s without heartbeat"),
        _check("Reconcile incidents", tot["reconcile"], tot["reconcile"] <= c["max_reconcile_incidents"], f"≤ {c['max_reconcile_incidents']}"),
    ]
    st = [x["status"] for x in checks]
    verdict = "NO-GO" if "fail" in st else ("GO" if all(s == "pass" for s in st) else "PENDING")
    out = {"node": node, "verdict": verdict, "since": _iso(since), "days": run_days, "totals": tot,
           "avg_slippage_pips": avg_slip, "avg_abs_slippage_pips": abs_slip, "p95_latency_s": p95, "strategy_ref": soak_ref(node), "decision_match": lvb.get("match_rate"),
           "parity_ts": (par or {}).get("ts"), "checks": checks}
    if full:
        out.update(daily=days, executions=ex[-50:], incidents=inc, parity=par, criteria=c)
    return out
