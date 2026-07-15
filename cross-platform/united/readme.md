---
id: united
services:
- http
name: United Airlines
description: Flight search, reservations, boarding passes, travel history, and MileagePlus account access
color: '#002244'
website: https://www.united.com
product:
  name: United Airlines
  website: https://www.united.com
  developer: United Airlines, Inc.
test:
  check_session: {}
  get_profile: {}
  get_mileageplus: {}
  list_trips:
    params:
      upcoming_only: true
  search_flights:
    params:
      origin: AUS
      destination: SFO
      depart_date: '2026-04-28'
  select_flight:
    skip: true  # requires a live cart_id from search_flights; destructive (mints held cart)
  register_traveler:
    skip: true  # requires a cart_id from select_flight; commits PII to a held cart
  get_seatmap:
    skip: true  # requires a live cart_id; auto-test can't synthesize
  register_seats:
    skip: true  # destructive — commits a seat to a held cart
  render_seatmap:
    skip: true  # requires a live cart_id
---

# United Airlines

Flight search, booking, reservations, boarding passes, and MileagePlus
account access. **Browser-driven**: every op runs as a same-origin
`fetch()` inside a tab of the engine-owned browser via the
`browser_session` service (the Exa/Greptile pattern). The session is the
browser profile itself — United's auth + Akamai bot-manager cookies on
`.united.com`, written by United's own Set-Cookie, never extracted, never
vaulted. Requests originate from the real browser, so the live session and
every anti-bot cookie ride by construction — which is exactly what defeats
the bot-manager a cookie-replay transport has to fight.

> **Before extending this app** (dev only), read:
> 1. [Browser-Driven Connectors](browser-driven on the system volume) — the pattern
> 2. [dev/requirements.md](./dev/requirements.md) — captured API shapes, endpoint inventory, auth details
> 3. The `reservation`, `flight`, `airline`, `airport`, and `pass` shape YAMLs
>
> **Layout** — runtime = `readme.md` + `united.py`; RE = `dev/` (never injected). `.captures/` is local RE scratch (gitignored).

## Graph model

| Entity | Represents |
|--------|------------|
| **reservation** | A booking — 1+ passengers, 1+ trips, a PNR, payment, fare conditions |
| **trip** | A directed journey — e.g. outbound SFO→EWR of a round-trip |
| **flight** | A single segment (UA 1234 SFO→DEN) within a trip |
| **pass** | An issued ticket / boarding pass — one per passenger per flight |
| **airline** | United as an organization (UA / UAL / "UNITED") |
| **airport** | Origin/destination airports (IATA/ICAO codes, city, timezone) |
| **aircraft** | Equipment type (B789, A321 etc.) |
| **membership** | MileagePlus, Premier status, Club membership — keyed on MP#/CK# |
| **person** | The passenger (legal name, middle name etc.) |
| **account** | united.com login identity (email + customer key) |

Relationships:
- `reservation --at--> airline (United)`
- `reservation --passengers--> person[]`
- `reservation --trips--> trip[]` (one for outbound, one for return)
- `trip --legs--> flight[]` (multi-flight trips have multiple; nonstop has one)
- `reservation --tickets--> pass[]` (one per person per flight)
- `pass --holder--> person` + `pass --for--> flight`

## Features

| Tool | What it does | Status |
|---|---|---|
| `check_session` | Verify the browser-profile session is live (reads `/User/profile` in-tab) | ✅ |
| `login` | Report the live session, or NeedsAuth (sign in once headed) | ✅ |
| `logout` | Sign out of United in the browser profile | ✅ |
| `get_profile` | Name, MP#, titles (person shape) | ✅ |
| `get_contact_info` | Name, DOB, phones, emails, KTN, redress — the canonical source for booking-form prefill | ✅ |
| `get_mileageplus` | Balance + elite tier | ✅ |
| `list_trips` | Upcoming reservations | ✅ |
| `get_cart` | Full cart detail (trips + fare + taxes) from `/api/ShoppingCart/LoadReservationAndCart` | ✅ |
| `search_flights` | One-way and (caveat: outbound only) round-trip flight search | ✅ one-way / ⚠️ round-trip needs CDP priming |
| `select_flight` | Commit a fare to the cart via `/api/flight/RegisterFlights` | ✅ |
| `register_traveler` | POST traveler PII (name / DOB / phone / email / KTN) | ✅ — uses `get_contact_info` defaults |
| `get_seatmap` / `render_seatmap` | Cabin map + ASCII render | ✅ |
| `register_seats` | Commit a seat selection | ✅ |
| `prepare_booking` | Assemble a signed `booking_offer` with ASCII review + resolved billing address + consent flags (save-card, insurance decline) | ✅ |
| `confirm_booking` | **Read-only until /api/ShoppingCart/checkout body is captured.** Gates: HMAC blob, confirm-amount string match, live re-read, card-on-file, explicit consents | ⚠️ awaiting POST body capture |

See [dev/requirements.md](./dev/requirements.md) for the ongoing reverse-engineering
log and the current state of the round-trip cart + checkout body capture
open items.

### Viewing the seat map

`render_seatmap` returns a pre-rendered ASCII cabin diagram. For the
cleanest display across terminals, chat UIs, and LLM echo-back paths,
call it with `view.format=text`:

```bash
agentos call apps '{
  "op": "run",
  "params": {
    "app": "united",
    "tool": "render_seatmap",
    "params": {
      "cart_id": "...",
      "flight_number": 1336,
      "origin": "AUS", "destination": "SFO",
      "departure_datetime": "2026-04-28T13:00",
      "arrival_datetime":   "2026-04-28T15:02",
      "fare_basis_code":    "LAA0AQBN"
    },
    "view": { "format": "text" }
  }
}'
```

The engine wraps the `ascii` field in a fenced ```` ```text ```` block
with a "Reproduce verbatim:" prefix, so the alignment survives markdown
rendering. See [Response shaping](/architecture/response-shaping/) for
the full format contract.

## Setup

Sign in once, headed, in the engine-owned browser. Run `login` (or any
op) — if there's no live session it returns a NeedsAuth error pointing
you at <https://www.united.com/en/us/account/sign-in>. Complete the
sign-in (username + password + MFA) by hand in that browser; United's
Akamai bot-manager challenge clears invisibly because it's a real
browser. The session then lives in the profile and every op rides it —
nothing is extracted, nothing is vaulted, no cookie ever reaches the app.

`check_session` confirms the live session by minting the anonymous-token
bearer in-tab and reading `/xapi/myunited/User/profile` same-origin. It
returns `{authenticated: false}` (not an error) when there's no live
session.

## Cart lifecycle (read before resuming any booking)

United's cart is **not** a 5-minute ecom idle cart. A round-trip
cart with no committed flights survives **at least ~30 min idle**
and probably longer. When resuming a booking flow:

1. **Don't assume the cart is dead.** Try
   `GET /api/ShoppingCart/LoadReservationAndCart?cartId=<id>` (i.e.
   `get_cart(cart_id=...)`) first — 200 = still live.
2. **Reload is cheap.** If the SPA tab shows an empty page but the
   URL has a `cartId=` param, `Page.reload(ignoreCache=true)`
   re-renders the correct slice picker from server-side cart state.
   No new `FetchSSENestedFlights` call needed.
3. **URL lies, DOM tells the truth.** `tripIndex=2` in the URL
   doesn't mean the return picker is rendered — the SPA shows
   whichever slice is next unheld. Read the rendered DOM
   (`UA \d+ \(` spans) to tell which slice you're on.
4. **Pure-Python return-slice search still fails.** Resume an
   existing round-trip cart via the Brave SPA; don't re-invent it
   with `search_flights(cart_id=..., trip_index=2)` — that returns
   0 offers on a zombie cart. See
   [dev/requirements.md "Cart lifecycle"](./dev/requirements.md#cart-lifecycle--observed-behavior-session-4-2026-04-24)
   for the full observed behavior.
5. **Cart id changes on fare commit.** When resuming an old cart
   and clicking Select on a fare variant, United mints a **new**
   cart id and redirects. Re-read `location.href` after every
   commit. The zombie cart is orphaned; further commits flow to
   the new one.

## Fare selection gotchas

- **Basic Economy has a gate.** Clicking a Basic Economy fare card
  opens an inline drill-down with a checkbox *"Basic Economy works
  for me"*. The `Select` button is disabled until the checkbox is
  ticked. Tick, then click Select. See
  [dev/requirements.md "Fare selection mechanics"](./dev/requirements.md#fare-selection-mechanics-session-4-2026-04-24).
- **Basic Economy gate fires on the outbound only.** For a round
  trip where outbound is Basic, the return-slice Basic Economy
  drill-down has **no checkbox** and Select is immediately
  enabled. The consent from the outbound is remembered for the
  whole cart.
- **Prices shown are round-trip totals**, not one-way. On the
  outbound picker each card's price is RT-total pairing that
  outbound with the cheapest available return. On the return
  picker each card's RT-total = outbound + that return's
  incremental cost.
- **Cabin card labels mislead.** The $373 Basic Economy card
  displays as *"From $373 / United Economy / cabin select to view
  fare options"* — the "United Economy" label is the cabin
  *family*, not the fare *variant*. Identify columns by x-coord
  aligned with the column header (`Basic Economy` / `United
  Economy®` / `Economy Plus®` / `United First®`), not the card's
  inner text.

## Standard booking flow — page sequence

The SPA advances through a fixed URL sequence during a round-trip
booking. Each arrow below is a Select/Continue click; a new cart
id is minted on the first fare-commit after resume (see Cart
lifecycle) but stable thereafter.

```
1. /en/us/fsr/choose-flights?f=AUS&t=SFO&d=...&r=...&tripIndex=1
      ├── renders outbound list (RT-total prices)
      ├── [click cabin card] → inline drill-down (same URL)
      ├── [tick Basic-Economy checkbox if Basic]
      └── [click Select] → POST /api/flight/RegisterFlights
            ↓ new cartId in URL
2. /en/us/fsr/choose-flights?...&tripIndex=2&idx=2&cartId=<new>
      ├── renders return list (RT-total prices)
      ├── [click cabin card] → inline drill-down
      └── [click Select] → POST /api/flight/RegisterFlights
            ↓ same cartId; SPA navigates
3. /en/us/traveler/choose-travelers?cartId=<same>&tqp=R
      ├── h1: "Traveler Info"
      ├── renders a "Select a traveler" <select> prefilled with the
      │    MileagePlus owner (read from /xapi/myunited/User/profile);
      │    surname/given/middle/DOB/gender/MP# all auto-filled
      ├── "Frequent flyer program" auto-attaches the profile's
      │    MileagePlus (****941)
      ├── "Traveler contact information" prompts for phone + email;
      │    phone & email prefill from profile when present
      └── [click Continue] → POST /api/ShoppingCart/RegisterTravelers
            ↓ same cartId
4. /en/us/book-flight/customizetravel/<cartId>?tqp=R
      ├── renders seat maps + ancillary upsells (checked bags,
      │    priority boarding, travel insurance, trip bundles)
      ├── Basic Economy: seats are not selectable (auto-assigned
      │    at check-in); page shows a notice + Continue
      └── [click Continue to payment] → SPA nav
5. /en/us/book-flight/checkout/<cartId>?tqp=R
      ├── renders payment form with saved AMEX (****2005) preselected
      ├── renders the "I agree to the terms" box
      └── [click "Agree and purchase"] → POST /api/ShoppingCart/checkout
            ↓ cart becomes PNR; SPA nav
6. /en/us/book-flight/confirmation/<cartId?>
      └── renders confirmation + 6-char PNR
```

The right-rail cart summary (`Total today`, trip list, CO₂ kg) is
present on stages 3–5 so the user can review totals before each
advance. A "$400 Statement Credit" United Explorer card promo
renders on stage 3 as a visual discount in the summary — it does
**not** apply until the user opens that card and is routed to it
via a separate co-brand apply flow.

## Transport

Browser-driven: every op is a same-origin `fetch()` evaluated inside the
united.com tab of the engine-owned browser via the `browser_session`
service. Ops bind to `@connection("none")` — there is no credential to
ride; the session is the browser profile. The `X-Authorization-api`
bearer is minted in-tab via `/api/auth/anonymous-token` (user-scoped
because the cookies are present by construction) and threaded on each
authed request exactly as United's frontend does. SSE endpoints
(`FetchSSENestedFlights`) come back as a non-JSON string body the op
parses. Endpoint inventory lives in [dev/requirements.md](./dev/requirements.md).

## Reverse engineering notes

See [dev/requirements.md](./dev/requirements.md) for captured endpoints and auth
details. Because requests now originate from the real browser tab, the
anti-bot / fingerprint apparatus the cookie-replay transport fought
(custom UA, Sec-* spoofing, http2 flags, hand-assembled cookie headers)
is gone — the tab supplies all of it.
