"""Trade history: SQLite (fills, equity, trades, journal) + statistics"""
from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from qx_strategies import fx

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

from . import attribution as attr

DB_FILE = paths.data_dir() / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_rate_pending (
  exec_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS fills (
  exec_id TEXT PRIMARY KEY, ts TEXT NOT NULL, key TEXT NOT NULL, symbol TEXT, side TEXT, qty REAL, price REAL,
  commission REAL, realized_pnl REAL, order_id INTEGER, order_ref TEXT
);
CREATE INDEX IF NOT EXISTS fills_ts ON fills(ts);
CREATE TABLE IF NOT EXISTS equity (
  ts TEXT NOT NULL, netliq REAL, unrealized REAL, realized REAL, account TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (ts, account)
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, symbol TEXT, source TEXT, side TEXT,
  entry_ts TEXT, exit_ts TEXT, qty REAL, entry_price REAL, exit_price REAL,
  pnl REAL, commission REAL, net_pnl REAL, holding_sec REAL, n_fills INTEGER, first_exec TEXT UNIQUE, open INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS node_orders (
  client_order_id TEXT PRIMARY KEY, node TEXT NOT NULL, strategy TEXT DEFAULT '', instrument TEXT DEFAULT '', ts TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS journal (
  first_exec TEXT PRIMARY KEY, note TEXT DEFAULT '', tags TEXT DEFAULT '', rating INTEGER DEFAULT 0
);
"""


_migrated: dict[str, tuple] = {}   # path → (inode, mtime) last checked — a restored / replaced file is checked again


def _migrate(con: sqlite3.Connection) -> None:
    """Add an account column to existing databases (empty = unknown account/before migration)"""
    def cols(t: str) -> set[str]:
        return {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    if "account" not in cols("fills"):
        con.execute("ALTER TABLE fills ADD COLUMN account TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS fills_acct ON fills(account, ts)")
    if "account" not in cols("trades"):
        con.execute("ALTER TABLE trades ADD COLUMN account TEXT NOT NULL DEFAULT ''")
    fc = cols("fills")
    for c in ("node", "strategy", "source"):
        if c not in fc:
            con.execute(f"ALTER TABLE fills ADD COLUMN {c} TEXT NOT NULL DEFAULT ''")
    if "usd_rate" not in fc:          # USD value of one unit of the quote currency when the fill was recorded (cross pairs)
        con.execute("ALTER TABLE fills ADD COLUMN usd_rate REAL")
    if "source" not in fc:
        con.execute("""UPDATE fills SET source = CASE
            WHEN order_ref IS NULL OR order_ref = '' OR order_ref = 'manual' THEN 'manual'
            WHEN order_ref GLOB 'O-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-*' THEN 'nautilus'
            ELSE order_ref END WHERE source = ''""")
    tc = cols("trades")
    for c in ("node", "strategy"):
        if c not in tc:
            con.execute(f"ALTER TABLE trades ADD COLUMN {c} TEXT NOT NULL DEFAULT ''")
    eq = cols("equity")
    if "account" not in eq:
        # old equity PK=ts → recreate with PK=(ts, account)
        con.executescript("""
            ALTER TABLE equity RENAME TO equity_old;
            CREATE TABLE equity (ts TEXT NOT NULL, netliq REAL, unrealized REAL, realized REAL, account TEXT NOT NULL DEFAULT '', PRIMARY KEY (ts, account));
            INSERT OR IGNORE INTO equity (ts, netliq, unrealized, realized, account) SELECT ts, netliq, unrealized, realized, '' FROM equity_old;
            DROP TABLE equity_old;
        """)
    con.commit()


def connect() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    st = DB_FILE.stat()
    if _migrated.get(str(DB_FILE)) != (st.st_ino, st.st_mtime_ns):
        _migrate(con)                    # cheap when nothing is missing (PRAGMA table_info only)
        st = DB_FILE.stat()
        _migrated[str(DB_FILE)] = (st.st_ino, st.st_mtime_ns)
    return con


def claim_unassigned(account: str) -> int:
    """Assign an account to old data without one (used once after migration when the user has a single account)"""
    if not account:
        return 0
    con = connect()
    n = con.execute("UPDATE fills SET account = ? WHERE account = ''", (account,)).rowcount
    con.execute("UPDATE equity SET account = ? WHERE account = ''", (account,))
    con.commit(); con.close()
    if n:
        rebuild_trades()
    return n


def _acct_clause(account: str | None, col: str = "account") -> tuple[str, list]:
    """'' or None = all accounts"""
    if not account:
        return "", []
    return f" AND {col} = ?", [account]


# ---------- ingest ----------
FILL_COLS = ("exec_id", "ts", "key", "symbol", "side", "qty", "price", "commission", "realized_pnl",
             "order_id", "order_ref", "account", "node", "strategy", "source", "usd_rate")
_node_cache: dict[str, tuple[str, str]] = {}


def _node_map(con: sqlite3.Connection, coids: set[str]) -> dict[str, tuple[str, str]]:
    out = {c: _node_cache[c] for c in coids if c in _node_cache}
    todo = [c for c in coids if c and c not in out]
    for i in range(0, len(todo), 500):
        part = todo[i:i + 500]
        for r in con.execute(f"SELECT client_order_id, node, strategy FROM node_orders WHERE client_order_id IN ({','.join('?' * len(part))})", part):
            out[r[0]] = _node_cache[r[0]] = (r[1], r[2] or "")
    return out


def node_for(client_order_id: str) -> tuple[str, str] | None:
    """Nautilus client_order_id → (node, strategy)"""
    if not client_order_id:
        return None
    if client_order_id in _node_cache:
        return _node_cache[client_order_id]
    con = connect()
    try:
        return _node_map(con, {client_order_id}).get(client_order_id)
    finally:
        con.close()


def _fill_usd_rate(key: str) -> float | None:
    """For a cross pair (EUR/GBP, EUR/JPY): USD value of one unit of the quote currency now — stored with the fill so the
    trade's USD P&L is fixed at the time it was realized and never re-valued with later rates"""
    if not key.startswith("FX:"):
        return None
    base, quote = fx.split_pair(key)
    if "USD" in (base, quote):
        return None                  # XXX/USD and USD/XXX convert with the fill price itself
    return fx.usd_rate(quote, _fx_rates)


def _is_cross(key: str) -> bool:
    return key.startswith("FX:") and "USD" not in fx.split_pair(key)


def _insert_fill(con: sqlite3.Connection, r: dict) -> None:
    r = {**r, "usd_rate": r.get("usd_rate") if r.get("usd_rate") else _fill_usd_rate(r.get("key") or "")}
    cur = con.execute(f"INSERT OR IGNORE INTO fills ({','.join(FILL_COLS)}) VALUES ({','.join(':' + c for c in FILL_COLS)})", r)
    if cur.rowcount and not r["usd_rate"] and _is_cross(r.get("key") or ""):
        # cross-pair fill recorded before any rate was known: its rate is filled in (then frozen) on the next set_fx_rates
        con.execute("INSERT OR IGNORE INTO fx_rate_pending VALUES (?)", (r["exec_id"],))


def upsert_fills(rows: list[dict]) -> int:
    """fills from IBKR · rows: exec_id, ts, key, symbol, side(BUY/SELL), qty, price, commission, realized_pnl, order_id, order_ref, account
    new row = insert · existing row = update late commission / realized, empty account and newly matched node
    returns the number of rows added/changed"""
    if not rows:
        return 0
    con = connect()
    nodes = _node_map(con, {attr.client_order_id(r.get("order_ref")) for r in rows} - {""})
    ids = [r["exec_id"] for r in rows]
    existing: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for e in con.execute(f"SELECT exec_id, commission, realized_pnl, account, node, order_ref, order_id FROM fills WHERE exec_id IN ({','.join('?' * len(part))})", part):
            existing[e["exec_id"]] = dict(e)
    changed = 0
    for r in rows:
        r.setdefault("account", "")
        node, strategy = nodes.get(attr.client_order_id(r.get("order_ref")), ("", ""))
        r["node"], r["strategy"], r["source"] = node, strategy, attr.classify(r.get("order_ref"), node)
        e = existing.get(r["exec_id"])
        if e is None:
            _insert_fill(con, r)
            changed += 1
            continue
        upd: dict = {}
        if r.get("commission") and abs((e["commission"] or 0.0) - r["commission"]) > 1e-9:
            upd["commission"] = r["commission"]
        if r.get("realized_pnl") and abs((e["realized_pnl"] or 0.0) - r["realized_pnl"]) > 1e-9:
            upd["realized_pnl"] = r["realized_pnl"]
        if not e["account"] and r["account"]:
            upd["account"] = r["account"]
        if not e["node"] and node:
            upd.update(node=node, strategy=strategy, source=r["source"])
        if r.get("order_ref") and e["order_ref"] != r["order_ref"] and attr.is_nautilus_ref(r["order_ref"]):
            upd["order_ref"] = r["order_ref"]
        if r.get("order_id") and not e["order_id"]:
            upd["order_id"] = r["order_id"]
        if upd:
            con.execute(f"UPDATE fills SET {', '.join(f'{k} = ?' for k in upd)} WHERE exec_id = ?", [*upd.values(), r["exec_id"]])
            changed += 1
    con.commit()
    con.close()
    if changed:
        rebuild_trades()
    return changed


def _attribute_pending(con: sqlite3.Connection, coid: str, node: str, strategy: str) -> int:
    n = 0
    for r in con.execute("SELECT exec_id, order_ref FROM fills WHERE node = '' AND (order_ref = ? OR order_ref LIKE ?)", (coid, coid + ":%")).fetchall():
        if attr.client_order_id(r["order_ref"]) == coid:
            con.execute("UPDATE fills SET node = ?, strategy = ?, source = ? WHERE exec_id = ?", (node, strategy, f"node:{node}", r["exec_id"]))
            n += 1
    return n


def record_node_order(client_order_id: str, node: str, strategy: str = "", instrument: str = "", ts: str = "") -> int:
    """order event from a node: remember which node this client_order_id belongs to, then match fills already recorded"""
    if not client_order_id or not node or _node_cache.get(client_order_id, ("",))[0] == node:
        return 0
    con = connect()
    new = con.execute("INSERT OR IGNORE INTO node_orders VALUES (?,?,?,?,?)", (client_order_id, node, strategy, instrument, ts)).rowcount
    _node_cache[client_order_id] = (node, strategy)
    n = _attribute_pending(con, client_order_id, node, strategy) if new else 0
    con.commit()
    con.close()
    if n:
        rebuild_trades()
    return n


def record_node_fill(r: dict) -> bool:
    """fill event from a node (trade_id = IB execId) → record/tag the node on the fills row · returns True if anything changed"""
    node, strategy = r.get("node", ""), r.get("strategy", "")
    if not r.get("exec_id") or not node:
        return False
    row = {c: r.get(c) for c in FILL_COLS}
    row.update(realized_pnl=r.get("realized_pnl") or 0.0, commission=r.get("commission") or 0.0, account=r.get("account") or "",
               source=f"node:{node}", strategy=strategy or "")
    record_node_order(r.get("order_ref") or "", node, strategy, r.get("instrument", ""), r.get("ts", ""))
    con = connect()
    e = con.execute("SELECT node, commission FROM fills WHERE exec_id = ?", (row["exec_id"],)).fetchone()
    changed = False
    if e is None:
        _insert_fill(con, row)
        changed = True
    elif not e["node"]:
        con.execute("UPDATE fills SET node = ?, strategy = ?, source = ? WHERE exec_id = ?", (node, row["strategy"], row["source"], row["exec_id"]))
        changed = True
    if e is not None and not e["commission"] and row["commission"]:
        con.execute("UPDATE fills SET commission = ? WHERE exec_id = ?", (row["commission"], row["exec_id"]))
        changed = True
    con.commit()
    con.close()
    if changed:
        rebuild_trades()
    return changed


def book_positions(account: str | None = None) -> dict[str, dict[str, float]]:
    """net qty per instrument per book from fill history: {key: {'' | 'node:<id>': qty}}"""
    con = connect()
    ac, aa = _acct_clause(account)
    q = ("SELECT key, CASE WHEN source LIKE 'node:%' THEN source ELSE '' END AS book, "
         "SUM(CASE WHEN side = 'BUY' THEN qty ELSE -qty END) AS net FROM fills WHERE 1=1" + ac + " GROUP BY key, book")
    out: dict[str, dict[str, float]] = {}
    for r in con.execute(q, aa):
        out.setdefault(r["key"], {})[r["book"]] = r["net"] or 0.0
    con.close()
    return out


def adjust_book(account: str, key: str, target: float, note: str = "", price: float | None = None) -> float:
    """Adjust an instrument's manual/agent book to target (e.g. the actual position at IBKR) with an 'adjustment' row
    Used when fill history is incomplete (fills before recording began / while the API was not running) — no effect on P&L:
    realized 0 and the row is ignored by the average-cost calculation (only a book opened from flat takes its price as cost)
    price: cost to record (e.g. the IBKR average cost) · default = cost of the open trade · unknown → NULL (never 0.0)
    returns the adjusted quantity (+ buy / − sell)"""
    cur = float((book_positions(account).get(key) or {}).get("", 0.0))
    delta = float(target) - cur
    if abs(delta) < 1e-6:
        return 0.0
    con = connect()
    if not (price and price > 0 and math.isfinite(price)):
        r = con.execute("SELECT entry_price FROM trades WHERE open = 1 AND node = '' AND key = ? AND account = ? ORDER BY entry_ts DESC LIMIT 1",
                        (key, account or "")).fetchone()
        price = float(r[0]) if r and r[0] and r[0] > 0 else None
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    _insert_fill(con, {"exec_id": f"adj:{key}:{now_dt.isoformat()}:{uuid.uuid4().hex[:8]}", "ts": now, "key": key, "symbol": key.split(":", 1)[1],
                       "side": "BUY" if delta > 0 else "SELL", "qty": abs(delta), "price": float(price) if price else None,
                       "commission": 0.0, "realized_pnl": 0.0, "order_id": None, "order_ref": "adjustment",
                       "account": account or "", "node": "", "strategy": note[:60], "source": "adjustment"})
    con.commit()
    con.close()
    rebuild_trades()
    return delta


def fix_fill_times(order_times: dict[int, str], offset_hours: float, apply: bool = False, now: datetime | None = None) -> dict:
    """Fix IBKR fill times that were read as local time instead of UTC (late by offset_hours) — runs once (remembered in the meta table)
    Decided per row: order in audit → use the actual difference · Nautilus order → compare with the time in client_order_id · the rest → shift (same path as most)
    Leaves adjustment rows and fills from node events (already correct) untouched, and never shifts past the current time"""
    now = now or datetime.now(timezone.utc)
    con = connect()
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    done = con.execute("SELECT v FROM meta WHERE k = 'fill_time_fix_v1'").fetchone()
    if done:
        con.close()
        return {"already_applied": done[0], "changed": 0}
    shift = timedelta(hours=offset_hours)
    changed, kept, rows = [], 0, con.execute("SELECT exec_id, ts, order_id, order_ref, source FROM fills").fetchall()
    for r in rows:
        if r["exec_id"].startswith("adj:"):
            kept += 1
            continue
        ts = datetime.fromisoformat(r["ts"])
        oid, ref = r["order_id"], r["order_ref"] or ""
        wrong = None
        if oid in order_times:
            d = (datetime.fromisoformat(order_times[oid]) - ts).total_seconds() / 3600
            wrong = abs(d - offset_hours) < 0.5
        elif attr.is_nautilus_ref(ref):
            try:
                made = datetime.strptime(ref[2:17], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
                wrong = abs((made - ts).total_seconds() / 3600 - offset_hours) < 0.5
            except ValueError:
                wrong = False
        else:
            wrong = True
        if wrong and ts + shift <= now + timedelta(minutes=5):
            changed.append((r["exec_id"], r["ts"], (ts + shift).isoformat()))
        else:
            kept += 1
    if apply:
        con.executemany("UPDATE fills SET ts = ? WHERE exec_id = ?", [(new, eid) for eid, _old, new in changed])
        con.execute("INSERT INTO meta VALUES ('fill_time_fix_v1', ?)", (f"{now.isoformat(timespec='seconds')} +{offset_hours}h {len(changed)} rows",))
        con.commit()
    con.close()
    if apply and changed:
        rebuild_trades()
    return {"applied": apply, "offset_hours": offset_hours, "changed": len(changed), "kept": kept, "sample": changed[:10]}


def open_sources(account: str | None = None) -> dict[str, str]:
    """source of open trades in the shared book (manual / agent) per instrument"""
    con = connect()
    ac, aa = _acct_clause(account)
    out = {r["key"]: r["source"] for r in con.execute("SELECT key, source FROM trades WHERE open = 1 AND node = ''" + ac + " ORDER BY entry_ts", aa)}
    con.close()
    return out


def open_cost(account: str | None, key: str) -> float | None:
    """Entry price of the open trade in the shared manual/agent book for one instrument (None if flat or unknown)
    — used as the average cost of positions rebuilt from FX cash balances (node books are excluded)"""
    con = connect()
    ac, aa = _acct_clause(account)
    r = con.execute("SELECT entry_price FROM trades WHERE open = 1 AND node = '' AND key = ?" + ac + " ORDER BY entry_ts DESC LIMIT 1",
                    [key, *aa]).fetchone()
    con.close()
    return float(r[0]) if r and r[0] and r[0] > 0 else None


def open_entry_price(account: str | None, key: str) -> float | None:
    """Quantity-weighted entry price of all open trades (every book: manual/agent and nodes) of one instrument
    — the price at which the currency balances of a USD/XXX position were exchanged"""
    con = connect()
    ac, aa = _acct_clause(account)
    r = con.execute("SELECT SUM(qty * entry_price) / SUM(qty) FROM trades WHERE open = 1 AND key = ? AND entry_price > 0" + ac,
                    [key, *aa]).fetchone()
    con.close()
    return float(r[0]) if r and r[0] and r[0] > 0 else None


def insert_equity(netliq: float, unrealized: float, realized: float, account: str = "") -> None:
    ts = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
    con = connect()
    con.execute("INSERT OR REPLACE INTO equity (ts, netliq, unrealized, realized, account) VALUES (?,?,?,?,?)", (ts, netliq, unrealized, realized, account or ""))
    con.commit()
    con.close()


def netliq_before(account: str, ts: str, not_older_than: str | None = None) -> float | None:
    """Last recorded NetLiq of an account before ts (ISO, UTC) — the daily-loss baseline at the trading-day boundary
    not_older_than: ignore rows recorded before this time (the API was offline → no trustworthy baseline)"""
    con = connect()
    q, args = "SELECT netliq FROM equity WHERE account = ? AND ts < ? AND netliq > 0", [account or "", ts]
    if not_older_than:
        q += " AND ts >= ?"
        args.append(not_older_than)
    r = con.execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()
    con.close()
    return float(r[0]) if r else None


# ---------- trade pairing (per instrument and book, flat → flat) ----------
_fx_rates: dict[str, float] = {}


def set_fx_rates(rates: dict[str, float] | None) -> None:
    """USD value of one unit per currency (qx_strategies.fx format, e.g. {"GBP": 1.30}) — needed to convert P&L of
    cross pairs (EUR/GBP, EUR/JPY) to USD; XXX/USD and USD/XXX pairs convert with the fill price itself"""
    _fx_rates.clear()
    _fx_rates.update({k: float(v) for k, v in (rates or {}).items() if v})
    if _fx_rates and DB_FILE.exists():
        _fill_pending_rates()


def _fill_pending_rates() -> int:
    """Give cross-pair fills that were recorded without a rate the current rate once (frozen afterwards) · returns rows filled"""
    con = connect()
    n = 0
    try:
        for r in con.execute("SELECT p.exec_id, f.key FROM fx_rate_pending p LEFT JOIN fills f ON f.exec_id = p.exec_id").fetchall():
            if r["key"] is None:
                con.execute("DELETE FROM fx_rate_pending WHERE exec_id = ?", (r["exec_id"],))
                continue
            rate = _fill_usd_rate(r["key"])
            if rate:
                con.execute("UPDATE fills SET usd_rate = ? WHERE exec_id = ? AND usd_rate IS NULL", (rate, r["exec_id"]))
                con.execute("DELETE FROM fx_rate_pending WHERE exec_id = ?", (r["exec_id"],))
                n += 1
        con.commit()
    finally:
        con.close()
    if n:
        rebuild_trades()
    return n


def _is_adjustment(f: dict) -> bool:
    return f.get("source") == "adjustment" or str(f.get("exec_id") or "").startswith("adj:")


def _pnl_usd(key: str, amount_quote: float, price: float, usd_rate: float | None = None) -> float | None:
    """Realized P&L in the instrument's quote currency → USD (stocks / crypto are quoted in USD)
    usd_rate: quote-currency rate stored with the closing fill (cross pairs) — preferred over today's rates"""
    if not key.startswith("FX:"):
        return amount_quote
    if usd_rate and usd_rate > 0 and "USD" not in fx.split_pair(key):
        return amount_quote * usd_rate
    return fx.pnl_to_usd(key, amount_quote, price, _fx_rates)


def pair_fills(fs: list[dict], key: str, account: str = "") -> list[dict]:
    """Fills of one instrument + book (sorted by time) → round-trip trades with the average-cost method
    · a fill that crosses zero (long → short) is split: the closing part ends the trade, the rest opens a new one
    · realized P&L is computed from fill prices (quote currency → USD, cross pairs with the rate stored at the closing fill);
      IBKR's realizedPNL is only a fallback when a cross pair cannot be converted (no rate) — without it (node fills) the
      trade's P&L is unknown (None), never 0 · commissions of a split fill are allocated pro rata by quantity
    · adjustment rows change the quantity only: realized 0, no effect on the average cost (except when opening from flat)"""
    trades: list[dict] = []
    cur: dict | None = None
    pos, avg = 0.0, None
    for f in fs:
        qty = abs(f["qty"] or 0.0)
        if qty < 1e-12:
            continue
        signed = qty if f["side"] == "BUY" else -qty
        adj = _is_adjustment(f)
        px = f["price"] if f.get("price") and f["price"] > 0 else None
        comm, ib_real = f.get("commission") or 0.0, f.get("realized_pnl") or 0.0
        rem, part_no = signed, 0
        while abs(rem) > 1e-12:
            if cur is None:
                cur = {"key": key, "account": account, "symbol": f["symbol"], "source": f["source"], "node": f.get("node") or "",
                       "strategy": f.get("strategy") or "", "side": "LONG" if rem > 0 else "SHORT",
                       "entry_ts": f["ts"], "exit_ts": None, "qty": 0.0, "entry_cost": 0.0, "entry_qty": 0.0, "adj_entry": None,
                       "exit_cost": 0.0, "exit_qty": 0.0, "adj_exit_cost": 0.0, "adj_exit_qty": 0.0,
                       "pnl": 0.0, "commission": 0.0, "n_fills": 0, "avg": None,
                       "first_exec": f["exec_id"] if part_no == 0 else f"{f['exec_id']}:flip"}
                pos, avg = 0.0, None
            if pos == 0 or (pos > 0) == (rem > 0):          # open / add
                part = rem
                if px is not None and (not adj or avg is None or pos == 0):
                    avg = px if (avg is None or pos == 0) else (avg * abs(pos) + px * abs(part)) / (abs(pos) + abs(part))
                if adj:
                    cur["adj_entry"] = cur["adj_entry"] or px
                elif px is not None:
                    cur["entry_cost"] += abs(part) * px
                    cur["entry_qty"] += abs(part)
                cur["qty"] += abs(part)
            else:                                           # reduce / close
                closing = min(abs(rem), abs(pos))
                part = closing if rem > 0 else -closing
                if adj:
                    if px is not None:
                        cur["adj_exit_cost"] += closing * px
                        cur["adj_exit_qty"] += closing
                else:
                    if px is not None:
                        cur["exit_cost"] += closing * px
                        cur["exit_qty"] += closing
                    usd = None
                    if px is not None and avg is not None:
                        usd = _pnl_usd(key, (px - avg) * closing * (1 if pos > 0 else -1), px, f.get("usd_rate"))
                    if usd is None and px is not None and avg is not None and not ib_real:
                        cur["pnl_unknown"] = True                # cross pair without a rate and no IBKR figure
                    cur["pnl"] += usd if usd is not None else ib_real * closing / qty
            share = abs(part) / qty
            cur["commission"] += comm * share
            if part_no == 0 or cur["n_fills"] == 0:
                cur["n_fills"] += 1
            pos += part
            rem -= part
            part_no += 1
            cur["avg"] = avg
            if abs(pos) < 1e-9:
                cur["exit_ts"] = f["ts"]
                trades.append(_finish(cur, open_=False))
                cur, pos, avg = None, 0.0, None
    if cur is not None:
        trades.append(_finish(cur, open_=True))
    return trades


def rebuild_trades() -> None:
    con = connect()
    fills = [dict(r) for r in con.execute("SELECT * FROM fills ORDER BY ts, exec_id")]
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    for f in fills:
        f["source"] = f.get("source") or attr.classify(f.get("order_ref"), f.get("node") or "")
        by_key.setdefault((f.get("account") or "", f["key"], attr.book_of(f["source"])), []).append(f)
    trades = []
    for (account, key, _book), fs in by_key.items():
        trades += pair_fills(fs, key, account)
    con.execute("DELETE FROM trades")
    con.executemany(
        """INSERT OR REPLACE INTO trades (key,account,symbol,source,node,strategy,side,entry_ts,exit_ts,qty,entry_price,exit_price,pnl,commission,net_pnl,holding_sec,n_fills,first_exec,open)
           VALUES (:key,:account,:symbol,:source,:node,:strategy,:side,:entry_ts,:exit_ts,:qty,:entry_price,:exit_price,:pnl,:commission,:net_pnl,:holding_sec,:n_fills,:first_exec,:open)""",
        trades,
    )
    con.commit()
    con.close()


def _finish(t: dict, open_: bool) -> dict:
    if t["entry_qty"]:
        entry_price = t["entry_cost"] / t["entry_qty"]
    else:
        entry_price = t["adj_entry"] or t.get("avg")
    if t["exit_qty"]:
        exit_price = t["exit_cost"] / t["exit_qty"]
    elif t["adj_exit_qty"]:
        exit_price = t["adj_exit_cost"] / t["adj_exit_qty"]
    else:
        exit_price = None
    unknown = bool(t.get("pnl_unknown"))           # P&L could not be converted to USD: null, never a wrong 0
    hold = None
    if t["exit_ts"]:
        hold = (datetime.fromisoformat(t["exit_ts"]) - datetime.fromisoformat(t["entry_ts"])).total_seconds()
    return {
        "key": t["key"], "account": t.get("account", ""), "symbol": t["symbol"], "source": t["source"],
        "node": t.get("node", ""), "strategy": t.get("strategy", ""), "side": t["side"],
        "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"], "qty": t["qty"], "entry_price": entry_price, "exit_price": exit_price,
        # rounded to 1e-8: price differences like 1.101 − 1.10 are not exact in binary floating point
        "pnl": None if unknown else round(t["pnl"], 8), "commission": round(t["commission"], 8),
        "net_pnl": None if unknown else round(t["pnl"] - t["commission"], 8), "holding_sec": hold,
        "n_fills": t["n_fills"], "first_exec": t["first_exec"], "open": 1 if open_ else 0,
    }


# ---------- queries ----------
def _since(range_: str, tz: str | None = None) -> str | None:
    """Start of the range as a UTC ISO string · tz = IANA zone of the viewer (e.g. Asia/Bangkok) so 'today' starts at local midnight"""
    now = datetime.now(timezone.utc)
    if range_ == "today":
        try:
            zone = ZoneInfo(tz) if tz else timezone.utc
        except Exception:
            zone = timezone.utc
        local = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        return local.astimezone(timezone.utc).isoformat()
    if range_ == "week":
        return (now - timedelta(days=7)).isoformat()
    if range_ == "month":
        return (now - timedelta(days=30)).isoformat()
    return None


def _source_clause(source: str, col: str = "t.source") -> tuple[str, list]:
    """'node:a' matches node:a only (not node:ab) · 'manual' also matches manual:reverse / manual:close_all · 'agent' matches agent:<id>"""
    return f" AND ({col} = ? OR {col} LIKE ?)", [source, source.rstrip(":") + ":%"]


def list_trades(range_: str = "all", key: str | None = None, source: str | None = None, limit: int = 500, account: str | None = None,
                tz: str | None = None) -> list[dict]:
    """Trades in the range: closed trades by exit time (when the P&L was realized), open trades always"""
    con = connect()
    q = "SELECT t.*, (t.pnl IS NULL) AS pnl_unknown, COALESCE(j.note,'') AS note, COALESCE(j.tags,'') AS tags, COALESCE(j.rating,0) AS rating FROM trades t LEFT JOIN journal j ON j.first_exec = t.first_exec WHERE 1=1"
    args: list = []
    ac, aa = _acct_clause(account, "t.account"); q += ac; args += aa
    since = _since(range_, tz)
    if since:
        q += " AND (t.open = 1 OR COALESCE(t.exit_ts, t.entry_ts) >= ?)"; args.append(since)
    if key:
        q += " AND t.key = ?"; args.append(key)
    if source:
        sc, sa = _source_clause(source); q += sc; args += sa
    q += " ORDER BY t.entry_ts DESC LIMIT ?"; args.append(limit)
    rows = [dict(r) for r in con.execute(q, args)]
    con.close()
    return rows


def _combine_accounts(rows: list[dict]) -> list[dict]:
    """Equity rows of several accounts → one total series over the WHOLE range.
    The recorder writes the ACTIVE account only, so accounts' series cover different periods. At each timestamp every account
    contributes its latest value; before its first row it contributes its first value, after its last row its last value
    (flat extension). That avoids a fake jump when an account appears / disappears and never truncates history.
    Points where any account is extended (outside its own recorded range) carry partial = True."""
    first: dict[str, dict] = {}
    last_ts: dict[str, str] = {}
    for r in rows:
        a = r.get("account") or ""
        first.setdefault(a, r)
        last_ts[a] = r["ts"]
    cur: dict[str, dict] = dict(first)
    out: list[dict] = []
    for r in rows:
        a = r.get("account") or ""
        cur[a] = r
        ts = r["ts"]
        tot = {"ts": ts, "account": "",
               "netliq": sum(x["netliq"] or 0.0 for x in cur.values()),
               "unrealized": sum(x["unrealized"] or 0.0 for x in cur.values()),
               "realized": sum(x["realized"] or 0.0 for x in cur.values()),
               "partial": any(ts < first[k]["ts"] or ts > last_ts[k] for k in first)}
        if out and out[-1]["ts"] == ts:
            out[-1] = tot
        else:
            out.append(tot)
    return out


def list_equity(range_: str = "all", max_points: int = 2000, account: str | None = None, tz: str | None = None,
                combine: bool = True) -> list[dict]:
    """account '' / None = all accounts — combined into one total series unless combine=False (raw rows, e.g. for export)"""
    con = connect()
    since = _since(range_, tz)
    q = "SELECT * FROM equity WHERE 1=1"; args: list = []
    if since:
        q += " AND ts >= ?"; args.append(since)
    ac, aa = _acct_clause(account); q += ac; args += aa
    rows = [dict(r) for r in con.execute(q + " ORDER BY ts", args)]
    con.close()
    if not account and combine and len({r.get("account") or "" for r in rows}) > 1:
        rows = _combine_accounts(rows)
    if len(rows) > max_points:
        step = math.ceil(len(rows) / max_points)
        last = rows[-1]
        rows = rows[::step]
        if rows[-1] is not last:
            rows[-1] = last              # always keep the latest point (keeps the length ≤ max_points)
    return rows


def set_journal(first_exec: str, note: str, tags: str, rating: int) -> None:
    con = connect()
    con.execute("INSERT OR REPLACE INTO journal VALUES (?,?,?,?)", (first_exec, note, tags, int(rating)))
    con.commit()
    con.close()


def _trade_curve(trades: list[dict], base: float) -> list[dict]:
    """Equity derived from cumulative P&L of closed trades (used when filtering by source — account equity covers all sources)"""
    out, acc = [], base
    for t in sorted(trades, key=lambda t: t["exit_ts"] or t["entry_ts"]):
        if t["net_pnl"] is None:
            continue
        acc += t["net_pnl"]
        out.append({"ts": t["exit_ts"] or t["entry_ts"], "netliq": acc})
    return out


def stats(range_: str = "all", account: str | None = None, source: str | None = None, tz: str | None = None) -> dict:
    all_trades = list_trades(range_, source=source or None, limit=100000, account=account, tz=tz)
    closed = [t for t in all_trades if not t["open"]]
    trades = [t for t in closed if t["net_pnl"] is not None]          # trades with unknown USD P&L are counted separately
    eq = list_equity(range_, max_points=100000, account=account, tz=tz)
    if source:
        eq = _trade_curve(trades, eq[0]["netliq"] if eq and eq[0]["netliq"] else 100_000.0)
    n = len(trades)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    breakeven = n - len(wins) - len(losses)
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    net = sum(t["net_pnl"] for t in trades)

    # drawdown from equity
    peak, max_dd, max_dd_pct, dd_start, dd_peak_ts = -1e18, 0.0, 0.0, None, None
    for r in eq:
        v = r["netliq"] or 0.0
        if v > peak:
            peak, dd_peak_ts = v, r["ts"]
        dd = peak - v
        if dd > max_dd:
            max_dd, max_dd_pct, dd_start = dd, (dd / peak * 100 if peak > 0 else 0.0), dd_peak_ts
    # daily sharpe from equity
    daily: dict[str, float] = {}
    for r in eq:
        daily[r["ts"][:10]] = r["netliq"] or 0.0
    vals = list(daily.values())
    rets = [(b - a) / a for a, b in zip(vals, vals[1:]) if a]
    sharpe = 0.0
    if len(rets) > 1:
        m = sum(rets) / len(rets)
        sd = (sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
        sharpe = (m / sd * math.sqrt(252)) if sd > 0 else 0.0

    def group(fn):
        g: dict[str, dict] = {}
        for t in trades:
            k = fn(t)
            d = g.setdefault(k, {"name": k, "trades": 0, "wins": 0, "net_pnl": 0.0})
            d["trades"] += 1; d["wins"] += 1 if t["net_pnl"] > 0 else 0; d["net_pnl"] += t["net_pnl"]
        out = list(g.values())
        for d in out:
            d["win_rate"] = d["wins"] / d["trades"] * 100 if d["trades"] else 0.0
        return sorted(out, key=lambda d: -d["net_pnl"])

    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_hour: dict[int, dict] = {}
    for t in trades:
        h = datetime.fromisoformat(t["entry_ts"]).astimezone(timezone.utc).hour
        d = by_hour.setdefault(h, {"hour": h, "trades": 0, "net_pnl": 0.0})
        d["trades"] += 1; d["net_pnl"] += t["net_pnl"]

    return {
        "range": range_, "trades": n, "wins": len(wins), "losses": len(losses), "breakeven": breakeven,
        "win_rate": len(wins) / n * 100 if n else 0.0,
        "net_pnl": net, "gross_win": gross_win, "gross_loss": gross_loss,
        # avg_loss is negative (same sign convention as perf.perf_block and backtest reports)
        "avg_win": gross_win / len(wins) if wins else 0.0, "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        "expectancy": net / n if n else 0.0,
        "avg_holding_sec": sum(t["holding_sec"] or 0 for t in trades) / n if n else 0.0,
        "commission": sum(t["commission"] for t in trades),
        "max_drawdown": max_dd, "max_drawdown_pct": max_dd_pct, "sharpe": sharpe,
        "equity_start": eq[0]["netliq"] if eq else 0.0, "equity_end": eq[-1]["netliq"] if eq else 0.0,
        "equity_partial": any(r.get("partial") for r in eq),
        "by_symbol": group(lambda t: t["symbol"]), "by_source": group(lambda t: t["source"]),
        "by_weekday": group(lambda t: weekdays[datetime.fromisoformat(t["entry_ts"]).weekday()]),
        "by_hour": [by_hour[h] for h in sorted(by_hour)],
        "open_trades": sum(1 for t in all_trades if t["open"]), "unknown_pnl_trades": len(closed) - n,
        "account": account or "", "source": source or "",
    }


def export_csv(what: str, range_: str = "all", account: str | None = None, tz: str | None = None) -> str:
    buf = io.StringIO()
    if what == "equity":
        rows = list_equity(range_, max_points=10**7, account=account, tz=tz, combine=False)
        cols = ["ts", "account", "netliq", "unrealized", "realized"]
    elif what == "fills":
        ac, aa = _acct_clause(account)
        since = _since(range_, tz)
        sc, sa = (" AND ts >= ?", [since]) if since else ("", [])
        con = connect(); rows = [dict(r) for r in con.execute("SELECT * FROM fills WHERE 1=1" + ac + sc + " ORDER BY ts", aa + sa)]; con.close()
        cols = ["exec_id", "ts", "account", "key", "symbol", "side", "qty", "price", "commission", "realized_pnl", "order_id", "order_ref", "source", "node", "strategy"]
    else:
        rows = list_trades(range_, limit=10**7, account=account, tz=tz)
        cols = ["id", "account", "key", "symbol", "source", "node", "strategy", "side", "entry_ts", "exit_ts", "qty", "entry_price", "exit_price", "pnl", "commission", "net_pnl", "holding_sec", "n_fills", "open", "note", "tags", "rating"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def pnl_curves_by_source(range_: str = "all", account: str | None = None, tz: str | None = None) -> dict[str, list[dict]]:
    """Cumulative net P&L curve (realized − commission, USD) per source from closed trades, for comparing nodes / agents
    (built from trades, whose P&L is computed from fill prices — node fills carry no IBKR realizedPNL)"""
    out: dict[str, list[dict]] = {}
    acc: dict[str, float] = {}
    closed = [t for t in list_trades(range_, limit=10**7, account=account, tz=tz)
              if not t["open"] and t["exit_ts"] and t["net_pnl"] is not None]       # unknown USD P&L: left out
    for t in sorted(closed, key=lambda t: (t["exit_ts"], t["id"])):
        src = t["source"] or "manual"
        acc[src] = acc.get(src, 0.0) + (t["net_pnl"] or 0.0)
        out.setdefault(src, []).append({"t": t["exit_ts"], "v": acc[src]})
    return out
