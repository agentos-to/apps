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
import asyncio
import re as _re
import time as _time

from agentos import (
    account,
    app_error,
    browser_session,
    claims,
    client,
    connection,
    normalize_email,
    provides,
    returns,
    services,
    test,
    timeout,
)


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
# Origins to copy login-profile → bg after headed sign-in (NextAuth cookie
# is on `.exa.ai`; storage can be host-scoped on auth/dashboard).
_MERGE_URLS = [
    "https://dashboard.exa.ai/",
    "https://auth.exa.ai/",
    "https://exa.ai/",
]

# First-class login declaration — email OTP only (Exa has no password path).
LOGIN = browser_session.LoginFlow(
    domain=".exa.ai",
    # Must go through dashboard /login so auth gets a callbackUrl — bare
    # auth.exa.ai shows "accessed incorrectly". Engine login mode reuses the
    # single --app window across the dashboard→auth redirect (no /json/new).
    login_url="https://dashboard.exa.ai/login",
    label="Exa",
    credentials=["email"],
    otp=browser_session.OtpSpec(
        channels=["email"],
        default_order=["email"],
        remember_as="lastAuthMethod",
        verify_tool="verify_login_code",
    ),
    window_on=["captcha", "unknown_challenge"],
    plugin_key="exa",
)


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


async def _eval(target: str, body: str, *, timeout_s: int = 45, mode: str = "background"):
    """Run an op body inside the target's tab in the engine-owned browser.

    Default ``mode=background`` (headless). Use ``mode=login`` while a
    ``login_window(strategy=profile)`` is open.
    """
    return await services.call("browser_session", verb="eval", params={
        "mode": mode,
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


async def _check_session(*, mode: str = "background") -> dict | None:
    """Live NextAuth session from the dashboard tab, or None."""
    value = await _eval(_DASHBOARD, _SESSION_JS, mode=mode)
    if not isinstance(value, dict) or not value.get("user"):
        return None
    return value


async def _login_profile_open() -> bool:
    """True when the headed login profile is running (strategy=profile).

    Probe ``auth.exa.ai`` — never ``dashboard``: on mode=login the engine
    reuses the single ``--app`` window, and a dashboard target would
    navigate away from the OTP form.
    """
    try:
        await services.call(
            "browser_session",
            verb="cookies",
            params={
                "mode": "login",
                "target": _AUTH,
                "urls": [f"https://{_AUTH}/", f"https://{_DASHBOARD}/"],
            },
        )
        return True
    except Exception:
        return False


# Exa OTP mail — subject is stable; code is 6 alnum in HTML + otp= query.
# Inbox is discovered via mailbox providers (never hardcode an app / address).
_OTP_SUBJECT = "Sign in to Exa Dashboard"
_OTP_CODE_RE = _re.compile(
    r"(?:otp=|/Exa is:?\s*)([A-Za-z0-9]{4,8})\b|>\s*([A-Z0-9]{6})\s*<",
    _re.I,
)


def _extract_otp_code(blob: str) -> str | None:
    if not blob:
        return None
    m = _OTP_CODE_RE.search(blob)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def _rows_from_mailbox(result) -> list[dict]:
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        for key in ("emails", "items", "results", "nodes"):
            rows = result.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        if result.get("name") or result.get("content") or result.get("text"):
            return [result]
    return []


async def _mailbox_poll_attempts(delivered_to: str) -> list[dict]:
    """Brokered mailbox attempts — no hardcoded provider or inbox address.

    1. ``account=<delivered_to>`` and let the engine route.
    2. Same-domain linked identities from ``services.capabilities`` (workspace
       aliases land here without naming joe@ / gmail).
    3. Each ``list_providers("mailbox")`` app with no account (provider default).
    """
    email = normalize_email(delivered_to)
    domain = email.split("@", 1)[-1] if "@" in email else ""
    attempts: list[dict] = [{"account": email}]

    try:
        from agentos._bridge import dispatch

        caps = await dispatch("services.capabilities", {})
        mb = caps.get("mailbox") if isinstance(caps, dict) else None
        for p in (mb or {}).get("providers") or []:
            if not isinstance(p, dict):
                continue
            app_id = p.get("app") or p.get("app_id")
            for acct in p.get("accounts") or []:
                acct_n = normalize_email(str(acct))
                if not acct_n or acct_n == email:
                    continue
                if domain and acct_n.endswith("@" + domain):
                    attempts.append({"account": acct_n, "app": app_id})
    except Exception:
        pass

    try:
        listing = await services.list_providers("mailbox")
        for p in (listing or {}).get("providers") or []:
            if not isinstance(p, dict):
                continue
            app_id = p.get("app_id") or p.get("app")
            if app_id:
                attempts.append({"app": app_id})
    except Exception:
        pass

    # Dedupe while preserving order.
    seen: set[tuple] = set()
    out: list[dict] = []
    for a in attempts:
        key = (a.get("app"), a.get("account"))
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


async def _fetch_exa_otp_once(delivered_to: str, *, not_before: float) -> str | None:
    """One mailbox poll for a fresh Exa sign-in code (brokered providers)."""
    query = f'subject:"{_OTP_SUBJECT}" newer_than:1d'
    delivered = normalize_email(delivered_to)
    for attempt in await _mailbox_poll_attempts(delivered_to):
        params: dict = {"query": query, "limit": 5}
        if attempt.get("account"):
            params["account"] = attempt["account"]
        try:
            if attempt.get("app"):
                raw = await services.call(
                    "mailbox", app=attempt["app"], params=params
                )
            else:
                raw = await services.call("mailbox", params=params)
        except Exception:
            continue
        for row in _rows_from_mailbox(raw):
            blob = " ".join(
                str(row.get(k) or "")
                for k in ("name", "text", "content", "subject", "snippet")
            )
            subj = (row.get("name") or "").lower()
            if _OTP_SUBJECT.lower() not in subj and delivered not in blob.lower():
                continue
            code = _extract_otp_code(blob)
            if not code:
                continue
            published = row.get("published") or row.get("datePublished") or ""
            if published and not_before > 0:
                try:
                    from datetime import datetime

                    ts = datetime.fromisoformat(
                        str(published).replace("Z", "+00:00")
                    ).timestamp()
                    if ts + 5 < not_before:
                        continue
                except Exception:
                    pass
            return code
    return None


async def _poll_exa_otp(
    delivered_to: str, *, not_before: float, timeout_s: float = 120, interval_s: float = 5
) -> str | None:
    """Poll mailbox every ``interval_s`` until an Exa OTP appears or timeout."""
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        code = await _fetch_exa_otp_once(delivered_to, not_before=not_before)
        if code:
            return code
        await asyncio.sleep(interval_s)
    return None


async def _complete_with_mailbox_otp(email: str, *, not_before: float) -> dict:
    """Poll inbox for the Exa code, verify it, merge login→bg when needed."""
    code = await _poll_exa_otp(email, not_before=not_before)
    if not code:
        return {
            "shape": "auth_challenge",
            "kind": "code_sent",
            "name": "Exa sign-in code",
            "payload": email,
            "artifact": f"Verification code emailed to {email}.",
            "retrieval": {
                "via": "email",
                "deliveredTo": email,
                "sender": "exa.ai",
                "subjectHint": _OTP_SUBJECT,
                "look_for": "6-character code in the body (or otp= in the link)",
            },
            "instructions": (
                f"Mailbox poll timed out for {email}. Read the Exa email "
                f"via any mailbox provider, then call "
                f"verify_login_code(email, code)."
            ),
            "continueWith": "verify_login_code",
        }
    return await verify_login_code(email=email, code=code)


async def _merge_login_into_bg() -> dict:
    """Copy Exa session from login profile → headless bg, then close login."""
    merged = await LOGIN.merge_into_background(urls=_MERGE_URLS)
    await browser_session.close_login_window(strategy="profile")
    return merged if isinstance(merged, dict) else {"merged": True}


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

# Drive the real sign-in form. Prefer headless; on Turnstile escalate to a
# headed login_window but KEEP driving (fill email from credentials) so the
# human only clears the challenge. Wait for the Turnstile *token* before
# Continue — clicking early leaves "Please complete the verification
# challenge" even after the widget shows Success. OTP still goes through
# verify_login_code.
# turnstile_wait_ms: headless ~8s (fail fast → escalate); login profile
# ~3m (human solves widget; we watch the token then click).
_LOGIN_JS = _REACT_SET + """
const bodyText = () => (document.body && document.body.innerText) || '';
const turnstileWidgetPresent = () =>
  !!document.querySelector('input[name="cf-turnstile-response"]')
  || !!document.querySelector('iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"], .cf-turnstile');
const turnstileTokenReady = () => {
  const el = document.querySelector('input[name="cf-turnstile-response"]');
  const fromInput = el && typeof el.value === 'string' && el.value.length > 20;
  let fromApi = false;
  let expired = false;
  try {
    if (typeof turnstile !== 'undefined') {
      if (typeof turnstile.isExpired === 'function') expired = !!turnstile.isExpired();
      if (typeof turnstile.getResponse === 'function') {
        const r = turnstile.getResponse();
        fromApi = typeof r === 'string' && r.length > 20;
      }
    }
  } catch (_) {}
  if (expired) return false;
  return !!(fromInput || fromApi);
};
const challengeMsg = () =>
  /please complete the verification challenge/i.test(bodyText());
let emailInp = null;
const d1 = Date.now() + 15000;
while (Date.now() < d1) {
  emailInp = document.querySelector('input[type=email]');
  if (emailInp) break;
  await new Promise(r => setTimeout(r, 300));
}
if (!emailInp) {
  if (turnstileWidgetPresent() || challengeMsg())
    return { __error: 'verification challenge (Turnstile) blocking headless submit' };
  return { __error: 'sign-in form never appeared' };
}
__setVal(emailInp, %(email)s);
await new Promise(r => setTimeout(r, 400));
const cont = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Continue');
if (!cont) return { __error: 'no Continue button on the sign-in form' };
for (let i = 0; i < 20 && cont.disabled; i++) await new Promise(r => setTimeout(r, 500));

// Wait for Turnstile token (or no widget) before Continue — do NOT treat the
// red challenge banner alone as "blocked"; that appears after a premature click.
const waitMs = %(turnstile_wait_ms)s;
const readyDeadline = Date.now() + waitMs;
while (Date.now() < readyDeadline) {
  if (turnstileTokenReady()) break;
  if (!turnstileWidgetPresent() && !challengeMsg()) break;
  await new Promise(r => setTimeout(r, 400));
}
if ((turnstileWidgetPresent() || challengeMsg()) && !turnstileTokenReady()) {
  return {
    __error: 'verification challenge (Turnstile) blocking headless submit',
    emailFilled: true,
    turnstileWaitMs: waitMs,
  };
}

cont.click();
const d2 = Date.now() + 20000;
while (Date.now() < d2) {
  if (document.querySelector('input[placeholder="Enter verification code"]')) return { sent: true };
  if (/verification code has been sent/i.test(bodyText())) return { sent: true };
  // Token consumed / expired after a bad click — wait for a fresh Success again.
  if (challengeMsg() && !turnstileTokenReady()) {
    const reDeadline = Date.now() + Math.min(waitMs, 120000);
    while (Date.now() < reDeadline) {
      if (turnstileTokenReady()) {
        cont.click();
        break;
      }
      await new Promise(r => setTimeout(r, 400));
    }
  }
  await new Promise(r => setTimeout(r, 400));
}
return {
  __error: 'code entry never appeared (Turnstile may have blocked the submit)',
  emailFilled: true,
};
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
@timeout(300)
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
        creds = await LOGIN.retrieve_credentials()
        if creds.get("unlock_required"):
            return creds
        if creds.get("code") == "MultipleMatches":
            return app_error(
                creds.get("error") or "Multiple login items match exa.ai.",
                code="MultipleMatches",
                candidates=creds.get("candidates"),
                hint="Pass `email` explicitly, or set account= on retrieve.",
            )
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
    # the form lets Turnstile clear invisibly in the real browser — when
    # headless still hits a challenge wall, escalate to login_window.
    value = await _eval(
        _DASHBOARD,
        _LOGIN_JS
        % {"email": json.dumps(email), "turnstile_wait_ms": 8000},
        timeout_s=70,
    )
    if not isinstance(value, dict) or value.get("__error"):
        detail = value.get("__error") if isinstance(value, dict) else value
        detail_s = str(detail or "")
        if _re.search(
            r"turnstile|verification challenge|code entry never appeared|captcha",
            detail_s,
            _re.I,
        ):
            # Dedicated login profile (bg stays headless). Drive with mode=login.
            challenge = await LOGIN.escalate_to_window(
                reason=(
                    f"Exa needs a headed window for Turnstile ({detail_s}). "
                    f"Email {email} is being filled automatically — complete "
                    "the verification challenge if shown."
                ),
                retrieval={
                    "via": "email",
                    "deliveredTo": email,
                    "sender": "exa.ai",
                    "subjectHint": "Sign in to Exa Dashboard",
                    "look_for": "a short verification code in the body",
                },
                strategy="profile",
            )
            driven = await _eval(
                _AUTH,
                _LOGIN_JS
                % {
                    "email": json.dumps(email),
                    # Human solves Turnstile; we watch cf-turnstile-response /
                    # turnstile.getResponse() then click Continue.
                    "turnstile_wait_ms": 180_000,
                },
                timeout_s=200,
                mode="login",
            )
            if isinstance(driven, dict) and driven.get("sent"):
                return await _complete_with_mailbox_otp(
                    email, not_before=_time.time() - 30
                )
            if isinstance(challenge, dict) and challenge.get("kind") == "login_window":
                challenge["payload"] = email
                challenge["instructions"] = (
                    f"Email {email} should be filled on the login-profile "
                    "window (bg is still headless). Complete Turnstile if "
                    "shown — login polls mailbox for the code, verifies, "
                    "merges into bg, and closes."
                )
            return challenge
        return app_error(
            f"Driving the Exa sign-in form failed: {detail}. The login page "
            "shape may have changed — re-inspect the form.",
            code="SigninFailed",
        )

    # Headless path: code emailed — poll mailbox and finish.
    return await _complete_with_mailbox_otp(email, not_before=_time.time() - 30)


@returns("account")
@claims("primary_user")
@connection("none")
@timeout(120)
async def verify_login_code(*, email: str, code: str, **params) -> dict:
    """Enter the verification code in the sign-in form and finish login.

    Types the code into the live form (the one `login` advanced to) and
    waits for the redirect to the dashboard. When sign-in ran on the
    dedicated login profile (Turnstile path), merges cookies/storage into
    the headless bg profile and closes the login window.
    """
    if not email or not code:
        return app_error("email and code are required.", code="BadParams")
    email = normalize_email(email)

    # Prefer login profile when open (headed Turnstile path); else bg.
    # Drive auth.exa.ai — that is where the code input lives. Never target
    # dashboard here: mode=login would navigate the sole --app window off
    # the OTP form.
    mode = "login" if await _login_profile_open() else "background"
    value = await _eval(
        _AUTH,
        _VERIFY_JS % {"code": json.dumps(code)},
        timeout_s=60,
        mode=mode,
    )
    if not isinstance(value, dict) or value.get("__error"):
        detail = value.get("__error") if isinstance(value, dict) else value
        return app_error(
            f"Entering the code failed: {detail}. If the code was rejected, "
            "request a fresh one with login and retry.",
            code="VerifyFailed",
        )

    session = await _check_session(mode=mode)
    if not session:
        return app_error(
            "The form accepted the code but no live session followed — "
            "request a fresh code with login and retry.",
            code="VerifyFailed",
        )

    if mode == "login":
        try:
            merge = await _merge_login_into_bg()
        except Exception as e:
            return app_error(
                f"Signed in on the login profile but merge into background "
                f"failed: {e}. Retry merge_login_session then "
                f"close_login_window, or call check_session.",
                code="MergeFailed",
            )
        # Confirm the session is live on the headless daemon profile.
        bg = await _check_session(mode="background")
        if not bg:
            return app_error(
                "Merged cookies into background but check_session on bg "
                "failed — session may need flip fallback "
                "(login_window strategy=flip).",
                code="MergeFailed",
                merge=merge,
            )
        account = _account_from_session(bg)
        account["merged"] = merge
        return account

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
@provides("web_search")
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
@returns("document")
@provides("web_fetch")
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
