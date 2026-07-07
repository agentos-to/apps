#!/usr/bin/env python3
"""
claude_web.py — claude.ai private API client, browser-driven.

Every op runs same-origin ``fetch()`` *inside a claude.ai tab of the
engine-owned browser* via the ``browser_session`` service (the Exa
pattern). The session is the browser profile itself — the ``sessionKey``
cookie on ``.claude.ai``, written by claude.ai's own login flow, never
extracted, never vaulted, never seen by this app. Because the request
originates from the real browser tab it carries the live session and
clears Cloudflare by construction — exactly the call the React app makes.

Login is magic-link: ``login`` drives the email form at claude.ai/login
and returns an ``auth_challenge``; the agent reads the magic-link URL from
the account's inbox by judgment and calls ``verify_login`` to navigate it
in the tab.
"""

import json
import re

from agentos import account, app_error, claims, connection, credentials, normalize_email, provides, returns, services, test, timeout, web_read

BASE_URL = "https://claude.ai"

# browser_session target — a URL substring; the engine opens
# https://<target>/ in the engine-owned browser when no tab matches.
_TARGET = "claude.ai"

# claude.ai ships its frontend build id in this header; same-origin GETs
# don't strictly require it, but we send it to match the React app's calls.
_CLIENT_VERSION = "claude-ai/web@1.1.5368"

# Readiness wait prepended to every op body — a freshly opened tab is at
# about:blank when our JS first runs, so a relative-URL fetch has no origin
# to resolve against. Wait until the document settles on a claude.ai origin.
# `__loggedOut` is true when claude.ai parked us on its /login page (the
# auth signal for a missing session).
_PRELUDE = """
const __deadline = Date.now() + 15000;
while (location.hostname.indexOf('claude.ai') === -1 || document.readyState !== 'complete') {
  if (Date.now() > __deadline) return { __error: 'tab_not_ready' };
  await new Promise(r => setTimeout(r, 200));
}
const __loggedOut = location.pathname.indexOf('/login') === 0
  || location.pathname.indexOf('/magic-link') === 0;
"""


async def _eval(body: str, *, timeout_s: int = 45):
    """Run an op body inside the claude.ai tab in the engine-owned browser."""
    return await services.call(services.browser_session, params={
        "target": _TARGET,
        "js": "(async () => {\n" + _PRELUDE + body + "\n})()",
        "timeout": timeout_s,
    })


async def _api(method: str, path: str, *, json_body: dict | None = None,
               timeout_s: int = 45) -> dict:
    """One same-origin fetch inside the tab. Returns {status, json}.

    Carries the sessionKey cookie because it's same-origin; sends the
    claude.ai client-version header the React app sends.
    """
    opts = {
        "method": method,
        "cache": "no-store",
        "headers": {"anthropic-client-version": _CLIENT_VERSION},
    }
    if json_body is not None:
        opts["headers"]["Content-Type"] = "application/json"
        opts["body"] = json.dumps(json_body)
    value = await _eval(f"""
if (__loggedOut) return {{ __loggedOut: true }};
const r = await fetch({json.dumps(path)}, {json.dumps(opts)});
let body = null;
try {{ body = await r.json(); }} catch (e) {{}}
return {{ status: r.status, json: body }};
""", timeout_s=timeout_s)
    if not isinstance(value, dict):
        raise RuntimeError(f"claude.ai tab eval returned {value!r}")
    if value.get("__loggedOut"):
        raise RuntimeError(
            "SESSION_EXPIRED: no live claude.ai session in the AgentOS browser "
            "profile — run claude.login or sign in once headed."
        )
    if value.get("__error"):
        raise RuntimeError(f"claude.ai fetch failed in the tab: {value['__error']}")
    return value


# -- API operations ------------------------------------------------------------


async def _get_organizations():
    resp = await _api("GET", "/api/organizations")
    data = resp.get("json")
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        code = err.get("details", {}).get("error_code", "") if isinstance(err, dict) else ""
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        if "session_invalid" in code or "authorization" in msg.lower():
            raise RuntimeError("SESSION_EXPIRED: Claude session is invalid — re-login at claude.ai")
        raise RuntimeError(f"Claude API error: {msg}")
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected orgs response (status {resp.get('status')}): {type(data).__name__}")
    return data


async def _resolve_org_uuid(org_uuid=None):
    """Resolve the org UUID for chat operations — explicit, else first chat-capable."""
    if org_uuid:
        return org_uuid
    orgs = await _get_organizations()
    for org in orgs:
        if "chat" in org.get("capabilities", []):
            return org["uuid"]
    if orgs:
        return orgs[0]["uuid"]
    raise RuntimeError("No organizations found for this account")


async def _get_conversations(org_uuid, limit=50, offset=0):
    path = f"/api/organizations/{org_uuid}/chat_conversations?limit={limit}&offset={offset}"
    resp = await _api("GET", path)
    if resp.get("status", 0) >= 400:
        raise RuntimeError(f"Conversations API returned {resp.get('status')}: {resp.get('json')}")
    return resp.get("json")


async def _get_conversation(org_uuid, conv_uuid):
    path = (
        f"/api/organizations/{org_uuid}/chat_conversations/{conv_uuid}"
        "?tree=True&rendering_mode=messages&render_all_tools=true"
    )
    resp = await _api("GET", path)
    return resp.get("json")


# -- Formatting helpers --------------------------------------------------------

def _format_conversation_list(convs, org_uuid):
    return [
        {
            "uuid": c.get("uuid"),
            "name": c.get("name") or "(untitled)",
            "updatedAt": c.get("updated_at"),
            "createdAt": c.get("created_at"),
            "orgUuid": org_uuid,
        }
        for c in convs
    ]


def _format_conversation(conv, org_uuid):
    messages = conv.get("chat_messages", [])
    formatted_messages = []
    content_lines = []
    for msg in messages:
        content_blocks = msg.get("content", [])
        text_parts = [
            b.get("text", "") for b in content_blocks
            if b.get("type") == "text" and b.get("text")
        ]
        text = "\n".join(text_parts)
        role = msg.get("sender", "human")
        formatted_messages.append({
            "role": role,
            "content": text,
            "createdAt": msg.get("created_at"),
            "uuid": msg.get("uuid"),
        })
        if text.strip():
            label = "Human" if role == "human" else "Assistant"
            content_lines.append(f"**{label}:**\n{text}")

    return {
        "uuid": conv.get("uuid"),
        "name": conv.get("name") or "(untitled)",
        "orgUuid": org_uuid,
        "createdAt": conv.get("created_at"),
        "updatedAt": conv.get("updated_at"),
        "content": "\n\n---\n\n".join(content_lines),
        "messages": formatted_messages,
        "messageCount": len(formatted_messages),
    }


# -- Operation entrypoints -----------------------------------------------------

@returns("conversation[]")
@connection("none")
@timeout(45)
async def list_conversations(*, org=None, limit=50, offset=0, **params) -> list:
    """List claude.ai web chat conversations, most recently updated first. Requires a valid session (run login flow if needed).

        Args:
            org: Org UUID to use (omit to use session default). Use list_orgs to discover available orgs.
            limit: Max conversations to return (max 250)
            offset: Pagination offset
        """
    limit = int(limit)
    offset = int(offset)
    resolved_org = await _resolve_org_uuid(org)
    convs = await _get_conversations(resolved_org, limit=limit, offset=offset)
    return _format_conversation_list(convs, resolved_org)


@returns("conversation")
@provides(web_read, urls=["claude.ai/chat/*", "www.claude.ai/chat/*"])
@connection("none")
@timeout(45)
async def get_conversation(*, id=None, url=None, org=None, **params) -> dict:
    """Get a full claude.ai web conversation with all messages. Returns the complete message history including both human and assistant turns.

        Args:
            id: Conversation UUID — optional if url is a claude.ai/chat/… link
            url: Claude chat URL copied from the browser (web_read)
            org: Org UUID (omit to use session default)
        """
    conv_id = id
    if url:
        m = re.search(r"chat/([0-9a-fA-F-]{36})", url)
        if m:
            conv_id = m.group(1)
    if not conv_id:
        raise ValueError("id or url is required for get_conversation")
    resolved_org = await _resolve_org_uuid(org)
    conv = await _get_conversation(resolved_org, conv_id)
    return _format_conversation(conv, resolved_org)


@returns("conversation[]")
@connection("none")
@timeout(60)
async def search_conversations(*, query="", org=None, limit=20, **params) -> list:
    """Search claude.ai web conversations by title/name. Fetches up to 250 conversations and filters locally (no server-side search). For full content search across message text, use import_conversation first, then search({ query: "...", types: ["message"] }) against the graph FTS index.

        Args:
            query: Text to search for in conversation titles
            org: Org UUID (omit to use session default)
            limit: Max results
        """
    limit = int(limit)

    query_lower = query.lower()
    results = []
    offset = 0
    page_size = 50

    resolved_org = await _resolve_org_uuid(org)
    while offset < 250:
        page = await _get_conversations(resolved_org, limit=page_size, offset=offset)
        if not page:
            break
        for conv in page:
            name = (conv.get("name") or "").lower()
            if query_lower in name:
                results.append(conv)
        if len(page) < page_size:
            break
        offset += page_size

    return _format_conversation_list(results[:limit], resolved_org)


@returns("message[]")
@connection("none")
@timeout(90)
async def import_conversation(*, org=None, limit=5, offset=0, **params) -> list:
    """Import claude.ai conversations and all their messages into the graph. Each message becomes a message entity with full content FTS-indexed. After import, use search({ query: "...", types: ["message"] }) for content search. Safe to run repeatedly — deduplicates by message UUID. Use limit+offset to page through conversations in batches of 5-10.

        Args:
            org: Org UUID (omit to use session default)
            limit: Conversations per batch (keep ≤10 to avoid DB lock)
            offset: Pagination offset
        """
    limit = int(limit)
    offset = int(offset)

    rows = []
    resolved_org = await _resolve_org_uuid(org)
    convs = await _get_conversations(resolved_org, limit=limit, offset=offset)
    for conv_stub in convs:
        conv_uuid = conv_stub["uuid"]
        conv_name = conv_stub.get("name") or "(untitled)"
        try:
            conv = await _get_conversation(resolved_org, conv_uuid)
        except Exception:
            continue
        for msg in conv.get("chat_messages", []):
            content_blocks = msg.get("content", [])
            text_parts = [
                b.get("text", "") for b in content_blocks
                if b.get("type") == "text" and b.get("text")
            ]
            text = "\n".join(text_parts).strip()
            if not text:
                continue
            msg_uuid = msg.get("uuid", "")
            rows.append({
                "id": f"{conv_uuid}_{msg_uuid}",
                "conversationId": conv_uuid,
                "conversationName": conv_name,
                "role": msg.get("sender", "human"),
                "content": text,
                "createdAt": msg.get("created_at", conv_stub.get("created_at")),
            })

    return rows


@returns({"uuid": "string", "name": "string", "capabilities": "array"})
@connection("none")
@timeout(30)
async def list_orgs(**params) -> list:
    """List all organizations the user has access to. Returns org UUIDs, names, and capabilities. Use this to discover which org has chat history (look for "chat" in capabilities)."""
    return await _get_organizations()


# -- The account trio — browser-driven -----------------------------------------

_CLAUDE_AI = {"shape": "product", "url": "https://claude.ai", "name": "Claude.ai"}


def _pick_identity(orgs: list) -> tuple[str, str] | None:
    """Return (identifier, display) from chat-capable org if any, else first org."""
    for org in orgs:
        if "chat" in org.get("capabilities", []):
            name = org.get("name", "")
            m = re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", name)
            val = m.group(0) if m else name
            return val, val
    if orgs:
        name = orgs[0].get("name", "")
        return name, name
    return None


async def _account_from_orgs() -> dict | None:
    """Resolve identity from /api/organizations, or None if logged out."""
    try:
        orgs = await _get_organizations()
    except Exception:
        return None
    picked = _pick_identity(orgs)
    if not picked:
        return None
    identifier, display = picked
    return {
        "authenticated": True,
        "at": _CLAUDE_AI,
        "identifier": identifier,
        "display": display,
    }


@account.check
@test.skip(reason='requires a live browser session')
@returns("account")
@claims("primary_user")
@connection("none")
@timeout(45)
async def check_session(**params) -> dict:
    """Verify the Claude.ai session and identify the logged-in account.

    The session lives in the engine-owned browser profile; this op asks
    /api/organizations from inside the claude.ai tab. No cookie reaches the app.
    """
    acct = await _account_from_orgs()
    return acct or {"authenticated": False}


# React-safe value set — the native setter + input/change events.
_REACT_SET = """
const __setVal = (el, v) => {
  const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  s.call(el, v);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
};
"""

# Get the tab onto the login form, then drive the email field + submit to
# trigger the magic-link email. Navigation to /login kills this eval's
# context, so we navigate and bail, letting the next eval's prelude wait.
_GOTO_LOGIN_JS = """
if (location.pathname.indexOf('/login') === 0
    && document.querySelector('input[type=email]')) return { ready: true };
location.replace('https://claude.ai/login');
return { navigating: true };
"""

_REQUEST_LINK_JS = _REACT_SET + """
let emailInp = null;
const d1 = Date.now() + 15000;
while (Date.now() < d1) {
  emailInp = document.querySelector('input[type=email]');
  if (emailInp) break;
  await new Promise(r => setTimeout(r, 300));
}
if (!emailInp) return { __error: 'login email field never appeared' };
__setVal(emailInp, %(email)s);
await new Promise(r => setTimeout(r, 400));
const btn = [...document.querySelectorAll('button')].find(b =>
  /continue with email|continue|log in|sign in/i.test(b.textContent.trim()));
if (!btn) return { __error: 'no continue button on the login form' };
for (let i = 0; i < 20 && btn.disabled; i++) await new Promise(r => setTimeout(r, 500));
btn.click();
const d2 = Date.now() + 20000;
while (Date.now() < d2) {
  if (/check your (email|inbox)|we sent|verification|sent you a/i.test(document.body.innerText)) {
    return { sent: true };
  }
  await new Promise(r => setTimeout(r, 400));
}
return { __error: 'no "check your email" confirmation after submitting' };
"""


@account.login
@returns("account | auth_challenge")
@connection("none")
@timeout(90)
async def login(*, email: str = "", **params) -> dict:
    """Sign in to claude.ai — or report the already-live session.

    Returns the `account` when the browser profile holds a live session.
    Otherwise drives the email form at claude.ai/login to request a
    magic-link email and returns an `auth_challenge` (kind: magic_link)
    whose `continueWith` is verify_login.

    Args:
        email: Address to sign in as. Optional — resolved from stored
            credentials (1Password, Keychain, vault) when omitted.
    """
    acct = await _account_from_orgs()
    if acct:
        return acct

    if not email:
        creds = await credentials.retrieve(domain=".claude.ai", required=["email"])
        if creds.get("found"):
            email = (creds.get("value") or {}).get("email") or creds.get("identifier") or ""
    if not email:
        return app_error(
            "No email to sign in as.",
            code="NeedsCredentials",
            required=["email"],
            hint="Pass `email`, or store a claude.ai item in a login_credentials provider.",
        )
    email = normalize_email(email)

    nav = await _eval(_GOTO_LOGIN_JS)
    if isinstance(nav, dict) and nav.get("__error"):
        return app_error(f"Reaching the login form failed: {nav['__error']}",
                         code="SigninFailed")

    value = await _eval(_REQUEST_LINK_JS % {"email": json.dumps(email)}, timeout_s=60)
    if not isinstance(value, dict) or value.get("__error"):
        detail = value.get("__error") if isinstance(value, dict) else value
        return app_error(
            f"Requesting the claude.ai magic link failed: {detail}. The login "
            "page shape may have changed — re-inspect the form.",
            code="SigninFailed",
        )

    return {
        "name": "Claude.ai sign-in link",
        "kind": "magic_link",
        "payload": email,
        "artifact": f"Magic-link sign-in email sent to {email}.",
        # Self-serve hint for an agent with email access — where to look,
        # NOT how to parse. The agent reads the message, confirms it's a
        # genuine Anthropic claude.ai sign-in, and extracts the magic-link
        # URL with judgment (no regex — judgment also catches a phishing
        # look-alike a pattern would blindly trust).
        "retrieval": {
            "via": "email",
            "deliveredTo": email,
            "sender": "Anthropic (claude.ai)",
            "subjectHint": "Sign in to Claude / your login link",
            "look_for": "a https://claude.ai/magic-link#… URL in the body",
        },
        "instructions": (
            f"Read the sign-in email Anthropic just sent to {email}. Confirm "
            "it's genuine and recent, copy the https://claude.ai/magic-link#… "
            "URL from the body, then call verify_login(magic_link=<that URL>). "
            "Only ask the human if the message isn't there."
        ),
        "continueWith": "verify_login",
    }


@returns("account")
@claims("primary_user")
@connection("none")
@timeout(60)
async def verify_login(*, magic_link: str, **params) -> dict:
    """Complete claude.ai login by navigating the magic-link URL in the tab.

    Navigating the link lands the sessionKey cookie in the browser profile
    through claude.ai's own flow — nothing extracted or vaulted. Confirms by
    reading identity back from /api/organizations.

    Args:
        magic_link: The https://claude.ai/magic-link#… URL from the sign-in email.
    """
    if not magic_link or "claude.ai/magic-link" not in magic_link:
        return app_error(
            "magic_link must be a https://claude.ai/magic-link#… URL from the "
            "sign-in email.", code="BadParams")

    # Navigate the tab to the magic link; this eval's context dies on
    # navigation, so just kick it off.
    await _eval(f"location.replace({json.dumps(magic_link)});\nreturn {{ navigating: true }};")

    # Poll for the session to settle (the next eval's prelude waits for the
    # post-redirect page to finish loading).
    for _ in range(8):
        acct = await _account_from_orgs()
        if acct:
            return acct
    return app_error(
        "Navigated the magic link but no live session followed — the link may "
        "be expired. Request a fresh one with login and retry.",
        code="VerifyFailed",
    )


@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(45)
async def logout(**params) -> dict:
    """Sign out of claude.ai — clears the session from the browser profile.

    POSTs claude.ai's logout endpoint same-origin in the tab; the response's
    Set-Cookie clears the sessionKey. Idempotent.
    """
    value = await _eval("""
if (__loggedOut) return { ok: true, already: 'logged_out' };
const r = await fetch('/api/auth/logout', { method: 'POST', cache: 'no-store' });
return { ok: r.ok, status: r.status };
""")
    if not isinstance(value, dict) or value.get("__error"):
        return app_error(
            f"Signout failed in the claude.ai tab: "
            f"{value.get('__error') if isinstance(value, dict) else value}",
            code="LogoutFailed",
        )
    return {
        "status": "logged_out",
        "hint": "sessionKey cleared from the browser profile by claude.ai's Set-Cookie.",
    }
