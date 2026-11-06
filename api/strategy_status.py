"""
Lifecycle state of strategy packages per version (step 20) — data/strategy_status.json

  installed → backtested → wf_passed → soak_go → live

- Advances automatically when criteria pass and steps back when they no longer do (re-evaluation fails, soak turns NO-GO, allow_live turned off)
  backtested : 90-day backtest net > 0 and max DD ≤ max_drawdown_pct in the manifest (default 10%)
  wf_passed  : walk-forward out-of-sample positive in ≥ 60% of folds
  soak_go    : soak verdict GO for at least one node using this version
  live       : a node using this version has allow_live
- Built-in strategies (@builtin) count as having passed backtest / walk-forward (reference implementations that passed parity in step 13), starting at wf_passed
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

ROOT = Path(__file__).resolve().parent.parent
STATES = ["installed", "backtested", "wf_passed", "soak_go", "live"]
_lock = threading.Lock()


def path() -> Path:
    return Path(os.getenv("QOS_STATUS_FILE") or paths.data_dir() / "strategy_status.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    try:
        return json.loads(path().read_text())
    except Exception:
        return {}


def _save(doc: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str))
    tmp.replace(p)


def _blank(ref: str, builtin: bool = False) -> dict:
    s = "wf_passed" if builtin else "installed"
    return {"state": s, "history": [{"ts": _now(), "state": s, "why": "builtin" if builtin else "installed"}],
            "installed_at": _now(), "backtest": None, "walkforward": None, "evaluating": False, "eval_error": None, "builtin": builtin}


def ensure(ref: str, builtin: bool = False, source: str | None = None) -> dict:
    with _lock:
        doc = load()
        if ref not in doc:
            doc[ref] = _blank(ref, builtin)
            if source:
                doc[ref]["source"] = source
            _save(doc)
        return doc[ref]


def reset(ref: str, source: str | None = None) -> dict:
    """Reinstall (--force): the code changed, old evaluation results no longer apply — restart at installed (history kept)"""
    with _lock:
        doc = load()
        old = doc.get(ref) or {}
        e = _blank(ref)
        e["history"] = (old.get("history") or [])[-40:] + [{"ts": _now(), "state": "installed", "from": old.get("state"), "why": "reinstalled"}]
        if source:
            e["source"] = source
        doc[ref] = e
        _save(doc)
        return e


def drop(ref: str) -> None:
    with _lock:
        doc = load()
        if doc.pop(ref, None) is not None:
            _save(doc)


def set_evaluating(ref: str, on: bool, error: str | None = None) -> None:
    with _lock:
        doc = load()
        e = doc.setdefault(ref, _blank(ref))
        e["evaluating"] = on
        if error is not None or not on:
            e["eval_error"] = error
        _save(doc)


_KEEP = object()


def set_result(ref: str, backtest=_KEEP, walkforward=_KEEP) -> dict:
    """Send only the parts that were re-run — parts not sent keep their previous results"""
    with _lock:
        doc = load()
        e = doc.setdefault(ref, _blank(ref))
        if backtest is not _KEEP:
            e["backtest"] = backtest
        if walkforward is not _KEEP:
            e["walkforward"] = walkforward
        e.update(evaluated_at=_now(), evaluating=False, eval_error=None)
        _advance(e, soak_go=False, live=False, keep_upper=True)
        _save(doc)
        return e


def derive(e: dict, soak_go: bool, live: bool) -> tuple[str, str]:
    """Return (state, note) — passes stages in order · note says which criterion blocks"""
    if e.get("builtin"):
        bt_ok = wf_ok = True
    else:
        bt_ok = bool((e.get("backtest") or {}).get("ok"))
        wf_ok = bt_ok and bool((e.get("walkforward") or {}).get("ok"))
    if not bt_ok:
        bt = e.get("backtest")
        if bt is None:
            return "installed", "evaluating…" if e.get("evaluating") else ("evaluation failed: " + e["eval_error"] if e.get("eval_error") else "not evaluated yet")
        return "installed", f"backtest net {bt.get('pnl_usd', 0):+,.0f}, DD {bt.get('max_drawdown_pct', 0):.1f}% (cap {bt.get('dd_cap_pct', 10):g}%)"
    if not wf_ok:
        wf = e.get("walkforward") or {}
        return "backtested", wf.get("error") or f"walk-forward {wf.get('positive_folds', 0)}/{wf.get('folds', 0)} positive folds (need 60%)"
    if not soak_go:
        return "wf_passed", "waiting for a soak GO on a node using this version"
    if not live:
        return "soak_go", "ready for allow_live"
    return "live", ""


def _advance(e: dict, soak_go: bool, live: bool, keep_upper: bool = False) -> bool:
    state, note = derive(e, soak_go, live)
    if keep_upper and STATES.index(e.get("state", "installed")) > STATES.index(state) and state == "wf_passed":
        state = e["state"]                                     # after a passing re-evaluation: soak/live stay until the next refresh
    e["note"] = note
    if state != e.get("state"):
        prev = e.get("state")
        e["state"] = state
        e.setdefault("history", []).append({"ts": _now(), "state": state, "from": prev, "why": note or state})
        e["history"] = e["history"][-50:]
        return True
    return False


def refresh(facts: dict[str, dict]) -> dict:
    """facts: {ref: {"soak_go": bool, "live": bool, "builtin": bool}} → update every state, return the whole document"""
    with _lock:
        doc = load()
        changed = False
        for ref, f in facts.items():
            if ref not in doc:
                doc[ref] = _blank(ref, f.get("builtin", False))
                changed = True
            changed |= _advance(doc[ref], bool(f.get("soak_go")), bool(f.get("live")))
        if changed:
            _save(doc)
        return doc


def reset_evaluating() -> None:
    """On API startup: evaluation jobs left over from the previous run no longer exist"""
    with _lock:
        doc = load()
        hit = [r for r, e in doc.items() if e.get("evaluating")]
        for r in hit:
            doc[r].update(evaluating=False, eval_error="interrupted (API restarted) — run: qos evaluate " + r)
        if hit:
            _save(doc)


def state_of(ref: str) -> str | None:
    return (load().get(ref) or {}).get("state")
