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

> **Before extending this app** (dev only), read:
> 1. [Browser-Driven Connectors](browser-driven on the system volume) — the pattern, the four footguns, the account trio
> 2. [dev/requirements.md](./dev/requirements.md) — captured API shapes, endpoint inventory, request bodies
> 3. Reference connectors: `apps/cross-platform/exa/exa.py`, `apps/cross-platform/greptile/greptile.py`
>
> **Layout**
> ```
> uber/
>   readme.md          # runtime contract — how to call tools
>   uber.py            # decorated tools
>   lib/               # Python helpers (not scanned for tools)
>   page/              # shipped page-local SDK (injected into the Eats tab)
>     eats.js          # window.__ueats
>     eats.cart.js     # cart / checkout verbs
>   dev/               # authoring only — never injected at runtime
>     requirements.md  # RE findings / API inventory
>     probes/          # one-shot RE scripts (e.g. order-net.js)
> ```

## Features

### Rides
- **`list_trips`** — Ride history with pagination. Returns trip ID, destination, fare, date, and map URL. Supports `profile_type` (PERSONAL/BUSINESS) filter and pagination via `next_page_token`. Max 50 per page.
- **`get_trip`** — Full trip details: driver info, pickup/dropoff addresses, fare breakdown, distance, duration, vehicle type, surge pricing, map URL, and rating.

### Account
- **`whoami`** — Full user profile: name, email, phone, rating, picture URL, Uber One membership, payment methods, and profiles (personal/business).
- **`check_session` / `login` / `verify_login_code` / `logout`** — rides account trio + OTP verify, bound to `auth.uber.com` (form) and `riders.uber.com` (session check).
- **`check_eats_session` / `login_eats` / `logout_eats` / `eats_health`** — Eats account trio (ubereats.com session; SSO via the same `login` flow) plus page-SDK health (`__ueats.health`).

## Auth — browser-driven (no cookies, no API keys)

The session **is the engine-owned browser profile**. Uber's auth cookies on `.uber.com` / `.ubereats.com` are written by Uber's own Set-Cookie, never extracted, never vaulted, never seen by this app. Every op runs as a same-origin `fetch()` evaluated inside the matching tab via the `browser_session` service (the Exa/Greptile pattern); the cookie rides because the fetch is same-origin, and the real browser supplies the live session, TLS fingerprint, and any anti-bot cookies by construction.

**Two registrable domains (sessions), three host-surfaces (tabs):**

| Tab target | Platform | Wire |
|---|---|---|
| `auth.uber.com` | SSO (rides + Eats) | driven login form — never park rides/Eats URLs here |
| `riders.uber.com` | rides | GraphQL `POST /graphql` |
| `www.ubereats.com` | Eats | RPC `POST /_p/api/{operation}` |

Engine rule: one sticky tab per **host**; cookies still share `.uber.com` /
`.ubereats.com`. `navigate(target, url)` requires matching hosts.

### Signing in (Exa-style — agent barely thinks)

```
uber.login()  →  account
              or auth_challenge { continueWith: verify_login_code, retrieval: … }
verify_login_code(email, code, method)  →  account (metadata.uber.lastAuthMethod set)
```

1. Resolve `uber@…` from args or `credentials.retrieve` (1Password Login for `uber.com`).
2. Drive `auth.uber.com` on the **background** profile: identifier → password via `type_secret` (never enters agent context) → OTP channel.
3. Channel order: account `metadata.uber.lastAuthMethod` first, then **email → SMS (if a Messages provider exists) → WhatsApp**.
4. Channel is **hard**: after clicking Email/SMS the screen must be OTP entry and match that channel, or login errors (`ChannelNotConfirmed` / `ChannelMismatch`). Challenge stamps `requestedAt` / `expiresAt` (10 min).
5. Return `auth_challenge` (`kind: code_sent`) with a `retrieval` hint — agent reads **only that channel**, confirms with the human if multiple codes are in flight, then `verify_login_code(method=<same channel>)`.
6. `verify_login_code` is fail-closed: must be on OTP entry at `auth.uber.com`, `method` must match the pending challenge, wrong/stale codes error instead of typing into the identifier field.
7. Card-digit challenges → headed `login_window` only (lockout risk; never automated).

`check_eats_session` uses draft-list / session cookies — **not** `getUserV1` (that endpoint often 403s on a live session). `logout` / `logout_eats` clear the profile session.

### Native-interface note

A same-origin `fetch()` of `/graphql` (rides) or `/_p/api/{op}` (Eats) is **byte-identical** to what Uber's own React app sends — it's the lowest stable contract that already carries the session. We do **not** reach into Uber's minified JS modules; the wire IS the tap.

### App-level contract headers

The real browser supplies all browser headers (UA, Sec-CH-UA*, Sec-Fetch-*). The app adds only Uber's literal app-level headers — not a fingerprint:

- Rides: `x-csrf-token: x` (literal string, not a rotating token), `x-uber-rv-session-type: desktop_session`
- Eats: `x-csrf-token: x`

Three rides GraphQL operations: `CurrentUserRidersWeb` (profile), `Activities` (trip history), `GetTrip` (trip detail).

## Uber Eats (in progress)

Uber Eats uses a **completely different API** from rides. It's NOT GraphQL — it's an RPC-style API at `www.ubereats.com/_p/api/`.

**Page helper.** Shipped `page/eats.js` + `page/eats.cart.js` install `window.__ueats` in the Eats tab (page-local SDK). Python injects both before every Eats op. Reads: `session` / `health` / `listOrders` / `getOrder` / `enrichItems`. Writes: `addToCart` / `previewCheckout` / `checkout` / `getCarts` / … — Python maps to AgentOS shapes and keeps the human gate before place-order. RE toolkit is authoring-only.

**Shopping provider.** `list_deliveries` `@provides("order_history")`; Shopping's live fan-out picks it up next to Amazon with zero app changes. Detail opens via `services.order_history {verb: get_order}` → `get_order` (alias of `get_delivery`).

**Order detail (2026-07-15).** Prefer `getReceiptByWorkflowUuidV1` with `contentType: "JSON"` — `receiptData` is a JSON string (`cart[].Items[{Title,Quantity,UnitPrice.AmountE5,…}]`). HTML (`WEB_HTML`) remains an in-page `DOMParser` fallback — not Python/lxml. `eats_health` / `__ueats.health` reports session + receipt JSON readiness.

### Discovery (2026-04-02)

Used CDP network capture (`browser.navigate` + `browser.eval` fetch interceptor) to navigate `ubereats.com/orders` in Brave and capture all API calls. Key findings:

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

Read: `check_eats_session`, `get_eats_profile`, `list_deliveries` (**`@provides("order_history")`** — Shopping fans this out), `get_delivery` / `get_order`, `get_messages`, `list_nearby_stores`, `search_stores`, `search_products`, `get_store`, `get_item_customizations`, `search_address`, `list_addresses`.

Write: `add_to_cart` (supports `dining_mode=PICKUP|DELIVERY`), `set_dining_mode`, `get_cart`, `preview_checkout`, `clear_cart`, `checkout`, `track_delivery`.

### Ordering flow (MANDATORY for any agent placing a real order)

Placing a pizza-to-the-pizza-place order once was hilarious. Twice would be
embarrassing. Follow this sequence every time, in this order:

**Never browser-navigate bare `/checkout`.** Uber is multicart (one draft per
store). Opening `/checkout` without the draft UUID is a footgun — the UI can
flash “All items removed” while the draft is still fine. Always place via
`checkout({draft_order_uuid})` after `preview_checkout` + explicit human go.

1. **Find the store.** `search_stores({query})` or use a past order's
   `store.id` from `list_deliveries` / `get_delivery`.
2. **Get the menu.** `get_store({store_uuid})`. The returned `offers[]` items
   carry a hidden `_raw` field with the full catalog payload — `add_to_cart`
   requires it, so pass `offers` items through directly; don't reconstruct.
3. **Customizations (if any).** `get_item_customizations({store_uuid, item_uuid})`.
   The app now auto-resolves section/subsection UUIDs when omitted.
4. **Delivery address (delivery only).** `list_addresses()` → choose by
   **`source == "SAVED"` + `label == "HOME"`** first. If `SAVED` is empty
   (common — Uber treats pasted/searched addresses as SUGGESTED until the user
   explicitly saves them), prompt the user to pick from the SUGGESTED list.
   **Never auto-pick a SUGGESTED entry.** For **pickup**, skip this — pass
   `dining_mode: "PICKUP"` to `add_to_cart` (no address required).
5. **Build the cart.** `add_to_cart({store_uuid, items, dining_mode, delivery_address_uuid?})`.
   Pickup flips the draft via `updateDraftOrderV2` after create.
6. **Pre-checkout checklist — SHOW THE USER via `preview_checkout`, then wait.**
   Pass **`draft_order_uuid` explicitly** when multiple carts exist. Surface all of:
   - **Mode**: PICKUP or DELIVERY
   - **Store**: name + address (+ distance for pickup)
   - **ETA**: pickup/delivery window
   - **Items**: name, customizations, quantity, price each
   - **Delivery address** (delivery only): full address **and deliveryNotes**
   - **Payment method** — always `payment.display` (e.g. `Personal: American Express ••••2005 (AMEX)`). Never ask to place with a bare UUID or a null display; `preview_checkout` errors with `PaymentDisplayMissing` if Uber omitted the label.
   - **Fare breakdown + total**
7. **Only then** call `checkout({draft_order_uuid})` after explicit human go.
   Do not call `checkout()` without an explicit "place it" from the user.
   Pin the **exact** draft UUID — never improvise a browser place-order click.
8. **Track.** `track_delivery()` / `list_deliveries` (active orders are prepended
   so Shopping sees them live). `track_delivery` polls `getActiveOrdersV1`.
   Returns driver, eta, and polyline traces — but **only while the order is
   active**. Once delivered/closed, driver + vehicle info are gone from Uber's
   API (privacy). `get_delivery` on a completed order returns store + items +
   fare but no driver; on an in-flight order it falls back to `track_delivery`
   (receipt isn't ready yet).

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

**`SESSION_EXPIRED` / `{authenticated: false}` / NeedsAuth from any op** — no live Uber session in the AgentOS **background** profile (your daily browser login does not count). Call `uber.login` / `login_eats` → pull the OTP via the `retrieval` hint → `verify_login_code`. Card-digit challenges need a headed `login_window`. Run `check_session` / `check_eats_session` to confirm.

**`rtapi.forbidden` on `getUserV1`** — ignore for auth. That endpoint often 403s on a live session; `check_eats_session` uses draft-list / cookies instead. A real logout shows `401` on `getDraftOrdersByEaterUuidV1`.

**`invalid_uuid` / 404 on `getMenuItemV1`** — `get_item_customizations` needs both `section_uuid` and `subsection_uuid`. The app auto-fetches them from `getStoreV1` when omitted; if you see this error again, the item UUID itself is stale or wrong.

## Reverse Engineering Notes

### Tools used

- **`agentos browse request uber`** — authenticated HTTP request with full header visibility. Used to verify cookie auth and inspect response headers.
- **`agentos browse cookies uber`** — cookie inventory showing all `.uber.com` cookies with timestamps and provenance.
- **`agentos browse auth uber`** — auth resolution trace showing which provider won (brave-browser) and identity (agentos@contini.co).
- **`browser.navigate` + `browser.eval`** — CDP network capture. Drove the engine-owned Brave to `ubereats.com/orders` and read the resource list / injected a fetch interceptor, capturing all `/_p/api/` calls with full headers and POST bodies.

### How to extend

**Step 1: Capture network traffic with CDP**

```bash
# Drive the engine-owned browser to any Uber Eats page, then read the wire
agentos call apps '{"op":"run","params":{"app":"browser","tool":"navigate","params":{"target":"ubereats.com","url":"https://www.ubereats.com/store/costco/..."}}}'

# browser.eval performance.getEntriesByType('resource') — or inject a fetch
# interceptor — to see /_p/api/ POST requests with response bodies
```

**Step 2: Extract full API surface from JS bundles**

Don't just capture what one page loads — extract ALL endpoint names from the client JS:

```bash
# Find the main bundle URL from the captured resource list
# Then grep for API endpoint patterns
curl -s "https://www.ubereats.com/_static/client-main-*.js" \
  | grep -oE 'get[A-Z][a-zA-Z]+V[0-9]+' | sort -u   # read endpoints
curl -s "https://www.ubereats.com/_static/client-main-*.js" \
  | grep -oE '[a-z]+[A-Z][a-zA-Z]+V[0-9]+' | sort -u | grep -v '^get'  # write endpoints
```

This revealed 32 endpoints (22 read, 10 write) that weren't visible from a single page capture. The pattern `{verb}{Entity}V{version}` is consistent across all Uber Eats endpoints.

**Step 3: Test individual endpoints**

Use `agentos browse request` or direct `curl` to test specific endpoints. The auth headers and cookie domain are documented in [dev/requirements.md](./dev/requirements.md).

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
