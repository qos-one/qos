"""Bring the fill-history books back in line with reality after nodes restarted flat (e.g. after moving the engine).

A restarted Nautilus node reports flat while its fill-history book still holds the old position, and closing that position by
hand books the close to the manual/agent book. Reconcile then shows "node live position and fill history disagree".

For each instrument this writes 'adjustment' rows (realized P&L 0, like api.history.adjust_book):
  - every node book whose node is live (fresh heartbeat) → set to the quantity the node reports
  - the manual/agent book → IBKR quantity minus what the live nodes hold
Nothing is sent to IBKR. Dry run by default.

    uv run scripts/reset_node_books.py FX:AUDUSD FX:GBPUSD FX:USDJPY            # show what would change
    uv run scripts/reset_node_books.py FX:AUDUSD FX:GBPUSD FX:USDJPY --apply    # write it
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def api_get(base: str, path: str) -> dict:
    req = urllib.request.Request(base + path, headers={"X-Operator-Token": os.getenv("QOS_OPERATOR_TOKEN", ""), "X-Source": "reset_node_books"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def plan(rows: dict, books: dict, keys: list[str]) -> list[dict]:
    """rows: reconcile rows by key · books: history.book_positions(account) → list of changes {key, book, current, target}"""
    out = []
    for k in keys:
        r = rows.get(k)
        if r is None:
            out.append({"key": k, "error": "not in reconcile (no IBKR position and no book) — nothing to do"})
            continue
        b = books.get(k, {})
        stale = [n["node"] for n in r.get("nodes", []) if n.get("state") != "live"]
        if stale:
            out.append({"key": k, "error": f"node {', '.join(stale)} has no fresh heartbeat — start it first, then run again"})
            continue
        live_total = 0.0
        for n in r.get("nodes", []):
            live_total += float(n.get("qty") or 0.0)
            cur = float(b.get(f"node:{n['node']}", 0.0))
            if abs(cur - float(n.get("qty") or 0.0)) > 1e-6:
                out.append({"key": k, "book": f"node:{n['node']}", "node": n["node"], "current": cur, "target": float(n.get("qty") or 0.0)})
        manual_target = float(r.get("ib_qty") or 0.0) - live_total
        cur = float(b.get("", 0.0))
        if abs(cur - manual_target) > 1e-6:
            out.append({"key": k, "book": "", "node": "", "current": cur, "target": manual_target})
    return out


def apply(account: str, changes: list[dict]) -> None:
    from api import history
    con = history.connect()
    now = datetime.now(timezone.utc)
    for c in changes:
        if "error" in c:
            continue
        delta = c["target"] - c["current"]
        history._insert_fill(con, {
            "exec_id": f"adj:{c['key']}:{c['book'] or 'manual'}:{now.isoformat()}:{uuid.uuid4().hex[:8]}",
            "ts": now.isoformat(timespec="seconds"), "key": c["key"], "symbol": c["key"].split(":", 1)[1],
            "side": "BUY" if delta > 0 else "SELL", "qty": abs(delta), "price": None, "commission": 0.0, "realized_pnl": 0.0,
            "order_id": None, "order_ref": "adjustment", "account": account, "node": c["node"],
            "strategy": "book reset after node restart", "source": c["book"] or "adjustment"})
    con.commit()
    con.close()
    history.rebuild_trades()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="+", help="instruments, e.g. FX:AUDUSD")
    ap.add_argument("--api", default=os.getenv("BOT_API", "http://127.0.0.1:8000"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    account = api_get(a.api, "/account")["account_id"]
    rows = {r["key"]: r for r in api_get(a.api, "/reconcile").get("rows", [])}
    from api import history
    changes = plan(rows, history.book_positions(account), a.keys)
    for c in changes:
        if "error" in c:
            print(f"{c['key']:<12} skip: {c['error']}")
        else:
            print(f"{c['key']:<12} {(c['book'] or 'manual/agent'):<24} {c['current']:+,.0f} → {c['target']:+,.0f}")
    if not changes:
        print("books already match")
    if a.apply and any("error" not in c for c in changes):
        apply(account, changes)
        print("written · reconcile updates within 30 s")
    elif changes:
        print("dry run · add --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
