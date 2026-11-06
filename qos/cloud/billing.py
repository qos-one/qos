"""Buy an Agent / dataset / plan through QOS Cloud (step 40)

Payment happens on the provider's web page (Stripe — or the Cloud TEST MODE page during development) · the Terminal never sees card details
After payment the Cloud creates a grant → the Terminal syncs and it is usable immediately
"""
from __future__ import annotations

import time

from .client import Cloud, CloudError, get

PAID = ("paid", "active")
DONE = ("paid", "active", "failed", "canceled", "refunded")


def prices(sku: str, c: Cloud | None = None) -> list[dict]:
    return (c or get())._authed("GET", f"/prices?sku={sku}")["prices"]


def pick(prices_: list[dict], kind: str | None = None) -> dict:
    if not prices_:
        raise CloudError("not_for_sale", "this item has no price yet")
    if kind is None:
        return prices_[0] if len(prices_) == 1 else next((p for p in prices_ if p["kind"] == "month"), prices_[0])
    for p in prices_:
        if p["kind"] == kind:
            return p
    raise CloudError("not_for_sale", f"no {kind} price — available: {', '.join(p['kind'] for p in prices_)}")


def fmt(p: dict) -> str:
    per = {"one_time": "", "month": " / month", "year": " / year"}[p["kind"]]
    return f"{p['amount_cents'] / 100:,.2f} {p['currency'].upper()}{per}"


def checkout(price_id: str, c: Cloud | None = None) -> dict:
    return (c or get())._authed("POST", "/checkout", json_body={"price_id": price_id})


def purchase(purchase_id: str, c: Cloud | None = None) -> dict:
    return (c or get())._authed("GET", f"/purchases/{purchase_id}")


def purchases(c: Cloud | None = None) -> dict:
    return (c or get())._authed("GET", "/purchases")


def portal(c: Cloud | None = None) -> str:
    return (c or get())._authed("POST", "/billing/portal")["url"]


def wait_paid(purchase_id: str, c: Cloud | None = None, timeout: float = 900, interval: float = 4, log=None) -> dict:
    """Wait for the payment result, then sync grants (and the Agent licence) as soon as payment succeeds"""
    from . import store
    c = c or get()
    end = time.time() + timeout
    st = {"status": "pending"}
    while time.time() < end:
        st = purchase(purchase_id, c)
        if st["status"] in DONE:
            break
        time.sleep(interval)
    if st["status"] in PAID:
        c.sync()
        if st["sku"].startswith("agent."):
            try:
                store.fetch_licence(st["sku"][len("agent."):], c)
            except CloudError:
                pass
    if log:
        log(f"{st.get('sku', '')}: {st['status']}")
    return st


def set_agent_price(slug: str, kind: str, amount: float, c: Cloud | None = None) -> dict:
    return (c or get())._authed("PUT", f"/agents/{slug}/price", json_body={"kind": kind, "amount_cents": round(amount * 100)})


def remove_agent_price(slug: str, kind: str, c: Cloud | None = None) -> dict:
    return (c or get())._authed("DELETE", f"/agents/{slug}/price/{kind}")


def payouts(c: Cloud | None = None) -> dict:
    return (c or get())._authed("GET", "/developer/payouts")


def onboard(c: Cloud | None = None) -> str:
    return (c or get())._authed("POST", "/developer/payouts/onboard")["url"]
