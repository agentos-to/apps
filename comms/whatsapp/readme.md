---
id: whatsapp
name: WhatsApp
description: WhatsApp messages, contacts, and sending via live WhatsApp Web
services:
  - blobs
color: "#2CD46B"
website: "https://www.whatsapp.com/"
product:
  name: WhatsApp
  website: https://whatsapp.com
  developer: Meta Platforms, Inc.
---

# WhatsApp

Read and send WhatsApp messages through a live WhatsApp Web tab. Ops run as
JS payloads in the engine-owned Brave instance via the `browser_session`
service — the engine holds the CDP session; this app never sees the
protocol. WhatsApp's own code decrypts everything; the payloads read the
in-page Store collections.

## Requirements

- **Brave Browser** installed (the engine launches its own instance with a
  dedicated profile at `~/.agentos/browsers/brave`)
- **One-time link**: on first use the engine opens web.whatsapp.com — scan
  the QR code with your phone (Settings → Linked Devices). The session
  persists in the engine-owned profile; ops return `NeedsAuth` until linked.

## IDs

All ids are WhatsApp JIDs: `15125555309@c.us` (person), `…@g.us` (group).
Every op that takes a chat accepts a JID **or a fuzzy name substring**
("vibe coders" finds the group).

## Common Tasks

- **Active chats:** `list_conversations` (non-archived, most recent first)
- **Archived chats:** `list_conversations` with `archived: true`
- **Unread messages:** `list_messages` with `is_unread: true`
- **Chat history:** `list_messages` with `conversation_id` — pages earlier
  history into memory until `limit` is reached
- **Group members:** `list_persons` with `conversation_id` (opens the chat
  to trigger WhatsApp's lazy participant load — takes a few seconds)
- **Send:** `send_message` with `to` + `text`
- **Send media:** `send_media` with `to` + `path` (a blob-store path) +
  optional `caption`; `ptt: true` sends an ogg/opus file as a voice note
- **React:** `send_reaction` with `emoji` + either `chat` (latest
  message) or `message_id` (that exact message)
- **Show typing:** `send_typing` with `chat` right before a real send
  (`kind: recording` for the mic indicator, `paused` to clear)
- **Online dot:** `set_presence` with `state: available | unavailable`
- **Deep search:** `search_messages` — WhatsApp's own server-side
  search, full history; scope with `conversation_id`, walk pages with
  `page`
- **Mark read:** `mark_read` with `conversation_id` — read receipt +
  badge clear on every device. Reading a chat on the user's behalf
  isn't finished until this runs.

## Behavior notes

- `search_messages` runs **server-side** — the same index the Web UI's
  search box hits, so results reach years back regardless of what's
  loaded in memory. WhatsApp matches words/prefixes, not substrings.
- `send_media` sends only from the engine's blob store: inbound media
  hydrated by `get_message` is already there; stage new bytes with
  `blobs.put`. Same 10MB eval-channel cap as inbound hydration, in the
  opposite direction. Voice notes (`ptt: true`) need ogg/opus input.
- Byte fidelity: WhatsApp's prep pipeline **re-encodes images** (sha
  changes, pixels survive — verified 367×206 in/out); ogg/opus voice
  notes pass through **byte-identical** (sha-verified round trip).
- `send_typing` is honesty, not theater — fire it only when a real
  send follows. WhatsApp's own decay clears it; `kind: paused` clears
  it explicitly.
- First op after an engine restart is slow (~10-30s): browser launch + page
  load + Store init. Warm ops run in well under a second.
- Media messages map with `type` (`image`, `video`, `ptt`, …) and use the
  caption as `content` (never the preview thumbnail WhatsApp stores in the
  body field). **`get_message` hydrates the payload**: the decrypted bytes
  land in the engine blob store and the message returns with an attached
  file entity (`attaches[0].path` is the on-disk file, typed `image` /
  `video` / `sound` / `file`, deduped by content hash). `list_messages`
  and live `watch` messages stay caption-only — re-read one message to
  pull its payload. Payloads over 10MB stay un-hydrated (the bytes cross
  the eval channel as one base64 JSON value; the worker caps a line at
  16MB).
- Meta AI responses (`rich_response` type) have their text extracted from
  response fragments automatically.
- `send_message` returns the sent message entity (same shape as
  `list_messages` rows) once WhatsApp's server acks the send — a failed
  send is a `SendFailed` error, never a silent success.
- `send_reaction` reports `dispatched`, not delivered: WhatsApp Web gives
  a headless tab no client-side echo for reactions. Check a phone if
  delivery matters.
- `watch` is durable: it survives page reloads, session drops, browser
  restarts (the engine re-installs the hook and reconnects with backoff),
  and engine restarts (the intent persists on the graph; boot re-arms it).
  Arm once, ever.
- Chats are `@lid`-keyed (WhatsApp's post-2026 chat ids); groups stay
  `@g.us`. `list_persons` resolves LIDs to names + phone JIDs via Contacts.

## Entity Model

- **person** — the human, with name and phone from WhatsApp contacts
- **account** — their WhatsApp identity (JID), on `person.accounts` /
  `message.from` / `conversation.participant`
- **conversation** — a chat thread (`isGroup`, `isArchived`, `unreadCount`)
- **message** — `content`, `published`, `isOutgoing`, `author`,
  `conversationId`, `type`

## Internals (for maintainers)

Payloads use WhatsApp Web's module system: `WAWebCollections` (Chat / Msg /
Contact), `WAWebChatLoadMessages.loadEarlierMsgs`, `WAWebSendMsgChatAction.
addAndSendMsgToChat`, `WAWebSendReactionMsgAction.sendReactionToMsg`,
`WAWebCmd.openChatAt`, `WAWebMsgKey.newId/fromString` (send ids),
`WAWebUserPrefsMeUser.getMeUser` (login probe),
`WAWebChatStateBridge.sendChatStateComposing/Recording/Paused(wid)`
(typing), `WAWebPresenceChatAction.sendPresenceAvailable/Unavailable()`
(online dot), `Msg.search(query, page, count, remote)` (server-side
search — positional args, 1-based page, `remote` = chat JID string or
undefined), and the media-send pipeline `WAWebMediaOpaqueData.
createFromData → WAWebPrepRawMedia.prepRawMedia → WAWebMediaStorage.
getOrCreateMediaObject → WAWebMmsMediaTypes.msgToMediaType →
WAWebMediaMmsV4Upload.uploadMedia → mediaData.set(entry) → spread
`mediaData.toJSON()` into the full message construct`. Model fields
carry the `__x_` prefix. If WhatsApp ships a breaking Web update, the
whatsapp-web.js project is the reference for re-deriving module names
and call shapes.

Drift traps already survived (patterns to keep):

- **Unset model fields are truthy sentinel objects**
  (`{sentinel: 'DEFAULT VALUE PLACEHOLDER'}`), not undefined. Never branch
  on truthiness — the helpers' `str()` / `Number.isFinite` / `=== true`
  guards exist for this.
- **Sends need the full message construct** (`WAWebMsgKey` id, `from`/`to`
  Wids, `t`, `self: 'out'`, `isNewMsg`, `local`) — a minimal `{body, type}`
  builds an empty husk that never reaches the wire. And
  `addAndSendMsgToChat` resolves to `[msg, sendPromise]`: only awaiting the
  *inner* promise surfaces wire success/failure.
- **Two message collections**: the global `Msg` collection sees loaded +
  synced messages; each chat's own `chat.msgs` is the authoritative
  per-chat list (locally-sent messages land there first).
- For media, `__x_body` holds the preview thumbnail base64 — text lives in
  `__x_caption` only.
- **`sendSeen` takes an options object** (`{chat, threadId?,
  afterAvailable?}`), NOT the bare chat model whatsapp-web.js passes —
  passing the model throws `Cannot read properties of undefined
  (reading 'markedUnread')`.
- **Headless tabs defer read receipts**: `Stream.available` is false
  while the tab is hidden, and `sendSeen` parks the receipt until the
  tab becomes visible — which a headless tab never does. Pass
  `afterAvailable: false` to send through the unavailable stream
  immediately (`mark_read` does).
- **`unreadCount: -1` is the manual marked-unread flag**, not a count.
  The UI's mark-as-read clears `chat.markedUnread = false` *then* sends
  seen; `sendSeen` alone early-returns while the flag is set.
- **Media download is `WAWebDownloadManager.downloadManager
  .downloadAndMaybeDecrypt({directPath, encFilehash, filehash, mediaKey,
  mediaKeyTimestamp, type, signal, downloadQpl})`** → decrypted
  ArrayBuffer. `downloadQpl` accepts a chainable mock (`addAnnotations`/
  `addPoint` returning `this`). The higher-level `downloadMsg` path
  resolves the mediaObject but parks the bytes out of reach
  (`mediaBlob` stays null, `contentInfo.staticUrl` empty in headless).
- **`__debug.modulesMap` only enumerates LOADED modules** —
  `window.require` lazy-loads on demand. A module missing from the map
  (e.g. `WAWebDownloadManager`) may still require() fine; probe by name
  before concluding drift.
