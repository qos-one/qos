"""Standalone verifier for documents signed by QOS Cloud.

Depends only on PyJWT and cryptography, so the Terminal can vendor this file as-is.
Everything Cloud sends down (entitlements, revocation lists, artifact manifests, key sets)
is a compact JWS signed with Ed25519 (alg EdDSA) and carries a `kid` and a `typ` header.
"""
from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path, PurePosixPath

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ISS = "qos-cloud"
AUD = "qos-terminal"
TYP_ENT = "qos-ent+jwt"
TYP_REV = "qos-rev+jwt"
TYP_MAN = "qos-man+jwt"
TYP_KEYS = "qos-keys+jwt"
TYP_LIC = "qos-lic+jwt"
MANIFEST_FILE = "QOS-MANIFEST.jws"


class VerifyError(Exception):
    pass


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwk_to_key(jwk: dict) -> Ed25519PublicKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise VerifyError("unsupported key type")
    return Ed25519PublicKey.from_public_bytes(_b64d(jwk["x"]))


class TrustStore:
    """Public keys the Terminal trusts. Start from keys pinned in the app, then learn rotations."""

    def __init__(self, jwks: dict | list | None = None):
        self.keys: dict[str, Ed25519PublicKey] = {}
        if jwks:
            self.add(jwks)

    def add(self, jwks: dict | list) -> list[str]:
        items = jwks.get("keys", []) if isinstance(jwks, dict) else jwks
        added = []
        for j in items:
            kid = j.get("kid")
            if kid and kid not in self.keys:
                self.keys[kid] = jwk_to_key(j)
                added.append(kid)
        return added

    def drop(self, kid: str) -> None:
        self.keys.pop(kid, None)

    def verify(self, token: str, typ: str, *, require_exp: bool = True, now: float | None = None,
               leeway: int = 60) -> dict:
        try:
            head = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise VerifyError(f"malformed: {e}") from None
        if head.get("alg") != "EdDSA":
            raise VerifyError("alg must be EdDSA")
        if head.get("typ") != typ:
            raise VerifyError(f"expected {typ}, got {head.get('typ')}")
        key = self.keys.get(head.get("kid", ""))
        if key is None:
            raise VerifyError(f"unknown signing key {head.get('kid')}")
        req = ["iat", "exp"] if require_exp else ["iat"]
        try:
            claims = jwt.decode(token, key, algorithms=["EdDSA"], audience=AUD, issuer=ISS, leeway=leeway,
                                options={"require": req, "verify_exp": now is None, "verify_iat": False})
        except jwt.PyJWTError as e:
            raise VerifyError(str(e)) from None
        if now is not None and "exp" in claims and claims["exp"] + leeway < now:
            raise VerifyError("expired")
        return claims

    def learn(self, doc: dict, now: float | None = None) -> list[str]:
        """Adopt keys from /.well-known/qos-keys.json if any keyset signature is from a key we already trust."""
        for tok in doc.get("keyset", []):
            try:
                claims = self.verify(tok, TYP_KEYS, now=now)
            except VerifyError:
                continue
            return self.add(claims.get("keys", []))
        raise VerifyError("key set is not signed by a trusted key")


def verify_entitlement(store: TrustStore, token: str, device_id: str | None = None,
                       now: float | None = None) -> dict:
    claims = store.verify(token, TYP_ENT, now=now)
    if device_id is not None and claims.get("did") != device_id:
        raise VerifyError("entitlement belongs to another device")
    return claims


def verify_license(store: TrustStore, token: str, device_id: str, agent: str, now: float | None = None) -> dict:
    """Licence for a paid agent, bound to one device. exp = end of the paid period or the offline window, whichever is first."""
    claims = store.verify(token, TYP_LIC, now=now)
    if claims.get("did") != device_id:
        raise VerifyError("licence belongs to another device")
    if claims.get("agent") != agent:
        raise VerifyError("licence is for another agent")
    return claims


def active_skus(claims: dict, now: float | None = None) -> set[str]:
    t = now if now is not None else time.time()
    return {g["sku"] for g in claims.get("grants", []) if g.get("exp") is None or g["exp"] > t}


def verify_revocations(store: TrustStore, token: str, min_seq: int = 0, now: float | None = None) -> dict:
    claims = store.verify(token, TYP_REV, now=now)
    if claims.get("seq", 0) < min_seq:
        raise VerifyError("revocation list is older than required")
    for item in claims.get("items", []):
        if item.get("kind") == "key":
            store.drop(item.get("ref", ""))
    return claims


def is_revoked(rev_claims: dict, kind: str, ref: str) -> bool:
    return any(i.get("kind") == kind and i.get("ref") == ref for i in rev_claims.get("items", []))


def _safe(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts and "\\" not in rel


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest(store: TrustStore, token: str, root: Path | None = None, strict: bool = True) -> dict:
    """Check the signature, then (with root) that every file matches its hash and nothing was added."""
    claims = store.verify(token, TYP_MAN, require_exp=False)
    files = claims.get("files", [])
    if claims.get("schema") != "qos-manifest/1" or not isinstance(files, list):
        raise VerifyError("bad manifest")
    for f in files:
        if not _safe(f.get("path", "")):
            raise VerifyError(f"unsafe path {f.get('path')!r}")
    if root is None:
        return claims
    root = Path(root)
    listed = set()
    for f in files:
        p = root / f["path"]
        listed.add(f["path"])
        if not p.is_file() or p.is_symlink():
            raise VerifyError(f"missing {f['path']}")
        if p.stat().st_size != f["size"] or sha256_file(p) != f["sha256"]:
            raise VerifyError(f"modified {f['path']}")
    if strict:
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if p.is_file() and rel != MANIFEST_FILE and rel not in listed:
                raise VerifyError(f"unexpected file {rel}")
    return claims
