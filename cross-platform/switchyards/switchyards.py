"""SwitchYards — book phone booths / club rooms (Skedda) + membership billing.

Public browse (venues, spaces, floorplans, occupancy): in-tab Skedda CSRF
JSON (`browser_session` on the venue `/booking` tab).

Member auth (engine background profile — not OS cookie-store HTTP):
  - First visit: passwordless `POST /venueusers` + `publicRegisterPayload`
  - Returning email with a Skedda userprofile: headed `login_window` to
    `app.skedda.com` (SPA `forceLogin` after `GET /userprofilepings`)
  - Session identity: `venueuser` and/or `web.userId`

Discovery rules (never hardcode in this file):
  - Venue hosts → scrape `www.switchyards.com/rooms` for `*.skedda.com`
  - Club tags / space ids / map ids → `/webs` (`spaceTags`, `assets`, maps)
  - Venue timezone → `venue.timeZoneId` → `place.timezone`
  - Outbound datetimes → `wall_to_utc(skedda_local, venue_tz)` (absolute `…Z`);
    Skedda writes → `utc_to_wall(utc, venue_tz)`
  - User "today" → OS `/etc/localtime` (same as `user_environment.timezone`)
  - `checkInAudits` → `venue.checkInRules.rules[].checkInAudits`
  - Country on register → `venue.cultureTwoLetterCountryCode`
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from agentos import (
    account,
    app_error,
    browser_session,
    client,
    connection,
    credentials,
    provides,
    returns,
    timeout,
    url,
    utc_to_wall,
    wall_to_utc,
)

connection(
    "skedda",
    description="Public Skedda venue JSON — CSRF cookie jar, no member login",
    client="browser",
)

# Member session (venueuser / Skedda login) — browser profile, no vault.
# Ops bind @connection("none"); domain= is the identity namespace.
connection("none", domain="skedda.com")

# Graph `at` — stable id so remember upserts one org, not one per place.
_SY_ORG = {
    "id": "switchyards",
    "shape": "organization",
    "name": "SwitchYards",
    "url": "https://www.switchyards.com/",
}
_AT = _SY_ORG
_ROOMS_DISCOVERY = "https://www.switchyards.com/rooms"
_BOOKING_PATH = "/booking"
_SKEDDA_ACCOUNT_LOGIN = "https://app.skedda.com/account/login"
_SKEDDA_APP_HOST = "app.skedda.com"
# Domains to try for `@provides("login_credentials")` (1Password, etc.).
_CRED_DOMAINS = (
    "app.skedda.com",
    ".skedda.com",
    "skedda.com",
    "switchyards.com",
    "www.switchyards.com",
)

_RE_TOKEN = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"'
)
_RE_SKEDDA = re.compile(
    r"https?://([a-z0-9.-]+\.skedda\.com)[^\"'\s<>]*", re.I
)
# Venue marketing names are "Switchyards Austin" — strip brand in the picker.
_RE_BRAND_PREFIX = re.compile(r"^switchyards?\s+", re.I)


# ──────────────────────────────────────────────────────────────────────
# Timezone helpers (shapes-overview §10b)
# ──────────────────────────────────────────────────────────────────────


def _user_timezone_name() -> str:
    """OS user IANA zone — same source as engine `user_environment.timezone`."""
    try:
        link = os.readlink("/etc/localtime")
        for marker in ("/zoneinfo/",):
            if marker in link:
                return link.split(marker, 1)[1]
    except OSError:
        pass
    local = datetime.now().astimezone().tzinfo
    key = getattr(local, "key", None)
    return key or "UTC"


def _venue_timezone(venue: dict | None) -> str | None:
    if not venue:
        return None
    tz = venue.get("timeZoneId") or venue.get("timeZone") or venue.get("timezone")
    return str(tz) if tz else None


def _day_window_local(
    date_str: str | None,
    days: int,
    venue_tz_name: str,
) -> tuple[str, str]:
    """Venue-local naive ISO bounds for Skedda `bookingslists` (no Z suffix).

    Skedda expects naive local strings (no offset). "Today" without `date`
    uses the OS user timezone calendar date, then that calendar day is
    queried against the venue (whose tz is `venue_tz_name`).
    """
    if not venue_tz_name:
        raise ValueError("venue timezone required for availability window")
    if date_str:
        start_local_date = date.fromisoformat(date_str[:10])
    else:
        user_tz = ZoneInfo(_user_timezone_name())
        start_local_date = datetime.now(user_tz).date()
    start_local = datetime.combine(start_local_date, datetime.min.time())
    end_local = start_local + timedelta(days=max(1, int(days)))
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S")
    return fmt(start_local), fmt(end_local)


# ──────────────────────────────────────────────────────────────────────
# Public Skedda — in-tab fetch (CSRF cookie lives in the browser profile)
# Marketing discovery + static map JSON may use client (no session jar).
# ──────────────────────────────────────────────────────────────────────


def _resp_body(resp: dict) -> str:
    body = resp.get("body")
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return str(body)


def _resp_json(resp: dict) -> Any:
    if resp.get("json") is not None:
        return resp["json"]
    body = _resp_body(resp)
    return json.loads(body) if body else None


_SKEDDA_PRELUDE = """
const __deadline = Date.now() + %(wait_ms)s;
while (!location.hostname.endsWith('skedda.com') || document.readyState === 'loading') {
  if (Date.now() > __deadline) return { __error: 'tab_not_ready' };
  await new Promise(r => setTimeout(r, 200));
}
const __tokenEl = document.querySelector('input[name="__RequestVerificationToken"]');
const __token = __tokenEl ? __tokenEl.value : '';
const __skeddaFetch = async (path, opts = {}) => {
  const headers = Object.assign({
    'Accept': 'application/json',
  }, opts.headers || {});
  if (__token) headers['X-Skedda-RequestVerificationToken'] = __token;
  const r = await fetch(path, Object.assign({
    credentials: 'same-origin',
    cache: 'no-store',
  }, opts, { headers: Object.assign(headers, (opts.headers || {})) }));
  const text = await r.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch (_) {}
  return { status: r.status, ok: r.ok, json, body: text };
};
"""


def _skedda_payload(body: str, *, wait_ms: int = 25000) -> str:
    return "(async () => {" + (_SKEDDA_PRELUDE % {"wait_ms": wait_ms}) + body + "})()"


async def _ensure_booking_tab(host: str) -> None:
    """Land on `/booking` — soft navigate (engine skips reload if already there)."""
    await browser_session.navigate(host, f"https://{host}/booking")


async def _skedda_eval(host: str, body: str, *, wait_ms: int = 25000, timeout_s: int = 60):
    """Run JS on a venue booking tab (opens /booking when needed)."""
    await _ensure_booking_tab(host)
    value = await browser_session.eval(
        host, _skedda_payload(body, wait_ms=wait_ms), timeout=timeout_s
    )
    if isinstance(value, dict) and value.get("__error") == "tab_not_ready":
        await browser_session.navigate(host, f"https://{host}/booking", force=True)
        value = await browser_session.eval(
            host, _skedda_payload(body, wait_ms=wait_ms), timeout=timeout_s
        )
    return value


# Short-lived /webs reuse across list + availability in one refresh.
_WEBS_MEMO: dict[str, tuple[float, dict]] = {}
_WEBS_MEMO_TTL_S = 90.0


def _webs_memo_get(host: str) -> dict | None:
    hit = _WEBS_MEMO.get(host)
    if not hit:
        return None
    ts, webs = hit
    if time.monotonic() - ts > _WEBS_MEMO_TTL_S:
        _WEBS_MEMO.pop(host, None)
        return None
    return webs


def _webs_memo_put(host: str, webs: dict) -> None:
    _WEBS_MEMO[host] = (time.monotonic(), webs)


async def _discover_hosts() -> list[str]:
    """Unique Skedda venue hosts linked from the marketing rooms page."""
    resp = await client.get(_ROOMS_DISCOVERY, client="browser")
    if not resp.get("ok") and resp.get("status", 0) >= 400:
        raise RuntimeError(
            f"Failed to discover SwitchYards venues from {_ROOMS_DISCOVERY}: "
            f"HTTP {resp.get('status')}"
        )
    html = _resp_body(resp)
    hosts = sorted({m.group(1).lower() for m in _RE_SKEDDA.finditer(html)})
    if not hosts:
        raise RuntimeError(
            f"No *.skedda.com hosts found on {_ROOMS_DISCOVERY} — marketing page shape may have changed"
        )
    return hosts


async def _webs_for_host(host: str, *, force: bool = False) -> dict:
    """GET /webs in the venue tab (public; CSRF cookie from the page)."""
    if not force:
        cached = _webs_memo_get(host)
        if cached is not None:
            return cached
    value = await _skedda_eval(
        host,
        """
  const r = await __skeddaFetch('/webs');
  if (!r.ok) return { __error: 'http_' + r.status, detail: r.json || r.body };
  const data = r.json || {};
  if (data.errors) return { __error: 'webs_errors', detail: data.errors };
  data._host = location.hostname;
  data._token = __token;
  return data;
""",
    )
    if isinstance(value, dict) and value.get("__error"):
        raise RuntimeError(f"/webs on {host}: {value.get('detail') or value['__error']}")
    if not isinstance(value, dict):
        raise RuntimeError(f"/webs on {host} returned non-object")
    value["_host"] = host
    _webs_memo_put(host, value)
    return value


def _venue_from_webs(webs: dict) -> dict:
    venue = webs.get("venue")
    if isinstance(venue, list):
        venue = venue[0] if venue else {}
    if not isinstance(venue, dict):
        venue = {}
    return venue


async def _bookings_list(host: str, start: str, end: str) -> list[dict]:
    """Occupied bookings via in-tab `/bookingslists` (token unused; page CSRF)."""
    value = await _skedda_eval(
        host,
        f"""
  const q = new URLSearchParams({{ start: {json.dumps(start)}, end: {json.dumps(end)} }});
  const r = await __skeddaFetch('/bookingslists?' + q);
  if (!r.ok) return {{ __error: 'http_' + r.status, detail: r.json || r.body }};
  const data = r.json || {{}};
  if (data.errors) return {{ __error: 'bookings_errors', detail: data.errors }};
  return {{ bookings: data.bookings || [] }};
""",
    )
    if isinstance(value, dict) and value.get("__error"):
        raise RuntimeError(f"bookingslists on {host}: {value.get('detail') or value['__error']}")
    rows = (value or {}).get("bookings") if isinstance(value, dict) else None
    return list(rows) if isinstance(rows, list) else []


def _map_json_url(webs: dict, map_id: str, last_updated: Any) -> str:
    web = webs.get("web") or {}
    host = web.get("spaceImagesHost") or "staticcontent.skedda.com"
    container = web.get("venueMapsContainer") or "venuemaps"
    base = f"https://{host}/{container}/{map_id}.json"
    # Skedda uses `?{lastUpdated}` as a bare cache-buster (not key=value).
    if last_updated in (None, ""):
        return base
    return f"{base}?{last_updated}"


async def _fetch_map_artwork(artwork_url: str) -> dict:
    # Public CDN — CORS *; no Skedda CSRF cookie required.
    resp = await client.get(artwork_url, client="browser")
    data = _resp_json(resp)
    if not isinstance(data, dict):
        raise RuntimeError(f"Map artwork not JSON: {artwork_url} (HTTP {resp.get('status')})")
    return data


# ──────────────────────────────────────────────────────────────────────
# Entity mappers
# ──────────────────────────────────────────────────────────────────────


def _space_kind(name: str | None) -> str:
    n = (name or "").lower()
    if "phone booth" in n or "phonebooth" in n:
        return "phone_booth"
    if "club room" in n or "meeting" in n:
        return "meeting_room"
    return "meeting_room"


def _strip_club_prefix(name: str, club_label: str | None) -> str:
    """Drop club label Skedda embeds in every space title.

    "East Austin Phone Booth 03" → "Phone Booth 03" when the club is
    already East Austin (picker / location context carries the club).
    """
    raw = (name or "").strip()
    label = (club_label or "").strip()
    if not raw or not label:
        return raw
    if raw.lower().startswith(label.lower()):
        rest = raw[len(label) :].lstrip(" \t-–—:|/")
        return rest or raw
    return raw


def _tag_space_ids(tag: dict) -> list[str]:
    raw = tag.get("spaces") or tag.get("spaceIds") or tag.get("spaceids") or []
    return [str(x) for x in raw]


def _short_metro_name(venue: dict | None, host: str | None) -> str:
    """City label without the redundant SwitchYards brand prefix."""
    if isinstance(venue, dict):
        raw = (venue.get("name") or "").strip()
        stripped = _RE_BRAND_PREFIX.sub("", raw).strip()
        if stripped:
            return stripped
        city = (venue.get("city") or "").strip()
        if city:
            return city
    if host:
        sub = str(host).split("/")[0].split(".")[0].lower()
        sub = re.sub(r"^switchyards?", "", sub)
        if not sub:
            # Bare switchyards.skedda.com is the Atlanta venue.
            return "Atlanta"
        return sub.title()
    return "SwitchYards"


def _map_name_for_spaces(space_ids: list[str], venue: dict) -> str | None:
    """Floorplan map whose hit-targets best cover this club's spaces.

    Map `name` is the human club label (e.g. Hyde Park / East Austin) —
    spaceTags stay cryptic (`HYP` / `EAT`). Discovered at runtime; never
    hardcode tag→label maps.
    """
    idset = {str(s) for s in space_ids}
    if not idset:
        return None
    maps = (venue.get("mapsStructure") or {}).get("maps") or []
    best_name: str | None = None
    best_overlap = 0
    for meta in maps:
        if not isinstance(meta, dict) or meta.get("draft"):
            continue
        rects = meta.get("dynamicRectangles") or []
        mids = {
            str(r.get("spaceId"))
            for r in rects
            if isinstance(r, dict) and r.get("spaceId") is not None
        }
        overlap = len(mids & idset)
        if overlap > best_overlap:
            best_overlap = overlap
            best_name = (meta.get("name") or "").strip() or None
    return best_name if best_overlap else None


def _club_places(webs: dict) -> list[dict]:
    """One `place` per server spaceTag (club within a venue)."""
    venue = _venue_from_webs(webs)
    host = webs.get("_host")
    tz = _venue_timezone(venue)
    tags = (venue.get("spacePresentation") or {}).get("spaceTags") or []
    venue_id = venue.get("id")
    metro = _short_metro_name(venue, host)
    out = []
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        tag_name = tag.get("name") or tag.get("id") or tag.get("label")
        if not tag_name:
            continue
        space_ids = _tag_space_ids(tag)
        # Prefer floorplan map name over cryptic tag (HYP → Hyde Park).
        label = _map_name_for_spaces(space_ids, venue) or str(tag_name)
        pid = f"sy-club:{venue_id}:{tag_name}"
        place = {
            "id": pid,
            "at": _AT,
            "name": label,
            "featureType": "poi",
            "categories": ["coworking_club"],
            "timezone": tz,
            "content": f"{len(space_ids)} spaces" if space_ids else None,
            "_venueId": venue_id,
            "_host": host,
            "_tag": str(tag_name),
            "_spaceIds": space_ids,
        }
        # Venue mailing address when present — club-specific street may differ.
        if venue.get("streetAddress"):
            place["street"] = venue.get("streetAddress")
        if venue.get("city"):
            place["city"] = venue.get("city")
        place["placeFormatted"] = f"{label} · {metro}" if metro else label
        out.append(place)
    return out


def _venue_place(webs: dict) -> dict:
    venue = _venue_from_webs(webs)
    host = webs.get("_host")
    tz = _venue_timezone(venue)
    vid = venue.get("id")
    return {
        "id": f"sy-venue:{vid}",
        "at": _AT,
        # Picker label — brand is the plugin tab; keep full name in placeFormatted.
        "name": _short_metro_name(venue, host),
        "placeFormatted": (venue.get("name") or _short_metro_name(venue, host)),
        "featureType": "poi",
        "categories": ["coworking"],
        "timezone": tz,
        "street": venue.get("streetAddress"),
        "city": venue.get("city"),
        "region": venue.get("region") or venue.get("state"),
        "postalCode": venue.get("postCode") or venue.get("postalCode"),
        "countryCode": venue.get("cultureTwoLetterCountryCode") or venue.get("countryCode"),
        "website": f"https://{host}/booking" if host else None,
        "_venueId": vid,
        "_host": host,
        "_subdomain": venue.get("subdomain"),
    }


def _venue_stub_from_host(host: str) -> dict:
    """City row when `/webs` failed — keeps the metro in the picker."""
    label = _short_metro_name(None, host)
    slug = re.sub(r"^switchyards?", "", host.split(".")[0].lower()) or "atlanta"
    # Prefer Eastern as a harmless default for stubs; live `/webs` replaces
    # this row with the real venue id + tz on the next successful probe.
    return {
        "id": f"sy-venue:pending:{slug}",
        "at": _AT,
        "name": label,
        "placeFormatted": f"Switchyards {label}",
        "featureType": "poi",
        "categories": ["coworking"],
        "timezone": "America/New_York",
        "website": f"https://{host}/booking",
        "_host": host,
    }


def _space_place(asset: dict, webs: dict, club_by_space: dict[str, dict]) -> dict:
    venue = _venue_from_webs(webs)
    host = webs.get("_host")
    tz = _venue_timezone(venue)
    aid = str(asset.get("id"))
    club = club_by_space.get(aid)
    raw_name = str(asset.get("name") or f"Space {aid}")
    name = _strip_club_prefix(raw_name, club.get("name") if club else None)
    kind = _space_kind(raw_name)  # kind from full Skedda title before strip
    out = {
        "id": aid,
        "at": _AT,
        "name": name,
        "featureType": kind,
        "categories": [kind],
        "timezone": tz,
        "_venueId": venue.get("id"),
        "_host": host,
        "content": asset.get("info") or None,
    }
    if club:
        out["location"] = {
            "at": _AT,
            "id": club["id"],
            "shape": "place",
            "name": club.get("name"),
        }
        out["_tag"] = club.get("_tag")
    return out


def _club_by_space_index(clubs: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for club in clubs:
        for sid in club.get("_spaceIds") or []:
            idx[str(sid)] = club
    return idx


def _assemble_svg(view_box: str | None, static_defs: str, static_graphics: str) -> str:
    vb = view_box or "0 0 1000 1000"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}">'
        f"{static_defs or ''}{static_graphics or ''}</svg>"
    )


def _floorplan_entity(meta: dict, webs: dict, artwork: dict) -> dict:
    venue = _venue_from_webs(webs)
    tz = _venue_timezone(venue)
    mid = meta.get("id")
    source = _map_json_url(webs, mid, meta.get("lastUpdated"))
    resources = []
    for rect in meta.get("dynamicRectangles") or []:
        if not isinstance(rect, dict):
            continue
        sid = rect.get("spaceId")
        if sid is None:
            continue
        resources.append(
            {
                "placeId": str(sid),
                "x": rect.get("x"),
                "y": rect.get("y"),
                "w": rect.get("w"),
                "h": rect.get("h"),
                "indicatorPos": rect.get("indicatorPos"),
            }
        )
    return {
        "id": str(mid),
        "at": _AT,
        "name": meta.get("name") or str(mid),
        "venueName": venue.get("name"),
        "viewBox": meta.get("viewBox"),
        "layoutSvg": _assemble_svg(
            meta.get("viewBox"),
            artwork.get("staticDefs") or "",
            artwork.get("staticGraphics") or "",
        ),
        "sourceUrl": source,
        "resources": resources,
        "mobileRotationDegrees": meta.get("mobileRotationDegrees"),
        "indicatorSize": meta.get("indicatorSize"),
        "timezone": tz,
        "_venueId": venue.get("id"),
        "_host": webs.get("_host"),
    }


def _reservation_from_booking(row: dict, webs: dict, spaces_by_id: dict[str, dict]) -> dict:
    venue = _venue_from_webs(webs)
    tz = _venue_timezone(venue)
    rid = str(row.get("id"))
    space_ids = [str(s) for s in (row.get("spaces") or [])]
    space = spaces_by_id.get(space_ids[0]) if space_ids else None
    kind = (space or {}).get("featureType") or "meeting_room"
    status = "confirmed"
    if row.get("pendingDeletion") or row.get("isDeleted"):
        status = "cancelled"
    name = row.get("title") or (space or {}).get("name") or f"Reservation {rid}"
    actions = _reservation_actions({**row, "status": status}, venue)
    start_utc = wall_to_utc(row.get("start"), tz)
    end_utc = wall_to_utc(row.get("end"), tz)
    out = {
        "id": f"sy-res:{rid}",
        "at": _AT,
        "reservationType": kind,
        "reservationId": rid,
        "status": status,
        "bookingType": "instant",
        "name": name,
        "startTime": start_utc,
        "endTime": end_utc,
        "startDate": start_utc,
        "endDate": end_utc,
        "timezone": tz,
        "availableActions": actions,
        "partySize": 1,
        "_venueId": venue.get("id"),
        "_host": webs.get("_host"),
        "_spaceIds": space_ids,
        "_venueuser": row.get("venueuser"),
    }
    if space:
        out["location"] = {
            "at": _AT,
            "id": space["id"],
            "shape": "place",
            "name": space.get("name"),
        }
    return out


# ──────────────────────────────────────────────────────────────────────
# Venue cache helpers
# ──────────────────────────────────────────────────────────────────────

# Regeneratable catalog on the plugin node (`cache.locations`). Cleared via
# Commons → Details → Clear cache. TTL forces a quiet re-probe; Clear bypasses.
_LOCATIONS_TTL_S = 24 * 60 * 60


def _filter_hosts(
    hosts: list[str],
    *,
    venue: str | None = None,
    host: str | None = None,
) -> list[str]:
    """Narrow discovered hosts before opening tabs (host name / venue substring)."""
    if host:
        host_l = host.lower().replace("https://", "").split("/")[0]
        return [h for h in hosts if h == host_l]
    if venue:
        q = venue.lower().replace(" ", "")
        matched = [h for h in hosts if q in h.replace(".", "").replace("-", "")]
        if matched:
            return matched
        # Fallback: substring on raw host (e.g. "austin" in switchyardsaustin…)
        matched = [h for h in hosts if venue.lower() in h]
        if matched:
            return matched
    return hosts


def _filter_places(
    places: list[dict],
    *,
    venue: str | None = None,
    host: str | None = None,
) -> list[dict]:
    """Narrow a cached/live place list by host or venue substring."""
    if host:
        host_l = host.lower().replace("https://", "").split("/")[0]
        return [
            p
            for p in places
            if str(p.get("_host") or "").lower() == host_l
            or host_l in str(p.get("website") or "").lower()
        ]
    if venue:
        q = venue.lower()
        out = []
        for p in places:
            blob = " ".join(
                str(p.get(k) or "")
                for k in ("name", "city", "placeFormatted", "_host", "_tag", "id")
            ).lower()
            if q in blob or q.replace(" ", "") in blob.replace(" ", "").replace(".", ""):
                out.append(p)
        return out
    return places


def _locations_cache_entry(cache: dict | None) -> dict | None:
    if not isinstance(cache, dict):
        return None
    loc = cache.get("locations")
    return loc if isinstance(loc, dict) else None


def _locations_cache_fresh(cache: dict | None) -> bool:
    loc = _locations_cache_entry(cache)
    if not loc:
        return False
    places = loc.get("places")
    if not isinstance(places, list) or not places:
        return False
    ts = loc.get("fetchedAt")
    if not isinstance(ts, str) or not ts:
        return False
    try:
        fetched = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)
    except ValueError:
        return False
    return age.total_seconds() < _LOCATIONS_TTL_S


def _places_from_webs(hosts: list[str], webs_list: list[dict]) -> list[dict]:
    """Build venue + club places; stub metros whose /webs probe failed."""
    loaded = {
        str(w.get("_host") or "").lower()
        for w in webs_list
        if w.get("_host")
    }
    places: list[dict] = []
    for webs in webs_list:
        places.append(_venue_place(webs))
        places.extend(_club_places(webs))
    for h in hosts:
        if h.lower() not in loaded:
            places.append(_venue_stub_from_host(h))
    by_id: dict[str, dict] = {}
    for p in places:
        pid = str(p.get("id") or "")
        if pid and pid not in by_id:
            by_id[pid] = p
    return list(by_id.values())


async def _load_webs(
    *,
    venue: str | None = None,
    host: str | None = None,
    location_id: str | None = None,
    cache: dict | None = None,
) -> list[dict]:
    """Load `/webs` for a *scoped* set of hosts — never every city at once.

    Opening each venue's `/booking` tab is headed + rate-limit sensitive.
    Callers must pass `host`, `venue`, or `location_id` so we only touch
    the venues the user actually selected.
    """
    if not host and not venue and not location_id:
        raise RuntimeError(
            "SwitchYards refuses to open every /booking tab at once — "
            "pass host, venue, or location_id for the city/club in scope."
        )
    loc = _locations_cache_entry(cache) or {}
    cached_hosts = [str(h) for h in (loc.get("hosts") or []) if h]
    all_hosts = cached_hosts if cached_hosts else await _discover_hosts()
    hosts = _filter_hosts(all_hosts, venue=venue, host=host)
    if location_id and not host:
        resolved = _host_for_location_id(str(location_id), cache)
        if resolved:
            hosts = _filter_hosts(hosts or all_hosts, host=resolved)
        else:
            # location_id may be a club/venue id only resolvable after /webs —
            # narrow via venue substring heuristics on the id slug.
            lid = str(location_id)
            if lid.startswith("sy-venue:pending:"):
                slug = lid.removeprefix("sy-venue:pending:")
                hosts = [h for h in all_hosts if slug in h.replace(".", "").replace("-", "")]
            elif lid.startswith("sy-host:"):
                hosts = _filter_hosts(all_hosts, host=lid.removeprefix("sy-host:"))
            elif not hosts:
                # Club id — try host from places cache; else leave hosts as filtered.
                hosts = _filter_hosts(all_hosts, venue=venue, host=host)
    if not hosts:
        raise RuntimeError(
            f"No SwitchYards host matched venue={venue!r} host={host!r} "
            f"location_id={location_id!r}"
        )
    out = []
    errors = []
    for h in hosts:
        try:
            out.append(await _webs_for_host(h))
        except Exception as e:
            errors.append(f"{h}: {e}")
    if not out:
        raise RuntimeError("No SwitchYards venues loaded. " + "; ".join(errors[:5]))
    return out


def _host_for_location_id(location_id: str, cache: dict | None) -> str | None:
    """Resolve a place id to a Skedda host from the locations cache when possible."""
    loc = _locations_cache_entry(cache) or {}
    for p in loc.get("places") or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("id")) == location_id:
            h = p.get("_host")
            return str(h) if h else None
    if location_id.startswith("sy-host:"):
        return location_id.removeprefix("sy-host:")
    if location_id.startswith("sy-venue:pending:"):
        slug = location_id.removeprefix("sy-venue:pending:")
        for h in loc.get("hosts") or []:
            if slug in str(h).replace(".", "").replace("-", ""):
                return str(h)
    return None


def _stub_places_for_hosts(hosts: list[str]) -> list[dict]:
    """City rows from hostnames alone — no /booking tabs."""
    by_id: dict[str, dict] = {}
    for h in hosts:
        p = _venue_stub_from_host(h)
        pid = str(p.get("id") or "")
        if pid and pid not in by_id:
            by_id[pid] = p
    return list(by_id.values())


def _merge_location_places(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Prefer live /webs rows over pending stubs for the same metro."""
    by_id: dict[str, dict] = {str(p.get("id")): p for p in existing if p.get("id")}
    # Drop pending stubs whose host was just enriched with a real venue.
    fresh_hosts = {
        str(p.get("_host") or "").lower()
        for p in fresh
        if p.get("_host") and not str(p.get("id") or "").startswith("sy-venue:pending:")
    }
    if fresh_hosts:
        by_id = {
            k: v
            for k, v in by_id.items()
            if not (
                str(k).startswith("sy-venue:pending:")
                and str(v.get("_host") or "").lower() in fresh_hosts
            )
        }
    for p in fresh:
        pid = str(p.get("id") or "")
        if pid:
            by_id[pid] = p
    return list(by_id.values())


@returns("place[]")
@provides("reservation_locations")
@connection("skedda")
@timeout(120)
async def list_locations(
    *,
    venue: str | None = None,
    host: str | None = None,
    location_id: str | None = None,
    refresh: bool = False,
    cache: dict | None = None,
    **params,
):
    """List SwitchYards cities (and clubs when a city is in scope).

    Brokered as `reservation_locations`. Discovers venue hosts from
    www.switchyards.com/rooms — never a hardcoded city table.

    **Does not open every city's `/booking` tab.** Unscoped calls return
    metro stubs (from hostnames) plus any clubs already in plugin cache.
    Pass `host` / `venue` / `location_id` to enrich **one** city via `/webs`
    (clubs, real venue id, timezone). Pass `refresh=true` to re-scrape the
    marketing host list (still no bulk /webs).
    """
    cache = cache if isinstance(cache, dict) else {}
    loc = _locations_cache_entry(cache) or {}
    cached_hosts = [str(h) for h in (loc.get("hosts") or []) if h]
    cached_places = [p for p in (loc.get("places") or []) if isinstance(p, dict)]

    need_host_discovery = refresh or not cached_hosts
    if need_host_discovery:
        hosts = await _discover_hosts()
    else:
        hosts = cached_hosts

    scoped = bool(host or venue or location_id)
    write_cache = need_host_discovery

    if not scoped:
        # City picker only — stubs + whatever clubs a prior scoped call cached.
        if not cached_places or need_host_discovery:
            places = _stub_places_for_hosts(hosts)
            # Keep any previously enriched clubs/venues whose host still exists.
            if cached_places and not need_host_discovery:
                places = _merge_location_places(places, cached_places)
            elif cached_places and need_host_discovery:
                places = _merge_location_places(_stub_places_for_hosts(hosts), [
                    p for p in cached_places
                    if not str(p.get("id") or "").startswith("sy-venue:pending:")
                ])
            write_cache = True
        else:
            places = list(cached_places)
        result = _filter_places(places, venue=venue, host=host)
        if not write_cache:
            return result
        return {
            "__cache__": {
                "locations": {
                    "hosts": hosts,
                    "places": places,
                    "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            },
            "__result__": result,
        }

    # Scoped enrich — at most the matching host(s), usually one city.
    target_hosts = _filter_hosts(hosts, venue=venue, host=host)
    if location_id and not host:
        resolved = _host_for_location_id(str(location_id), cache)
        if resolved:
            target_hosts = _filter_hosts(hosts, host=resolved)
        elif str(location_id).startswith("sy-venue:pending:"):
            slug = str(location_id).removeprefix("sy-venue:pending:")
            target_hosts = [
                h for h in hosts if slug in h.replace(".", "").replace("-", "")
            ]
    if not target_hosts:
        # Fall back to stubs so the picker still works.
        return _filter_places(
            cached_places or _stub_places_for_hosts(hosts),
            venue=venue,
            host=host,
        )

    webs_list: list[dict] = []
    errors: list[str] = []
    for h in target_hosts:
        try:
            webs_list.append(await _webs_for_host(h))
        except Exception as e:
            errors.append(f"{h}: {e}")
    if not webs_list:
        # Keep stubs for those hosts rather than failing the whole picker.
        stubs = [_venue_stub_from_host(h) for h in target_hosts]
        places = _merge_location_places(cached_places or _stub_places_for_hosts(hosts), stubs)
        return {
            "__cache__": {
                "locations": {
                    "hosts": hosts,
                    "places": places,
                    "fetchedAt": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            },
            "__result__": _filter_places(places, venue=venue, host=host),
        }

    fresh = _places_from_webs([str(w.get("_host")) for w in webs_list if w.get("_host")], webs_list)
    base = cached_places or _stub_places_for_hosts(hosts)
    places = _merge_location_places(base, fresh)
    return {
        "__cache__": {
            "locations": {
                "hosts": hosts,
                "places": places,
                "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        },
        "__result__": _filter_places(places, venue=venue, host=host),
    }


def _match_webs(
    all_webs: list[dict],
    *,
    venue: str | None = None,
    host: str | None = None,
    location_id: str | None = None,
) -> list[dict]:
    """Filter already-loaded venues (club location_id needs webs in hand)."""
    if host:
        host_l = host.lower().replace("https://", "").split("/")[0]
        return [w for w in all_webs if (w.get("_host") or "").lower() == host_l]
    if location_id:
        lid = str(location_id)
        if lid.startswith("sy-host:") or lid.startswith("sy-venue:pending:"):
            host_l = (
                lid.removeprefix("sy-host:")
                if lid.startswith("sy-host:")
                else None
            )
            slug = (
                lid.removeprefix("sy-venue:pending:")
                if lid.startswith("sy-venue:pending:")
                else None
            )
            matched = []
            for w in all_webs:
                wh = (w.get("_host") or "").lower()
                if host_l and wh == host_l.lower():
                    matched.append(w)
                    continue
                if slug and slug in wh.replace(".", "").replace("-", ""):
                    matched.append(w)
            return matched
        matched = []
        for w in all_webs:
            v = _venue_from_webs(w)
            if str(v.get("id")) == lid or f"sy-venue:{v.get('id')}" == lid:
                matched.append(w)
                continue
            for club in _club_places(w):
                if club["id"] == lid or club.get("_tag") == lid:
                    matched.append(w)
                    break
        return matched
    if venue:
        q = venue.lower()
        matched = []
        for w in all_webs:
            v = _venue_from_webs(w)
            blob = " ".join(
                str(x or "")
                for x in (
                    w.get("_host"),
                    v.get("name"),
                    v.get("subdomain"),
                    v.get("city"),
                )
            ).lower()
            if q in blob:
                matched.append(w)
        return matched
    return all_webs


# ──────────────────────────────────────────────────────────────────────
# Browser session (authed ops)
# ──────────────────────────────────────────────────────────────────────


async def _eval_on(host: str, body: str, *, wait_ms: int = 20000, timeout_s: int = 45):
    """In-tab eval with NeedsAuth / NotReady mapping for account tools."""
    value = await _skedda_eval(host, body, wait_ms=wait_ms, timeout_s=timeout_s)
    if isinstance(value, dict) and "__error" in value:
        code = value["__error"]
        if code == "auth_required":
            return app_error(
                "SwitchYards / Skedda is signed out. Run switchyards.login and "
                "complete sign-in in the browser window, then retry.",
                code="NeedsAuth",
            )
        if code == "tab_not_ready":
            return app_error(
                f"Skedda tab for {host} never became ready.",
                code="NotReady",
            )
        if (
            code in ("no_venueuser", "bookings_empty", "venueusers_empty", "auth_required")
            or str(code).startswith("http_")
            or str(code).startswith("bookings_")
            or str(code).startswith("venueusers_")
            or str(code).startswith("cancel_")
            or str(code).startswith("checkin_")
            or str(code).startswith("update_")
        ):
            return value
        return app_error(f"SwitchYards payload error: {code}", code="PayloadError")
    return value


_SESSION_JS = """
  const r = await __skeddaFetch('/webs');
  if (!r.ok) return { __error: 'http_' + r.status };
  const j = r.json || {};
  const web = j.web || {};
  const ua = (j.useraccount || [])[0] || null;
  const vu = (j.venueusers || [])[0] || null;
  const userId = web.userId || (ua && (ua.id || ua.userId)) || null;
  // Member book path: venueuser is enough (passwordless). Full Skedda
  // useraccount / userId is optional and often absent after self-register.
  if (!vu && !userId) return { authenticated: false };
  const email =
    (vu && vu.username) ||
    (ua && (ua.email || ua.username || ua.userName || ua.login)) ||
    null;
  const display =
    (vu && [vu.firstName, vu.lastName].filter(Boolean).join(' ')) ||
    (ua && (ua.name || ua.displayName)) ||
    null;
  return {
    authenticated: true,
    at: { shape: 'organization', name: 'SwitchYards', url: 'https://www.switchyards.com/' },
    platform: 'membership',
    identifier: email || (vu && String(vu.id)) || String(userId),
    email: email || null,
    userId: userId != null ? String(userId) : null,
    venueuserId: vu ? String(vu.id) : null,
    displayName: display || null,
  };
"""


# ──────────────────────────────────────────────────────────────────────
# Account trio
# ──────────────────────────────────────────────────────────────────────


async def _hosts_for_session(
    *, venue: str | None = None, host: str | None = None
) -> list[str]:
    """Discovered Skedda hosts, optionally filtered — no preferred city."""
    return _filter_hosts(await _discover_hosts(), venue=venue, host=host)


@account.check
@returns("account")
@connection("none")
@timeout(90)
async def check_session(
    *, venue: str | None = None, host: str | None = None, **params
):
    """Probe in-tab `/webs` for venueuser (passwordless) or Skedda userId."""
    at = dict(_SY_ORG)
    hosts = await _hosts_for_session(venue=venue, host=host)
    if not hosts:
        return {"authenticated": False, "at": at}
    last: dict | None = None
    for h in hosts:
        await browser_session.navigate(h, f"https://{h}{_BOOKING_PATH}")
        value = await _eval_on(h, _SESSION_JS, wait_ms=25000, timeout_s=45)
        if isinstance(value, dict) and value.get("code") == "NeedsAuth":
            last = {"authenticated": False, "at": at}
            continue
        if isinstance(value, dict) and value.get("authenticated"):
            return value
        if isinstance(value, dict):
            value.setdefault("at", at)
            last = value
    return last or {"authenticated": False, "at": at}


async def _resolve_skedda_password_login(
    *, email: str = "", password: str = ""
) -> tuple[str, str] | dict[str, Any] | None:
    """email/password from args or `credentials.retrieve` (1Password, etc.).

    Returns ``(email, password)``, an unlock-challenge dict, or ``None``.
    """
    email = (email or "").strip()
    password = password or ""
    if email and password:
        return email, password
    for domain in _CRED_DOMAINS:
        creds = await credentials.retrieve(
            domain=domain,
            account=email or None,
            required=["email", "password"],
        )
        if creds and creds.get("unlock_required"):
            return {
                "unlock_required": True,
                "challengeId": creds.get("challengeId"),
                "prompt": creds.get("prompt") or "secret_challenge",
                "forApp": creds.get("forApp"),
                "code": creds.get("code") or "OnePasswordUnlockRequired",
                "error": creds.get("error")
                or "Unlock 1Password in AgentOS Security, then retry login.",
                "hint": creds.get("hint")
                or (
                    "Enter your Master Password in the AgentOS Security window, "
                    "then retry switchyards.login."
                ),
                "domain": domain,
            }
        if not (creds and creds.get("found")):
            continue
        val = creds.get("value") or {}
        email = email or str(val.get("email") or "").strip()
        password = password or str(val.get("password") or "")
        if email and password:
            return email, password
    return None


@account.login
@returns("account | auth_challenge")
@connection("none")
@timeout(120)
async def login(
    *,
    venue: str | None = None,
    host: str | None = None,
    email: str = "",
    password: str = "",
    **params,
):
    """Sign in on the background profile.

    Order:
      1. Already have a venueuser / userId session → return it
      2. `email`+`password` args, or `credentials.retrieve` (1Password item
         for app.skedda.com / SwitchYards) → `POST /logins` on app.skedda.com
      3. Else headed `/booking` window (passwordless email gate, or manual
         password if Skedda redirects)
    """
    session = await check_session(venue=venue, host=host, **params)
    # Skedda userId alone is not enough for member bookings — we need a
    # club `venueuser` on the venue host. Re-land on /booking when missing.
    if (
        isinstance(session, dict)
        and session.get("authenticated")
        and session.get("venueuserId")
    ):
        return session
    hosts = await _hosts_for_session(venue=venue, host=host)
    if not hosts:
        return app_error("No SwitchYards Skedda hosts discovered.", code="NotReady")
    booking_url = f"https://{hosts[0]}{_BOOKING_PATH}"
    if isinstance(session, dict) and session.get("authenticated"):
        await browser_session.navigate(hosts[0], booking_url)
        session = await check_session(venue=venue, host=host or hosts[0], **params)
        if (
            isinstance(session, dict)
            and session.get("authenticated")
            and session.get("venueuserId")
        ):
            return session

    resolved = await _resolve_skedda_password_login(email=email, password=password)
    if isinstance(resolved, dict) and resolved.get("unlock_required"):
        return app_error(
            resolved.get("error")
            or "Unlock 1Password in AgentOS Security, then retry login.",
            code=resolved.get("code") or "OnePasswordUnlockRequired",
            challengeId=resolved.get("challengeId"),
            forApp=resolved.get("forApp"),
            prompt=resolved.get("prompt") or "secret_challenge",
            hint=resolved.get("hint"),
            domain=resolved.get("domain"),
        )
    if resolved:
        user, pwd = resolved
        login_url = url.build(
            _SKEDDA_ACCOUNT_LOGIN,
            params={"returnUrl": booking_url, "username": user},
        )
        await browser_session.navigate(_SKEDDA_APP_HOST, login_url)
        # Password stays in-tab only — never returned to the agent transcript.
        payload = await browser_session.eval(
            _SKEDDA_APP_HOST,
            _skedda_payload(
                f"""
  const username = {json.dumps(user)};
  const password = {json.dumps(pwd)};
  const redirectUrl = {json.dumps(booking_url)};
  const body = {{
    login: {{
      username,
      password,
      rememberMe: true,
      redirectUrl,
    }},
  }};
  const r = await __skeddaFetch('/logins', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(body),
  }});
  if (!r.ok) {{
    return {{
      __error: 'login_http_' + r.status,
      detail: (r.json && r.json.errors) || r.body,
    }};
  }}
  const j = r.json || {{}};
  if (j.errors) return {{ __error: 'login_errors', detail: j.errors }};
  return {{
    ok: true,
    redirectUrl: (j.login && j.login.redirectUrl) || redirectUrl,
    userId: j.web && j.web.userId,
  }};
""",
                wait_ms=25000,
            ),
            timeout=60,
        )
        if isinstance(payload, dict) and payload.get("__error"):
            return app_error(
                "Skedda password login failed. Check the 1Password item "
                f"for app.skedda.com. ({payload.get('__error')})",
                code="NeedsAuth",
            )
        # Land on venue booking so venueuser/cookies bind to the club host.
        await browser_session.navigate(hosts[0], booking_url)
        session = await check_session(venue=venue, host=host or hosts[0], **params)
        if (
            isinstance(session, dict)
            and session.get("authenticated")
            and session.get("venueuserId")
        ):
            return session
        # Password login alone can leave userId without a club venueuser —
        # fall through to headed booking so the member gate can mint one.

    return await browser_session.login_window(
        booking_url,
        label="SwitchYards",
        instructions=(
            "No Skedda password found in 1Password/Keychain. Either add a "
            "Login for app.skedda.com, or on this booking page enter the "
            "member email (first-time passwordless) / use Log in if Skedda "
            f"asks for a password. Target: {booking_url}"
        ),
    )


@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(45)
async def logout(*, venue: str | None = None, host: str | None = None, **params):
    """Clear Skedda session in the background profile (full-account logout URL)."""
    hosts = await _hosts_for_session(venue=venue, host=host)
    if hosts:
        await browser_session.navigate(hosts[0], "https://app.skedda.com/account/logout")
    return {
        "status": "logged_out",
        "hint": (
            "Navigated to Skedda logout. Venueuser cookies on venue hosts "
            "may remain until the profile clears them; pass email/name on "
            "create_reservation to re-register if needed."
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# Public tools
# ──────────────────────────────────────────────────────────────────────


@returns("place[]")
@connection("skedda")
@timeout(120)
async def list_spaces(
    *,
    venue: str | None = None,
    host: str | None = None,
    location_id: str | None = None,
    cache: dict | None = None,
    **params,
):
    """List bookable spaces (phone booths / club rooms) as `place[]`."""
    loaded = await _load_webs(
        venue=venue, host=host, location_id=location_id, cache=cache
    )
    webs_list = _match_webs(
        loaded, venue=venue, host=host, location_id=location_id
    )
    out: list[dict] = []
    for webs in webs_list:
        clubs = _club_places(webs)
        # If location_id is a club, restrict assets to that tag's spaces.
        club_filter = None
        if location_id:
            for c in clubs:
                if c["id"] == location_id or c.get("_tag") == location_id:
                    club_filter = set(c.get("_spaceIds") or [])
                    break
        idx = _club_by_space_index(clubs)
        for asset in webs.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            aid = str(asset.get("id"))
            if club_filter is not None and aid not in club_filter:
                continue
            out.append(_space_place(asset, webs, idx))
    return out


@returns("floorplan[]")
@connection("skedda")
@timeout(180)
async def list_floorplans(
    *,
    venue: str | None = None,
    host: str | None = None,
    location_id: str | None = None,
    cache: dict | None = None,
    **params,
):
    """List venue floorplans (SVG + space hit rects) discovered from `/webs`."""
    loaded = await _load_webs(
        venue=venue, host=host, location_id=location_id, cache=cache
    )
    webs_list = _match_webs(
        loaded, venue=venue, host=host, location_id=location_id
    )
    out: list[dict] = []
    for webs in webs_list:
        venue_obj = _venue_from_webs(webs)
        maps = (venue_obj.get("mapsStructure") or {}).get("maps") or []
        for meta in maps:
            if not isinstance(meta, dict) or meta.get("draft"):
                continue
            mid = meta.get("id")
            if not mid:
                continue
            url = _map_json_url(webs, mid, meta.get("lastUpdated"))
            try:
                artwork = await _fetch_map_artwork(url)
            except Exception as e:
                return app_error(
                    f"Failed to fetch floorplan artwork {url}: {e}",
                    code="ProviderError",
                )
            out.append(_floorplan_entity(meta, webs, artwork))
    return out


def _parse_local_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value)[:19])


def _fmt_local_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _skedda_local(value: str, tz_name: str | None) -> str:
    """UTC/aware → Skedda venue-local naive (raises on empty)."""
    out = utc_to_wall(value, tz_name)
    if not out:
        raise ValueError("empty datetime")
    return out


def _free_slot_events(
    *,
    spaces: dict[str, dict],
    occupied_rows: list[dict],
    window_start: str,
    window_end: str,
    timezone_name: str,
    space_id: str | None = None,
    slot_minutes: int = 30,
    day_start_hour: int = 8,
    day_end_hour: int = 21,
    max_slots: int = 120,
) -> list[dict]:
    """Invert occupied bookings into bookable `event` rows for the Commons app."""
    win_start = _parse_local_dt(window_start)
    win_end = _parse_local_dt(window_end)
    by_space: dict[str, list[tuple[datetime, datetime]]] = {
        sid: [] for sid in spaces
    }
    for row in occupied_rows:
        if not isinstance(row, dict):
            continue
        try:
            rs = _parse_local_dt(row["start"])
            re_ = _parse_local_dt(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        for sid in (str(s) for s in (row.get("spaces") or [])):
            if sid in by_space:
                by_space[sid].append((rs, re_))
    out: list[dict] = []
    step = timedelta(minutes=max(15, int(slot_minutes)))
    for sid, place in spaces.items():
        if space_id and sid != str(space_id):
            continue
        busy = sorted(by_space.get(sid) or [], key=lambda x: x[0])
        day = win_start.date()
        end_day = (win_end - timedelta(seconds=1)).date()
        while day <= end_day and len(out) < max_slots:
            cursor = datetime.combine(day, datetime.min.time()).replace(
                hour=day_start_hour
            )
            day_end = datetime.combine(day, datetime.min.time()).replace(
                hour=day_end_hour
            )
            if cursor < win_start:
                cursor = win_start
            if day_end > win_end:
                day_end = win_end
            while cursor + step <= day_end and len(out) < max_slots:
                slot_end = cursor + step
                overlap = any(not (slot_end <= b0 or cursor >= b1) for b0, b1 in busy)
                if not overlap:
                    # Slot id keeps venue-local wall (Skedda round-trip);
                    # startDate/endDate are UTC instants for the calendar.
                    local_start = _fmt_local_dt(cursor)
                    local_end = _fmt_local_dt(slot_end)
                    start_s = wall_to_utc(local_start, timezone_name)
                    end_s = wall_to_utc(local_end, timezone_name)
                    out.append(
                        {
                            # Slash-separated so ISO times (with `:`) stay unambiguous.
                            "id": f"sy-slot:{sid}/{local_start}/{local_end}",
                            "shape": "event",
                            "at": _AT,
                            "name": place.get("name") or f"Space {sid}",
                            "startDate": start_s,
                            "endDate": end_s,
                            "startTime": start_s,
                            "endTime": end_s,
                            "timezone": timezone_name,
                            "location": {
                                "id": place.get("id") or sid,
                                "name": place.get("name"),
                                "shape": "place",
                                "at": _AT,
                            },
                            "space_id": sid,
                            "isFull": False,
                            "availableActions": ["book"],
                        }
                    )
                cursor = slot_end
            day += timedelta(days=1)
    return out


@returns("event[]")
@provides("reservation_availability")
@connection("skedda")
@timeout(120)
async def get_availability(
    *,
    venue: str | None = None,
    host: str | None = None,
    location_id: str | None = None,
    space_id: str | None = None,
    date: str | None = None,
    days: int = 1,
    cache: dict | None = None,
    **params,
):
    """Free bookable slots as `event[]` (brokered `reservation_availability`).

    Built by inverting public `bookingslists` occupancy. Times are
    venue-local. Default `date` is the user's today (OS timezone); the
    query window uses each venue's `timeZoneId`.
    """
    loaded = await _load_webs(
        venue=venue, host=host, location_id=location_id, cache=cache
    )
    webs_list = _match_webs(
        loaded, venue=venue, host=host, location_id=location_id
    )
    out: list[dict] = []
    for webs in webs_list:
        v = _venue_from_webs(webs)
        tz = _venue_timezone(v)
        if not tz:
            return app_error(
                f"Venue {v.get('name')} has no timeZoneId — cannot build availability window",
                code="ProviderError",
            )
        start, end = _day_window_local(date, days, tz)
        h = webs["_host"]
        rows = await _bookings_list(h, start, end)
        clubs = _club_places(webs)
        idx = _club_by_space_index(clubs)
        club_filter = None
        if location_id:
            for c in clubs:
                if c["id"] == location_id or c.get("_tag") == location_id:
                    club_filter = set(c.get("_spaceIds") or [])
                    break
        spaces = {}
        for a in webs.get("assets") or []:
            if not isinstance(a, dict) or a.get("id") is None:
                continue
            aid = str(a["id"])
            if club_filter is not None and aid not in club_filter:
                continue
            spaces[aid] = _space_place(a, webs, idx)
        out.extend(
            _free_slot_events(
                spaces=spaces,
                occupied_rows=[r for r in rows if isinstance(r, dict)],
                window_start=start,
                window_end=end,
                timezone_name=tz,
                space_id=space_id,
                max_slots=max(0, 120 - len(out)),
            )
        )
        if len(out) >= 120:
            break
    return out


@returns("event")
@provides("reservation_get")
@connection("skedda")
@timeout(60)
async def get_bookable(
    *,
    id: str | None = None,
    booking_instance_id: str | None = None,
    location_id: str | None = None,
    venue: str | None = None,
    host: str | None = None,
    cache: dict | None = None,
    **params,
):
    """Hydrate one SwitchYards free slot (`sy-slot:{space}/{start}/{end}`)."""
    raw = id or booking_instance_id or params.get("booking_instance_id")
    if not raw or not str(raw).startswith("sy-slot:"):
        return app_error(
            "reservation_get expects id like sy-slot:{space_id}/{start}/{end}",
            code="InvalidArgument",
        )
    try:
        space_id, start, end = str(raw).removeprefix("sy-slot:").split("/", 2)
    except ValueError:
        return app_error(f"Malformed slot id: {raw}", code="InvalidArgument")
    loaded = await _load_webs(
        venue=venue, host=host, location_id=location_id, cache=cache
    )
    webs_list = _match_webs(
        loaded, venue=venue, host=host, location_id=location_id
    )
    for webs in webs_list:
        clubs = _club_places(webs)
        idx = _club_by_space_index(clubs)
        for a in webs.get("assets") or []:
            if not isinstance(a, dict) or str(a.get("id")) != space_id:
                continue
            place = _space_place(a, webs, idx)
            tz = _venue_timezone(_venue_from_webs(webs)) or ""
            start_utc = wall_to_utc(start, tz)
            end_utc = wall_to_utc(end, tz)
            return {
                "id": str(raw),
                "shape": "event",
                "at": _AT,
                "name": place.get("name") or f"Space {space_id}",
                "startDate": start_utc,
                "endDate": end_utc,
                "startTime": start_utc,
                "endTime": end_utc,
                "timezone": tz,
                "location": {
                    "id": place.get("id") or space_id,
                    "name": place.get("name"),
                    "shape": "place",
                    "at": _AT,
                },
                "space_id": space_id,
                "isFull": False,
                "availableActions": ["book"],
            }
    return app_error(f"Space {space_id} not found", code="NotFound")


# ──────────────────────────────────────────────────────────────────────
# Authed tools (venueuser session; cancel/check-in still pending RE)
# ──────────────────────────────────────────────────────────────────────


async def _find_space(
    space_id: str,
    *,
    cache: dict | None = None,
    host: str | None = None,
    venue: str | None = None,
    location_id: str | None = None,
) -> tuple[dict, dict]:
    """Locate `space_id` on a scoped venue → (webs, asset)."""
    sid = str(space_id)
    for webs in await _load_webs(
        venue=venue, host=host, location_id=location_id, cache=cache
    ):
        for asset in webs.get("assets") or []:
            if isinstance(asset, dict) and str(asset.get("id")) == sid:
                return webs, asset
    raise RuntimeError(
        f"Space {sid!r} not found on the scoped SwitchYards venue "
        "(pass host/venue/location_id from the selected city)."
    )


def _raw_booking_id(reservation_id: str) -> str:
    """Accept `sy-res:123` or bare Skedda booking id."""
    s = str(reservation_id or "").strip()
    if s.startswith("sy-res:"):
        s = s.split(":", 1)[1]
    if not s:
        raise ValueError("reservation_id required")
    return s


def _reservation_actions(row: dict, venue: dict | None = None) -> list[str]:
    """`cancel` when active; `check_in` inside venue checkInRules window (venue-local)."""
    status = str(row.get("status") or "confirmed").lower()
    if status in ("cancelled", "canceled", "deleted"):
        return []
    actions = ["cancel"]
    start_s = row.get("start")
    if not start_s:
        return actions
    try:
        start = datetime.fromisoformat(_skedda_local(str(start_s), None))
    except ValueError:
        return actions
    open_m, close_m = -30, 10
    rules = ((venue or {}).get("checkInRules") or {}).get("rules") or []
    if rules and isinstance(rules[0], dict):
        if rules[0].get("windowOpenMinutes") is not None:
            open_m = int(rules[0]["windowOpenMinutes"])
        if rules[0].get("windowCloseMinutes") is not None:
            close_m = int(rules[0]["windowCloseMinutes"])
    # Compare in venue-local naive clock (same as Skedda start strings).
    now = datetime.now().replace(tzinfo=None)
    # Prefer venue tz wall-clock when we have it.
    tz_name = _venue_timezone(venue)
    if tz_name:
        try:
            now = datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
        except Exception:
            pass
    lo = start + timedelta(minutes=open_m)
    hi = start + timedelta(minutes=close_m)
    if lo <= now <= hi and not row.get("checkInHistory"):
        actions.append("check_in")
    return actions


@returns("reservation[]")
@provides("reservation_list", account_param="account")
@connection("none")
@timeout(120)
async def get_my_reservations(
    *,
    venue: str | None = None,
    host: str | None = None,
    date: str | None = None,
    days: int = 14,
    cache: dict | None = None,
    **params,
):
    """List the signed-in member's bookings (in-tab bookingslists × venueuser)."""
    session = await check_session(**params)
    if not (isinstance(session, dict) and session.get("authenticated")):
        return app_error(
            "Sign in first (switchyards.login).",
            code="NeedsAuth",
        )
    if host or venue:
        webs_list = await _load_webs(venue=venue, host=host, cache=cache)
    else:
        # Only cities already enriched in cache — never open every /booking.
        loc = _locations_cache_entry(cache) or {}
        hosts = sorted(
            {
                str(p.get("_host"))
                for p in (loc.get("places") or [])
                if isinstance(p, dict)
                and p.get("_host")
                and not str(p.get("id") or "").startswith("sy-venue:pending:")
            }
        )
        webs_list = []
        for h in hosts:
            try:
                webs_list.append(await _webs_for_host(h))
            except Exception:
                continue
    out: list[dict] = []
    missing_venueuser_hosts: list[str] = []
    for webs in webs_list:
        v = _venue_from_webs(webs)
        tz = _venue_timezone(v)
        if not tz:
            continue
        start, end = _day_window_local(date, days, tz)
        h = webs["_host"]
        list_js = f"""
  const start = {json.dumps(start)};
  const end = {json.dumps(end)};
  const websR = await __skeddaFetch('/webs');
  if (!websR.ok) return {{ __error: 'http_' + websR.status }};
  const websJ = websR.json || {{}};
  const vu = (websJ.venueusers || [])[0] || null;
  const vuId = vu && (vu.id || vu.venueUserId);
  const userId =
    (websJ.web && websJ.web.userId) ||
    ((websJ.useraccount || [])[0] &&
      ((websJ.useraccount || [])[0].id || (websJ.useraccount || [])[0].userId)) ||
    null;
  if (!vuId) {{
    return {{
      __error: 'no_venueuser',
      userId: userId != null ? String(userId) : null,
    }};
  }}
  const q = new URLSearchParams({{ start, end }});
  const br = await __skeddaFetch('/bookingslists?' + q);
  if (!br.ok) return {{ __error: 'http_' + br.status }};
  const bj = br.json || {{}};
  const rows = bj.bookings || [];
  return {{
    venueuserId: String(vuId),
    bookings: rows.filter(b => String(b.venueuser || '') === String(vuId)),
  }};
"""
        payload = await _eval_on(h, list_js, wait_ms=25000, timeout_s=60)
        if isinstance(payload, dict) and payload.get("code"):
            return payload
        # Skedda userId session without club venueuser — land on /booking
        # so the venue host binds venueusers, then retry once.
        if isinstance(payload, dict) and payload.get("__error") == "no_venueuser":
            await browser_session.navigate(h, f"https://{h}{_BOOKING_PATH}")
            payload = await _eval_on(h, list_js, wait_ms=25000, timeout_s=60)
            if isinstance(payload, dict) and payload.get("code"):
                return payload
        if isinstance(payload, dict) and payload.get("__error") == "no_venueuser":
            missing_venueuser_hosts.append(h)
            continue
        if isinstance(payload, dict) and payload.get("__error"):
            return app_error(
                f"bookingslists failed on {h}: {payload.get('__error')}",
                code="ProviderError",
            )
        clubs = _club_places(webs)
        idx = _club_by_space_index(clubs)
        spaces = {
            str(a["id"]): _space_place(a, webs, idx)
            for a in (webs.get("assets") or [])
            if isinstance(a, dict) and a.get("id") is not None
        }
        for row in (payload or {}).get("bookings") or []:
            if isinstance(row, dict):
                out.append(_reservation_from_booking(row, webs, spaces))
    if not out and missing_venueuser_hosts:
        return app_error(
            "SwitchYards is signed in to Skedda but has no club venueuser yet. "
            "Run switchyards.login and finish the booking-page sign-in "
            f"({', '.join(missing_venueuser_hosts[:3])}), then retry.",
            code="NeedsAuth",
        )
    return out


def _booker_from_params(
    *,
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    params: dict,
) -> dict[str, str] | None:
    """Member identity for `/venueusers` — caller/session only; never baked-in PII.

    Returns None when register is not needed yet (caller still validates before
    POST). Country comes from the venue row in-tab, not from this dict.
    """
    auth = params.get("auth") if isinstance(params.get("auth"), dict) else {}
    account = params.get("account") if isinstance(params.get("account"), dict) else {}
    username = (
        (email or "").strip()
        or str(auth.get("email") or auth.get("identifier") or "").strip()
        or str(account.get("email") or account.get("identifier") or "").strip()
    )
    first = (
        (first_name or "").strip()
        or str(auth.get("firstName") or auth.get("given_name") or "").strip()
        or str(account.get("firstName") or "").strip()
    )
    last = (
        (last_name or "").strip()
        or str(auth.get("lastName") or auth.get("family_name") or "").strip()
        or str(account.get("lastName") or "").strip()
    )
    display = str(
        auth.get("displayName") or account.get("displayName") or account.get("name") or ""
    ).strip()
    if (not first or not last) and display and " " in display:
        left, right = display.split(None, 1)
        first = first or left
        last = last or right
    if not username or "@" not in username:
        return None
    if not first or not last:
        return None
    return {"username": username, "firstName": first, "lastName": last}


@returns("reservation")
@provides("reservation_create", account_param="account")
@connection("none")
@timeout(90)
async def create_reservation(
    *,
    space_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    title: str | None = None,
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    id: str | None = None,
    **params,
):
    """Book a booth/room: ensure venueuser, then `POST /bookings` in-tab.

    Brokered as `reservation_create`. Accepts `space_id`+`start`+`end`, or
    a slot `id` (`sy-slot:{space}/{start}/{end}`) from availability.

    Passwordless — if the tab has no venueuser yet, registers one with
    `venue.publicRegisterPayload` plus the caller's email/name (params or
    injected account). No baked-in member identity. Country from the venue
    row. `start`/`end` may be UTC (`…Z`) or venue-local naive ISO.
    """
    slot = id or params.get("booking_instance_id")
    if (not space_id or not start or not end) and slot and str(slot).startswith(
        "sy-slot:"
    ):
        try:
            space_id, start, end = str(slot).removeprefix("sy-slot:").split("/", 2)
        except ValueError:
            return app_error(f"Malformed slot id: {slot}", code="InvalidArgument")
    if not space_id or not start or not end:
        return app_error(
            "reservation_create requires space_id+start+end or a sy-slot id",
            code="InvalidArgument",
        )
    cache = params.get("cache") if isinstance(params.get("cache"), dict) else None
    try:
        webs, asset = await _find_space(
            str(space_id),
            cache=cache,
            host=params.get("host"),
            venue=params.get("venue"),
            location_id=params.get("location_id"),
        )
        venue = _venue_from_webs(webs)
        tz = _venue_timezone(venue)
        start_n = _skedda_local(start, tz)
        end_n = _skedda_local(end, tz)
    except Exception as e:
        return app_error(str(e), code="InvalidArgument")

    host = webs["_host"]
    venue_id = str(venue.get("id") or "")
    if not venue_id:
        return app_error(f"No venue id on {host}", code="ProviderError")

    booker = _booker_from_params(
        email=email, first_name=first_name, last_name=last_name, params=params
    )

    payload = await _eval_on(
        host,
        f"""
  const spaceId = {json.dumps(str(space_id))};
  const start = {json.dumps(start_n)};
  const end = {json.dumps(end_n)};
  const title = {json.dumps(title)};
  const booker = {json.dumps(booker)};
  const venueId = {json.dumps(venue_id)};

  const websR = await __skeddaFetch('/webs');
  if (!websR.ok) return {{ __error: 'http_' + websR.status, detail: websR.json || websR.body }};
  const websJ = websR.json || {{}};
  if (websJ.errors) return {{ __error: 'webs_errors', detail: websJ.errors }};
  const venue = Array.isArray(websJ.venue) ? websJ.venue[0] : websJ.venue;
  if (!venue) return {{ __error: 'no_venue' }};

  let vu = (websJ.venueusers || [])[0] || null;
  if (!vu) {{
    if (!booker || !booker.username || !booker.firstName || !booker.lastName) {{
      return {{
        __error: 'auth_required',
        detail: 'No venueuser — pass email, first_name, and last_name to register',
      }};
    }}
    const meta = venue.publicRegisterPayload;
    if (!meta) {{
      return {{
        __error: 'auth_required',
        detail: 'No venueuser and publicRegisterPayload empty — open /booking anonymously or login_window',
      }};
    }}
    const country =
      venue.cultureTwoLetterCountryCode ||
      venue.countryCode ||
      venue.twoLetterCountryCode ||
      null;
    const regBody = {{
      venueuser: {{
        username: booker.username,
        firstName: booker.firstName,
        lastName: booker.lastName,
        organisation: null,
        twoLetterCountryCode: country,
        contactNumber: null,
        termsAgreed: true,
        registerMetadata: meta,
        venueusertags: venue.defaultVisitorTags || [],
      }},
    }};
    const reg = await __skeddaFetch('/venueusers', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(regBody),
    }});
    if (!reg.ok) {{
      return {{ __error: 'venueusers_http_' + reg.status, detail: reg.json || reg.body }};
    }}
    const regJ = reg.json || {{}};
    if (regJ.errors) return {{ __error: 'venueusers_errors', detail: regJ.errors }};
    vu = (regJ.venueusers || [])[0] || null;
    if (!vu) return {{ __error: 'venueusers_empty', detail: regJ }};
  }}

  const vuId = String(vu.id);
  const rules = (venue.checkInRules && venue.checkInRules.rules) || [];
  let audits = '';
  for (const rule of rules) {{
    const ids = rule.spaceIds;
    if (!ids || ids.map(String).includes(String(spaceId))) {{
      audits = rule.checkInAudits || '';
      break;
    }}
  }}
  if (!audits && rules[0]) audits = rules[0].checkInAudits || '';

  // `price` is currencyCultured — omit/null → 422; free member booths use 0.
  const bookBody = {{
    booking: {{
      start,
      end,
      spaces: [String(spaceId)],
      venue: String(venue.id || venueId),
      venueuser: vuId,
      type: 1,
      paymentStatus: 0,
      price: 0,
      title: title || null,
      checkInAudits: audits,
      hideAttendees: true,
      availabilityStatus: 1,
      attendees: [],
      addOns: [],
      customFields: [],
    }},
  }};
  const br = await __skeddaFetch('/bookings', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(bookBody),
  }});
  if (!br.ok) {{
    return {{ __error: 'bookings_http_' + br.status, detail: br.json || br.body }};
  }}
  const bj = br.json || {{}};
  if (bj.errors) return {{ __error: 'bookings_errors', detail: bj.errors }};
  // Success payload is singular `booking` (not `bookings[]`).
  const booking = bj.booking || (Array.isArray(bj.bookings) ? bj.bookings[0] : null);
  if (!booking || typeof booking !== 'object') {{
    return {{ __error: 'bookings_empty', detail: bj }};
  }}
  return {{ booking, venueuserId: vuId }};
""",
        wait_ms=25000,
        timeout_s=75,
    )

    if isinstance(payload, dict) and payload.get("code"):
        return payload
    if isinstance(payload, dict) and payload.get("__error"):
        err = payload["__error"]
        detail = payload.get("detail")
        detail_s = json.dumps(detail) if not isinstance(detail, str) else detail
        if err == "auth_required":
            return app_error(
                str(detail)
                if isinstance(detail, str) and detail
                else "SwitchYards has no venueuser — pass email, first_name, "
                "last_name (or finish login) to register.",
                code="NeedsAuth",
            )
        if err in ("venueusers_errors", "venueusers_http_422") and detail_s and (
            "already a user" in detail_s.lower()
            or "associated with that email" in detail_s.lower()
        ):
            return app_error(
                "That email already has a Skedda userprofile at this venue. "
                "Run switchyards.login (headed) to restore the session, then retry.",
                code="NeedsAuth",
            )
        return app_error(
            f"create_reservation failed: {err}"
            + (f" — {detail}" if detail is not None else ""),
            code="ProviderError",
        )

    row = (payload or {}).get("booking") if isinstance(payload, dict) else None
    if not isinstance(row, dict):
        return app_error("Skedda returned no booking object", code="ProviderError")

    clubs = _club_places(webs)
    spaces = {
        str(a["id"]): _space_place(a, webs, _club_by_space_index(clubs))
        for a in (webs.get("assets") or [])
        if isinstance(a, dict) and a.get("id") is not None
    }
    # Prefer the asset we resolved if /webs cache was stale.
    if str(space_id) not in spaces:
        spaces[str(space_id)] = _space_place(asset, webs, _club_by_space_index(clubs))
    return _reservation_from_booking(row, webs, spaces)


async def _find_booking_row(
    booking_id: str,
    *,
    venue: str | None = None,
    host: str | None = None,
    location_id: str | None = None,
    cache: dict | None = None,
) -> tuple[dict, dict] | None:
    """Locate a booking id on a venue → (webs, booking row) via bookingslists."""
    bid = str(booking_id)
    for webs in await _load_webs(
        venue=venue, host=host, location_id=location_id, cache=cache
    ):
        v = _venue_from_webs(webs)
        tz = _venue_timezone(v)
        if not tz:
            continue
        # Wide window — Skedda lists are day-bounded; cover ±7d from user "today".
        start, end = _day_window_local(None, 14, tz)
        # Shift start back a week so we still see today's past slots.
        try:
            start_dt = datetime.fromisoformat(start) - timedelta(days=7)
            start = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
        rows = await _bookings_list(webs["_host"], start, end)
        for row in rows:
            if isinstance(row, dict) and str(row.get("id")) == bid:
                return webs, row
    return None


@returns("reservation")
@provides("reservation_update", account_param="account")
@connection("none")
@timeout(90)
async def update_reservation(
    *,
    reservation_id: str,
    start: str | None = None,
    end: str | None = None,
    title: str | None = None,
    venue: str | None = None,
    host: str | None = None,
    **params,
):
    """Extend/move a booking — `PUT /bookings/:id` (Ember updateRecord).

    Pass new `start` and/or `end` (UTC `…Z` or venue-local naive ISO). Omitting
    a side keeps the existing value from bookingslists.
    """
    session = await check_session(venue=venue, host=host, **params)
    if not (isinstance(session, dict) and session.get("authenticated")):
        return app_error(
            "Sign in first (switchyards.login) before updating a booking.",
            code="NeedsAuth",
        )
    if start is None and end is None and title is None:
        return app_error(
            "Pass start, end, and/or title to update.",
            code="InvalidArgument",
        )
    try:
        bid = _raw_booking_id(reservation_id)
    except ValueError as e:
        return app_error(str(e), code="InvalidArgument")

    found = await _find_booking_row(bid, venue=venue, host=host)
    if not found:
        return app_error(
            f"Booking {bid} not found on discovered venues.",
            code="NotFound",
        )
    webs, row = found
    h = webs["_host"]
    venue_obj = _venue_from_webs(webs)
    tz = _venue_timezone(venue_obj)
    try:
        start_n = _skedda_local(start, tz) if start else None
        end_n = _skedda_local(end, tz) if end else None
    except ValueError as e:
        return app_error(str(e), code="InvalidArgument")
    new_start = start_n or row.get("start")
    new_end = end_n or row.get("end")
    spaces = [str(s) for s in (row.get("spaces") or [])]
    if not new_start or not new_end or not spaces:
        return app_error(
            "Booking row missing start/end/spaces — cannot update.",
            code="ProviderError",
        )

    payload = await _eval_on(
        h,
        f"""
  const id = {json.dumps(bid)};
  const start = {json.dumps(new_start)};
  const end = {json.dumps(new_end)};
  const spaces = {json.dumps(spaces)};
  const venueId = {json.dumps(str(venue_obj.get("id") or ""))};
  const venueuser = {json.dumps(str(row.get("venueuser") or ""))};
  const title = {json.dumps(title if title is not None else row.get("title"))};

  const websR = await __skeddaFetch('/webs');
  if (!websR.ok) return {{ __error: 'http_' + websR.status }};
  const websJ = websR.json || {{}};
  const venue = Array.isArray(websJ.venue) ? websJ.venue[0] : websJ.venue;
  const vu = (websJ.venueusers || [])[0];
  const vuId = venueuser || (vu && String(vu.id)) || null;
  if (!vuId && !(websJ.web && websJ.web.userId)) {{
    return {{ __error: 'auth_required' }};
  }}

  const rules = (venue && venue.checkInRules && venue.checkInRules.rules) || [];
  let audits = '';
  for (const rule of rules) {{
    const ids = rule.spaceIds;
    if (!ids || ids.map(String).includes(String(spaces[0]))) {{
      audits = rule.checkInAudits || '';
      break;
    }}
  }}
  if (!audits && rules[0]) audits = rules[0].checkInAudits || '';

  const bookBody = {{
    booking: {{
      id: String(id),
      start,
      end,
      spaces,
      venue: String(venueId || (venue && venue.id) || ''),
      venueuser: vuId ? String(vuId) : null,
      type: 1,
      paymentStatus: 0,
      price: 0,
      title: title || null,
      checkInAudits: audits,
      hideAttendees: true,
      availabilityStatus: 1,
      attendees: [],
      addOns: [],
      customFields: [],
    }},
  }};
  const r = await __skeddaFetch('/bookings/' + encodeURIComponent(id), {{
    method: 'PUT',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(bookBody),
  }});
  if (r.status === 401 || r.status === 403) {{
    return {{ __error: 'auth_required', detail: r.json || r.body }};
  }}
  if (!r.ok) {{
    return {{ __error: 'update_http_' + r.status, detail: r.json || r.body }};
  }}
  const j = r.json || {{}};
  if (j.errors) return {{ __error: 'update_errors', detail: j.errors }};
  const booking = j.booking || (Array.isArray(j.bookings) ? j.bookings[0] : null);
  if (!booking) return {{ __error: 'update_empty', detail: j }};
  return {{ booking }};
""",
        wait_ms=25000,
        timeout_s=60,
    )
    if isinstance(payload, dict) and payload.get("code"):
        return payload
    if isinstance(payload, dict) and payload.get("__error") == "auth_required":
        return app_error(
            "SwitchYards session cannot update — run switchyards.login.",
            code="NeedsAuth",
        )
    if isinstance(payload, dict) and payload.get("__error"):
        return app_error(
            f"update_reservation failed: {payload['__error']}"
            + (
                f" — {payload.get('detail')}"
                if payload.get("detail") is not None
                else ""
            ),
            code="ProviderError",
        )

    updated = (payload or {}).get("booking") if isinstance(payload, dict) else None
    if not isinstance(updated, dict):
        return app_error("Skedda returned no booking after update", code="ProviderError")
    # Merge list-row fields Skedda may omit on PUT response.
    merged = {**row, **updated, "start": updated.get("start") or new_start, "end": updated.get("end") or new_end}
    clubs = _club_places(webs)
    spaces_by_id = {
        str(a["id"]): _space_place(a, webs, _club_by_space_index(clubs))
        for a in (webs.get("assets") or [])
        if isinstance(a, dict) and a.get("id") is not None
    }
    return _reservation_from_booking(merged, webs, spaces_by_id)


@returns("reservation")
@provides("reservation_cancel", account_param="account")
@connection("none")
@timeout(90)
async def cancel_reservation(
    *,
    reservation_id: str,
    venue: str | None = None,
    host: str | None = None,
    **params,
):
    """Cancel a booking — `DELETE /bookings/:id` in-tab (Ember destroyRecord).

    Brokered as `reservation_cancel`. `reservation_id` may be bare Skedda
    id or `sy-res:{id}`.
    """
    session = await check_session(venue=venue, host=host, **params)
    if not (isinstance(session, dict) and session.get("authenticated")):
        return app_error(
            "Sign in first (switchyards.login) — returning Skedda accounts "
            "need a headed sign-in before cancel.",
            code="NeedsAuth",
        )
    try:
        bid = _raw_booking_id(reservation_id)
    except ValueError as e:
        return app_error(str(e), code="InvalidArgument")

    found = await _find_booking_row(bid, venue=venue, host=host)
    hosts = (
        [found[0]["_host"]]
        if found
        else await _hosts_for_session(venue=venue, host=host)
    )
    last_err: Any = None
    for h in hosts:
        payload = await _eval_on(
            h,
            f"""
  const id = {json.dumps(bid)};
  const r = await __skeddaFetch('/bookings/' + encodeURIComponent(id), {{
    method: 'DELETE',
  }});
  if (r.status === 401 || r.status === 403) {{
    return {{ __error: 'auth_required', detail: r.json || r.body }};
  }}
  if (!r.ok && r.status !== 204) {{
    return {{ __error: 'cancel_http_' + r.status, detail: r.json || r.body }};
  }}
  const bj = r.json || {{}};
  if (bj.errors) return {{ __error: 'cancel_errors', detail: bj.errors }};
  return {{
    ok: true,
    booking: bj.booking || {{ id, pendingDeletion: true, isDeleted: true }},
  }};
""",
            wait_ms=25000,
            timeout_s=60,
        )
        if isinstance(payload, dict) and payload.get("code"):
            return payload
        if isinstance(payload, dict) and payload.get("__error") == "auth_required":
            return app_error(
                "SwitchYards session cannot cancel — run switchyards.login.",
                code="NeedsAuth",
            )
        if isinstance(payload, dict) and payload.get("ok"):
            webs = found[0] if found else await _webs_for_host(h)
            row = payload.get("booking") or {"id": bid}
            if found and isinstance(found[1], dict):
                row = {**found[1], **row, "pendingDeletion": True, "isDeleted": True}
            else:
                row = {**row, "pendingDeletion": True, "isDeleted": True, "id": bid}
            clubs = _club_places(webs)
            spaces = {
                str(a["id"]): _space_place(a, webs, _club_by_space_index(clubs))
                for a in (webs.get("assets") or [])
                if isinstance(a, dict) and a.get("id") is not None
            }
            return _reservation_from_booking(row, webs, spaces)
        last_err = payload
    return app_error(
        f"cancel_reservation failed for {bid}: {last_err}",
        code="ProviderError",
    )


@returns("reservation")
@provides("reservation_check_in", account_param="account")
@connection("none")
@timeout(90)
async def check_in(
    *,
    reservation_id: str,
    venue: str | None = None,
    host: str | None = None,
    **params,
):
    """Check in — `POST /bookingcheckins` (SPA `_checkInBooking`).

    Brokered as `reservation_check_in`. Window from `venue.checkInRules`
    (typically −30 / +10 minutes from start).
    """
    session = await check_session(venue=venue, host=host, **params)
    if not (isinstance(session, dict) and session.get("authenticated")):
        return app_error(
            "Sign in first (switchyards.login) before check-in.",
            code="NeedsAuth",
        )
    try:
        bid = _raw_booking_id(reservation_id)
    except ValueError as e:
        return app_error(str(e), code="InvalidArgument")

    found = await _find_booking_row(bid, venue=venue, host=host)
    if not found:
        return app_error(
            f"Booking {bid} not found on discovered venues (check date window).",
            code="NotFound",
        )
    webs, row = found
    h = webs["_host"]
    venue_obj = _venue_from_webs(webs)
    occurrence = row.get("start")
    space_ids = [str(s) for s in (row.get("spaces") or [])]
    space_id = space_ids[0] if space_ids else None

    payload = await _eval_on(
        h,
        f"""
  const bookingId = {json.dumps(bid)};
  const occurrenceDate = {json.dumps(occurrence)};
  const spaceId = {json.dumps(space_id)};

  const websR = await __skeddaFetch('/webs');
  if (!websR.ok) return {{ __error: 'http_' + websR.status, detail: websR.json || websR.body }};
  const websJ = websR.json || {{}};
  const venue = Array.isArray(websJ.venue) ? websJ.venue[0] : websJ.venue;
  if (!venue) return {{ __error: 'no_venue' }};
  const vu = (websJ.venueusers || [])[0];
  if (!vu && !(websJ.web && websJ.web.userId)) {{
    return {{ __error: 'auth_required' }};
  }}

  const rules = (venue.checkInRules && venue.checkInRules.rules) || [];
  let audits = null;
  for (const rule of rules) {{
    const ids = rule.spaceIds;
    if (!spaceId || !ids || ids.map(String).includes(String(spaceId))) {{
      audits = rule.checkInAudits || null;
      break;
    }}
  }}
  if (audits == null && rules[0]) audits = rules[0].checkInAudits || null;

  // Ember: createRecord("bookingcheckin", {{ bookingId: intId, occurrenceDate, checkInAudits }})
  const body = {{
    bookingcheckin: {{
      bookingId: Number(bookingId),
      occurrenceDate,
      checkInAudits: audits,
    }},
  }};
  const r = await __skeddaFetch('/bookingcheckins', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(body),
  }});
  if (r.status === 401 || r.status === 403) {{
    return {{ __error: 'auth_required', detail: r.json || r.body }};
  }}
  if (!r.ok) {{
    return {{ __error: 'checkin_http_' + r.status, detail: r.json || r.body }};
  }}
  const j = r.json || {{}};
  if (j.errors) return {{ __error: 'checkin_errors', detail: j.errors }};
  return {{
    ok: true,
    checkin: j.bookingcheckin || j.bookingcheckins || j,
  }};
""",
        wait_ms=25000,
        timeout_s=60,
    )
    if isinstance(payload, dict) and payload.get("code"):
        return payload
    if isinstance(payload, dict) and payload.get("__error") == "auth_required":
        return app_error(
            "SwitchYards session cannot check in — run switchyards.login.",
            code="NeedsAuth",
        )
    if isinstance(payload, dict) and payload.get("__error"):
        return app_error(
            f"check_in failed: {payload['__error']}"
            + (
                f" — {payload.get('detail')}"
                if payload.get("detail") is not None
                else ""
            ),
            code="ProviderError",
        )

    # Refresh row so checkInHistory / actions update when possible.
    refreshed = await _find_booking_row(bid, venue=venue, host=h)
    if refreshed:
        webs, row = refreshed
    else:
        row = {**row, "checkInHistory": row.get("checkInHistory") or [{"ok": True}]}
    clubs = _club_places(webs)
    spaces = {
        str(a["id"]): _space_place(a, webs, _club_by_space_index(clubs))
        for a in (webs.get("assets") or [])
        if isinstance(a, dict) and a.get("id") is not None
    }
    return _reservation_from_booking(row, webs, spaces)
