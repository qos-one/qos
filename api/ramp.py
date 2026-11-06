"""
Live ramp-up (step 14) — a node granted allow_live starts trading real money at a small size, then scales up on actual results

  stage  0     1      2     3
  size   10% → 25% → 50% → 100% of trade_size in the config

- Advance when all hold: traded live for min_days business days in this stage, closed trades ≥ min_trades, loss/DD within the stage limit,
  no crash / order rejected / reconcile mismatch, parity (if run) ≥ min_match
  auto_advance=false (default) → notify "ready to advance" for the operator to confirm · true → advance automatically
- Automatic step-down (safety does not wait for a human): DD or loss over the stage limit → down 1 stage (first stage = stop the node)
  crash / rejected / reconcile mismatch → stop the node (paused) until the operator resumes with the live confirmation code
- Stage loss limit = budget_usd × stage size (budget_usd = money you accept losing over the whole ramp-up at full size)
- The new size takes effect when the node restarts — the API waits until the node has no position and no pending orders before restarting

This module is pure logic (no IB / file access) — server.py gathers the figures and calls evaluate()
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

DEFAULT = {"stages": [0.1, 0.25, 0.5, 1.0], "min_days": 3, "min_trades": 3, "budget_usd": 1000.0, "max_crashes": 0,
           "max_rejects": 0, "max_reconcile": 0, "min_match": 0.9, "auto_advance": False, "lot": 1000}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def clean_config(cfg: dict | None) -> dict:
    c = {**DEFAULT, **{k: v for k, v in (cfg or {}).items() if k in DEFAULT}}
    st = [float(x) for x in c["stages"]]
    if not st or any(not 0 < x <= 1 for x in st) or st != sorted(st) or st[-1] != 1.0:
        raise ValueError("stages must be increasing fractions ending at 1.0, e.g. [0.1, 0.25, 0.5, 1.0]")
    c["stages"] = st
    for k, lo, hi in (("min_days", 1, 60), ("min_trades", 0, 1000), ("max_crashes", 0, 100), ("max_rejects", 0, 100),
                      ("max_reconcile", 0, 100), ("lot", 1, 1_000_000)):
        c[k] = int(c[k])
        if not lo <= c[k] <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
    c["budget_usd"] = float(c["budget_usd"])
    if c["budget_usd"] <= 0:
        raise ValueError("budget_usd must be > 0")
    c["min_match"] = float(c["min_match"])
    if not 0 <= c["min_match"] <= 1:
        raise ValueError("min_match must be between 0 and 1")
    c["auto_advance"] = bool(c["auto_advance"])
    return c


def new_ramp(cfg: dict | None = None, now: datetime | None = None) -> dict:
    now = now or _now()
    return {"config": clean_config(cfg), "stage": 0, "stage_started": _iso(now), "status": "active", "ready": False, "note": "",
            "history": [{"ts": _iso(now), "stage": 0, "action": "start", "by": "system", "why": "live allowed"}]}


def multiplier(r: dict | None) -> float:
    if not r:
        return DEFAULT["stages"][0]                    # no ramp-up on live = smallest size to be safe
    st = r["config"]["stages"]
    return float(st[min(int(r["stage"]), len(st) - 1)])


def size_problem(base, mult: float, lot: int = 1000) -> str:
    """'' if trade_size × stage size is at least 1 lot · otherwise why the stage cannot be traded at its intended size"""
    b = float(base)
    target = b * mult
    if not math.isfinite(target) or target <= 0:
        return f"invalid trade_size {base!r}"
    if mult >= 1.0:                                   # final stage: the configured trade_size is used as is (no lot rule)
        return ""
    if round(target, 6) < lot:
        return (f"stage size {mult:.0%} of trade_size {b:,.0f} = {target:,.0f} is below one lot ({lot:,}) — "
                f"raise trade_size to at least {math.ceil(lot / mult):,} or lower the ramp lot")
    return ""


def scaled_size(base, mult: float, lot: int = 1000) -> str:
    """trade_size × stage size, rounded DOWN to a whole number of lots (never above the stage size)
    Raises ValueError when that is less than 1 lot — trading 1 lot instead would exceed the stage (e.g. 1000 at 10% = 100% size)
    Final 100% stage: the configured trade_size unchanged (not rounded to lots)"""
    why = size_problem(base, mult, lot)
    if why:
        raise ValueError(why)
    if mult >= 1.0:
        return str(base)
    return str(int(math.floor(round(float(base) * mult / lot, 6)) * lot))


def stage_cap(r: dict) -> float:
    return round(r["config"]["budget_usd"] * multiplier(r), 2)


def business_days(since_iso: str, now: datetime | None = None) -> int:
    """Full business days (Mon–Fri) elapsed since the stage start (excludes the start day · includes today)"""
    start = datetime.fromisoformat(since_iso).astimezone(timezone.utc).date()
    end = (now or _now()).astimezone(timezone.utc).date()
    d, n = start + timedelta(days=1), 0
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def drawdown(pnls: list[float]) -> float:
    eq = peak = dd = 0.0
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 2)


def evaluate(r: dict, m: dict, now: datetime | None = None) -> dict:
    """m: {net_usd, max_dd_usd, trades, crashes, rejects, reconcile, match (None = parity not run yet)}
    returns {action: hold|advance|demote|pause|complete, checks: [{name, ok, value, target}], reasons}"""
    c, cap = r["config"], stage_cap(r)
    days = business_days(r["stage_started"], now)
    checks = []

    def chk(name, ok, value, target):
        checks.append({"name": name, "ok": bool(ok), "value": value, "target": target})
        return ok

    loss_ok = chk("loss", m.get("net_usd", 0.0) > -cap, round(m.get("net_usd", 0.0), 2), f"> -{cap:,.0f} USD")
    dd_ok = chk("drawdown", m.get("max_dd_usd", 0.0) <= cap, m.get("max_dd_usd", 0.0), f"≤ {cap:,.0f} USD")
    crash_ok = chk("crashes", m.get("crashes", 0) <= c["max_crashes"], m.get("crashes", 0), f"≤ {c['max_crashes']}")
    rej_ok = chk("rejects", m.get("rejects", 0) <= c["max_rejects"], m.get("rejects", 0), f"≤ {c['max_rejects']}")
    rec_ok = chk("reconcile", m.get("reconcile", 0) <= c["max_reconcile"], m.get("reconcile", 0), f"≤ {c['max_reconcile']}")
    days_ok = chk("days", days >= c["min_days"], days, f"≥ {c['min_days']} trading days")
    trades_ok = chk("trades", m.get("trades", 0) >= c["min_trades"], m.get("trades", 0), f"≥ {c['min_trades']}")
    match = m.get("match")
    match_ok = chk("parity", match is None or match >= c["min_match"], match, f"≥ {c['min_match']:.0%} (if run)")
    if not (crash_ok and rej_ok and rec_ok):
        bad = [x["name"] for x in checks if x["name"] in ("crashes", "rejects", "reconcile") and not x["ok"]]
        return {"action": "pause", "checks": checks, "reasons": [f"{', '.join(bad)} over the limit — node paused for review"]}
    if not (loss_ok and dd_ok):
        act = "demote" if r["stage"] > 0 else "pause"
        return {"action": act, "checks": checks, "reasons": [f"loss/drawdown over this stage's cap {cap:,.0f} USD"]}
    if r["stage"] >= len(c["stages"]) - 1:
        return {"action": "complete", "checks": checks, "reasons": ["at full size"]}
    waiting = [x["name"] for x in checks if not x["ok"]]
    if waiting:
        return {"action": "hold", "checks": checks, "reasons": [f"waiting for: {', '.join(waiting)}"]}
    return {"action": "advance", "checks": checks, "reasons": [f"all criteria met at {multiplier(r):.0%} size"]}


def apply(r: dict, action: str, by: str = "auto", why: str = "", now: datetime | None = None) -> bool:
    """Change stage/state per action · returns True if the size changed (the node must restart)"""
    now = now or _now()
    old = (r["stage"], r["status"])
    last = len(r["config"]["stages"]) - 1
    if action == "advance" and r["stage"] < last:
        r["stage"] += 1
    elif action == "demote" and r["stage"] > 0:
        r["stage"] -= 1
    elif action == "pause":
        r["status"] = "paused"
    elif action == "resume":
        r["status"] = "active"
    elif action == "reset":
        r["stage"], r["status"] = 0, "active"
    if (r["stage"], r["status"]) == old and action not in ("resume", "reset"):
        return False
    if r["stage"] != old[0] or action in ("resume", "reset"):
        r["stage_started"] = _iso(now)
    r["ready"] = False
    r["note"] = why
    r.setdefault("history", []).append({"ts": _iso(now), "stage": r["stage"], "status": r["status"], "action": action, "by": by, "why": why})
    r["history"] = r["history"][-100:]
    return r["stage"] != old[0]


def today() -> date:
    return _now().date()
