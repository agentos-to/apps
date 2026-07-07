---
id: uber
services:
- http
name: Uber
description: Ride history, trip details, Eats order history, and account info from
  Uber
color: '#000000'
website: https://uber.com
product:
  name: Uber
  website: https://uber.com
  developer: Uber Technologies
---

# Uber

Ride history, trip details, receipts, and account info from Uber. Rides use Uber's internal GraphQL API at `riders.uber.com/graphql`; Uber Eats uses a separate RPC API at `ubereats.com/_p/api/`. **Browser-driven** — every request runs as a same-origin `fetch()` inside the engine-owned browser tab.

> **Before extending this app**, read:
> 1. [Browser-Driven Connectors](browser-driven on the system volume) — the pattern, the four footguns, the account trio
> 2. [requirements.md](./requirements.md) — captured API shapes, endpoint inventory, request bodies
> 3. Reference connectors: `apps/web/exa/exa.py`, `apps/dev/greptile/greptile.py`

## Features

### Rides
- **`list_trips`** — Ride history with pagination. Returns trip ID, destination, fare, date, and map URL. Supports `profile_type` (PERSONAL/BUSINESS) filter and pagination via `next_page_token`. Max 50 per page.
- **`get_trip`** — Full trip details: driver info, pickup/dropoff addresses, fare breakdown, distance, duration, vehicle type, surge pricing, map URL, and rating.

### Account
- **`whoami`** — Full user profile: name, email, phone, rating, picture URL, Uber One membership, payment methods, and profiles (personal/business).
- **`check_session` / `login` / `logout`** — the rides account trio, bound to the riders.uber.com tab.
- **`check_eats_session` / `login_eats` / `logout_eats`** — the Eats account trio (separate platform, separate ubereats.com session).

## Auth — browser-driven (no cookies, no API keys)

The session **is the engine-owned browser profile**. Uber's auth cookies on `.uber.com` / `.ubereats.com` are written by Uber's own Set-Cookie, never extracted, never vaulted, never seen by this app. Every op runs as a same-origin `fetch()` evaluated inside the matching tab via the `browser_session` service (the Exa/Greptile pattern); the cookie rides because the fetch is same-origin, and the real browser supplies the live session, TLS fingerprint, and any anti-bot cookies by construction.

**Two registrable domains, two tabs** (one tab per domain):

| Tab target | Platform | Wire |
|---|---|---|
| `riders.uber.com` | rides | GraphQL `POST /graphql` |
| `www.ubereats.com` | Eats | RPC `POST /_p/api/{operation}` |

### Signing in

Sign in **once, headed**, in the AgentOS browser:

1. Open `riders.uber.com` (rides) and/or `ubereats.com` (Eats) in the AgentOS browser.
2. Complete Uber's phone/email OTP + any anti-bot challenge as a human — the durable, safe path; there's no stable form shape to drive blind.
3. The session lands in the browser profile. Every op rides it from then on.

`login` / `login_eats` report the live session when one exists, and otherwise return a NeedsAuth `app_error` pointing you to sign in headed. `logout` / `logout_eats` clear the session in the profile.

### Native-interface note

A same-origin `fetch()` of `/graphql` (rides) or `/_p/api/{op}` (Eats) is **byte-identical** to what Uber's own React app sends — it's the lowest stable contract that already carries the session. We do **not** reach into Uber's minified JS modules; the wire IS the tap.

### App-level contract headers

The real browser supplies all browser headers (UA, Sec-CH-UA*, Sec-Fetch-*). The app adds only Uber's literal app-level headers — not a fingerprint:

- Rides: `x-csrf-token: x` (literal string, not a rotating token), `x-uber-rv-session-type: desktop_session`
- Eats: `x-csrf-token: x`

Three rides GraphQL operations: `CurrentUserRidersWeb` (profile), `Activities` (trip history), `GetTrip` (trip detail).

## Uber Eats (in progress)

Uber Eats uses a **completely different API** from rides. It's NOT GraphQL — it's an RPC-style API at `www.ubereats.com/_p/api/`.

### Discovery (2026-04-02)

Used `browse capture` (CDP network capture via `bin/browse-capture.py`) to navigate `ubereats.com/orders` in Brave and capture all API calls. Key findings:

**Uber Eats API endpoints** (all `POST https://www.ubereats.com/_p/api/`):

| Endpoint | Purpose | Request body |
|----------|---------|-------------|
| `getPastOrdersV1` | Order history | `{ "lastWorkflowUUID": "" }` (pagination) |
| `getOrderEntitiesV1` | Order details — items, driver, receipt | `{}` |
| `getActiveOrdersV1` | Live orders in progress | `{ "orderUuid": null, "timezone": "America/Chicago" }` |
| `getCartsViewForEaterUuidV1` | Current cart state | `{}` |
| `getSearchHomeV2` | Store browsing / search | `{ "dropPastOrders": true }` |
| `getDraftOrdersByEaterUuidV1` | Draft (unsent) orders | `{ "removeAdapters": true }` |
| `getUserV1` | User profile for Eats | `{ "shouldGetSubsMetadata": true }` |
| `getProfilesForUserV1` | User profiles | `{}` |
| `getInstructionForLocationV1` | Delivery instructions | `{ "location": { "latitude": ..., "longitude": ... } }` |
| `setRobotEventsV1` | Bot detection telemetry | `{ "action": "rendered", "payload": { "isBot": false } }` |

**Auth headers for Eats** (different from rides):

```
x-csrf-token: x
x-uber-session-id: <from uev2.id.session cookie>
x-uber-target-location-latitude: 30.271044
x-uber-target-location-longitude: -97.695755
x-uber-client-gitref: <client version hash>
x-uber-ciid: <client instance ID>
x-uber-request-id: <UUID per request>
Content-Type: application/json
```

**Cookie domain:** `.ubereats.com` (NOT `.uber.com` — different domain from rides)

**Real-time events:** `ramenphx/events/recv` and `ramendca/events/recv` — likely SSE or long-polling for live delivery tracking updates.

**Key difference from rides:** The `order_types: "EATS"` parameter on the rides GraphQL `Activities` query does NOT work — `EATS` is not a valid enum value in `RVWebCommonActivityOrderType`. Uber Eats order history must be fetched from the Eats-specific `getPastOrdersV1` endpoint.

### Eats operations (shipped)

Read: `check_eats_session`, `get_eats_profile`, `list_deliveries`, `get_delivery`, `get_messages`, `list_nearby_stores`, `search_stores`, `search_products`, `get_store`, `get_item_customizations`, `search_address`, `list_addresses`.

Write: `add_to_cart`, `get_cart`, `clear_cart`, `checkout`, `track_delivery`.

Use `agentos call apps '{"op":"load","params":{"app":"uber"}}'` or `load({app:"uber"})` for the live tool manifest with full param schemas — it's generated from the `@returns` decorators, so it's always in sync with the code.

### Ordering flow (MANDATORY for any agent placing a real order)

Placing a pizza-to-the-pizza-place order once was hilarious. Twice would be
embarrassing. Follow this sequence every time, in this order:

1. **Find the store.** `search_stores({query})` or use a past order's
   `store.id` from `list_deliveries` / `get_delivery`.
2. **Get the menu.** `get_store({store_uuid})`. The returned `offers[]` items
   carry a hidden `_raw` field with the full catalog payload — `add_to_cart`
   requires it, so pass `offers` items through directly; don't reconstruct.
3. **Customizations (if any).** `get_item_customizations({store_uuid, item_uuid})`.
   The app now auto-resolves section/subsection UUIDs when omitted.
4. **Pick an explicit delivery address.** `list_addresses()` → choose by
   **`source == "SAVED"` + `label == "HOME"`** first. If `SAVED` is empty
   (common — Uber treats pasted/searched addresses as SUGGESTED until the user
   explicitly saves them), prompt the user to pick from the SUGGESTED list.
   **Never auto-pick a SUGGESTED entry.** Uber mixes real addresses and POIs
   (restaurants, shops) into SUGGESTED; auto-picking got a pizza delivered to
   the pizza restaurant on 2026-04-20.
5. **Build the cart.** `add_to_cart({store_uuid, items, delivery_address_uuid})`.
   The app creates a draft via `createDraftOrderV2` and then **pins the
   address via `updateDraftOrderV2`** — `createDraftOrderV2` silently ignores
   `deliveryAddress` in its own body and inherits whatever the account's
   "active target" is (the thing that bit us).
6. **Pre-checkout checklist — SHOW THE USER, then wait for explicit go.**
   Surface all of:
   - **Store**: name + address
   - **Items**: name, customizations, quantity, price each
   - **Delivery address**: full address **and deliveryNotes** (gate codes,
     apartment numbers — critical for actual delivery)
   - **Total** (from the checkout presentation) + fare breakdown
   - **ETA**

   Do not call `checkout()` without an explicit "place it" from the user.
7. **Place the order.** `checkout({draft_order_uuid})`. This actually spends
   money; it's irreversible within seconds.
8. **Track.** `track_delivery()` polls `getActiveOrdersV1`. Returns driver,
   eta, and polyline traces — but **only while the order is active**. Once
   delivered/closed, driver + vehicle info are gone from Uber's API (privacy).
   `get_delivery` on a completed order returns store + items + fare but no
   driver.

### `get_messages` is ephemeral

Driver chat via `getEaterMessagingContentV1` only returns content while the
order is active or very recently delivered. Once the delivery completes and
Uber closes the chat, **the server returns empty body/head** — no message
history is persisted to the eater side. If you need a durable record, capture
messages during `track_delivery` polling, not after.

Also: the app currently returns `{body, head, messages[]}` on the order
shape rather than proper `conversation` + `message[]` entities. Known kludge.
Fix when a future order actually produces a non-empty chat we can shape against.

### Troubleshooting

**`SESSION_EXPIRED` / `{authenticated: false}` / NeedsAuth from any op** — there's no live Uber session in the AgentOS browser profile, OR the tab got bounced to `auth.uber.com` (logged out). Sign in once headed: open `riders.uber.com` (rides) or `ubereats.com` (Eats) in the AgentOS browser and complete sign-in. No cookie flushing, no SQLite staleness to worry about — the session is the live browser profile, so it's fresh by construction. Run `check_session` / `check_eats_session` to confirm.

**`rtapi.forbidden` on `getUserV1` / `code=3` on `getPastOrdersV1` / `401` on `getDraftOrdersByEaterUuidV1`** — the tab's session is visitor-level, not user-level (you're signed out of Eats specifically). `search_stores` / anonymous endpoints still work because they don't need a logged-in identity, which can mask the failure. Sign in headed at [ubereats.com](https://www.ubereats.com) in the AgentOS browser.

**`invalid_uuid` / 404 on `getMenuItemV1`** — `get_item_customizations` needs both `section_uuid` and `subsection_uuid`. The app auto-fetches them from `getStoreV1` when omitted; if you see this error again, the item UUID itself is stale or wrong.

## Reverse Engineering Notes

### Tools used

- **`agentos browse request uber`** — authenticated HTTP request with full header visibility. Used to verify cookie auth and inspect response headers.
- **`agentos browse cookies uber`** — cookie inventory showing all `.uber.com` cookies with timestamps and provenance.
- **`agentos browse auth uber`** — auth resolution trace showing which provider won (brave-browser) and identity (agentos@contini.co).
- **`bin/browse-capture.py`** — CDP network capture. Connected to Brave via CDP, navigated to `ubereats.com/orders`, captured 90 requests including all `/_p/api/` calls with full headers and POST bodies.

### How to extend

**Step 1: Capture network traffic with CDP**

```bash
# Launch Brave with CDP
open -a "Brave Browser" --args --remote-debugging-port=9222 --remote-allow-origins="*"

# Capture network traffic for any Uber Eats page
python3 bin/browse-capture.py https://www.ubereats.com/store/costco/... --port 9222

# Look for /_p/api/ POST requests in the output
# Response bodies are captured automatically via CDP Network.getResponseBody
```

**Step 2: Extract full API surface from JS bundles**

Don't just capture what one page loads — extract ALL endpoint names from the client JS:

```bash
# Find the main bundle URL from browse-capture output
# Then grep for API endpoint patterns
curl -s "https://www.ubereats.com/_static/client-main-*.js" \
  | grep -oE 'get[A-Z][a-zA-Z]+V[0-9]+' | sort -u   # read endpoints
curl -s "https://www.ubereats.com/_static/client-main-*.js" \
  | grep -oE '[a-z]+[A-Z][a-zA-Z]+V[0-9]+' | sort -u | grep -v '^get'  # write endpoints
```

This revealed 32 endpoints (22 read, 10 write) that weren't visible from a single page capture. The pattern `{verb}{Entity}V{version}` is consistent across all Uber Eats endpoints.

**Step 3: Test individual endpoints**

Use `agentos browse request` or direct `curl` to test specific endpoints. The auth headers and cookie domain are documented in [requirements.md](./requirements.md).

See [Reverse Engineering overview](../../../platform/docs/src/content/docs/apps/reverse-engineering/overview.md) for the full methodology and [Browse Toolkit spec](../../../docs/specs/browse-toolkit.md) for tool documentation.

### CDP tips for testing Eats endpoints

**Making authenticated API calls via CDP:**
```python
import json, urllib.request, websocket

# Connect to Brave (must be running with --remote-debugging-port=9222)
tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=15)

# IMPORTANT: Navigate to ubereats.com first — fetch with credentials: 'include'
# only sends cookies for same-origin requests
ws.send(json.dumps({"id": 1, "method": "Page.navigate",
    "params": {"url": "https://www.ubereats.com/"}}))
import time; time.sleep(5)  # wait for page load

# Call any /_p/api/ endpoint
js = """
(async () => {
    const r = await fetch('https://www.ubereats.com/_p/api/getPastOrdersV1', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json', 'x-csrf-token': 'x'},
        body: JSON.stringify({"lastWorkflowUUID": ""})
    });
    return await r.text();
})()
"""
ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
    "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))

# Read response (skip any navigation events)
for _ in range(20):
    resp = json.loads(ws.recv())
    if resp.get("id") == 2:
        data = json.loads(resp["result"]["result"]["value"])
        break
```

**Key gotchas:**
- Use `websocket` module (installed), NOT `websockets` (not installed). Synchronous API, no asyncio.
- Brave's cookie DB is encrypted — can't extract cookies from SQLite directly. Use CDP `Network.getCookies` or the agentOS engine's auth resolver.
- The `x-csrf-token: x` header is required. Other Eats headers (`x-uber-session-id`, `x-uber-target-location-*`) are optional for basic reads — the browser sends them automatically via cookies.
- When reading CDP responses, check `resp.get("id")` to match your request — navigation and other events arrive on the same websocket.
