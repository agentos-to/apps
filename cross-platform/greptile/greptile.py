"""greptile.py — Dashboard session + org member management for Greptile.

Auth architecture
-----------------
Greptile's dashboard (app.greptile.com) uses **Auth.js v5** (the rebrand of
NextAuth) fronted by an OAuth login service at auth.greptile.com: an
unauthenticated visit to any app page redirects to
``auth.greptile.com/login?login_challenge=…`` — an email+password form (plus
GitHub/GitLab/Google OAuth buttons).

Every op runs **inside a tab of the engine-owned browser** via the
``browser_session`` service (the Exa/WhatsApp pattern). The session is the
browser profile itself — ``__Secure-authjs.session-token`` on
``app.greptile.com``, written by Auth.js's own Set-Cookie, never extracted,
never vaulted, never seen by this app. Both hosts share one ``greptile.com``
tab; where the tab lands IS the auth signal, and each op body branches on
``__onApp``.

The dashboard session response ALSO contains a ``greptileToken`` — a
short-lived HS256 JWT the frontend passes as ``Authorization: Bearer`` to
the backend API at api.greptile.com (cross-origin from the tab, exactly as
the real frontend does). Org / people / invites live on the dashboard's
tRPC route at ``/api/trpc`` instead.
"""

import json

from agentos import account, app_error, claims, connection, credentials, normalize_email, returns, test, timeout, services


DASHBOARD_BASE = "https://app.greptile.com"
BACKEND_BASE = "https://api.greptile.com"

# Invite link format pulled from the "Copy Invite Link" button's onClick in
# chunk 164: `${appUrl}/invitation?token=${token}`.
INVITE_URL_TEMPLATE = f"{DASHBOARD_BASE}/invitation?token={{token}}"

# Valid role values from the bundle (chunks reference `n.X.ADMIN`, `n.X.MEMBER`).
VALID_ROLES = ("ADMIN", "MEMBER")

# browser_session target — a URL substring; the engine opens
# https://<target>/ in the engine-owned browser when no tab matches.
# auth.greptile.com shares the same registrable-domain tab.
_APP = "app.greptile.com"


# Readiness wait, prepended to every op body. A freshly opened tab is still
# at about:blank when our JS first runs — a relative-URL fetch has no origin
# to resolve against. We wait for the *family*, not a specific host, because
# an unauthenticated visit bounces to auth.greptile.com: where the tab lands
# IS the auth signal, and each body branches on `__onApp`.
_PRELUDE = """
const __deadline = Date.now() + 15000;
while (location.hostname.indexOf('greptile.com') === -1 || document.readyState !== 'complete') {
  if (Date.now() > __deadline) return { __error: 'tab_not_ready' };
  await new Promise(r => setTimeout(r, 200));
}
const __onApp = location.hostname === 'app.greptile.com';
"""


async def _eval(body: str, *, timeout_s: int = 45):
    """Run an op body inside the greptile.com tab in the engine-owned browser.

    The engine matchmakes the `browser_session` provider, opens the tab (and
    launches the browser) when needed, and returns the JS value. The body
    runs once the tab has settled on a greptile.com origin, with `__onApp`
    in scope telling it whether we're on the authenticated dashboard host or
    were bounced to the auth host (logged out).
    """
    return await services.call(services.browser_session, params={
        "target": _APP,
        "js": "(async () => {\n" + _PRELUDE + body + "\n})()",
        "timeout": timeout_s,
    })


# Session probe. On the auth host we're logged out — no need to ask. On the
# app host Auth.js returns `{}` (not an error) for anonymous visitors, so
# `user` is the live signal.
_SESSION_JS = """
if (!__onApp) return null;
const r = await fetch('/api/auth/session', { cache: 'no-store' });
if (!r.ok) return { __error: 'http_' + r.status };
const data = await r.json().catch(() => null);
return (data && data.user) ? data : null;
"""


async def _get_session() -> dict | None:
    """Live Auth.js session from the dashboard tab, or None."""
    value = await _eval(_SESSION_JS)
    if not isinstance(value, dict) or not value.get("user"):
        return None
    return value


async def _require_session() -> dict:
    session = await _get_session()
    if not session:
        raise RuntimeError(
            "SESSION_EXPIRED: no live Greptile session in the AgentOS browser "
            "profile — run greptile.login to sign in."
        )
    return session


def _needs_auth():
    return app_error(
        "No live Greptile dashboard session in the AgentOS browser profile. "
        "Run greptile.login — it drives the email+password sign-in form at "
        "auth.greptile.com with vault credentials. OAuth-only accounts "
        "(GitHub/GitLab/Google SSO) need a human: call the `login_window` "
        "service with url=https://auth.greptile.com, poll check_session "
        "until authenticated, then login_window(close=true).",
        code="NeedsAuth",
        login_url="https://auth.greptile.com",
    )


def _org_from_session(session: dict) -> dict:
    user = session.get("user", {}) or {}
    current = user.get("currentTenantExternalId")
    orgs = user.get("organizations") or []
    if current:
        for o in orgs:
            if o.get("tenantExternalId") == current:
                return o
    return orgs[0] if orgs else {}


def _greptile_account_from_user(user: dict, org: dict) -> dict:
    """Map the logged-in user + current org into an account shape.

    Identity is graph-native per docs/shapes/account.yaml:
    `(at, identifier)` where `at` is a relation to the namespace
    (here: the Greptile product node). The engine creates / dedups
    the product node from the inline `{shape: "product", url, name}`
    sidecar.
    """
    return {
        "id": f"greptile:{user.get('greptileId')}",
        "at": {"shape": "product", "url": "https://greptile.com", "name": "Greptile"},
        "identifier": user.get("email") or "",
        "email": user.get("email"),
        "handle": user.get("email"),
        "displayName": user.get("name") or user.get("email"),
        "image": user.get("image"),
        "accountType": (org.get("role") or "MEMBER").lower(),
        "isActive": True,
        "url": f"{DASHBOARD_BASE}/settings/organization/people",
    }


def _member_to_account(item: dict) -> dict:
    """Map a searchPeople item into an `account` shape dict.

    Items look like: {email, role, token, type}. `type` is `"member"` for real
    org members and `"invite"` for pending email invites. `token` is non-null
    only on invite rows — use that as a second signal.

    Pending invites get `accountType:"invite"` + `isActive:false` so callers
    can tell them apart from real members without inspecting a `type` field
    that isn't part of the account shape.
    """
    email = item.get("email") or ""
    role = item.get("role") or "MEMBER"
    itype = (item.get("type") or "member").lower()
    is_invite = itype == "invite" or bool(item.get("token"))
    return {
        "identifier": email,
        "at": {"shape": "product", "url": "https://greptile.com", "name": "Greptile"},
        "email": email,
        "handle": email,
        "displayName": email,
        "accountType": "invite" if is_invite else role.lower(),
        "isActive": not is_invite,
        "url": f"{DASHBOARD_BASE}/settings/organization/people",
    }


def _item_to_invitation(item: dict, tenant_external_id: str) -> dict:
    """Map a searchPeople invite row into an `invitation` shape dict."""
    email = item.get("email") or ""
    token = item.get("token") or ""
    return {
        "id": token or email,
        "invitationType": "organization",
        "email": email,
        "role": (item.get("role") or "MEMBER").lower(),
        "status": "pending",
        "token": token,
        "url": f"{DASHBOARD_BASE}/settings/organization/people",
    }


def _normalize_role(role: str | None) -> str:
    """Uppercase + validate a role string. Raises on unknown roles."""
    if not role:
        return "MEMBER"
    r = role.strip().upper()
    if r not in VALID_ROLES:
        raise ValueError(f"Invalid role {role!r}; expected one of {VALID_ROLES}")
    return r


# ---------------------------------------------------------------------------
# tRPC helpers — GET for queries, POST for mutations, both same-origin
# fetch() inside the app tab. All requests hit /api/trpc/<procedure> with
# the "superjson" envelope `{json: {...}}`.
# ---------------------------------------------------------------------------


async def _trpc_query(procedure: str, input_args: dict) -> dict:
    """GET /api/trpc/<procedure>?input=<urlencoded json> inside the tab."""
    payload = json.dumps({"json": input_args}, separators=(",", ":"))
    return await _eval(f"""
if (!__onApp) return {{ __error: 'needs_auth' }};
const u = '/api/trpc/{procedure}?input=' + encodeURIComponent({json.dumps(payload)});
const r = await fetch(u, {{ cache: 'no-store' }});
const body = await r.json().catch(() => null);
return {{ status: r.status, json: body }};
""")


async def _trpc_mutate(procedure: str, input_args: dict) -> dict:
    """POST /api/trpc/<procedure> with {json: {...}} body inside the tab."""
    payload = json.dumps({"json": input_args}, separators=(",", ":"))
    return await _eval(f"""
if (!__onApp) return {{ __error: 'needs_auth' }};
const r = await fetch('/api/trpc/{procedure}', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: {json.dumps(payload)},
}});
const body = await r.json().catch(() => null);
return {{ status: r.status, json: body }};
""")


def _unwrap_trpc(resp, *, procedure: str) -> dict:
    """Return the inner `result.data.json` payload or raise with the tRPC error.

    Shape: {"result":{"data":{"json":<payload>}}} on success.
             {"error":{"json":{"message":...,"code":...}}} on failure.
    """
    if not isinstance(resp, dict):
        raise RuntimeError(f"tRPC {procedure}: tab eval returned {resp!r}")
    if resp.get("__error") == "needs_auth":
        raise RuntimeError(
            "SESSION_EXPIRED: no live Greptile session — run greptile.login."
        )
    if resp.get("__error"):
        raise RuntimeError(f"tRPC {procedure} failed in the tab: {resp['__error']}")
    status = resp.get("status")
    body = resp.get("json")
    if not isinstance(body, dict):
        raise RuntimeError(
            f"tRPC {procedure} returned non-JSON body (status={status})"
        )
    if "error" in body:
        err = body["error"]
        inner = err.get("json") if isinstance(err, dict) else None
        msg = (inner or {}).get("message") or str(err)
        code = (inner or {}).get("code") or (inner or {}).get("data", {}).get("code")
        raise RuntimeError(f"tRPC {procedure} failed ({code}, status={status}): {msg}")
    if status and status >= 400:
        raise RuntimeError(f"tRPC {procedure} HTTP {status}: {body!r}")
    try:
        return body["result"]["data"]["json"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"tRPC {procedure} unexpected envelope: {body!r}") from e


async def _resolve_tenant_id(tenant_id: str | None = None) -> str:
    """Return the tenant external id — provided, else from session."""
    if tenant_id:
        return tenant_id
    session = await _require_session()
    org = _org_from_session(session)
    tid = org.get("tenantExternalId")
    if not tid:
        raise RuntimeError("Could not resolve tenant external id from session")
    return tid


# ---------------------------------------------------------------------------
# The account trio — check_session / login / logout, browser-driven
# ---------------------------------------------------------------------------


@account.check
@test
@returns("account")
@claims("primary_user")
@connection("none")
@timeout(60)
async def check_session(**params) -> dict:
    """Verify the Greptile dashboard session and identify the logged-in user + org.

    The session lives in the engine-owned browser profile; this op asks
    Auth.js from inside the app tab. No cookie ever reaches the app.
    """
    session = await _get_session()
    if not session:
        return {"authenticated": False}
    user = session.get("user", {}) or {}
    org = _org_from_session(session)
    label = user.get("email")
    if org.get("name"):
        label = f"{user.get('email')} @ {org['name']} ({org.get('role','MEMBER')})"
    acct = _greptile_account_from_user(user, org)
    return {
        **acct,
        "authenticated": True,
        "identifier": user.get("email") or "",
        "display": label,
    }


# React-safe value set: the native setter + input/change events. A plain
# `el.value = …` is ignored by the framework's controlled inputs.
_REACT_SET = """
const __setVal = (el, v) => {
  const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  s.call(el, v);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
};
"""

# If the tab is parked on a stale app.greptile.com SPA page with a dead
# session, reload the app root — the server bounces us to the auth host's
# sign-in form. Navigation kills this eval's context, so we just kick it
# off and let the *next* eval's prelude wait for the page to settle.
_GOTO_LOGIN_JS = """
if (location.hostname === 'auth.greptile.com'
    && document.querySelector('input[name=email]')) return { ready: true };
location.replace('https://app.greptile.com/');
return { navigating: true };
"""

# Drive the real email+password form at auth.greptile.com (an OAuth
# login_challenge flow in front of Auth.js). Submitting the actual form —
# not a programmatic POST — keeps any anti-bot check clearing invisibly in
# the real browser, and the session lands in the profile via the flow's own
# redirect chain back to app.greptile.com.
_LOGIN_JS = _REACT_SET + """
let emailInp = null;
const d1 = Date.now() + 15000;
while (Date.now() < d1) {
  emailInp = document.querySelector('input[name=email]');
  if (emailInp) break;
  await new Promise(r => setTimeout(r, 300));
}
if (!emailInp) return { __error: 'sign-in form never appeared (host: ' + location.hostname + ')' };
const passInp = document.querySelector('input[name=password]');
if (!passInp) return { __error: 'no password input on the sign-in form' };
__setVal(emailInp, %(email)s);
__setVal(passInp, %(password)s);
await new Promise(r => setTimeout(r, 400));
const btn = [...document.querySelectorAll('button')].find(b => /^log\\s?in$/i.test(b.textContent.trim()));
if (!btn) return { __error: 'no Login button on the sign-in form' };
for (let i = 0; i < 20 && btn.disabled; i++) await new Promise(r => setTimeout(r, 500));
btn.click();
const d2 = Date.now() + 30000;
while (Date.now() < d2) {
  if (location.hostname === 'app.greptile.com') return { ok: true };
  const t = document.body.innerText;
  if (/invalid|incorrect|wrong|not found|failed/i.test(t)
      && document.querySelector('input[name=password]')) {
    return { __error: 'credentials rejected by the sign-in form' };
  }
  await new Promise(r => setTimeout(r, 400));
}
return { __error: 'no redirect to app.greptile.com after submitting the form' };
"""


@account.login
@returns("account")
@connection("none")
@timeout(120)
async def login(*, email: str = "", password: str = "", **params) -> dict:
    """Sign in to the Greptile dashboard — or report the already-live session.

    Drives the email+password form at auth.greptile.com inside the
    engine-owned browser tab; the session lands in the profile through the
    flow's own redirect chain. Accounts created via GitHub/GitLab/Google
    OAuth have no password — sign in once headed in the AgentOS browser
    instead.

    Args:
        email: Address to sign in as. Optional — resolved from stored
            credentials (1Password, Keychain, vault) when omitted.
        password: Account password. Same resolution as email.
    """
    session = await _get_session()
    if session:
        user = session.get("user", {}) or {}
        return _greptile_account_from_user(user, _org_from_session(session))

    if not email or not password:
        creds = await credentials.retrieve(domain=".greptile.com",
                                           required=["email", "password"])
        if creds.get("found"):
            value = creds.get("value") or {}
            email = email or value.get("email") or creds.get("identifier") or ""
            password = password or value.get("password") or ""
    if not email or not password:
        return app_error(
            "No Greptile credentials to sign in with.",
            code="NeedsCredentials",
            required=["email", "password"],
            hint="Pass email+password, store a greptile.com item in a "
                 "login_credentials provider, or (OAuth-only account) open "
                 "the headed AgentOS sign-in window via the `login_window` "
                 "service (url=https://auth.greptile.com).",
        )
    email = normalize_email(email)

    # Get the tab onto the sign-in form (a dead-session app page won't have
    # one — reloading the app root bounces us to auth.greptile.com).
    nav = await _eval(_GOTO_LOGIN_JS)
    if isinstance(nav, dict) and nav.get("__error"):
        return app_error(f"Reaching the sign-in form failed: {nav['__error']}",
                         code="SigninFailed")

    value = await _eval(_LOGIN_JS % {"email": json.dumps(email),
                                     "password": json.dumps(password)},
                        timeout_s=90)
    if not isinstance(value, dict) or value.get("__error"):
        detail = value.get("__error") if isinstance(value, dict) else value
        return app_error(
            f"Driving the Greptile sign-in form failed: {detail}. The login "
            "page shape may have changed — re-inspect the form. OAuth-only "
            "accounts (GitHub/GitLab/Google) must sign in once headed.",
            code="SigninFailed",
        )

    session = await _get_session()
    if not session:
        return app_error(
            "The form submitted but no live session followed — the account "
            "may require an OAuth provider. Sign in once headed in the "
            "AgentOS browser.",
            code="SigninFailed",
        )
    user = session.get("user", {}) or {}
    return _greptile_account_from_user(user, _org_from_session(session))


@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(60)
async def logout(**params) -> dict:
    """Sign out of the Greptile dashboard and invalidate the session.

    Runs Auth.js's signout same-origin in the app tab; the response's
    Set-Cookie clears the session token from the browser profile.
    Idempotent: signing out a dead session is still a 200 at Auth.js.
    """
    value = await _eval("""
if (!__onApp) return { ok: true, already: 'logged_out' };
const csrf = await (await fetch('/api/auth/csrf')).json().catch(() => null);
if (!csrf || !csrf.csrfToken) return { __error: 'csrf_failed' };
const body = new URLSearchParams({ csrfToken: csrf.csrfToken, json: 'true' });
const r = await fetch('/api/auth/signout', { method: 'POST', body });
if (!r.ok) return { __error: 'http_' + r.status };
return { ok: true };
""")
    if not isinstance(value, dict) or value.get("__error"):
        return app_error(
            f"Signout failed in the app tab: "
            f"{value.get('__error') if isinstance(value, dict) else value}",
            code="LogoutFailed",
        )
    return {
        "status": "logged_out",
        "hint": "Session token cleared from the browser profile by Auth.js's own Set-Cookie.",
    }


# ---------------------------------------------------------------------------
# Member management — the primary surface of this app. All routes go through
# /api/trpc on the dashboard host, same-origin inside the tab. Procedures
# captured from bundle spelunking; see the readme "People / Org API" table.
# ---------------------------------------------------------------------------


@test
@returns("account[]")
@connection("none")
@timeout(60)
async def list_members(*, tenant_external_id: str = None, query: str = "",
                       role: str = None, page: int = 0, page_size: int = 100,
                       **params) -> dict:
    """List active members of the current Greptile organization.

    Calls `organization.searchPeople` and returns only real members (type=member)
    as `account` shapes. Pending invites are excluded — use `list_invites` for those.

    Args:
        tenant_external_id: Override the org id (defaults to current session org).
        query: Optional email search filter.
        role: Optional role filter (`ADMIN` or `MEMBER`). `None` returns all.
        page: Zero-indexed page.
        page_size: Rows per page (default 100).
    """
    tid = await _resolve_tenant_id(tenant_external_id)
    args: dict = {
        "tenantExternalId": tid,
        "query": query or "",
        "page": page,
        "pageSize": page_size,
    }
    if role:
        args["roles"] = [_normalize_role(role)]
    resp = await _trpc_query("organization.searchPeople", args)
    data = _unwrap_trpc(resp, procedure="organization.searchPeople")
    items = data.get("items") or []
    members = [it for it in items if (it.get("type") or "").lower() == "member"]
    return [_member_to_account(it) for it in members]


@returns("invitation[]")
@connection("none")
@timeout(60)
async def list_invites(*, tenant_external_id: str = None, query: str = "",
                       page: int = 0, page_size: int = 100,
                       **params) -> dict:
    """List pending invitations in the current Greptile organization.

    Calls `organization.searchPeople` and returns only invite rows as
    `invitation` shapes. Active members are excluded — use `list_members`.

    Args:
        tenant_external_id: Override the org id (defaults to current session org).
        query: Optional email search filter.
        page: Zero-indexed page.
        page_size: Rows per page (default 100).
    """
    tid = await _resolve_tenant_id(tenant_external_id)
    args: dict = {
        "tenantExternalId": tid,
        "query": query or "",
        "page": page,
        "pageSize": page_size,
    }
    resp = await _trpc_query("organization.searchPeople", args)
    data = _unwrap_trpc(resp, procedure="organization.searchPeople")
    items = data.get("items") or []
    invites = [it for it in items if (it.get("type") or "").lower() == "invite"
               or bool(it.get("token"))]
    return {"__result__": [_item_to_invitation(it, tid) for it in invites]}


@returns({"inviteUrl": "string", "token": "string", "defaultRole": "string",
          "tenantName": "string"})
@connection("none")
@timeout(60)
async def get_invite_link(*, tenant_external_id: str = None, **params) -> dict:
    """Fetch the current shareable org invite link.

    Calls `invitation.getOrganizationInviteLink` and assembles the full URL
    using the captured template `{appUrl}/invitation?token={token}`.
    """
    tid = await _resolve_tenant_id(tenant_external_id)
    resp = await _trpc_query("invitation.getOrganizationInviteLink",
                             {"tenantExternalId": tid})
    data = _unwrap_trpc(resp, procedure="invitation.getOrganizationInviteLink")
    token = data.get("token") or ""
    return {"__result__": {
        "inviteUrl": INVITE_URL_TEMPLATE.format(token=token) if token else "",
        "token": token,
        "defaultRole": data.get("defaultRole") or "MEMBER",
        "tenantName": (data.get("tenant") or {}).get("name") or "",
    }}


@returns({"inviteUrl": "string", "token": "string", "defaultRole": "string"})
@connection("none")
@timeout(60)
async def create_invite_link(*, default_role: str = "MEMBER",
                             tenant_external_id: str = None,
                             **params) -> dict:
    """Create or rotate the org invite link.

    Calls `invitation.createOrganizationInviteLink` — if a link already exists
    this rotates the token. Returns the new fully-qualified URL.
    """
    tid = await _resolve_tenant_id(tenant_external_id)
    args = {"tenantExternalId": tid, "defaultRole": _normalize_role(default_role)}
    resp = await _trpc_mutate("invitation.createOrganizationInviteLink", args)
    data = _unwrap_trpc(resp, procedure="invitation.createOrganizationInviteLink")
    token = data.get("token") or ""
    return {"__result__": {
        "inviteUrl": INVITE_URL_TEMPLATE.format(token=token) if token else "",
        "token": token,
        "defaultRole": data.get("defaultRole") or _normalize_role(default_role),
    }}


@returns({"ok": "boolean", "revokedToken": "string"})
@connection("none")
@timeout(60)
async def revoke_invite_link(*, tenant_external_id: str = None,
                             **params) -> dict:
    """Revoke the org invite link entirely. New invitees won't be able to join.

    Calls `invitation.revokeOrganizationInviteLink`.
    """
    tid = await _resolve_tenant_id(tenant_external_id)
    resp = await _trpc_mutate("invitation.revokeOrganizationInviteLink",
                              {"tenantExternalId": tid})
    data = _unwrap_trpc(resp, procedure="invitation.revokeOrganizationInviteLink")
    return {"__result__": {
        "ok": True,
        "revokedToken": (data or {}).get("token") or "",
    }}


@returns("invitation")
@connection("none")
@timeout(60)
async def send_invite(*, email: str, role: str = "MEMBER",
                      tenant_external_id: str = None,
                      **params) -> dict:
    """Email an invite to join the Greptile org.

    Calls `invitation.create` — matches the "Invite by email" flow on the people
    settings page. Returns an `invitation` shape.

    Args:
        email: Address to invite.
        role: `ADMIN` or `MEMBER`. Defaults to `MEMBER`.
    """
    if not email:
        raise ValueError("email is required")
    tid = await _resolve_tenant_id(tenant_external_id)
    clean_email = email.strip().lower()
    norm_role = _normalize_role(role)
    args = {
        "tenantExternalId": tid,
        "email": clean_email,
        "role": norm_role,
    }
    resp = await _trpc_mutate("invitation.create", args)
    _unwrap_trpc(resp, procedure="invitation.create")
    return {"__result__": {
        "id": clean_email,
        "invitationType": "organization",
        "email": clean_email,
        "role": norm_role.lower(),
        "status": "pending",
        "url": f"{DASHBOARD_BASE}/settings/organization/people",
    }}


@returns({"ok": "boolean", "email": "string", "role": "string"})
@connection("none")
@timeout(60)
async def update_role(*, email: str, role: str,
                      tenant_external_id: str = None,
                      **params) -> dict:
    """Change a member's role in the current org.

    Calls `organization.setMemberRole`.

    Args:
        email: Member's email address.
        role: New role — `ADMIN` or `MEMBER`.
    """
    if not email:
        raise ValueError("email is required")
    if not role:
        raise ValueError("role is required")
    tid = await _resolve_tenant_id(tenant_external_id)
    args = {
        "tenantExternalId": tid,
        "email": email.strip().lower(),
        "role": _normalize_role(role),
    }
    resp = await _trpc_mutate("organization.setMemberRole", args)
    _unwrap_trpc(resp, procedure="organization.setMemberRole")
    return {"__result__": {
        "ok": True,
        "email": args["email"],
        "role": args["role"],
    }}


@returns({"ok": "boolean", "email": "string"})
@connection("none")
@timeout(60)
async def remove_member(*, email: str, tenant_external_id: str = None,
                        **params) -> dict:
    """Remove a member from the current org.

    Calls `organization.removeMember`. Arg shape `{email, tenantExternalId}`
    (captured from the bundle — no `namespaceExternalId`, which would scope it
    to a repo-level namespace instead of the org).
    """
    if not email:
        raise ValueError("email is required")
    tid = await _resolve_tenant_id(tenant_external_id)
    args = {"email": email.strip().lower(), "tenantExternalId": tid}
    resp = await _trpc_mutate("organization.removeMember", args)
    _unwrap_trpc(resp, procedure="organization.removeMember")
    return {"__result__": {"ok": True, "email": args["email"]}}


@returns({"ok": "boolean", "email": "string"})
@connection("none")
@timeout(60)
async def revoke_invite(*, email: str, tenant_external_id: str = None,
                        **params) -> dict:
    """Revoke a single pending email invite (not the shared link).

    Calls `invitation.revoke`. For the pending-invite rows in list_members.
    """
    if not email:
        raise ValueError("email is required")
    tid = await _resolve_tenant_id(tenant_external_id)
    args = {"email": email.strip().lower(), "tenantExternalId": tid}
    resp = await _trpc_mutate("invitation.revoke", args)
    _unwrap_trpc(resp, procedure="invitation.revoke")
    return {"__result__": {"ok": True, "email": args["email"]}}


# ---------------------------------------------------------------------------
# Reverse-engineering helpers. Keep while the tRPC surface is unstable.
# ---------------------------------------------------------------------------


@returns({"status": "integer", "url": "string", "body": "string", "json": "object"})
@connection("none")
@timeout(60)
async def probe(*, path: str, method: str = "GET", json_body: dict = None,
                max_body: int = 4000, **params) -> dict:
    """Fetch an app.greptile.com path same-origin inside the tab.

    Args:
        path: Path on the dashboard host (e.g. /api/auth/session).
        method: HTTP verb (GET, POST, PATCH, DELETE, PUT).
        json_body: JSON body for writes.
        max_body: Body clip length (default 4000).
    """
    method = (method or "GET").upper()
    opts = {"method": method, "cache": "no-store"}
    if json_body is not None:
        opts["headers"] = {"Content-Type": "application/json"}
        opts["body"] = json.dumps(json_body)
    value = await _eval(f"""
if (!__onApp) return {{ __error: 'needs_auth' }};
const r = await fetch({json.dumps(path)}, {json.dumps(opts)});
const text = await r.text();
let parsed = null;
try {{ parsed = JSON.parse(text); }} catch (e) {{}}
return {{ status: r.status, body: parsed ? '' : text.slice(0, {int(max_body)}), json: parsed }};
""")
    if isinstance(value, dict) and value.get("__error") == "needs_auth":
        return _needs_auth()
    if not isinstance(value, dict) or value.get("__error"):
        return app_error(f"probe failed in the tab: {value!r}", code="DashboardError")
    return {"__result__": {
        "status": value.get("status"),
        "url": f"{DASHBOARD_BASE}{path}" if not path.startswith("http") else path,
        "body": value.get("body") or "",
        "json": value.get("json"),
    }}


@returns({"status": "integer", "url": "string", "body": "string", "json": "object"})
@connection("none")
@timeout(60)
async def backend_probe(*, path: str, method: str = "GET", base: str = None,
                        json_body: dict = None, max_body: int = 4000,
                        **params) -> dict:
    """Call the Greptile backend API (api.greptile.com) with the greptileToken.

    The bearer is read from the session response and the request runs
    cross-origin from the app tab — exactly the call the real frontend makes,
    so CORS already allows it.

    Args:
        path: Path or full URL on the backend.
        method: HTTP verb.
        base: Backend base URL override (defaults to https://api.greptile.com).
        json_body: JSON body for writes.
    """
    u = path if path.startswith("http") else f"{base or BACKEND_BASE}{path}"
    method = (method or "GET").upper()
    body_js = json.dumps(json.dumps(json_body)) if json_body is not None else "null"
    value = await _eval(f"""
if (!__onApp) return {{ __error: 'needs_auth' }};
const s = await fetch('/api/auth/session', {{ cache: 'no-store' }});
const session = await s.json().catch(() => null);
const bearer = session && session.user ? session.user.greptileToken : null;
if (!bearer) return {{ __error: 'needs_auth' }};
const opts = {{ method: {json.dumps(method)}, headers: {{ 'Authorization': 'Bearer ' + bearer }} }};
const bodyStr = {body_js};
if (bodyStr) {{ opts.headers['Content-Type'] = 'application/json'; opts.body = bodyStr; }}
const r = await fetch({json.dumps(u)}, opts);
const text = await r.text();
let parsed = null;
try {{ parsed = JSON.parse(text); }} catch (e) {{}}
return {{ status: r.status, body: parsed ? '' : text.slice(0, {int(max_body)}), json: parsed }};
""")
    if isinstance(value, dict) and value.get("__error") == "needs_auth":
        return _needs_auth()
    if not isinstance(value, dict) or value.get("__error"):
        return app_error(f"backend_probe failed in the tab: {value!r}",
                         code="DashboardError")
    return {"__result__": {
        "status": value.get("status"),
        "url": u,
        "body": value.get("body") or "",
        "json": value.get("json"),
    }}
