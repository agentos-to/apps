---
id: switchyards
name: SwitchYards
description: Book phone booths and club rooms at SwitchYards; membership dues via Stripe portal
services:
  - browser_session
  - http
color: "#1a1a1a"
website: "https://www.switchyards.com"
product:
  name: SwitchYards
  website: https://www.switchyards.com
---

# SwitchYards

Member coworking clubs — book phone booths / meeting rooms (Skedda) and
read membership billing (Stripe Customer Portal — WIP).

> **Before extending this app** (dev only), read:
> 1. [Browser-Driven Connectors](browser-driven on the system volume)
> 2. [dev/requirements.md](./dev/requirements.md) — Skedda / Stripe portal RE notes
>
> **Layout** — runtime = `readme.md` + `switchyards.py`; RE = `dev/` (never injected).

**Auth:** engine **background browser profile** (not OS cookie jars).
First-time: email + `POST /venueusers`. Returning Skedda userprofile:
`login` → headed `app.skedda.com`. Mutations stay in-tab
(`browser_session`). Places/reservations use `at` → SwitchYards
`organization` so they land on the graph when remembered.

**Public reads** (no member session): venues, spaces, floorplans, free slots.
Venue hosts are discovered from `www.switchyards.com/rooms` — never a
hardcoded city/host table.

## Locations

Clubs/spaces/tags/maps come from Skedda at runtime (`/webs`) — **never
hardcode** `HYP`/`EAT`/asset ids/map ids. Venue timezone from
`venue.timeZoneId` → `place.timezone`.

## Setup

1. Add a 1Password Login for `app.skedda.com` (after first passworded
   account) — `login` pulls it via `credentials.retrieve`
2. Or first-time: `create_reservation` with member `email` / `first_name` /
   `last_name` (passwordless venueuser)
3. `check_session` is true when `/webs` has a `venueuser` / `userId`

Billing portal (`membership.switchyards.com`) is a second session — see
`dev/requirements.md` (not wired yet).

## Tools

| Tool | Auth | Returns |
|---|---|---|
| `check_session` / `login` / `logout` | venueuser | `account` / `auth_challenge` |
| `list_locations` | public | `place[]` (venues + club tags); caches catalog on plugin `cache.locations` (24h; `refresh=true` or Commons → Clear cache) |
| `list_spaces` | public | `place[]` (booths / rooms) |
| `list_floorplans` | public | `floorplan[]` |
| `get_availability` | public | free-slot `event[]` |
| `get_bookable` | public | one slot `event` |
| `get_my_reservations` | venueuser | `reservation[]` |
| `create_reservation` | venueuser (auto-register first time) | `reservation` |
| `update_reservation` | session | `reservation` (`PUT /bookings/:id`) |
| `cancel_reservation` | session | `reservation` (`DELETE /bookings/:id`) |
| `check_in` | session | `reservation` (`POST /bookingcheckins`) |

Filters: `venue` (name/city/subdomain substring), `host` (`*.skedda.com`),
`location_id` (venue or club place id).

## Commons services (`@provides`)

| Service | Tool |
|---|---|
| `reservation_locations` | `list_locations` |
| `reservation_availability` | `get_availability` |
| `reservation_get` | `get_bookable` |
| `reservation_list` | `get_my_reservations` |
| `reservation_create` | `create_reservation` |
| `reservation_update` | `update_reservation` |
| `reservation_cancel` | `cancel_reservation` |
| `reservation_check_in` | `check_in` |

Slot ids: `sy-slot:{spaceId}/{start}/{end}` (venue-local naive ISO).

## Notes

- Check-in window: −30 / +10 minutes from start (auto-cancel if missed)
- Alta Open door access is out of scope
