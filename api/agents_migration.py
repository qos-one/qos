"""
Cutover (step 13): move old agents (data/agents.json) → configs in configs/strategies + Nautilus nodes

- Convert agent parameters → qx_strategies config (the reverse of qx_strategies.parity.PAIRS)
- Reuse an existing config if strategy / instrument / timeframe / parameters match (size may differ — noted in note)
- A node already using that config → skip · an agent that was active → node starts immediately (unless halted)
- Old agent still holds a position (manual/agent book in history) → not migrated until closed, or force
  (the node starts flat — if not closed first, the old position is left with no one managing it)
- When done, rename data/agents.json → data/agents.migrated.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from qos import paths  # noqa: E402  (step 17: all file locations come from QOS_HOME)

ROOT = Path(__file__).resolve().parent.parent
AGENTS_FILE = paths.data_dir() / "agents.json"
DONE_FILE = paths.data_dir() / "agents.migrated.json"
CONFIG_DIR = paths.config_dir()
from .catalog import DEFAULT_SYMBOLS as _CAT_SYMS, instrument_id_str as _iid  # noqa: E402
CATALOG = tuple(_iid(s) for s in _CAT_SYMS)
TF = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
SHORT = {"ema_cross": "ema", "breakout": "breakout", "mean_reversion": "mr", "grid_dca": "grid", "ml_signal": "ml"}
PARAMS = {
    "ema_cross": lambda c: {"fast": int(c.get("fast", 9)), "slow": int(c.get("slow", 21)), "allow_short": bool(c.get("allow_short", True))},
    "breakout": lambda c: {"entry_n": int(c.get("entry_n", 20)), "exit_n": int(c.get("exit_n", 10)), "allow_short": bool(c.get("allow_short", True))},
    "mean_reversion": lambda c: {"lookback": int(c.get("lookback", 30)), "entry_z": float(c.get("entry_z", 2.0)),
                                 "exit_z": float(c.get("exit_z", 0.3)), "allow_short": True},
    "grid_dca": lambda c: {"step_pct": float(c.get("step_pct", 0.15)), "max_levels": int(c.get("max_levels", 5)),
                           "anchor_price": 0, "allow_short": False},
    "ml_signal": lambda c: {"model_file": c.get("model_file", "eurusd_5m.joblib"), "p_long": float(c.get("p_long", 0.58)),
                            "p_short": float(c.get("p_short", 0.42)), "window": 500, "allow_short": bool(c.get("allow_short", True))},
}
IGNORE = {"strategy_id", "trade_size"}


def load_agents() -> dict:
    try:
        return json.loads(AGENTS_FILE.read_text())
    except Exception:
        return {}


def pending() -> list[str]:
    return sorted(load_agents())


def to_config(agent_id: str, a: dict) -> tuple[dict | None, str]:
    """agent config → qx config · returns (config, reason if it cannot be converted)"""
    if agent_id not in PARAMS:
        return None, "no Nautilus port of this agent"
    c = a.get("config") or {}
    mkt, _, sym = str(c.get("key", "")).partition(":")
    if mkt != "FX" or len(sym) != 6:
        return None, f"{c.get('key')}: only FX pairs run on Nautilus nodes"
    tf = TF.get(str(c.get("timeframe", "")))
    if not tf:
        return None, f"timeframe {c.get('timeframe')} is not supported (use {', '.join(TF)})"
    size = float(c.get("unit_qty" if agent_id == "grid_dca" else "qty") or 0)
    cfg = {"strategy": agent_id, "instrument": f"{sym[:3]}/{sym[3:]}.IDEALPRO", "tf_minutes": tf,
           **PARAMS[agent_id](c), "trade_size": str(int(size)), "sl_pips": 0, "tp_pips": 0}
    if agent_id == "grid_dca":
        cfg.pop("sl_pips"), cfg.pop("tp_pips")
    return cfg, ""


def _same(a: dict, b: dict) -> bool:
    keys = (set(a) | set(b)) - IGNORE
    return all(str(a.get(k)) == str(b.get(k)) for k in keys if not (k in ("sl_pips", "tp_pips") and not a.get(k) and not b.get(k)))


def _existing_configs() -> dict[str, dict]:
    out = {}
    for f in sorted(CONFIG_DIR.glob("*.json")):
        try:
            out[f.name] = json.loads(f.read_text())
        except Exception:
            pass
    return out


def plan(nm, books: dict[str, dict[str, float]] | None = None, open_sources: dict[str, str] | None = None,
         ib_positions: dict[str, float] | None = None) -> dict:
    """ib_positions (actual positions at IBKR) decide leftover positions — fill history may be incomplete, so it is only supporting data"""
    books, open_sources = books or {}, open_sources or {}
    agents = load_agents()
    configs = _existing_configs()
    used = {n.get("config_file"): nid for nid, n in nm.nodes.items()}
    taken_ids = set(nm.nodes)
    items, skipped = [], []
    for aid in sorted(agents):
        a = agents[aid]
        cfg, why = to_config(aid, a)
        if cfg is None:
            skipped.append({"agent": aid, "reason": why})
            continue
        notes = []
        file = next((f for f, c in configs.items() if _same(c, cfg)), None)
        if file:
            if str(configs[file].get("trade_size")) != cfg["trade_size"]:
                notes.append(f"uses existing {file} (trade size {configs[file].get('trade_size')}, agent had {cfg['trade_size']})")
            if file in used:
                skipped.append({"agent": aid, "reason": f"already running as node '{used[file]}' ({file})"})
                continue
        else:
            sym = cfg["instrument"].split(".")[0].replace("/", "").lower()
            file = f"{aid}_{sym}_{cfg['tf_minutes']}m.json"
            if file in configs:
                file = f"{aid}_{sym}_{cfg['tf_minutes']}m_agent.json"
            if float(cfg["trade_size"]) < 1000:
                skipped.append({"agent": aid, "reason": f"trade size {cfg['trade_size']} is below 1,000 units — set a size in the Strategies tab"})
                continue
        if cfg["instrument"] not in CATALOG:
            notes.append(f"{cfg['instrument']} is not in the data catalog — backtests and soak parity need it added first")
        key = "FX:" + cfg["instrument"].split(".")[0].replace("/", "")
        bk = books.get(key) or {}
        legacy_qty = float(bk.get("", 0.0))
        align_to = None
        if ib_positions is not None:
            residual = float(ib_positions.get(key, 0.0)) - sum(v for b, v in bk.items() if b.startswith("node:"))
            open_pos = residual if abs(residual) > 0.5 else 0.0
            if abs(legacy_qty - residual) > 0.5:
                notes.append(f"fill history shows {legacy_qty:+,.0f} for manual/agent but IBKR has {residual:+,.0f} — history will be aligned to IBKR")
                align_to = residual
        else:
            src = open_sources.get(key, "")
            open_pos = legacy_qty if abs(legacy_qty) > 1e-6 and src == f"agent:{aid}" else 0.0
        nid = f"{SHORT[aid]}_{key[3:].lower()}"
        if nid in taken_ids:
            nid = f"{nid}_{cfg['tf_minutes']}m"
        n = 2
        while nid in taken_ids:
            nid = f"{SHORT[aid]}_{key[3:].lower()}_{n}"
            n += 1
        taken_ids.add(nid)
        items.append({"agent": aid, "node": nid, "name": f"{aid.replace('_', ' ').title()} {key[3:]}", "config_file": file,
                      "new_file": file not in configs, "config": cfg if file not in configs else configs[file],
                      "start": bool(a.get("active")), "open_legacy_position": open_pos, "align_history_to": align_to, "notes": notes,
                      "dropped_risk": {k: v for k, v in (a.get("risk") or {}).items() if v}})
    blockers = []
    for i in items:
        if i["open_legacy_position"]:
            pair = i["config"]["instrument"].split(".")[0]
            msg = (f"IBKR still holds {i['open_legacy_position']:+,.0f} {pair} outside the nodes ({i['agent']} / manual) — close it first or use force"
                   if ib_positions is not None else f"{i['agent']} still holds {i['open_legacy_position']:+,.0f} {pair} — close it first or use force")
            if msg not in blockers:
                blockers.append(msg)
    return {"agents_file": str(AGENTS_FILE.name), "found": len(agents), "migrate": items, "skipped": skipped, "blockers": blockers,
            "note": "per-agent risk limits are replaced by the shared account risk limits (Risk tab) that every node obeys"}


def apply(nm, p: dict, force: bool = False, start: bool = True, align=None) -> dict:
    """align(key, target_qty): adjust the manual/agent book in history to match IBKR (if the plan finds history incomplete)"""
    if p["blockers"] and not force:
        raise ValueError("; ".join(p["blockers"]))
    from qx_strategies import REGISTRY
    created, started, errors = [], [], []
    for it in p["migrate"]:
        try:
            if it["new_file"]:
                cfg = dict(it["config"])
                cfg["strategy_id"] = REGISTRY[cfg["strategy"]][0].__name__ + "-" + Path(it["config_file"]).stem.upper().replace("_", "-")[:30]
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                (CONFIG_DIR / it["config_file"]).write_text(json.dumps(cfg, indent=2) + "\n")
            nm.create(it["name"], it["config_file"], {}, True, it["node"])
            created.append({"agent": it["agent"], "node": it["node"], "config_file": it["config_file"], "new_file": it["new_file"]})
            if start and it["start"]:
                nm.start(it["node"])
                started.append(it["node"])
        except Exception as e:
            errors.append({"agent": it["agent"], "error": str(e)[:200]})
    aligned = {}
    if align is not None:
        for it in p["migrate"]:
            key = "FX:" + it["config"]["instrument"].split(".")[0].replace("/", "")
            if it.get("align_history_to") is not None and key not in aligned:
                try:
                    aligned[key] = align(key, it["align_history_to"])
                except Exception as e:
                    errors.append({"agent": it["agent"], "error": f"history alignment failed: {e}"[:200]})
    if not errors and AGENTS_FILE.exists():
        doc = {"migrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "created": created,
               "skipped": p["skipped"], "agents": load_agents()}
        DONE_FILE.write_text(json.dumps(doc, indent=2))
        AGENTS_FILE.write_text("{}\n")
    return {"created": created, "started": started, "skipped": p["skipped"], "errors": errors, "history_aligned": aligned,
            "archived_to": DONE_FILE.name if not errors else None}
