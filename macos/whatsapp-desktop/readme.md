---
id: whatsapp-desktop
name: WhatsApp Desktop
description: Read local on-device WhatsApp history from the macOS app's SQLite database, plus import full-history media (photos, videos, voice notes) from the phone's plaintext iCloud backup — read-only, no browser, no key needed
services:
  - sql
  - crypto
  - http
  - blobs
color: "#2CD46B"
website: "https://www.whatsapp.com/"
product:
  name: WhatsApp Desktop
  website: https://whatsapp.com
  developer: Meta Platforms, Inc.
---

# WhatsApp Desktop

Reads the native WhatsApp macOS app's on-device SQLite database directly —
no browser session, no login, no on-demand sync limit. Complements the live
`whatsapp` app (which tops out at messages synced to the web session). This
app reaches full local history plus calls and voice notes.

## Requirements

- **WhatsApp Desktop** (Mac Catalyst version) installed and having synced at
  least once — the database lives at
  `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`
- **Full Disk Access** granted to the AgentOS engine (System Settings →
  Privacy & Security → Full Disk Access). Without it the SQLite open fails silently.

## IDs

Conversations use `Z_PK` integers (Core Data row IDs). Every tool that accepts
`conversation_id` also accepts a **fuzzy name substring** — `"vibe coders"`
resolves to the chat whose `ZPARTNERNAME` contains that string. JIDs are exposed
on messages but are not used as primary IDs here.

## Tools

### `list_conversations`
All chats, ordered by most-recent message. Pass `archived: true` for archived
chats. Returns `id`, `name`, `jid`, `isGroup`, `lastMessage`, `unreadCount`.

### `list_messages(conversation_id, limit=200, include_system=false, since=None, until=None, order="desc")`
Messages in a conversation. `conversation_id` is a `Z_PK` or a fuzzy name
substring. `since` / `until` are inclusive ISO date/datetime bounds (e.g.
`"2024-01-01"`) — pull one period of a relationship in a single call. `order` is
`"desc"` (newest first, default) or `"asc"` (oldest first — read from the
beginning). Returns:

| Field | Notes |
|---|---|
| `id` | ZWAMESSAGE.Z_PK |
| `content` | Text body; null for media-only rows |
| `published` | ISO datetime (Apple epoch + 978307200 → Unix) |
| `isOutgoing` | true = sent by you |
| `author` | JID string for incoming messages |
| `authorName` | Resolved display name (ZPARTNERNAME for 1:1; push-name for groups) |
| `typeCode` | Raw ZMESSAGETYPE integer |
| `typeLabel` | Human label: `text`, `image`, `audio`, `video`, `voice-note`, `link`, `sticker`, `deleted`, etc. |
| `mediaPath` | Absolute on-disk path if media is locally cached; null if cloud-only |
| `sizeBytes` | File size from DB metadata |
| `mime` | Inferred from file extension (e.g. `audio/ogg; codecs=opus`) |
| `durationSecs` | Duration for audio/video (ZMOVIEDURATION); null if not cached |
| `durationMin` | Same, in minutes |

System message types (6, 14, 15, 19, 28, 46, 75, 76) are filtered by default.
Pass `include_system: true` to see them.

### `summarize_conversation(conversation_id, n=15, include_system=false)`
**The relationship-arc tool — one call instead of fetch-500-and-parse.**
`conversation_id` is a `Z_PK` or fuzzy name. Returns:

| Field | Notes |
|---|---|
| `conversationId` / `name` | Resolved `Z_PK` + `ZPARTNERNAME` |
| `firstMessage` / `lastMessage` | ISO timestamps of the first and last message — the arc bounds |
| `messageCount` | Total non-system messages in the thread |
| `callCount` | Calls in `CallHistory.sqlite` whose partner matches this chat's contact (same partner-match as `list_calls`) |
| `firstN` / `lastN` | The oldest `n` and newest `n` messages, both in chronological order |

### `list_calls(limit=200, conversation_id=null)`
Call history from `CallHistory.sqlite` as interval events, newest first.

| Field | Notes |
|---|---|
| `callId` | ZWACDCALLEVENT.ZCALLIDSTRING |
| `start` | ISO datetime |
| `durationMin` | Call duration in minutes (0 for missed) |
| `durationSecs` | Raw seconds |
| `kind` | `voice` or `video` |
| `status` | `answered`, `missed`, `declined`, `ringing`, or `group-offered` |
| `partnerJid` | JID of the other party |
| `partnerName` | Resolved display name |
| `isIncoming` | true = they called you |
| `bytesSent` / `bytesReceived` | Data used (may be 0) |

### `list_voice_notes(conversation_id=null, limit=200)`
Voice notes — `ZMESSAGETYPE = 3` (`.opus`). Covers both downloaded notes and
ones not yet pulled to disk (`mediaPath` null, but downloadable while their URL
is live — see **Media download & decryption**). Type 59 is excluded (it's calls).

| Field | Notes |
|---|---|
| `id` | ZWAMESSAGE.Z_PK |
| `conversationId` | Chat this note belongs to |
| `start` | ISO datetime |
| `kind` | Always `audio` |
| `authorName` | Sender display name |
| `author` | Sender JID |
| `isOutgoing` | true = you sent it |
| `mediaPath` | Absolute path if downloaded (`.opus` file); null if not yet downloaded |
| `fileExists` | Whether the file is actually on disk |
| `sizeBytes` | File size |
| `mime` | `audio/ogg; codecs=opus` for .opus; `audio/mp4` for .m4a |
| `durationMin` | Duration from DB (ZMOVIEDURATION); null when not downloaded |

**Voice notes are type 3** (`.opus`). When downloaded, `mediaPath` points to
`<group-container>/Message/Media/<lid>/<c1>/<c2>/<uuid>.opus`. When not yet
downloaded, `mediaPath` is null but `ZMEDIAURL` + `ZMEDIAKEY` are present — see
**Media download & decryption** below.

> **Type 59 is NOT a voice note.** It is a *call-event* row — the ChatStorage
> mirror of a leg in `CallHistory.sqlite` (`ZWAMESSAGE.ZSTANZAID` equals the
> call's `ZCALLIDSTRING`; `ZGROUPEVENTTYPE` carries the call status, 2=answered
> 5=missed). It has no media. Earlier docs misclassified it as "PTT" — it isn't.

### `download_media(message_id)`
Download + decrypt a message's media to disk, returning the on-disk path. Works
for any media kind (voice notes, audio, video, image, document). Fully
offline + session-free — fetches the encrypted CDN blob and decrypts it via the
engine's `crypto.hkdf` + `crypto.aes` (the full mechanism is **Media download &
decryption** below).

`message_id` is a `ZWAMESSAGE.Z_PK`. Returns `status`:

| `status` | Meaning |
|---|---|
| `already_on_disk` | WhatsApp already cached it; `path` is its location, no fetch. |
| `downloaded` | Fetched + decrypted just now; `path` is in the engine blob store. |
| `expired` | The URL signature lapsed (~25-day window); can't fetch offline. Open it in the WhatsApp app (or the live `whatsapp` connector) to refresh, then re-call. |

Other fields: `path`, `mime`, `sizeBytes`, `kind`, `messageId`.

> **Large-media limit.** The decrypted bytes round-trip to the app worker as
> hex over its stdin, which has a line-size cap — a ~50 MB video overflows it
> (`stdin read error: chunk exceed the limit`). Voice notes and images are tiny,
> so this never bites them; multi-MB video is the edge. Proper fix (future):
> fetch→decrypt→store entirely engine-side so bytes never enter Python.

### `search_messages(query, limit=200)`
Full-text search across all chats. Returns messages containing `query` in
`ZTEXT`.

### `get_message(id)`
Single message by `Z_PK`.

### `diag(table=null, db=null)`
Schema inspection — `PRAGMA table_info` for a table, or list all tables.
Useful for exploring the DB. `db` defaults to `ChatStorage.sqlite`; pass
`CallHistory.sqlite` to inspect the calls DB.

## Type codes

| Code | Label | Notes |
|---|---|---|
| 0 | text | Plain text |
| 1 | image | JPEG/PNG |
| 2 | audio | Non-PTT audio |
| 3 | video | Video, and `.opus` **voice notes** (the real PTT format) |
| 4 | contact | vCard share |
| 5 | location | GPS pin |
| 6 | system | _(filtered by default)_ |
| 7 | link | Link preview |
| 8 | gif | Animated GIF |
| 9 | document | PDF / file |
| 10 | call-voice | Voice call row |
| 11 | call-video | Video call row |
| 14 | deleted | Deleted message _(filtered)_ |
| 54 | poll | Poll |
| 59 | call-event | Call leg (mirrors CallHistory; **not** a voice note) |
| 66 | sticker | Sticker |

## Database layout

Two SQLite files, both under the group container
(`~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/`):

```
ChatStorage.sqlite
  ZWACHATSESSION      — conversations
  ZWAMESSAGE          — messages (ZTEXT, ZMESSAGETYPE, ZFROMJID, …)
  ZWAMEDIAITEM        — media metadata (ZMEDIALOCALPATH, ZMOVIEDURATION, ZFILESIZE, ZMEDIAURL, ZMEDIAKEY)
  ZWAPROFILEPUSHNAME  — JID → display name map

CallHistory.sqlite
  ZWACDCALLEVENT      — one row per call leg
  ZWAAGGREGATECALLEVENT — outcome flags (missed, video, incoming)
```

Media files on disk: `<group-container>/Message/Media/<lid>/<c1>/<c2>/<uuid>.ext`
`ZMEDIALOCALPATH` is relative to `<group-container>/Message/`.

**`ZWAMESSAGE` holds the full message history, not a recent window** — verified
~41.5k messages reaching back to 2015. If a deep-history read comes up short,
it's the default `limit=200`, not a retention cap: raise `limit` or pass
`since`/`until`. (The on-disk `Media/` tree is larger, ~55k files, because it
also holds blobs whose DB rows have aged out — but the *text* history is complete.)

## Media download & decryption

WhatsApp media (voice notes, images, video, audio, docs) is **end-to-end
encrypted at rest on Meta's CDN**, and the local DB stores everything needed to
fetch and decrypt it **with no live session** — until the URL's signature
expires.

### The model

When a contact sends media, their device AES-encrypts the file and uploads the
opaque `.enc` blob to `mmg.whatsapp.net`. The **download URL and the decryption
key travel inside the E2E message**; your device persists both into
`ZWAMEDIAITEM`:

- `ZMEDIAURL` — a **pre-signed** CDN URL. `oh=` is an HMAC signature, `oe=` is
  the expiry (unix time, **hex**). The CDN authenticates the *URL*, never the
  caller — anyone holding it pulls the ciphertext until `oe` passes (~25-day
  window). After expiry only the stable `directPath` remains; re-signing it
  requires the live WhatsApp session (not available offline).
- `ZMEDIAKEY` — a **protobuf**, not raw bytes. Field 1 = the 32-byte `mediaKey`.
  (Field 2 = `fileEncSha256`.)

### Decrypt algorithm (WhatsApp standard, verified byte-exact)

```
mediaKey  = protobuf_field_1(ZMEDIAKEY)              # 32 bytes
expanded  = HKDF_SHA256(mediaKey, 112, info)         # info per media type:
            #   "WhatsApp Audio Keys"  (voice notes / audio)
            #   "WhatsApp Video Keys"  (video, incl. some mp4 stored as type 2)
            #   "WhatsApp Image Keys"
            #   "WhatsApp Document Keys"
iv        = expanded[0:16]
cipherKey = expanded[16:48]                           # macKey = [48:80], unused for read
ciphertext, mac = enc[:-10], enc[-10:]                # last 10 bytes are the MAC
plaintext = AES_256_CBC_decrypt(cipherKey, iv, ciphertext)  # then strip PKCS7
```

HKDF is plain HMAC-SHA256 expansion (stdlib `hmac`/`hashlib`, no deps). The
`info` string is keyed off the media type, **not** the file extension —
WhatsApp sometimes stores a video under `ZMESSAGETYPE=2` (the on-disk ext
reveals the truth). Choosing the wrong `info` yields garbage that still has the
right length, so validate against a known-downloaded item when in doubt.

### Practical reach

- **Already downloaded** (`ZMEDIALOCALPATH` set, file on disk) → just return the
  path. WhatsApp aggressively auto-downloads recent media.
- **Not downloaded + URL live** (`oe` in the future) → fetch + decrypt offline.
  This is the window the download tool fills (currently ~222 live URLs).
- **Not downloaded + URL expired** → the signature is dead. Open the message in
  the WhatsApp app to refresh+download it (it writes `ZMEDIALOCALPATH`), then it
  falls into the first case. Voice-note backlog is almost entirely here, because
  recent ones auto-download before they'd ever be a fetch target.

## Importing media from the iCloud backup

The Desktop db above only holds the recent slice synced to this companion — the
*deep* history (years of photos/videos/voice notes) lives only in the phone's
**iCloud backup**. macOS syncs that backup to disk as dataless stubs under
`~/Library/Mobile Documents/57T9237FN3~net~whatsapp~WhatsApp/Accounts/<phone>/backup/`.
Its media archives — `Media.tar`, `Video.tar`, `Document.tar`, `GIFs.tar` — are
**plaintext POSIX tars** organised by conversation. Only the message-text db
(`ChatStorage.sqlite.enc`) is encrypted (per-account key, server-escrowed — not
reachable without re-registering the number). So media from any chat, full
history, extracts with **no key and no WhatsApp session**.

Three tools:

- `backup_status` — list the backup(s) on this Mac: each archive's size,
  whether it's hydrated (bytes local) or still an iCloud stub, and
  encrypted-vs-plaintext. Metadata only; never downloads.
- `hydrate_backup {media, video, documents}` — `brctl download` the chosen
  archives (Media/Video ≈ 4–5 GB each). No auth — the OS already holds the
  iCloud session. Run before importing.
- `import_backup_media {conversation, include_video}` — resolve the
  conversation (Z_PK or fuzzy name, e.g. `"Alice"`) to its folder ids via the
  Desktop db, pull every matching file out of the plaintext tars, and land each
  content-addressed in the blob store. Returns `file[]` (sha-identified, deduped
  by bytes). Raises `NeedsHydration` if a needed tar is still a stub.

### Conversation → tar-folder id mapping

The tars key folders by chat id, but the *format differs by era*: the
full-history `Media.tar` keys DMs by **phone-JID** (`15551234567@s.whatsapp.net`),
while recent media uses the **`@lid`** privacy id. The Desktop db is the Rosetta
Stone — `ZWACHATSESSION` carries both `ZCONTACTJID` (phone-JID) and
`ZCONTACTIDENTIFIER` (the `@lid`); the import tool matches *both*. Groups key by
`@g.us` in every era.

### Hydration & eviction

`hydrate_backup` materialises the tars; they can be `brctl evict`ed back to
stubs afterward to reclaim disk (re-hydratable anytime). A tar can also be
silently re-evicted by macOS under storage pressure — `import_backup_media`
detects the dataless state and tells you to re-hydrate rather than failing
opaquely.
