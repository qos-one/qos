"""Step 37: agent forward paper on real bars (qos.forward_check) + qos feed push"""
import shutil
import os
from pathlib import Path

import numpy as np
import pandas as pd

from qos.forward_check import run

EXAMPLES = Path(os.environ.get("QOS_EXAMPLES") or Path(__file__).resolve().parents[2] / "qos-examples")


def _csv(tmp_path, sym="GBPUSD", days=10) -> Path:
    d = tmp_path / "bars"
    d.mkdir()
    end = pd.Timestamp("2026-10-28 08:00", tz="UTC")
    idx = pd.date_range(end=end, periods=days * 1440, freq="1min")
    rng = np.random.default_rng(3)
    c = 1.27 + np.cumsum(rng.normal(0, 0.0002, len(idx)))
    pd.DataFrame({"ts": idx.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": c, "high": c + 0.0002, "low": c - 0.0002, "close": c}).to_csv(d / f"{sym}.csv", index=False)
    return d


def _pkg(tmp_path) -> Path:
    p = tmp_path / "pkg"
    if p.exists():
        return p
    shutil.copytree(EXAMPLES / "rsi_reversion", p, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    return p


def test_forward_counts_only_after_start(tmp_path, monkeypatch):
    for k in ("QOS_STRATEGIES_DIR", "QOS_REPORTS_DIR"):
        monkeypatch.setenv(k, "")
    bars = _csv(tmp_path)
    r = run(_pkg(tmp_path), bars, "2026-10-23T08:00:00Z")
    assert r["ok"] and r["instrument"] == "GBP/USD.IDEALPRO" and 4.9 < r["days"] < 5.1, r
    assert r["trades"] > 0 and all(pd.Timestamp(t["opened"]) >= pd.Timestamp("2026-10-23T08:00:00Z") for t in r["recent_trades"])
    assert r["equity"][0][1] == 100_000 and abs(r["equity"][-1][1] - 100_000 - r["pnl_usd"]) < 0.01
    late = run(_pkg(tmp_path), bars, "2026-10-28T09:00:00Z")
    assert late["ok"] and late["waiting"]


def test_forward_waits_without_bars(tmp_path):
    (tmp_path / "bars").mkdir()
    r = run(_pkg(tmp_path), tmp_path / "bars", "2026-10-23T08:00:00Z")
    assert r["ok"] and r["waiting"] and "no live bars" in r["error"]


def test_feed_push_sends_new_bars_only(tmp_path, monkeypatch):
    from qos.cloud import feed
    from qos.sdk import catalog
    cat_dir = tmp_path / "cat"
    cat = catalog.open_catalog(cat_dir)
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("min"), periods=120, freq="1min")
    df = pd.DataFrame({"open": 1.1, "high": 1.1002, "low": 1.0998, "close": 1.1001}, index=idx)
    cat.write_data([catalog.make_fx_instrument("EURUSD")])
    cat.write_data(catalog.frame_to_bars(df, "EUR/USD", "1m"))
    sent = []

    class C:
        def _authed(self, method, path, json_body=None, **kw):
            if path == "/admin/feed/status":
                return {"instruments": [{"instrument": "EURUSD", "last": idx[59].isoformat()}]}
            sent.append(json_body)
            return {"inserted": len(json_body["bars"])}
    assert feed.local_instruments(cat_dir) == ["EURUSD"]
    out = feed.push(C(), catalog_dir=cat_dir)
    assert 60 <= out["EURUSD"] <= 61 and sent[0]["instrument"] == "EURUSD" and len(sent[0]["bars"][0]) == 5
