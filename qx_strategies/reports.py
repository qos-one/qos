"""
Storage for backtest / parity / walk-forward reports (reports/<kind>/*.json — not in git) and read helpers for the API/Terminal
Does not import nautilus → lightweight enough for the API
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file paths come from QOS_HOME)

ROOT = Path(__file__).resolve().parent.parent
REPORTS = paths.reports_dir()


def shown(path: Path) -> str:
    """Short report path read by the Strategy lab: reports/<kind>/<file> (independent of where reports live)"""
    path = Path(path)
    return f"reports/{path.parent.name}/{path.name}"
KINDS = {"backtest": "backtests", "parity": "parity", "walkforward": "walkforward"}
_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+\.json$")


def save_report(kind: str, name: str, doc: dict) -> Path:
    d = REPORTS / KINDS[kind]
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", name)
    path = d / f"{safe}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"kind": kind, "created": datetime.now().isoformat(timespec="seconds"), **doc}, indent=2, default=str))
    return path


def headline(kind: str, doc: dict) -> dict:
    """Key figures for lists (every kind has the same pnl/trades/label)"""
    if kind == "parity":
        return {"label": f"match {doc.get('match_rate', 0) * 100:.1f}%", "pnl": doc.get("new", {}).get("pnl_usd"),
                "trades": doc.get("new", {}).get("closed_positions"), "score": doc.get("match_rate")}
    if kind == "walkforward":
        oos = doc.get("oos", {})
        return {"label": f"{oos.get('positive_folds', 0)}/{oos.get('folds', 0)} folds +", "pnl": oos.get("pnl_usd"),
                "trades": oos.get("trades"), "score": oos.get("pnl_usd")}
    return {"label": f"win {((doc.get('win_rate') or 0) * 100):.0f}%", "pnl": doc.get("pnl_usd"),
            "trades": doc.get("closed_positions"), "score": doc.get("return_pct")}


def list_reports(limit: int = 100) -> list[dict]:
    files = []
    for kind, sub in KINDS.items():
        d = REPORTS / sub
        if d.exists():
            files += [(kind, f) for f in d.glob("*.json")]
    files.sort(key=lambda kf: kf[1].stat().st_mtime, reverse=True)
    out = []
    for kind, f in files[:limit]:
        try:
            doc = json.loads(f.read_text())
        except Exception:
            continue
        cfg = doc.get("config", {})
        out.append({"kind": kind, "file": f.name, "created": doc.get("created") or datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
                    "strategy": doc.get("strategy") or cfg.get("strategy_id") or cfg.get("strategy", ""),
                    "instrument": cfg.get("instrument", ""), "tf_minutes": cfg.get("tf_minutes"),
                    "start": doc.get("start") or doc.get("from"), "end": doc.get("end") or doc.get("to"),
                    "config_file": doc.get("config_file", ""), "strategy_id": cfg.get("strategy_id", ""),
                    "passed": doc.get("passed"),
                    **headline(kind, doc)})
    return out


def _thin(points: list, n: int = 600) -> list:
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)] + [points[-1]]


def load_report(kind: str, file: str, max_trades: int = 500) -> dict:
    if kind not in KINDS or not _SAFE.match(file):
        raise ValueError("invalid report")
    doc = json.loads((REPORTS / KINDS[kind] / file).read_text())
    doc["kind"] = kind
    doc["file"] = file
    for k in ("equity", "equity_old"):
        if k in doc:
            doc[k] = _thin(doc[k])
    for side in ("oos", "new", "old"):
        if isinstance(doc.get(side), dict) and "equity" in doc[side]:
            doc[side]["equity"] = _thin(doc[side]["equity"])
    if isinstance(doc.get("trades"), list):
        doc["trades_total"] = len(doc["trades"])
        doc["trades"] = doc["trades"][-max_trades:]
    return doc
