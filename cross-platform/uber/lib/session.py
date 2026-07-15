"""Browser tab kernel for the Uber plugin — eval, __ueats, GraphQL, constants."""
from __future__ import annotations

import json as _json
from pathlib import Path as _Path

from agentos import browser_session, services
from price_parser import Price

# page/eats.js + page/eats.cart.js — shipped page-local SDK.
_PLUGIN_ROOT = _Path(__file__).resolve().parent.parent
_UEATS_JS = (_PLUGIN_ROOT / "page" / "eats.js").read_text(encoding="utf-8")
_UEATS_CART_JS = (_PLUGIN_ROOT / "page" / "eats.cart.js").read_text(encoding="utf-8")

_UEATS_ENSURE = (
    _UEATS_JS
    + "\n"
    + _UEATS_CART_JS
    + "\n"
    + "if (!globalThis.__ueats) throw new Error('page/eats.js failed to install __ueats');\n"
    + "if (!globalThis.__ueats.addToCart) throw new Error('page/eats.cart.js failed to install cart verbs');\n"
)

_RIDES = "riders.uber.com"

_EATS = "www.ubereats.com"

GRAPHQL_PATH = "/graphql"

RIDES_EXTRA_HEADERS = {
    "x-csrf-token": "x",
    "x-uber-rv-session-type": "desktop_session",
}

EATS_API_PATH = "/_p/api"

EATS_EXTRA_HEADERS = {
    "x-csrf-token": "x",
}

CHECKOUT_PAYLOAD_TYPES = [
    "canonicalProductStorePickerPayload", "fulfillmentPromotionInfo",
    "deliveryOptInInfo", "eta", "fareBreakdown", "upfrontTipping",
    "basketSizeTracker", "total", "cartItems", "subtotal", "promotion",
    "disclaimers", "orderConfirmations", "passBanner", "taxProfiles",
    "addressNudge", "basketSize", "complements", "messageBanner",
    "merchantMembership", "giftInfo", "restrictedItems",
    "timeWindowPicker", "locationInfo", "paymentProfilesEligibility",
    "upsellCatalogSections", "subTotalFareBreakdown",
    "storeSwitcherActionableBannerPayload",
    "promoAndMembershipSavingBannerPayloadCheckout",
    "promoAndMembershipSavingBannerPayload", "venueSectionPicker",
    "paymentBarPayload", "neutralZonePayload", "allDetailsHeader",
    "allDetailsActions", "subsRenewalBanner",
    "splitPaymentMessageBanner", "upsellFeed", "requestUtensilPayload",
]

CURRENT_USER_QUERY = """
query CurrentUserRidersWeb {
  currentUser {
    firstName
    lastName
    email
    formattedNumber
    pictureUrl
    rating
    tenancy
    uuid
    role
    signupCountry
    userTags {
      hasDelegate
      isAdmin
      isTester
      isTeen
      __typename
    }
    paymentProfiles {
      authenticationType
      displayable {
        displayName
        iconURL
        __typename
      }
      hasBalance
      tokenType
      uuid
      __typename
    }
    profiles {
      defaultPaymentProfileUuid
      name
      type
      uuid
      __typename
    }
    membershipBenefits {
      hasUberOne
      __typename
    }
    __typename
  }
}
"""

ACTIVITIES_QUERY = """
query Activities($cityID: Int, $endTimeMs: Float, $includePast: Boolean = true, $includeUpcoming: Boolean = true, $limit: Int = 5, $nextPageToken: String, $orderTypes: [RVWebCommonActivityOrderType!] = [RIDES, TRAVEL], $profileType: RVWebCommonActivityProfileType = PERSONAL, $startTimeMs: Float) {
  activities(cityID: $cityID) {
    cityID
    past(
      endTimeMs: $endTimeMs
      limit: $limit
      nextPageToken: $nextPageToken
      orderTypes: $orderTypes
      profileType: $profileType
      startTimeMs: $startTimeMs
    ) @include(if: $includePast) {
      activities {
        ...RVWebCommonActivityFragment
        __typename
      }
      nextPageToken
      __typename
    }
    upcoming @include(if: $includeUpcoming) {
      activities {
        ...RVWebCommonActivityFragment
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment RVWebCommonActivityFragment on RVWebCommonActivity {
  buttons {
    isDefault
    startEnhancerIcon
    text
    url
    __typename
  }
  cardURL
  description
  imageURL {
    light
    dark
    __typename
  }
  subtitle
  title
  uuid
  __typename
}
"""

GET_TRIP_QUERY = """
query GetTrip($tripUUID: String!) {
  getTrip(tripUUID: $tripUUID) {
    trip {
      beginTripTime
      cityID
      countryID
      disableCanceling
      disableRating
      disableResendReceipt
      driver
      dropoffTime
      fare
      guest
      isRidepoolTrip
      isScheduledRide
      isSurgeTrip
      isUberReserve
      jobUUID
      marketplace
      paymentProfileUUID
      showRating
      status
      uuid
      vehicleDisplayName
      vehicleViewID
      waypoints
      __typename
    }
    mapURL
    polandTaxiLicense
    rating
    reviewer
    receipt {
      carYear
      distance
      distanceLabel
      duration
      vehicleType
      __typename
    }
    concierge {
      sourceType
      __typename
    }
    organization {
      name
      __typename
    }
    __typename
  }
}
"""

def _prelude(family: str) -> str:
    """Readiness wait, prepended to every op body.

    A freshly opened tab is still at about:blank when our JS first runs — a
    relative-URL fetch has no origin to resolve against, and a cross-origin
    fetch wouldn't carry the session cookie. We wait until the document has
    settled on the right Uber family origin before the body runs. ``family``
    is ``uber.com`` (rides) or ``ubereats.com`` (eats); ``__onApp`` tells the
    body whether the tab is on the expected host (vs bounced to auth.uber.com
    when logged out).
    """
    return f"""
const __deadline = Date.now() + 15000;
while (location.hostname.indexOf({_json.dumps(family)}) === -1 || document.readyState !== 'complete') {{
  if (Date.now() > __deadline) return {{ __error: 'tab_not_ready' }};
  await new Promise(r => setTimeout(r, 200));
}}
const __onApp = location.hostname.indexOf('auth.uber.com') === -1;
"""

_RIDES_PRELUDE = _prelude("uber.com")

_EATS_PRELUDE = _prelude("ubereats.com")

async def _eval(target: str, body: str, *, timeout_s: int = 45, inject_eats: bool = True):
    """Run an op body inside the target's tab in the engine-owned browser.

    The engine matchmakes the ``browser_session`` provider, opens the tab
    (and launches the browser) when needed, and returns the JS value. The
    body runs only once the tab has settled on the right Uber origin, with
    ``__onApp`` in scope (False when Uber bounced us to auth.uber.com).

    Eats ops prepend ``page/eats.js`` (+ cart) (``window.__ueats``) so Python can call
    page helpers instead of inlining fetch/parse logic.
    """
    prelude = _EATS_PRELUDE if target == _EATS else _RIDES_PRELUDE
    lib = _UEATS_ENSURE if (target == _EATS and inject_eats) else ""
    return await services.call("browser_session", verb="eval", params={
        "mode": "background",  # headless bg profile (rule 19) — never the daily browser
        "target": target,
        "js": "(async () => {\n" + prelude + lib + body + "\n})()",
        "timeout": timeout_s,
    })

async def _ueats(method: str, *args, timeout_s: int = 60):
    """Call ``window.__ueats.<method>(...)`` in the Eats tab; return the value."""
    resp = await _eval(
        _EATS,
        f"""
if (!__onApp) return {{ __error: 'session_expired' }};
return await __ueats[{_json.dumps(method)}](...{_json.dumps(list(args))});
""",
        timeout_s=timeout_s,
    )
    return _check_tab(resp, what=f"__ueats.{method}")

async def _tab_request(target: str, method: str, url: str, *,
                       json_body=None, headers=None) -> dict:
    """One same-origin fetch() inside the target's tab.

    Returns ``{status, json, body}`` so op bodies can read ``resp["json"]`` /
    ``resp.get("status")`` exactly as they did against the HTTP client. The
    cookie rides automatically because the fetch is same-origin.

    Prefer ``__ueats.api`` / ``_ueats`` for Eats RPC — this remains for rides
    GraphQL and any one-off path that isn't on the helper yet.
    """
    opts = {"method": method.upper(), "credentials": "include", "cache": "no-store",
            "headers": dict(headers or {})}
    if json_body is not None:
        opts["headers"]["Content-Type"] = "application/json"
        opts["body"] = _json.dumps(json_body)
    value = await _eval(target, f"""
if (!__onApp) return {{ __error: 'session_expired' }};
const r = await fetch({_json.dumps(url)}, {_json.dumps(opts)});
const text = await r.text();
let parsed = null;
try {{ parsed = JSON.parse(text); }} catch (e) {{}}
return {{ status: r.status, json: parsed, body: parsed ? '' : text, url: r.url }};
""")
    return value

def _check_tab(resp, *, what: str) -> dict:
    """Translate an _eval/_tab_request return into a usable dict or raise.

    ``__error: session_expired`` (the tab bounced to auth.uber.com) and
    ``tab_not_ready`` both mean "no live session" — surface as SESSION_EXPIRED
    so callers' check_session paths report unauthenticated cleanly.
    """
    if not isinstance(resp, dict):
        raise RuntimeError(f"{what}: tab eval returned {resp!r}")
    err = resp.get("__error")
    if err == "session_expired" or err == "tab_not_ready":
        raise RuntimeError(f"SESSION_EXPIRED: no live Uber session in the AgentOS browser profile ({what}).")
    if err:
        raise RuntimeError(f"{what} failed in the tab: {err}")
    return resp

async def _gql(operation_name: str, query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against riders.uber.com via the rides tab.

    Native-interface note: this same-origin POST of {operationName, query,
    variables} to /graphql is byte-identical to what Uber's rider React app
    sends — the stable tap. The real browser supplies all browser headers; we
    add only the app-level x-csrf-token contract.
    """
    resp = _check_tab(
        await _tab_request(_RIDES, "POST", GRAPHQL_PATH,
                           json_body={
                               "operationName": operation_name,
                               "query": query,
                               "variables": variables or {},
                           },
                           headers=RIDES_EXTRA_HEADERS),
        what=f"GraphQL {operation_name}")

    status = resp.get("status") or 0
    body_str = resp.get("body") or ""
    url_final = resp.get("url") or ""
    if status != 200:
        if "auth.uber.com" in body_str or "auth.uber.com" in url_final:
            raise RuntimeError("SESSION_EXPIRED: Uber redirected to login — session expired.")
        raise RuntimeError(f"Uber GraphQL HTTP {status} url={url_final} body={body_str[:200]}")
    body = resp.get("json")
    if not body or not isinstance(body, dict):
        raise RuntimeError(f"Uber GraphQL returned non-JSON: status={status} len={len(body_str)} body={body_str[:300]}")
    if body.get("errors"):
        raise RuntimeError(f"Uber GraphQL error: {body['errors']}")
    return body.get("data", {})

_SYMBOL_TO_ISO = {
    "$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY",
    "₹": "INR", "R$": "BRL", "A$": "AUD", "C$": "CAD",
    "CA$": "CAD", "NZ$": "NZD", "HK$": "HKD", "S$": "SGD",
}

def _parse_fare(fare_str: str) -> tuple[float | None, str | None]:
    """Parse a fare string like '$16.37', 'ZAR 303.00', '£12.50' into (amount, currency_code).

    Uses price-parser for extraction. Returns (None, None) if unparseable.
    """
    if not fare_str:
        return None, None
    is_negative = fare_str.strip().startswith("-")
    p = Price.fromstring(fare_str)
    amount = float(p.amount) if p.amount is not None else None
    if amount is not None and is_negative and amount > 0:
        amount = -amount
    currency = p.currency
    if currency and len(currency) == 3 and currency.isupper():
        return amount, currency  # already ISO code (ZAR, TRY, etc.)
    return amount, _SYMBOL_TO_ISO.get(currency) if currency else None

_UBER = {"shape": "product", "url": "https://uber.com", "name": "Uber"}

_UBER_EATS = {"shape": "product", "url": "https://ubereats.com", "name": "Uber Eats"}

