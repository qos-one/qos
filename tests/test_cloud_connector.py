"""Step 34: QOS Cloud connector (tested against an in-memory fake Cloud · E2E against the real Cloud is in cloud/tests)"""
import asyncio
import base64
import os
import secrets
import stat
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from qos.cloud.client import Cloud, CloudError, NotSignedIn
from qos.cloud.tokens import FileStore

URL = "http://localhost:8090"


class FakeCloud:
    def __init__(self):
        self.key, self.kid = Ed25519PrivateKey.generate(), "k1"
        self.approved, self.device, self.rt, self.access = False, "dev1", None, None
        self.grants = [{"sku": "plan.pro", "kind": "plan", "exp": None}]
        self.items, self.revoked, self.calls, self.ent_device, self.role = [], False, [], None, "trader"

    def jwk(self):
        x = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return {"kty": "OKP", "crv": "Ed25519", "x": base64.urlsafe_b64encode(x).rstrip(b"=").decode(), "kid": self.kid}

    def sign(self, payload, typ, life=7 * 86400):
        t = int(time.time())
        return jwt.encode({"iss": "qos-cloud", "aud": "qos-terminal", "iat": t, "exp": t + life, **payload}, self.key,
                          algorithm="EdDSA", headers={"kid": self.kid, "typ": typ})

    def tokens(self):
        self.rt, self.access = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        return {"access_token": self.access, "refresh_token": self.rt, "expires_in": 900, "device_id": self.device}

    def __call__(self, method, url, json_body=None, form=None, token=None, timeout=15):
        path = url[len(URL):]
        self.calls.append((method, path))
        if path == "/.well-known/qos-keys.json":
            return 200, {"keys": [self.jwk()], "keyset": [self.sign({"keys": [self.jwk()]}, "qos-keys+jwt")]}
        if path == "/oauth/device/code":
            return 200, {"device_code": "dc", "user_code": "BCDF-GHJK", "verification_uri": URL + "/device",
                         "verification_uri_complete": URL + "/device?user_code=BCDF-GHJK", "interval": 0, "expires_in": 60}
        if path == "/oauth/token":
            if form["grant_type"] == "refresh_token":
                return (200, self.tokens()) if form["refresh_token"] == self.rt and not self.revoked else (400, {"error": "invalid_grant"})
            return (200, self.tokens()) if self.approved else (400, {"error": "authorization_pending"})
        if path == "/oauth/revoke":
            self.revoked = True
            return 200, {}
        if path == "/revocations":
            return 200, {"token": self.sign({"seq": len(self.items), "items": self.items}, "qos-rev+jwt")}
        if token != self.access or self.revoked:
            return 401, {"error": "revoked" if self.revoked else "invalid_token"}
        if path == "/entitlements":
            t = int(time.time())
            return 200, {"token": self.sign({"sub": "u1", "did": self.ent_device or self.device, "email": "thai@example.com",
                                             "role": self.role, "plan": "pro", "max_devices": 5, "grants": self.grants,
                                             "rev_seq": len(self.items), "refresh_after": t + 6 * 3600}, "qos-ent+jwt")}
        if path == "/devices":
            return 200, {"plan": "pro", "limit": 5, "devices": [{"id": self.device, "current": True}]}
        if path == f"/devices/{self.device}" and method == "DELETE":
            self.revoked = True
            return 200, {"status": "revoked"}
        return 404, {"error": "not_found"}


@pytest.fixture
def fake():
    return FakeCloud()


def make(fake, tmp_path, **kw):
    return Cloud(url=URL, cloud_dir=tmp_path / "cloud", store=FileStore(tmp_path / "cloud" / ".tokens.json"), http=fake, **kw)


def signed_in(fake, tmp_path):
    c = make(fake, tmp_path)
    d = c.login_start("iMac")
    assert c.login_poll(d["device_code"])["state"] == "pending"
    fake.approved = True
    assert c.login_poll(d["device_code"])["state"] == "signed_in"
    return c


def test_login_sync_and_offline_status(fake, tmp_path):
    c = signed_in(fake, tmp_path)
    s = c.status()
    assert s["signed_in"] and s["cloud_features"] and s["plan"] == "pro" and s["email"] == "thai@example.com"
    assert s["trust"] == {"keys": 1, "pinned": 0, "tofu": True} and s["token_store"] == "file"
    assert c.entitled("plan.pro") and not c.entitled("data.fx")
    mode = stat.S_IMODE(os.stat(tmp_path / "cloud" / ".tokens.json").st_mode)
    assert mode == 0o600 and fake.access not in (tmp_path / "cloud" / ".tokens.json").read_text()
    for f in ("state.json", "entitlement.jws", "revocations.jws", "trust.json"):
        assert fake.rt not in (tmp_path / "cloud" / f).read_text()
    later = time.time() + 8 * 86400
    assert not c.cloud_features(now=later) and c.status(now=later)["offline_left_s"] == 0
    assert c.sync_due(now=time.time() + 7 * 3600) and not c.sync_due()


def test_cloud_down_keeps_features(fake, tmp_path):
    c = signed_in(fake, tmp_path)

    def down(*a, **k):
        from qos.cloud.client import Offline
        raise Offline("connection refused")
    c2 = Cloud(url=URL, cloud_dir=tmp_path / "cloud", store=FileStore(tmp_path / "cloud" / ".tokens.json"), http=down)
    with pytest.raises(CloudError):
        c2.sync()
    s = c2.status()
    assert s["cloud_features"] and s["signed_in"] and "cannot reach" in s["last_error"]


def test_prod_requires_pinned_keys(fake, tmp_path):
    c = make(fake, tmp_path, env="prod")
    with pytest.raises(CloudError, match="no pinned"):
        c.login_start()
    assert ("POST", "/oauth/device/code") not in fake.calls


def test_entitlement_for_other_device_ignored(fake, tmp_path):
    c = signed_in(fake, tmp_path)
    fake.ent_device = "someone-else"
    with pytest.raises(CloudError):
        c.sync()


def test_remote_revoke_signs_out(fake, tmp_path):
    c = signed_in(fake, tmp_path)
    fake.revoked = True
    with pytest.raises(NotSignedIn):
        c.sync()
    s = c.status()
    assert not s["signed_in"] and not s["cloud_features"]
    assert FileStore(tmp_path / "cloud" / ".tokens.json").get(URL) is None


def test_refresh_reuse_signs_out(fake, tmp_path):
    c = signed_in(fake, tmp_path)
    c._access = None
    fake.rt = "rotated-elsewhere"
    with pytest.raises(NotSignedIn, match="sign in again"):
        c.devices()
    assert not c.signed_in()


def test_revocations_enforced_even_when_list_old(fake, tmp_path):
    c = signed_in(fake, tmp_path)
    fake.items = [{"seq": 1, "kind": "agent", "ref": "trend@1.0.0", "reason": "bug"}]
    c.sync()
    tok = fake.sign({"seq": 1, "items": fake.items}, "qos-rev+jwt", life=-3600)
    (tmp_path / "cloud" / "revocations.jws").write_text(tok)
    assert c.revoked("agent", "trend@1.0.0") and not c.revoked("agent", "trend@1.1.0")


def test_logout_and_already_signed_in(fake, tmp_path):
    c = signed_in(fake, tmp_path)
    with pytest.raises(CloudError, match="already signed in"):
        c.login_start()
    c.logout()
    assert fake.revoked and not c.signed_in() and not c.cloud_features()


def test_api_login_flow(fake, tmp_path, monkeypatch):
    from api import cloud_api
    from qos.cloud import client as cc
    monkeypatch.setattr(cc, "_default", make(fake, tmp_path))
    monkeypatch.setattr(cloud_api, "_audit", lambda *a, **k: None)

    async def run():
        with pytest.raises(cloud_api.HTTPException):
            await cloud_api.cloud_login_open()
        v = await cloud_api.cloud_login(cloud_api.LoginBody(device_name="iMac"))
        assert v["state"] == "pending" and v["user_code"] == "BCDF-GHJK" and "device_code" not in v
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u) or True)
        assert (await cloud_api.cloud_login_open())["url"].endswith("user_code=BCDF-GHJK") and opened
        fake.approved = True
        for _ in range(50):
            await asyncio.sleep(0.01)
            if (await cloud_api.cloud_login_state())["state"] == "signed_in":
                break
        s = await cloud_api.cloud_status()
        assert s["signed_in"] and s["login"]["state"] == "signed_in" and s["plan"] == "pro"
        assert (await cloud_api.cloud_devices())["limit"] == 5
        s = await cloud_api.cloud_logout()
        assert not s["signed_in"]
        with pytest.raises(cloud_api.HTTPException) as e:
            await cloud_api.cloud_devices()
        assert e.value.status_code == 401
    asyncio.run(run())


def test_cli_status_without_api(fake, tmp_path, monkeypatch, capsys):
    from qos import cli
    from qos.cloud import client as cc
    monkeypatch.setattr(cc, "_default", signed_in(fake, tmp_path))
    assert cli.main(["--api", "http://127.0.0.1:9", "cloud", "status"]) == 0
    out = capsys.readouterr().out
    assert "signed in as thai@example.com" in out and "plan pro" in out and "first-use pin" in out


def test_verify_copy_matches_cloud():
    from pathlib import Path
    cloud = Path(__file__).resolve().parents[2] / "cloud" / "app" / "verify.py"
    if not cloud.exists():
        pytest.skip("cloud repo not present")
    assert (Path(__file__).resolve().parents[1] / "qos" / "cloud" / "verify.py").read_bytes() == cloud.read_bytes()
