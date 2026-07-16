"""uber.py — Uber rides (GraphQL) + Uber Eats (RPC) via the browser session.

Both halves run **inside a tab of the engine-owned browser** via the
``browser_session`` service (the Exa/Greptile pattern). The session is the
browser profile itself — Uber's auth cookies on ``.uber.com`` /
``.ubereats.com``, written by Uber's own Set-Cookie, never extracted, never
vaulted, never seen by this app. Requests originate from the real browser, so
the live session, TLS fingerprint, and any anti-bot cookies ride by
construction.

Two registrable domains, two tabs (one per ``one tab per domain`` rule):

  - riders.uber.com  — rides. Uber's internal GraphQL at /graphql.
  - www.ubereats.com — Eats. RPC-style POST at /_p/api/{operation}.

Native-interface note: a same-origin ``fetch()`` of ``/graphql`` (rides) or
``/_p/api/{op}`` (Eats) is byte-identical to the request Uber's own React app
sends — it's the lowest stable contract that already carries the session. We
do NOT reach into Uber's minified JS modules; the wire IS the tap.

No anti-bot gymnastics (custom UA, Sec-CH-UA, http2 toggles, hand-rolled
x-csrf) — the real browser tab supplies all of it. Uber's literal
``x-csrf-token: x`` header is still sent because it's an app-level contract
the endpoint checks, not a fingerprint.

API shapes (GraphQL queries, RPC endpoint paths, request bodies) are
unchanged from the cookie-transport version — see dev/requirements.md.
"""

from __future__ import annotations

import asyncio
import json as _json
import re as _re
import sys
from pathlib import Path as _Path

# Worker loads this file as module ``m`` — plugin dir on path for ``lib``.
_PLUGIN_DIR = str(_Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
# Worker process reuses sys.modules — drop stale lib so helper edits apply.
for _k in [k for k in list(sys.modules) if k == "lib" or k.startswith("lib.")]:
    del sys.modules[_k]

from agentos import (
    account,
    app_error,
    browser_session,
    claims,
    connection,
    credentials,
    normalize_email,
    provides,
    returns,
    services,
    test,
    timeout,
)

from lib.session import (
    ACTIVITIES_QUERY,
    CURRENT_USER_QUERY,
    GET_TRIP_QUERY,
    GRAPHQL_PATH,
    RIDES_EXTRA_HEADERS,
    _EATS,
    _RIDES,
    _UBER,
    _UBER_EATS,
    _check_tab,
    _eval,
    _gql,
    _parse_fare,
    _tab_request,
    _ueats,
)
from lib.auth import (
    _AUTH,
    _AUTH_LOGIN_URL,
    _CHANNEL_PATTERNS,
    _EATS_LOGIN_URL,
    _RIDES_LOGIN_URL,
    _auth_challenge_for_channel,
    _auth_method_order,
    _bs_click,
    _bs_snapshot,
    _bs_type,
    _clear_pending_otp,
    _detect_auth_screen,
    _drive_uber_login_until_challenge,
    _eats_account_if_live,
    _eats_needs_auth,
    _ensure_password_vaulted,
    _find_ref,
    _infer_otp_channel,
    _iso,
    _issue_otp_after_channel_click,
    _page_text,
    _pending_otp_for,
    _pick_otp_channel,
    _read_account_prefs,
    _resolve_uber_email,
    _rides_account_if_live,
    _rides_needs_auth,
    _snap_nodes,
    _snap_url,
    _uber_meta,
    _utc_now,
)
from lib.shape import (
    _active_order_to_list_row,
    _active_order_uuid,
    _detail_from_past_meta,
    _enrich_items_from_store_catalog,
    _fetch_active_orders,
    _money_str,
    _past_order_meta,
    _shape_ueats_order,
    _shop_items,
    _summary_from_checkout_info,
)

# Browser-session identity namespace — ops bind @connection("none").
connection("none", domain="uber.com")

# ---------------------------------------------------------------------------
# Tools + remaining Eats cart/store helpers (page SDK owns RPC; Python shapes)
# ---------------------------------------------------------------------------

@account.check
@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("account")
@claims("primary_user")
@connection("none")
@timeout(60)
async def check_session(**params) -> dict:
    """Verify the Uber rider session and identify the logged-in account.

    The session lives in the engine-owned browser profile; this op asks Uber's
    own ``CurrentUserRidersWeb`` GraphQL query from inside the riders.uber.com
    tab. No cookie ever reaches the app.

    Falls back to the Eats session when rides SSO is still cold — common after
    an Eats-only login. ``accounts.check({app:\"uber\"})`` / Shopping identity
    routing need a live identifier either way.
    """
    acct = await _rides_account_if_live()
    if acct:
        return acct
    eats = await _eats_account_if_live()
    if eats:
        return eats
    return {"authenticated": False}

@account.login
@returns("account | auth_challenge")
@connection("none")
@timeout(180)
async def login(*, email: str = "", method: str = "", **params) -> dict:
    """Sign in to Uber on the AgentOS background profile (Exa-style OTP flow).

    Resolution order:
      1. Already have a live riders session → return the account
      2. Resolve email from args / 1Password (``uber.com``)
      3. Drive auth.uber.com: identifier → password (via ``type_secret``) →
         OTP channel (account ``metadata.uber.lastAuthMethod`` first, else
         email → sms → whatsapp)
      4. Return ``auth_challenge`` (kind: code_sent) with ``retrieval`` +
         ``continueWith: verify_login_code``

    Card-digit challenges fall through to a headed ``login_window`` (lockout
    risk — never automated).
    """
    live = await _rides_account_if_live()
    if live:
        return live

    email = await _resolve_uber_email(email)
    if not email:
        return app_error(
            "No Uber email to sign in as.",
            code="NeedsCredentials",
            required=["email"],
            domain="uber.com",
            hint="Pass email=, or store a Login for uber.com in 1Password/vault.",
        )

    order = await _auth_method_order(email)
    if method:
        method = method.lower().strip()
        if method in _CHANNEL_PATTERNS:
            order = [method] + [c for c in order if c != method]

    result = await _drive_uber_login_until_challenge(
        email=email, method_order=order, prefer_password=True
    )
    if isinstance(result, dict) and result.get("__error"):
        return app_error(
            f"Driving Uber sign-in failed: {result.get('__error')}. "
            f"Page hint: {(result.get('text') or '')[:200]}",
            code="SigninFailed",
        )
    return result

@returns("account | auth_challenge")
@claims("primary_user")
@connection("none")
@timeout(120)
async def verify_login_code(
    *, email: str = "", code: str, method: str = "email", **params
) -> dict:
    """Enter the Uber OTP on the live auth tab and finish (or continue) login.

    Fail-closed: must already be on the OTP entry screen at auth.uber.com;
    ``method`` must match the pending challenge channel; stale challenges
    are rejected. Types ``code`` into the verification inputs ``login``
    advanced to. On success, returns the account with
    ``metadata.uber.lastAuthMethod`` set. If Uber demands a second factor,
    returns another ``auth_challenge`` (or a headed window for card digits).
    """
    if not code:
        return app_error("code is required.", code="BadParams")
    email = await _resolve_uber_email(email)
    method = (method or "email").lower().strip()

    pending = _pending_otp_for(email)
    if pending:
        if pending.get("channel") and pending["channel"] != method:
            return app_error(
                f"Active Uber challenge is {pending['channel']}, not {method}. "
                f"Use method={pending['channel']!r} with the code from that "
                f"channel only (requestedAt={pending.get('requestedAt')}). "
                "Do not mix email and SMS codes.",
                code="WrongChannel",
                pendingChannel=pending.get("channel"),
                requestedAt=pending.get("requestedAt"),
            )
        exp = pending.get("expiresAt")
        if exp:
            try:
                exp_dt = datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                if _utc_now() > exp_dt:
                    _clear_pending_otp(email)
                    return app_error(
                        f"Uber {method} challenge expired at {exp}. "
                        "Call login again for a fresh code.",
                        code="ChallengeExpired",
                        expiresAt=exp,
                    )
            except ValueError:
                pass

    snap = await _bs_snapshot(_AUTH)
    url = _snap_url(snap)
    if "auth.uber.com" not in url:
        return app_error(
            f"Auth tab is not on auth.uber.com (url={url!r}). Call login first.",
            code="VerifyFailed",
        )
    screen = _detect_auth_screen(snap)
    if screen != "otp_entry":
        return app_error(
            f"Not on OTP entry (screen=`{screen}`). Call login so a code is "
            f"requested — refusing to type into the wrong fields. "
            f"Page hint: {_page_text(snap)[:180]}",
            code="VerifyFailed",
            screen=screen,
        )
    seen = _infer_otp_channel(snap)
    if seen and seen != method:
        return app_error(
            f"OTP screen looks like {seen}, but method={method!r}. "
            f"Pass method={seen!r} with the code from that channel.",
            code="WrongChannel",
            onScreen=seen,
            method=method,
        )

    # OTP digit boxes only — skip the identifier-style email/phone field.
    boxes = [
        n for n in _snap_nodes(snap)
        if (n.get("role") or "").lower() in ("textbox", "spinbutton")
        and n.get("ref")
        and not _re.search(
            r"email|phone|mobile|password", (n.get("name") or ""), _re.I
        )
    ]
    if not boxes:
        boxes = [
            n for n in _snap_nodes(snap)
            if (n.get("role") or "").lower() in ("textbox", "spinbutton")
            and n.get("ref")
        ]
    if not boxes:
        return app_error(
            "No OTP input on the Uber auth tab — call login first so a code "
            "is requested.",
            code="VerifyFailed",
        )

    digits = _re.sub(r"\D", "", str(code))
    if len(boxes) >= 4 and len(digits) >= 4:
        for i, ch in enumerate(digits[: len(boxes)]):
            await _bs_type(_AUTH, boxes[i]["ref"], ch, clear=True)
            await asyncio.sleep(0.15)
    else:
        await _bs_type(_AUTH, boxes[0]["ref"], digits or str(code), clear=True)

    cont = _find_ref(snap, role="button", name_re=r"^continue$|verify|next|submit|confirm")
    if cont:
        await _bs_click(_AUTH, cont)
    await asyncio.sleep(1.5)

    live = await _rides_account_if_live(last_auth_method=method)
    if live:
        _clear_pending_otp(email)
        try:
            await browser_session.navigate(_EATS, _EATS_LOGIN_URL)
            await asyncio.sleep(0.8)
        except Exception:
            pass
        return live

    snap = await _bs_snapshot(_AUTH)
    screen = _detect_auth_screen(snap)
    page = _page_text(snap)

    if screen == "identifier":
        return app_error(
            "Code was rejected or the auth flow reset to the identifier "
            "screen. Request a fresh code with login (do not reuse this one).",
            code="VerifyFailed",
            screen=screen,
        )
    if screen == "otp_entry":
        err_hint = ""
        if _re.search(r"incorrect|invalid|try again|wrong|expired", page, _re.I):
            err_hint = " Uber reported the code as incorrect/expired."
        return app_error(
            f"Still on OTP entry after submit.{err_hint} "
            "Get a fresh code via login, or confirm the channel matches.",
            code="VerifyFailed",
            screen=screen,
        )
    if screen == "card_challenge":
        _clear_pending_otp(email)
        return await browser_session.login_window(
            _AUTH_LOGIN_URL,
            label="Uber card verification",
            instructions=(
                "OTP accepted, but Uber wants payment-card digits. Finish in "
                "the headed window (do not automate card digits — lockout risk), "
                "then poll check_session. Call login_window close=true when done."
            ),
        )
    if screen in ("otp_picker",):
        order = await _auth_method_order(email)
        prefs = await _read_account_prefs(email)
        second = (prefs.get("lastSecondFactor") or "").lower()
        if second and second in _CHANNEL_PATTERNS:
            order = [second] + [c for c in order if c != second]
        # Don't re-offer the channel we just completed as the second factor first.
        order = [c for c in order if c != method] + [method]
        picked = await _pick_otp_channel(snap, order)
        if picked:
            channel, ref = picked
            return await _issue_otp_after_channel_click(
                email=email, channel=channel, ref=ref, step="second_factor"
            )

    eats = await _eats_account_if_live(last_auth_method=method)
    if eats:
        _clear_pending_otp(email)
        return eats

    return app_error(
        f"Code entered but no live Uber session (screen=`{screen}`). "
        "Request a fresh code with login, or finish in a headed login_window.",
        code="VerifyFailed",
        screen=screen,
    )

@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(60)
async def logout(**params) -> dict:
    """Sign out of Uber rides in the browser profile.

    Drives Uber's own logout same-origin in the riders.uber.com tab, then
    confirms the session is gone. Idempotent — a dead session is still a clean
    logout.
    """
    await _eval(_RIDES, """
if (!__onApp) return { ok: true, already: 'logged_out' };
try { await fetch('/logout', { method: 'POST', credentials: 'include' }); } catch (e) {}
return { ok: true };
""")
    return {
        "status": "logged_out",
        "hint": "Cleared the rider session in the browser profile. Re-auth with uber.login.",
    }

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns({"id": "string", "name": "string", "email": "string", "phone": "string", "rating": "string", "hasUberOne": "boolean", "paymentMethods": "array", "profiles": "array", "country": "string"})
@connection("none")
async def whoami(**params) -> dict:
    """Get current user profile with full details."""

    data = await _gql("CurrentUserRidersWeb", CURRENT_USER_QUERY)

    user = data.get("currentUser", {})
    benefits = user.get("membershipBenefits") or {}
    payment_profiles = user.get("paymentProfiles") or []

    first = user.get("firstName", "")
    last = user.get("lastName", "")

    return {
        # Account-shaped entity — stable ID for graph upsert
        "id": user.get("uuid"),
        "name": f"{first} {last}".strip() or None,
        "givenName": first or None,
        "familyName": last or None,
        "email": user.get("email"),
        "phone": user.get("formattedNumber"),
        "rating": user.get("rating"),
        "image": user.get("pictureUrl"),
        "hasUberOne": benefits.get("hasUberOne", False),
        "country": user.get("signupCountry"),
        "paymentMethods": [
            {
                "id": p.get("uuid"),
                "name": (p.get("displayable") or {}).get("displayName"),
                "type": p.get("tokenType"),
            }
            for p in payment_profiles
        ],
        "profiles": [
            {"id": p.get("uuid"), "name": p.get("name"), "type": p.get("type")}
            for p in (user.get("profiles") or [])
        ],
    }

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("trip[]")
@connection("none")
async def list_trips(
    limit: int = 10,
    next_page_token: str | None = None,
    profile_type: str = "PERSONAL",
    order_types: str = "RIDES,TRAVEL",
    **params,
) -> list:
    """List past trips.

    Returns: trip[] — each with fare, currency, destination as name.
    """

    types = [t.strip() for t in order_types.split(",")]
    variables = {
        "includePast": True,
        "includeUpcoming": False,
        "limit": min(int(limit), 50),
        "orderTypes": types,
        "profileType": profile_type,
    }
    if next_page_token:
        variables["nextPageToken"] = next_page_token

    data = await _gql("Activities", ACTIVITIES_QUERY, variables)
    activities = data.get("activities", {})
    past = activities.get("past", {})
    raw_trips = past.get("activities") or []

    trips = []
    for t in raw_trips:
        fare_str = t.get("description") or ""
        total_amount, currency = _parse_fare(fare_str)

        trips.append({
            # Standard fields
            "id": t.get("uuid"),
            "name": t.get("title"),  # destination name
            "image": (t.get("imageURL") or {}).get("light"),
            "url": t.get("cardURL"),
            "published": t.get("subtitle"),
            # Trip shape fields
            "tripType": "ride",
            "status": "completed",
            "fare": fare_str or None,
            "fareAmount": total_amount,
            "currency": currency,
        })

    return trips

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("trip")
@connection("none")
async def get_trip(trip_id: str, **params) -> dict:
    """Get full trip details.

    Returns: trip with driver→person, origin→place, destination→place, legs→leg[].
    Multi-stop rides have multiple legs (one per waypoint pair).
    """

    data = await _gql(
        "GetTrip",
        GET_TRIP_QUERY,
        {"tripUUID": trip_id},
    )

    result = data.get("getTrip", {})
    trip = result.get("trip", {})
    receipt = result.get("receipt") or {}
    waypoints = trip.get("waypoints") or []

    fare_str = trip.get("fare") or ""
    fare_amount, currency = _parse_fare(fare_str)
    status_raw = (trip.get("status") or "").lower()

    driver_name = trip.get("driver") or ""
    driver_parts = driver_name.split(None, 1) if driver_name else []

    distance = f"{receipt.get('distance', '')} {receipt.get('distanceLabel', '')}".strip() or None

    # Vehicle info from trip and receipt
    vehicle = {}
    if trip.get("vehicleDisplayName"):
        vehicle["name"] = trip["vehicleDisplayName"]
    if receipt.get("vehicleType"):
        vehicle["type"] = receipt["vehicleType"]
    if receipt.get("carYear"):
        vehicle["year"] = receipt["carYear"]

    out = {
        # Standard fields
        "id": trip.get("uuid") or trip.get("jobUUID"),
        "at": _UBER,
        "name": waypoints[-1] if waypoints else trip_id,
        "image": result.get("mapURL"),
        "published": trip.get("beginTripTime"),
        # Trip shape fields
        "tripType": "ride",
        "status": status_raw,
        "departureTime": trip.get("beginTripTime"),
        "arrivalTime": trip.get("dropoffTime"),
        "duration": receipt.get("duration"),
        "distance": distance,
        "vehicleType": receipt.get("vehicleType"),
        "fare": fare_str or None,
        "fareAmount": fare_amount,
        "currency": currency,
        "rating": result.get("rating") or None,
        "isSurge": trip.get("isSurgeTrip", False),
        "isScheduled": trip.get("isScheduledRide", False),
        "isPool": trip.get("isRidepoolTrip", False),
        "isReserve": trip.get("isUberReserve", False),
        "stops": max(0, len(waypoints) - 2) if waypoints else 0,
    }
    if vehicle:
        out["vehicle"] = vehicle
    if trip.get("marketplace"):
        out["marketplace"] = trip["marketplace"]
    if trip.get("guest"):
        out["guest"] = trip["guest"]

    if driver_name:
        out["driven_by"] = {
            "shape": "person",
            "name": driver_name,
            "givenName": driver_parts[0] if driver_parts else None,
            "familyName": driver_parts[1] if len(driver_parts) > 1 else None,
        }

    if waypoints:
        out["starts_at"] = {"shape": "place", "id": waypoints[0], "name": waypoints[0], "fullAddress": waypoints[0], "featureType": "address"}
        out["ends_at"] = {"shape": "place", "id": waypoints[-1], "name": waypoints[-1], "fullAddress": waypoints[-1], "featureType": "address"}

    # Build legs from waypoint pairs (multi-stop support)
    if len(waypoints) >= 2:
        trip_id = trip.get("uuid") or trip.get("jobUUID") or trip_id
        out["routed_through"] = [
            {
                "shape": "leg",
                "id": f"{trip_id}_leg_{i + 1}",
                "name": f"Leg {i + 1}: {waypoints[i + 1]}",
                "sequence": i + 1,
                "starts_at": {"shape": "place", "id": waypoints[i], "name": waypoints[i], "fullAddress": waypoints[i], "featureType": "address"},
                "ends_at": {"shape": "place", "id": waypoints[i + 1], "name": waypoints[i + 1], "fullAddress": waypoints[i + 1], "featureType": "address"},
            }
            for i in range(len(waypoints) - 1)
        ]

    if (result.get("organization") or {}).get("name"):
        out["operated_by"] = {"shape": "organization", "name": result["organization"]["name"]}

    return out

async def _eats_post(endpoint: str, body: dict | None = None) -> dict:
    """POST to Uber Eats RPC API via ``window.__ueats.api`` in the eats tab.

    Endpoint is the operation name (e.g. 'getPastOrdersV1'); query strings
    (e.g. 'getFeedV1?localeCode=en-US') are preserved verbatim.

    Native-interface note: this same-origin POST to /_p/api/{op} is
    byte-identical to what the Uber Eats React app sends — the stable tap.
    """
    result = await _ueats("api", endpoint, body or {})
    if not isinstance(result, dict):
        raise RuntimeError(f"Eats {endpoint}: __ueats.api returned {result!r}")
    if result.get("__error"):
        # _check_tab already handled session_expired on the outer eval;
        # method-level errors come back as {ok:false,...}.
        raise RuntimeError(f"SESSION_EXPIRED: no live Uber session ({endpoint}).")
    if not result.get("ok"):
        err = result.get("error") or "unknown"
        if err == "session_expired":
            raise RuntimeError("SESSION_EXPIRED: Uber Eats redirected to login — cookies expired.")
        code = result.get("code") or ""
        raw = result.get("raw")
        raw_s = _json.dumps(raw, default=str)[:500] if raw is not None else ""
        raise RuntimeError(
            f"Uber Eats API error: {err} code={code} endpoint={endpoint} raw={raw_s}"
        )
    data = result.get("data")
    return data if isinstance(data, dict) else (data or {})

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("account")
@claims("primary_user")
@connection("none")
@timeout(60)
async def check_eats_session(**params) -> dict:
    """Verify the Uber Eats session and identify the logged-in account.

    Eats is a separate registrable domain from rides, but shares Uber SSO.
    Auth signal is draft-list success / session cookies — NOT ``getUserV1``
    (that endpoint often 403s on a live session).
    """
    acct = await _eats_account_if_live()
    if not acct:
        return {"authenticated": False}
    return acct

@returns("account | auth_challenge")
@connection("none")
@timeout(180)
async def login_eats(*, email: str = "", method: str = "", **params) -> dict:
    """Sign in to Uber Eats — same SSO as ``login``, then warm the Eats tab.

    Prefers ``metadata.uber.lastAuthMethod`` on the account; defaults to email
    OTP. Returns ``auth_challenge`` → ``verify_login_code`` when a code is
    needed.
    """
    live = await _eats_account_if_live()
    if live:
        return live

    # Drive the shared Uber SSO (rides entrypoint), then bind Eats.
    result = await login(email=email, method=method, **params)
    if isinstance(result, dict) and result.get("authenticated"):
        try:
            await browser_session.navigate(_EATS, _EATS_LOGIN_URL)
            await asyncio.sleep(1.0)
        except Exception:
            pass
        eats = await _eats_account_if_live(
            last_auth_method=((result.get("metadata") or {}).get("uber") or {}).get("lastAuthMethod")
        )
        return eats or result
    return result

@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(60)
async def logout_eats(**params) -> dict:
    """Sign out of Uber Eats in the browser profile.

    Drives Uber Eats' own logout same-origin via ``__ueats.logout``.
    Idempotent — a dead session is still a clean logout.
    """
    try:
        await _ueats("logout")
    except RuntimeError:
        pass
    return {
        "status": "logged_out",
        "hint": "Cleared the Eats session in the browser profile. Re-auth with uber.login_eats.",
    }

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns({
    "ok": "boolean",
    "checks": "object",
    "repairs": "array",
    "tip": "string",
})
@connection("none")
@timeout(90)
async def eats_health(repair: bool = False, **params) -> dict:
    """Page-local Eats health report (``window.__ueats.health``).

    Checks host, session, pastOrders, activeOrders, and receipt JSON path.
    Pass ``repair=True`` for cheap in-page fixes (e.g. navigate to /orders).
    Does not place orders or open a login window — escalate with ``login_eats``.
    """
    raw = await _ueats("health", {"repair": bool(repair)}, timeout_s=90)
    if not isinstance(raw, dict):
        raise RuntimeError(f"eats_health: unexpected {raw!r}")
    return raw

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns({"id": "string", "name": "string", "email": "string", "phone": "string", "subscription": "object", "savedAddresses": "array", "paymentMethods": "array"})
@connection("none")
async def get_eats_profile(**params) -> dict:
    """Get Uber Eats user profile — name, photo, subscription, business profiles.

    Backed by getUserV1 + getProfilesForUserV1. Returns account-shaped entity.
    Note: getUserV1 returns hashedEmail (not plain email) and no phone.
    Use whoami (Rides API) for email, phone, rating, and payment method details.
    """

    data = await _eats_post("getUserV1", {"shouldGetSubsMetadata": True})

    # getUserV1 shape: {firstName, lastName, pictureUrl, hashedEmail,
    #   subscriptionMeta, creationTime, geoIpCountryCode, hasConfirmedMobile}
    first = data.get("firstName", "")
    last = data.get("lastName", "")
    name = f"{first} {last}".strip()

    # Subscription metadata
    subs = data.get("subscriptionMeta") or {}

    result = {
        "name": name or None,
        "givenName": first or None,
        "familyName": last or None,
        "image": data.get("pictureUrl"),
        "country": data.get("geoIpCountryCode"),
        "createdAt": data.get("creationTime"),
        "hasConfirmedMobile": data.get("hasConfirmedMobile", False),
    }

    if subs and isinstance(subs, dict):
        result["subscription"] = {
            "name": subs.get("title") or subs.get("passType"),
            "status": subs.get("eatsSubscriptionStatus"),
            "type": subs.get("passType"),
        }

    # getProfilesForUserV1 — business vs personal profiles
    try:
        profiles_data = await _eats_post("getProfilesForUserV1", {})
        profiles_list = profiles_data.get("profiles") or []
        selected = profiles_data.get("selectedProfile") or {}
        if profiles_list:
            result["profiles"] = [
                {
                    "id": p.get("uuid"),
                    "name": p.get("name") or p.get("displayName"),
                    "type": p.get("type"),  # Personal, Business
                    "isSelected": p.get("uuid") == selected.get("uuid") if selected else False,
                }
                for p in profiles_list
                if isinstance(p, dict)
            ]
    except Exception:
        pass  # best-effort

    return result




@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order[]")
@provides("order_history", account_param="account")
@connection("none")
@timeout(90)
async def list_deliveries(cursor: str = "", account=None, limit: int = 50, **params) -> list:
    """List Uber Eats order history as order-shaped entities.

    Brokered as ``order_history``: the Shopping Commons app fans this out per
    connected account alongside Amazon (and any future provider) — never
    naming Uber. ``account`` rides the run for multi-account pinning; today's
    single browser-profile session answers regardless.

    Returns: order[] — each with store relation (organization) and shipping_address (place).
    Active in-flight orders (getActiveOrdersV1) are prepended so Shopping can
    track a live PICKUP/DELIVERY before it lands in getPastOrdersV1.
    Backed by getPastOrdersV1 (+ active). See dev/requirements.md for response shape.
    """
    _ = account  # broker account pin — session is the browser profile
    max_n = max(1, min(int(limit or 50), 100))

    # One page-SDK round-trip: past page + active, completed-active deduped in JS.
    try:
        bundled = await _ueats("listOrders", {"cursor": cursor or "", "limit": max_n})
    except RuntimeError as e:
        msg = str(e)
        if "SESSION_EXPIRED" in msg or "session_expired" in msg:
            return _eats_needs_auth()
        raise

    if not isinstance(bundled, dict):
        raise RuntimeError(f"listOrders returned {bundled!r}")
    if bundled.get("error") == "session_expired" or (
        not bundled.get("ok") and not bundled.get("ordersMap") and not bundled.get("active")
    ):
        err = bundled.get("error") or ""
        if err == "session_expired" or "session" in err:
            return _eats_needs_auth()
        if not bundled.get("ok"):
            raise RuntimeError(f"listOrders failed: {err or 'unknown'}")

    order_uuids = bundled.get("orderUuids") or []
    orders_map = bundled.get("ordersMap") or {}
    active_raw = bundled.get("active") or []

    active_rows = []
    for raw in active_raw:
        row = _active_order_to_list_row(raw)
        if row:
            active_rows.append(row)

    if not orders_map and active_rows:
        return active_rows[:max_n]
    if not orders_map and not active_rows:
        raise RuntimeError("getPastOrdersV1 failed and no active orders")

    active_ids = {r["id"] for r in active_rows}
    orders = list(active_rows)
    for uuid in order_uuids:
        if uuid in active_ids:
            continue
        if len(orders) >= max_n:
            break
        order = orders_map.get(uuid, {})
        base = order.get("baseEaterOrder") or {}
        store_info = order.get("storeInfo") or {}
        fare = order.get("fareInfo") or {}
        location = store_info.get("location") or {}
        raw_addr = location.get("address") or {}
        if isinstance(raw_addr, str):
            raw_addr = {"eaterFormattedAddress": raw_addr}

        total_cents = fare.get("totalPrice", 0)
        currency = fare.get("currencyCode")

        if base.get("isCancelled"):
            status = "cancelled"
        elif base.get("isCompleted"):
            status = "completed"
        else:
            status = "in_progress"

        dining_mode = (
            order.get("diningMode")
            or base.get("diningMode")
            or order.get("fulfillmentType")
            or ""
        )
        if isinstance(dining_mode, str):
            dining_mode = dining_mode.upper()
        else:
            dining_mode = ""

        order_states = base.get("orderStateChanges") or []
        delivery_states = base.get("deliveryStateChanges") or []
        timeline = []
        for sc in order_states:
            timeline.append({"type": sc.get("type"), "at": sc.get("stateChangeTime"), "category": "order"})
        for sc in delivery_states:
            timeline.append({"type": sc.get("type"), "at": sc.get("stateChangeTime"), "category": "delivery"})
        timeline.sort(key=lambda x: x.get("at") or "")

        courier = order.get("courierInfo") or {}
        courier_name = courier.get("name") or None

        delivery_addr = base.get("deliveryAddress") or {}
        delivery_loc = delivery_addr.get("location") or {}
        delivery_address_obj = delivery_addr.get("address") or {}
        if isinstance(delivery_address_obj, str):
            delivery_address_obj = {"eaterFormattedAddress": delivery_address_obj}

        when = base.get("completedAt") or base.get("lastStateChangeAt") or base.get("createdAt")
        store_title = store_info.get("title", "Unknown store")
        total_amount = total_cents / 100 if total_cents else None
        total_str = _money_str(total_amount)
        checkout = fare.get("checkoutInfo") or []
        summary = _summary_from_checkout_info(checkout, total=total_str)
        ship_addr = (
            delivery_address_obj.get("eaterFormattedAddress")
            or (raw_addr.get("eaterFormattedAddress") if dining_mode == "PICKUP" else None)
        )

        order_data = {
            "id": uuid,
            "orderId": uuid,
            "name": store_title,
            "image": store_info.get("heroImageUrl"),
            "published": when,
            "orderDate": when,
            "deliveryDate": when if status == "completed" else None,
            "itemCount": None,
            "total": total_str,
            "totalAmount": total_amount,
            "currency": currency or "USD",
            "status": status,
            "diningMode": dining_mode or None,
            "isPickup": dining_mode == "PICKUP",
            "interactionType": order.get("interactionType"),
            "fareBreakdown": [
                {"label": item.get("label"), "amount": item.get("rawValue"), "key": item.get("key")}
                for item in checkout
            ],
            "summary": summary or None,
            "at": _UBER_EATS,
            "purchased_at": {
                "shape": "place",
                "id": store_info.get("uuid"),
                "name": store_info.get("title"),
                "image": store_info.get("heroImageUrl"),
                "featureType": "poi",
                "fullAddress": raw_addr.get("eaterFormattedAddress"),
                "latitude": location.get("latitude"),
                "longitude": location.get("longitude"),
            },
            "shipped_to": {
                "shape": "place",
                "fullAddress": ship_addr,
                "latitude": delivery_loc.get("latitude") or location.get("latitude"),
                "longitude": delivery_loc.get("longitude") or location.get("longitude"),
            } if ship_addr else None,
            "url": f"https://www.ubereats.com/orders/{uuid}",
        }

        if timeline:
            order_data["timeline"] = timeline
        if courier_name:
            order_data["courier"] = {"name": courier_name}

        orders.append(order_data)

    return orders[:max_n]

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order")
@connection("none")
@timeout(90)
async def get_order(*, order_id: str = "", order_uuid: str = "", **params) -> dict:
    """Shopping / ``order_history`` detail verb — same as ``get_delivery``.

    The broker routes ``services.order_history {verb: get_order}`` to this
    tool by name (Amazon parity). Accepts ``order_id`` (Shopping) or
    ``order_uuid`` (legacy Uber callers).
    """
    oid = (order_id or order_uuid or "").strip()
    if not oid:
        raise RuntimeError("order_id is required")
    return await get_delivery(order_uuid=oid, **params)

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order")
@connection("none")
@timeout(90)
async def get_delivery(order_uuid: str = "", order_id: str = "", **params) -> dict:
    """Get full delivery details including items, quantities, and fare breakdown.

    Runs through ``window.__ueats.getOrder`` (page/eats.js) in the Eats tab —
    past metadata, JSON receipt, and catalog enrich happen in-page. Python maps
    to AgentOS/Shopping order shapes.
    """
    order_uuid = (order_uuid or order_id or "").strip()
    if not order_uuid:
        raise RuntimeError("order_uuid is required")

    raw = await _ueats("getOrder", order_uuid, timeout_s=90)
    if not isinstance(raw, dict) or not raw.get("ok"):
        err = (raw or {}).get("error") if isinstance(raw, dict) else "unknown"
        # Soft-fallback: track + past (stale active / missing receipt)
        past_meta = await _past_order_meta(order_uuid)
        tracked: dict = {}
        try:
            tracked = await track_delivery(order_uuid=order_uuid, **params)
        except Exception:
            tracked = {}
        track_ok = bool(
            tracked.get("orderId")
            or tracked.get("status") not in ("not_found", None, "")
        )
        if track_ok:
            if tracked.get("contains") and not tracked.get("items"):
                tracked["items"] = tracked["contains"]
            tracked["items"] = _shop_items(tracked.get("items") or [])
            tracked["contains"] = tracked["items"]
            tracked.setdefault("itemCount", len(tracked.get("items") or []))
            tracked.setdefault("url", f"https://www.ubereats.com/orders/{order_uuid}")
        base = (past_meta.get("baseEaterOrder") or {}) if past_meta else {}
        if past_meta and (base.get("isCompleted") or base.get("isCancelled")):
            detail = _detail_from_past_meta(
                order_uuid,
                past_meta,
                items=(tracked.get("items") if track_ok else None),
            )
            store_uuid = (
                (detail.get("purchased_at") or {}).get("id")
                if isinstance(detail.get("purchased_at"), dict)
                else None
            ) or (past_meta.get("storeInfo") or {}).get("uuid")
            detail["items"] = await _enrich_items_from_store_catalog(
                store_uuid, detail.get("items") or []
            )
            detail["contains"] = detail["items"]
            detail["itemCount"] = len(detail["items"]) or detail.get("itemCount")
            return detail
        if track_ok:
            return tracked
        if past_meta:
            return _detail_from_past_meta(order_uuid, past_meta)
        raise RuntimeError(f"getOrder failed: {err}")

    return await _shape_ueats_order(order_uuid, raw)

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("place")
@connection("none")
async def get_store(store_uuid: str, **params) -> dict:
    """Get store details and full product catalog.

    Backed by getStoreV1. Returns store metadata (open/orderable, ETA, rating)
    and every available product with title, uuid, price, image.
    See dev/requirements.md for full response shape documentation.
    """

    data = await _eats_post("getStoreV1", {"storeUuid": store_uuid})

    if not data.get("title"):
        raise RuntimeError(f"getStoreV1 returned no store data for {store_uuid} — session may be stale")

    # Extract products from catalogSectionsMap
    # Items are nested: sections → HORIZONTAL_GRID items → payload → standardItemsPayload → catalogItems
    # See dev/requirements.md "getStoreV1" section for the full structure.
    # getStoreV1 may include currencyCode at top level (91 keys — not all documented yet)
    store_currency = data.get("currencyCode") or data.get("currency")
    sections_map = data.get("catalogSectionsMap") or {}
    products = []
    seen_uuids = set()

    # Catalog sections come in multiple layout types depending on store vertical.
    # Grocery stores use HORIZONTAL_GRID; restaurants use VERTICAL_GRID.
    # Both carry the same catalog shape under payload.standardItemsPayload.
    _ITEM_GRID_TYPES = {"HORIZONTAL_GRID", "VERTICAL_GRID"}
    for sec_items in sections_map.values():
        if not isinstance(sec_items, list):
            continue
        for item in sec_items:
            if item.get("type") not in _ITEM_GRID_TYPES:
                continue
            payload = item.get("payload") or {}
            std = payload.get("standardItemsPayload") or {}
            section_title = (std.get("title") or {}).get("title", "")
            for ci in (std.get("catalogItems") or []):
                uid = ci.get("uuid", "")
                if uid in seen_uuids:
                    continue  # items appear in multiple sections — deduplicate
                seen_uuids.add(uid)
                price_cents = ci.get("price", 0)
                # RE principle: preserve raw catalog item for write operations.
                # add_to_cart needs the EXACT fields the API returned — sectionUUID,
                # sellingOption, imageUrl, etc. Don't reconstruct, replay.
                # See docs/reverse-engineering/overview.md "Write operations"
                # Product shape: standard fields + product-specific fields
                # Extract weight/size from thumbnail labels (e.g. "12 oz", "32 oz")
                weight = None
                for te in (ci.get("itemThumbnailElements") or []):
                    at = (te.get("payload", {}).get("labelPayload", {})
                          .get("label", {}).get("accessibilityText", ""))
                    if (at and not at.startswith("$") and at != ci.get("title", "")
                            and at.lower() not in ("sponsored", "ad")):
                        if _re.search(r'\d+\s*(?:oz|lb|lbs|g|kg|ml|fl|ct|count|pk|pack|each|ea)', at, _re.I):
                            weight = at
                            break

                # Check if priced by weight
                purchase = ci.get("purchaseInfo", {})
                pricing = purchase.get("pricingInfo", {}).get("pricedByUnit", {})
                sold_by_weight = pricing.get("measurementType", "") != "MEASUREMENT_TYPE_COUNT"

                # Promo / endorsement badges from imageOverlayElements
                badges = []
                for overlay in (ci.get("imageOverlayElements") or []):
                    for tag in ((overlay.get("element", {}).get("payload", {})
                                 .get("tagsPayload", {}).get("tags")) or []):
                        if tag.get("text"):
                            badges.append(tag["text"])

                # Original price from priceTagline (e.g. "$18.40, discounted from $23.00")
                original_price = None
                tagline = ci.get("priceTagline") or {}
                a11y = tagline.get("accessibilityText", "")
                if "discounted from" in a11y or "previous price" in a11y:
                    m = _re.search(r'\$(\d+(?:\.\d+)?)\s*$', a11y)
                    if m:
                        original_price = float(m.group(1))

                # Dietary tags (grocery items have these — e.g. VEGAN, SNAP, Non-GMO)
                # Filter out promo-looking tags (% off, $ amounts, "#N most liked")
                dietary_tags = []
                for te in (ci.get("itemThumbnailElements") or []):
                    for tag in ((te.get("payload", {}).get("tagsPayload", {})
                                 .get("tags")) or []):
                        t = tag.get("text", "")
                        if t and t not in badges:
                            if _re.search(r'%\s*off|^\$|\bmost\s+liked\b|^\d+\s*for\s*\$', t, _re.I):
                                badges.append(t)  # promo, not dietary
                            else:
                                dietary_tags.append(t)

                product = {
                    # Standard fields
                    "id": uid,
                    "name": ci.get("title", ""),
                    "image": ci.get("imageUrl"),
                    "content": ci.get("itemDescription"),
                    # Product shape fields
                    "priceAmount": price_cents / 100 if price_cents else None,
                    "currency": store_currency,
                    "availability": "in_stock" if ci.get("isAvailable", True) else "out_of_stock",
                    "categories": [section_title] if section_title else [],
                    "aisle": section_title or None,
                    "sku": uid,
                    # Raw catalog item — passed through to add_to_cart verbatim.
                    # RE principle: preserve raw data for write operations.
                    "_raw": ci,
                    "_parent_section_uuid": item.get("catalogSectionUUID", ""),
                }
                if ci.get("hasCustomizations"):
                    product["hasCustomizations"] = True
                if badges:
                    product["badges"] = badges
                if original_price:
                    product["originalPrice"] = original_price
                if dietary_tags:
                    product["dietaryTags"] = dietary_tags
                if ci.get("numAlcoholicItems", 0) > 0:
                    product["isAlcoholic"] = True
                if sold_by_weight:
                    product["sold_by_weight"] = True
                if weight:
                    product["weight"] = weight
                    wm = _re.match(r'(\d+(?:\.\d+)?)\s*(.+)', weight)
                    if wm:
                        product["weight_value"] = float(wm.group(1))
                        product["weight_unit"] = wm.group(2).strip().lower()
                products.append(product)

    location = data.get("location") or {}
    raw_addr = location.get("address") or {}
    address = raw_addr if isinstance(raw_addr, dict) else {"eaterFormattedAddress": str(raw_addr)}

    # Convert hours from minutes-since-midnight to readable format
    def _fmt_minutes(m):
        h, mn = divmod(int(m), 60)
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{mn:02d} {suffix}"

    formatted_hours = []
    for h in (data.get("hours") or []):
        day = h.get("dayRange", "")
        for sh in (h.get("sectionHours") or []):
            start = sh.get("startTime")
            end = sh.get("endTime")
            if start is not None and end is not None:
                formatted_hours.append(f"{day}: {_fmt_minutes(start)}–{_fmt_minutes(end)}")

    # isOpen can be True even when the store isn't accepting orders right now.
    # closedMessage tells you when it actually opens (e.g. "Opens Saturday 9:30 AM").
    # Check BOTH is_open AND closed_message to determine real availability.
    rating_data = data.get("rating") or {}

    # A store is a place (POI), not an organization. The organization (brand)
    # is the company (e.g. "Sprouts Farmers Market Inc."). The store is a
    # location of that brand — with address, hours, rating, delivery availability.
    # See place.yaml — modeled after Google Places API.
    return {
        # Standard fields
        "id": data.get("uuid", ""),
        "name": data.get("title", ""),
        "image": (data.get("heroImageUrls") or [None])[0],
        "url": f"https://www.ubereats.com/store/{data.get('slug', '')}",
        # Place shape fields
        "fullAddress": address.get("eaterFormattedAddress"),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "featureType": "poi",
        "categories": [c if isinstance(c, str) else c.get("name", "") for c in (data.get("categories") or []) if c],
        "phone": data.get("phoneNumber"),
        "hours": formatted_hours or None,
        "businessStatus": "open" if data.get("isOpen") and not data.get("closedMessage") else "closed",
        "rating": rating_data.get("ratingValue"),
        "reviewCount": rating_data.get("reviewCount"),
        # delivery/eta/is_orderable are CONTEXTUAL — they depend on the user's
        # delivery address, not intrinsic to the place. Returned here for UX
        # but not part of the place shape.
        "isOrderable": data.get("isOrderable", False),
        "closedMessage": data.get("closedMessage"),
        "eta": (data.get("etaRange") or {}).get("text"),
        "brand": {
            "name": data.get("title", ""),
        },
        # Products — place.offers→product[] relation creates linked product nodes
        "offers": products,
        "productCount": len(products),
    }

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("product")
@connection("none")
@timeout(15)
async def get_item_customizations(store_uuid: str, item_uuid: str, section_uuid: str = "", subsection_uuid: str = "", **params) -> dict:
    """Get customization options for a menu item (toppings, sizes, sides, etc.).

    Uses getMenuItemV1. The section/subsection UUIDs come from the item's _raw
    field in get_store output. If omitted, attempts to look them up via get_store.

    Returns a dict with the item name and customization groups, each containing
    options with UUID, title, price, and nested child customizations.
    """

    # getMenuItemV1 returns `invalid_uuid` (404) when section/subsection are empty.
    # Look them up from the store catalog when the caller didn't supply them.
    if not section_uuid or not subsection_uuid:
        store_data = await _eats_post("getStoreV1", {"storeUuid": store_uuid})
        for sec_items in (store_data.get("catalogSectionsMap") or {}).values():
            if not isinstance(sec_items, list):
                continue
            for item in sec_items:
                if item.get("type") != "HORIZONTAL_GRID":
                    continue
                payload = item.get("payload") or {}
                std = payload.get("standardItemsPayload") or {}
                for ci in (std.get("catalogItems") or []):
                    if ci.get("uuid") == item_uuid:
                        section_uuid = section_uuid or item.get("catalogSectionUUID", "")
                        subsection_uuid = subsection_uuid or ci.get("subsectionUuid", "")
                        break
                if section_uuid and subsection_uuid:
                    break
            if section_uuid and subsection_uuid:
                break
        if not section_uuid or not subsection_uuid:
            raise RuntimeError(f"get_item_customizations: could not resolve section/subsection for item {item_uuid} in store {store_uuid}")

    # Build the request — sectionUuid and subsectionUuid are required
    body = {
        "itemRequestType": "ITEM",
        "storeUuid": store_uuid,
        "sectionUuid": section_uuid,
        "subsectionUuid": subsection_uuid,
        "menuItemUuid": item_uuid,
        "cbType": "EATER_ENDORSED",
        "includeCheaperAlternatives": False,
        "contextReferences": [
            {"type": "GROUP_ITEMS", "payload": {"type": "groupItemsContextReferencePayload",
             "groupItemsContextReferencePayload": {}}, "pageContext": "UNKNOWN"}
        ],
    }

    data = await _eats_post("getMenuItemV1", body)

    # Parse customization groups
    groups = []
    for group in (data.get("customizationsList") or []):
        options = []
        for opt in (group.get("options") or []):
            option_data = {
                "uuid": opt.get("uuid"),
                "title": opt.get("title"),
                "price": opt.get("price", 0),  # cents
                "priceAmount": (opt.get("price") or 0) / 100,
                "maxQuantity": opt.get("maxPermitted", 1),
                "defaultQuantity": opt.get("defaultQuantity", 0),
                "isSoldOut": opt.get("isSoldOut", False),
            }
            # Nested customizations (e.g. half toppings → 1ST HALF / 2ND HALF)
            children = opt.get("childCustomizationList") or []
            if children:
                option_data["childGroups"] = []
                for child_group in children:
                    child_options = []
                    for child_opt in (child_group.get("options") or []):
                        child_options.append({
                            "uuid": child_opt.get("uuid"),
                            "title": child_opt.get("title"),
                            "price": child_opt.get("price", 0),
                            "priceAmount": (child_opt.get("price") or 0) / 100,
                            "maxQuantity": child_opt.get("maxPermitted", 1),
                            "isSoldOut": child_opt.get("isSoldOut", False),
                        })
                    option_data["childGroups"].append({
                        "uuid": child_group.get("uuid"),
                        "title": child_group.get("title"),
                        "groupId": child_group.get("groupId"),
                        "options": child_options,
                    })
            options.append(option_data)

        groups.append({
            "uuid": group.get("uuid"),
            "title": group.get("title"),
            "groupId": group.get("groupId"),
            "minRequired": group.get("minPermitted", 0),
            "maxAllowed": group.get("maxPermitted", 1),
            "options": options,
        })

    item_title = data.get("title") or data.get("itemTitle") or ""
    item_desc = data.get("itemDescription") or ""
    item_price = data.get("price") or 0

    return {
        "id": item_uuid,
        "name": item_title,
        "content": item_desc,
        "priceAmount": item_price / 100 if item_price else None,
        "currency": "USD",
        "customizationGroups": groups,
    }

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("product[]")
@connection("none")
@timeout(15)
async def search_products(store_uuid: str, query: str, **params) -> list:
    """Search products within a store. Server-side search via getInStoreSearchV1.

    Returns richer data than get_store catalog: dietary tags (VEGAN, Non-GMO, SNAP),
    original/discounted prices, promotion info. Also returns aisle/department filters.

    Returns: product[] — each with tags, prices, weight, availability.
    """

    data = await _eats_post("getInStoreSearchV1", {
        "diningMode": "DELIVERY",
        "storeUUIDs": [store_uuid],
        "userQuery": query,
        "isGrocery": True,
        "sectionUUIDs": None,
    })

    products = []
    seen = set()
    for sec_val in (data.get("catalogSectionsMap") or {}).values():
        if not isinstance(sec_val, list):
            continue
        for item in sec_val:
            payload = item.get("payload") or {}
            sp = payload.get("standardItemsPayload") or payload.get("verticalGridItemsPayload") or {}
            section_title = (sp.get("title") or {}).get("title", "")
            for ci in (sp.get("catalogItems") or []):
                uid = ci.get("uuid", "")
                if uid in seen:
                    continue
                seen.add(uid)

                price_cents = ci.get("price", 0)

                # Extract dietary tags from titleBadge accessibility text
                # e.g. "California Olive Ranch Extra Virgin Olive Oil, VEGAN"
                badge = ci.get("titleBadge") or {}
                badge_text = badge.get("accessibilityText", "")
                tags = []
                if badge_text and ", " in badge_text:
                    # Tags are after the last comma in accessibilityText
                    tag_part = badge_text.rsplit(", ", 1)[-1] if ", " in badge_text else ""
                    if tag_part and tag_part.upper() == tag_part:
                        tags = [t.strip() for t in tag_part.split(",")]

                # Also check for badge images (vegan.png, non-gmo.png, etc.)
                badge_html = badge.get("textFormat", "")
                if "vegan.png" in badge_html and "VEGAN" not in tags:
                    tags.append("VEGAN")
                if "non-gmo" in badge_html.lower() and "NON-GMO" not in tags:
                    tags.append("NON-GMO")
                if "organic" in badge_html.lower() and "ORGANIC" not in tags:
                    tags.append("ORGANIC")
                if "snap" in badge_html.lower() and "SNAP" not in tags:
                    tags.append("SNAP")

                # Extract original/discounted price from priceTagline
                tagline = ci.get("priceTagline") or {}
                tagline_text = tagline.get("accessibilityText", "")
                original_price = None
                if "discounted from" in tagline_text:
                    original_price = tagline_text.split("discounted from ")[-1].strip()

                # Extract weight/size from thumbnail labels (e.g. "12 oz", "3 lbs")
                weight = None
                for te in (ci.get("itemThumbnailElements") or []):
                    at = (te.get("payload", {}).get("labelPayload", {})
                          .get("label", {}).get("accessibilityText", ""))
                    if (at and not at.startswith("$") and at != ci.get("title", "")
                            and at.lower() not in ("sponsored", "ad")):
                        # Only accept if it looks like a weight/unit (has a number + unit)
                        if _re.search(r'\d+\s*(?:oz|lb|lbs|g|kg|ml|fl|ct|count|pk|pack|each|ea)', at, _re.I):
                            weight = at
                            break

                product = {
                    "id": uid,
                    "name": ci.get("title", ""),
                    "image": ci.get("imageUrl"),
                    "priceAmount": price_cents / 100 if price_cents else None,
                    "originalPrice": original_price,
                    "currency": "USD",  # TODO: from store data
                    "availability": "in_stock" if ci.get("isAvailable", True) else "out_of_stock",
                    "categories": [section_title] if section_title else [],
                }
                if tags:
                    product["tagged"] = [
                        {"name": t, "tagType": "dietary"}
                        for t in tags
                    ]
                if weight:
                    product["weight"] = weight
                    wm = _re.match(r'(\d+(?:\.\d+)?)\s*(.+)', weight)
                    if wm:
                        product["weight_value"] = float(wm.group(1))
                        product["weight_unit"] = wm.group(2).strip().lower()

                # Aisle from section title
                if section_title:
                    product["aisle"] = section_title

                # Sold by weight
                purchase = ci.get("purchaseInfo", {})
                pricing = purchase.get("pricingInfo", {}).get("pricedByUnit", {})
                if pricing.get("measurementType", "") != "MEASUREMENT_TYPE_COUNT":
                    product["sold_by_weight"] = True

                product["sku"] = uid

                if ci.get("hasCustomizations"):
                    product["has_customizations"] = True
                # Preserve raw for add_to_cart
                product["_raw"] = ci

                products.append(product)

    return products

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("place[]")
@provides("geocoding")
@connection("none")
async def search_address(query: str, resolve: bool = True, **params) -> list:
    """Search for addresses worldwide — autocomplete + geocoding.

    Backed by mapsSearchV1 (typeahead) + getDeliveryLocationV2 (coordinate resolution).
    Returns place-shaped entities with structured address components and lat/lng.
    Uses HERE Maps and Uber Places as providers.

    Set resolve=False to skip coordinate lookup (faster, but no lat/lng).
    """

    results = await _eats_post("mapsSearchV1", {"query": query})
    if not isinstance(results, list):
        results = []

    places = []
    for r in results:
        place_id = r.get("id", "")
        provider = r.get("provider", "")

        place = {
            "id": place_id,
            "name": r.get("addressLine1", ""),
            "fullAddress": f"{r.get('addressLine1', '')}, {r.get('addressLine2', '')}".strip(", "),
            "featureType": "address",
            "categories": r.get("categories") or [],
        }

        # Resolve coordinates + structured address if requested
        if resolve and place_id and provider:
            try:
                detail = await _eats_post("getDeliveryLocationV2", {
                    "placeId": place_id,
                    "provider": provider,
                    "source": "manual_auto_complete",
                })
                loc = (detail.get("deliveryLocation") or {}).get("location") or {}
                coord = loc.get("coordinate") or {}
                comps = loc.get("addressComponents") or {}

                if coord.get("latitude"):
                    place["latitude"] = coord["latitude"]
                    place["longitude"] = coord.get("longitude")
                if loc.get("fullAddress"):
                    place["fullAddress"] = loc["fullAddress"]
                if comps:
                    place["city"] = comps.get("CITY")
                    place["state"] = comps.get("FIRST_LEVEL_SUBDIVISION_CODE")
                    place["country"] = comps.get("COUNTRY_CODE")
                    place["postalCode"] = comps.get("POSTAL_CODE")
                    place["neighborhood"] = comps.get("NEIGHBORHOOD")
                    place["streetName"] = comps.get("STREET_NAME")
                    place["houseNumber"] = comps.get("HOUSE_NUMBER")
            except Exception:
                pass  # resolve failed — return without coordinates

        places.append(place)

    return places

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("place[]")
@connection("none")
@timeout(15)
async def list_addresses(**params) -> list:
    """List saved, suggested, and currently-active delivery addresses.

    Backed by getDeliveryLocationsV2, which returns three buckets:
    - SAVED: explicit Home/Work/custom pins the user created. Usually empty
      for users who never tapped "Save this address."
    - SUGGESTED: auto-populated from past deliveries, searches, ride pickups.
      May include points-of-interest like restaurants — NOT safe to trust as
      a delivery home.
    - TARGET: the one Uber currently considers "active" for new orders.

    Each returned place exposes `source` (which bucket) and, when Uber set one,
    a `label` (HOME / WORK / custom nickname). Use `label == "HOME"` to pick the
    user's home address; fall back to prompting the user if there's no label.
    Never auto-pick a SUGGESTED entry as the delivery address — it may be a
    random POI (see pepperoni-to-pizza-restaurant incident, 2026-04-20).
    """

    data = await _eats_post("getDeliveryLocationsV2", {})
    locs = data.get("deliveryLocations") or {}

    places = []
    seen = set()
    for source, items in locs.items():  # SAVED / SUGGESTED / TARGET
        if not isinstance(items, list):
            continue
        for item in items:
            loc = item.get("location") or {}
            loc_id = loc.get("id", "")
            if not loc_id or loc_id in seen:
                continue
            seen.add(loc_id)

            coord = loc.get("coordinate") or {}
            comps = loc.get("addressComponents") or {}
            dp = item.get("deliveryPayload") or {}
            instructions = dp.get("deliveryInstructions") or {}

            # Extract delivery notes from all interaction types
            notes = None
            for instr in instructions.values():
                n = instr.get("deliveryNotes", "")
                if n:
                    notes = n
                    break

            # Label (HOME / WORK / custom) — only present on SAVED entries.
            # Uber keeps it on the item envelope, sometimes under `label`,
            # sometimes nested in `personalization`. Check both.
            label = (item.get("label")
                     or (item.get("personalization") or {}).get("label")
                     or loc.get("label"))

            place = {
                "id": loc_id,
                "name": loc.get("name") or loc.get("title", ""),
                "fullAddress": loc.get("fullAddress"),
                "featureType": "address",
                "latitude": coord.get("latitude"),
                "longitude": coord.get("longitude"),
                "city": comps.get("CITY"),
                "state": comps.get("FIRST_LEVEL_SUBDIVISION_CODE"),
                "country": comps.get("COUNTRY_CODE"),
                "postalCode": comps.get("POSTAL_CODE"),
                "neighborhood": comps.get("NEIGHBORHOOD"),
                "streetName": comps.get("STREET_NAME"),
                "houseNumber": comps.get("HOUSE_NUMBER"),
                "categories": loc.get("categories") or [],
                "source": source,  # SAVED / SUGGESTED / TARGET — bucket from Uber
            }
            if label:
                place["label"] = label
            if notes:
                place["deliveryNotes"] = notes

            places.append(place)

    return places

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order")
@connection("none")
@timeout(15)
async def get_messages(order_uuid: str, **params) -> dict:
    """Get delivery chat messages for an order.

    Backed by getEaterMessagingContentV1. Returns chat content between
    the eater and courier/store during delivery. Chat is ephemeral —
    only available during or shortly after active delivery.
    """

    data = await _eats_post("getEaterMessagingContentV1", {
        "orderUuid": order_uuid,
    })

    body = data.get("body", "")
    head = data.get("head", "")

    if not body and not head:
        return {"orderId": order_uuid, "at": _UBER_EATS, "orderUuid": order_uuid, "messages": [], "status": "empty"}

    return {
        "orderId": order_uuid,
        "at": _UBER_EATS,
        "orderUuid": order_uuid,
        "body": body,
        "head": head,
    }

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("place[]")
@connection("none")
@timeout(15)
async def search_stores(query: str = "", **params) -> list:
    """Search for stores/restaurants on Uber Eats by name or cuisine.

    Backed by getSearchFeedV1 (server-side search). Returns place-shaped entities.
    If no query, returns suggestions and recent searches from getSearchHomeV2.
    """

    if not query:
        # Return search suggestions and history
        data = await _eats_post("getSearchHomeV2", {"dropPastOrders": True})
        suggestions = []
        for section in (data.get("suggestedSections") or []):
            for item in (section.get("items") or []):
                suggestions.append(item.get("title") or item.get("text", ""))
        history = [h.get("title") or h.get("text", "") for h in (data.get("searchHistory") or [])]
        return [{"suggestions": [s for s in suggestions if s], "recentSearches": [h for h in history if h]}]

    # Server-side search via getSearchFeedV1 (discovered via CDP RE 2026-04-05)
    data = await _eats_post("getSearchFeedV1", {
        "userQuery": query,
        "date": "",
        "startTime": 0,
        "endTime": 0,
        "sortAndFilters": [],
        "vertical": "",
        "searchSource": "",
        "displayType": "SEARCH_RESULTS",
        "searchType": "",
        "keyName": "",
        "cacheKey": "",
        "recaptchaToken": "",
    })

    currency = data.get("currencyCode")
    favorites = data.get("favorites") or {}
    items = data.get("feedItems") or []

    stores = []
    seen = set()
    for item in items:
        if item.get("type") == "REGULAR_STORE":
            store_data = item.get("store", item)
            _extract_feed_store(store_data, stores, seen, currency, favorites)

    return stores

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("place[]")
@connection("none")
async def list_nearby_stores(**params) -> list:
    """List nearby stores/restaurants available for delivery.

    Backed by getFeedV1. Returns place-shaped entities (POIs) with
    rating, ETA, delivery fee, and image. Uses the user's saved delivery
    address from their Uber Eats session.
    """

    data = await _eats_post("getFeedV1?localeCode=en-US", {})

    currency = data.get("currencyCode")
    favorites = data.get("favorites") or {}
    items = data.get("feedItems") or []

    stores = []
    seen = set()
    for item in items:
        # Extract stores from both REGULAR_STORE and carousel types
        if item.get("type") == "REGULAR_STORE":
            store_data = item.get("store", item)
            _extract_feed_store(store_data, stores, seen, currency, favorites)
        elif item.get("type") in ("REGULAR_CAROUSEL", "FEATURED_STORES"):
            for store_data in (item.get("carousel", {}).get("stores") or []):
                _extract_feed_store(store_data, stores, seen, currency, favorites)

    return stores

def _extract_feed_store(store_data: dict, out: list, seen: set, currency: str | None, favorites: dict | None = None):
    """Extract a place-shaped entity from a getFeedV1 store item."""
    uuid = store_data.get("storeUuid", "")
    if not uuid or uuid in seen:
        return
    seen.add(uuid)

    title_obj = store_data.get("title") or {}
    name = title_obj.get("text", "") if isinstance(title_obj, dict) else str(title_obj)
    if not name:
        return

    rating_obj = store_data.get("rating") or {}
    marker = store_data.get("mapMarker") or {}
    images = (store_data.get("image") or {}).get("items") or []
    action = store_data.get("actionUrl", "")

    # Parse meta badges for ETA, delivery fee
    eta = None
    delivery_fee = None
    for badge in (store_data.get("meta") or []):
        badge_type = badge.get("badgeType", "")
        text = badge.get("text", "")
        if badge_type == "ETD":
            eta = text
        elif badge_type in ("FARE", "MembershipBenefit"):
            delivery_fee = text

    # Rating text is "4.7" — parse to float
    rating_val = None
    try:
        rating_val = float(rating_obj.get("text", ""))
    except (ValueError, TypeError):
        pass

    # Signpost promos (e.g. "20% off", "Buy 1 get 1", "Spend $20, save $5")
    promos = []
    for sp in (store_data.get("signposts") or []):
        text = sp.get("text") or (sp.get("title") or {}).get("text", "")
        if text:
            promos.append(text)

    # Categories from meta badges (cuisine type like "Pizza", "Italian")
    categories = []
    for badge in (store_data.get("meta") or []):
        badge_type = badge.get("badgeType", "")
        text = badge.get("text", "")
        if badge_type == "CUISINE" or (badge_type == "" and text and text not in ("Delivers", "")):
            categories.append(text)

    # Favorite status — check top-level favorites map and per-store field
    is_fav = store_data.get("favorite", False)
    if not is_fav and favorites:
        is_fav = favorites.get(uuid, False)

    store = {
        # Standard fields
        "id": uuid,
        "name": name,
        "image": images[0]["url"] if images else None,
        "url": f"https://www.ubereats.com{action}" if action else None,
        # Place shape fields
        "featureType": "poi",
        "latitude": marker.get("latitude"),
        "longitude": marker.get("longitude"),
        "rating": rating_val,
        "reviewCount": rating_obj.get("accessibilityText"),
        # Contextual delivery info (depends on user's address, not intrinsic to place)
        "eta": eta,
        "deliveryFee": delivery_fee,
        "currency": currency,
    }
    if is_fav:
        store["isFavorite"] = True
    if categories:
        store["categories"] = categories
    if promos:
        store["promos"] = promos

    out.append(store)
















async def _enrich_payment_display(confirmation: dict) -> dict:
    """Fill payment.display if presentation omitted it (rides whoami fallback)."""
    pay = confirmation.get("payment") or {}
    if pay.get("display"):
        return confirmation
    uuid = pay.get("paymentProfileUuid") or ""
    if not uuid:
        return confirmation
    try:
        data = await _gql("CurrentUserRidersWeb", CURRENT_USER_QUERY)
        user = data.get("currentUser") or {}
        for p in (user.get("paymentProfiles") or []):
            if p.get("uuid") == uuid:
                displayable = p.get("displayable") or {}
                pay["display"] = displayable.get("displayName")
                pay["iconUrl"] = displayable.get("iconURL")
                pay["tokenType"] = p.get("tokenType")
                confirmation["payment"] = pay
                break
    except Exception:
        pass
    return confirmation

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order")
@connection("none")
@timeout(60)
async def add_to_cart(store_uuid: str, items: list, delivery_address_uuid: str = "",
                      currency_code: str = "USD", dining_mode: str = "DELIVERY",
                      **params) -> dict:
    """Add items to an Uber Eats cart for a store.

    Wire path runs in ``__ueats.addToCart`` (page/eats.cart.js). Products must
    include ``_raw`` from ``get_store``. Never call ``checkout`` without
    ``preview_checkout`` + explicit human go.
    """
    mode = (dining_mode or "DELIVERY").upper()
    if mode not in ("DELIVERY", "PICKUP"):
        raise RuntimeError(f"dining_mode must be DELIVERY or PICKUP, got {dining_mode!r}")
    if not items:
        return {"error": "no items to add"}
    if mode == "DELIVERY" and not delivery_address_uuid:
        raise RuntimeError(
            "delivery_address_uuid is required for DELIVERY. Call list_addresses() "
            "to see saved/suggested addresses and pick one explicitly. Apps must "
            "never silently inherit whatever address happens to be active on "
            "the Uber account — that previously caused a pizza to be delivered "
            "to the pizza restaurant. For pickup, pass dining_mode='PICKUP'."
        )

    raw = await _ueats(
        "addToCart",
        {
            "storeUuid": store_uuid,
            "items": items,
            "deliveryAddressUuid": delivery_address_uuid or "",
            "currencyCode": currency_code or "USD",
            "diningMode": mode,
        },
        timeout_s=90,
    )
    if not isinstance(raw, dict) or not raw.get("ok"):
        err = (raw or {}).get("error") if isinstance(raw, dict) else "unknown"
        if err == "session_expired":
            raise RuntimeError("SESSION_EXPIRED: Uber Eats redirected to login — cookies expired.")
        if err == "delivery_address_required":
            raise RuntimeError("delivery_address_uuid is required for DELIVERY.")
        if err == "address_not_found":
            raise RuntimeError(
                f"delivery_address_uuid not found. Known: {(raw or {}).get('known')}"
            )
        raise RuntimeError(f"addToCart failed: {err} raw={_json.dumps(raw, default=str)[:400]}")
    return raw.get("draft") or {}

@returns({"diningMode": "string", "fulfillmentType": "string", "draftOrderUuid": "string", "isPickup": "boolean"})
@connection("none")
@timeout(30)
async def set_dining_mode(draft_order_uuid: str, dining_mode: str, **params) -> dict:
    """Flip a draft cart between DELIVERY and PICKUP via ``__ueats.setDiningMode``."""
    mode = (dining_mode or "").upper()
    if mode not in ("DELIVERY", "PICKUP"):
        raise RuntimeError(f"dining_mode must be DELIVERY or PICKUP, got {dining_mode!r}")
    raw = await _ueats("setDiningMode", draft_order_uuid, mode)
    if not isinstance(raw, dict) or not raw.get("ok"):
        err = (raw or {}).get("error") if isinstance(raw, dict) else "unknown"
        raise RuntimeError(f"setDiningMode failed: {err}")
    return {k: v for k, v in raw.items() if k != "ok"}

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order[]")
@connection("none")
async def get_cart(**params) -> list:
    """Get current Uber Eats carts (``__ueats.getCarts``) as order-shaped drafts."""
    raw = await _ueats("getCarts")
    if not isinstance(raw, dict) or not raw.get("ok"):
        err = (raw or {}).get("error") if isinstance(raw, dict) else "unknown"
        if err == "session_expired":
            raise RuntimeError("SESSION_EXPIRED: Uber Eats redirected to login — cookies expired.")
        raise RuntimeError(f"getCarts failed: {err}")
    return raw.get("carts") or []

@returns({
    "draftOrderUuid": "string", "diningMode": "string", "isPickup": "boolean",
    "store": "object", "eta": "object", "items": "array", "fareBreakdown": "array",
    "total": "string", "totalAmount": "number", "currency": "string",
    "payment": "object", "deliveryAddress": "object",
})
@connection("none")
@timeout(60)
async def preview_checkout(draft_order_uuid: str = "", **params) -> dict:
    """Pre-checkout confirmation — show this, then wait for explicit go.

    Runs ``__ueats.previewCheckout``. Does **not** place the order.
    """
    raw = await _ueats("previewCheckout", draft_order_uuid or "", timeout_s=90)
    if not isinstance(raw, dict) or not raw.get("ok"):
        err = (raw or {}).get("error") if isinstance(raw, dict) else "unknown"
        if err == "multiple_drafts":
            raise RuntimeError(
                "Multiple draft carts — pass draft_order_uuid explicitly. "
                f"Carts: {(raw or {}).get('known')}"
            )
        if err == "no_drafts":
            raise RuntimeError("No draft carts. Call add_to_cart first.")
        if err == "draft_not_found":
            raise RuntimeError(
                f"draft_order_uuid not found. Known: {(raw or {}).get('known')}"
            )
        if err == "session_expired":
            raise RuntimeError("SESSION_EXPIRED: Uber Eats redirected to login — cookies expired.")
        raise RuntimeError(f"previewCheckout failed: {err}")
    confirmation = raw.get("confirmation") or {}
    confirmation = await _enrich_payment_display(confirmation)
    pay = confirmation.get("payment") or {}
    if not pay.get("display"):
        return app_error(
            "Checkout presentation did not include a payment method label. "
            "Refusing to return a confirmation without payment.display — "
            "re-open the cart in Uber Eats or pick a payment method, then "
            "retry preview_checkout.",
            code="PaymentDisplayMissing",
            paymentProfileUuid=pay.get("paymentProfileUuid"),
            draftOrderUuid=confirmation.get("draftOrderUuid"),
        )
    return confirmation

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns({"status": "string", "draftOrderUuid": "string"})
@connection("none")
async def clear_cart(draft_order_uuid: str, **params) -> dict:
    """Discard a draft order via ``__ueats.clearCart``."""
    raw = await _ueats("clearCart", draft_order_uuid)
    if not isinstance(raw, dict) or not raw.get("ok"):
        err = (raw or {}).get("error") if isinstance(raw, dict) else "unknown"
        raise RuntimeError(f"clearCart failed: {err}")
    return {"status": "cleared", "draftOrderUuid": draft_order_uuid}

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order")
@connection("none")
@timeout(60)
async def checkout(draft_order_uuid: str, **params) -> dict:
    """Place an Uber Eats order via ``__ueats.checkout``.

    WRITE — spends money. Requires explicit user consent after
    ``preview_checkout``. Never browser-navigate bare ``/checkout``.
    """
    raw = await _ueats("checkout", draft_order_uuid, timeout_s=90)
    if not isinstance(raw, dict) or not raw.get("ok"):
        err = (raw or {}).get("error") if isinstance(raw, dict) else "unknown"
        if err == "checkout_missing_fields":
            raise RuntimeError(
                "checkout refuse — missing required fields: "
                + ", ".join((raw or {}).get("missing") or [])
                + f". draft={draft_order_uuid!r}. Call preview_checkout first."
            )
        if err == "session_expired":
            raise RuntimeError("SESSION_EXPIRED: Uber Eats redirected to login — cookies expired.")
        if err in ("multiple_drafts", "no_drafts", "draft_not_found"):
            raise RuntimeError(f"checkout draft resolve failed: {err} known={(raw or {}).get('known')}")
        raise RuntimeError(f"checkout failed: {err} raw={_json.dumps(raw, default=str)[:500]}")
    return raw.get("order") or {}

@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order")
@connection("none")
async def track_delivery(order_uuid: str = "", **params) -> dict:
    """Track a live Uber Eats delivery — courier location, ETA, progress, item fulfillment.

    Backed by getActiveOrdersV1 + getOrderEntityByUuidV1.
    If order_uuid is omitted, auto-discovers the current active order.
    Returns order with delivery→trip (courier as driver→person, vehicle),
    and item fulfillment states (PENDING, FOUND, REPLACED, NOT_FOUND).

    Always lists with ``orderUuid: null`` — pinning a UUID on getActiveOrdersV1
    often 500s (PICKUP 2026-07-15). Filter client-side when a UUID is given.
    """
    try:
        discover_orders = await _fetch_active_orders()
    except RuntimeError as e:
        msg = str(e)
        if "SESSION_EXPIRED" in msg or "code=3" in msg or "code=401" in msg:
            return _eats_needs_auth()
        raise

    if not discover_orders:
        return {"status": "not_found", "error": "No active deliveries"}

    order = None
    if order_uuid:
        for candidate in discover_orders:
            if _active_order_uuid(candidate) == order_uuid:
                order = candidate
                break
        if order is None:
            return {
                "id": order_uuid,
                "orderId": order_uuid,
                "status": "not_found",
                "error": f"No active order matching {order_uuid!r}",
            }
    else:
        order = discover_orders[0]
        order_uuid = _active_order_uuid(order)
        if not order_uuid:
            first = discover_orders[0]
            return {
                "status": "not_found",
                "error": "No UUID found in active order",
                "_debug_keys": list(first.keys()),
                "_debug_orderInfo_keys": list(first.get("orderInfo", {}).keys()),
            }

    # Entity fetch is best-effort — grocery fulfillment states only.
    entity_data = {}
    try:
        entity_data = await _eats_post("getOrderEntityByUuidV1", {
            "orderUUID": order_uuid,
            "workflowUuid": order_uuid,
        })
    except RuntimeError:
        entity_data = {}

    # Parse active order
    status_obj = order.get("activeOrderStatus") or {}
    info = order.get("orderInfo") or {}
    contacts = order.get("contacts") or []
    overview = order.get("activeOrderOverview") or {}

    # Extract delivery address from the delivery feed card (always present)
    delivery_card = next((c.get("delivery") for c in (order.get("feedCards") or []) if c.get("type") == "delivery"), None) or {}

    # Courier from contacts + map entities
    courier_contact = next((c for c in contacts if c.get("type") == "COURIER"), None)
    courier_cards = []
    for card in (order.get("feedCards") or []):
        if card.get("courier"):
            courier_cards = card["courier"]

    courier_loc = None
    courier_path = None
    route_polyline = None
    for card in (order.get("backgroundFeedCards") or []):
        for entity in (card.get("mapEntity") or []):
            if entity.get("type") == "COURIER":
                courier_loc = {"latitude": entity.get("latitude"), "longitude": entity.get("longitude")}
                courier_path = entity.get("pathPoints")
                legs = entity.get("routelineLegs") or []
                if legs:
                    route_polyline = legs[0].get("encodedPolyline")

    # Parse item fulfillment from order entity
    entity = entity_data.get("orderEntity") or {}
    cart = entity.get("cart", {}).get("shoppingCart", {})
    items_raw = cart.get("items") or []

    items = []
    for item in items_raw:
        fc = item.get("fulfillmentContext") or {}
        fs = fc.get("fulfillmentState") or {}
        title = item.get("title") or ""
        items.append({
            "shape": "product",
            "id": item.get("skuUUID") or item.get("itemID", {}).get("catalogItemUUID"),
            "name": title,
            "title": title,
            "image": item.get("imageURL"),
            "quantity": item.get("quantity", 1),
            "fulfillmentState": fs.get("type", "UNKNOWN"),
        })

    # Count fulfillment states
    state_counts = {}
    for i in items:
        s = i["fulfillmentState"]
        state_counts[s] = state_counts.get(s, 0) + 1

    # Build the delivery trip
    phase = (status_obj.get("titleSummary") or {}).get("summary", {}).get("text", "")
    eta = (status_obj.get("subtitleSummary") or {}).get("summary", {}).get("text", "")

    # Latest arrival from status card
    status_cards = [c for c in (order.get("feedCards") or []) if c.get("type") == "status"]
    status_card = status_cards[0].get("status", {}) if status_cards else {}
    latest_arrival = (status_card.get("statusSummary") or {}).get("text", "")

    courier_info = courier_cards[0] if courier_cards else {}

    dining_mode = (
        info.get("diningMode")
        or overview.get("diningMode")
        or info.get("fulfillmentType")
        or ""
    )
    if isinstance(dining_mode, str):
        dining_mode = dining_mode.upper()
    else:
        dining_mode = ""

    result = {
        "id": order_uuid,
        "orderId": order_uuid,
        "name": overview.get("title") or info.get("storeInfo", {}).get("name"),
        "status": phase.lower().replace("...", "").replace("…", "").strip() or "active",
        "eta": eta,
        "latestArrival": latest_arrival or None,
        "progress": status_obj.get("currentProgress"),
        "progressTotal": status_obj.get("totalProgressSegments"),
        "total": overview.get("subtitle"),
        "itemStates": state_counts,
        "diningMode": dining_mode or None,
        "isPickup": dining_mode == "PICKUP",
        "contains": items,
        "items": items,
        "itemCount": len(items) or None,
        "url": f"https://www.ubereats.com/orders/{order_uuid}",
        "at": _UBER_EATS,
    }

    # If no items from getOrderEntityByUuidV1, try overview.items and orderSummary feed card
    if not items:
        # overview.items — present on active restaurant orders
        overview_items = overview.get("items") or []
        for oi in overview_items:
            name = oi.get("title") or oi.get("text", "")
            item_data = {
                "name": name,
                "title": name,
                "quantity": oi.get("quantity", 1),
            }
            # Customizations often in subtitle/description
            customization_text = oi.get("subtitle") or oi.get("description") or ""
            if customization_text:
                item_data["customizations"] = customization_text
            if oi.get("imageUrl") or oi.get("image"):
                item_data["image"] = oi.get("imageUrl") or oi.get("image")
            items.append(item_data)

        # Fallback: orderSummary feed card
        if not items:
            for card in (order.get("feedCards") or []):
                if card.get("type") == "orderSummary":
                    summary = card.get("orderSummary") or card
                    summary_items = summary.get("items") or summary.get("orderItems") or []
                    for si in summary_items:
                        item_data = {
                            "name": si.get("title") or si.get("name", ""),
                            "quantity": si.get("quantity", 1),
                        }
                        customization_text = si.get("subtitle") or si.get("description") or ""
                        if customization_text:
                            item_data["customizations"] = customization_text
                        items.append(item_data)

        result["contains"] = items
        result["items"] = items
        result["itemCount"] = len(items) or None

    # Delivery trip with courier — use order UUID as stable ID for upsert
    store_name = overview.get("title") or info.get("storeInfo", {}).get("name") or "Delivery"
    trip_data = {
        "shape": "trip",
        "id": f"{order_uuid}_delivery",
        "name": f"{store_name} {'pickup' if dining_mode == 'PICKUP' else 'delivery'}",
        "tripType": "pickup" if dining_mode == "PICKUP" else "delivery",
        "status": "in_progress",
        "eta": eta,
    }

    if courier_contact:
        # Use courier UUID or phone as stable ID for upsert
        courier_id = courier_contact.get("uuid") or courier_contact.get("formattedPhoneNumber")
        person_data = {
            "shape": "person",
            "name": courier_contact.get("title"),
        }
        if courier_id:
            person_data["id"] = courier_id
        if courier_contact.get("formattedPhoneNumber"):
            person_data["phone"] = courier_contact["formattedPhoneNumber"]
        if courier_info.get("iconUrl"):
            person_data["image"] = courier_info["iconUrl"]
        trip_data["driven_by"] = person_data

    # Parse vehicle from courier card: description="YUSIEL is in a Toyota RAV4", title="YUSIEL • VVL5357"
    desc = courier_info.get("description") or ""
    title_str = courier_info.get("title") or ""
    plate = title_str.split("•")[-1].strip() if "•" in title_str else None
    vehicle_name = desc.split(" is in a ")[-1] if " is in a " in desc else None
    vehicle_parts = vehicle_name.split(None, 1) if vehicle_name else []

    if vehicle_name:
        vehicle = {"name": vehicle_name}
        if len(vehicle_parts) >= 2:
            vehicle["make"] = vehicle_parts[0]
            vehicle["model"] = vehicle_parts[1]
        if plate:
            vehicle["licensePlate"] = plate
        trip_data["vehicle"] = vehicle

    # Courier GPS location as a leg with trace
    if courier_loc:
        leg_data = {
            "shape": "leg",
            "id": f"{order_uuid}_leg_1",
            "name": f"Delivery leg",
            "sequence": 1,
        }
        if courier_loc.get("latitude") and courier_loc.get("longitude"):
            leg_data["trace"] = [courier_loc]
        if courier_path:
            leg_data["trace"] = courier_path
        if route_polyline:
            leg_data["polyline"] = route_polyline
        trip_data["routed_through"] = [leg_data]

    # Store as trip origin
    store_info = info.get("storeInfo") or {}
    store_loc = store_info.get("location") or {}
    if store_info.get("name"):
        store_addr = (store_loc.get("address") or {}).get("eaterFormattedAddress")
        trip_data["starts_at"] = {
            "shape": "place",
            "id": store_info.get("uuid") or store_addr or store_info["name"],
            "name": store_info["name"],
            "fullAddress": store_addr,
            "latitude": store_loc.get("latitude"),
            "longitude": store_loc.get("longitude"),
            "featureType": "poi",
        }
        result["purchased_at"] = trip_data["starts_at"]

    # Delivery address as trip destination
    delivery_addr = info.get("deliveryAddress") or {}
    card_addr = delivery_card.get("address") or {}
    dest_address = delivery_addr.get("address") or card_addr.get("formattedAddress")
    if dest_address:
        eater_entity = next(
            (e for card in (order.get("backgroundFeedCards") or [])
             for e in (card.get("mapEntity") or [])
             if e.get("type") == "EATER"),
            None,
        )
        dest_place = {
            "shape": "place",
            "id": dest_address,
            "name": dest_address,
            "fullAddress": dest_address,
            "featureType": "address",
        }
        if eater_entity:
            dest_place["latitude"] = eater_entity.get("latitude")
            dest_place["longitude"] = eater_entity.get("longitude")
        trip_data["ends_at"] = dest_place

    result["delivered_via"] = trip_data

    # Fare / payment — check all known locations in the response
    fare_info = info.get("fareInfo") or overview.get("fareInfo") or {}
    checkout_info = fare_info.get("checkoutInfo") or []
    if checkout_info:
        result["fareBreakdown"] = [
            {"label": ci.get("label"), "amount": ci.get("rawValue"), "key": ci.get("key")}
            for ci in checkout_info
        ]
    if fare_info.get("totalPrice"):
        result["totalAmount"] = fare_info["totalPrice"] / 100  # cents to dollars

    # Payment — look in feed cards too
    for card in (order.get("feedCards") or []):
        if card.get("type") == "receipt":
            receipt_card = card.get("receipt") or {}
            receipt_items = receipt_card.get("items") or []
            if receipt_items:
                result["fareBreakdown"] = [
                    {"label": ri.get("label"), "amount": ri.get("value"), "key": ri.get("key", "")}
                    for ri in receipt_items
                ]

    # Delivery instructions — from delivery feed card
    delivery_notes = delivery_card.get("description") or delivery_card.get("instructions") or {}
    if isinstance(delivery_notes, dict):
        # Already structured {title, notes}
        if delivery_notes.get("notes") or delivery_notes.get("title"):
            result["deliveryInstructions"] = delivery_notes
    elif delivery_notes:
        result["deliveryInstructions"] = delivery_notes

    return result


