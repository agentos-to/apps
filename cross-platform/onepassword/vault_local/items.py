"""Find and decrypt vault items by category / query."""

from __future__ import annotations

from typing import Any

from . import db
from .unlock import UnlockSession

# 1Password B5 category_uuid → friendly name.
# Verified against live local vault item titles (Server↔Software License and
# Driver License↔Membership were historically swapped in older notes).
CATEGORIES: dict[str, str] = {
    "001": "Login",
    "002": "Credit Card",
    "003": "Secure Note",
    "004": "Identity",
    "005": "Password",
    "100": "Software License",
    "101": "Database",
    "102": "Membership",
    "103": "Driver License",
    "105": "Passport",
    "106": "Rewards",
    "110": "Server",
    "111": "SSH Key",
    "112": "Wireless Router",
    "114": "Document",
    "115": "API Credential",
}

_NAME_TO_IDS: dict[str, list[str]] = {}
for _id, _name in CATEGORIES.items():
    _NAME_TO_IDS.setdefault(_name.lower(), []).append(_id)

# Government-id style categories we surface via get_government_id.
# Driver License=103, Passport=105 (after CATEGORIES correction above).
_GOV_IDS = {"103", "105"}


def category_ids_for(name: str) -> list[str]:
    key = name.strip().lower()
    if key in _NAME_TO_IDS:
        return list(_NAME_TO_IDS[key])
    # allow raw id
    if key in CATEGORIES:
        return [key]
    if key == "government id" or key == "government_id":
        return sorted(_GOV_IDS)
    raise KeyError(f"unknown category: {name}")


def _title_match(overview: dict, q: str) -> bool:
    title = overview.get("title") or overview.get("ainfo") or ""
    if not isinstance(title, str):
        title = str(title)
    needle = q.strip().lower()
    return needle in title.lower() if needle else False


def _field_map_from_details(details: dict) -> dict[str, str]:
    """Normalize decrypted details into the same id→value map CLI mappers expect."""
    out: dict[str, str] = {}

    def _put(keys: list[Any], val: Any) -> None:
        if val is None or val == "":
            return
        if isinstance(val, (dict, list)):
            return
        s = str(val).strip('"') if isinstance(val, str) else str(val)
        for key in keys:
            if key:
                out[str(key)] = s

    fields = details.get("fields") or []
    if isinstance(fields, list):
        for f in fields:
            if not isinstance(f, dict):
                continue
            val = f.get("value")
            if val is None:
                val = f.get("v")
            # designation → also index as username/password
            des = f.get("designation")
            keys = [f.get("id"), f.get("n"), f.get("name"), f.get("label"), f.get("t"), des]
            _put(keys, val)

    notes = details.get("notesPlain") or details.get("notes")
    if notes and "notesPlain" not in out:
        out["notesPlain"] = str(notes)

    for sec in details.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        for f in sec.get("fields") or []:
            if not isinstance(f, dict):
                continue
            val = f.get("v") if "v" in f else f.get("value")
            _put([f.get("n"), f.get("id"), f.get("t"), f.get("name")], val)

    # Top-level string extras (Password items, etc.)
    for k, v in details.items():
        if k in ("fields", "sections", "itemUUID", "notesPlain", "notes"):
            continue
        if isinstance(v, str) and v and k not in out:
            out[k] = v
    return out


def _urls_from_overview(overview: dict) -> list[str]:
    urls = []
    for u in overview.get("urls") or overview.get("URLS") or []:
        if isinstance(u, dict):
            href = u.get("u") or u.get("url") or u.get("href")
            if href:
                urls.append(str(href))
        elif isinstance(u, str):
            urls.append(u)
    url = overview.get("url")
    if url:
        urls.append(str(url))
    return urls


async def _vault_index(session: UnlockSession) -> dict[str, dict]:
    vaults = await db.list_vaults(session.account_uuid, db=session._db)
    return {v["vault_uuid"]: v for v in vaults}


async def list_overviews(
    session: UnlockSession,
    *,
    category: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """Decrypt overviews (titles/urls) — no detail secrets."""
    ids = None
    if category:
        ids = set(category_ids_for(category))
    vaults = await _vault_index(session)
    out: list[dict[str, Any]] = []
    for vuuid, vault in vaults.items():
        for item in await db.list_items(session.account_uuid, vuuid, db=session._db):
            cat = str(item.get("category_uuid") or "")
            if ids is not None and cat not in ids:
                continue
            try:
                overview = __import__("json").loads(
                    await session.decrypt_blob(vault, item["overview"])
                )
            except Exception:
                continue
            if q and not _title_match(overview, q):
                continue
            out.append(
                {
                    "id": item["item_uuid"],
                    "title": overview.get("title") or "",
                    "category": CATEGORIES.get(cat, cat),
                    "category_uuid": cat,
                    "vault_uuid": vuuid,
                    "urls": _urls_from_overview(overview),
                }
            )
    return out


async def find_item(
    session: UnlockSession,
    category: str,
    q: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return (overview, details, meta) for the best title match."""
    ids = set(category_ids_for(category))
    vaults = await _vault_index(session)
    hits: list[tuple[dict, dict, dict, dict]] = []
    for vuuid, vault in vaults.items():
        for item in await db.list_items(session.account_uuid, vuuid, db=session._db):
            cat = str(item.get("category_uuid") or "")
            if cat not in ids:
                continue
            try:
                overview, details = await session.decrypt_item(vault, item)
            except Exception:
                continue
            if not _title_match(overview, q):
                continue
            hits.append((overview, details, item, vault))
    if not hits:
        raise LookupError(q)
    if len(hits) > 1:
        exact = [
            h
            for h in hits
            if str(h[0].get("title") or "").lower() == q.strip().lower()
        ]
        if len(exact) == 1:
            hits = exact
        else:
            raise LookupError(f"Multiple matches for {q!r}")
    overview, details, item, vault = hits[0]
    meta = {
        "id": item["item_uuid"],
        "title": overview.get("title") or q,
        "category_uuid": item.get("category_uuid"),
        "vault_uuid": vault["vault_uuid"],
        "fields": _field_map_from_details(details),
        "urls": _urls_from_overview(overview),
        "overview": overview,
        "details": details,
    }
    return overview, details, meta


async def get_item_details(
    session: UnlockSession, item_uuid: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    vaults = await _vault_index(session)
    for vuuid, vault in vaults.items():
        for item in await db.list_items(session.account_uuid, vuuid, db=session._db):
            if item["item_uuid"] != item_uuid:
                continue
            overview, details = await session.decrypt_item(vault, item)
            meta = {
                "id": item_uuid,
                "title": overview.get("title") or "",
                "category_uuid": item.get("category_uuid"),
                "vault_uuid": vuuid,
                "fields": _field_map_from_details(details),
                "urls": _urls_from_overview(overview),
            }
            return overview, details, meta
    raise LookupError(item_uuid)
