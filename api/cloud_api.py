"""Step 34: QOS Cloud connector endpoints — sign-in (device flow), sync, devices.
The Cloud never blocks local trading · Cloud calls run in a thread (blocking urllib)"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from qos.cloud import client as cloud_mod
from qos.cloud.client import CloudError, NotSignedIn

from . import operators as ops

router = APIRouter(prefix="/cloud", tags=["cloud"])
_login: dict = {}
_task: asyncio.Task | None = None


def cloud() -> cloud_mod.Cloud:
    return cloud_mod.get()


def _admin_only(what: str) -> None:
    op = ops.current.get()
    if ops.list_ops() and (op or {}).get("role") != "admin":
        raise HTTPException(403, f"only an admin may {what}")


def _audit(event: str, **kw) -> None:
    try:
        from .server import audit
        audit(event, **kw)
    except Exception:
        pass


def _err(e: CloudError) -> HTTPException:
    code = 401 if isinstance(e, NotSignedIn) else 503 if e.code == "offline" else 400
    return HTTPException(code, e.message)


def _login_view() -> dict | None:
    if not _login:
        return None
    v = {k: _login.get(k) for k in ("state", "user_code", "verification_uri", "verification_uri_complete", "error",
                                     "message", "expires_at", "started")}
    if v["state"] in ("pending", "slow_down") and time.time() > (_login.get("expires_at") or 0):
        v["state"] = "expired"
    return v


@router.get("/status")
async def cloud_status():
    s = await asyncio.to_thread(cloud().status)
    return {**s, "login": _login_view(), "now": int(time.time())}


class LoginBody(BaseModel):
    device_name: str | None = None
    force: bool = False


async def _poll(device_code: str, interval: int, expires_at: float) -> None:
    while time.time() < expires_at and _login.get("device_code") == device_code:
        await asyncio.sleep(interval)
        try:
            r = await asyncio.to_thread(cloud().login_poll, device_code)
        except CloudError as e:
            _login.update(state="pending", message=e.message)
            continue
        if r["state"] == "slow_down":
            interval += 5
        if r["state"] in ("pending", "slow_down"):
            continue
        _login.update(state=r["state"], error=r.get("error"), message=r.get("message", ""))
        if r["state"] == "signed_in":
            _audit("cloud_signed_in", email=r["status"].get("email"), device=r["status"].get("device_id"))
        _login.pop("device_code", None)
        return
    if _login.get("device_code") == device_code:
        _login.update(state="expired")
        _login.pop("device_code", None)


@router.post("/login")
async def cloud_login(body: LoginBody):
    global _task
    _admin_only("sign this Terminal in to QOS Cloud")
    try:
        d = await asyncio.to_thread(cloud().login_start, body.device_name, body.force)
    except CloudError as e:
        raise _err(e) if e.code != "already_signed_in" else HTTPException(409, e.message)
    if _task and not _task.done():
        _task.cancel()
    exp = time.time() + int(d.get("expires_in", 600))
    _login.clear()
    _login.update(state="pending", device_code=d["device_code"], user_code=d["user_code"],
                  verification_uri=d["verification_uri"], verification_uri_complete=d.get("verification_uri_complete"),
                  expires_at=exp, started=time.time())
    _task = asyncio.create_task(_poll(d["device_code"], int(d.get("interval", 5)), exp))
    return _login_view()


@router.post("/login/open")
async def cloud_login_open():
    """Open the approval page in this machine's browser (only the URL of the pending login — no external URLs accepted)"""
    uri = _login.get("verification_uri_complete") or _login.get("verification_uri")
    if not uri or _login.get("state") not in ("pending", "slow_down"):
        raise HTTPException(409, "no sign-in waiting for approval")
    import webbrowser
    ok = await asyncio.to_thread(webbrowser.open, uri)
    return {"opened": bool(ok), "url": uri}


@router.get("/login")
async def cloud_login_state():
    return _login_view() or {"state": "idle"}


@router.delete("/login")
async def cloud_login_cancel():
    if _task and not _task.done():
        _task.cancel()
    _login.clear()
    return {"state": "idle"}


@router.post("/logout")
async def cloud_logout():
    _admin_only("sign this Terminal out of QOS Cloud")
    await asyncio.to_thread(cloud().logout)
    _audit("cloud_signed_out")
    return await cloud_status()


@router.post("/sync")
async def cloud_sync():
    try:
        await asyncio.to_thread(cloud().sync)
    except CloudError as e:
        raise _err(e)
    return await cloud_status()


@router.get("/devices")
async def cloud_devices():
    try:
        return await asyncio.to_thread(cloud().devices)
    except CloudError as e:
        raise _err(e)


@router.delete("/devices/{device_id}")
async def cloud_revoke_device(device_id: str):
    _admin_only("remove a device from the QOS account")
    try:
        out = await asyncio.to_thread(cloud().revoke_device, device_id)
    except CloudError as e:
        raise _err(e)
    _audit("cloud_device_removed", device=device_id)
    return out


async def cloud_loop(interval: float = 900.0) -> None:
    """Refresh entitlement/revocations when due · may fail (offline) without affecting trading"""
    await asyncio.sleep(20)
    while True:
        try:
            c = cloud()
            if await asyncio.to_thread(c.sync_due):
                await asyncio.to_thread(c.sync)
                await asyncio.to_thread(enforce_revocations)
                from qos.cloud import store as _store
                await asyncio.to_thread(_store.refresh_licences, c)
        except CloudError as e:
            print("cloud sync:", e.message)
        except Exception as e:
            print("cloud sync error:", e)
        await asyncio.sleep(interval)


# ---------- data store (step 35) ----------
@router.get("/datasets")
async def cloud_datasets():
    from qos.cloud import data as cd
    return await asyncio.to_thread(cd.overview, cloud())


_pulling: set[str] = set()


@router.post("/datasets/{slug}/pull")
async def cloud_dataset_pull(slug: str, version: str | None = None):
    from qos.cloud import data as cd
    if slug in _pulling:
        raise HTTPException(409, f"{slug} is already downloading")
    _pulling.add(slug)
    try:
        e = await asyncio.to_thread(cd.pull, slug, version, cloud())
    except CloudError as err:
        raise _err(err) if err.code not in ("not_entitled",) else HTTPException(403, err.message)
    finally:
        _pulling.discard(slug)
    _audit("dataset_pulled", dataset=slug, version=e["version"])
    return e


@router.delete("/datasets/{slug}")
async def cloud_dataset_remove(slug: str):
    from qos.cloud import data as cd
    _admin_only("delete downloaded data")
    return {"removed": await asyncio.to_thread(cd.remove, slug, cloud())}


async def feed_loop(interval: float = 300.0) -> None:
    """Step 37: push 1-minute bars to QOS Cloud for forward paper (only when QOS_CLOUD_FEED=1 and the account is admin)"""
    import os
    if os.getenv("QOS_CLOUD_FEED") != "1":
        return
    from qos.cloud import feed
    await asyncio.sleep(60)
    while True:
        try:
            if cloud().signed_in():
                await asyncio.to_thread(feed.push, cloud(), None, 7)
        except CloudError as e:
            print("cloud feed:", e.message)
        except Exception as e:
            print("cloud feed error:", e)
        await asyncio.sleep(interval)


# ---------- Agent Store (step 38) ----------
@router.get("/store")
async def store_list():
    from qos.cloud import store
    return await asyncio.to_thread(store.overview, cloud())


@router.get("/store/{slug}")
async def store_detail(slug: str):
    from qos.cloud import store
    try:
        return await asyncio.to_thread(store.detail, slug, cloud())
    except CloudError as e:
        raise _err(e) if e.code != "not_found" else HTTPException(404, f"{slug} is not in the store")


class StoreInstall(BaseModel):
    version: str | None = None


@router.post("/store/{slug}/install")
async def store_install(slug: str, body: StoreInstall | None = None):
    """Download + verify the signature, then install as a Strategy lab job (checks → evaluates backtest/walk-forward on its own)"""
    from qos.cloud import store
    from .server import PackageInstall, strategies_packages_install
    _admin_only("install agents from the QOS Store")
    try:
        src = await asyncio.to_thread(store.download, slug, (body or StoreInstall()).version, cloud())
    except CloudError as e:
        raise HTTPException(403, e.message) if e.code == "not_entitled" else _err(e)
    job = await strategies_packages_install(PackageInstall(source=str(src)))
    _audit("store_install", agent=slug, source=str(src), job=job.get("id"))
    return {"slug": slug, "version": src.name, "job": job}


def cloud_package_check(ref: str | None) -> tuple[bool, str]:
    """Before a node starts: Cloud packages must pass store.check_installed (other packages always pass)"""
    if not ref or ref.endswith("@builtin"):
        return True, ""
    from qos.cloud import store
    from qx_strategies.registry import REG
    try:
        e = REG.resolve(ref)
    except (KeyError, ValueError):
        return True, ""
    if not store.is_cloud_package(e.path):
        return True, ""
    return store.check_installed(e.path, cloud())


def enforce_revocations() -> list[str]:
    """After sync: stop nodes running an Agent version the Cloud has revoked (positions are not closed automatically — alert to review)"""
    from qos.cloud import store
    from . import notify
    from .server import _node_ref, nm
    stopped = []
    inst = {m["ref"]: slug for slug, ms in store.installed().items() for m in ms}
    for nid in list(nm.nodes):
        if not nm._running(nid):
            continue
        ref = _node_ref(nid)
        if ref in inst and cloud().revoked("agent", ref):
            nm.stop(nid, "agent version withdrawn by QOS Cloud")
            stopped.append(nid)
            _audit("node_stopped_revoked", node=nid, ref=ref)
            notify.fire(f"{nid} stopped: {ref} was withdrawn by QOS Cloud. The strategy closes its positions on stop (close_positions_on_stop) — check IBKR that they are flat.",
                        "error")
    return stopped


# ---------- payments (step 40) ----------
_waiting: dict[str, asyncio.Task] = {}


class CheckoutBody(BaseModel):
    price_id: str


async def _wait(purchase_id: str) -> None:
    from qos.cloud import billing
    try:
        st = await asyncio.to_thread(billing.wait_paid, purchase_id, cloud())
        _audit("purchase_" + st["status"], purchase=purchase_id, sku=st.get("sku"))
    except CloudError as e:
        print("purchase wait:", e.message)
    finally:
        _waiting.pop(purchase_id, None)


@router.post("/billing/checkout")
async def billing_checkout(body: CheckoutBody):
    """Open the payment page in this machine's browser, then wait for the result in the background (syncs automatically once paid)"""
    import webbrowser
    from qos.cloud import billing
    _admin_only("make purchases for this QOS account")
    try:
        r = await asyncio.to_thread(billing.checkout, body.price_id, cloud())
    except CloudError as e:
        raise HTTPException(409, e.message) if e.code in ("already_owned", "developer_not_ready", "own_agent") else _err(e)
    await asyncio.to_thread(webbrowser.open, r["url"])
    _waiting[r["purchase_id"]] = asyncio.create_task(_wait(r["purchase_id"]))
    _audit("checkout_started", purchase=r["purchase_id"])
    return r


@router.get("/billing/purchase/{purchase_id}")
async def billing_purchase(purchase_id: str):
    from qos.cloud import billing
    try:
        st = await asyncio.to_thread(billing.purchase, purchase_id, cloud())
    except CloudError as e:
        raise _err(e)
    return {**st, "waiting": purchase_id in _waiting}


@router.get("/billing")
async def billing_status():
    from qos.cloud import billing
    try:
        return await asyncio.to_thread(billing.purchases, cloud())
    except CloudError as e:
        raise _err(e)


@router.post("/billing/portal")
async def billing_portal():
    import webbrowser
    from qos.cloud import billing
    try:
        url = await asyncio.to_thread(billing.portal, cloud())
    except CloudError as e:
        raise _err(e)
    await asyncio.to_thread(webbrowser.open, url)
    return {"url": url}
