"""2SKD + AES-GCM + RSA-OAEP orchestration via ``agentos.crypto``."""

from __future__ import annotations

import base64
import json
from typing import Any

from agentos import crypto


def b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _hex(b: bytes) -> str:
    return b.hex()


async def compute_2skd(
    secret_key: str,
    password: str,
    email: str,
    salt: bytes,
    iterations: int,
    algorithm: str,
) -> bytes:
    """Two-Secret Key Derivation (AgileBits) — see opcli compute2SKD."""
    parts = secret_key.split("-")
    if len(parts) < 3:
        raise ValueError("invalid secret key format")
    version = parts[0]
    account_id = parts[1]
    secret = "".join(parts[2:])
    email_lower = email.lower()

    # HKDF(ikm=salt, salt=email, info=algorithm) → 32
    hkdf_pass_salt = await crypto.hkdf(
        _hex(salt),
        salt=_hex(email_lower.encode("utf-8")),
        info=algorithm,
        length=32,
    )
    password_key = await crypto.pbkdf2_sha256(
        password,
        _hex(hkdf_pass_salt),
        iterations,
        length=32,
    )
    secret_derived = await crypto.hkdf(
        _hex(secret.encode("utf-8")),
        salt=_hex(account_id.encode("utf-8")),
        info=version,
        length=32,
    )
    return bytes(a ^ b for a, b in zip(password_key, secret_derived, strict=True))


async def decrypt_aes_gcm(ed: dict[str, Any], key: bytes) -> bytes:
    enc = ed.get("enc") or ""
    if enc != "A256GCM":
        raise ValueError(f"unsupported enc: {enc}")
    iv = b64url_decode(ed["iv"])
    data = b64url_decode(ed["data"])
    return await crypto.aes_gcm(_hex(key), _hex(data), _hex(iv))


async def decrypt_pbes2(
    ed: dict[str, Any],
    *,
    secret_key: str,
    password: str,
    email: str,
) -> bytes:
    alg = ed.get("alg") or ""
    if not alg.startswith("PBES2"):
        raise ValueError(f"not PBES2: {alg}")
    salt = b64url_decode(ed["p2s"])
    iterations = int(ed["p2c"])
    key = await compute_2skd(secret_key, password, email, salt, iterations, alg)
    return await decrypt_aes_gcm(ed, key)


def extract_oct_key(jwk_json: bytes) -> bytes:
    jwk = json.loads(jwk_json)
    if jwk.get("kty") != "oct":
        raise ValueError(f"expected oct JWK, got {jwk.get('kty')}")
    return b64url_decode(jwk["k"])


async def rsa_oaep_decrypt(jwk_json: bytes | str, ciphertext: bytes) -> bytes:
    if isinstance(jwk_json, bytes):
        jwk_json = jwk_json.decode("utf-8")
    return await crypto.rsa_oaep(jwk_json, _hex(ciphertext))
