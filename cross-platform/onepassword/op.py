"""1Password — local B5 vault decrypt (AgentOS-owned, no Integrate IPC).

Reads ``1password.sqlite`` on disk. Unlock material (master password +
secret key) lives in the per-user AgentOS credential store (``op.local``).
Crypto runs in Rust via ``agentos.crypto.*``.

Two jobs:
1. **Credential providers** — ``@provides(login_credentials|api_key)`` for
   matchmaking. Secrets ride ``__secrets__`` only.
2. **Graph import** — typed getters return ontology shapes. Nested children
   stamped with ``shape:`` become deterministic links (URLs → ``website``,
   licenses → ``software``, holders → ``person``, orgs → ``organization``).
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

# Worker loads this file as module ``m`` — put the plugin dir on path so
# ``vault_local`` resolves for every AgentOS user install.
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from agentos import (
    connection,
    normalize_email,
    provides,
    returns,
    app_error,
    app_secret,
    url as urlmod,
)

from vault_local import (
    find_item as _local_find,
    list_overviews,
    setup_unlock_secret,
    unlock as _local_unlock,
)
from vault_local.unlock import UnlockSession

connection(
    "local",
    description="1Password local vault (B5 sqlite) via AgentOS crypto.",
    client="api",
)

_SETUP_HELP = "https://support.1password.com/secret-key/"
_OP_ORG = {"shape": "organization", "name": "1Password", "url": "https://1password.com"}
# Vault-import secrets MUST NOT use domain "onepassword" — that is the
# plugin's derived credential domain, and multiple identifiers there make
# the executor demand `account=` on every later run.
_VAULT_SECRET_DOMAIN = "op.vault"

_URL_MATCH_SCORE = 100
_TITLE_EXACT_SCORE = 60
_TITLE_WORD_SCORE = 40
_TAG_MATCH_SCORE = 25
_MIN_SCORE = 40

_CARD_SUBTYPE = {
    "visa": "VI",
    "mastercard": "MC",
    "master card": "MC",
    "american express": "AX",
    "amex": "AX",
    "discover": "DS",
    "diners": "DC",
    "jcb": "JC",
    "unionpay": "UP",
}

# Derived vault keys only — not the Master Password. Sliding TTL so a long-
# lived Python worker does not keep decrypt capability forever after unlock.
_SESSION: UnlockSession | None = None
_SESSION_EXPIRES_AT: float = 0.0
_SESSION_TTL_SECS = 30 * 60  # 30 minutes


def _clear_session() -> None:
    global _SESSION, _SESSION_EXPIRES_AT
    _SESSION = None
    _SESSION_EXPIRES_AT = 0.0


class _Multi(Exception):
    def __init__(self, query: str, hits: list[dict]):
        self.query = query
        self.hits = hits
        super().__init__(query)


def _multi_error(exc: _Multi) -> dict[str, Any]:
    return app_error(
        f"Multiple 1Password items match {exc.query!r}. Retry with a tighter `q`.",
        code="MultipleMatches",
        query=exc.query,
        candidates=[
            {"id": h.get("id"), "title": h.get("title"), "category": h.get("category")}
            for h in exc.hits[:12]
        ],
    )


def _catch(exc: Exception) -> dict[str, Any] | None:
    if isinstance(exc, _Multi):
        return _multi_error(exc)
    msg = str(exc)
    low = msg.lower()
    if "not configured" in low or "op.local" in low:
        return app_error(
            msg,
            code="OnePasswordUnlockRequired",
            help_url=_SETUP_HELP,
            hint=(
                "Call setup_local_unlock() once — AgentOS opens a Security "
                "window for your Master Password (Secret Key loads from this Mac)."
            ),
            open=["1Password"],
        )
    if any(s in low for s in ("invalid secret key", "pbes2", "gcm decrypt", "unlock failed")):
        return app_error(
            "Local vault unlock failed — check master password and secret key.",
            code="OnePasswordUnlockFailed",
            hint="Re-run setup_local_unlock with the correct credentials.",
        )
    return None


async def _session() -> UnlockSession:
    """Return a live unlock session; re-derive from ``op.local`` after TTL.

    While ``op.local`` holds unlock material, expiry re-reads the vault
    without prompting. Missing ``op.local`` raises → Security challenge.
    """
    global _SESSION, _SESSION_EXPIRES_AT
    now = time.monotonic()
    if _SESSION is not None and now < _SESSION_EXPIRES_AT:
        _SESSION_EXPIRES_AT = now + _SESSION_TTL_SECS
        return _SESSION
    _SESSION = await _local_unlock()
    _SESSION_EXPIRES_AT = time.monotonic() + _SESSION_TTL_SECS
    return _SESSION


def _f(fields: dict[str, str], *ids: str) -> str | None:
    for i in ids:
        if i in fields and fields[i]:
            return fields[i]
    # Field ids from B5 decrypt can be non-str keys — coerce before .lower().
    lower = {str(k).lower(): v for k, v in fields.items()}
    for i in ids:
        key = str(i).lower()
        if key in lower and lower[key]:
            return lower[key]
    return None


def _urls_of(item: dict) -> list[str]:
    out: list[str] = []
    for u in item.get("urls") or []:
        if isinstance(u, dict):
            href = u.get("href") or u.get("u") or u.get("url")
            if href:
                out.append(str(href))
        elif isinstance(u, str):
            out.append(u)
    return out


async def _list_category(category: str) -> list[dict]:
    sess = await _session()
    return await list_overviews(sess, category=category)


async def _find(category: str | list[str], q: str) -> dict:
    sess = await _session()
    cats = [category] if isinstance(category, str) else list(category)
    last_err: Exception | None = None
    for c in cats:
        try:
            overview, details, meta = await _local_find(sess, c, q)
            return {
                "id": meta["id"],
                "title": meta["title"],
                "category": c,
                "urls": [{"href": u} for u in meta.get("urls") or []],
                "fields": [
                    {"id": k, "value": v} for k, v in (meta.get("fields") or {}).items()
                ],
                "notesPlain": (meta.get("fields") or {}).get("notesPlain"),
                "vault": {"id": meta.get("vault_uuid")},
                "_meta": meta,
                "_overview": overview,
                "_details": details,
            }
        except LookupError as e:
            last_err = e
            if "Multiple" in str(e):
                overs = await list_overviews(sess, category=c, q=q)
                raise _Multi(q, overs) from e
            continue
    raise LookupError(q) from last_err


def _field_map(item: dict) -> dict[str, str]:
    if item.get("_meta"):
        return dict(item["_meta"].get("fields") or {})
    out: dict[str, str] = {}
    for f in item.get("fields") or []:
        if isinstance(f, dict) and f.get("id") is not None and f.get("value") is not None:
            out[str(f["id"])] = str(f["value"])
    if item.get("notesPlain"):
        out["notesPlain"] = str(item["notesPlain"])
    return out


def _op_meta(item: dict) -> dict[str, Any]:
    return {
        "op_item_id": item.get("id"),
        "op_item_title": item.get("title"),
        "op_vault_id": (item.get("vault") or {}).get("id")
        if isinstance(item.get("vault"), dict)
        else None,
        "op_backend": "local",
    }


def _website_node(href: str) -> dict[str, Any] | None:
    href = (href or "").strip()
    if not href:
        return None
    if not re.match(r"^https?://", href, re.I):
        href = "https://" + href
    try:
        parts = urlmod.parse(href)
    except Exception:
        return None
    host_port = (getattr(parts, "host", None) or "").lower()
    if not host_port:
        return None
    host = host_port.split(":", 1)[0]
    scheme = (getattr(parts, "scheme", None) or "https").lower()
    origin = f"{scheme}://{host}"
    return {"shape": "website", "url": origin, "name": host}


def _org_node(name: str, href: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"shape": "organization", "name": name}
    if href and (w := _website_node(href)):
        node["url"] = w["url"]
        node["about"] = w
    return node


def _software_node(*, name: str, href: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"shape": "software", "name": name}
    if href and (w := _website_node(href)):
        node["url"] = w["url"]
        node["about"] = w
    return node


def _as_text(v: Any) -> str:
    """Coerce overview/field values that may be non-str from B5 JSON."""
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _title_match(item: dict, q: str) -> bool:
    q = q.strip().lower()
    if not q:
        return False
    blob = _as_text(item.get("title")).lower()
    blob += " " + " ".join(_as_text(u) for u in _urls_of(item)).lower()
    return q in blob


def _last4(s: str | None) -> str | None:
    if not s or len(s) < 4:
        return None
    return s[-4:]


def _score_login(item: dict, domain: str) -> int:
    stripped = domain.strip().lstrip(".").lower()
    if not stripped:
        return 0
    target = urlmod.registrable(stripped)
    root = target.split(".", 1)[0]
    score = 0
    for href in _urls_of(item):
        try:
            host = getattr(urlmod.parse(href), "host", "") or ""
        except Exception:
            continue
        if host and urlmod.same_site(host, target):
            score += _URL_MATCH_SCORE
            break
    title_lc = _as_text(item.get("title")).lower()
    if title_lc:
        words = title_lc.replace("-", " ").replace("_", " ").split()
        if title_lc == root:
            score += _TITLE_EXACT_SCORE
        elif root in words:
            score += _TITLE_WORD_SCORE
    for tag in item.get("tags") or []:
        if root in _as_text(tag).lower():
            score += _TAG_MATCH_SCORE
            break
    return score


def _not_found(q: str) -> dict[str, Any]:
    return app_error(f"No 1Password item matches {q!r}.", code="NotFound", query=q)


# ---------------------------------------------------------------------------
# Credential providers
# ---------------------------------------------------------------------------


async def _get_by_id(item_id: str) -> dict:
    from vault_local.items import get_item_details

    sess = await _session()
    overview, details, meta = await get_item_details(sess, item_id)
    return {
        "id": meta["id"],
        "title": meta["title"],
        "urls": [{"href": u} for u in meta.get("urls") or []],
        "fields": [{"id": k, "value": v} for k, v in (meta.get("fields") or {}).items()],
        "notesPlain": (meta.get("fields") or {}).get("notesPlain"),
        "vault": {"id": meta.get("vault_uuid")},
        "_meta": meta,
        "_overview": overview,
        "_details": details,
    }


def _for_app_from(params: dict[str, Any]) -> str | None:
    v = params.get("forApp") or params.get("for_app")
    if isinstance(v, str) and v.strip() and v.strip() != "onepassword":
        return v.strip()
    return None


async def _mint_unlock_challenge(*, for_app: str | None = None) -> dict[str, Any]:
    """Open AgentOS Security for Master Password; SK stays engine-private."""
    from agentos._bridge import dispatch
    from vault_local import db as vdb
    from vault_local.secret_key import load_secret_key_from_local_db

    account = await vdb.get_account()
    email = account.get("user_email") or ""
    try:
        sk = await load_secret_key_from_local_db()
    except Exception as e:
        return app_error(
            f"Could not load Secret Key from the 1Password app: {e}",
            code="OnePasswordSecretKeyMissing",
            hint="Open 1Password on this Mac once, or pass secret_key= for tests.",
        )
    body: dict[str, Any] = {
        "plugin": "onepassword",
        "title": "Unlock 1Password for AgentOS",
        "reason": (
            "Enter your 1Password Master Password once. AgentOS will unlock "
            "the local vault on this Mac to provide credentials and import items."
        ),
        "account": email or None,
        "notes": [
            "Secret Key loaded from the 1Password app on this Mac",
            "Saved only in your AgentOS credential vault (op.local)",
        ],
        "fields": [
            {
                "name": "masterPassword",
                "label": "Master Password",
                "kind": "password",
            }
        ],
        "store": {
            "domain": "op.local",
            "itemType": "vault_unlock",
            "identifier": "default",
            "source": "onepassword",
        },
        "private": {"secretKey": sk},
        "verify": {"app": "onepassword", "tool": "verify_local_unlock"},
    }
    if for_app:
        body["forApp"] = for_app
    challenge = await dispatch("security.challenge", body)
    return {
        "ok": False,
        "status": "pending",
        "provided": False,
        "identifier": "default",
        "challengeId": challenge.get("id"),
        "prompt": "secret_challenge",
        "forApp": for_app,
        "message": (
            "AgentOS Security is open — enter your Master Password in the "
            "desktop window. Secrets are not returned to the agent."
        ),
    }


@returns({"ok": "boolean", "identifier": "string", "status": "string", "challengeId": "string"})
@connection("local")
async def setup_local_unlock(
    *,
    master_password: str | None = None,
    secret_key: str | None = None,
    **params,
) -> dict[str, Any]:
    """Store unlock material for this user.

    Prefer omitting ``master_password`` — AgentOS opens the Security window
    for the human to type it. The Secret Key is loaded from the local
    1Password DB when omitted. Agents never receive plaintext secrets.

    Legacy/tests: pass ``master_password`` (and optionally ``secret_key``)
    to verify + persist in one call via ``__secrets__``.
    """
    global _SESSION
    from vault_local import db as vdb
    from vault_local import crypto_flow
    from vault_local.secret_key import load_secret_key_from_local_db

    account = await vdb.get_account()
    email = account.get("user_email") or ""
    for_app = _for_app_from(params)

    # Interactive path — open AgentOS Security; SK stays in engine private.
    if not master_password:
        return await _mint_unlock_challenge(for_app=for_app)

    # Legacy / tests — verify then persist via __secrets__.
    try:
        sk = (secret_key or "").strip() or await load_secret_key_from_local_db()
    except Exception as e:
        return app_error(
            f"Secret Key required: {e}",
            code="OnePasswordSecretKeyMissing",
            hint="Pass secret_key=A3-… or open 1Password so AgentOS can load it.",
        )
    secret = setup_unlock_secret(master_password=master_password, secret_key=sk)
    _clear_session()
    try:
        keyset = await vdb.get_primary_keyset(account["account_uuid"])
        sym_json = await crypto_flow.decrypt_pbes2(
            keyset["encSymKey"],
            secret_key=sk,
            password=master_password,
            email=email,
        )
        crypto_flow.extract_oct_key(sym_json)
    except Exception as e:
        return app_error(
            f"Unlock verification failed: {e}",
            code="OnePasswordUnlockFailed",
            hint="Check master password and secret key (A3-…).",
        )
    return {
        "__secrets__": [secret],
        "__result__": {"ok": True, "identifier": "default", "status": "completed"},
    }


@returns({"ok": "boolean"})
@connection("local")
async def verify_local_unlock(
    *,
    master_password: str | None = None,
    secret_key: str | None = None,
    **params,
) -> dict[str, Any]:
    """PBES2 probe used by ``security.submit`` — never echoes secrets.

    Accepts camelCase aliases (``masterPassword`` / ``secretKey``) from the
    engine merge path.
    """
    from vault_local import db as vdb
    from vault_local import crypto_flow

    mp = master_password or params.get("masterPassword")
    sk = secret_key or params.get("secretKey")
    if not mp or not sk:
        return app_error(
            "master_password and secret_key are required",
            code="OnePasswordUnlockFailed",
        )
    sk = str(sk).strip()
    try:
        account = await vdb.get_account()
        email = account.get("user_email") or ""
        keyset = await vdb.get_primary_keyset(account["account_uuid"])
        sym_json = await crypto_flow.decrypt_pbes2(
            keyset["encSymKey"],
            secret_key=sk,
            password=str(mp),
            email=email,
        )
        crypto_flow.extract_oct_key(sym_json)
    except Exception as e:
        return app_error(
            f"Unlock verification failed: {e}",
            code="OnePasswordUnlockFailed",
            hint="Check master password.",
        )
    return {"ok": True}


@returns({"provided": "boolean", "identifier": "string"})
@provides("login_credentials", description="Reads {email, password} from 1Password Login items matching a domain")
@connection("local")
async def get_credentials(*, domain: str, account: str | None = None, **params) -> dict[str, Any]:
    """Match a Login item for ``domain``; return email/password via ``__secrets__``."""
    for_app = _for_app_from(params)
    try:
        items = await _list_category("Login")
        scored = [(it, _score_login(it, domain)) for it in items]
        scored = [(it, s) for it, s in scored if s >= _MIN_SCORE]
        if account:
            acc = account.lower()
            narrowed = [(it, s) for it, s in scored if _as_text(it.get("title")).lower() == acc]
            if narrowed:
                scored = narrowed
        if not scored:
            return {"provided": False}
        scored.sort(key=lambda x: x[1], reverse=True)
        top_score = scored[0][1]
        tied = [it for it, s in scored if s == top_score]
        if len(tied) > 1:
            return app_error(
                f"Multiple 1Password items match {domain!r}. Retry with `account=`.",
                code="MultipleMatches",
                domain=domain,
                candidates=[
                    {"id": t.get("id"), "title": t.get("title"), "urls": _urls_of(t)}
                    for t in tied
                ],
            )
        item = await _get_by_id(tied[0]["id"])
        fields = _field_map(item)
        username = _f(fields, "username")
        password = _f(fields, "password")
        if not username or not password:
            return {"provided": False}
        identifier = normalize_email(username) if "@" in username else username
        secret = app_secret(
            domain=domain,
            identifier=identifier,
            item_type="login_credentials",
            value={"email": identifier, "password": password},
            source="onepassword",
            metadata={"masked": {"password": "••••••••"}, **_op_meta(item)},
        )
        return {
            "__secrets__": [secret],
            "__result__": {"provided": True, "identifier": identifier},
        }
    except Exception as e:
        # Mid-retrieve unlock: open Security with For: <consumer app> when
        # services.call stamped forApp (e.g. SwitchYards login).
        msg = str(e).lower()
        if "not configured" in msg or "op.local" in msg:
            pending = await _mint_unlock_challenge(for_app=for_app)
            if isinstance(pending, dict) and pending.get("prompt") == "secret_challenge":
                return app_error(
                    pending.get("message")
                    or "Unlock 1Password in AgentOS Security, then retry.",
                    code="OnePasswordUnlockRequired",
                    challengeId=pending.get("challengeId"),
                    forApp=for_app,
                    hint=(
                        "Enter your Master Password in the AgentOS Security window, "
                        "then retry the login."
                    ),
                )
            return pending
        if (err := _catch(e)) is not None:
            return err
        raise


@returns({"provided": "boolean", "identifier": "string"})
@provides("api_key", description="Reads API keys from 1Password API Credential items by service name")
@connection("local")
async def get_api_key(*, service: str, account: str | None = None, **params) -> dict[str, Any]:
    try:
        items = await _list_category("API Credential")
        matching = [
            it for it in items if service.lower() in _as_text(it.get("title")).lower()
        ]
        if account:
            filtered = [
                it
                for it in matching
                if _as_text(it.get("title")).lower() == account.lower()
            ]
            if filtered:
                matching = filtered
        if not matching:
            return {"provided": False}
        item = await _get_by_id(matching[0]["id"])
        fields = _field_map(item)
        key_value = (
            _f(fields, "credential", "password", "api_key", "apikey", "token", "secret")
            or next(
                (
                    v
                    for k, v in fields.items()
                    if v
                    and any(
                        tip in _as_text(k).lower()
                        for tip in ("key", "token", "secret", "credential", "password")
                    )
                ),
                None,
            )
            or _f(fields, "notesPlain")
        )
        if not key_value:
            return {"provided": False}
        identifier = _as_text(item.get("title") or service).strip()
        secret = app_secret(
            domain=service,
            identifier=identifier,
            item_type="api_key",
            value={"key": key_value},
            source="onepassword",
            metadata={
                "masked": {"key": "••••" + key_value[-4:] if len(key_value) >= 4 else "••••"},
                **_op_meta(item),
            },
        )
        return {
            "__secrets__": [secret],
            "__result__": {"provided": True, "identifier": identifier},
        }
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


# ---------------------------------------------------------------------------
# Graph import — shapes + deterministic nested links
# ---------------------------------------------------------------------------


@returns("secure_note")
@provides("secure_note", description="Import a 1Password Secure Note (body in __secrets__)")
@connection("local")
async def get_secure_note(*, q: str, **params) -> dict[str, Any]:
    try:
        item = await _find("Secure Note", q)
        fields = _field_map(item)
        body = _f(fields, "notesPlain") or ""
        if not body:
            return _not_found(q)
        iid = item.get("id") or q
        secret = app_secret(
            domain=_VAULT_SECRET_DOMAIN,
            identifier=f"secure_note:{iid}",
            item_type="secure_note",
            value={"body": body},
            source="onepassword",
            metadata={"masked": {"body": f"••••({len(body)} chars)"}, **_op_meta(item)},
        )
        node = {
            "name": item.get("title") or q,
            "at": dict(_OP_ORG),
            "id": iid,
            "secretRef": iid,
            "category": "secure_note",
        }
        return {"__secrets__": [secret], "__result__": node}
    except LookupError:
        return _not_found(q)
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


@returns("payment_method")
@provides("payment_method", description="Import a 1Password Credit Card as PCI-safe payment_method")
@connection("local")
async def get_credit_card(*, q: str, **params) -> dict[str, Any]:
    try:
        item = await _find("Credit Card", q)
        fields = _field_map(item)
        number = _f(fields, "ccnum", "number")
        cvv = _f(fields, "cvv")
        holder = _f(fields, "cardholder") or ""
        brand_raw = (_f(fields, "type") or "").strip().lower()
        subtype = _CARD_SUBTYPE.get(brand_raw, brand_raw.upper()[:2] if brand_raw else None)
        brand = brand_raw.title() if brand_raw else None
        last4 = _last4(number)
        iid = item.get("id") or q
        secrets = []
        if number or cvv:
            secrets.append(
                app_secret(
                    domain=_VAULT_SECRET_DOMAIN,
                    identifier=f"credit_card:{iid}",
                    item_type="credit_card",
                    value={
                        k: v
                        for k, v in {"number": number, "cvv": cvv, "holderName": holder}.items()
                        if v
                    },
                    source="onepassword",
                    metadata={
                        "masked": {
                            "number": f"••••{last4}" if last4 else "••••",
                            "cvv": "•••" if cvv else None,
                        },
                        **_op_meta(item),
                    },
                )
            )
        node = {
            "name": item.get("title") or q,
            "at": dict(_OP_ORG),
            "identifier": f"op:{iid}",
            "type": "card",
            "subtype": subtype,
            "brand": brand,
            "displayName": (
                f"{brand or 'Card'} ••••{last4}" if last4 else (item.get("title") or "Card")
            ),
            "holderName": holder or None,
            "last4": last4,
            "customDescription": item.get("title"),
            "status": "active",
        }
        return {"__secrets__": secrets, "__result__": node}
    except LookupError:
        return _not_found(q)
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


@returns("person")
@connection("local")
async def get_identity(*, q: str, **params) -> dict[str, Any]:
    try:
        item = await _find("Identity", q)
        fields = _field_map(item)
        given = _f(fields, "firstname", "first_name") or ""
        family = _f(fields, "lastname", "last_name") or ""
        phone = _f(fields, "defphone", "cellphone", "homephone", "phone")
        email = _f(fields, "email")
        address = _f(fields, "address")
        identities: list[dict] = [{"platform": "onepassword", "id": item.get("id")}]
        if phone:
            identities.append({"platform": "phone", "id": phone})
        if email:
            identities.append({"platform": "email", "id": normalize_email(email)})
        name = " ".join(p for p in (given, family) if p) or (item.get("title") or q)
        return {
            "name": name,
            "givenName": given or None,
            "familyName": family or None,
            "birthDate": _f(fields, "birthdate"),
            "gender": _f(fields, "sex", "gender"),
            "notes": address,
            "identities": identities,
            "jobTitle": _f(fields, "jobtitle"),
        }
    except LookupError:
        return _not_found(q)
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


@returns("government_id")
@connection("local")
async def get_government_id(*, q: str, **params) -> dict[str, Any]:
    try:
        item = await _find(["Driver License", "Passport"], q)
        fields = _field_map(item)
        cat_raw = (item.get("category") or "").upper()
        if "PASSPORT" in cat_raw:
            node: dict[str, Any] = {
                "name": item.get("title") or q,
                "category": "passport",
                "identifier": _f(fields, "number"),
                "issuingCountry": _f(fields, "issuing_country", "country") or "",
                "fullName": _f(fields, "fullname"),
                "sex": _f(fields, "sex", "gender"),
                "nationality": _f(fields, "nationality"),
                "issueDate": _f(fields, "issue_date"),
                "expiryDate": _f(fields, "expiry_date"),
                "placeOfIssue": _f(fields, "issuing_authority"),
                "birthDate": _f(fields, "birthdate"),
                "status": "active",
            }
        else:
            node = {
                "name": item.get("title") or q,
                "category": "driver_license",
                "identifier": _f(fields, "number"),
                "issuingCountry": _f(fields, "country") or "US",
                "issuingState": _f(fields, "state"),
                "fullName": _f(fields, "fullname"),
                "birthDate": _f(fields, "birthdate"),
                "class": _f(fields, "class"),
                "status": "active",
            }
            dates = sorted(
                v
                for k, v in fields.items()
                if re.match(r"^\d{4}-\d{2}-\d{2}$", v or "") and k != "birthdate"
            )
            if dates:
                node["issueDate"] = dates[0]
                if len(dates) > 1:
                    node["expiryDate"] = dates[-1]
        if node.get("fullName"):
            parts = str(node["fullName"]).split()
            node["held_by"] = {
                "shape": "person",
                "name": node["fullName"],
                "givenName": parts[0] if parts else None,
                "familyName": parts[-1] if len(parts) > 1 else None,
                "identities": [{"platform": "onepassword", "id": item.get("id")}],
            }
        return node
    except LookupError:
        return _not_found(q)
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


@returns("membership")
@connection("local")
async def get_membership(*, q: str, **params) -> dict[str, Any]:
    try:
        # Membership first; Rewards + Passport as fallbacks (vaults often
        # file Costco / PADI / club cards under those templates).
        item = await _find(["Membership", "Rewards", "Passport"], q)
        fields = _field_map(item)
        mid = _f(fields, "membership_no", "member_id", "number") or item.get("id")
        org_name = _f(fields, "org_name") or (item.get("title") or q)
        website = _f(fields, "website")
        node: dict[str, Any] = {
            "name": item.get("title") or org_name,
            "at": _org_node(org_name, website),
            "id": mid,
            "tier": item.get("title"),
            "status": "active",
        }
        if website and (w := _website_node(website)):
            node["about"] = w
        pin = _f(fields, "pin")
        if pin:
            secret = app_secret(
                domain=_VAULT_SECRET_DOMAIN,
                identifier=f"membership:{mid}",
                item_type="membership_pin",
                value={"pin": pin},
                source="onepassword",
                metadata={"masked": {"pin": "••••"}, **_op_meta(item)},
            )
            return {"__secrets__": [secret], "__result__": node}
        return node
    except LookupError:
        return _not_found(q)
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


@returns("server")
@connection("local")
async def get_server(*, q: str, **params) -> dict[str, Any]:
    try:
        item = await _find("Server", q)
        fields = _field_map(item)
        host_raw = _f(fields, "url", "hostname", "admin_console_url") or ""
        username = _f(fields, "username") or ""
        password = _f(fields, "password")
        protocol = "ssh"
        host = host_raw
        port = None
        if host_raw.startswith("http"):
            parts = urlmod.parse(host_raw)
            protocol = (parts.scheme or "https").lower()
            host_port = (parts.host or host_raw).lower()
            if ":" in host_port:
                host, _, p = host_port.rpartition(":")
                if p.isdigit():
                    port = int(p)
            else:
                host = host_port
        elif ":" in host_raw and not host_raw.startswith("["):
            h, _, p = host_raw.rpartition(":")
            if p.isdigit():
                host, port = h, int(p)
        iid = item.get("id") or q
        node: dict[str, Any] = {
            "name": item.get("title") or host,
            "hostname": host,
            "protocol": protocol,
            "username": username,
            "port": port,
            "secretRef": iid if password else None,
        }
        if host_raw.startswith("http") and (w := _website_node(host_raw)):
            node["about"] = w
        elif host and "." in host and (w := _website_node(f"https://{host}")):
            node["about"] = w
        if password:
            secret = app_secret(
                domain=_VAULT_SECRET_DOMAIN,
                identifier=f"server:{iid}",
                item_type="server_password",
                value={"username": username, "password": password, "hostname": host},
                source="onepassword",
                metadata={"masked": {"password": "••••••••"}, **_op_meta(item)},
            )
            return {"__secrets__": [secret], "__result__": node}
        return node
    except LookupError:
        return _not_found(q)
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


@returns("software_license")
@connection("local")
async def get_software_license(*, q: str, **params) -> dict[str, Any]:
    try:
        item = await _find("Software License", q)
        fields = _field_map(item)
        key = _f(fields, "reg_code", "license_key")
        product = item.get("title") or q
        version = _f(fields, "product_version")
        licensed_to = _f(fields, "reg_email", "reg_name", "licensed_to") or ""
        download = _f(fields, "download_link", "publisher_website")
        iid = item.get("id") or q
        node: dict[str, Any] = {
            "name": product,
            "productName": product,
            "version": version,
            "licensedTo": licensed_to or None,
            "licenseKeyLast4": _last4(key),
            "secretRef": iid if key else None,
            "orderNumber": _f(fields, "order_number"),
            "purchaseDate": _f(fields, "order_date"),
            # Deterministic: this license entitles that software product.
            "licenses": _software_node(name=product, href=download),
        }
        if key:
            secret = app_secret(
                domain=_VAULT_SECRET_DOMAIN,
                identifier=f"software_license:{iid}",
                item_type="software_license",
                value={"key": key, "licensedTo": licensed_to, "productName": product},
                source="onepassword",
                metadata={
                    "masked": {"key": "••••" + (key[-4:] if len(key) >= 4 else "")},
                    **_op_meta(item),
                },
            )
            return {"__secrets__": [secret], "__result__": node}
        return node
    except LookupError:
        return _not_found(q)
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


@returns("account")
@connection("local")
async def get_login(*, q: str, **params) -> dict[str, Any]:
    """Import a Login item onto the graph as account + website (+ secrets)."""
    try:
        item = await _find("Login", q)
        fields = _field_map(item)
        username = _f(fields, "username") or ""
        password = _f(fields, "password")
        identifier = normalize_email(username) if "@" in username else username
        if not identifier:
            identifier = item.get("id") or q
        urls = _urls_of(item)
        websites = [w for u in urls if (w := _website_node(u))]
        # Namespace: prefer first website host as org-ish product surface,
        # else 1Password itself.
        if websites:
            host = websites[0]["name"]
            at_node = {
                "shape": "organization",
                "name": host,
                "about": websites[0],
                "url": websites[0]["url"],
            }
        else:
            at_node = dict(_OP_ORG)
        node: dict[str, Any] = {
            "name": item.get("title") or identifier,
            "at": at_node,
            "identifier": identifier,
            "email": identifier if "@" in identifier else None,
            "displayName": item.get("title"),
            "userId": item.get("id"),
        }
        if websites:
            node["for_site"] = websites[0] if len(websites) == 1 else websites
        secrets = []
        if username and password:
            # Credential-store domain from first URL when possible.
            domain = ".local"
            if websites:
                try:
                    domain = "." + urlmod.registrable(websites[0]["name"])
                except Exception:
                    domain = "." + websites[0]["name"]
            secrets.append(
                app_secret(
                    domain=domain,
                    identifier=identifier,
                    item_type="login_credentials",
                    value={"email": identifier, "password": password},
                    source="onepassword",
                    metadata={"masked": {"password": "••••••••"}, **_op_meta(item)},
                )
            )
        if secrets:
            return {"__secrets__": secrets, "__result__": node}
        return node
    except LookupError:
        return _not_found(q)
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise


@returns({"items": "json", "count": "integer"})
@connection("local")
async def list_items(
    *,
    category: str | None = None,
    q: str | None = None,
    **params,
) -> dict[str, Any]:
    """List vault item overviews (titles/urls only — no secrets)."""
    try:
        cats = (
            [category]
            if category
            else [
                "Login",
                "Secure Note",
                "Credit Card",
                "Identity",
                "Driver License",
                "Passport",
                "Membership",
                "Server",
                "Software License",
                "API Credential",
            ]
        )
        items: list[dict] = []
        for c in cats:
            try:
                batch = await _list_category(c)
            except RuntimeError:
                continue
            for it in batch:
                if q and not _title_match(it, q):
                    continue
                items.append(
                    {
                        "id": it.get("id"),
                        "title": it.get("title"),
                        "category": it.get("category") or c,
                        "urls": _urls_of(it),
                        "vault": (it.get("vault") or {}).get("name")
                        if isinstance(it.get("vault"), dict)
                        else None,
                    }
                )
        return {"items": items, "count": len(items)}
    except Exception as e:
        if (err := _catch(e)) is not None:
            return err
        raise
