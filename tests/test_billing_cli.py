"""Step 40: purchasing via the Terminal (CLI / API) — the real Cloud is tested in cloud/tests/test_terminal_billing.py"""
import asyncio

import pytest

from qos.cloud import billing
from qos.cloud.client import CloudError


def test_pick_and_fmt():
    ps = [{"id": "a", "kind": "one_time", "amount_cents": 9900, "currency": "usd"},
          {"id": "b", "kind": "month", "amount_cents": 1900, "currency": "usd"}]
    assert billing.pick(ps)["id"] == "b" and billing.pick(ps, "one_time")["id"] == "a"
    assert billing.fmt(ps[1]) == "19.00 USD / month" and billing.fmt(ps[0]) == "99.00 USD"
    with pytest.raises(CloudError, match="no year price"):
        billing.pick(ps, "year")
    with pytest.raises(CloudError, match="no price"):
        billing.pick([])


def test_cli_store_buy_without_api(monkeypatch, capsys):
    from qos import cli
    opened = []
    monkeypatch.setattr(billing, "prices", lambda sku, c=None: [{"id": "p1", "kind": "month", "amount_cents": 1900, "currency": "usd"}])
    monkeypatch.setattr(billing, "checkout", lambda pid, c=None: {"url": "http://localhost:8090/pay/test/checkout/x", "purchase_id": "x"})
    monkeypatch.setattr(billing, "wait_paid", lambda pid, c=None, **k: {"status": "active", "sku": "agent.trend"})
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u) or True)
    assert cli.main(["--api", "http://127.0.0.1:9", "store", "buy", "trend"]) == 0
    out = capsys.readouterr().out
    assert "19.00 USD / month" in out and "ready to use" in out and opened


def test_api_checkout_opens_browser_and_waits(monkeypatch):
    from api import cloud_api
    opened, waited = [], []
    monkeypatch.setattr(billing, "checkout", lambda pid, c=None: {"url": "http://localhost:8090/pay/test/checkout/x", "purchase_id": "x"})
    monkeypatch.setattr(billing, "wait_paid", lambda pid, c=None: waited.append(pid) or {"status": "paid", "sku": "data.fx"})
    monkeypatch.setattr(billing, "purchase", lambda pid, c=None: {"id": pid, "status": "paid", "sku": "data.fx"})
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u) or True)
    monkeypatch.setattr(cloud_api, "_audit", lambda *a, **k: None)

    async def run():
        r = await cloud_api.billing_checkout(cloud_api.CheckoutBody(price_id="p1"))
        assert r["purchase_id"] == "x" and opened
        for _ in range(50):
            await asyncio.sleep(0.01)
            if waited:
                break
        st = await cloud_api.billing_purchase("x")
        assert st["status"] == "paid"
    asyncio.run(run())
    assert waited == ["x"]

    def taken(pid, c=None):
        raise CloudError("already_owned", "you already have data.fx")
    monkeypatch.setattr(billing, "checkout", taken)
    with pytest.raises(cloud_api.HTTPException) as e:
        asyncio.run(cloud_api.billing_checkout(cloud_api.CheckoutBody(price_id="p1")))
    assert e.value.status_code == 409
