"""
Build/update the Nautilus data catalog from IBKR (FX 1m + 5m)

  First run (1 year back, takes about 10–30 minutes):  uv run scripts/build_catalog.py
  Update from the latest data (launchd, nightly):      uv run scripts/build_catalog.py --update
  Repair missing ranges (e.g. IBKR timeout):           uv run scripts/build_catalog.py --repair
  Choose symbols/range:                                uv run scripts/build_catalog.py --symbols EURUSD --days 30

The port follows the gateway selected in the Terminal (like a Nautilus node) — override with --port
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api import catalog as cat  # noqa: E402
from api.nautilus_bridge import resolve_gateway  # noqa: E402

LOG = cat.paths.logs_dir() / "catalog.log"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(cat.DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--update", action="store_true", help="continue from the latest time in the catalog")
    ap.add_argument("--repair", action="store_true", help="re-fetch only the gaps in existing data")
    ap.add_argument("--port", type=int, default=0, help="0 = use the selected gateway")
    ap.add_argument("--client-id", type=int, default=cat.CATALOG_CLIENT_ID)
    a = ap.parse_args()
    target = resolve_gateway()
    port = a.port or target["port"]
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    log(f"catalog {'repair' if a.repair else 'update' if a.update else 'backfill'} · {syms} · port {port} ({target['gateway_id'] or 'env'}) · {cat.CATALOG_DIR}")
    summary = asyncio.run(cat.build(syms, a.days, a.update, a.repair, target["host"], port, a.client_id, log=log))
    log("done " + json.dumps({s: {k: v[k] for k in ("bars_1m_written", "bars_5m_written", "first", "last", "gaps_over_15m")}
                              for s, v in summary["symbols"].items()}))


if __name__ == "__main__":
    main()
