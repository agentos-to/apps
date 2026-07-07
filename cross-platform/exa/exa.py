"""
exa.py — Exa search/extraction (api_key) + dashboard via the browser session.

Two halves, two auth substrates:

- **API half** (`search`, `read_webpage`): plain HTTP against api.exa.ai
  with a portable api_key from the vault — the `api` connection.
- **Dashboard half** (account trio + key management): every op is a
  `fetch()` evaluated *inside* a tab of the engine-owned browser via the
  `browser_session` service (the WhatsApp pattern). The session is the
  browser profile itself — `next-auth.session-token` on `.exa.ai`,
  written by NextAuth's own Set-Cookie, never extracted, never stored in
  the vault, never seen by this app. Requests originate from the real
  browser, so Vercel's security checkpoint and Cloudflare cookies stay
  live by construction.

Dashboard architecture (NextAuth.js, email verification code):
  - auth.exa.ai       — csrf, signin/email, verify-otp, callback, signout
  - dashboard.exa.ai  — session check + key/team endpoints
  Ops run same-origin in the matching tab; the profile carries the
  `.exa.ai` cookie across both hosts.

The API key secret lives in `legacyBearerSecret` of /api/get-api-keys
(`id` is the row UUID, `publicId` the display handle — neither
authenticates).
"""

import json

from agentos import account, app_error, claims, client, connection, credentials, normalize_email, provides, returns, services, test, timeout, web_read, web_search


connection(
    'api',
    base_url='https://api.exa.ai',
    domain='exa.ai',
    auth={'type': 'api_key', 'header': {'x-api-key': '.auth.key'}},
    label='API Key',
    help_url='https://dashboard.exa.ai/api-keys')


API_BASE = "https://api.exa.ai"

# browser_session targets — a URL substring; the engine opens
# https://<target>/ in the engine-owned browser when no tab matches.
_DASHBOARD = "dashboard.exa.ai"
_AUTH = "auth.exa.ai"


# Readiness wait, prepended to every op body. A freshly opened tab is
# still at about:blank when our JS first runs — a relative-URL fetch has
# no origin to resolve against, and a cross-origin fetch wouldn't carry
# the `.exa.ai` cookie. So we wait until the document has settled on an
# `.exa.ai` origin before the body runs. We wait for the *family*, not a
# specific host, because NextAuth bounces an unauthenticated dashboard
# visit to auth.exa.ai: where the tab lands IS the auth signal, and each
# body branches on `location.hostname`. This is exa's analogue of
# WhatsApp's `_PRELUDE` Store-readiness wait.
_PRELUDE = """
const __deadline = Date.now() + 15000;
while (location.hostname.indexOf('exa.ai') === -1 || document.readyState !== 'complete') {
  if (Date.now() > __deadline) return { __error: 'tab_not_ready' };
  await new Promise(r => setTimeout(r, 200));
}
const __onDashboard = location.hostname.indexOf('dashboard.') === 0;
"""


async def _eval(target: str, body: str, *, timeout_s: int = 45):
    """Run an op body inside the target's tab in the engine-owned browser.

    The engine matchmakes the `browser_session` provider, opens the tab
    (and launches the browser) when needed, and returns the JS value.
    The body runs only once the tab has settled on an `.exa.ai` origin,
    with `__onDashboard` in scope telling it whether NextAuth kept us on
    the dashboard (authenticated) or bounced us to auth (logged out).
    """
    return await services.call(services.browser_session, params={
        "target": target,
        "js": "(async () => {\n" + _PRELUDE + body + "\n})()",
        "timeout": timeout_s,
    })


# Session probe. If NextAuth bounced us off the dashboard, we're logged
# out — no need to call the API. On the dashboard, NextAuth returns `{}`
# (not an error) for anonymous visitors, so `user` is the live signal.
_SESSION_JS = """
if (!__onDashboard) return null;
const r = await fetch('/api/auth/session', { cache: 'no-store' });
if (!r.ok) return { __error: 'http_' + r.status };
const data = await r.json().catch(() => null);
return (data && data.user) ? data : null;
"""


async def _check_session() -> dict | None:
    """Live NextAuth session from the dashboard tab, or None."""
    value = await _eval(_DASHBOARD, _SESSION_JS)
    if not isinstance(value, dict) or not value.get("user"):
        return None
    return value


def _needs_auth():
    return app_error(
        "No live Exa dashboard session in the AgentOS browser profile. "
        "Run exa.login — it emails a verification code to the account "
        "address; verify_login_code completes the sign-in in the browser.",
        code="NeedsAuth",
    )


# React-safe value set: the native setter + input/change events. A plain
# `el.value = …` is ignored by the framework's controlled inputs and the
# submit button stays disabled.
_REACT_SET = """
const __setVal = (el, v) => {
  const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  s.call(el, v);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
};
"""

# Drive the real sign-in form rather than POST /api/auth/signin/email:
# Exa fronts that endpoint with Cloudflare Turnstile, which 403s a
# programmatic call. Submitting the actual form lets Turnstile solve
# invisibly in the real browser — the whole point of browser-as-store.
_LOGIN_JS = _REACT_SET + """
let emailInp = null;
const d1 = Date.now() + 15000;
while (Date.now() < d1) {
  emailInp = document.querySelector('input[type=email]');
  if (emailInp) break;
  await new Promise(r => setTimeout(r, 300));
}
if (!emailInp) return { __error: 'sign-in form never appeared' };
__setVal(emailInp, %(email)s);
await new Promise(r => setTimeout(r, 400));
const cont = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Continue');
if (!cont) return { __error: 'no Continue button on the sign-in form' };
for (let i = 0; i < 20 && cont.disabled; i++) await new Promise(r => setTimeout(r, 500));
cont.click();
const d2 = Date.now() + 25000;
while (Date.now() < d2) {
  if (document.querySelector('input[placeholder="Enter verification code"]')) return { sent: true };
  if (/verification code has been sent/i.test(document.body.innerText)) return { sent: true };
  await new Promise(r => setTimeout(r, 400));
}
return { __error: 'code entry never appeared (Turnstile may have blocked the submit)' };
"""

# Enter the code in the real form and wait for the redirect to the
# dashboard. The session lands in the profile via the form's own flow —
# nothing extracted, nothing vaulted.
_VERIFY_JS = _REACT_SET + """
let codeInp = null;
const d1 = Date.now() + 12000;
while (Date.now() < d1) {
  codeInp = document.querySelector('input[placeholder="Enter verification code"]')
    || [...document.querySelectorAll('input')].find(i => i.maxLength === 6 && i.type !== 'hidden');
  if (codeInp) break;
  await new Promise(r => setTimeout(r, 300));
}
if (!codeInp) return { __error: 'code input never appeared — run login first' };
__setVal(codeInp, %(code)s);
await new Promise(r => setTimeout(r, 400));
const btn = [...document.querySelectorAll('button')].find(b => /verify/i.test(b.textContent));
if (!btn) return { __error: 'no Verify button' };
btn.click();
const d2 = Date.now() + 20000;
while (Date.now() < d2) {
  if (location.hostname.indexOf('dashboard.') === 0) return { ok: true };
  if (/invalid|incorrect|expired|wrong code/i.test(document.body.innerText)) return { __error: 'code rejected' };
  await new Promise(r => setTimeout(r, 400));
}
return { __error: 'no redirect to dashboard after verify' };
"""


# ---------------------------------------------------------------------------
# Operations — called by the Python executor with kwargs
# ---------------------------------------------------------------------------

_EXA = {"shape": "product", "url": "https://exa.ai", "name": "Exa"}


def _account_from_session(session: dict) -> dict:
    """Project a NextAuth session payload onto the `account` shape —
    the check_session convention (auth-flows.md): identifier is the
    canonical email, userId is Exa's internal stable id."""
    user = session.get("user", {})
    email_raw = user.get("email")
    if not email_raw:
        return {"authenticated": True, "at": _EXA}
    email = normalize_email(email_raw)
    return {
        "authenticated": True,
        "at": _EXA,
        "identifier": email,
        "email": email,
        "displayName": user.get("name"),
        "userId": str(user["id"]) if user.get("id") is not None else None,
    }


@account.check
@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("account")
@claims("primary_user")
@connection("none")
@timeout(60)
async def check_session(**params) -> dict:
    """Verify the Exa dashboard session and identify the logged-in account.

    The session lives in the engine-owned browser profile; this op asks
    NextAuth from inside the dashboard tab. No cookie ever reaches the app.
    """
    session = await _check_session()
    if not session:
        return {"authenticated": False}
    return _account_from_session(session)


@account.login
@returns("account | auth_challenge")
@connection("none")
@timeout(90)
async def login(*, email: str = "", **params) -> dict:
    """Sign in to the Exa dashboard — or report the already-live session.

    Returns the `account` when the browser profile holds a live session.
    Otherwise triggers Exa's email verification code from inside the
    auth tab and returns an `auth_challenge` (kind: code_sent) whose
    `continueWith` is verify_login_code.

    Args:
        email: Address to sign in as. Optional — resolved from stored
            credentials (1Password, Keychain, vault) when omitted.
    """
    session = await _check_session()
    if session:
        return _account_from_session(session)

    if not email:
        creds = await credentials.retrieve(domain=".exa.ai", required=["email"])
        if creds.get("found"):
            email = (creds.get("value") or {}).get("email") or creds.get("identifier") or ""
    if not email:
        return app_error(
            "No email to sign in as.",
            code="NeedsCredentials",
            required=["email"],
            hint="Pass `email` explicitly, or store an exa.ai item in a login_credentials provider.",
        )
    email = normalize_email(email)

    # Drive the real sign-in form (not a fetch to /api/auth/signin/email —
    # that's Turnstile-fronted and 403s a programmatic call). Submitting
    # the form lets Turnstile clear invisibly in the real browser.
    value = await _eval(_DASHBOARD, _LOGIN_JS % {"email": json.dumps(email)},
                        timeout_s=70)
    if not isinstance(value, dict) or value.get("__error"):
        detail = value.get("__error") if isinstance(value, dict) else value
        return app_error(
            f"Driving the Exa sign-in form failed: {detail}. The login page "
            "shape may have changed — re-inspect the form.",
            code="SigninFailed",
        )

    return {
        "name": "Exa sign-in code",
        "kind": "code_sent",
        "payload": email,
        "artifact": f"Verification code emailed to {email}.",
        # Self-serve hint for an agent with email access — where to look,
        # NOT how to parse. The agent reads the message, confirms it's a
        # genuine Exa sign-in (sender, recency, subject) and reads the
        # code with judgment. No regex: the code may be digits, alnum
        # (e.g. "23THE6"), or a link, and judgment also catches a
        # phishing look-alike a pattern would blindly trust.
        "retrieval": {
            "via": "email",
            "deliveredTo": email,
            "sender": "exa.ai",
            "subjectHint": "Sign in to Exa Dashboard",
            "look_for": "a short verification code in the body "
                        "('Your verification code for Exa is: …')",
        },
        "instructions": (
            f"Read the verification email Exa just sent to {email} (from "
            "exa.ai, subject 'Sign in to Exa Dashboard'). Confirm it's "
            "genuine and recent, read the code from the body, then call "
            "verify_login_code(email, code). Only ask the human if the "
            "message isn't there."
        ),
        "continueWith": "verify_login_code",
    }


@returns("account")
@claims("primary_user")
@connection("none")
@timeout(90)
async def verify_login_code(*, email: str, code: str, **params) -> dict:
    """Enter the verification code in the sign-in form and finish login.

    Types the code into the live form (the one `login` advanced to) and
    waits for the redirect to the dashboard. The session lands in the
    browser profile through the form's own flow — the profile IS the
    session store; nothing is extracted or vaulted. Confirms by reading
    the session back.
    """
    if not email or not code:
        return app_error("email and code are required.", code="BadParams")
    email = normalize_email(email)

    value = await _eval(_DASHBOARD, _VERIFY_JS % {"code": json.dumps(code)},
                        timeout_s=60)
    if not isinstance(value, dict) or value.get("__error"):
        detail = value.get("__error") if isinstance(value, dict) else value
        return app_error(
            f"Entering the code failed: {detail}. If the code was rejected, "
            "request a fresh one with login and retry.",
            code="VerifyFailed",
        )

    session = await _check_session()
    if not session:
        return app_error(
            "The form accepted the code but no live session followed — "
            "request a fresh code with login and retry.",
            code="VerifyFailed",
        )
    return _account_from_session(session)


@returns({"apiKeys": "array", "count": "integer"})
@connection("none")
@timeout(60)
async def get_api_keys(*, store: bool = True, **params) -> dict:
    """List API keys from the Exa dashboard and optionally store the first enabled key.

    Runs in the dashboard tab — the request carries the live browser
    session. The bearer secret is `legacyBearerSecret`; the stored key is
    a *portable* secret, so it (alone) goes to the vault via __secrets__.

    Args:
        store: Store the first enabled key as a credential (default True)
    """
    session = await _check_session()
    if not session:
        return _needs_auth()
    email = session["user"]["email"]

    data = await _eval(_DASHBOARD, """
if (!__onDashboard) return { __error: 'needs_auth' };
const r = await fetch('/api/get-api-keys', { cache: 'no-store' });
if (!r.ok) return { __error: 'http_' + r.status };
return await r.json().catch(() => ({}));
""")
    if isinstance(data, dict) and data.get("__error") == "needs_auth":
        return _needs_auth()
    if not isinstance(data, dict) or data.get("__error"):
        return app_error(
            f"get-api-keys failed in the dashboard tab: "
            f"{data.get('__error') if isinstance(data, dict) else data}",
            code="DashboardError",
        )

    keys = data.get("apiKeys", [])
    # The secret is `legacyBearerSecret`; `id` is the row UUID. Keys the
    # endpoint returns without a secret can be listed but not stored.
    storable = [k for k in keys if k.get("enabled") and k.get("legacyBearerSecret")]

    result = {
        "__result__": {
            "apiKeys": [
                {
                    "name": k["name"],
                    "enabled": k["enabled"],
                    "createdAt": k["createdAt"],
                    "rateLimit": k.get("rateLimit"),
                    "storable": bool(k.get("legacyBearerSecret")),
                }
                for k in keys
            ],
            "count": len(keys),
        }
    }

    if store and storable:
        key = storable[0]
        secret = key["legacyBearerSecret"]
        result["__secrets__"] = [{
            "domain": "exa.ai",
            "identifier": email,
            "itemType": "api_key",
            "label": f"Exa API Key ({key['name']})",
            "source": "exa",
            "value": {"key": secret},
            "metadata": {
                "masked": {"key": secret[:6] + "••••••••"},
                "dashboardUrl": "https://dashboard.exa.ai/api-keys",
                "keyName": key["name"],
            },
        }]

    return result


@returns({"teams": "array", "count": "integer"})
@connection("none")
@timeout(60)
async def get_teams(**params) -> dict:
    """Get team info including rate limits, credits, and usage from the dashboard."""
    data = await _eval(_DASHBOARD, """
if (!__onDashboard) return { __error: 'needs_auth' };
const r = await fetch('/api/get-teams', { cache: 'no-store' });
if (r.status === 401 || r.status === 403) return { __error: 'needs_auth' };
if (!r.ok) return { __error: 'http_' + r.status };
return await r.json().catch(() => ({}));
""")
    if isinstance(data, dict) and data.get("__error") == "needs_auth":
        return _needs_auth()
    if not isinstance(data, dict) or data.get("__error"):
        return app_error(
            f"get-teams failed in the dashboard tab: "
            f"{data.get('__error') if isinstance(data, dict) else data}",
            code="DashboardError",
        )

    teams = data.get("teams", [])
    return {
        "__result__": {
            "teams": [
                {
                    "id": t["id"],
                    "name": t["name"],
                    "role": t.get("role"),
                    "rateLimit": t.get("customRateLimit"),
                    "maxResults": t.get("customNumResults"),
                    "creditsCents": t.get("totalAppliedCreditsCents"),
                    "usageLimit": t.get("usageLimit"),
                    "monthlyUsage": t.get("monthlyUsage"),
                    "isEnterprise": t.get("isEnterprise"),
                    "users": [
                        {"email": u["email"], "role": u["role"]}
                        for u in t.get("users", [])
                    ],
                }
                for t in teams
            ],
            "count": len(teams),
        }
    }


@returns({"status": "string", "keyName": "string", "domain": "string", "maskedKey": "string"})
@connection("none")
@timeout(60)
async def create_api_key(*, name: str = "agentOS", **params) -> dict:
    """Create a new API key on the Exa dashboard and store it via __secrets__.

    Args:
        name: Name for the new API key (default "agentOS")
    """
    session = await _check_session()
    if not session:
        return _needs_auth()
    email = session["user"]["email"]

    data = await _eval(_DASHBOARD, f"""
if (!__onDashboard) return {{ __error: 'needs_auth' }};
const r = await fetch('/api/create-api-key', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{ name: {json.dumps(name)} }}),
}});
if (!r.ok) return {{ __error: 'http_' + r.status }};
return await r.json().catch(() => ({{}}));
""")
    if isinstance(data, dict) and data.get("__error") == "needs_auth":
        return _needs_auth()
    if not isinstance(data, dict) or data.get("__error"):
        return app_error(
            f"create-api-key failed in the dashboard tab: "
            f"{data.get('__error') if isinstance(data, dict) else data}",
            code="DashboardError",
        )

    key_obj = data.get("apiKey") or {}
    # The secret lives in `legacyBearerSecret`; `id` is the row UUID and
    # `publicId` the display handle — neither authenticates.
    api_key = key_obj.get("legacyBearerSecret") if isinstance(key_obj, dict) else None
    if not api_key or not isinstance(api_key, str):
        return {"__result__": {"error": "legacyBearerSecret not found in creation response",
                               "fields": sorted(key_obj.keys()) if isinstance(key_obj, dict) else []}}

    masked = api_key[:6] + "••••" + api_key[-4:]
    return {
        "__secrets__": [{
            "domain": "exa.ai",
            "identifier": email or "unknown",
            "itemType": "api_key",
            "label": f"Exa API Key ({name})",
            "source": "exa",
            "value": {"key": api_key},
            "metadata": {
                "masked": {"key": masked},
                "dashboardUrl": "https://dashboard.exa.ai/api-keys",
                "keyName": name,
            },
        }],
        "__result__": {
            "status": "created",
            "keyName": name,
            "domain": "exa.ai",
            "maskedKey": masked,
        },
    }


def _map_result(r: dict) -> dict:
    """Map Exa result to shape-native result fields."""
    highlights = r.get("highlights") or []
    text = r.get("text") or r.get("summary") or (highlights[0] if highlights else None)
    return {
        "id": r.get("url"),
        "name": r.get("title"),
        "content": text,
        "url": r.get("url"),
        "image": r.get("image"),
        "favicon": r.get("favicon"),
        "author": r.get("author"),
        "published": r.get("publishedDate"),
    }


@test(params={'query': 'agentOS personal AI', 'limit': 3})
@returns("result[]")
@provides(web_search)
@connection("api")
@timeout(30)
async def search(*, query: str, limit: int = 10, category: str = None, include_text: bool = True, **params) -> list[dict]:
    """Search the web using Exa's neural/semantic search.

    Args:
        query: Search query
        limit: Max results to return (default 10)
        category: Optional category filter (e.g. "research paper", "company")
        include_text: Include full text content in results (default True)
    """
    api_key = params.get("auth", {}).get("key", "")
    body: dict = {
        "query": query,
        "numResults": limit,
        "type": "auto",
        "contents": {"text": include_text, "summary": True},
    }
    if category:
        body["category"] = category

    resp = await client.post(
        f"{API_BASE}/search",
        json=body, headers={"x-api-key": api_key},
    )

    return [_map_result(r) for r in (resp["json"] or {}).get("results", [])]


@test(params={'url': 'https://exa.ai'})
@returns("webpage")
@provides(web_read)
@connection("api")
@timeout(30)
async def read_webpage(*, url: str, **params) -> dict:
    """Extract full content from a URL using Exa.

    Args:
        url: URL to extract content from
    """
    api_key = params.get("auth", {}).get("key", "")
    resp = await client.post(
        f"{API_BASE}/contents",
        json={"urls": [url], "text": True}, headers={"x-api-key": api_key},
    )

    results = (resp["json"] or {}).get("results", [])
    if not results:
        return {"id": url, "url": url, "error": "No content found"}
    return _map_result(results[0])


@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(60)
async def logout(**params) -> dict:
    """Sign out of the Exa dashboard and invalidate the session.

    Runs NextAuth's signout same-origin in the auth tab; the response's
    Set-Cookie clears the session token from the browser profile.
    Idempotent: signing out a dead session is still a 200 at NextAuth.
    """
    value = await _eval(_AUTH, """
const csrf = await (await fetch('/api/auth/csrf')).json().catch(() => null);
if (!csrf || !csrf.csrfToken) return { __error: 'csrf_failed' };
const body = new URLSearchParams({ csrfToken: csrf.csrfToken, json: 'true' });
const r = await fetch('/api/auth/signout', { method: 'POST', body });
if (!r.ok) return { __error: 'http_' + r.status };
return { ok: true };
""")
    if not isinstance(value, dict) or value.get("__error"):
        return app_error(
            f"Signout failed in the auth tab: "
            f"{value.get('__error') if isinstance(value, dict) else value}",
            code="LogoutFailed",
        )
    return {
        "status": "logged_out",
        "hint": "Session token cleared from the browser profile by NextAuth's own Set-Cookie.",
    }
