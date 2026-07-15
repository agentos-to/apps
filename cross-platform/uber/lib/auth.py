"""Uber SSO / OTP helpers (no @returns tools — those stay in uber.py)."""
from __future__ import annotations

import asyncio
import json as _json
import re as _re
from datetime import datetime, timedelta, timezone

from agentos import (
    app_error,
    browser_session,
    credentials,
    normalize_email,
    services,
)

from lib.session import (
    CURRENT_USER_QUERY,
    _EATS,
    _RIDES,
    _UBER,
    _UBER_EATS,
    _check_tab,
    _eval,
    _gql,
    _ueats,
)

_AUTH = "auth.uber.com"

_AUTH_LOGIN_URL = "https://auth.uber.com/v2"

_RIDES_LOGIN_URL = "https://riders.uber.com"

_EATS_LOGIN_URL = "https://www.ubereats.com"

_CRED_DOMAINS = ("uber.com", ".uber.com", "login.uber.com")

_DEFAULT_AUTH_ORDER = ("email", "sms", "whatsapp")

# First-class login declaration (OTP multi-channel). Site drive still lives
# in `_drive_uber_login_until_challenge`; Phase D fills generic headless.
LOGIN = browser_session.LoginFlow(
    domain="uber.com",
    login_url=_AUTH_LOGIN_URL,
    label="Uber",
    credentials=["email", "password"],
    otp=browser_session.OtpSpec(
        channels=["email", "sms", "whatsapp", "call"],
        default_order=["email", "sms", "whatsapp"],
        remember_as="lastAuthMethod",
        verify_tool="verify_login_code",
    ),
    window_on=["captcha", "card_digits", "unknown_challenge"],
    plugin_key="uber",
)

_CHANNEL_PATTERNS = {
    "email": (r"\bemail\b", r"send.*email", r"email.*code"),
    "sms": (r"\btext\b", r"\bsms\b", r"text message", r"send.*text"),
    "whatsapp": (r"whatsapp",),
    "call": (r"call me", r"phone call", r"\bcall\b"),
}

_EATS_SESSION_COOKIES = ("sid", "csid", "uev2.id.session", "jwt-session")

_OTP_TTL = timedelta(minutes=10)

_PENDING_OTP: dict[str, dict] = {}

def _rides_needs_auth():
    return app_error(
        "No live Uber rider session in the AgentOS browser profile. "
        "Call uber.login — it drives email/password from 1Password, requests "
        "an OTP on the preferred channel (email first), and returns an "
        "auth_challenge whose continueWith is verify_login_code.",
        code="NeedsAuth",
        login_url=_RIDES_LOGIN_URL,
    )

def _eats_needs_auth():
    return app_error(
        "No live Uber Eats session in the AgentOS browser profile. "
        "Call uber.login_eats (or uber.login — same SSO). It prefers the "
        "account's lastAuthMethod, defaults to email OTP, and returns "
        "verify_login_code when a code is needed.",
        code="NeedsAuth",
        login_url=_EATS_LOGIN_URL,
    )

def _uber_meta(last_auth_method: str | None = None, last_second_factor: str | None = None,
               **extra) -> dict:
    blob = {k: v for k, v in extra.items() if v is not None}
    if last_auth_method:
        blob["lastAuthMethod"] = last_auth_method
    if last_second_factor:
        blob["lastSecondFactor"] = last_second_factor
    return {"uber": blob} if blob else {}

def _meta_uber(account_row: dict | None) -> dict:
    meta = (account_row or {}).get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    uber = meta.get("uber") if isinstance(meta, dict) else None
    return uber if isinstance(uber, dict) else {}

async def _read_account_prefs(identifier: str) -> dict:
    """Pull metadata.uber prefs off the graph account (preferred channel)."""
    if not identifier:
        return {}
    try:
        from agentos._bridge import dispatch
        resp = await dispatch("data.list", {
            "shape": "account",
            "q": identifier,
            "limit": 10,
            "fields": ["id", "identifier", "email", "metadata", "name"],
        })
    except Exception:
        return {}
    rows = resp.get("data") if isinstance(resp, dict) else resp
    if not isinstance(rows, list):
        return {}
    needle = identifier.lower()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("identifier", "email", "name"):
            val = (row.get(key) or "")
            if isinstance(val, str) and val.lower() == needle:
                return _meta_uber(row)
    for row in rows:
        prefs = _meta_uber(row if isinstance(row, dict) else None)
        if prefs:
            return prefs
    return {}

async def _sms_channel_available() -> bool:
    """True when something in this OS can read SMS/iMessage OTPs."""
    try:
        listing = await services.list_providers("login_credentials")
    except Exception:
        listing = {}
    # Cheap heuristic: if iMessage / WhatsApp Desktop are installed as
    # plugins, SMS/chat OTPs are recoverable. Email is always preferred.
    try:
        from agentos._bridge import dispatch
        plugs = await dispatch("plugins.list", {"limit": 100})
        names = {
            (p.get("id") or p.get("name") or "").lower()
            for p in ((plugs.get("data") if isinstance(plugs, dict) else plugs) or [])
            if isinstance(p, dict)
        }
        return bool(names & {"imessage", "whatsapp", "whatsapp-desktop", "messages"})
    except Exception:
        return True  # optimistic — agent can still fall through

async def _auth_method_order(identifier: str = "") -> list[str]:
    prefs = await _read_account_prefs(identifier)
    preferred = (prefs.get("lastAuthMethod") or "").lower().strip()
    otp = browser_session.OtpSpec(
        channels=list(_DEFAULT_AUTH_ORDER) + ["call"],
        default_order=list(_DEFAULT_AUTH_ORDER),
        remember_as="lastAuthMethod",
        verify_tool="verify_login_code",
    )
    order = await browser_session.resolve_otp_order_async(
        otp=otp, account_pref=preferred or None
    )
    if not await _sms_channel_available():
        order = [c for c in order if c != "sms"]
    return order

async def _bs_snapshot(target: str, *, timeout_s: int = 30) -> dict:
    return await browser_session.snapshot(target, timeout=timeout_s) or {}

async def _bs_click(target: str, ref: str) -> dict:
    return await browser_session.click(target, ref) or {}

async def _bs_type(target: str, ref: str, text: str, *, clear: bool = True) -> dict:
    return await services.call("browser_session", verb="type", params={
        "mode": "background",
        "target": target,
        "ref": ref,
        "text": text,
        "clear": clear,
    }) or {}

async def _bs_type_secret(target: str, ref: str, domain: str, *,
                          account: str | None = None) -> dict:
    params = {
        "mode": "background",
        "target": target,
        "ref": ref,
        "domain": domain,
        "item_type": "login_credentials",
        "field": "password",
    }
    if account:
        params["account"] = account
    return await services.call("browser_session", verb="type_secret", params=params) or {}

def _snap_nodes(snap: dict) -> list[dict]:
    tree = snap.get("tree") if isinstance(snap, dict) else None
    if isinstance(tree, list):
        return [n for n in tree if isinstance(n, dict)]
    # Some surfaces nest under snapshot.tree
    inner = snap.get("snapshot") if isinstance(snap, dict) else None
    if isinstance(inner, dict) and isinstance(inner.get("tree"), list):
        return [n for n in inner["tree"] if isinstance(n, dict)]
    return []

def _find_ref(snap: dict, *, role: str | None = None, name_re: str | None = None,
              name_exact: str | None = None) -> str | None:
    rx = _re.compile(name_re, _re.I) if name_re else None
    for n in _snap_nodes(snap):
        if role and (n.get("role") or "").lower() != role.lower():
            continue
        name = (n.get("name") or "").strip()
        if name_exact is not None and name != name_exact:
            continue
        if rx and not rx.search(name):
            continue
        ref = n.get("ref")
        if ref:
            return ref
    return None

def _page_text(snap: dict) -> str:
    return " ".join((n.get("name") or "") for n in _snap_nodes(snap))

def _snap_url(snap: dict) -> str:
    if not isinstance(snap, dict):
        return ""
    url = snap.get("url") or ""
    if url:
        return str(url)
    inner = snap.get("snapshot")
    if isinstance(inner, dict):
        return str(inner.get("url") or "")
    return ""

def _detect_auth_screen(snap: dict) -> str:
    """Classify the current auth.uber.com screen for the login state machine."""
    text = _page_text(snap).lower()
    # Card digit challenge — require "last N digits" / masked pan copy, not the
    # More-options "Payment card" button (that false-fired a headed window).
    if _re.search(
        r"last\s+\d+\s+digits|••••|enter.*(card|payment).*(digit|number)|"
        r"digits of your (card|payment)",
        text,
    ):
        return "card_challenge"
    # OTP entry BEFORE identifier — digit screens can still mention email/phone
    # in copy, and typing a code into the identifier field is a silent fail.
    if _re.search(
        r"enter.*(code|digit)|verification code|security code|4-digit|6-digit|one-time|"
        r"enter an otp",
        text,
    ) and not _find_ref(snap, role="textbox", name_re=r"password"):
        return "otp_entry"
    if _find_ref(snap, role="textbox", name_re=r"email|phone|mobile") and \
            not _find_ref(snap, role="textbox", name_re=r"password"):
        return "identifier"
    if _find_ref(snap, role="textbox", name_re=r"password") or \
            _re.search(r"\bpassword\b", text):
        return "password"
    if _re.search(r"whatsapp|text message|\bemail\b|call me|send code|more options", text) and \
            _re.search(r"verif|code|authent", text):
        return "otp_picker"
    if _re.search(r"welcome back|you're signed in|go to uber", text):
        return "done"
    return "unknown"

def _infer_otp_channel(snap: dict) -> str | None:
    """Best-effort: which channel the OTP-entry copy says the code was sent on.

    Ignores channel-picker buttons ("Send code via WhatsApp", "Email", …) that
    linger in the AX tree when the More-options sheet is open — those used to
    false-match WhatsApp while an email OTP was active.
    """
    parts: list[str] = []
    for n in _snap_nodes(snap):
        role = (n.get("role") or "").lower()
        name = n.get("name") or ""
        if role in ("button", "link") and _re.search(
            r"whatsapp|text message|\bsms\b|\bemail\b|password|call me|"
            r"more options|resend|close|see all|send code via",
            name,
            _re.I,
        ):
            continue
        parts.append(name)
    text = " ".join(parts).lower()
    if _re.search(r"\bwhatsapp\b", text):
        return "whatsapp"
    if _re.search(r"\b(text message|sms|texted|sent.*(text|sms))\b", text):
        return "sms"
    if _re.search(r"\b(call me|phone call|voice call)\b", text):
        return "call"
    if _re.search(r"\b(email|e-?mail|emailed|inbox)\b", text) or "@" in text:
        return "email"
    return None

def _channel_ref(snap: dict, channel: str) -> str | None:
    # Buttons/links only — never match the identifier textbox
    # ("Enter phone number or email") via a bare name_re.
    for pat in _CHANNEL_PATTERNS.get(channel, ()):
        ref = _find_ref(snap, role="button", name_re=pat) or \
            _find_ref(snap, role="link", name_re=pat)
        if ref:
            return ref
    return None

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _stamp_pending_otp(*, email: str, channel: str, step: str) -> dict:
    now = _utc_now()
    blob = {
        "channel": channel,
        "step": step,
        "requestedAt": _iso(now),
        "expiresAt": _iso(now + _OTP_TTL),
    }
    _PENDING_OTP[(email or "").lower()] = blob
    return blob

def _pending_otp_for(email: str) -> dict | None:
    return _PENDING_OTP.get((email or "").lower())

def _clear_pending_otp(email: str) -> None:
    _PENDING_OTP.pop((email or "").lower(), None)

def _auth_challenge_for_channel(*, email: str, channel: str, step: str = "otp") -> dict:
    """Exa-style auth_challenge — agent retrieves the code, then verify_login_code."""
    channel = (channel or "email").lower()
    stamp = _stamp_pending_otp(email=email, channel=channel, step=step)
    if channel == "email":
        retrieval = {
            "via": "email",
            "deliveredTo": email,
            "sender": "uber.com",
            "subjectHint": "verification OR security OR code OR Uber",
            "look_for": "a short (usually 4-digit) Uber verification code in the body",
            "requestedAt": stamp["requestedAt"],
            "expiresAt": stamp["expiresAt"],
        }
        artifact = (
            f"Uber emailed a verification code to {email} "
            f"(requested {stamp['requestedAt']}). Ignore SMS/WhatsApp codes."
        )
        instructions = (
            f"ONLY use the EMAIL code for {email} (sender uber.com), requested at "
            f"{stamp['requestedAt']}. Do not use a Messages/SMS code for this challenge. "
            "Confirm with the human if more than one code is in flight, then call "
            f"verify_login_code(email={email!r}, code=<code>, method='email')."
        )
    elif channel == "sms":
        retrieval = {
            "via": "sms",
            "senderHint": "Uber",
            "look_for": "a short Uber verification code in a recent text message",
            "requestedAt": stamp["requestedAt"],
            "expiresAt": stamp["expiresAt"],
        }
        artifact = (
            f"Uber texted a verification code (requested {stamp['requestedAt']}). "
            "Ignore email codes."
        )
        instructions = (
            f"ONLY use the SMS/Messages code from Uber (requested {stamp['requestedAt']}). "
            "Do not use an email code. Confirm with the human if unsure, then call "
            f"verify_login_code(email={email!r}, code=<code>, method='sms')."
        )
    elif channel == "whatsapp":
        retrieval = {
            "via": "whatsapp",
            "senderHint": "Uber",
            "look_for": "a short Uber verification code in WhatsApp",
            "requestedAt": stamp["requestedAt"],
            "expiresAt": stamp["expiresAt"],
        }
        artifact = (
            f"Uber sent a WhatsApp verification code (requested {stamp['requestedAt']})."
        )
        instructions = (
            f"ONLY use the WhatsApp code (requested {stamp['requestedAt']}). "
            f"Then call verify_login_code(email={email!r}, code=<code>, method='whatsapp')."
        )
    else:
        retrieval = {
            "via": channel,
            "look_for": "Uber verification code",
            "requestedAt": stamp["requestedAt"],
            "expiresAt": stamp["expiresAt"],
        }
        artifact = f"Uber sent a verification code via {channel}."
        instructions = (
            f"Retrieve the Uber {channel} code and call "
            f"verify_login_code(email={email!r}, code=<code>, method={channel!r})."
        )
    return {
        "shape": "auth_challenge",
        "kind": "code_sent",
        "name": f"Uber {step} ({channel})",
        "payload": email,
        "artifact": artifact,
        "retrieval": retrieval,
        "instructions": instructions,
        "continueWith": "verify_login_code",
        "metadata": {
            "uber": {
                "pendingAuthMethod": channel,
                "step": step,
                "requestedAt": stamp["requestedAt"],
                "expiresAt": stamp["expiresAt"],
            }
        },
    }

async def _issue_otp_after_channel_click(
    *, email: str, channel: str, ref: str, step: str = "otp"
) -> dict:
    """Click a channel control, require OTP entry, assert on-screen channel."""
    await _bs_click(_AUTH, ref)
    await asyncio.sleep(1.0)
    snap = await _bs_snapshot(_AUTH)
    if "auth.uber.com" not in _snap_url(snap):
        return app_error(
            f"After choosing {channel}, auth tab left auth.uber.com "
            f"(url={_snap_url(snap)!r}). Call login again.",
            code="ChannelNotConfirmed",
        )
    screen = _detect_auth_screen(snap)
    if screen != "otp_entry":
        return app_error(
            f"Clicked {channel} but screen is `{screen}`, not OTP entry. "
            f"Page hint: {_page_text(snap)[:220]}",
            code="ChannelNotConfirmed",
        )
    seen = _infer_otp_channel(snap)
    if seen and seen != channel:
        return app_error(
            f"Requested {channel} OTP but the screen looks like {seen}. "
            "Refusing a mismatched auth_challenge — call login again and pick "
            "one channel.",
            code="ChannelMismatch",
            requested=channel,
            onScreen=seen,
        )
    # Prefer the screen's channel when we can read it; otherwise the click.
    return _auth_challenge_for_channel(
        email=email, channel=seen or channel, step=step
    )

async def _resolve_uber_email(email: str = "") -> str:
    email = (email or "").strip()
    if email:
        return normalize_email(email)
    for domain in _CRED_DOMAINS:
        creds = await credentials.retrieve(domain=domain, required=["email"])
        if creds.get("found"):
            val = creds.get("value") or {}
            got = (val.get("email") or creds.get("identifier") or "").strip()
            if got:
                return normalize_email(got)
    return ""

async def _ensure_password_vaulted(email: str) -> str | None:
    """Make sure a login_credentials row exists so type_secret can inject it.

    credentials.retrieve triggers 1Password → vault write. Returns the domain
    that matched, or None if no password is available.
    """
    for domain in _CRED_DOMAINS:
        creds = await credentials.retrieve(
            domain=domain, account=email or None, required=["email", "password"]
        )
        if creds.get("found") and (creds.get("value") or {}).get("password"):
            return domain
    for domain in _CRED_DOMAINS:
        creds = await credentials.retrieve(domain=domain, required=["password"])
        if creds.get("found") and (creds.get("value") or {}).get("password"):
            return domain
    return None

async def _pick_otp_channel(snap: dict, order: list[str]) -> tuple[str, str] | None:
    """Return (channel, ref) for the first preferred channel visible on screen."""
    # Open "More options" if the preferred channel isn't on the first screen.
    for channel in order:
        ref = _channel_ref(snap, channel)
        if ref:
            return channel, ref
    more = _find_ref(snap, role="button", name_re=r"more options|other options|try another")
    if more:
        snap = await _bs_click(_AUTH, more)
        await asyncio.sleep(0.6)
        snap = await _bs_snapshot(_AUTH)
        for channel in order:
            ref = _channel_ref(snap, channel)
            if ref:
                return channel, ref
    return None

async def _drive_uber_login_until_challenge(
    *, email: str, method_order: list[str], prefer_password: bool = True
) -> dict:
    """Drive auth.uber.com from identifier → password → OTP request.

    Returns an account dict, an auth_challenge, a NeedsHuman/NeedsAuth error,
    or a structured {__error: ...} dict.
    """
    # Auth is its own host-surface (engine: one tab per host). Never navigate
    # the auth target to riders/m.uber.com — that overwrote the form and made
    # OTP detection fall through to a headed login_window.
    snap = await _bs_snapshot(_AUTH)
    screen = _detect_auth_screen(snap)
    # If a prior step left us on OTP entry, do NOT reload — that burns the code.
    if screen != "otp_entry" or "auth.uber.com" not in _snap_url(snap):
        await browser_session.navigate(_AUTH, _AUTH_LOGIN_URL)
        await asyncio.sleep(1.2)
        snap = await _bs_snapshot(_AUTH)
        screen = _detect_auth_screen(snap)

    # Already signed in?
    rides = await _rides_account_if_live()
    if rides:
        return rides

    # Identifier step
    if screen == "identifier" or _find_ref(snap, role="textbox", name_re=r"email|phone|mobile"):
        box = _find_ref(snap, role="textbox", name_re=r"email|phone|mobile") or \
            _find_ref(snap, role="textbox")
        if not box:
            return {"__error": "no_identifier_field"}
        await _bs_type(_AUTH, box, email, clear=True)
        cont = _find_ref(snap, role="button", name_re=r"^continue$|next|submit") or \
            _find_ref(snap, role="button", name_exact="Continue")
        if cont:
            snap = await _bs_click(_AUTH, cont)
        await asyncio.sleep(1.0)
        snap = await _bs_snapshot(_AUTH)
        screen = _detect_auth_screen(snap)

    # Prefer password when available — quieter than OTP for returning users,
    # and still followed by email OTP / 2FA when Uber demands it.
    if prefer_password and (
        screen == "password"
        or _find_ref(snap, role="button", name_re=r"^password$|use password|sign in with password")
        or _re.search(r"\bpassword\b", _page_text(snap), _re.I)
    ):
        # May need to click into the password path from "More options"
        pw_btn = _find_ref(snap, role="button", name_re=r"^password$|use password|sign in with password")
        if pw_btn and not _find_ref(snap, role="textbox", name_re=r"password"):
            snap = await _bs_click(_AUTH, pw_btn)
            await asyncio.sleep(0.8)
            snap = await _bs_snapshot(_AUTH)
        if not _find_ref(snap, role="textbox", name_re=r"password"):
            more = _find_ref(snap, role="button", name_re=r"more options|other options")
            if more:
                snap = await _bs_click(_AUTH, more)
                await asyncio.sleep(0.6)
                snap = await _bs_snapshot(_AUTH)
                pw_btn = _find_ref(snap, role="button", name_re=r"^password$|use password")
                if pw_btn:
                    snap = await _bs_click(_AUTH, pw_btn)
                    await asyncio.sleep(0.8)
                    snap = await _bs_snapshot(_AUTH)
        pw_box = _find_ref(snap, role="textbox", name_re=r"password")
        if pw_box:
            domain = await _ensure_password_vaulted(email)
            if not domain:
                return app_error(
                    "Uber password not available from any login_credentials provider "
                    "(1Password / vault). Store a Login for uber.com, or pass credentials.",
                    code="NeedsCredentials",
                    required=["email", "password"],
                    domain="uber.com",
                )
            await _bs_type_secret(_AUTH, pw_box, domain, account=email)
            cont = _find_ref(snap, role="button", name_re=r"^continue$|next|sign in|log in|submit")
            if cont:
                snap = await _bs_click(_AUTH, cont)
            await asyncio.sleep(1.2)
            snap = await _bs_snapshot(_AUTH)
            screen = _detect_auth_screen(snap)

    rides = await _rides_account_if_live()
    if rides:
        return rides

    if screen == "card_challenge" or _detect_auth_screen(snap) == "card_challenge":
        return await browser_session.login_window(
            _AUTH_LOGIN_URL,
            label="Uber card verification",
            instructions=(
                "Uber is asking for payment-card digits (lockout risk if guessed). "
                "Finish that step in the headed window, then poll check_session. "
                "When done, call login_window close=true."
            ),
        )

    if screen == "otp_picker" or _detect_auth_screen(snap) == "otp_picker":
        # From password success, Uber often lands on OTP / 2FA picker.
        # Do NOT run this branch on identifier — `\bemail\b` used to match
        # the phone/email textbox and "request" a fake channel.
        picked = await _pick_otp_channel(snap, method_order)
        if not picked:
            # Try More options → Email explicitly
            more = _find_ref(snap, role="button", name_re=r"more options|other options|try another")
            if more:
                snap = await _bs_click(_AUTH, more)
                await asyncio.sleep(0.6)
                snap = await _bs_snapshot(_AUTH)
                picked = await _pick_otp_channel(snap, method_order)
        if not picked:
            return await browser_session.login_window(
                _AUTH_LOGIN_URL,
                label="Uber sign-in",
                instructions=(
                    "Could not find an agent-readable OTP channel on the Uber "
                    "auth screen. Finish sign-in in the headed window, then poll "
                    "check_session. Call login_window close=true when done."
                ),
            )
        channel, ref = picked
        return await _issue_otp_after_channel_click(
            email=email, channel=channel, ref=ref, step="otp"
        )

    if screen == "otp_entry":
        # Code already requested (Uber defaulted to a channel). Prefer switching
        # to the ordered channel; otherwise stamp whatever the screen says.
        switch = await _pick_otp_channel(snap, method_order)
        if switch:
            channel, ref = switch
            name = next((n.get("name") for n in _snap_nodes(snap) if n.get("ref") == ref), "")
            if name and not _re.search(r"^\d$", name or ""):
                return await _issue_otp_after_channel_click(
                    email=email, channel=channel, ref=ref, step="otp"
                )
        seen = _infer_otp_channel(snap) or (method_order[0] if method_order else "email")
        return _auth_challenge_for_channel(email=email, channel=seen, step="otp")

    return {"__error": f"unhandled_auth_screen:{screen}", "text": _page_text(snap)[:400]}

async def _rides_account_if_live(last_auth_method: str | None = None) -> dict | None:
    try:
        data = await _gql("CurrentUserRidersWeb", CURRENT_USER_QUERY)
    except RuntimeError:
        return None
    user = data.get("currentUser")
    if not user:
        return None
    identifier = user.get("email") or user.get("uuid")
    if not identifier:
        return None
    out = {
        "authenticated": True,
        "at": _UBER,
        "identifier": identifier,
        "email": user.get("email"),
        "display": f"{user.get('firstName', '')} {user.get('lastName', '')}".strip(),
        "userId": user.get("uuid"),
    }
    meta = _uber_meta(last_auth_method=last_auth_method)
    if meta:
        out["metadata"] = meta
    return out

async def _eats_account_if_live(last_auth_method: str | None = None) -> dict | None:
    """Honest Eats session check.

    ``getUserV1`` often 403s (rtapi.forbidden) even on a live session — do NOT
    use it as the auth signal. Draft-list success and/or session cookies are
    the write-capable proof.
    """
    # Cookie gate first — cheap, httpOnly-honest.
    jar_ok = False
    for name in _EATS_SESSION_COOKIES:
        try:
            if await browser_session.session_cookie_present(_EATS, name):
                jar_ok = True
                break
        except Exception:
            pass

    try:
        sess = await _ueats("session")
    except RuntimeError:
        sess = {}
    if not isinstance(sess, dict):
        sess = {}

    if not sess.get("authenticated"):
        if not jar_ok:
            return None
        # Cookie present but drafts failed — still treat as authed enough to
        # surface identity from rides SSO when possible.
        rides = await _rides_account_if_live(last_auth_method=last_auth_method)
        if rides:
            return {
                "authenticated": True,
                "at": _UBER_EATS,
                "identifier": rides.get("identifier"),
                "email": rides.get("email"),
                "display": rides.get("display"),
                "metadata": rides.get("metadata") or _uber_meta(last_auth_method=last_auth_method),
            }
        return None

    # Session probe succeeded → Eats tab is warm enough.
    identifier = None
    display = None
    rides = await _rides_account_if_live(last_auth_method=last_auth_method)
    if rides:
        identifier = rides.get("identifier")
        display = rides.get("display")
    if not identifier:
        # Fall back to any email we can resolve from credentials
        identifier = await _resolve_uber_email() or "uber-session"
    out = {
        "authenticated": True,
        "at": _UBER_EATS,
        "identifier": identifier,
        "email": identifier if isinstance(identifier, str) and "@" in identifier else None,
        "display": display or identifier,
    }
    meta = _uber_meta(last_auth_method=last_auth_method)
    if meta:
        out["metadata"] = meta
    # Stash draft count for debug — not part of account shape contract
    if sess.get("draftCount") is not None:
        out.setdefault("metadata", {}).setdefault("uber", {})["draftCount"] = sess["draftCount"]
    return out

