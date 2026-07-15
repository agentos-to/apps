"""Unlock session: MP + Secret Key → vault keys → item decrypt."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agentos import app_secret
from agentos._bridge import dispatch

from . import crypto_flow, db

_UNLOCK_DOMAIN = "op.local"
_UNLOCK_ITEM_TYPE = "vault_unlock"
_UNLOCK_IDENTIFIER = "default"


@dataclass
class UnlockSession:
    account_uuid: str
    email: str
    primary_keyset_id: str
    keyset_rsa_jwk: dict[str, str] = field(default_factory=dict)  # uuid -> jwk json str
    keyset_sym: dict[str, bytes] = field(default_factory=dict)
    vault_keys: dict[str, bytes] = field(default_factory=dict)
    _db: str | None = None

    async def vault_key(self, vault: dict[str, Any]) -> bytes:
        vuuid = vault["vault_uuid"]
        if vuuid in self.vault_keys:
            return self.vault_keys[vuuid]
        enc = vault["enc_vault_key"]
        kid = enc.get("kid") or self.primary_keyset_id
        jwk = await self._rsa_jwk_for(kid)
        ct = crypto_flow.b64url_decode(enc["data"])
        key_json = await crypto_flow.rsa_oaep_decrypt(jwk, ct)
        key = crypto_flow.extract_oct_key(key_json)
        self.vault_keys[vuuid] = key
        return key

    async def _rsa_jwk_for(self, keyset_uuid: str) -> str:
        if keyset_uuid in self.keyset_rsa_jwk:
            return self.keyset_rsa_jwk[keyset_uuid]
        # Non-primary: decrypt via parent RSA.
        keyset = await db.get_keyset(self.account_uuid, keyset_uuid, db=self._db)
        parent_id = keyset.get("encryptedBy") or self.primary_keyset_id
        if parent_id == "mp":
            parent_id = self.primary_keyset_id
        parent_jwk = self.keyset_rsa_jwk[parent_id]
        # sym key for this keyset may be RSA-wrapped
        enc_sym = keyset["encSymKey"]
        if (enc_sym.get("alg") or "").startswith("PBES2"):
            raise RuntimeError("unexpected PBES2 on non-primary keyset")
        # RSA-OAEP wrapped sym key (data field)
        sym_json = await crypto_flow.rsa_oaep_decrypt(
            parent_jwk, crypto_flow.b64url_decode(enc_sym["data"])
        )
        sym = crypto_flow.extract_oct_key(sym_json)
        self.keyset_sym[keyset_uuid] = sym
        pri_json = await crypto_flow.decrypt_aes_gcm(keyset["encPriKey"], sym)
        self.keyset_rsa_jwk[keyset_uuid] = pri_json.decode("utf-8")
        return self.keyset_rsa_jwk[keyset_uuid]

    async def decrypt_blob(self, vault: dict[str, Any], ed: dict[str, Any]) -> bytes:
        key = await self.vault_key(vault)
        return await crypto_flow.decrypt_aes_gcm(ed, key)

    async def decrypt_item(
        self, vault: dict[str, Any], item: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        overview = json.loads(await self.decrypt_blob(vault, item["overview"]))
        details = json.loads(await self.decrypt_blob(vault, item["details"]))
        return overview, details


def setup_unlock_secret(*, master_password: str, secret_key: str) -> dict[str, Any]:
    """Build ``__secrets__`` entry for per-user unlock material."""
    return app_secret(
        domain=_UNLOCK_DOMAIN,
        identifier=_UNLOCK_IDENTIFIER,
        item_type=_UNLOCK_ITEM_TYPE,
        value={
            "masterPassword": master_password,
            "secretKey": secret_key.strip(),
        },
        source="onepassword",
        metadata={"masked": {"masterPassword": "••••••••", "secretKey": "A3-••••"}},
    )


async def load_unlock_secrets() -> dict[str, str]:
    row = await dispatch(
        "auth_store.read",
        {
            "domain": _UNLOCK_DOMAIN,
            "item_type": _UNLOCK_ITEM_TYPE,
            "account": _UNLOCK_IDENTIFIER,
        },
    )
    if not row or not row.get("found"):
        raise RuntimeError(
            "Local unlock not configured. Call setup_local_unlock() once — "
            "AgentOS opens a Security window for your Master Password."
        )
    value = row.get("value") or {}
    mp = value.get("masterPassword") or value.get("master_password")
    sk = value.get("secretKey") or value.get("secret_key")
    if not mp or not sk:
        raise RuntimeError("op.local unlock row missing masterPassword or secretKey")
    return {"masterPassword": mp, "secretKey": sk}


async def unlock(*, db_path: str | None = None) -> UnlockSession:
    secrets = await load_unlock_secrets()
    account = await db.get_account(db=db_path)
    email = account.get("user_email") or ""
    account_uuid = account["account_uuid"]
    keyset = await db.get_primary_keyset(account_uuid, db=db_path)

    sym_json = await crypto_flow.decrypt_pbes2(
        keyset["encSymKey"],
        secret_key=secrets["secretKey"],
        password=secrets["masterPassword"],
        email=email,
    )
    primary_sym = crypto_flow.extract_oct_key(sym_json)
    pri_json = await crypto_flow.decrypt_aes_gcm(keyset["encPriKey"], primary_sym)
    session = UnlockSession(
        account_uuid=account_uuid,
        email=email,
        primary_keyset_id=keyset["keyset_uuid"],
        _db=db_path,
    )
    session.keyset_sym[keyset["keyset_uuid"]] = primary_sym
    session.keyset_rsa_jwk[keyset["keyset_uuid"]] = pri_json.decode("utf-8")
    return session
