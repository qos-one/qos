"""Step 38: agents from the QOS Store — checked before the node starts, node stopped when the Cloud withdraws the version (download E2E is in cloud/tests)"""
import hashlib
import json
import shutil
import os
from pathlib import Path

from qos.cloud import store
from test_cloud_connector import FakeCloud, signed_in

EXAMPLES = Path(os.environ.get("QOS_EXAMPLES") or Path(__file__).resolve().parents[2] / "qos-examples")


def _installed(fake, c, base: Path, sku=None) -> Path:
    dest = base / "rsi_reversion@1.0.0"
    shutil.copytree(EXAMPLES / "rsi_reversion", dest, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    files = [{"path": p.relative_to(dest).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "size": p.stat().st_size}
             for p in sorted(dest.rglob("*")) if p.is_file()]
    tok = fake.sign({"schema": "qos-manifest/1", "name": "rsi_reversion", "version": "1.0.0", "kind": "agent", "files": files}, "qos-man+jwt")
    (dest / "QOS-MANIFEST.jws").write_text(tok)
    (dest / "cloud.json").write_text(json.dumps({"source": "qos-cloud", "slug": "rsi_reversion", "version": "1.0.0", "sku": sku}))
    (dest / "install.json").write_text("{}")
    return dest


def test_check_installed_rules(tmp_path):
    fake = FakeCloud()
    c = signed_in(fake, tmp_path)
    base = tmp_path / "strategies"
    dest = _installed(fake, c, base)
    assert store.check_installed(dest, c) == (True, "")
    assert store.installed(base)["rsi_reversion"][0]["ref"] == "rsi_reversion@1.0.0"
    (dest / "__pycache__").mkdir()
    (dest / "__pycache__" / "x.pyc").write_bytes(b"x")
    assert store.check_installed(dest, c)[0]
    other = FakeCloud()
    (dest / "QOS-MANIFEST.jws").write_text(other.sign({"schema": "qos-manifest/1", "name": "rsi_reversion", "version": "1.0.0",
                                                       "kind": "agent", "files": []}, "qos-man+jwt"))
    assert "signature" in store.check_installed(dest, c)[1]
    shutil.rmtree(dest)
    dest = _installed(fake, c, base, sku="agent.rsi_reversion")
    ok, why = store.check_installed(dest, c)
    assert not ok and "needs agent.rsi_reversion" in why
    fake.grants.append({"sku": "agent.rsi_reversion", "kind": "agent", "exp": None})
    c.sync()
    assert store.check_installed(dest, c)[0]
    fake.items = [{"seq": 1, "kind": "agent", "ref": "rsi_reversion@1.0.0", "reason": "bug"}]
    c.sync()
    assert "withdrawn" in store.check_installed(dest, c)[1]


def test_node_start_and_revocation_enforcement(tmp_path, monkeypatch):
    from api import cloud_api, server
    from qos.cloud import client as cc
    fake = FakeCloud()
    c = signed_in(fake, tmp_path)
    monkeypatch.setattr(cc, "_default", c)
    base = tmp_path / "strategies"
    _installed(fake, c, base)
    monkeypatch.setenv("QOS_STRATEGIES_DIR", str(base))
    from qx_strategies.registry import REG
    REG.scan(force=True)
    assert cloud_api.cloud_package_check("rsi_reversion@1.0.0") == (True, "")
    assert cloud_api.cloud_package_check("ema_cross@builtin") == (True, "")
    assert cloud_api.cloud_package_check(None) == (True, "")

    stopped, sent = [], []
    monkeypatch.setattr(server.nm, "nodes", {"n1": {"desired": True}, "n2": {"desired": True}})
    monkeypatch.setattr(server.nm, "_running", lambda nid: True)
    monkeypatch.setattr(server.nm, "stop", lambda nid, reason="": stopped.append((nid, reason)))
    monkeypatch.setattr(server, "_node_ref", lambda nid: {"n1": "rsi_reversion@1.0.0", "n2": "ema_cross@builtin"}[nid])
    monkeypatch.setattr(server.notify, "fire", lambda msg, kind="": sent.append(msg))
    monkeypatch.setattr(cloud_api, "_audit", lambda *a, **k: None)
    assert cloud_api.enforce_revocations() == []
    fake.items = [{"seq": 1, "kind": "agent", "ref": "rsi_reversion@1.0.0", "reason": "bug"}]
    c.sync()
    assert cloud_api.enforce_revocations() == ["n1"] and stopped[0][0] == "n1" and "check IBKR" in sent[0]
    ok, why = cloud_api.cloud_package_check("rsi_reversion@1.0.0")
    assert not ok and "withdrawn" in why
    monkeypatch.setattr(server, "load_risk", lambda account=None: {})
    ok, why = server._node_can_start("n1")
    assert not ok and "withdrawn" in why
    REG.scan(force=True)


def test_cli_store_list(tmp_path, monkeypatch, capsys):
    from qos import cli
    monkeypatch.setattr(store, "overview", lambda c=None, base=None: {"error": "", "agents": [
        {"slug": "rsi_reversion", "version": "1.1.0", "title": "RSI Reversion", "installed": ["1.0.0"], "update": True, "revoked": False,
         "evaluation": {"backtest": {"days": 90, "pnl_usd": 408, "trades": 242, "max_drawdown_pct": 0.3}},
         "forward": {"stats": {"days": 15.0, "trades": 22, "pnl_usd": 120, "max_drawdown_pct": 0.4}}}]})
    assert cli.main(["--api", "http://127.0.0.1:9", "store", "list"]) == 0
    out = capsys.readouterr().out
    assert "rsi_reversion" in out and "installed 1.0.0" in out and "update available" in out and "forward 15.0d · 22 trades" in out
