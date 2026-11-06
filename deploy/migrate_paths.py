"""Run ON THE VPS after deploy/migrate-data.sh copied the Mac's data: make it valid for this machine.

- rewrites absolute Mac paths in JSON/YAML/TOML files (data/, configs/, strategies/) to the server layout (QOS_HOME)
- removes pid/lock files from the Mac (those processes do not exist here)
- marks every node desired=false so nothing starts trading before you have checked the gateway and started nodes yourself

    python3 deploy/migrate_paths.py --mac-trading /Users/<you>/trading --home /srv/qos/home [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

TEXT = {".json", ".yaml", ".yml", ".toml", ".txt"}


def mapping(mac_trading: str, home: str) -> list[tuple[str, str]]:
    m, h = mac_trading.rstrip("/"), home.rstrip("/")
    # longest prefix first: bot/data before bot, bot before trading
    return [(f"{m}/bot/data", f"{h}/data"), (f"{m}/bot/reports", f"{h}/reports"), (f"{m}/bot/configs", f"{h}/configs"),
            (f"{m}/bot/models", f"{h}/models"), (f"{m}/bot", "/srv/qos/engine/bot"), (f"{m}/strategies", f"{h}/strategies"),
            (f"{m}/ibgw", f"{h}/ibgw"), (m, h)]


def rewrite(text: str, pairs: list[tuple[str, str]]) -> str:
    for old, new in pairs:
        text = text.replace(old, new)
    return text


def run(mac_trading: str, home: str, dry: bool = False) -> dict:
    root = Path(home)
    pairs = mapping(mac_trading, home)
    changed, removed, nodes_off = [], [], 0
    for sub in ("data", "configs", "strategies", "ibgw"):
        base = root / sub
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.is_symlink():
                continue
            if f.suffix in (".pid", ".lock"):
                removed.append(str(f.relative_to(root)))
                if not dry:
                    f.unlink()
                continue
            if f.suffix not in TEXT or f.stat().st_size > 20_000_000:
                continue
            try:
                old = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            new = rewrite(old, pairs)
            if new != old:
                changed.append(str(f.relative_to(root)))
                if not dry:
                    f.write_text(new, encoding="utf-8")
    nodes = root / "data" / "nautilus_nodes.json"
    if nodes.is_file():
        doc = json.loads(nodes.read_text())
        items = doc.get("nodes", doc) if isinstance(doc, dict) else {}
        for v in (items.values() if isinstance(items, dict) else items):
            if isinstance(v, dict) and v.get("desired"):
                v["desired"] = False
                nodes_off += 1
        if nodes_off and not dry:
            nodes.write_text(json.dumps(doc, indent=2))
    left = []
    for sub in ("data", "configs", "strategies"):
        for f in (root / sub).rglob("*") if (root / sub).is_dir() else []:
            if f.is_file() and f.suffix in TEXT and f.stat().st_size <= 20_000_000:
                try:
                    if "/Users/" in f.read_text(encoding="utf-8"):
                        left.append(str(f.relative_to(root)))
                except (UnicodeDecodeError, OSError):
                    pass
    return {"rewritten": changed, "removed": removed, "nodes_set_not_desired": nodes_off, "still_has_mac_paths": left}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac-trading", required=True, help="the Mac's trading folder, e.g. /Users/you/trading")
    ap.add_argument("--home", default="/srv/qos/home")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    r = run(a.mac_trading, a.home, a.dry_run)
    print(f"rewritten {len(r['rewritten'])} files: {', '.join(r['rewritten'][:12])}")
    print(f"removed {len(r['removed'])} pid/lock files · {r['nodes_set_not_desired']} nodes set to stopped (start them from the Terminal)")
    if r["still_has_mac_paths"]:
        print("still contain /Users/ paths (check by hand): " + ", ".join(r["still_has_mac_paths"][:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
