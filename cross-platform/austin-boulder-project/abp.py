"""Austin Boulder Project — Tilefive Portal API.

Two connections:
  - public: https://widgets.api.prod.tilefive.com (unauth — schedule, locations)
  - portal: https://portal.api.prod.tilefive.com  (authed — bookings, memberships)

Authentication: the portal is a CloudFront-fronted static SPA that
authenticates against AWS Cognito via `USER_PASSWORD_AUTH`. The `login`
tool resolves `{email, password}` from a credential provider
(`credentials.retrieve(".approach.app", required=["email","password"])`
— 1Password or any other `@provides("login_credentials")` app),
runs the Cognito handshake, and persists the resulting `{email,
password, idToken, refreshToken}` in the credential store via
`__secrets__`. The portal connection's jaq `Authorization: .auth.idToken`
template declares the field; tools also refresh near-expiry IdTokens
via Cognito `REFRESH_TOKEN_AUTH` (~1h TTL).
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from agentos import (
    account,
    claims,
    client,
    connection,
    credentials,
    normalize_email,
    provides,
    returns,
    app_error,
    app_secret,
    test,
    canonicalize_datetime,
)

connection("public",
    description="Tilefive widgets API — locations, schedule. No auth.",
    base_url="https://widgets.api.prod.tilefive.com",
    client="api")

connection("portal",
    description="Tilefive portal API — bookings, memberships, passes.",
    base_url="https://portal.api.prod.tilefive.com",
    domain=".approach.app",
    client="api",
    auth={"type": "api_key", "header": {"Authorization": ".auth.idToken"}},
    optional=True,
    label="ABP Portal Session",
    help_url="https://boulderingproject.portal.approach.app/login")

NAMESPACE = "boulderingproject"
PORTAL_ORIGIN = "https://boulderingproject.portal.approach.app"
PORTAL_API = "https://portal.api.prod.tilefive.com"
WIDGETS_API = "https://widgets.api.prod.tilefive.com"
COGNITO_ENDPOINT = "https://cognito-idp.us-east-1.amazonaws.com/"
CAL_PAGE_SIZE = 50

AUSTIN_SPRINGDALE_ID = 6
AUSTIN_WESTGATE_ID = 5

_ABP_ORG = {
    "shape": "organization",
    "name": "Austin Boulder Project",
    "url": "https://austinboulderingproject.com",
}

# Portal cancel policy (from booking confirmation email).
_CANCEL_CONDITIONS = {
    "cancel": (
        "Cancel at least 24 hours before start for a full class-credit refund. "
        "Day-of cancel allowed up to 1 hour before start; later cancels and "
        "no-shows may forfeit the credit."
    ),
}

# locationId → IANA tz from widgets /locations (Tilefive `timeZone`)
_location_tz_cache: dict[int, str] = {}
# locationId → display name ("Austin Springdale", …)
_location_name_cache: dict[int, str] = {}

# ---------------------------------------------------------------------------
# Config discovery — widgets API key, Cognito pool/client are in the bundle
# ---------------------------------------------------------------------------

_RE_BUNDLE_URL  = re.compile(r'src="/assets/(app-[A-Za-z0-9_-]+\.js)"')
_RE_WIDGETS_KEY = re.compile(r'widgetsApiKey:\{"us-east-1":"([^"]{30,})"')
_RE_POOL_ID     = re.compile(r'userPoolId:"(us-east-1_[A-Za-z0-9]+)"')
_RE_CLIENT_ID   = re.compile(r'userPoolClientId:"([A-Za-z0-9]{20,60})"')

_config_cache: dict | None = None


async def _discover_config(force: bool = False) -> dict:
    """Extract widgetsApiKey + Cognito pool/client from the portal bundle.

    Same values every visitor sees; we re-read at runtime so the app
    survives Tilefive redeploys without shipping a new app version.
    """
    global _config_cache
    if _config_cache and not force:
        return _config_cache

    html = await client.get(PORTAL_ORIGIN)
    if html["status"] >= 400:
        raise RuntimeError(f"portal HTML fetch failed: {html['status']}")
    m = _RE_BUNDLE_URL.search(html["body"] or "")
    if not m:
        raise RuntimeError("portal HTML has no app-*.js bundle reference")
    bundle_url = f"{PORTAL_ORIGIN}/assets/{m.group(1)}"

    bundle = await client.get(bundle_url, headers={
        "Referer": f"{PORTAL_ORIGIN}/",
        "Origin": PORTAL_ORIGIN,
    })
    text = bundle["body"] or ""
    km = _RE_WIDGETS_KEY.search(text)
    pm = _RE_POOL_ID.search(text)
    cm = _RE_CLIENT_ID.search(text)
    if not (km and pm and cm):
        missing = [n for n, v in [("widgetsApiKey", km), ("poolId", pm), ("clientId", cm)] if not v]
        raise RuntimeError(f"bundle missing {missing} — regex patterns may need updating")

    _config_cache = {
        "widgetsApiKey": km.group(1),
        "cognitoPoolId": pm.group(1),
        "cognitoClientId": cm.group(1),
    }
    return _config_cache


# ---------------------------------------------------------------------------
# Authed token — USER_PASSWORD_AUTH + REFRESH_TOKEN_AUTH
# ---------------------------------------------------------------------------

async def _cognito_initiate_auth(email: str, password: str) -> dict:
    """Run Cognito USER_PASSWORD_AUTH. Returns the full AuthenticationResult.

    Callers use `IdToken` as the portal bearer token and `RefreshToken`
    to mint fresh IdTokens without re-prompting for the password. The
    returned dict shape is Cognito's — `{IdToken, AccessToken,
    RefreshToken, ExpiresIn, TokenType}`.
    """
    if not email or not password:
        raise ValueError("email and password required for Cognito auth")
    cfg = await _discover_config()
    resp = await client.post(
        COGNITO_ENDPOINT,
        json={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": cfg["cognitoClientId"],
            "AuthParameters": {
                "USERNAME": email.strip(),
                "PASSWORD": password.strip(),
            },
        },
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
    )
    if resp["status"] >= 400:
        raise RuntimeError(
            f"Cognito login failed: {resp['status']} "
            f"{(resp.get('body') or '')[:200]}"
        )
    return resp["json"]["AuthenticationResult"]


class _AuthRequired(Exception):
    """Session cannot be reminted — surface as NeedsAuth, not a Python crash."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _cognito_body_is_not_authorized(body: str | None) -> bool:
    text = body or ""
    return "NotAuthorizedException" in text or "Invalid Refresh Token" in text


async def _cognito_refresh_auth(refresh_token: str) -> dict:
    """Mint a fresh IdToken via Cognito REFRESH_TOKEN_AUTH."""
    cfg = await _discover_config()
    resp = await client.post(
        COGNITO_ENDPOINT,
        json={
            "AuthFlow": "REFRESH_TOKEN_AUTH",
            "ClientId": cfg["cognitoClientId"],
            "AuthParameters": {"REFRESH_TOKEN": refresh_token},
        },
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
    )
    if resp["status"] >= 400:
        body = (resp.get("body") or "")[:200]
        raise RuntimeError(f"Cognito refresh failed: {resp['status']} {body}")
    return resp["json"]["AuthenticationResult"]


def _jwt_exp(token: str) -> int | None:
    """Read `exp` from a JWT payload without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return int(payload["exp"]) if payload.get("exp") is not None else None
    except Exception:
        return None


def _id_token_expired(token: str, skew_s: int = 120) -> bool:
    exp = _jwt_exp(token)
    if exp is None:
        return True
    return time.time() >= (exp - skew_s)


def _session_secret(
    *,
    email: str,
    password: str,
    id_token: str,
    refresh_token: str | None,
    access_token: str | None = None,
    expires_in=None,
    token_type: str | None = None,
) -> dict:
    canonical = normalize_email(email)
    return app_secret(
        domain=".approach.app",
        identifier=canonical,
        item_type="login_credentials",
        value={
            "email": canonical,
            "password": password,
            "idToken": id_token,
            "refreshToken": refresh_token,
            "accessToken": access_token,
            "expiresIn": expires_in,
        },
        source="austin-boulder-project",
        metadata={
            "masked": {
                "password": "••••••••",
                "idToken": f"•••{id_token[-6:]}" if id_token else None,
            },
            "tokenType": token_type,
        },
    )


def _attach_secrets(result: Any, secrets: list[dict] | None) -> Any:
    """Wrap a tool result so refreshed tokens land in the vault writeback."""
    if not secrets:
        return result
    return {"__secrets__": secrets, "__result__": result}


async def _ensure_fresh_id_token(params: dict) -> tuple[str, list[dict]]:
    """Return a live IdToken, refreshing via refreshToken when near expiry.

    Mutates `params["auth"]` in place so later reads in the same call see
    the fresh token. Returns `(id_token, secrets_for_writeback)`.

    If the vault handed us a password-only row (newer 1Password import
    beating an app-minted session), remint via Cognito USER_PASSWORD_AUTH.
    Dead refresh tokens fall through to password remint; if that also
    fails, raise `_AuthRequired` so tools return `NeedsAuth` cleanly.
    """
    auth = dict(params.get("auth") or {})
    token = auth.get("idToken") or ""
    refresh = auth.get("refreshToken")
    email = auth.get("email") or auth.get("identifier") or ""
    password = auth.get("password") or ""
    secrets: list[dict] = []

    if token and not _id_token_expired(token):
        return token, secrets

    if refresh:
        try:
            result = await _cognito_refresh_auth(refresh)
        except RuntimeError as err:
            body = str(err)
            # Stale/revoked refresh — drop it and try password remint below.
            if not _cognito_body_is_not_authorized(body):
                raise
            auth.pop("refreshToken", None)
            params["auth"] = auth
            refresh = None
        else:
            token = result["IdToken"]
            access = result.get("AccessToken")
            auth["idToken"] = token
            if access:
                auth["accessToken"] = access
            params["auth"] = auth
            if email and password:
                secrets.append(_session_secret(
                    email=email,
                    password=password,
                    id_token=token,
                    refresh_token=refresh,
                    access_token=access,
                    expires_in=result.get("ExpiresIn"),
                    token_type=result.get("TokenType"),
                ))
            return token, secrets

    # Password-only vault row (or expired/dead refresh) — remint.
    if email and password and (not token or _id_token_expired(token) or not refresh):
        try:
            result = await _cognito_initiate_auth(email, password)
        except RuntimeError as err:
            raise _AuthRequired(
                "ABP login failed — check the password for "
                f"{email} (.approach.app), then run austin-boulder-project.login."
            ) from err
        token = result["IdToken"]
        refresh = result.get("RefreshToken")
        access = result.get("AccessToken")
        auth["idToken"] = token
        if refresh:
            auth["refreshToken"] = refresh
        if access:
            auth["accessToken"] = access
        params["auth"] = auth
        secrets.append(_session_secret(
            email=email,
            password=password,
            id_token=token,
            refresh_token=refresh,
            access_token=access,
            expires_in=result.get("ExpiresIn"),
            token_type=result.get("TokenType"),
        ))
        return token, secrets

    if not token or _id_token_expired(token):
        raise _AuthRequired(
            "ABP session expired — sign in again "
            "(austin-boulder-project.login / Settings → Accounts)."
        )
    # Expired with no refresh — try the stale token; caller may 401.
    return token, secrets


def _portal_headers(id_token: str) -> dict:
    # Authorization is also declared on the connection jaq template; we
    # still set it explicitly (plus Origin/Referer Tilefive requires).
    return {
        "Authorization": id_token,
        "Origin": PORTAL_ORIGIN,
        "Referer": f"{PORTAL_ORIGIN}/",
    }


def _widgets_headers(widgets_key: str) -> dict:
    return {
        "X-Api-Key": widgets_key,
        "Authorization": NAMESPACE,   # namespace, not a JWT — API-Gateway tenant routing
        "Origin": PORTAL_ORIGIN,
        "Referer": f"{PORTAL_ORIGIN}/",
    }


async def _authed_get(params: dict, path: str, query: dict | None = None) -> tuple[Any, list[dict]]:
    token, secrets = await _ensure_fresh_id_token(params)
    resp = await client.get(
        f"{PORTAL_API}{path}",
        headers=_portal_headers(token),
        params=query,
    )
    if resp["status"] in (401, 403) or (
        resp["status"] >= 400 and _cognito_body_is_not_authorized(resp.get("body"))
    ):
        raise _AuthRequired(
            "ABP session expired — sign in again "
            "(austin-boulder-project.login / Settings → Accounts)."
        )
    if resp["status"] >= 400:
        raise RuntimeError(
            f"GET {path} -> {resp['status']}: {(resp.get('body') or '')[:200]}"
        )
    return resp["json"], secrets


def _auth_required_error(err: _AuthRequired) -> Any:
    return app_error(
        err.message,
        code="NeedsAuth",
        domain=".approach.app",
        help_url="https://boulderingproject.portal.approach.app/login",
    )


async def _current_customer_id(params: dict, token: str) -> int:
    """Portal endpoint that returns the authenticated user profile."""
    resp = await client.get(f"{PORTAL_API}/customers", headers=_portal_headers(token))
    if resp["status"] >= 400:
        raise RuntimeError(f"GET /customers -> {resp['status']}")
    return int(resp["json"]["id"])


async def _active_membership_id(token: str) -> int | None:
    """Find the active membership to bill a class reservation against.

    Tilefive's booking endpoint needs an explicit `membershipId` — even if
    the user has only one active membership, omitting it yields a cryptic
    "Pass or Membership required" error. We pick the first active row;
    users with multiple actives will want a per-class pick UX eventually.
    """
    resp = await client.get(f"{PORTAL_API}/customers/memberships", headers=_portal_headers(token))
    if resp["status"] >= 400:
        return None
    for m in resp["json"] or []:
        if m.get("isActive") and (m.get("status") or "").lower() == "active":
            return int(m["id"])
    return None


# ---------------------------------------------------------------------------
# Entity helpers
# ---------------------------------------------------------------------------

def _user_timezone_name() -> str:
    """OS user IANA zone — same source as engine `user_environment.timezone`.

    Used only for user-relative "today" / "this morning". Venue-bound
    windows use the location's provider `timeZone` instead
    (shapes-overview §10b).
    """
    try:
        link = os.readlink("/etc/localtime")
        for marker in ("/zoneinfo/",):
            if marker in link:
                return link.split(marker, 1)[1]
    except OSError:
        pass
    # Last resort: whatever the process local tz reports (still not a city constant).
    local = datetime.now().astimezone().tzinfo
    key = getattr(local, "key", None)
    return key or "UTC"


def _provider_timezone(*objs: dict | None) -> str | None:
    """Tilefive stamps `timeZone` (camelCase) on locations, bookings, events."""
    for obj in objs:
        if not obj:
            continue
        tz = obj.get("timeZone") or obj.get("timezone")
        if tz:
            return str(tz)
    return None


def _instant(value: str | None) -> str | None:
    """Outbound datetime → UTC ``…Z`` (Tilefive is usually already Z)."""
    if not value:
        return None
    return canonicalize_datetime(str(value))


def _day_window_utc(
    date_str: str | None,
    days: int,
    location_tz_name: str,
) -> tuple[str, str]:
    """UTC [start, end] covering `days` local calendar days at the venue.

    When `date` is omitted, the start calendar date is "today" in the OS
    user timezone; that date is then interpreted in the *location*
    timezone for midnight boundaries (venue-bound schedule query).
    """
    loc_tz = ZoneInfo(location_tz_name)
    if date_str:
        start_local_date = datetime.fromisoformat(date_str).date()
    else:
        user_tz = ZoneInfo(_user_timezone_name())
        start_local_date = datetime.now(user_tz).date()
    start_local = datetime.combine(start_local_date, datetime.min.time(), tzinfo=loc_tz)
    end_local = start_local + timedelta(days=days)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc) - timedelta(milliseconds=1)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return iso(start_utc), iso(end_utc)


async def _fetch_locations_raw() -> list[dict]:
    cfg = await _discover_config()
    resp = await client.get(
        f"{WIDGETS_API}/locations",
        headers=_widgets_headers(cfg["widgetsApiKey"]),
    )
    if resp["status"] >= 400:
        raise RuntimeError(f"/locations -> {resp['status']}")
    raw = resp["json"]
    rows = raw.get("data") if isinstance(raw, dict) else raw
    return list(rows or [])


async def _timezone_for_location(location_id: int) -> str:
    """Resolve IANA tz for a Tilefive location id (cached)."""
    lid = int(location_id)
    if lid in _location_tz_cache:
        return _location_tz_cache[lid]
    for loc in await _fetch_locations_raw():
        tz = _provider_timezone(loc)
        if loc.get("id") is not None and tz:
            _location_tz_cache[int(loc["id"])] = tz
    if lid not in _location_tz_cache:
        raise RuntimeError(
            f"No timeZone on Tilefive location {lid} — cannot build schedule window"
        )
    return _location_tz_cache[lid]


def _location_to_entity(loc: dict) -> dict:
    """Tilefive location → generic `place` shape."""
    tz = _provider_timezone(loc)
    lid = loc.get("id")
    name = loc.get("name") or loc.get("locationName") or (
        f"ABP Location {lid}" if lid is not None else "ABP Location"
    )
    if lid is not None:
        if tz:
            _location_tz_cache[int(lid)] = tz
        _location_name_cache[int(lid)] = name
    return {
        "id": lid,
        "at": "austin-boulder-project",   # namespace so membership.location stubs resolve
        "name": name,
        "street": loc.get("address1"),
        "city": loc.get("city"),
        "region": loc.get("state"),
        "postalCode": loc.get("postalCode") or loc.get("zipCode"),
        "countryCode": loc.get("countryCode") or "US",
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
        "phone": loc.get("phone"),
        "timezone": tz,
        "featureType": "poi",
    }


async def _warm_location_cache() -> None:
    """Fill id→name (and tz) from widgets /locations for booking stubs."""
    for loc in await _fetch_locations_raw():
        if not isinstance(loc, dict) or loc.get("id") is None:
            continue
        _location_to_entity(loc)


_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")
# Class titles encode the instructor as "Flow w/Todd C" when Tilefive's
# `staff` / `staffHasBooking` arrays are empty (typical on /cal).
_RE_INSTRUCTOR_SUFFIX = re.compile(r"\s+w/\s*(.+)$", re.IGNORECASE)


def _strip_html(html: str | None) -> str | None:
    """Tilefive stores class descriptions as HTML blobs with inline
    styles. Strip tags for a plain-text rendering suitable for agent
    reasoning and terse UI. Keeps the prose; drops the markup noise.
    """
    if not html:
        return None
    text = _RE_HTML_TAG.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = _RE_WS.sub(" ", text).strip()
    return text or None


def _event_activities(event: dict | None) -> list[dict]:
    """Tilefive renamed the nested activity list to `activities`.

    Older captures (and our first mapper) used `activitys` — accept both
    so a tenant still on the typo spelling doesn't lose activityType.
    """
    if not isinstance(event, dict):
        return []
    raw = event.get("activities")
    if raw is None:
        raw = event.get("activitys")
    return list(raw) if isinstance(raw, list) else []


def _staff_person(staff_row: dict) -> dict | None:
    """Map a Tilefive staff row → `person` (when /cal actually fills staff)."""
    if not isinstance(staff_row, dict):
        return None
    sid = staff_row.get("id") or staff_row.get("staffId") or staff_row.get("userId")
    first = (staff_row.get("firstName") or "").strip()
    last = (staff_row.get("lastName") or "").strip()
    name = (
        staff_row.get("name")
        or staff_row.get("displayName")
        or " ".join(p for p in (first, last) if p).strip()
    )
    if not name and sid is None:
        return None
    person: dict[str, Any] = {
        "shape": "person",
        "at": "austin-boulder-project",
        "name": name or f"Instructor {sid}",
    }
    if sid is not None:
        person["id"] = int(sid) if str(sid).isdigit() else sid
        person["identities"] = [{
            "platform": "austin-boulder-project",
            "id": f"staff:{sid}",
        }]
    if first:
        person["givenName"] = first
    if last:
        person["familyName"] = last
    image = (
        staff_row.get("imageURL")
        or staff_row.get("imageUrl")
        or staff_row.get("avatarURL")
        or staff_row.get("photoURL")
        or staff_row.get("image")
    )
    if image:
        person["image"] = image
    return person


def _instructor_from_name(class_name: str | None) -> dict | None:
    """Parse `Class Name w/Instructor` → person when staff arrays are empty."""
    if not class_name:
        return None
    m = _RE_INSTRUCTOR_SUFFIX.search(str(class_name).strip())
    if not m:
        return None
    raw = m.group(1).strip()
    if not raw:
        return None
    # "Gillian E" / "Todd C" — given + optional initial family.
    parts = raw.split()
    person: dict[str, Any] = {
        "shape": "person",
        "at": "austin-boulder-project",
        "name": raw,
        "identities": [{
            "platform": "austin-boulder-project",
            "id": f"instructor:{raw.lower()}",
            "handle": raw,
        }],
    }
    if parts:
        person["givenName"] = parts[0]
    if len(parts) >= 2:
        person["familyName"] = parts[-1]
    return person


def _performer_for_booking(b: dict, event: dict) -> dict | None:
    """Instructor as schema.org performer → `person`.

    Prefer Tilefive `staff` / `staffHasBooking` when populated; otherwise
    parse the `w/` suffix from the class title. Instructor *photos* are
    not on `/cal` today (staff arrays arrive empty) — see dev/requirements.md.
    """
    for row in (b.get("staff") or []) + (b.get("staffHasBooking") or []) + (event.get("staff") or []):
        person = _staff_person(row if isinstance(row, dict) else {})
        if person:
            return person
    return _instructor_from_name(b.get("name") or event.get("name"))


def _class_image(b: dict, event: dict, activities: list[dict]) -> str | None:
    """Class photo: event image first, then activity category avatar."""
    for src in (
        b.get("imageURL"),
        b.get("imageUrl"),
        event.get("imageURL"),
        event.get("imageUrl"),
    ):
        if src:
            return str(src)
    for act in activities:
        if isinstance(act, dict) and (act.get("imageURL") or act.get("imageUrl")):
            return str(act.get("imageURL") or act.get("imageUrl"))
    return None


def _booking_to_entity(b: dict) -> dict:
    """Tilefive BookingInstance → `class` (schedule discovery)."""
    event = b.get("event", {}) or {}
    if not isinstance(event, dict):
        event = {}
    loc = b.get("location") or {}
    if not isinstance(loc, dict):
        loc = {}
    activities = _event_activities(event)
    activity_name = ""
    if activities and isinstance(activities[0], dict):
        activity_name = activities[0].get("name") or ""
    # Tilefive widget response uses `customerCount` (currently reserved)
    # and `event.maxCustomers` (capacity). Prefer those over the legacy
    # `ticketsRemaining` field (often 0 even when spots remain).
    taken = b.get("customerCount")
    capacity = event.get("maxCustomers") or b.get("maxNumOfGuests")
    spots = (capacity - taken) if (capacity is not None and taken is not None) else None
    full = spots == 0 if spots is not None else False
    desc = []
    if activity_name: desc.append(activity_name)
    if full: desc.append("FULL")
    elif spots is not None and capacity is not None: desc.append(f"{spots}/{capacity} spots")
    location_id = b.get("locationId") or loc.get("id") or event.get("locationId")
    tz = _provider_timezone(b, event, loc)
    if location_id is not None and tz:
        _location_tz_cache[int(location_id)] = tz
    image = _class_image(b, event, activities)
    performer = _performer_for_booking(b, event)
    out = {
        "id": b["id"],
        "at": "austin-boulder-project",
        "name": b["name"],
        "content": " — ".join(desc),
        "description": _strip_html(b.get("description") or event.get("description")),
        "startDate": _instant(b.get("startDT")),
        "endDate": _instant(b.get("endDT")),
        "timezone": tz,
        "activityType": activity_name,
        "capacity": capacity,
        "customerCount": taken,
        "spotsRemaining": spots,
        "isFull": full,
    }
    if image:
        out["image"] = image
    loc_stub = _hydrate_location(location_id, loc)
    if loc_stub:
        out["location"] = loc_stub
    if performer:
        # schema.org/EducationEvent performer = instructor (class.yaml).
        out["performer"] = performer
    return out


def _normalize_reservation_status(raw: str | None, *, cancelled: bool = False) -> str:
    if cancelled:
        return "cancelled"
    s = (raw or "confirmed").strip().lower()
    if s in ("active", "booked", "reserved", "confirmed", ""):
        return "confirmed"
    if s in ("cancelled", "canceled"):
        return "cancelled"
    if s in ("pending", "hold", "completed", "no_show", "changed"):
        return s
    return s or "confirmed"


def _class_stub_from_booking(b: dict | None, booking_instance_id: int | None = None) -> dict | None:
    """Identity stub (+ light fields) for the booked class."""
    bid = booking_instance_id
    name = None
    start = end = tz = None
    if b:
        bid = bid or b.get("id") or b.get("bookingId") or b.get("bookingInstanceId")
        name = b.get("name")
        start = b.get("startDT") or b.get("startDate")
        end = b.get("endDT") or b.get("endDate")
        tz = _provider_timezone(b, b.get("event") if isinstance(b.get("event"), dict) else None)
    if bid is None:
        return None
    stub = {
        "at": "austin-boulder-project",
        "id": int(bid),
        "shape": "class",
    }
    if name: stub["name"] = name
    if start: stub["startDate"] = _instant(start)
    if end: stub["endDate"] = _instant(end)
    if tz: stub["timezone"] = tz
    return stub


def _reservation_from_portal(
    *,
    reservation_id,
    booking_instance_id=None,
    status: str = "confirmed",
    name: str | None = None,
    start=None,
    end=None,
    timezone_name: str | None = None,
    location_id=None,
    location_nested: dict | None = None,
    email: str | None = None,
    booking: dict | None = None,
    party_size: int | None = None,
    booking_time=None,
    cancelled: bool = False,
    description: str | None = None,
    image: str | None = None,
    performer: dict | None = None,
    activity_type: str | None = None,
    raw: dict | None = None,
    include_raw: bool = False,
) -> dict:
    """Build a `reservation` node for a fitness-class booking."""
    rid = str(reservation_id)
    norm_status = _normalize_reservation_status(status, cancelled=cancelled)
    actions = ["cancel"] if norm_status == "confirmed" else []
    out = {
        "id": f"abp-res:{rid}",
        "at": _ABP_ORG,
        "reservationType": "fitness_class",
        "reservationId": rid,
        "status": norm_status,
        "bookingType": "instant",
        "name": name or f"ABP class reservation {rid}",
        "startTime": _instant(start),
        "endTime": _instant(end),
        "startDate": _instant(start),
        "endDate": _instant(end),
        "timezone": timezone_name,
        "availableActions": actions,
        "partySize": party_size if party_size is not None else 1,
        "bookingTime": booking_time,
        "conditions": _CANCEL_CONDITIONS,
    }
    if description:
        out["description"] = description
    if image:
        out["image"] = image
    if performer:
        out["performer"] = performer
    if activity_type:
        out["activityType"] = activity_type
    loc = _hydrate_location(location_id, location_nested)
    if loc:
        out["location"] = loc
    cls = _class_stub_from_booking(booking, booking_instance_id)
    if cls:
        out["event"] = cls
    acct = _account_stub(email)
    if acct:
        out["account"] = acct
    if booking_instance_id is not None:
        out["_bookingInstanceId"] = int(booking_instance_id)
    if include_raw and raw is not None:
        out["_raw"] = raw
    return out


def _customer_booking_to_reservation(
    row: dict, email: str | None = None, *, include_raw: bool = False,
) -> dict:
    """Map GET /customers/bookings row → `reservation`.

    Tilefive's customer bookings list returns BookingInstance-shaped
    rows. The portal reservation id lives on `customerHasBooking.id`
    (top-level `id` is the booking instance / class id).

    Carries the same class surface the schedule emits (description,
    image, instructor, activityType, named place) so the Reservations
    app doesn't have to fan out `reservation_get` just to paint a booking.
    """
    booking = row.get("booking") or row.get("bookingInstance") or {}
    if not isinstance(booking, dict):
        booking = {}
    # List endpoint: the row IS the booking instance.
    if not booking and row.get("startDT") and row.get("id"):
        booking = row
    event = booking.get("event") if isinstance(booking.get("event"), dict) else {}
    if not event and isinstance(row.get("event"), dict):
        event = row["event"]
    loc = booking.get("location") if isinstance(booking.get("location"), dict) else {}
    if not loc and isinstance(row.get("location"), dict):
        loc = row["location"]

    chb = row.get("customerHasBooking") or booking.get("customerHasBooking") or {}
    if not isinstance(chb, dict):
        chb = {}

    reservation_id = (
        chb.get("id")
        or row.get("reservationId")
        or (row.get("id") if chb else None)
        or row.get("id")
    )
    booking_instance_id = (
        chb.get("bookingId")
        or row.get("bookingId")
        or row.get("bookingInstanceId")
        or booking.get("id")
        or row.get("id")
    )
    location_id = (
        row.get("locationId")
        or booking.get("locationId")
        or loc.get("id")
        or event.get("locationId")
    )
    start = row.get("startDT") or row.get("startDate") or booking.get("startDT")
    end = row.get("endDT") or row.get("endDate") or booking.get("endDT")
    tz = _provider_timezone(row, booking, event, loc)
    if location_id is not None and tz:
        _location_tz_cache[int(location_id)] = tz
    name = row.get("name") or booking.get("name") or event.get("name")
    guests = chb.get("numGuests") or row.get("numGuests")
    party = (1 + int(guests)) if guests is not None else None
    status = (
        chb.get("bookingStatus")
        or chb.get("status")
        or row.get("status")
        or booking.get("status")
        or "confirmed"
    )
    # Enrichment source: the BookingInstance row (same fields as /cal).
    instance = booking if booking else row
    activities = _event_activities(event)
    activity_name = ""
    if activities and isinstance(activities[0], dict):
        activity_name = activities[0].get("name") or ""
    return _reservation_from_portal(
        reservation_id=reservation_id,
        booking_instance_id=booking_instance_id,
        status=status,
        name=name,
        start=start,
        end=end,
        timezone_name=tz,
        location_id=location_id,
        location_nested=loc or None,
        email=email,
        booking=booking or {
            "id": booking_instance_id,
            "name": name,
            "startDT": start,
            "endDT": end,
        },
        party_size=party,
        booking_time=chb.get("createdAt") or row.get("createdAt") or row.get("bookedAt"),
        description=_strip_html(
            instance.get("description") or event.get("description"),
        ),
        image=_class_image(instance, event, activities),
        performer=_performer_for_booking(instance, event),
        activity_type=activity_name or None,
        raw=row,
        include_raw=include_raw,
    )


def _parse_dt(value) -> datetime | None:
    """Parse Tilefive ISO timestamps (with or without Z / offset)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _reservation_is_upcoming(res: dict, now: datetime | None = None) -> bool:
    """True when the reservation is not cancelled and has not ended yet."""
    if (res.get("status") or "").lower() == "cancelled":
        return False
    now = now or datetime.now(timezone.utc)
    end = _parse_dt(res.get("endTime") or res.get("endDate"))
    start = _parse_dt(res.get("startTime") or res.get("startDate"))
    if end is not None:
        return end >= now
    if start is not None:
        return start >= now
    # Unknown times — keep (better than silently dropping).
    return True


async def _fetch_booking_instance(
    params: dict, booking_instance_id: int, token: str,
) -> dict | None:
    """Best-effort load of a BookingInstance for enriching sparse book/cancel."""
    # Portal sometimes exposes the instance directly.
    resp = await client.get(
        f"{PORTAL_API}/bookings/{int(booking_instance_id)}",
        headers=_portal_headers(token),
    )
    if resp["status"] < 400 and isinstance(resp.get("json"), dict):
        return resp["json"]
    # Fallback: find it on the customer's bookings list.
    try:
        raw, _ = await _authed_get(params, "/customers/bookings")
    except (_AuthRequired, RuntimeError):
        return None
    rows = raw if isinstance(raw, list) else (
        (raw or {}).get("data") or (raw or {}).get("bookings") or []
    )
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        bid = (
            row.get("id")
            or row.get("bookingId")
            or row.get("bookingInstanceId")
            or (row.get("booking") or {}).get("id")
        )
        chb = row.get("customerHasBooking") or {}
        if isinstance(chb, dict) and chb.get("bookingId") is not None:
            bid = bid or chb.get("bookingId")
        if bid is not None and int(bid) == int(booking_instance_id):
            return row
    return None


# ---------------------------------------------------------------------------
# Operations — public connection (no credentials)
# ---------------------------------------------------------------------------

@returns("void")
@connection("public")
async def public_authenticate(*, force: bool = False, **params) -> dict:
    """Force re-reading widgetsApiKey + Cognito config from the live bundle.

    Public because these values are embedded in the portal's own JS bundle.
    """
    return await _discover_config(force=bool(force))


@test
@returns("place[]")
@provides("reservation_locations")
@connection("public")
async def get_locations(**params) -> list[dict]:
    """List all Bouldering Project locations as place entities.

    Brokered as `reservation_locations` — public venue list for schedule filters.

    Austin has two — Springdale (id=6) and Westgate (id=5). Shape-
    typed so "what gyms are there?" works cross-app. Each place carries
    Tilefive's per-location `timeZone` (e.g. Seattle = America/Los_Angeles,
    Austin = America/Chicago) — never a brand-wide constant.
    """
    rows = await _fetch_locations_raw()
    return [_location_to_entity(loc) for loc in rows]


@test
@returns("class[]")
@provides("reservation_availability")
@connection("public")
async def get_schedule(
    location_id: int = AUSTIN_SPRINGDALE_ID,
    activity_ids: str | list | None = None,
    date: str | None = None,
    days: int = 3,
    **params,
) -> list[dict]:
    """Get the upcoming class schedule as `class` entities (also `event`).

    Brokered as `reservation_availability` — public (no account). The
    Reservations Commons app fans this out across every provider that
    declares it; ABP's bookable units happen to be fitness classes.

    Args:
        location_id:  6 = Austin Springdale (default), 5 = Austin Westgate
        activity_ids: "4,5,6" or [4,5,6] — Climbing(4), Yoga(5), Fitness(6)
        date:         YYYY-MM-DD; default: today in the OS user timezone
        days:         number of days from `date` (default 3)

    Day boundaries for the Tilefive `/cal` query use the *location's*
    timezone. Class nodes stamp that same IANA zone on `timezone`.

    Pages through `/cal` until complete — `days=7` no longer silently
    truncates at `pageSize=50`. Raises `IncompleteSchedule` if the
    provider response can't be fully collected.
    """
    if isinstance(activity_ids, str):
        ids = [int(x.strip()) for x in activity_ids.split(",") if x.strip()]
    elif isinstance(activity_ids, list):
        ids = [int(x) for x in activity_ids]
    else:
        ids = [4, 5, 6]

    loc_tz = await _timezone_for_location(int(location_id))
    start_dt, end_dt = _day_window_utc(date, days=int(days), location_tz_name=loc_tz)
    cfg = await _discover_config()
    headers = _widgets_headers(cfg["widgetsApiKey"])
    base_params = {
        "startDT": start_dt, "endDT": end_dt,
        "locationId": int(location_id),
        "activityId": ",".join(str(i) for i in ids),
        "pageSize": CAL_PAGE_SIZE,
    }

    all_bookings: list[dict] = []
    page = 1
    page_count: int | None = None
    while True:
        resp = await client.get(
            f"{WIDGETS_API}/cal",
            headers=headers,
            params={**base_params, "page": page},
        )
        if resp["status"] >= 400:
            raise RuntimeError(f"/cal -> {resp['status']}")
        data = resp["json"] or {}
        batch = data.get("bookings") or []
        all_bookings.extend(batch)
        pagination = data.get("pagination") or {}
        if pagination.get("pageCount") is not None:
            page_count = int(pagination["pageCount"])
        if page_count is not None:
            if page >= page_count:
                break
        elif len(batch) < CAL_PAGE_SIZE:
            break
        page += 1
        if page > 100:
            raise RuntimeError(
                f"IncompleteSchedule: /cal exceeded 100 pages for "
                f"location={location_id} days={days} "
                f"(collected {len(all_bookings)} bookings)"
            )
        if not batch:
            break

    # Safety: if pagination claimed one page but we filled pageSize and
    # days is large, refuse a silently truncated week.
    if (
        page_count == 1
        and len(all_bookings) >= CAL_PAGE_SIZE
        and int(days) >= 7
    ):
        raise RuntimeError(
            f"IncompleteSchedule: /cal returned pageCount=1 with "
            f"{len(all_bookings)} bookings (pageSize={CAL_PAGE_SIZE}) "
            f"for days={days} — refuse truncated week; narrow the window "
            f"or investigate Tilefive pagination."
        )

    
    
    
    return [_booking_to_entity(b) for b in all_bookings]


@test
@returns("class")
@provides("reservation_get")
@connection("public")
async def get_class(
    booking_instance_id: int | str | None = None,
    location_id: int | None = None,
    days: int = 14,
    **params,
) -> dict:
    """Hydrate one bookable class for detail view.

    Brokered as `reservation_get`. Walks the public schedule window
    (default 14 days) across Austin locations when `location_id` is
    omitted, and returns the matching BookingInstance as a `class`
    entity (also `event`). Accepts `booking_instance_id` or generic `id`.
    """
    raw_id = booking_instance_id if booking_instance_id is not None else params.get("id")
    if raw_id is None:
        raise RuntimeError("reservation_get requires booking_instance_id or id")
    bid = int(raw_id)
    loc_ids = (
        [int(location_id)]
        if location_id is not None
        else [AUSTIN_SPRINGDALE_ID, AUSTIN_WESTGATE_ID]
    )
    for lid in loc_ids:
        rows = await get_schedule(location_id=lid, days=int(days), **params)
        for row in rows:
            if int(row.get("id") or 0) == bid:
                return row
    raise RuntimeError(
        f"Class {bid} not found in the next {days} day(s) "
        f"at location(s) {loc_ids}"
    )


# ---------------------------------------------------------------------------
# Operations — portal connection (credentials required)
# ---------------------------------------------------------------------------

@test.skip(reason="destructive — actually books a class")
@returns("reservation")
@provides("reservation_create", account_param="account")
@connection("portal")
async def book_class(
    booking_instance_id: int | str | None = None,
    num_guests: int = 0,
    membership_id: int | None = None,
    **params,
) -> dict:
    """Book a class; returns a `reservation` (`reservationType: fitness_class`).

    Brokered as `reservation_create`. Accepts `booking_instance_id` or
    generic `id` (the bookable unit from `reservation_availability`).

    The booking is billed against a specific membership. If the caller
    doesn't pass `membership_id`, the app looks up the user's first
    active membership. Explicit override supported for multi-membership
    users.
    """
    raw_id = booking_instance_id if booking_instance_id is not None else params.get("id")
    if raw_id is None:
        return app_error(
            "reservation_create requires booking_instance_id or id",
            code="InvalidArgument",
        )
    booking_instance_id = int(raw_id)
    try:
        token, secrets = await _ensure_fresh_id_token(params)
        customer_id = await _current_customer_id(params, token)
    except _AuthRequired as err:
        return _auth_required_error(err)
    email = _email_from_credentials(params)
    if membership_id is None:
        membership_id = await _active_membership_id(token)
        if membership_id is None:
            return app_error(
                "No active membership or pass — purchase one at "
                "https://boulderingproject.portal.approach.app/ to book classes.",
                code="NeedsMembership",
            )
    resp = await client.post(
        f"{PORTAL_API}/bookings/{int(booking_instance_id)}/customers",
        headers=_portal_headers(token),
        json={
            "customerId": customer_id,
            "numGuests": int(num_guests),
            "membershipId": int(membership_id),
        },
    )
    if resp["status"] >= 400:
        body = resp.get("body") or ""
        j = resp.get("json") or {}
        msg = (j.get("message") if isinstance(j, dict) else None) or f"HTTP {resp['status']}: {body[:200]}"
        # The portal returns "Booking is already full" as its capacity
        # signal (via 404 on /bookings/{id}/customers). We enrich the
        # message with a suggestion to re-query the schedule so the
        # caller sees current fullness rather than assuming stale state.
        if "full" in msg.lower():
            msg = (
                f"{msg} Class {booking_instance_id} is at capacity. "
                "Call `get_schedule` to see other times with open spots."
            )
        raise RuntimeError(msg)
    j = resp.get("json") or {}
    booking = j.get("booking") if isinstance(j.get("booking"), dict) else {}
    # POST /customers often returns a sparse customerHasBooking row —
    # enrich from the booking instance when name/times/location missing.
    sparse = not (
        (j.get("name") or booking.get("name"))
        and (j.get("startDT") or booking.get("startDT"))
        and (
            j.get("locationId")
            or booking.get("locationId")
            or (booking.get("location") or {}).get("id")
        )
    )
    if sparse:
        enriched = await _fetch_booking_instance(params, int(booking_instance_id), token)
        if enriched:
            if enriched.get("startDT") or enriched.get("name"):
                booking = enriched
            else:
                inner = enriched.get("booking") or enriched.get("bookingInstance")
                if isinstance(inner, dict):
                    booking = {**booking, **inner}
                else:
                    booking = {**booking, **enriched}
    location_id = (
        j.get("locationId")
        or booking.get("locationId")
        or (booking.get("location") or {}).get("id")
    )
    start = j.get("startDT") or booking.get("startDT")
    end = j.get("endDT") or booking.get("endDT")
    name = j.get("name") or booking.get("name")
    tz = _provider_timezone(j, booking)
    if location_id is not None and not tz:
        try:
            tz = await _timezone_for_location(int(location_id))
        except RuntimeError:
            tz = None
    reservation = _reservation_from_portal(
        reservation_id=j.get("id"),
        booking_instance_id=j.get("bookingId") or booking_instance_id,
        status=j.get("status") or "confirmed",
        name=name,
        start=start,
        end=end,
        timezone_name=tz,
        location_id=location_id,
        email=email,
        booking=booking or {"id": booking_instance_id},
        party_size=1 + int(num_guests),
        booking_time=j.get("createdAt"),
    )
    if email:
        reservation["accountEmail"] = email
    return _attach_secrets(reservation, secrets)


@test.skip(reason="destructive — cancels a real reservation")
@returns("reservation")
@provides("reservation_cancel", account_param="account")
@connection("portal")
async def cancel_booking(
    reservation_id: int | str,
    booking_instance_id: int | str | None = None,
    **params,
) -> dict:
    """Cancel a class reservation; returns the reservation with status=cancelled.

    Brokered as `reservation_cancel`. `reservation_id` comes from
    `book_class` / `get_my_bookings` (`reservation.reservationId`).
    `booking_instance_id` is ABP-specific (Tilefive path); when omitted,
    look it up from the member's bookings.
    """
    rid_raw = str(reservation_id).removeprefix("abp-res:")
    rid = int(rid_raw)
    bid: int | None = None
    if booking_instance_id is not None:
        bid = int(booking_instance_id)
    else:
        event = params.get("event") if isinstance(params.get("event"), dict) else None
        if event and event.get("id") is not None:
            bid = int(event["id"])
        elif params.get("_bookingInstanceId") is not None:
            bid = int(params["_bookingInstanceId"])
    try:
        token, secrets = await _ensure_fresh_id_token(params)
    except _AuthRequired as err:
        return _auth_required_error(err)
    email = _email_from_credentials(params)
    if bid is None:
        mine = await get_my_bookings(include_past=True, **params)
        if isinstance(mine, dict) and mine.get("code"):
            return mine
        for row in mine if isinstance(mine, list) else []:
            if str(row.get("reservationId")) == str(rid):
                bid = int(row.get("_bookingInstanceId") or (row.get("event") or {}).get("id") or 0) or None
                break
    if bid is None:
        return app_error(
            f"Could not resolve booking_instance_id for reservation {rid}",
            code="InvalidArgument",
        )
    booking_instance_id = bid
    resp = await client.delete(
        f"{PORTAL_API}/bookings/{int(booking_instance_id)}/reservations/{rid}",
        headers=_portal_headers(token),
    )
    if resp["status"] >= 400:
        j = resp.get("json") or {}
        msg = j.get("message") if isinstance(j, dict) else None
        raise RuntimeError(msg or f"HTTP {resp['status']}")
    booking = {"id": booking_instance_id}
    enriched = await _fetch_booking_instance(params, int(booking_instance_id), token)
    if enriched:
        if enriched.get("startDT") or enriched.get("name"):
            booking = enriched
        else:
            inner = enriched.get("booking") or enriched.get("bookingInstance")
            booking = inner if isinstance(inner, dict) else enriched
    location_id = (
        booking.get("locationId")
        or (booking.get("location") or {}).get("id")
    )
    reservation = _reservation_from_portal(
        reservation_id=rid,
        booking_instance_id=booking_instance_id,
        status="cancelled",
        cancelled=True,
        name=booking.get("name"),
        start=booking.get("startDT"),
        end=booking.get("endDT"),
        timezone_name=_provider_timezone(booking),
        location_id=location_id,
        email=email,
        booking=booking,
    )
    return _attach_secrets(reservation, secrets)

def _email_from_credentials(params: dict) -> str | None:
    """Return the authed account's email.

    After Phase 1, the engine splats the credential row's value fields
    onto `params.auth`, so `params.auth.email` and
    `params.auth.identifier` both hold the canonical email.
    """
    auth = params.get("auth") or {}
    ident = auth.get("identifier") or auth.get("email")
    return str(ident) if ident else None


def _account_stub(email: str | None) -> dict | None:
    """Identity-stub for the account node — engine resolves by (at, identifier)."""
    if not email:
        return None
    return {"at": "austin-boulder-project", "identifier": email}


def _location_stub(location_id) -> dict | None:
    """Identity-stub for an ABP location place node."""
    if location_id is None:
        return None
    return {"at": "austin-boulder-project", "id": location_id}


def _hydrate_location(location_id, nested: dict | None = None) -> dict | None:
    """Location stub with a human name when we know one.

    Prefer a nested Tilefive `location` object, then the warmed
    `/locations` cache — never leave the UI stuck on `Location 6`.
    """
    stub = _location_stub(location_id)
    if not stub:
        return None
    name = None
    if isinstance(nested, dict):
        name = nested.get("name") or nested.get("locationName")
    if not name and location_id is not None:
        name = _location_name_cache.get(int(location_id))
    if name:
        stub = {**stub, "name": name, "shape": "place"}
    return stub


def _membership_to_entity(m: dict, email: str | None = None) -> dict:
    """Tilefive membership → generic `membership` shape."""
    mt = m.get("membershipType") or {}
    # Tilefive's `isRecurring` is 1/0; `billingType` is opaque (e.g. "DOP").
    # `durationType` (YEAR/MONTH/WEEK) is a cleaner standard cadence.
    cadence = (mt.get("durationType") or "").lower() or None
    cadence_map = {"year": "annual", "month": "monthly", "week": "weekly"}
    out = {
        "id": m["id"],
        "at": "austin-boulder-project",
        "name": mt.get("name") or f"Membership {m['id']}",
        "tier": mt.get("name"),
        "status": m.get("status"),
        "startEffectiveDate": m.get("startEffectiveDate"),
        "endEffectiveDate": m.get("endEffectiveDate"),
        "nextBillDate": m.get("nextBillDate"),
        "autoRenew": bool(m.get("isRecurring")),
        "price": m.get("price"),
        "currency": "USD",
        "billingType": cadence_map.get(cadence, cadence),
        "useCount": m.get("useCount"),
        "guestPassQuantity": m.get("guestPassQuantity"),
        "content": mt.get("description"),
    }
    acct = _account_stub(email)
    if acct: out["account"] = acct
    loc = _location_stub(m.get("purchasedLocationId"))
    if loc: out["location"] = loc
    return out


def _pass_to_entity(p: dict, email: str | None = None) -> dict:
    """Tilefive pass → generic `pass` shape."""
    pt = p.get("passType") or {}
    status = p.get("status") or ("depleted" if p.get("quantity") == 0 else "active")
    out = {
        "id": p["id"],
        "at": "austin-boulder-project",
        "name": pt.get("name") or f"Pass {p['id']}",
        "status": status,
        "purchasedDate": p.get("purchasedDate") or p.get("createdAt"),
        "startEffectiveDate": p.get("startEffectiveDate"),
        "endEffectiveDate": p.get("endEffectiveDate") or p.get("endEffectiveDT"),
        "quantity": p.get("quantity"),
        "purchasedQuantity": p.get("purchasedQuantity"),
        "isAllDayPass": bool(p.get("isAllDayPass")),
        "depletedDate": p.get("depletedDate"),
        "price": p.get("price"),
        "currency": "USD",
    }
    acct = _account_stub(email)
    if acct: out["account"] = acct
    loc = _location_stub(p.get("purchasedLocationId"))
    if loc: out["location"] = loc
    return out


@test.skip(reason="needs credentials")
@returns("membership[]")
@connection("portal")
async def get_my_memberships(include_expired: bool = False, **params) -> list[dict]:
    """List memberships held by the logged-in account.

    Emitted memberships link to both the `account` (ABP login) and
    the `location` (gym branch) so "what memberships do I have?" and
    "which gym?" work cross-app on the graph.

    Args:
        include_expired: when false (default), filter to `status=="active"`
            memberships only. Historical/cancelled/expired rows clutter
            the common "what am I paying for" query; callers who want the
            full history pass `include_expired=true`.
    """
    email = _email_from_credentials(params)
    try:
        raw, secrets = await _authed_get(params, "/customers/memberships")
    except _AuthRequired as err:
        return _auth_required_error(err)
    rows = raw or []
    if not include_expired:
        rows = [m for m in rows if (m.get("status") or "").lower() == "active"]
    return _attach_secrets([_membership_to_entity(m, email) for m in rows], secrets)


@test.skip(reason="needs credentials")
@returns("pass[]")
@connection("portal")
async def get_my_passes(**params) -> list[dict]:
    """List class passes held by the logged-in account."""
    email = _email_from_credentials(params)
    try:
        raw, secrets = await _authed_get(params, "/customers/passes")
    except _AuthRequired as err:
        return _auth_required_error(err)
    return _attach_secrets([_pass_to_entity(p, email) for p in (raw or [])], secrets)


@test.skip(reason="needs credentials")
@returns("reservation[]")
@provides("reservation_list", account_param="account")
@connection("portal")
async def get_my_bookings(
    include_past: bool = False,
    include_raw: bool = False,
    **params,
) -> list[dict]:
    """List the authenticated user's class reservations.

    Brokered as `reservation_list` (not `reservations` — that id is the
    Commons app). The app fans this out across every provider × account.

    Defaults to upcoming/active only (not cancelled, endTime still in
    the future). Pass `include_past=true` for history. Pass
    `include_raw=true` to attach Tilefive `_raw` payloads.

    Each row is a `reservation` with `reservationType: fitness_class`,
    linked to the gym `place` and the booked `class` (via `event`).
    """
    email = _email_from_credentials(params)
    # Warm place names before mapping so location stubs say
    # "Austin Springdale" instead of bare id 6.
    try:
        await _warm_location_cache()
    except Exception:
        pass
    try:
        raw, secrets = await _authed_get(params, "/customers/bookings")
    except _AuthRequired as err:
        return _auth_required_error(err)
    rows = raw if isinstance(raw, list) else (raw.get("data") or raw.get("bookings") or [])
    reservations = [
        _customer_booking_to_reservation(r, email, include_raw=include_raw)
        for r in (rows or [])
    ]
    if not include_past:
        reservations = [r for r in reservations if _reservation_is_upcoming(r)]
    # Stamp the mailbox-style account attribution the Commons app merges on.
    if email:
        for r in reservations:
            r.setdefault("accountEmail", email)
    return _attach_secrets(reservations, secrets)


# ---------------------------------------------------------------------------
# Identity — account.check + login
# ---------------------------------------------------------------------------


@account.check
@test.skip(reason="destructive or unsupported — migrated from yaml")
@returns("account")
@claims("primary_user")
@connection("portal")
async def check_session(**params) -> dict[str, Any]:
    """Verify the portal session and return the authed identity.

    Calls `/customers` on the portal API with the stored IdToken
    (refreshing first when near expiry). The response includes the
    Cognito subject plus the account email; the email is the canonical
    identifier.
    """
    auth = params.get("auth") or {}
    email_hint = _email_from_credentials(params)

    def _unauth(secrets: list[dict] | None = None) -> Any:
        out: dict[str, Any] = {
            "authenticated": False,
            "at": _ABP_ORG,
        }
        if email_hint:
            out["identifier"] = email_hint
        return _attach_secrets(out, secrets) if secrets else out

    if not auth.get("idToken") and not auth.get("refreshToken") and not (
        auth.get("email") and auth.get("password")
    ):
        return _unauth()
    try:
        token, secrets = await _ensure_fresh_id_token(params)
    except (_AuthRequired, RuntimeError):
        return _unauth()
    resp = await client.get(
        f"{PORTAL_API}/customers",
        headers=_portal_headers(token),
    )
    if resp["status"] >= 400:
        return _unauth(secrets)

    customer = resp["json"]
    canonical = normalize_email(customer["email"])
    display = " ".join(
        p for p in (customer.get("firstName"), customer.get("lastName")) if p
    ).strip()
    account_node = {
        "authenticated": True,
        "at": _ABP_ORG,
        "identifier": canonical,
        "email": canonical,
        "displayName": display,
        "userId": str(customer["id"]),
    }
    return _attach_secrets(account_node, secrets)


@account.login
@returns("account | auth_challenge")
@connection("public")
async def login(*, email: str = "", password: str = "", **params) -> dict[str, Any]:
    """Log in to the ABP portal and persist a session for reuse.

    Returns the `account` on success. Credential resolution order:
      1. Caller passed `email` + `password` explicitly.
      2. `credentials.retrieve(".approach.app", required=["email","password"])`
         matchmakes an installed `@provides("login_credentials")` app
         (1Password, Keychain, etc.).
      3. Provider needs unlock → `OnePasswordUnlockRequired` with
         `challengeId` (AgentOS Security; **For:** this app).
      4. Nothing matched → structured `NeedsCredentials` error; agent
         surfaces "add it to your password manager, or pass it directly."

    On success, the app runs the Cognito USER_PASSWORD_AUTH handshake
    and persists `{email, password, idToken, refreshToken}` via the
    `__secrets__` envelope under `(.approach.app, email)`.
    """
    if not email or not password:
        creds = await credentials.retrieve(
            domain=".approach.app",
            required=["email", "password"],
        )
        if creds and creds.get("unlock_required"):
            return app_error(
                creds.get("error")
                or "Unlock 1Password in AgentOS Security, then retry login.",
                code=creds.get("code") or "OnePasswordUnlockRequired",
                challengeId=creds.get("challengeId"),
                forApp=creds.get("forApp"),
                prompt=creds.get("prompt") or "secret_challenge",
                hint=creds.get("hint")
                or (
                    "Enter your Master Password in the AgentOS Security window, "
                    "then retry austin-boulder-project.login."
                ),
                domain=".approach.app",
            )
        if creds and creds.get("found"):
            val = creds.get("value") or {}
            email = email or val.get("email") or ""
            password = password or val.get("password") or ""

    if not email or not password:
        return app_error(
            "Missing credentials for .approach.app. Add an ABP login "
            "item to 1Password / Keychain, or call login() with "
            "email= and password= directly.",
            code="NeedsCredentials",
            domain=".approach.app",
            required=["email", "password"],
            help_url="https://boulderingproject.portal.approach.app/login",
        )

    result = await _cognito_initiate_auth(email, password)
    canonical = normalize_email(email)
    secret = _session_secret(
        email=canonical,
        password=password,
        id_token=result["IdToken"],
        refresh_token=result.get("RefreshToken"),
        access_token=result.get("AccessToken"),
        expires_in=result.get("ExpiresIn"),
        token_type=result.get("TokenType"),
    )
    return {
        "__secrets__": [secret],
        "__result__": {
            "authenticated": True,
            "at": _ABP_ORG,
            "identifier": canonical,
            "email": canonical,
        },
    }


@test.skip(reason="destructive — revokes the live Cognito session")
@account.logout
@returns({"ok": "boolean", "message": "string"})
@connection("portal")
async def logout(**params) -> dict[str, Any]:
    """Revoke the current Cognito session via `GlobalSignOut`.

    `GlobalSignOut` invalidates every IdToken / AccessToken for this
    user across all devices — correct for "log out" semantics. After
    it returns, any token we persisted or handed out becomes dead at
    Cognito; the access token's ~1h natural TTL is the only
    remaining validity window. The refresh token is dead immediately.

    The engine runs the cleanup tail (delete app-written credential
    rows, invalidate cache) after this returns, so we don't touch
    `__secrets__` here. Provider rows (1Password) stay put — logout
    forgets the session, not the password.

    Idempotent: a second call hits `NotAuthorizedException` which we
    treat as success — the session was already revoked.
    """
    auth = params.get("auth") or {}
    access_token = auth.get("accessToken")
    if not access_token and (auth.get("idToken") or auth.get("refreshToken")):
        try:
            await _ensure_fresh_id_token(params)
            access_token = (params.get("auth") or {}).get("accessToken")
        except RuntimeError:
            access_token = None
    if not access_token:
        # No live session to revoke — engine's cleanup tail still runs.
        # Report ok=false so `revoked_server_side` doesn't lie: we didn't
        # actually talk to Cognito.
        return {"ok": False, "message": "No live access token; skipped server revoke."}

    resp = await client.post(
        COGNITO_ENDPOINT,
        json={"AccessToken": access_token},
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.GlobalSignOut",
        },
    )

    # Cognito quirk: already-revoked / expired tokens return 400
    # NotAuthorizedException. That's "done," not "failed."
    if resp["status"] == 200:
        return {"ok": True, "message": "Cognito session revoked."}

    body = (resp.get("body") or "")[:200]
    j = resp.get("json") or {}
    err_type = j.get("__type") if isinstance(j, dict) else None
    if err_type == "NotAuthorizedException":
        return {"ok": True, "message": "Session already expired at Cognito."}

    return {
        "ok": False,
        "message": f"Cognito GlobalSignOut failed: HTTP {resp['status']} {err_type or body}",
    }
