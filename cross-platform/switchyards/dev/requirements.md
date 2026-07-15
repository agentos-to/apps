# SwitchYards — Reverse Engineering Notes

Brand plugin (`switchyards`). Platforms underneath:

| Connection | Host | Role |
|---|---|---|
| Skedda (session) | `switchyardsaustin.skedda.com` | Spaces + reservations + check-in |
| Billing (session) | `membership.switchyards.com` | Stripe Customer Portal — membership / invoices |

Login UI redirects to `https://app.skedda.com/account/login`.

---

## Status

### Done (public / unauth)
- **Browse + Map UI work without login** — availability is public; create/cancel/check-in still need session
- Venue + assets via `GET /webs` (CSRF cookie + `X-Skedda-RequestVerificationToken`)
- Occupied slots via `GET /bookingslists?start=&end=` → `{bookings: [...]}`
- Clubs are **server tags** on a venue (`spacePresentation.spaceTags`) — Austin today has `HYP` / `EAT`; **do not hardcode tag names or space ids in the plugin**. Discover at runtime from `/webs` (+ maps)
- **Venue hosts:** scrape `www.switchyards.com/rooms` for `*.skedda.com` (16 cities today) — never a hardcoded host table
- Asset inventory is dynamic from `assets[]` — Austin snapshot below is RE reference only
- **Floorplans (SVG):** `mapsStructure.maps[]` + `GET {venueMapsUrlBase}{mapId}.json?{lastUpdated}` → `{staticDefs, staticGraphics}` — see Floorplans section
- Rules: check-in −30/+10, 15-min granularity, booth duration denials, quotas
- **Plugin shipped (public):** `list_locations` / `list_spaces` / `list_floorplans` / `get_availability` (free-slot `event[]`) + account trio
- **Commons `@provides`:** `reservation_locations` / `reservation_availability` / `reservation_get` / `reservation_list` / `reservation_create` / `reservation_update` / `reservation_cancel` / `reservation_check_in`

### In progress
- [x] Headed booking path (`login` → venue `/booking`; member book is passwordless venueuser, not `app.skedda.com`)
- [x] Authed create / cancel / check-in request shapes — wired (`POST /bookings`, `DELETE /bookings/:id`, `POST /bookingcheckins`)
- [x] My bookings filter path (`get_my_reservations` — in-tab `/webs` venueuser + `/bookingslists`)
- [ ] Billing portal login + membership/invoice scrape or in-page fetch

### Out of scope
- Alta Open door unlock
- Admin Stripe API keys

---

## Austin venue (public `/webs`)

```
venue.id:          230089
name:              Switchyards Austin
subdomain:         switchyardsaustin
timeZoneId:        America/Chicago   ← stamp on place.timezone; never hardcode
streetAddress:     4403 Guadalupe St (Hyde Park mailing; East Austin is 1408 E 13th)
timeGranularityMinutes: 15
```

### Space tags (`spacePresentation.spaceTags`)

| Tag | Space ids |
|---|---|
| HYP | 1473094–1473098 |
| EAT | 1556314–1556319 |

### Assets → `place`

| id | name | club | kind |
|---|---|---|---|
| 1473094 | Hyde Park Club Room 01 | HYP | meeting_room |
| 1473095–1473098 | Hyde Park Phone Booth 1–4 | HYP | phone_booth |
| 1556314 | East Austin Phone Booth 01 | EAT | phone_booth |
| 1556315 | East Austin Club Room 01 | EAT | meeting_room |
| 1556316–1556318 | East Austin Phone Booth 02–04 | EAT | phone_booth |
| 1556319 | East Austin Club Room 02 | EAT | meeting_room |

Kind from name (`Phone Booth` / `Club Room`). `categories` / `featureType` on place.

### Booking list row (public)

Key fields: `id`, `start`, `end`, `spaces[]`, `venue`, `venueuser`, `title`,
`checkInHistory`, `type`, `price`. Times are **venue-local naive** strings
(`2026-07-15T12:00:00`) — interpret with `venue.timeZoneId`.

### Rules (summary)

- **Check-in:** `windowOpenMinutes: -30`, `windowCloseMinutes: 10`, `action: 2` (cancel), reminder on
- **Phone booths:** deny rules around min 15 / max 60 minutes (expression codes — verify in UI)
- **Club rooms:** daily quota 240 minutes; weekly daytime quota 600 minutes (Mon–Fri 08:00–17:00 venue)

---

## Auth model (live — 2026-07-15)

**Member booking is NOT Skedda password login.** Browse is public; booking is
**email + self-register venueuser** in the booking modal:

1. Enter member email → UI shows `Your details (<email>)` — **no password**
2. Fill first/last (+ check club rules + terms)
3. Confirm → `POST /venueusers` then `POST /bookings`

Member identity is **per user** — pass `email` / `first_name` / `last_name` at
book time (or reuse an existing venueuser session). Do **not** bake a member
email, name, or country into plugin source.

`GET /webs` may still show `userId: null` / empty `useraccount[]` even after
booking — identity for bookings is **`venueuser`** (`id` + `username` email),
not a full Skedda `useraccount`. Detect session via `venueusers` / cookies in
the browser profile after register.

Old `app.skedda.com/account/login` path is optional/admin — **not** the member
book flow we captured.

Do **not** reconstruct cookie jars over `client.get` for mutations — stay
in-tab (`browser_session`).

---

## Create booking (captured live)

Two-step, same CSRF cookie + `X-Skedda-RequestVerificationToken`:

### 1. `POST /venueusers`

```json
{
  "venueuser": {
    "username": "<member email>",
    "firstName": "<member first>",
    "lastName": "<member last>",
    "organisation": null,
    "twoLetterCountryCode": "<venue.cultureTwoLetterCountryCode>",
    "contactNumber": null,
    "termsAgreed": true,
    "registerMetadata": "<venue.publicRegisterPayload>",
    "venueusertags": []
  }
}
```

Response includes `venueusers[0].id` + fresh `antiForgeryToken`.

### 2. `POST /bookings`

```json
{
  "booking": {
    "start": "<venue-local naive ISO>",
    "end": "<venue-local naive ISO>",
    "spaces": ["<space id from /webs assets>"],
    "venue": "<venue.id>",
    "venueuser": "<venueusers[0].id>",
    "type": 1,
    "paymentStatus": 0,
    "price": 0,
    "title": null,
    "checkInAudits": "<from venue.checkInRules>",
    "hideAttendees": true,
    "availabilityStatus": 1,
    "attendees": [],
    "addOns": [],
    "customFields": []
  }
}
```

Success response is singular `{ "booking": { "id": "…" } }` (not `bookings[]`).
`price` is required (`currencyCultured`); omit/null → 422. Free booths: `0`.
`checkInAudits` from `venue.checkInRules.rules[].checkInAudits`.

### Wired in plugin (`create_reservation`)

- `registerMetadata` ← `venue.publicRegisterPayload` (SPA:
  `registerMetadata:e.venue.publicRegisterPayload`)
- `checkInAudits` ← matching `venue.checkInRules.rules[].checkInAudits`
  (spaceIds filter, else first rule)
- `twoLetterCountryCode` ← `venue.cultureTwoLetterCountryCode` (not a constant)
- Skips `/venueusers` when `/webs` already has `venueusers[0]`
- Member email/name from call params / injected account — **never plugin constants**

### Update / extend (SPA `saveBooking` → Ember `updateRecord`)

```
PUT /bookings/:id
{ "booking": { "id", "start", "end", "spaces", "venue", "venueuser", "price": 0, … } }
```

Same CSRF headers as create. Plugin tool: `update_reservation`.

### Cancel (SPA `destroyRecord` → Ember RESTAdapter)

```
DELETE /bookings/:id
```

Same CSRF headers as create. `deleteUserBookingBehavior` query is for
**venueuser** deletion, not booking cancel. 401 if no session.

### Check-in (SPA `_checkInBooking`)

```
POST /bookingcheckins
{
  "bookingcheckin": {
    "bookingId": <int>,
    "occurrenceDate": "<booking.start venue-local>",
    "checkInAudits": "<from venue.checkInRules or null>"
  }
}
```

Window: `venue.checkInRules.rules[].windowOpenMinutes` /
`windowCloseMinutes` (Austin: −30 / +10).

### Returning member session

`GET /userprofilepings?email=` — if `userprofileId > 0`, passwordless
`POST /venueusers` is rejected (“already a user”). Plugin `login` then
uses `credentials.retrieve` (1Password Login for `app.skedda.com`) →
`POST /logins` on `app.skedda.com`, then venue `/booking`.

First-time only: `POST /venueusers` + `publicRegisterPayload`.

---

## Billing portal

URL pattern: `https://membership.switchyards.com/p/login/...` (Stripe Customer Portal).
Separate session from Skedda likely. Map to `membership` + invoices after CDP.

---

## Club place addresses (marketing site)

| Club | Address |
|---|---|
| Hyde Park | 4403 Guadalupe St, Austin, TX 78751 |
| East Austin | 1408 E 13th St, Austin, TX 78702 |

---

## Floorplans — SVG (deep RE)

### Discovery (public, no login)

1. `GET /webs` → `venue.mapsStructure.maps[]` (metadata + hit targets)
2. `web.spaceImagesHost` + `web.venueMapsContainer` → URL base  
   (Austin: `https://staticcontent.skedda.com/venuemaps/`)
3. Artwork: **`GET {venueMapsUrlBase}{mapId}.json?{lastUpdated}`**  
   Bundle: `fetch(\`${venueMapsUrlBase}${id}.json?${lastUpdated}\`)`  
   (`.svg` / `.png` at that path 404 — JSON is the real asset)

### Map metadata (`mapsStructure.maps[]`) — server-discovered

Do **not** hardcode map ids/names. Fields per map:

| Field | Role |
|---|---|
| `id` | Stable map id (blob key) |
| `name` | Club/floor label (e.g. whatever Skedda named it) |
| `viewBox` | SVG viewBox string |
| `mobileRotationDegrees` | UI rotate hint |
| `indicatorSize` | Booking pin size |
| `dynamicRectangles[]` | Hit targets: `{ spaceId, x, y, w, h, indicatorPos }` |
| `lastUpdated` | Cache-buster query on the JSON URL |
| `draft` | Skip drafts for member UI |

Austin snapshot (RE only — will change): Hyde Park + East Austin maps with
rects covering all 11 assets.

### SVG payload (`{mapId}.json`)

```json
{
  "staticDefs": "<style>…</style>…",      // CSS + defs fragment
  "staticGraphics": "<g id=\"Background\">…" // geometry fragment
}
```

Assemble for render (Skedda’s Map view does the same conceptually):

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="{mapsStructure.viewBox}">
  {staticDefs}
  {staticGraphics}
  <!-- overlays: dynamicRectangles as <rect> linked to place/spaceId -->
</svg>
```

Layer `id`s seen on Hyde Park art: `Background`, `Externals_Walls`,
`Internals_Walls`, `Furniture`, `Labels` (Illustrator/Sketch export). East
Austin uses its own layer names — treat as opaque SVG, don’t parse labels.

Sizes (approx): Hyde Park ~133KB JSON / ~122KB assembled SVG; East Austin ~113KB.

### Plugin mapping → `floorplan`

| Skedda | Shape |
|---|---|
| map `id` / `name` / `viewBox` | identity + display |
| `staticDefs` + `staticGraphics` | `layoutSvg` (assembled) and/or keep fragments + `sourceUrl` |
| `dynamicRectangles[]` | resources[] → `{ place id = spaceId, x,y,w,h }` |
| venue / club tag | link to club `place` (tag from `spaceTags`, not hardcoded) |

**Shape:** new `floorplan` (not `venue_floorplan`) sibling of `seatmap`. Prior art: IMDF-lite;
Skedda is SVG + axis-aligned hit rects (not GeoJSON polygons). Prefer storing
assembled SVG (or defs/graphics + viewBox) over raster screenshots.

### Still open

- Exact DOM overlay markup for busy/free coloring (Map UI) — optional for v1
  if we only need static plan + space rects + availability from `bookingslists`
