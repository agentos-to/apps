---
id: austin-boulder-project
services:
  - http
name: Austin Boulder Project
description: Class schedules and bookings for the Austin Bouldering Project gym
color: "#1e3a2f"
website: "https://boulderingproject.portal.approach.app"
---

# Austin Boulder Project

Class schedules and booking for the [Austin Bouldering Project](https://austinboulderingproject.com) — Texas's premier bouldering and fitness gym with locations in Springdale and Westgate (plus other Bouldering Project cities on the same Tilefive tenant).

Built on the **Tilefive** platform (`approach.app`), authenticated via **AWS Cognito**.

## Setup

No credentials needed to view the schedule — `get_schedule` is fully public.

To book classes, run the `login` tool once. Credentials are resolved in
this order:

1. **Caller-supplied** — `run login '{"email":"...", "password":"..."}'`.
2. **Credential providers** — an installed `@provides("login_credentials")`
   app matched on `.approach.app` (1Password, macOS Keychain, etc.).
3. **`NeedsCredentials`** — structured error when neither path resolves,
   telling the agent what domain and fields it needs.

On success, the app runs AWS Cognito `USER_PASSWORD_AUTH` and stashes
`{email, password, idToken, refreshToken}` in the credential store.
`login` returns a full `account` (`authenticated`, `at`, `identifier`).
Authed tools refresh near-expiry IdTokens via Cognito
`REFRESH_TOKEN_AUTH` (~1h TTL) and read the token from `params.auth`.

## Locations

| Name | ID |
|---|---|
| Austin Springdale | `6` (default) |
| Austin Westgate | `5` |

Each `place` from `get_locations` carries Tilefive's per-location
`timeZone` (mapped to `place.timezone`) — e.g. Austin locations are
`America/Chicago`, Seattle locations are `America/Los_Angeles`. Never
hardcoded in the plugin.

## Activity IDs

| Activity | ID |
|---|---|
| Climbing Classes | `4` |
| Yoga | `5` |
| Fitness | `6` |

## Examples

```js
// Next 3 days of classes at Springdale (default)
// "today" = OS user timezone; day windows = location timezone
run({ app: "austin-boulder-project", tool: "get_schedule" })

// Full week — auto-paginates /cal (no silent pageSize=50 truncate)
run({ app: "austin-boulder-project", tool: "get_schedule", params: {
  days: 7
}})

// One specific day, yoga only, at Westgate
run({ app: "austin-boulder-project", tool: "get_schedule", params: {
  date: "2026-03-18",
  days: 1,
  location_id: 5,
  activity_ids: "5"
}})

// Book a class (use id from get_schedule) → reservation
run({ app: "austin-boulder-project", tool: "book_class", params: {
  booking_instance_id: 826115
}})

// List my upcoming class reservations
run({ app: "austin-boulder-project", tool: "get_my_bookings" })

// Cancel (reservationId + booking instance id from book / list)
run({ app: "austin-boulder-project", tool: "cancel_booking", params: {
  booking_instance_id: 826115,
  reservation_id: 123456
}})
```

### Commons services (`@provides`)

| Service | Tool | Auth |
|---|---|---|
| `reservation_availability` | `get_schedule` | public |
| `reservation_get` | `get_class` | public |
| `reservation_locations` | `get_locations` | public |
| `reservation_create` | `book_class` | portal account |
| `reservation_list` | `get_my_bookings` | portal account |
| `reservation_cancel` | `cancel_booking` | portal account |

ABP exposes fitness **classes** as bookable units on these verbs. The
Reservations Commons app fans them out — it never names this plugin and
never speaks `class_*`.

### Schedule → `class[]`

`get_schedule` returns `class` entities (Tilefive BookingInstance /
cal). Each carries `startDate` / `endDate` in UTC plus `timezone` from
the location's provider `timeZone`, and a `location` place stub.

Capacity fields:
- `capacity` — max registrants (from `event.maxCustomers`)
- `customerCount` — currently reserved
- `spotsRemaining` = `capacity - customerCount`
- `isFull` — convenience flag when `spotsRemaining == 0`

Enrichment:
- `image` — `event.imageURL` (class series photo), else activity avatar
- `performer` — instructor as `person` (Tilefive `staff` when filled;
  otherwise parsed from the `w/` title suffix). Instructor photos are
  **not** on `/cal` today — see `requirements.md`.
- `activityType` — from `event.activities[]` (Tilefive spelling; legacy
  `activitys` still accepted)

### Book / list / cancel → `reservation` / `reservation[]`

`book_class`, `get_my_bookings`, and `cancel_booking` emit the
`reservation` shape with:

| Field | Value |
|---|---|
| `reservationType` | `"fitness_class"` |
| `reservationId` | portal reservation id |
| `status` | `confirmed` / `cancelled` / … |
| `startTime` / `endTime` (also `startDate` / `endDate`) | class window |
| `timezone` | location `place.timezone` (Tilefive `timeZone`) |
| `availableActions` | `["cancel"]` when confirmed |
| `conditions.cancel` | 24h full refund; day-of until 1h before start |
| `location` | place stub for the gym |
| `event` | class stub (booking instance) |

`get_my_bookings` defaults to upcoming/active only. Pass
`include_past: true` for history, `include_raw: true` for Tilefive
payloads. `book_class` enriches sparse POST responses from the booking
instance so name/times/location are filled in.

Schedule discovery stays on `class`; the commitment is the `reservation`.

## Memberships

`get_my_memberships` filters to `status="active"` by default. Pass
`include_expired: true` to see historical rows (cancelled annuals,
old prepaid memberships, etc.) — useful for "what have I bought
before?" queries.

`book_class` auto-picks the caller's first active membership when
`membership_id` isn't supplied. For multi-membership accounts, pass
the explicit id returned by `get_my_memberships`.

## Technical Notes

See `requirements.md` for full reverse-engineering notes on the Tilefive API.

Key discoveries:
- `Authorization` header on the widgets API is the namespace string (`boulderingproject`), not a JWT
- `httpx` with `http2=True` is required — CloudFront WAF uses JA4 TLS fingerprinting that blocks urllib/requests
- Cognito auth uses `IdToken` (not `AccessToken`) for portal API calls; refresh via `REFRESH_TOKEN_AUTH` when the JWT is within ~2 min of `exp`
- Portal connection declares `Authorization: .auth.idToken` (raw token, no Bearer)
- `/cal` is auto-paged; incomplete collection raises `IncompleteSchedule`
- The widgets `/cal` response uses `customerCount` (not `ticketsRemaining`) for current fullness; capacity lives on `event.maxCustomers`
- Location timezone is Tilefive `timeZone` on `/locations` (and on each BookingInstance) — stamp onto `place` / `class` / `reservation`; do not invent a brand-wide default
- Cancel policy (from confirmation email): cancel ≥24h before start for a full class-credit refund; day-of cancel allowed until 1h before start
