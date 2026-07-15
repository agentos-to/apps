---
id: call-history
name: Call History
description: Read the macOS call log (FaceTime + phone + group calls) as interval/call events — read-only, no browser needed
services:
  - sql
capabilities:
  - full_disk_access
color: "#34C759"
website: "https://support.apple.com/guide/iphone/"
product:
  name: macOS Call History
  website: https://www.apple.com/macos/
  developer: Apple Inc.
---

# Call History

Surfaces the macOS unified call log — FaceTime audio/video, cellular phone
calls relayed from iPhone, and group calls — as interval/call events. Reads the
Core Data SQLite store directly; no browser, no login, no sync limit. Sibling to
`whatsapp-desktop` (which exposes WhatsApp's own call log).

## Requirements

- **Call History synced** to the Mac (FaceTime + Continuity / iPhone call relay).
  The database lives at
  `~/Library/Application Support/CallHistoryDB/CallHistory.storedata`.
- **Full Disk Access** — engine-brokered (`capabilities: full_disk_access`): a
  missing grant returns a `NeedsCapability` error naming the one-time
  System Settings toggle for AgentOS; relay it to the human.

## IDs

Calls use `Z_PK` integers (Core Data row IDs) as `id` — pass one to `get_call`.
`conversationId` is a hex UUID grouping calls in the same thread; pass it to
`list_calls(conversation_id=...)`.

## Timestamps & enums

Core Data epoch: `ZDATE` is seconds since 2001-01-01; add `978307200` for Unix
time. Enum values were reverse-engineered against the live DB:

| Column | Meaning |
|---|---|
| `ZCALLTYPE` | 1 = phone audio · 8 = FaceTime audio · 16 = FaceTime video → `video` when `& 16` |
| `ZSERVICE_PROVIDER` | `com.apple.FaceTime` → `facetime` · `com.apple.Telephony` → `phone` |
| `ZORIGINATED` | 1 = outgoing, 0 = incoming |
| `ZANSWERED` | set only on **incoming** rows; outgoing-answered calls keep `ZANSWERED=0` with `ZDURATION>0` |
| `ZHANDLE_TYPE` | 2 = phone number · 3 = email / Apple ID |

**Status** derives from both duration and direction: `answered` if
`duration > 0 or ZANSWERED`; otherwise incoming → `missed`, outgoing →
`declined`.

## Tools

### `list_calls(limit=200, since=None, until=None, conversation_id=None, service=None)`
Call events, newest first. `since`/`until` are inclusive ISO date/datetime
bounds; `service` is `"facetime"` | `"phone"`. Returns `{calls: [...]}`:

| Field | Notes |
|---|---|
| `id` | ZCALLRECORD.Z_PK |
| `kind` | `voice` \| `video` |
| `status` | `answered` \| `missed` \| `declined` |
| `start` | ISO datetime (Apple epoch + 978307200 → Unix) |
| `durationSecs` / `durationMin` | Connected talk time; 0 / null for unanswered |
| `isIncoming` | true = they called me |
| `by` | `"me"` for outgoing; the partner handle for incoming |
| `partnerHandle` | Remote handle (phone/email) |
| `partnerName` | Resolved AddressBook name, else stored `ZNAME` |
| `service` | `facetime` \| `phone` |
| `isJunk` | Flagged junk or blocked by a call-directory extension |
| `location` | Carrier/CNAM location string, if known |

### `get_call(id)`
One call by `Z_PK`. All `list_calls` fields plus `uniqueId`, `conversationId`,
`countryCode`, and `participants` — for group calls, every remote handle from
`Z_2REMOTEPARTICIPANTHANDLES` → `ZHANDLE`, each `{handle, name, kind}`.

### `summarize_calls(handle=None, since=None, until=None)`
Relationship arc. `handle` scopes to one person (format-insensitive for phones).
Returns `callCount`, `totalDurationSecs`, `firstCall`, `lastCall`,
`byService` (`{facetime, phone}`), `missedCount`.

## Contact resolution

Handles (`ZADDRESS`) are resolved to names by globbing every macOS AddressBook
source (`~/Library/Application Support/AddressBook/**/*.abcddb`), normalizing
phones to their last 10 digits — the same approach as the `imessage` app.
