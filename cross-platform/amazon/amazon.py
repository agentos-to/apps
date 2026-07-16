#!/usr/bin/env python3
"""
Amazon app — search, products, order history, and account identity.

Two halves, two substrates:

- **Public half** (``search_suggestions``, ``search_products``,
  ``get_product``): plain ``client.get`` against Amazon's public
  completion API + HTML pages — the ``public`` connection. No cookies.
- **Account half** (order history, lists, subscriptions, identity, the
  account trio): every op runs as a same-origin ``fetch()`` evaluated
  *inside* a tab of the engine-owned browser via the ``browser_session``
  service (the Exa/Greptile/Uber pattern). The session is the browser
  profile itself — Amazon's ``.amazon.com`` auth cookies (``at-main``,
  ``sess-at-main``, ``sst-main``, …), written by Amazon's own Set-Cookie,
  never extracted, never vaulted, never seen by this app. Requests
  originate from the real browser, so the live session, real TLS
  fingerprint, and every anti-bot cookie ride by construction.

Native-interface note: the browser tab IS a warm, real Amazon session, so
the whole cookie-vault era's anti-bot machinery (custom client hints,
``skip_cookies`` to drop ``csd-key``/``csm-hit``/``aws-waf-token``, the
homepage session-warming hop) is *gone* for auth. Order history still goes
through Amazon Siege (client-side HTML decryption): we navigate and read
the painted DOM after ``SiegeClientSideDecryption`` unlocks the cards —
never parse a raw ``fetch()`` of Your Orders (that's ciphertext).

Amazon's sign-in is email + password + frequent OTP/CAPTCHA, fronted by
heavy anti-bot (Lightsaber). There is no stable form to drive blind, so
``login`` opens a headed ``login_window``, polls until the session is live
(``at-main`` + homepage identity), closes the window, and returns the
account. ``check_session`` + ``logout`` are fully implemented against the tab.
"""

import json
import re
import sys
import asyncio
import time
from typing import Any

from agentos import account, browser_session, claims, client, connection, molt, normalize_email, parse_int, provides, returns, services, test, timeout, url
from lxml import html as lhtml
from lxml.html import HtmlElement


connection(
    'public',
    description='Public Amazon pages and autocomplete API — no auth needed',
    client='browser')


# ───────────────────────────────────────────────────────────────────────────
# Browser-session transport — every account op runs as a same-origin fetch()
# evaluated inside the www.amazon.com tab of the engine-owned browser. The
# session is the browser profile; nothing is extracted or vaulted.
# Template: apps/cross-platform/exa/exa.py, apps/cross-platform/greptile/greptile.py,
# apps/cross-platform/uber/uber.py.
# ───────────────────────────────────────────────────────────────────────────

# browser_session target — a URL substring; the engine opens
# https://<target>/ in the engine-owned browser when no tab matches. One tab
# per registrable domain: www.amazon.com (and its amazon.com family).
_TARGET = "www.amazon.com"

# Honest session cookie — httpOnly, invisible to document.cookie. Present ⇒
# account ops will work; absent ⇒ genuinely logged out. Same gate Instagram
# uses for `sessionid` (see apps-browser-driven § "The honest account check").
_SESSION_COOKIE = "at-main"

# How long `login` waits for the human after opening the headed window before
# returning the auth_challenge for the agent to keep polling. Fits under the
# engine's 10m login_window abandon timer.
_LOGIN_WAIT_S = 240
_LOGIN_POLL_S = 2.0


# Readiness wait, prepended to every op body. A freshly opened tab is still
# at about:blank when our JS first runs — a relative-URL fetch has no origin
# to resolve against, and a cross-origin fetch wouldn't carry the
# ``.amazon.com`` cookie. We wait until the document has settled on an
# amazon.com origin before the body runs. ``__onApp`` tells the body whether
# the tab is on amazon.com proper (vs bounced to the ap/signin auth flow when
# logged out).
_PRELUDE = """
const __deadline = Date.now() + 15000;
while (location.hostname.indexOf('amazon.com') === -1 || document.readyState !== 'complete') {
  if (Date.now() > __deadline) return { __error: 'tab_not_ready' };
  await new Promise(r => setTimeout(r, 200));
}
const __onApp = location.pathname.indexOf('/ap/signin') === -1
  && location.hostname.indexOf('amazon.com') !== -1;
"""


async def _eval(body: str, *, timeout_s: int = 45):
    """Run an op body inside the www.amazon.com tab in the engine-owned browser.

    The engine matchmakes the ``browser_session`` provider, opens the tab
    (and launches the browser) when needed, and returns the JS value. The
    body runs only once the tab has settled on an amazon.com origin, with
    ``__onApp`` in scope (False when Amazon bounced us to the sign-in flow).
    """
    return await services.call("browser_session", verb="eval", params={
        "mode": "background",  # headless bg profile (rule 19) — never the daily browser
        "target": _TARGET,
        "js": "(async () => {\n" + _PRELUDE + body + "\n})()",
        "timeout": timeout_s,
    })


async def _tab_request(method: str, u: str, *, json_body=None, headers=None) -> dict:
    """One same-origin fetch() inside the www.amazon.com tab.

    Returns ``{status, json, body, url}`` so op bodies can read the HTML
    ``body`` for the existing lxml parsers exactly as they did against the
    HTTP client. The cookie rides automatically because the fetch is
    same-origin.
    """
    opts = {"method": method.upper(), "credentials": "include", "cache": "no-store",
            "headers": dict(headers or {})}
    if json_body is not None:
        opts["headers"]["Content-Type"] = "application/json"
        opts["body"] = json.dumps(json_body)
    value = await _eval(f"""
if (!__onApp) return {{ __error: 'session_expired' }};
const r = await fetch({json.dumps(u)}, {json.dumps(opts)});
const text = await r.text();
let parsed = null;
try {{ parsed = JSON.parse(text); }} catch (e) {{}}
return {{ status: r.status, json: parsed, body: parsed ? '' : text, url: r.url }};
""")
    return value


def _check_tab(resp, *, what: str) -> dict:
    """Translate an _eval/_tab_request return into a usable dict or raise.

    ``__error: session_expired`` (tab bounced to the sign-in flow) and
    ``tab_not_ready`` both mean "no live session" — surface as
    SESSION_EXPIRED so callers report unauthenticated cleanly.
    """
    if not isinstance(resp, dict):
        raise RuntimeError(f"{what}: tab eval returned {resp!r}")
    err = resp.get("__error")
    if err in ("session_expired", "tab_not_ready"):
        raise RuntimeError(
            f"SESSION_EXPIRED: no live Amazon session in the AgentOS browser profile ({what})."
        )
    if err == "paint_timeout":
        raise RuntimeError(
            f"{what}: page loaded but Siege never painted plaintext "
            f"(url={resp.get('url')})"
        )
    if err:
        raise RuntimeError(f"{what} failed in the tab: {err}")
    return resp


async def _get_html(u: str, *, headers=None, what: str) -> str:
    """GET a URL same-origin in the tab and return its HTML body for lxml.

    Raises SESSION_EXPIRED if the tab is logged out or Amazon bounced the
    request to the sign-in page.

    Prefer ``_get_painted_html`` for Your Orders / anything Siege-encrypts —
    a raw fetch returns ciphertext even same-origin.
    """
    resp = _check_tab(await _tab_request("GET", u, headers=headers), what=what)
    status = resp.get("status") or 0
    body = resp.get("body") or ""
    url_final = resp.get("url") or ""
    if "ap/signin" in url_final or _is_login_redirect_body(body):
        raise RuntimeError(
            f"SESSION_EXPIRED: Amazon redirected to login ({what}) — sign in headed."
        )
    if status != 200:
        raise RuntimeError(f"Amazon HTTP {status} ({what}) url={url_final}")
    return body


async def _get_painted_html(u: str, *, ready_sel: str, what: str, timeout_s: int = 60) -> str:
    """Return post-Siege (plaintext) HTML for a Siege-encrypted Amazon page.

    Amazon's Your Orders cards are Siege-encrypted on the wire: a same-origin
    ``fetch()`` returns ciphertext that only ``SiegeClientSideDecryption``
    unlocks in-page. Same instinct as WhatsApp / Messenger — if it paints,
    something local decrypted it; read *that*, don't parse the wire.

    Preferred path (avoids a full navigation that can force Amazon's
    ``max_auth_age=0`` order step-up): fetch the HTML in the live tab → mount
    the ``order-card`` nodes into a hidden host → let Siege decrypt (or call
    ``decryptInElementWithId``) → return a synthetic document of plaintext
    cards for the existing lxml parsers.

    Falls back to navigate + scrape when the fetch path can't paint.
    """
    value = await _eval(
        f"""
if (!__onApp) return {{ __error: 'session_expired' }};
const pageUrl = {json.dumps(u)};
const readySel = {json.dumps(ready_sel)};

const r = await fetch(pageUrl, {{
  credentials: 'include',
  cache: 'no-store',
  headers: {{ 'Referer': location.href }},
}});
const text = await r.text();
if (/\\/ap\\/signin|\\/ap\\/mfa/.test(r.url) || /name=["']email["']/.test(text.slice(0, 4000))) {{
  return {{ __error: 'session_expired', url: r.url }};
}}

const doc = new DOMParser().parseFromString(text, 'text/html');
const cards = [...doc.querySelectorAll('div.order-card, div.order')];
if (cards.length === 0) {{
  // Nothing to decrypt — return the fetch body as-is (may already be plain).
  return {{ status: r.status, url: r.url, body: text, via: 'fetch-plain' }};
}}

// Already plaintext? (no Siege scripts, yohtmlc markers present)
const alreadyPlain = cards.some(c =>
  c.querySelector('.yohtmlc-order-id, li.order-header__header-list-item, [data-component="orderId"]')
  && !/SiegeClientSideDecryption/i.test(c.innerHTML));
if (alreadyPlain) {{
  return {{ status: r.status, url: r.url, body: text, via: 'fetch-plain' }};
}}

// Mount into the LIVE document so Siege can unlock (wire ciphertext → DOM).
const host = document.createElement('div');
host.id = 'aos-siege-host';
host.setAttribute('data-aos', 'siege-orders');
host.style.cssText = 'position:fixed;left:-14000px;top:0;width:960px;height:1px;overflow:hidden;opacity:0;pointer-events:none;';
host.innerHTML = cards.map(c => c.outerHTML).join('');
document.body.appendChild(host);

const S = window.SiegeClientSideDecryption;
if (S && typeof S.decryptInElementWithId === 'function') {{
  for (const el of host.querySelectorAll('[id]')) {{
    try {{
      const out = S.decryptInElementWithId(el.id);
      if (out && typeof out.then === 'function') await out;
    }} catch (e) {{}}
  }}
  try {{
    if (typeof S.bootstrap === 'function') {{
      const b = S.bootstrap();
      if (b && typeof b.then === 'function') await b;
    }}
  }} catch (e) {{}}
}}

// Wait until mounted cards show plaintext markers (Siege finished).
const deadline = Date.now() + 12000;
while (Date.now() < deadline) {{
  if (host.querySelector(readySel)) break;
  await new Promise(r => setTimeout(r, 150));
}}

const painted = host.querySelector(readySel);
const paintedCards = [...host.querySelectorAll('div.order-card, div.order')];
const num = doc.querySelector('.num-orders');
const pager = doc.querySelector('ul.a-pagination');
if (painted) {{
  const parts = [];
  if (num) parts.push(num.outerHTML);
  if (pager) parts.push(pager.outerHTML);
  parts.push(...paintedCards.map(c => c.outerHTML));
  const body = '<!doctype html><html><body>' + parts.join('\\n') + '</body></html>';
  const n = paintedCards.length;
  host.remove();
  return {{ status: 200, url: r.url, body, via: 'siege-mount', cards: n }};
}}

host.remove();
// Fallback: full navigation — only if mount decrypt failed.
return {{ __error: 'siege_mount_failed', url: r.url, cardCount: cards.length }};
""",
        timeout_s=timeout_s,
    )

    if isinstance(value, dict) and value.get("__error") == "siege_mount_failed":
        # Last resort: navigate and scrape the painted page.
        await browser_session.navigate(_TARGET, u, timeout=timeout_s)
        value = await _eval(
            f"""
if (!__onApp) return {{ __error: 'session_expired' }};
const readySel = {json.dumps(ready_sel)};
const deadline = Date.now() + 20000;
while (Date.now() < deadline) {{
  if (location.pathname.indexOf('/ap/signin') !== -1
      || location.pathname.indexOf('/ap/mfa') !== -1) {{
    return {{ __error: 'session_expired' }};
  }}
  if (document.querySelector(readySel)) break;
  await new Promise(r => setTimeout(r, 200));
}}
if (location.pathname.indexOf('/ap/signin') !== -1
    || location.pathname.indexOf('/ap/mfa') !== -1) {{
  return {{ __error: 'session_expired' }};
}}
if (!document.querySelector(readySel)) {{
  return {{ __error: 'paint_timeout', url: location.href }};
}}
await new Promise(r => setTimeout(r, 400));
return {{ status: 200, url: location.href, body: document.documentElement.outerHTML, via: 'navigate' }};
""",
            timeout_s=timeout_s,
        )

    resp = _check_tab(value, what=what)
    body = resp.get("body") or ""
    url_final = resp.get("url") or ""
    if "ap/signin" in url_final or _is_login_redirect_body(body):
        raise RuntimeError(
            f"SESSION_EXPIRED: Amazon redirected to login ({what}) — sign in headed."
        )
    return body


# Decrypted-order signal: yohtmlc markers / header list items only exist after
# SiegeClientSideDecryption has unlocked the card bodies.
_ORDERS_READY_SEL = (
    "div.order-card .yohtmlc-order-id, "
    "div.order-card li.order-header__header-list-item, "
    "div.order-card [data-component='orderId']"
)


# ═══════════════════════════════════════════════════════════════════════════════
# MARKETPLACE & DEPARTMENT REGISTRIES
# ═══════════════════════════════════════════════════════════════════════════════

MARKETPLACES: dict[str, dict[str, str]] = {
    "US": {"mid": "ATVPDKIKX0DER", "tld": "com"},
    "UK": {"mid": "A1F83G8C2ARO7P", "tld": "co.uk"},
    "DE": {"mid": "A1PA6795UKMFR9", "tld": "de"},
    "FR": {"mid": "A13V1IB3VIYBER", "tld": "fr"},
    "JP": {"mid": "A1VC38T7YXB528", "tld": "co.jp"},
    "CA": {"mid": "A2EUQ1WTGCTBG2", "tld": "ca"},
    "AU": {"mid": "A39IBJ37TRP1C6", "tld": "com.au"},
    "IN": {"mid": "A21TJRUUN4KGV", "tld": "in"},
    "ES": {"mid": "A1RKKUPIHCS9HS", "tld": "es"},
    "IT": {"mid": "APJ6JRA9NG5V4", "tld": "it"},
    "MX": {"mid": "A1AM78C64UM0Y8", "tld": "com.mx"},
    "BR": {"mid": "A2Q3Y263D00KWC", "tld": "com.br"},
    "NL": {"mid": "A1805IZSGTT6HS", "tld": "nl"},
    "SE": {"mid": "A2NODRKZP88ZB9", "tld": "se"},
    "SG": {"mid": "A19VAU5U5O7RUS", "tld": "sg"},
    "AE": {"mid": "A2VIGQ35RCS4UG", "tld": "ae"},
    "SA": {"mid": "A17E79C6D8DWNP", "tld": "sa"},
    "TR": {"mid": "A33AVAJ2PDY3EV", "tld": "com.tr"},
    "BE": {"mid": "AMEN7PMS3EDWL", "tld": "com.be"},
    "EG": {"mid": "ARBP9OOSHTCHU", "tld": "eg"},
}

DEPARTMENTS: dict[str, str] = {
    "all": "aps",
    "electronics": "electronics",
    "books": "stripbooks",
    "sports": "sporting",
    "toys": "toys-and-games",
    "fashion": "fashion",
    "grocery": "grocery",
    "beauty": "beauty",
    "automotive": "automotive",
    "garden": "garden",
    "videogames": "videogames",
    "tools": "tools",
    "baby": "baby-products",
    "office": "office-products",
    "pets": "pets",
    "music": "digital-music",
    "appliances": "appliances",
    "kitchen": "kitchen",
    "movies": "movies-tv",
    "software": "software",
    "health": "hpc",
    "jewelry": "jewelry",
    "watches": "watches",
    "shoes": "shoes",
    "industrial": "industrial",
    "arts": "arts-crafts-sewing",
    "smart-home": "smart-home",
    "kindle": "digital-text",
}

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _marketplace(key: str | None) -> dict[str, str]:
    return MARKETPLACES.get((key or "US").upper(), MARKETPLACES["US"])


def _alias(department: str | None) -> str:
    if not department:
        return "aps"
    key = department.lower().strip()
    return DEPARTMENTS.get(key, key)


def _extract(pattern: str, s: str, group: int = 1) -> str | None:
    m = re.search(pattern, s, re.S)
    return m.group(group).strip() if m else None


def _parse_price(price_str: str | None) -> float | None:
    if not price_str:
        return None
    digits = re.sub(r"[^\d.]", "", price_str)
    try:
        return float(digits)
    except ValueError:
        return None


def _parse_rating(rating_str: str | None) -> float | None:
    if not rating_str:
        return None
    m = re.search(r"([\d.]+)\s+out of", rating_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _is_captcha(body: str) -> bool:
    markers = ["Robot Check", "Sorry! Something went wrong", "ap_captcha", "opfcaptcha"]
    return any(m in body for m in markers)


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH SUGGESTIONS — completion.amazon.com public JSON API
# ═══════════════════════════════════════════════════════════════════════════════


@test(params={'query': 'wireless headphones'})
@returns({"suggestions": "array", "count": "integer"})
@connection("public")
@timeout(15)
async def search_suggestions(
    query: str,
    department: str | None = None,
    personalized: bool = False,
    marketplace: str | None = None,
    **params,
) -> list[dict[str, Any]]:
    """Fetch autocomplete keyword suggestions from Amazon's public completion API.

    Returns up to 10 keyword suggestions. When personalized=True, the API uses
    Amazon's p13n-expert-pd-ops-ranker for population-level personalized ranking
    instead of the default organic strategy.
    """
    mp = _marketplace(marketplace)
    alias = _alias(department)
    tld = mp["tld"]

    params: dict[str, str] = {
        "mid": mp["mid"],
        "alias": alias,
        "prefix": query,
        "suggestion-type": "KEYWORD",
    }

    if personalized:
        params["session-id"] = "000-0000000-0000000"
        params["lop"] = "en_US"
        params["page-type"] = "Gateway"
        params["site-variant"] = "desktop"

    u = f"https://completion.amazon.{tld}/api/2017/suggestions"

    resp = await client.get(u, params=params)
    data = resp["json"]

    suggestions = [
        {
            "value": s["value"],
            "searchUrl": url.build(f"https://www.amazon.{tld}/s", params={"k": s["value"]}),
            "strategy": s.get("strategyId", "organic"),
            "refTag": s.get("refTag"),
            "department": alias,
            "marketplace": (marketplace or "US").upper(),
        }
        for s in (data.get("suggestions") or [])
        if s.get("value")
    ]
    return {"suggestions": suggestions, "count": len(suggestions)}


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH PRODUCTS — HTML parsing of search result pages
# ═══════════════════════════════════════════════════════════════════════════════


@test(params={'query': 'usb c cable'})
@returns("product[]")
@connection("public")
async def search_products(
    query: str,
    department: str | None = None,
    page: int = 1,
    marketplace: str | None = None,
    **params,
) -> list[dict[str, Any]]:
    """Search Amazon products by parsing search result HTML.

    Navigates to the homepage first to establish a session (anti-bot mitigation),
    then fetches the search results page and extracts product cards.
    """
    mp = _marketplace(marketplace)
    alias = _alias(department)
    tld = mp["tld"]
    base = f"https://www.amazon.{tld}"

    search_params: dict[str, str] = {"k": query}
    if page > 1:
        search_params["page"] = str(page)
    if alias != "aps":
        search_params["i"] = alias

    # Session warming: visit homepage first so the jar accumulates
    # Amazon's anti-bot cookies; /s is then served as a normal browse
    # continuation, not a cold deep-link.
    await client.get(base, headers={"Accept": "text/html"})
    await asyncio.sleep(0.5)
    resp = await client.get(
        f"{base}/s",
        params=search_params,
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    body = resp["body"]

    if _is_captcha(body):
        raise RuntimeError(
            "Amazon returned a CAPTCHA or block page. "
            "Try again later, or use search_suggestions which uses the JSON API with no blocking."
        )

    return _parse_search_results(body, tld)


def _parse_search_results(body: str, tld: str) -> list[dict[str, Any]]:
    soup = _parse(body)
    products: list[dict[str, Any]] = []

    for card in soup.cssselect('div[data-asin][data-component-type="s-search-result"]'):
        asin = card.get("data-asin", "")
        if not asin or not re.match(r'^[A-Z0-9]{10}$', asin):
            continue

        # Title: h2 aria-label or h2 > span text
        h2 = (card.cssselect("h2") or [None])[0]
        title = None
        if h2 is not None:
            title = h2.get("aria-label") or _text(h2)
        title = molt(title)
        if not title:
            continue

        # Price
        price_el = (card.cssselect(".a-price .a-offscreen") or [None])[0]
        price = _text(price_el)

        # Rating
        rating_el = (card.cssselect(".a-icon-alt") or [None])[0]
        rating = _parse_rating(_text(rating_el))

        # Rating count
        count_el = (card.cssselect("[class*='s-underline-text']") or [None])[0]
        ratings_count = parse_int(_text(count_el))

        # Image
        img_el = (card.cssselect("img.s-image") or [None])[0]
        image = img_el.get("src") if img_el is not None else None

        prime = bool(card.cssselect('[aria-label="Amazon Prime"]')) or bool(card.cssselect(".s-prime"))
        sponsored = bool(card.cssselect(".AdHolder"))

        products.append({
            "asin": asin,
            "title": title,
            "price": price,
            "priceAmount": _parse_price(price),
            "rating": rating,
            "ratingsCount": ratings_count,
            "imageUrl": image,
            "url": f"https://www.amazon.{tld}/dp/{asin}",
            "prime": prime,
            "sponsored": sponsored,
        })

    return products


# ═══════════════════════════════════════════════════════════════════════════════
# GET PRODUCT — HTML + a-state parsing of product detail pages
# ═══════════════════════════════════════════════════════════════════════════════


@test(params={'asin': 'B0BQPNMXQV'})
@returns("product")
@connection("public")
async def get_product(
    asin: str,
    marketplace: str | None = None,
    **params,
) -> dict[str, Any]:
    """Fetch detailed product info from an Amazon product detail page."""
    mp = _marketplace(marketplace)
    tld = mp["tld"]
    base = f"https://www.amazon.{tld}"

    # Warm jar with homepage cookies before hitting the detail page.
    await client.get(base, headers={"Accept": "text/html"})
    await asyncio.sleep(0.5)
    resp = await client.get(
        f"{base}/dp/{asin}",
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    body = resp["body"]

    if _is_captcha(body):
        raise RuntimeError("Amazon returned a CAPTCHA or block page.")

    return _parse_product_page(body, asin, tld)


def _parse_product_page(body: str, asin: str, tld: str) -> dict[str, Any]:
    soup = _parse(body)

    title = molt(_text((soup.cssselect("#productTitle") or [None])[0]))

    # Price: core price display → any offscreen price
    price_el = (soup.cssselect("#corePrice_feature_div .a-offscreen, #corePriceDisplay_desktop_feature_div .a-offscreen") or [None])[0]
    if not price_el:
        price_el = (soup.cssselect(".a-offscreen") or [None])[0]
    price = _text(price_el)

    rating = _parse_rating(_text((soup.cssselect("#acrPopover .a-icon-alt") or [None])[0]))
    ratings_count = parse_int(_text((soup.cssselect("#acrCustomerReviewText") or [None])[0]))

    brand_el = (soup.cssselect("#bylineInfo") or [None])[0]
    brand = molt(_text(brand_el))
    if brand:
        brand = re.sub(r"^Visit the\s+", "", brand, flags=re.I)
        brand = re.sub(r"\s+Store$", "", brand, flags=re.I)
        brand = re.sub(r"^Brand:\s*", "", brand, flags=re.I)

    avail_el = (soup.cssselect("#availability span") or [None])[0]
    availability = molt(_text(avail_el))

    img_el = (soup.cssselect("#landingImage") or [None])[0]
    main_image = img_el.get("src") if img_el is not None else None

    desc_el = (soup.cssselect("#productDescription") or [None])[0]
    description = molt(_text(desc_el))
    if not description:
        bullets_el = (soup.cssselect("#feature-bullets") or [None])[0]
        description = molt(_text(bullets_el))

    # Breadcrumb categories
    categories = [molt(_text(a)) for a in soup.cssselect("#wayfinding-breadcrumbs_feature_div a") if molt(_text(a))]

    # Images from ImageBlockATF — extract the JSON array after 'initial':
    images: list[str] = []
    img_start = body.find("'colorImages': { 'initial': [")
    if img_start >= 0:
        arr_start = body.index("[", img_start)
        depth = 0
        arr_end = arr_start
        for ci, ch in enumerate(body[arr_start:arr_start + 20000], arr_start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    arr_end = ci + 1
                    break
        try:
            img_data = json.loads(body[arr_start:arr_end])
            images = [
                item.get("hiRes") or item.get("large") or ""
                for item in img_data
                if isinstance(item, dict)
            ]
            images = [u for u in images if u]
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "asin": asin,
        "title": title,
        "description": description,
        "price": price,
        "priceAmount": _parse_price(price),
        "brand": brand,
        "rating": rating,
        "ratingsCount": ratings_count,
        "reviewCount": ratings_count,
        "imageUrl": main_image or (images[0] if images else None),
        "images": images[:10],
        "url": f"https://www.amazon.{tld}/dp/{asin}",
        "availability": availability,
        "categories": categories,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER HISTORY — authenticated HTML scraping with lxml
# ═══════════════════════════════════════════════════════════════════════════════

BASE = "https://www.amazon.com"


def _is_login_redirect_body(body: str) -> bool:
    """Detect a sign-in page served in the response body (Amazon sometimes
    serves the login form with a 200 instead of a redirect)."""
    head = body[:5000]
    return "form[name='signIn']" in head or "ap_email" in head[:3000] or "signIn" in head[:3000]


def _parse(body: str) -> HtmlElement:
    return lhtml.fromstring(body)


def _select(tag: HtmlElement, selectors: list[str]) -> list[HtmlElement]:
    for sel in selectors:
        result = tag.cssselect(sel)
        if result:
            return result
    return []


def _select_one(tag: HtmlElement, selectors: list[str]) -> HtmlElement | None:
    for sel in selectors:
        result = tag.cssselect(sel)
        if result:
            return result[0]
    return None


def _text(tag: HtmlElement | None) -> str | None:
    if tag is None:
        return None
    t = tag.text_content().strip()
    return t if t else None


# ─── CSS selectors (derived from amazon-orders library) ──────────────────────

ORDER_CARD_SEL = ["div.order-card", "div.order"]
ORDER_ID_SEL = [
    "[data-component='orderId']",
    ".order-date-invoice-item bdi[dir='ltr']",
    ".order-date-invoice-item span[dir='ltr']",
    ".yohtmlc-order-id bdi[dir='ltr']",
    ".yohtmlc-order-id span[dir='ltr']",
    "bdi[dir='ltr']",
    "span[dir='ltr']",
]
ORDER_DATE_SEL = [
    "[data-component='orderDate']",
    "span.order-date-invoice-item",
    "[data-component='briefOrderInfo'] div.a-column",
]
ORDER_TOTAL_SEL = [
    "div.yohtmlc-order-total span.value",
    "div.order-header div.a-column.a-span2",
    "div.order-header div.a-col-left .a-span9",
]
ITEM_SEL = [
    "[data-component='purchasedItems'] .a-fixed-left-grid",
    "div.yohtmlc-item",
    ".item-box",
]
ITEM_TITLE_SEL = [
    "[data-component='itemTitle']",
    ".yohtmlc-item a",
    ".yohtmlc-product-title",
]
ITEM_LINK_SEL = [
    "[data-component='itemTitle'] a",
    ".yohtmlc-item a",
    ".yohtmlc-product-title a",
]
ITEM_IMG_SEL = ["a img"]
ITEM_PRICE_SEL = [
    ".a-price .a-offscreen",
    "[data-component='unitPrice'] .a-text-price :not(.a-offscreen)",
    ".yohtmlc-item .a-color-price",
]
ITEM_QTY_SEL = [
    "[data-component='quantity']",
    ".item-view-qty",
]
SHIPMENT_STATUS_SEL = [
    "span.delivery-box__primary-text",
    ".yohtmlc-shipment-status-primaryText",
]
DETAIL_STATUS_SEL = SHIPMENT_STATUS_SEL + ["h4"]


@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order[]")
@provides("order_history", account_param="account")
@connection("none")
async def list_orders(*, account=None, filter=None, page=1, limit=None, **params) -> list[dict[str, Any]]:
    """List Amazon orders from the order history page.

    Navigates the www.amazon.com tab to Your Orders and parses the
    **post-Siege** DOM. Order-card bodies are Siege-encrypted on the wire —
    a same-origin ``fetch()`` returns ciphertext; after paint,
    ``SiegeClientSideDecryption`` has unlocked the markup the lxml parsers
    already understand. Same instinct as WhatsApp / Messenger: hook the
    local unlock, not the wire.

    Brokered as the `order_history` capability: the Shopping Commons app (and
    any future orders surface) fans this out per connected account and merges,
    never naming Amazon. `account` rides the run so a multi-account future pins
    each read; today the single browser-profile session answers regardless.

    ``limit`` (Shopping Load more) walks pagination until filled or exhausted.
    ``page`` alone still fetches a single Amazon page (~10 cards).
    """
    order_filter = filter or "last30"
    page = int(page or 1)
    want = int(limit) if limit not in (None, "", 0) else None
    if want is not None:
        want = max(1, min(want, 200))

    async def _fetch_page(page_i: int) -> dict[str, Any]:
        url_params: dict[str, str] = {"timeFilter": order_filter}
        if page_i > 1:
            url_params["startIndex"] = str((page_i - 1) * 10)
        body = await _get_painted_html(
            url.build(f"{BASE}/your-orders/orders", params=url_params),
            ready_sel=_ORDERS_READY_SEL,
            what="list_orders",
        )
        return _parse_order_history(body, page=page_i, order_filter=order_filter)

    if want is None:
        return (await _fetch_page(page))["orders"]

    out: list[dict[str, Any]] = []
    page_i = 1
    seen: set[str] = set()
    while len(out) < want and page_i <= 40:
        result = await _fetch_page(page_i)
        for o in result["orders"]:
            oid = str(o.get("orderId") or o.get("id") or "")
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            out.append(o)
            if len(out) >= want:
                break
        if not result.get("hasNext"):
            break
        page_i += 1
    return out[:want]


def _parse_order_history(
    body: str, *, page: int = 1, order_filter: str = "last30",
) -> dict[str, Any]:
    soup = _parse(body)
    orders: list[dict[str, Any]] = []

    total_orders = None
    num_el = (soup.cssselect(".num-orders") or [None])[0]
    if num_el is not None:
        m = re.search(r"(\d+)", _text(num_el) or "")
        if m:
            total_orders = int(m.group(1))

    has_next = bool(soup.cssselect("ul.a-pagination li.a-last a"))

    order_cards = _select(soup, ORDER_CARD_SEL)

    for card in order_cards:
        order_id_tag = _select_one(card, ORDER_ID_SEL)
        order_id_raw = _text(order_id_tag) or ""
        # Live cards often render "Order #\\n 111-…" — extract the id, don't
        # require the whole string to be bare.
        m_id = re.search(r"\d{3}-\d{7}-\d{7}", order_id_raw)
        if not m_id:
            slot = card.get("data-csa-c-slot-id") or ""
            m_id = re.search(r"\d{3}-\d{7}-\d{7}", slot)
        order_id = m_id.group(0) if m_id else None
        if not order_id:
            continue

        order_date = None
        total = None
        for li in card.cssselect("li.order-header__header-list-item"):
            li_text = _text(li) or ""
            if re.search(r"Order\s+placed", li_text, re.I):
                order_date = re.sub(
                    r"^.*?Order\s+[Pp]laced\s*", "", li_text, flags=re.I
                ).strip()
                order_date = re.sub(r"\s+", " ", order_date).strip() or None
            elif re.match(r"Total\b", li_text.lstrip(), re.I):
                m = re.search(r"\$[\d,.]+", li_text)
                total = m.group() if m else None

        if not order_date:
            date_tag = _select_one(card, ORDER_DATE_SEL)
            order_date = _text(date_tag)
            if order_date:
                order_date = re.sub(r"^.*?Order [Pp]laced\s*", "", order_date).strip()
                order_date = re.sub(r"\s*Order #.*$", "", order_date).strip()

        if not total:
            total_tag = _select_one(card, ORDER_TOTAL_SEL)
            total_text = _text(total_tag)
            if total_text:
                m = re.search(r"\$[\d,.]+", total_text)
                total = m.group() if m else total_text.strip()

        status = None
        status_tag = _select_one(card, SHIPMENT_STATUS_SEL)
        if status_tag:
            status = molt(_text(status_tag))

        delivery_date = None
        if status:
            dm = re.search(
                r"(?:Delivered|Arriving)\s+([A-Z][a-z]+ \d{1,2}(?:,\s*\d{4})?)",
                status, re.I,
            )
            delivery_date = dm.group(1) if dm else None

        items = _parse_order_items(card)

        orders.append({
            "id": order_id,
            "orderId": order_id,
            "name": f"Order {order_id}",
            "orderDate": order_date,
            "total": total,
            "totalAmount": _parse_price(total),
            "status": status,
            "deliveryDate": delivery_date,
            "itemCount": len(items),
            "items": items,
            "url": f"{BASE}/gp/your-account/order-details?orderID={order_id}",
        })

    total_pages = None
    if total_orders is not None:
        total_pages = (total_orders + 9) // 10

    return {
        "orders": orders,
        "page": page,
        "perPage": 10,
        "totalOrders": total_orders,
        "totalPages": total_pages,
        "hasNext": has_next,
        "filter": order_filter,
    }


def _parse_order_items(card: HtmlElement, *, detail_page: bool = False) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_asins: set[str] = set()

    if detail_page:
        for title_el in card.cssselect("[data-component='itemTitle']"):
            title = molt(_text(title_el))

            container = title_el
            for _ in range(10):
                container = container.getparent()
                if container is None or not hasattr(container, "get"):
                    break
                if "a-fixed-left-grid" in (container.get("class") or "").split():
                    break

            ctx = container if container is not None else title_el.getparent()
            asin = None
            for a in ctx.cssselect("a[href]"):
                m = re.search(r"/dp/([A-Z0-9]{10})", a.get("href", ""))
                if m:
                    asin = m.group(1)
                    break

            if not asin or asin in seen_asins:
                continue
            seen_asins.add(asin)

            price_tag = _select_one(ctx, ITEM_PRICE_SEL)
            price = _text(price_tag)

            qty_tag = _select_one(ctx, ITEM_QTY_SEL)
            qty_text = _text(qty_tag)
            quantity = int(qty_text) if qty_text and qty_text.isdigit() else 1

            img_tag = (ctx.cssselect("img") or [None])[0]
            image_url = img_tag.get("src") if img_tag is not None else None

            items.append({
                "asin": asin,
                "title": title,
                "url": f"{BASE}/dp/{asin}",
                "imageUrl": str(image_url) if image_url else None,
                "price": price,
                "priceAmount": _parse_price(price),
                "quantity": quantity,
            })
        return items

    item_tags = _select(card, ITEM_SEL)
    for item_tag in item_tags:
        link_tag = _select_one(item_tag, ITEM_LINK_SEL)
        href = link_tag.get("href", "") if link_tag else ""
        asin_m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", str(href))
        asin = asin_m.group(1) if asin_m else None

        if not asin or asin in seen_asins:
            continue
        seen_asins.add(asin)

        title_tag = _select_one(item_tag, ITEM_TITLE_SEL)
        title = molt(_text(title_tag))

        img_tag = _select_one(item_tag, ITEM_IMG_SEL)
        image_url = img_tag.get("src") if img_tag else None

        price_tag = _select_one(item_tag, ITEM_PRICE_SEL)
        price = _text(price_tag)

        items.append({
            "asin": asin,
            "title": title,
            "url": f"{BASE}/dp/{asin}",
            "imageUrl": str(image_url) if image_url else None,
            "price": price,
            "priceAmount": _parse_price(price),
            "quantity": 1,
        })

    if not items:
        asin_titles: dict[str, str | None] = {}
        asin_images: dict[str, str | None] = {}
        for a in card.cssselect("a[href]"):
            href = a.get("href", "")
            m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", str(href))
            if not m:
                continue
            asin = m.group(1)
            text = a.text_content().strip()
            if text and asin not in asin_titles:
                asin_titles[asin] = text
            elif asin not in asin_titles:
                asin_titles.setdefault(asin, None)
            img = (a.cssselect("img") or [None])[0]
            if img is not None and asin not in asin_images:
                asin_images[asin] = str(img.get("src", ""))

        for asin, title in asin_titles.items():
            if asin in seen_asins:
                continue
            seen_asins.add(asin)
            items.append({
                "asin": asin,
                "title": title,
                "url": f"{BASE}/dp/{asin}",
                "imageUrl": asin_images.get(asin),
                "price": None,
                "priceAmount": None,
                "quantity": 1,
            })

    return items


@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("product[]")
@provides("product_feed", account_param="account")
@connection("none")
async def buy_again(*, account=None, **params) -> list[dict[str, Any]]:
    """Get products Amazon recommends for repurchase.

    Brokered as the `product_feed` capability — the account's shelf of
    likely-repurchase products. The Shopping app reads it the same way it
    reads `order_history`; a future provider (Uber's reorder shelf) lights up
    for free by declaring the same `@provides`.
    """

    body = await _get_html(
        f"{BASE}/gp/buyagain",
        headers={"Referer": f"{BASE}/your-orders/orders"},
        what="buy_again",
    )
    return _parse_buy_again(body)


def _parse_buy_again(body: str) -> list[dict[str, Any]]:
    soup = _parse(body)
    products: list[dict[str, Any]] = []
    seen: set[str] = set()

    for el in soup.cssselect("[data-asin]"):
        asin = el.get("data-asin", "")
        if not asin or not re.match(r"^[A-Z0-9]{10}$", asin) or asin in seen:
            continue

        title_el = (
            (el.cssselect("span.a-truncate-full") or [None])[0]
            or (el.cssselect("[data-component='title']") or [None])[0]
        )
        title = molt(_text(title_el))
        if not title:
            continue

        seen.add(asin)

        price_el = (el.cssselect(".a-price .a-offscreen") or [None])[0]
        price = _text(price_el)

        img = (el.cssselect("img") or [None])[0]
        image_url = str(img.get("src", "")) if img is not None else None

        prime = bool(el.cssselect("i.a-icon-prime"))

        badge_el = (el.cssselect(".a-badge-text") or [None])[0]
        badge = _text(badge_el)

        products.append({
            "asin": asin,
            "title": title,
            "url": f"{BASE}/dp/{asin}",
            "imageUrl": image_url,
            "price": price,
            "priceAmount": _parse_price(price),
            "prime": prime,
            "badge": badge,
        })

    return products


@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("membership[]")
@provides("memberships", account_param="account")
@connection("none")
@timeout(60)
async def subscriptions(*, account=None, **params) -> list[dict[str, Any]]:
    """List active Subscribe & Save subscriptions as ``membership`` rows.

    Brokered as the ``memberships`` capability — Shopping (and any future
    "what am I subscribed to" surface) fans this out per account. Amazon's
    Subscribe & Save is a commerce subscription; we emit the shared
    ``membership`` shape (status/price/billing cadence) so gym plans and
    SNS share one list. Navigate the live tab and scrape painted cards —
    the ajax fragment's title often lives only in ``img[alt]``.
    """
    await browser_session.navigate(
        _TARGET,
        f"{BASE}/auto-deliveries/subscriptionList",
        timeout=60,
    )
    raw = await _eval("""
if (!__onApp) return { __error: 'session_expired' };
const deadline = Date.now() + 20000;
while (Date.now() < deadline) {
  if (location.pathname.indexOf('/ap/signin') !== -1) return { __error: 'session_expired' };
  if (document.querySelectorAll('[data-subscription-id]').length > 0) {
    await new Promise(r => setTimeout(r, 600));
    break;
  }
  await new Promise(r => setTimeout(r, 200));
}
const cards = [...document.querySelectorAll('[data-subscription-id]')];
const items = cards.map(el => {
  const id = el.getAttribute('data-subscription-id') || '';
  const img = el.querySelector('img');
  const trunc = el.querySelector('span.a-truncate-full, .a-truncate-cut');
  let title = (trunc && trunc.textContent || '').trim();
  if (!title && img) title = (img.getAttribute('alt') || '').trim();
  if (!title) {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    title = t.split('Next delivery')[0].trim().slice(0, 160);
  }
  const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
  let nextDelivery = null;
  const nm = text.match(
    /Next delivery by\\s*([A-Za-z]+\\s+\\d{1,2}(?:,?\\s*\\d{4})?)/i,
  );
  if (nm) nextDelivery = nm[1].trim();
  let frequency = null;
  const fm = text.match(/(\\d+\\s+unit[s]?\\s+every\\s+\\d+\\s+(?:month|week|day)s?)/i)
    || text.match(/every\\s+\\d+\\s+(?:month|week|day)s?/i);
  if (fm) frequency = fm[1].trim();
  const priceEl = el.querySelector('.a-price .a-offscreen, .a-color-price');
  const price = priceEl ? (priceEl.textContent || '').trim() : null;
  const imageUrl = img
    ? (img.getAttribute('data-a-hires') || img.getAttribute('data-src') || img.getAttribute('src') || null)
    : null;
  return { subscriptionId: id, title, nextDelivery, frequency, price, imageUrl };
}).filter(x => x.subscriptionId && x.title);
return { items, url: location.href, count: items.length };
""", timeout_s=60)
    data = _check_tab(raw, what="subscriptions")
    items = data.get("items") or []
    return [_sns_to_membership(it) for it in items]


def _sns_to_membership(it: dict[str, Any]) -> dict[str, Any]:
    """Subscribe & Save card → ``membership`` shape."""
    sub_id = str(it.get("subscriptionId") or "")
    title = molt(it.get("title")) or f"Subscribe & Save {sub_id}"
    price = it.get("price")
    frequency = it.get("frequency")
    next_delivery = it.get("nextDelivery")
    billing = None
    if isinstance(frequency, str):
        if re.search(r"every\s+1\s+month|every\s+month", frequency, re.I):
            billing = "monthly"
        elif re.search(r"every\s+(\d+)\s+month", frequency, re.I):
            billing = "monthly"
        elif re.search(r"week", frequency, re.I):
            billing = "weekly"

    out: dict[str, Any] = {
        "id": sub_id,
        "name": title,
        "status": "active",
        "autoRenew": True,
        "tier": "Subscribe & Save",
        "billingType": billing,
        "price": _parse_price(price) if price else None,
        "currency": "USD",
        "url": f"{BASE}/auto-deliveries/subscriptionList",
        "image": it.get("imageUrl"),
        "content": " · ".join(
            p for p in (frequency, f"Next: {next_delivery}" if next_delivery else None) if p
        ) or None,
        # SNS-native fields for the Shopping shelf (not all are membership vals).
        "frequency": frequency,
        "nextDelivery": next_delivery,
        "subscriptionId": sub_id,
    }
    return out


def _parse_subscriptions(body: str) -> list[dict[str, Any]]:
    """Legacy ajax-fragment parser — kept for debugging; live path scrapes the DOM."""
    soup = _parse(body)
    items: list[dict[str, Any]] = []

    for el in soup.cssselect("[data-subscription-id]"):
        sub_id = el.get("data-subscription-id", "")

        title_el = (el.cssselect("span.a-truncate-full") or [None])[0]
        title = molt(_text(title_el))
        if not title:
            img0 = (el.cssselect("img") or [None])[0]
            if img0 is not None:
                title = molt(img0.get("alt"))
        if not title:
            continue

        # Image: use data-a-hires or data-src (src is a placeholder pixel)
        img = (el.cssselect("img.sns-product-image, img") or [None])[0]
        image_url = None
        if img is not None:
            image_url = (
                img.get("data-a-hires")
                or img.get("data-src")
                or img.get("src", "")
            )
            if image_url and "grey-pixel" in image_url:
                image_url = None

        # Next delivery date
        next_delivery = None
        for div in el.cssselect("div, span"):
            text = _text(div) or ""
            m = re.search(
                r"Next delivery by\s*(.+)",
                text, re.I,
            )
            if m:
                next_delivery = m.group(1).strip()
                break
        if not next_delivery:
            for span in el.cssselect("span, div"):
                text = _text(span) or ""
                if re.match(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2}", text) and len(text) < 30:
                    next_delivery = text
                    break

        # Frequency (e.g., "1 unit every 3 months")
        frequency = None
        for a in el.cssselect("a.consumption-pattern-ingress-text, span.a-declarative a"):
            text = _text(a) or ""
            if re.search(r"every\s+\d+", text, re.I):
                frequency = text
                break
        if not frequency:
            for span in el.cssselect("span, div"):
                text = _text(span) or ""
                if re.search(r"\d+\s+unit.*every", text, re.I):
                    frequency = text
                    break

        # Price (sometimes shown)
        price_el = (el.cssselect(".a-price .a-offscreen") or [None])[0]
        price = _text(price_el)

        items.append({
            "subscriptionId": sub_id,
            "title": title,
            "imageUrl": str(image_url) if image_url else None,
            "nextDelivery": next_delivery,
            "frequency": frequency,
            "price": price,
            "priceAmount": _parse_price(price),
        })

    return items


@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("order")
@connection("none")
async def get_order(*, order_id, **params) -> dict[str, Any]:
    """Fetch detailed info for a specific Amazon order."""
    if not order_id:
        raise ValueError("order_id is required")

    body = await _get_html(
        url.build(f"{BASE}/gp/your-account/order-details", params={"orderID": order_id}),
        headers={"Referer": f"{BASE}/your-orders/orders"},
        what="get_order",
    )
    return _parse_order_detail(body, order_id)


def _parse_order_detail(body: str, order_id: str) -> dict[str, Any]:
    soup = _parse(body)

    container = _select_one(soup, ["div#orderDetails", "div#ordersContainer"]) or soup

    # Order date — detail page puts it in .order-date-invoice-item directly
    order_date = None
    date_tag = _select_one(container, ORDER_DATE_SEL)
    if date_tag:
        raw = _text(date_tag) or ""
        order_date = re.sub(r"^.*?Order [Pp]laced\s*", "", raw).strip() or raw

    # Delivery status — detail page often uses <h4> with "DeliveredMarch 10" format
    status = None
    for sel_list in [SHIPMENT_STATUS_SEL, ["h4"]]:
        for sel in sel_list:
            for el in container.cssselect(sel):
                text = _text(el) or ""
                if re.search(r"Deliver|Arriving|Shipped|Return|Cancel", text, re.I):
                    status = re.sub(r"(Delivered|Arriving)", r"\1 ", text).strip()
                    status = re.sub(r"\s{2,}", " ", status)
                    break
            if status:
                break
        if status:
            break

    delivery_date = None
    if status:
        dm = re.search(
            r"(?:Delivered|Arriving)\s+([A-Z][a-z]+ \d{1,2}(?:,\s*\d{4})?)",
            status, re.I,
        )
        delivery_date = dm.group(1) if dm else None

    # Shipping address — extract from list items, join with newlines
    shipping_address = None
    addr_tag = _select_one(container, [
        "[data-component='shippingAddress']",
        "div.displayAddressDiv",
    ])
    if addr_tag:
        parts = []
        for li in addr_tag.cssselect("li .a-list-item"):
            text = ", ".join(t.strip() for t in li.itertext() if t.strip())
            if text:
                parts.append(text)
        if parts:
            shipping_address = "\n".join(parts)
        else:
            raw_addr = _text(addr_tag) or ""
            shipping_address = re.sub(r"^Ship\s*to\s*", "", raw_addr).strip()

    # Tracking link
    track_tag = (container.cssselect("a[href*='track']") or [None])[0]
    tracking_url = None
    if track_tag is not None:
        href = track_tag.get("href", "")
        tracking_url = href if href.startswith("http") else f"{BASE}{href}"

    # Order summary from #od-subtotals
    summary: dict[str, str | None] = {}
    subtotals = (container.cssselect("#od-subtotals") or [None])[0]
    if subtotals is not None:
        for row in subtotals.cssselect(".a-row"):
            label_el = (row.cssselect(".a-column.a-span7") or [None])[0]
            value_el = (row.cssselect(".a-column.a-span5") or [None])[0]
            if label_el and value_el:
                label = (_text(label_el) or "").rstrip(":").strip()
                value = _text(value_el)
                if "Subtotal" in label:
                    summary["subtotal"] = value
                elif "Shipping" in label:
                    summary["shipping"] = value
                elif "tax" in label.lower():
                    summary["tax"] = value
                elif "Grand Total" in label:
                    summary["grand_total"] = value
                elif "saving" in label.lower() or "discount" in label.lower():
                    summary["discount"] = value

    total = summary.get("grand_total")
    if not total:
        total_tag = _select_one(container, ORDER_TOTAL_SEL)
        total_text = _text(total_tag)
        if total_text:
            m = re.search(r"\$[\d,.]+", total_text)
            total = m.group() if m else None

    items = _parse_order_items(container, detail_page=True)

    return {
        "id": order_id,
        "orderId": order_id,
        "name": f"Order {order_id}",
        "orderDate": order_date,
        "total": total,
        "totalAmount": _parse_price(total),
        "status": status,
        "deliveryDate": delivery_date,
        "shipped_to": shipping_address,
        "trackingUrl": tracking_url,
        "summary": summary or None,
        "itemCount": len(items),
        "items": items,
        "url": f"{BASE}/gp/your-account/order-details?orderID={order_id}",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LISTS — wishlists, shopping lists, idea lists
# ═══════════════════════════════════════════════════════════════════════════════

LIST_NAV_SEL = [
    "#your-lists-nav .wl-list",
    ".wl-list",
]

LIST_ITEM_SEL = [
    "li.g-item-sortable[data-itemid]",
    "li[data-itemid]",
]

LIST_ITEM_TITLE_SEL = [
    "a[id^='itemName_']",
    "h2 a.a-link-normal[title]",
    "a.a-link-normal[title]",
]

LIST_ITEM_PRICE_SEL = [
    ".price-section .a-price .a-offscreen",
    ".a-price .a-offscreen",
]

LIST_ITEM_RATING_SEL = [
    ".a-icon-star-small span.a-icon-alt",
    "i.a-icon-star-small span.a-icon-alt",
]

LIST_ITEM_REVIEW_COUNT_SEL = [
    "a[id^='review_count_']",
    "a.a-link-normal[aria-label]",
]

MAX_LIST_PAGES = 20


@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("list[]")
@provides("lists", account_param="account")
@connection("none")
async def list_lists(*, account=None, **params) -> list[dict[str, Any]]:
    """List all of the user's Amazon lists (wishlists, shopping lists, etc.).

    Brokered as the ``lists`` capability — Shopping fans this out per account
    and nests each wishlist under the account tree. Items load via the
    sibling ``get_list`` verb (HTML + ``/hz/wishlist/slv/items`` AJAX — no
    GraphQL on this surface).
    """

    body = await _get_html(
        f"{BASE}/hz/wishlist/ls",
        headers={"Referer": BASE},
        what="list_lists",
    )
    return _parse_lists_nav(body)


def _parse_lists_nav(body: str) -> list[dict[str, Any]]:
    soup = _parse(body)
    lists: list[dict[str, Any]] = []

    for entry in _select(soup, LIST_NAV_SEL):
        link = (entry.cssselect("a[id^='wl-list-link-']") or [None])[0]
        if not link:
            continue

        link_id = (link.get("id") or "").replace("wl-list-link-", "")
        if not link_id:
            continue

        title_el = (entry.cssselect("span[id^='wl-list-entry-title-']") or [None])[0]
        name = _text(title_el) or "Untitled List"

        privacy_el = (entry.cssselect(".wl-list-entry-privacy span") or [None])[0]
        privacy = _text(privacy_el)

        is_default = bool(entry.cssselect("#list-default-collaborator-label"))

        list_type = None
        href = link.get("href", "")
        if "type=" in href:
            m = re.search(r"type=([^&]+)", href)
            if m:
                list_type = m.group(1)

        lists.append({
            "id": link_id,
            "listId": link_id,
            "name": name,
            "url": f"{BASE}/hz/wishlist/ls/{link_id}",
            "privacy": privacy,
            "isPublic": (privacy or "").lower() == "public",
            "isDefault": is_default,
            "listType": list_type or "WishList",
            "member_shape": "product",
            "ordering_mode": "unordered",
        })

    return lists


@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("list")
@connection("none")
@timeout(60)
async def get_list(*, list_id, filter=None, **params) -> dict[str, Any]:
    """Get a wishlist (or shopping list) with its product items.

    Sibling of ``list_lists`` — Shopping opens one via
    ``services.lists {verb: get_list, list_id}``. Pagination is Amazon's
    HTML AJAX ``/hz/wishlist/slv/items`` (token in ``scrollState``); each
    item carries ``dateAdded`` from the painted ``itemAddedDate_*`` span.
    """
    if not list_id:
        raise ValueError("get_list requires a list_id parameter")
    item_filter = filter or "unpurchased"

    all_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    list_name = None
    list_privacy = None
    list_type = None

    body = await _get_html(
        url.build(f"{BASE}/hz/wishlist/ls/{list_id}",
                  params={"filter": item_filter, "sort": "date-added", "viewType": "list"}),
        headers={"Referer": BASE},
        what="get_list",
    )

    soup = _parse(body)

    name_el = (soup.cssselect("#profile-list-name") or [None])[0]
    list_name = _text(name_el) or "Wish List"

    privacy_el = (soup.cssselect("#listPrivacy") or [None])[0]
    list_privacy = _text(privacy_el)

    remember_state = _extract_a_state(soup, "rememberState")
    if remember_state:
        list_type = remember_state.get("listType")

    page_items = _parse_list_items(soup, list_id=list_id, list_name=list_name)
    for item in page_items:
        key = item.get("asin") or item.get("id")
        if key and key not in seen:
            seen.add(str(key))
            all_items.append(item)

    for _ in range(MAX_LIST_PAGES - 1):
        scroll_state = _extract_a_state(soup, "scrollState")
        if not scroll_state:
            break
        show_more = scroll_state.get("showMoreUrl")
        if not show_more:
            break

        ajax_resp = _check_tab(
            await _tab_request(
                "GET",
                f"{BASE}{show_more}" if show_more.startswith("/") else show_more,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{BASE}/hz/wishlist/ls/{list_id}",
                },
            ),
            what="get_list_ajax",
        )
        if ajax_resp.get("status") != 200:
            break

        ajax_body = ajax_resp.get("body") or ""
        ajax_soup = _parse(ajax_body)

        page_items = _parse_list_items(ajax_soup, list_id=list_id, list_name=list_name)
        if not page_items:
            break

        new_count = 0
        for item in page_items:
            key = item.get("asin") or item.get("id")
            if key and str(key) not in seen:
                seen.add(str(key))
                all_items.append(item)
                new_count += 1
        if new_count == 0:
            break

        soup = ajax_soup

    return {
        "id": list_id,
        "listId": list_id,
        "name": list_name,
        "url": f"{BASE}/hz/wishlist/ls/{list_id}",
        "privacy": list_privacy,
        "isPublic": (list_privacy or "").lower() == "public",
        "listType": list_type or "WishList",
        "member_shape": "product",
        "ordering_mode": "unordered",
        "itemCount": len(all_items),
        "items": all_items,
    }


def _extract_a_state(soup: HtmlElement, key: str) -> dict[str, Any] | None:
    for script in soup.cssselect('script[type="a-state"]'):
        try:
            state_meta = json.loads(script.get("data-a-state", "{}"))
            if state_meta.get("key") == key:
                return json.loads(script.text or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _parse_list_items(
    soup: HtmlElement,
    *,
    list_id: str | None = None,
    list_name: str | None = None,
) -> list[dict[str, Any]]:
    """Wishlist row HTML → ``product`` dicts (with SNS-style extras).

    ``dateAdded`` is only in the painted DOM (``Item added …``) — Amazon's
    wishlist AJAX returns HTML fragments, not GraphQL JSON.
    """
    items: list[dict[str, Any]] = []

    for li in _select(soup, LIST_ITEM_SEL):
        item_id = li.get("data-itemid", "")
        if not item_id:
            continue

        asin = None
        repo_params_str = li.get("data-reposition-action-params", "")
        if repo_params_str:
            try:
                repo = json.loads(repo_params_str)
                ext_id = repo.get("itemExternalId", "")
                m = re.match(r"ASIN:([A-Z0-9]{10})", ext_id)
                if m:
                    asin = m.group(1)
            except (json.JSONDecodeError, TypeError):
                pass

        title_el = _select_one(li, LIST_ITEM_TITLE_SEL)
        title = None
        if title_el is not None:
            title = title_el.get("title") or _text(title_el)
            if not asin:
                href = title_el.get("href", "") or ""
                m = re.search(r"/dp/([A-Z0-9]{10})", href)
                if m:
                    asin = m.group(1)
        if not title:
            trunc = (
                (li.cssselect("span.a-truncate-full") or [None])[0]
                or (li.cssselect(".a-truncate-cut") or [None])[0]
            )
            title = _text(trunc)

        if not asin:
            continue

        price_el = _select_one(li, LIST_ITEM_PRICE_SEL)
        price = _text(price_el)

        byline_el = (li.cssselect("span[id^='item-byline-']") or [None])[0]
        byline = _text(byline_el)

        rating_el = _select_one(li, LIST_ITEM_RATING_SEL)
        rating_text = _text(rating_el)

        review_el = _select_one(li, LIST_ITEM_REVIEW_COUNT_SEL)
        review_text = _text(review_el)
        review_count = None
        if review_text:
            clean = re.sub(r"[^\d]", "", review_text)
            review_count = int(clean) if clean else None

        img_el = (li.cssselect(f"#itemImage_{item_id} img") or li.cssselect("img") or [None])[0]
        image_url = None
        if img_el is not None:
            image_url = (
                img_el.get("data-a-hires")
                or img_el.get("data-src")
                or img_el.get("src")
                or None
            )
            if image_url and "grey-pixel" in image_url:
                image_url = None
            if not title:
                alt = (img_el.get("alt") or "").strip()
                if alt and not re.fullmatch(r"[A-Z0-9]{10}", alt):
                    title = alt

        if not asin:
            continue
        # Prefer a real title; never ship the ASIN as the display name.
        name = molt(title) if title else None
        if not name or name == asin:
            name = molt(byline) or f"Amazon item {asin}"

        date_el = (li.cssselect("span[id^='itemAddedDate_']") or [None])[0]
        date_added = _text(date_el)
        if date_added:
            date_added = re.sub(r"^Item added\s*", "", date_added).strip()

        priority_el = (li.cssselect("span[id^='itemPriorityLabel_']") or [None])[0]
        priority = _text(priority_el)

        comment_el = (li.cssselect("span[id^='itemComment_']") or [None])[0]
        comment = _text(comment_el)

        items.append({
            "id": asin,
            "asin": asin,
            "itemId": item_id,
            "name": name,
            "title": name,
            "url": f"{BASE}/dp/{asin}",
            "image": image_url,
            "imageUrl": image_url,
            "author": molt(byline),
            "byline": molt(byline),
            "price": price,
            "priceAmount": _parse_price(price),
            "rating": _parse_rating(rating_text),
            "ratingsCount": review_count,
            "dateAdded": date_added,
            "content": f"Added {date_added}" if date_added else None,
            "priority": priority,
            "comment": comment if comment else None,
            "listId": list_id,
            "listName": list_name,
        })

    return items


# ═══════════════════════════════════════════════════════════════════════════════
# IDENTITY — session check + account identity from HTML
# ═══════════════════════════════════════════════════════════════════════════════


_AMAZON = {"shape": "organization", "url": "https://amazon.com", "name": "Amazon"}
_LOGIN_URL = "https://www.amazon.com/gp/sign-in.html"


def _email_from_amazon_html(body: str) -> str | None:
    """Pull the account email from Amazon HTML — manage page OR step-up.

    Login & Security often redirects to ``/ap/signin`` / ``/ap/mfa`` with a
    possession challenge. That challenge page still pre-fills the claim:

        <input type="hidden" name="email" value="you@example.com" id="ap-claim"/>

    So the email is available without clearing MFA. Prefer that over a
    free-text regex (which picks up marketing addresses).
    """
    if not body:
        return None
    for pat in (
        r'id=["\']ap-claim["\'][^>]*value=["\']([^"\']+@[^"\']+)["\']',
        r'value=["\']([^"\']+@[^"\']+)["\'][^>]*id=["\']ap-claim["\']',
        r'name=["\']email["\'][^>]*value=["\']([^"\']+@[^"\']+)["\']',
        r'data-claim=["\']([^"\']+@[^"\']+)["\']',
        r'auth-text-truncate["\']>\s*([\w.+-]+@[\w.-]+\.[a-z]{2,})\s*<',
    ):
        m = re.search(pat, body, re.I)
        if not m:
            continue
        candidate = m.group(1).strip()
        if re.search(r"amazon\.|example\.|sentry|aws|cloudfront", candidate, re.I):
            continue
        return normalize_email(candidate)
    return None


async def _close_login_window() -> None:
    """Flip the background profile back to its headless daemon after sign-in."""
    await services.call(
        "browser_session",
        verb="login_window",
        params={"close": True},
    )


async def _account_identity() -> dict[str, Any] | None:
    """Read the signed-in Amazon identity from inside the tab, or None.

    Honest gate first: the httpOnly ``at-main`` cookie. Then the homepage
    (customerId / marketplace / Prime / display name). Email comes from
    Login & Security when Amazon serves it, or from the prefilled claim on
    the MFA/signin step-up page — either way we prefer email as the
    account identifier. ``customerId`` stays as ``userId`` / metadata.
    """
    if not await browser_session.session_cookie_present(_TARGET, _SESSION_COOKIE):
        return None

    try:
        body = await _get_html(f"{BASE}/gp/css/homepage.html", what="check_session")
    except RuntimeError:
        return None

    customer_id_m = re.search(r'"customerId"\s*:\s*"([A-Z0-9]+)"', body)
    marketplace = re.search(r"ue_mid\s*=\s*'([^']+)'", body)
    is_prime = bool(re.search(r"isPrimeMember[=:]\s*['\"]?true", body, re.I))
    display_m = re.search(
        r"""\$Nav\.declare\(['"]config\.customerName['"],\s*'([^']+)'\)""",
        body,
    )
    if not display_m:
        display_m = re.search(
            r'class="nav-line-1[^"]*"[^>]*>\s*Hello,\s*([^<]+)',
            body,
            re.I,
        )
    customer_id = customer_id_m.group(1) if customer_id_m else None
    display = display_m.group(1).strip() if display_m else None

    # A live session always exposes customerId (and usually the Hello name).
    # Cookie alone isn't enough — at-main can linger briefly after logout.
    if not customer_id and not display:
        return None

    email: str | None = None
    try:
        manage_resp = _check_tab(
            await _tab_request("GET", f"{BASE}/ax/account/manage"),
            what="check_session_manage",
        )
        # Works on the manage page itself AND on the MFA/signin claim page
        # Amazon serves when it wants a step-up — both carry the email.
        email = _email_from_amazon_html(manage_resp.get("body") or "")
    except RuntimeError:
        pass

    identifier = email or customer_id or display
    if not identifier:
        return None

    metadata: dict[str, Any] = {}
    if customer_id:
        metadata["customerId"] = customer_id
    if marketplace:
        metadata["marketplaceId"] = marketplace.group(1)
    if is_prime:
        metadata["isPrime"] = True

    result: dict[str, Any] = {
        "authenticated": True,
        "at": _AMAZON,
        "identifier": identifier,
    }
    if email:
        result["email"] = email
    if display:
        result["displayName"] = display
    if customer_id:
        result["userId"] = customer_id
    if metadata:
        result["metadata"] = {"amazon": metadata}
    return result


def _needs_auth():
    return browser_session.needs_auth(
        "No live Amazon session in the AgentOS browser profile. Amazon's "
        "login (email + password + frequent OTP/CAPTCHA, fronted by "
        "Lightsaber anti-bot) is best cleared by a human.",
        login_op="amazon.login",
        login_url=_LOGIN_URL,
    )



# Browser-session identity namespace — ops bind @connection("none").
connection("none", domain="amazon.com")

@account.check
@test.skip(reason='destructive or unsupported — migrated from yaml')
@returns("account")
@claims("primary_user")
@connection("none")
@timeout(60)
async def check_session(**params) -> dict[str, Any]:
    """Verify the Amazon session and identify the logged-in account.

    Honest gate: httpOnly ``at-main`` cookie, then homepage identity
    (customerId / Hello name). Email is the preferred ``identifier`` —
    scraped from Login & Security, or from the prefilled ``ap-claim`` on
    Amazon's MFA/signin step-up when that page is gated. ``customerId``
    rides ``userId`` / ``metadata.amazon``.
    """
    identity = await _account_identity()
    if not identity:
        return {"authenticated": False}
    return identity


async def _orders_reachable() -> bool:
    """True iff order history answers without bouncing to sign-in.

    Amazon's soft session (homepage Hello + ``at-main``) is not enough for
    Your Orders — those pages force ``max_auth_age=0`` reauth. ``login`` uses
    this so a soft session still opens the headed window for the step-up.
    """
    try:
        resp = _check_tab(
            await _tab_request(
                "GET",
                f"{BASE}/your-orders/orders?timeFilter=last30",
                headers={"Referer": f"{BASE}/"},
            ),
            what="orders_probe",
        )
    except RuntimeError:
        return False
    url_final = resp.get("url") or ""
    body = resp.get("body") or ""
    if any(tok in url_final for tok in ("/ap/signin", "/ap/mfa", "/ap/cvf")):
        return False
    if _is_login_redirect_body(body):
        return False
    return (resp.get("status") or 0) == 200


@account.login
@returns("account | auth_challenge")
@connection("none")
@timeout(300)
async def login(**params) -> dict[str, Any]:
    """Sign in to Amazon — or report the already-live session.

    Returns the ``account`` when the browser profile already holds a session
    that can read order history. A soft homepage session alone is not enough
    — Amazon step-ups Your Orders — so this opens a headed sign-in window,
    **polls until authenticated AND orders are reachable**, closes the window,
    and returns the registered account. If the human hasn't finished within
    ~4 minutes, returns a ``login_window`` ``auth_challenge`` so the agent can
    keep polling ``check_session`` and close later.
    """
    identity = await _account_identity()
    if identity and await _orders_reachable():
        return identity

    challenge = await browser_session.login_window(_LOGIN_URL, label="Amazon")
    deadline = time.monotonic() + _LOGIN_WAIT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(_LOGIN_POLL_S)
        identity = await _account_identity()
        if identity and await _orders_reachable():
            try:
                await _close_login_window()
            except Exception:
                pass
            return identity

    # Still signing in — leave the window open; agent keeps polling.
    if isinstance(challenge, dict):
        challenge["instructions"] = (
            "Amazon sign-in window is still open. Finish signing in (OTP/"
            "CAPTCHA / any 'confirm it's you' step for Your Orders), then "
            "poll amazon.check_session until authenticated. When done, call "
            "the login_window service with close=true to return the profile "
            "to its headless daemon."
        )
    return challenge


@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(60)
async def logout(**params) -> dict[str, Any]:
    """Sign out of Amazon in the browser profile.

    Drives Amazon's own sign-out same-origin in the www.amazon.com tab. The
    response's Set-Cookie clears the session from the browser profile.
    Idempotent — a dead session is still a clean logout.
    """
    await _eval("""
if (!__onApp) return { ok: true, already: 'logged_out' };
try {
  await fetch('/gp/flex/sign-out.html?path=%2Fgp%2Fyourstore%2Fhome&signIn=1&useRedirectOnSuccess=1&action=sign-out', { credentials: 'include' });
} catch (e) {}
return { ok: true };
""")
    return {
        "status": "logged_out",
        "hint": "Cleared the Amazon session in the browser profile. Re-auth by signing in headed at amazon.com.",
    }



# ═══════════════════════════════════════════════════════════════════════════════
# CLI — for local testing
# ═══════════════════════════════════════════════════════════════════════════════


async def _main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "Usage: amazon.py <command> [args...]\n"
            "Commands:\n"
            "  search_suggestions <query> [department] [marketplace]\n"
            "  search_products <query> [department] [marketplace]\n"
            "  get_product <asin> [marketplace]"
        )

    cmd = sys.argv[1]

    if cmd == "searchSuggestions":
        query = sys.argv[2] if len(sys.argv) > 2 else "wireless headphones"
        dept = sys.argv[3] if len(sys.argv) > 3 else None
        mkt = sys.argv[4] if len(sys.argv) > 4 else None
        result = await search_suggestions(query, department=dept, marketplace=mkt)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "searchProducts":
        query = sys.argv[2] if len(sys.argv) > 2 else "usb c cable"
        dept = sys.argv[3] if len(sys.argv) > 3 else None
        mkt = sys.argv[4] if len(sys.argv) > 4 else None
        result = await search_products(query, department=dept, marketplace=mkt)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "getProduct":
        asin_val = sys.argv[2] if len(sys.argv) > 2 else "B0BQPNMXQV"
        mkt = sys.argv[4] if len(sys.argv) > 4 else None
        result = await get_product(asin_val, marketplace=mkt)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    else:
        raise SystemExit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    asyncio.run(_main())
