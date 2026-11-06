"""QOS Cloud connector for the Terminal (step 34).

* Sign-in: OAuth device flow (client `qos-desktop`). Refresh token in the Keychain, access token in memory.
* Sync: key set (rotation), entitlement (device-bound, signed) and revocation list, verified with verify.py.
* Offline: the last verified entitlement keeps Cloud features on until it expires (OFFLINE_GRACE_DAYS on Cloud).
* Cloud never gates local trading. Callers use `entitled(sku)` / `cloud_features()` for Cloud-only features.
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlparse

from .tokens import default_store
from .verify import (TYP_KEYS, TrustStore, VerifyError, active_skus, is_revoked, verify_entitlement,
                     verify_revocations)

DEFAULT_URL = "http://localhost:8090"
CLIENT_ID = "qos-desktop"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
PINNED = Path(__file__).with_name("trust") / "prod.json"


class CloudError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code, self.message = code, message or code


class Offline(CloudError):
    def __init__(self, message: str):
        super().__init__("offline", f"cannot reach QOS Cloud: {message}")


class NotSignedIn(CloudError):
    def __init__(self, message: str = "not signed in to QOS Cloud"):
        super().__init__("not_signed_in", message)


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    import uuid
    b = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, path in files.items():
        p = Path(path)
        out += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{p.name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode()
        out += p.read_bytes() + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"


def urllib_http(method: str, url: str, json_body=None, form=None, token=None, timeout: float = 15, files=None):
    headers = {"Accept": "application/json", "User-Agent": "qos-terminal"}
    data = None
    if files:
        data, headers["Content-Type"] = _multipart(form or {}, files)
        timeout = max(timeout, 600)
    elif json_body is not None:
        data, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
    elif form is not None:
        data, headers["Content-Type"] = urlencode(form).encode(), "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        return e.code, body if isinstance(body, dict) else {"error": str(body)}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Offline(str(getattr(e, "reason", e))) from None


def urllib_fetch(url: str, dest: Path, max_bytes: int, timeout: float = 60) -> tuple[int, str]:
    """Download to a file as a stream · returns (size, sha256)"""
    import hashlib
    h, n = hashlib.sha256(), 0
    req = urllib.request.Request(url, headers={"User-Agent": "qos-terminal"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                n += len(chunk)
                if n > max_bytes:
                    raise CloudError("too_large", f"{dest.name} is larger than the manifest says")
                h.update(chunk)
                f.write(chunk)
    except urllib.error.HTTPError as e:
        raise CloudError(f"http_{e.code}", f"download failed ({e.code}) for {dest.name}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Offline(str(getattr(e, "reason", e))) from None
    return n, h.hexdigest()


def _loopback(url: str) -> bool:
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def default_dir() -> Path:
    from qos import paths
    return paths.data_dir() / "cloud"


class Cloud:
    def __init__(self, url: str | None = None, cloud_dir: Path | None = None, store=None, http=None,
                 env: str | None = None, fetch=None):
        self.dir = Path(cloud_dir) if cloud_dir else default_dir()
        self.url = (url or os.getenv("QOS_CLOUD_URL") or DEFAULT_URL).rstrip("/")
        self.env = env or os.getenv("QOS_CLOUD_ENV") or ("dev" if _loopback(self.url) else "prod")
        self.store = store if store is not None else default_store(self.dir)
        self._http = http or urllib_http
        self._fetch = fetch or urllib_fetch
        self._lock = threading.RLock()
        self._access: str | None = None
        self._access_exp = 0.0

    # ---------- files ----------
    @property
    def account(self) -> str:
        return self.url

    def _p(self, name: str) -> Path:
        return self.dir / name

    def _read_json(self, name: str) -> dict:
        try:
            return json.loads(self._p(name).read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, name: str, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._p(name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, self._p(name))

    def _state(self) -> dict:
        st = self._read_json("state.json")
        return st if st.get("url") == self.url else {"url": self.url}

    def _save_state(self, st: dict) -> None:
        self._write("state.json", json.dumps({**st, "url": self.url}, indent=2))

    @contextlib.contextmanager
    def _flock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:
            yield
            return
        with open(self._p(".refresh.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ---------- http ----------
    def _call(self, method: str, path: str, **kw) -> tuple[int, dict]:
        return self._http(method, self.url + path, **kw)

    # ---------- trust ----------
    def _trust_doc(self) -> dict:
        return self._read_json("trust.json").get(self.url, {})

    def _save_trust(self, doc: dict) -> None:
        all_ = self._read_json("trust.json")
        all_[self.url] = {**doc, "updated": int(time.time())}
        self._write("trust.json", json.dumps(all_, indent=2))

    def _pinned(self) -> list[dict]:
        if self.env != "prod":
            return []
        try:
            return json.loads(PINNED.read_text()).get("keys", [])
        except (OSError, ValueError):
            return []

    def trust(self) -> TrustStore:
        doc = self._trust_doc()
        revoked = set(doc.get("revoked", []))
        keys = [k for k in self._pinned() + doc.get("keys", []) if k.get("kid") not in revoked]
        return TrustStore({"keys": keys})

    def _ensure_trust(self) -> None:
        if self.trust().keys:
            return
        if self.env == "prod" and os.getenv("QOS_CLOUD_TOFU") != "1":
            raise CloudError("no_trusted_keys", f"this build has no pinned signing keys for {self.url}")
        st, doc = self._call("GET", "/.well-known/qos-keys.json")
        if st != 200 or not doc.get("keys"):
            raise CloudError("bad_keys", f"could not read signing keys from {self.url} ({st})")
        store = TrustStore(doc)
        try:
            store.learn(doc)
        except VerifyError as e:
            raise CloudError("bad_keys", f"key set signature invalid: {e}") from None
        self._save_trust({"keys": doc["keys"], "revoked": [], "tofu": True, "pinned_at": int(time.time())})

    def _learn_keys(self) -> list[str]:
        st, doc = self._call("GET", "/.well-known/qos-keys.json")
        if st != 200:
            raise CloudError("bad_keys", f"key set unavailable ({st})")
        store = self.trust()
        for tok in doc.get("keyset", []):
            try:
                claims = store.verify(tok, TYP_KEYS)
            except VerifyError:
                continue
            tdoc = self._trust_doc()
            known = {k["kid"] for k in self._pinned() + tdoc.get("keys", [])}
            new = [k for k in claims.get("keys", []) if k.get("kid") not in known]
            if new:
                self._save_trust({**tdoc, "keys": tdoc.get("keys", []) + new})
            return [k["kid"] for k in new]
        raise CloudError("bad_keys", "key set is not signed by a trusted key")

    # ---------- sign-in ----------
    def signed_in(self) -> bool:
        return bool(self._state().get("signed_in"))

    def login_start(self, device_name: str | None = None, force: bool = False) -> dict:
        if self.signed_in():
            if not force:
                raise CloudError("already_signed_in", f"already signed in as {self._state().get('email')}")
            self.logout()
        self._ensure_trust()
        plat = f"macOS {platform.mac_ver()[0]}".strip() if platform.system() == "Darwin" else platform.system()
        st, body = self._call("POST", "/oauth/device/code", form={
            "client_id": CLIENT_ID, "device_name": (device_name or socket.gethostname())[:80], "platform": plat})
        if st != 200:
            raise CloudError(body.get("error", f"http_{st}"), body.get("error_description", ""))
        return body

    def login_poll(self, device_code: str) -> dict:
        st, body = self._call("POST", "/oauth/token", form={"grant_type": DEVICE_GRANT, "client_id": CLIENT_ID,
                                                             "device_code": device_code})
        if st == 200:
            self._store_tokens(body, first=True)
            try:
                self.sync()
            except CloudError:
                pass
            return {"state": "signed_in", "status": self.status()}
        err = body.get("error", f"http_{st}")
        state = {"authorization_pending": "pending", "slow_down": "slow_down", "access_denied": "denied",
                 "expired_token": "expired"}.get(err, "error")
        return {"state": state, "error": err, "message": body.get("error_description", "")}

    def login(self, on_code=print, timeout: float = 900, device_name: str | None = None, force: bool = False) -> dict:
        d = self.login_start(device_name, force)
        on_code(d)
        interval, end = d.get("interval", 5), time.time() + min(timeout, d.get("expires_in", 600))
        while time.time() < end:
            time.sleep(interval)
            r = self.login_poll(d["device_code"])
            if r["state"] == "slow_down":
                interval += 5
            elif r["state"] != "pending":
                return r
        return {"state": "expired", "error": "expired_token"}

    def _store_tokens(self, body: dict, first: bool = False) -> None:
        self.store.set(self.account, body["refresh_token"])
        self._access, self._access_exp = body["access_token"], time.time() + int(body.get("expires_in", 900))
        if first:
            st = self._state()
            st.update(signed_in=True, device_id=body.get("device_id"), signed_in_at=int(time.time()), last_error="")
            self._save_state(st)

    def _signed_out(self, why: str) -> None:
        self.store.delete(self.account)
        self._access, self._access_exp = None, 0.0
        with contextlib.suppress(FileNotFoundError):
            self._p("entitlement.jws").unlink()
        st = self._state()
        st.update(signed_in=False, device_id=None, last_error=why)
        self._save_state(st)

    def access_token(self) -> str:
        with self._lock:
            if self._access and time.time() < self._access_exp - 60:
                return self._access
            if not self.signed_in():
                raise NotSignedIn()
            with self._flock():
                rt = self.store.get(self.account)
                if not rt:
                    self._signed_out("refresh token missing from the token store; sign in again")
                    raise NotSignedIn()
                st, body = self._call("POST", "/oauth/token", form={"grant_type": "refresh_token", "refresh_token": rt})
                if st == 200:
                    self._store_tokens(body)
                    return self._access
                if body.get("error") == "invalid_grant":
                    self._signed_out("signed out by QOS Cloud (device removed or session expired)")
                    raise NotSignedIn("signed out by QOS Cloud — sign in again")
                raise CloudError(body.get("error", f"http_{st}"), body.get("error_description", ""))

    def fetch(self, url: str, dest: Path, max_bytes: int) -> tuple[int, str]:
        return self._fetch(url if url.startswith("http") else self.url + url, dest, max_bytes)

    def _authed(self, method: str, path: str, **kw) -> dict:
        for attempt in (0, 1):
            st, body = self._call(method, path, token=self.access_token(), **kw)
            if st == 401:
                if body.get("error") == "revoked":
                    self._signed_out("this device was removed from your QOS account")
                    raise NotSignedIn("this device was removed from your QOS account")
                self._access = None
                continue
            if st >= 400:
                raise CloudError(body.get("error", f"http_{st}"), body.get("error_description", ""))
            return body
        raise NotSignedIn()

    # ---------- sync ----------
    def sync(self) -> dict:
        st = self._state()
        st["last_attempt"] = int(time.time())
        try:
            self._learn_keys()
            tok = self._authed("POST", "/entitlements")["token"]
            ent = verify_entitlement(self.trust(), tok, device_id=st.get("device_id"))
            self._write("entitlement.jws", tok)
            code, rv = self._call("GET", "/revocations")
            if code != 200:
                raise CloudError("revocations_unavailable", f"revocation list unavailable ({code})")
            store = self.trust()
            claims = verify_revocations(store, rv["token"], min_seq=ent.get("rev_seq", 0))
            dropped = [i["ref"] for i in claims.get("items", []) if i.get("kind") == "key"]
            if dropped:
                tdoc = self._trust_doc()
                self._save_trust({**tdoc, "revoked": sorted(set(tdoc.get("revoked", [])) | set(dropped))})
            self._write("revocations.jws", rv["token"])
            st = self._state()
            st.update(last_sync=int(time.time()), last_error="", email=ent.get("email"),
                      last_attempt=st.get("last_attempt") or int(time.time()))
            self._save_state(st)
        except NotSignedIn:
            raise
        except (CloudError, VerifyError) as e:
            st = self._state()
            st["last_error"] = getattr(e, "message", str(e))[:300]
            st["last_attempt"] = int(time.time())
            self._save_state(st)
            raise e if isinstance(e, CloudError) else CloudError("verify_failed", str(e))
        return self.status()

    def sync_due(self, now: float | None = None) -> bool:
        if not self.signed_in():
            return False
        ent = self.entitlement(now)
        t = now or time.time()
        if ent is None:
            return True
        return t >= ent.get("refresh_after", 0)

    # ---------- offline view ----------
    def entitlement(self, now: float | None = None) -> dict | None:
        try:
            tok = self._p("entitlement.jws").read_text().strip()
            return verify_entitlement(self.trust(), tok, device_id=self._state().get("device_id"), now=now)
        except (OSError, VerifyError):
            return None

    def revocations(self) -> dict:
        try:
            tok = self._p("revocations.jws").read_text().strip()
            return self.trust().verify(tok, "qos-rev+jwt", now=0.0)
        except (OSError, VerifyError):
            return {"seq": 0, "items": []}

    def entitled(self, sku: str, now: float | None = None) -> bool:
        ent = self.entitlement(now)
        return ent is not None and sku in active_skus(ent, now)

    def cloud_features(self, now: float | None = None) -> bool:
        return self.entitlement(now) is not None

    def revoked(self, kind: str, ref: str) -> bool:
        return is_revoked(self.revocations(), kind, ref)

    def status(self, now: float | None = None) -> dict:
        t = now or time.time()
        st = self._state()
        ent = self.entitlement(t)
        rev = self.revocations()
        tdoc = self._trust_doc()
        out = {"url": self.url, "env": self.env, "signed_in": bool(st.get("signed_in")), "email": st.get("email"),
               "device_id": st.get("device_id"), "token_store": getattr(self.store, "kind", "custom"),
               "cloud_features": ent is not None, "last_sync": st.get("last_sync"), "last_attempt": st.get("last_attempt"),
               "last_error": st.get("last_error") or "",
               "revocations": {"seq": rev.get("seq", 0), "count": len(rev.get("items", []))},
               "trust": {"keys": len(self.trust().keys), "pinned": len(self._pinned()), "tofu": bool(tdoc.get("tofu"))}}
        if ent:
            skus = active_skus(ent, t)
            out.update(plan=ent.get("plan"), role=ent.get("role"), max_devices=ent.get("max_devices"),
                       grants=[g for g in ent.get("grants", []) if g["sku"] in skus],
                       entitlement_expires_at=ent["exp"], offline_left_s=max(0, int(ent["exp"] - t)),
                       refresh_after=ent.get("refresh_after"), email=ent.get("email") or out["email"])
        else:
            out.update(plan=None, role=None, max_devices=None, grants=[], entitlement_expires_at=None, offline_left_s=0,
                       refresh_after=None)
        return out

    # ---------- account ----------
    def logout(self) -> None:
        rt = self.store.get(self.account)
        if rt:
            with contextlib.suppress(CloudError):
                self._call("POST", "/oauth/revoke", form={"token": rt})
        self._signed_out("")

    def devices(self) -> dict:
        return self._authed("GET", "/devices")

    def revoke_device(self, device_id: str) -> dict:
        out = self._authed("DELETE", f"/devices/{device_id}")
        if device_id == self._state().get("device_id"):
            self._signed_out("")
        return out


_default: Cloud | None = None


def get() -> Cloud:
    global _default
    if _default is None:
        _default = Cloud()
    return _default
