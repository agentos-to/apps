# Austin Boulder Project — Reverse Engineering Notes

This file documents the API contract discovered via Playwright network capture.
The portal is a React SPA built on the **Tilefive** platform (`approach.app`).

---

## Status & What's Next

### ✅ Done
- `get_schedule` — widgets `/cal`, auto-paged (no silent `pageSize=50` truncate); returns `class[]`
- `get_locations` — Tilefive `timeZone` → `place.timezone` (no brand-wide Austin constant)
- `login` / `check_session` / `logout` — Cognito `USER_PASSWORD_AUTH` + `REFRESH_TOKEN_AUTH`; login returns full `account`
- `book_class` / `cancel_booking` / `get_my_bookings` — emit `reservation` / `reservation[]` with `reservationType: fitness_class`
- `get_my_memberships` / `get_my_passes` — shape-typed, account + location stubs
- `discover_config` — dynamic API key + Cognito config from portal bundle
- Portal connection: `auth.header.Authorization = .auth.idToken` (raw Cognito IdToken)
- Commons services: `@provides("reservation_availability"|"reservation_get"|"reservation_locations"|"reservation_create"|"reservation_list"|"reservation_cancel")` — ABP maps Tilefive classes onto these; the Reservations app never names `class_*`
- Class enrichment: `event.imageURL` → `class.image`; instructor via `performer` person (staff when present, else `w/` title parse); `activities` (not legacy `activitys`)

### Gap — instructor photo
Tilefive `/cal` returns `staff: []` / `staffHasBooking: []` on BookingInstance
rows (verified Jul 2026). Class series photos land on `event.imageURL`
(Firebase). Activity category avatars land on `activities[].imageURL`.
There is **no per-instructor headshot** on `/cal` today — we do not fake
one. When Tilefive starts filling `staff` with `imageURL`, `_staff_person`
already maps it onto `person.image`.

### 📋 Needs live credentials
- `login` via 1Password — local vault unlock (`onepassword.setup_local_unlock` → AgentOS Security), then `credentials.retrieve(".approach.app")` / `plugins.run login` (grant scoped to `login_credentials` / `.approach.app`). No 1Password “Integrate with other apps” toggle.
- After login: `get_my_memberships`, optionally book if Joe asks

### Notes
- Earlier MCP spawn / `working_dir` issues are resolved (Python-first plugin load).
- `days=7` used to silently truncate at 50 bookings; now pages or raises `IncompleteSchedule`.

---

## Portal URL

```
https://boulderingproject.portal.approach.app
```

Namespace slug used in all API calls: `boulderingproject`

---

## Discovered API Endpoints

### 1. Region Lookup ✅ (works without auth, no origin check)
```
GET https://portal.api.prod.tilefive.com/region?namespace=boulderingproject
→ { "AVAILABLE_REGIONS": ["us-east-1"], "DEFAULT_REGION": "us-east-1" }
```

### 2. Account Config ✅ (works without auth, no origin check)
```
GET https://portal.api.prod.tilefive.com/accounts/boulderingproject
→ { displayName, sections, styles, scheduleView: "week", ... }
```
Active sections: `bookings`, `schedule`, `memberships`, `passes`, `waivers`, `giftcard`

### 3. Locations ✅ (working from Python)
```
GET https://widgets.api.prod.tilefive.com/locations
→ [ { id, UUID, name, address1, city, state, timeZone, ... }, ... ]
```
Austin locations:
- **Austin Springdale** — `id: 6`, `UUID: bd3709e9-a27c-11ed-ae87-0a21e3900363`, tz: `America/Chicago`
- **Austin Westgate**  — `id: 5`, `UUID: b859f96e-a27c-11ed-ae87-0a21e3900363`, tz: `America/Chicago`

### 4. Location Settings ✅ (working from Python)
```
GET https://widgets.api.prod.tilefive.com/locationsettings/{locationId}/portal
→ {
    locationId: 6,
    section: "PORTAL",
    setting: {
      featuredMemberships: true,
      featuredPasses: true,
      membershipTypeIds: [418],
      passTypeIds: [307],
      showAllMultidayBookings: true
    }
  }
```

### 5. Activities (category list) ✅ (confirmed via browser)
```
GET https://widgets.api.prod.tilefive.com/activities?
→ {
    data: [ { id, name, description, imageURL, isActive, isPublic }, ... ],
    pagination: { limit: 250, offset: 0, pageCount: 1, rowCount: 15 }
  }
```
Relevant activity IDs for Austin Springdale:
- `4` = Climbing Classes
- `5` = Yoga
- `6` = Fitness (also id 6 used in embed URL `categoryIds=4,5,6`)

### 6. 🏆 Schedule / Cal Endpoint ✅ (confirmed via browser — NO AUTH NEEDED)
```
GET https://widgets.api.prod.tilefive.com/cal
  ?startDT=2026-03-17T05:00:00.000Z
  &endDT=2026-03-18T04:59:59.999Z
  &locationId=6
  &activityId=4%2C5%2C6
  &page=1
  &pageSize=50

→ {
    bookings: [ <BookingInstance>, ... ],
    calEvents: [],
    pagination: { page: 1, pageCount: 1, pageSize: 50, rowCount: 7 }
  }
```

#### BookingInstance shape (full class data):
```json
{
  "id": 826115,
  "UUID": "56d7a5ed-2d98-4fac-a49e-a48fcc89f82d",
  "calendarId": 79,
  "eventId": 20732,
  "name": "Flow w/Todd C",
  "startDT": "2026-03-17T21:00:00.000Z",
  "endDT": "2026-03-17T22:00:00.000Z",
  "occurrenceDate": "2026-03-17",
  "status": "active",
  "ticketsRemaining": 0,
  "customerCount": 2,
  "maxNumOfGuests": null,
  "cutOffTimeInHours": 0,
  "cutoffStartDT": "2026-03-17T21:00:00.000Z",
  "locationId": 6,
  "timeZone": "America/Chicago",
  "event": {
    "id": 20732,
    "name": "Flow w/Todd C",
    "description": "...",
    "duration": "01H00M",
    "maxCustomers": 40,
    "entranceRequirement": "MP",
    "entranceFee": 0,
    "billingType": "fcfs",
    "locationId": 6,
    "parentId": 1382,
    "calendarId": 79,
    "rrule": "DTSTART;TZID=America/Chicago:20231107T160000\nRRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=TU;WKST=SU\n",
    "startTime": "16:00",
    "timeZone": "America/Chicago",
    "rollingBookingInDays": 60,
    "lastGeneratedBookingDate": "2026-05-12T21:00:00.000Z",
    "activitys": [ { "id": 5, "name": "Yoga" } ],
    "ticketTypes": [],
    "pricingTiers": []
  },
  "location": { "id": 6, "name": "Austin Springdale", ... }
}
```

Key fields for booking:
- `id` — booking instance ID (use this to book)
- `ticketsRemaining` — spots left (0 = full)
- `entranceRequirement: "MP"` — likely "Membership/Pass" required
- `billingType: "fcfs"` — first come first served

### 7. Carts ✅ (seen in browser capture)
```
GET https://widgets.api.prod.tilefive.com/carts/{cart-uuid}
```
Guest cart UUID appears to be auto-created per session.

### 8. Marketing Settings
```
GET https://widgets.api.prod.tilefive.com/marketing/settings/
```
Not relevant for booking.

---

## Widgets API — Required Headers ✅ SOLVED

All three required headers for `widgets.api.prod.tilefive.com`:

```
X-Api-Key: <widgetsApiKey from bundle>    ← tenant API key (exact casing)
Authorization: boulderingproject          ← namespace/tenant ID (NOT a JWT!)
Origin: https://boulderingproject.portal.approach.app
```

### Authorization header — the tricky part

This is NOT a bearer token. The app bundle contains this function:

```js
Jl = () => {
  const { host, protocol, hostname } = window.location;
  if (localhost || file:// || 192.168.x.x)  return "alpha1";
  return host.split(".")[0];   // → "boulderingproject"
}
```

Then the widgets axios client is created as:
```js
Fe = async () => ({
  baseURL: si.widgetsApiRoot[region],
  headers: {
    Authorization: Jl(),         // ← "boulderingproject" (the subdomain)
    "X-Api-Key": si.widgetsApiKey[region]
  }
})
```

The API Gateway uses this to route to the correct tenant. When a user IS logged in,
the authenticated portal API uses a real Cognito `IdToken` as `Authorization` instead.

### X-Api-Key — how to find it

1. Load the portal in a browser (or Playwright)
2. Get the main bundle URL from the HTML — it looks like `/assets/app-HASH.js`
3. Fetch that bundle and search for the string `widgetsApiKey`
4. It appears in an object like:
   ```js
   widgetsApiKey:{"us-east-1":"<40-char alphanumeric key>","ap-southeast-2":"<key>"}
   ```

Regex that extracts it:
```python
re.search(r'"widgetsApiKey"\s*:\s*\{[^}]*"us-east-1"\s*:\s*"([^"]{30,})"', bundle_text)
# OR (minified variant):
re.search(r'widgetsApiKey:\{"us-east-1":"([^"]{30,})"', bundle_text)
```

Key format: ~40 alphanumeric characters, starts with `OQ2z4Q...` (as of Mar 2026).

### Why urllib/requests failed (but httpx works)

`requests`/`urllib3` only advertises `http/1.1` in the TLS ALPN extension.
CloudFront WAF uses JA4 fingerprinting which includes ALPN as a field.
~98% of real browser traffic is HTTP/2+, so ALPN=http/1.1 is a bot signal.
`httpx` with `http2=True` advertises `h2, http/1.1` — matching browsers.
See `abp.py` `_fetch()` for implementation.

### Bundle access note
The bundle `app-HASH.js` redirects to portal HTML when fetched by most tools.
`discover_config()` in `abp.py` tries a direct fetch with browser-like headers,
falling back to hardcoded constants if that fails.

The bundle also exposes:
- `widgetsApiRoot.us-east-1` = `https://widgets.api.prod.tilefive.com`
- `apiRoot.us-east-1`        = `https://portal.api.prod.tilefive.com`
- `approachApiRoot`           = `https://app.api.prod.tilefive.com`

---

## Authentication

### Method
**AWS Cognito** — confirmed by:
- App bundle loads `/assets/aws-BmFRG873.js` (AWS Amplify/SDK)
- Region endpoint returns `us-east-1`
- Login form is React SPA: email + password fields, no HTML `<form>` tag

### Cognito Config ✅ FOUND (extracted from app bundle)

Found alongside `widgetsApiKey` in the main bundle, under `aws:`:
```js
aws:{userPoolId:"us-east-1_XXXXXXXX",userPoolClientId:"<26-char alphanumeric>", ...}
```

Regex:
```python
re.search(r'userPoolId\s*:\s*"(us-east-1_[A-Za-z0-9]+)"', bundle_text)
re.search(r'userPoolClientId\s*:\s*"([A-Za-z0-9]{20,60})"', bundle_text)
```

Current values (as of Mar 2026): `us-east-1_x871N...` / `jikhc095m6r9...`

Expected Cognito request:
```
POST https://cognito-idp.us-east-1.amazonaws.com/
X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth
Content-Type: application/x-amz-json-1.1

{
  "AuthFlow": "USER_PASSWORD_AUTH",
  "ClientId": "<client_id>",
  "AuthParameters": { "USERNAME": "<email>", "PASSWORD": "<password>" }
}
```

Expected response:
```json
{
  "AuthenticationResult": {
    "AccessToken": "...",
    "IdToken": "...",
    "RefreshToken": "...",
    "ExpiresIn": 3600
  }
}
```

### Login Flow (Browser UX)
1. Page load → location picker dialog (radio `value="{location_id}"`) → SAVE
2. Login form appears: `Email *` + `Password *` + `SIGN IN` button
3. Submit → Cognito `InitiateAuth` → tokens returned
4. Tokens used as `Authorization: Bearer <AccessToken>` on authenticated endpoints

---

## Booking Flow ✅ (endpoints confirmed from bundle)

The booking flow is simpler than the cart flow — it's a single POST call.

### How to book

```
POST https://portal.api.prod.tilefive.com/bookings/{bookingInstanceId}/customers
Authorization: {Cognito IdToken}     ← NOT AccessToken, NOT "Bearer ..."
Content-Type: application/json
Origin: https://boulderingproject.portal.approach.app

{ "customerId": <id>, "numGuests": 0, "membershipId": <id> }
```

`bookingInstanceId` = the `id` field from the `/cal` response (e.g. `826115`)

**AgentOS mapping:** schedule discovery stays `@returns("class[]")`
(`/cal` BookingInstance). `book_class` / `get_my_bookings` /
`cancel_booking` emit `reservation` with `reservationType:
"fitness_class"`, `reservationId` = portal reservation id, times +
`timezone` from the location's Tilefive `timeZone` (via place), and
`availableActions: ["cancel"]` when confirmed.

### Authorization: IdToken (not AccessToken)

The authenticated portal client (`Ie()` in the bundle) uses the Cognito **IdToken**:
```js
bI = async () => (await zE()).tokens?.idToken
Ie = async () => {
  if (loggedIn) { headers = { Authorization: await bI() } }
  return axios.create({ baseURL: apiRoot, headers })
}
```
→ Pass `auth["IdToken"]` from `login()`, not `auth["AccessToken"]`.

### How to cancel

```
DELETE https://portal.api.prod.tilefive.com/bookings/{bookingInstanceId}/reservations/{reservationId}
Authorization: {Cognito IdToken}
```

`reservationId` comes from the `book_class()` response.

### Other authenticated endpoints (from bundle)

```
GET /customers/memberships    ← active memberships
GET /customers/passes         ← active class passes
GET /customers/bookings       ← upcoming bookings (path inferred, needs confirmation)
GET /bookings/{id}/customers  ← who's booked into a class
```

### Entrance requirements

Classes with `entranceRequirement: "MP"` require an active membership or pass.
Use `get_my_memberships()` / `get_my_passes()` to check before booking.
Error response when requirement not met is unknown — needs live capture.

### Cart flow (for paid bookings / passes — not needed for free class bookings)

The bundle also has cart endpoints (via `Fe()` = widgets API):
```
PUT /carts            ← create/update cart
GET /carts/{uuid}     ← get cart
PUT /carts/session/{id}/cards  ← add payment card
```
Used for purchasing memberships and passes, not for free class registration.

---

## Schedule Embed URL Pattern

The public schedule embed (no login required in browser):
```
https://boulderingproject.portal.approach.app/schedule/embed?categoryIds=4%2C5%2C6
```
Category IDs: `4` = Climbing Classes, `5` = Yoga, `6` = Fitness

The embed calls `/cal` with date range and `activityId=4,5,6`.
Time offsets suggest UTC — Austin is UTC-5 (CST) or UTC-6 (CDT).
The embed uses `startDT` at `05:00:00Z` = midnight CST.

---

## Key Data IDs

| Thing | Value |
|-------|-------|
| Namespace | `boulderingproject` |
| Austin Springdale location id | `6` |
| Austin Springdale UUID | `bd3709e9-a27c-11ed-ae87-0a21e3900363` |
| Austin Westgate location id | `5` |
| Austin Westgate UUID | `b859f96e-a27c-11ed-ae87-0a21e3900363` |
| Climbing Classes activity id | `4` |
| Yoga activity id | `5` |
| Fitness activity id | `6` |
| Featured membership type id | `418` |
| Featured pass type id | `307` |
| Calendar id (Springdale) | `79` |
| AWS region | `us-east-1` |
| Payment processor | `fullsteam` |
