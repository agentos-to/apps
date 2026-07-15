"""Deobfuscate 1Password secret keys stored in the local B5 SQLite DB.

Algorithm mirrors AgileBits' local-DB masking (XOR with a fixed key, hex +
``obfus`` suffix). Do not log or print real secret keys.
"""

from __future__ import annotations

_OBFUSCATION_KEY = (
    b"This is an obfuscation key used to mask the secret key in the local "
    b"database and nothing more. If this seems interesting to you, come "
    b"work with us :)"
)


def deobfuscate_secret_key(obfuscated: str) -> str:
    """Reverse 1Password's secret-key obfuscation → ``A3-…`` plaintext."""
    if not obfuscated.endswith("obfus"):
        raise ValueError("not an obfuscated secret key (missing 'obfus' suffix)")
    raw = bytes.fromhex(obfuscated[:-5])
    if len(raw) > len(_OBFUSCATION_KEY):
        raise ValueError("obfuscated key too long")
    plain = bytes(b ^ _OBFUSCATION_KEY[i] for i, b in enumerate(raw))
    return plain.decode("utf-8")


def _validate_secret_key(key: str) -> str:
    """Masked checks only — must look like ``A3-…`` with dashes."""
    if not key.startswith("A3"):
        raise ValueError("deobfuscated secret key must start with A3")
    if "-" not in key:
        raise ValueError("deobfuscated secret key must contain dashes")
    return key


async def load_secret_key_from_local_db(db: str | None = None) -> str:
    """Read ``accounts.sign_in_provider.secret_key`` and deobfuscate it."""
    from . import db as vault_db

    account = await vault_db.get_account(db=db)
    sip = account.get("sign_in_provider") or {}
    obfuscated = sip.get("secret_key")
    if not obfuscated or not isinstance(obfuscated, str):
        raise RuntimeError("accounts.sign_in_provider.secret_key missing")
    return _validate_secret_key(deobfuscate_secret_key(obfuscated))
