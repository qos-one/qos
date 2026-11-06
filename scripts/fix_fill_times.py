"""Fix fill times in data/history.db that lag by the machine's timezone offset (before ib.TimezoneTWS = UTC was set) — run once

uv run scripts/fix_fill_times.py            preview how many rows would change
uv run scripts/fix_fill_times.py --apply    apply the fix (backs up data/history.db first)
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api import history  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--offset-hours", type=float, default=None, help="default = this machine's timezone")
a = ap.parse_args()
offset = a.offset_hours if a.offset_hours is not None else datetime.now().astimezone().utcoffset().total_seconds() / 3600
orders: dict[int, str] = {}
audit = history.DB_FILE.parent / "audit.jsonl"
if audit.exists():
    for line in audit.read_text(errors="ignore").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "order" and r.get("order_id"):
            orders.setdefault(int(r["order_id"]), r["ts"])
res = history.fix_fill_times(orders, offset, apply=a.apply)
print(json.dumps(res, indent=2))
