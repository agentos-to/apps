"""Read-only access to the local 1Password B5 SQLite database."""

from __future__ import annotations

import json
import os
from typing import Any

from agentos import sql

# Standard macOS 1Password 8 group-container path (per logged-in OS user).
_DB_REL = (
    "Library/Group Containers/2BUA8C4S2C.com.1password/"
    "Library/Application Support/1Password/Data/1password.sqlite"
)


def default_db_path() -> str:
    return os.path.join(os.path.expanduser("~"), _DB_REL)


def _decode_cell(value: Any) -> Any:
    """sql.query returns BLOBs as ``hex:<hex>``; JSON blobs are UTF-8 text."""
    if isinstance(value, str) and value.startswith("hex:"):
        raw = bytes.fromhex(value[4:])
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw
    return value


def _parse_json_cell(value: Any) -> Any:
    cell = _decode_cell(value)
    if isinstance(cell, (bytes, bytearray)):
        cell = cell.decode("utf-8")
    if isinstance(cell, str):
        return json.loads(cell)
    return cell


async def query(sql_text: str, *, params: dict[str, Any] | None = None, db: str | None = None) -> list[dict]:
    rows = await sql.query(sql_text, db or default_db_path(), params=params or {})
    return [{k: _decode_cell(v) for k, v in row.items()} for row in rows]


async def get_account(db: str | None = None) -> dict[str, Any]:
    rows = await query("SELECT account_uuid, data FROM accounts LIMIT 1", db=db)
    if not rows:
        raise RuntimeError("No 1Password account in local database")
    data = _parse_json_cell(rows[0]["data"])
    data["account_uuid"] = rows[0]["account_uuid"]
    return data


async def get_primary_keyset(account_uuid: str, db: str | None = None) -> dict[str, Any]:
    rows = await query(
        """
        SELECT key_name, data FROM objects_associated
        WHERE account_uuid = :account_uuid
          AND type = 36
          AND json_extract(data, '$.encryptedBy') = 'mp'
        LIMIT 1
        """,
        params={"account_uuid": account_uuid},
        db=db,
    )
    if not rows:
        raise RuntimeError("Primary (mp) keyset not found")
    keyset = _parse_json_cell(rows[0]["data"])
    keyset["keyset_uuid"] = rows[0]["key_name"]
    return keyset


async def get_keyset(account_uuid: str, keyset_uuid: str, db: str | None = None) -> dict[str, Any]:
    rows = await query(
        """
        SELECT key_name, data FROM objects_associated
        WHERE account_uuid = :account_uuid AND type = 36 AND key_name = :key_name
        LIMIT 1
        """,
        params={"account_uuid": account_uuid, "key_name": keyset_uuid},
        db=db,
    )
    if not rows:
        raise RuntimeError(f"Keyset not found: {keyset_uuid}")
    keyset = _parse_json_cell(rows[0]["data"])
    keyset["keyset_uuid"] = rows[0]["key_name"]
    return keyset


async def list_vaults(account_uuid: str, db: str | None = None) -> list[dict[str, Any]]:
    rows = await query(
        "SELECT vault_uuid, data FROM vaults WHERE account_uuid = :account_uuid",
        params={"account_uuid": account_uuid},
        db=db,
    )
    out = []
    for row in rows:
        vault = _parse_json_cell(row["data"])
        vault["vault_uuid"] = row["vault_uuid"]
        out.append(vault)
    return out


async def list_items(account_uuid: str, vault_uuid: str, db: str | None = None) -> list[dict[str, Any]]:
    rows = await query(
        """
        SELECT item_uuid, data FROM items
        WHERE account_uuid = :account_uuid AND vault_uuid = :vault_uuid
        """,
        params={"account_uuid": account_uuid, "vault_uuid": vault_uuid},
        db=db,
    )
    out = []
    for row in rows:
        item = _parse_json_cell(row["data"])
        # B5 `state` is often an int enum (e.g. 1=active). Never call .lower()
        # on the raw value — coerce first.
        raw_state = item.get("state")
        state = "" if raw_state is None else str(raw_state).lower()
        if state in ("trashed", "deleted"):
            continue
        item["item_uuid"] = row["item_uuid"]
        item["vault_uuid"] = vault_uuid
        out.append(item)
    return out
