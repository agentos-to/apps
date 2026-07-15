---
id: whatsapp
name: WhatsApp
description: Full WhatsApp presence via live WhatsApp Web — read, send text and media, react, show typing, search the full server-side history
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

- **Brave Browser** installed (the engine runs its own HEADLESS instance on a
  dedicated background profile at `~/.agentos/browsers-bg/brave`)
- **One-time link**: on first use the QR is read *headless* off web.whatsapp.com
  and returned as a scannable text artifact — scan it with your phone (Settings
  → Linked Devices). The session persists in the engine-owned background
  profile; ops return `NeedsAuth` until linked. No browser window ever opens —
  WhatsApp is the reference headless connector (its payload surfaces in the
  Messaging app, so no window is the right surface — CLAUDE.md rule 19).

## Linking (for the agent)

When ops return `NeedsAuth`, the session has expired or was never linked. The QR
kind of the login protocol — headless, no window:

1. `whatsapp.login` — reads the linked-device QR off the page (`div[data-ref]`)
   and returns an `auth_challenge{kind: "qr"}` whose `artifact` is the QR as a
   scannable Unicode block (`qr.text(ref)`). No window opens.
2. The human scans the artifact (WhatsApp phone → Settings → Linked Devices →
   Link a Device).
3. Poll `whatsapp.check_session` until it returns `authenticated: true`.

The session then persists in the engine-owned background profile — no re-link
needed across engine restarts. (Contrast the `login_window` kind — Outlook,
Instagram — which flips this same profile *headed* for a sign-in that can't be
scanned; WhatsApp needs no window because its QR is text.)

## IDs

All ids are WhatsApp JIDs: `15125555309@c.us` (person), `…@g.us` (group).
Every op that takes a chat accepts a JID **or a fuzzy name substring**
("vibe coders" finds the group).

## Common Tasks

- **Active chats:** `list_conversations` (non-archived, most recent first;
  skips local new-chat stubs with no `__x_t` — same as WhatsApp's own list)
- **Archived chats:** `list_conversations` with `archived: true`
- **Unread messages:** `list_messages` with `is_unread: true`
- **Chat history:** `list_messages` with `conversation_id` — pages earlier
  history into memory until `limit` is reached
- **Status stories:** `list_posts` — WhatsApp Status (24h stories) as
  `post` rows (`postType: story`); brokered as `feeds` for the Social app
- **One story + media:** `get_post` with a status message id — hydrates
  image/video into the blob store (`attaches[0].path`)
- **Group members:** `list_persons` with `conversation_id` (opens the chat
  to trigger WhatsApp's lazy participant load — takes a few seconds)
- **Send:** `send_message` with `to` + `text`; pass `reply_to` (a
  serialized message id) to quote/reply to that message
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
- **A send that parks (`SendFailed`, "parked at ack 0") while
  `check_session` stays healthy means the tab's session socket died** —
  classically because a second `web.whatsapp.com` tab claimed the session
  (WhatsApp Web is single-tab). The failure is typed and fast:
  `send_message`/`send_media` bound the inner wire promise with a 15s
  race (well inside the 45s eval timeout) and return `SendFailed`
  carrying the live diagnosis (parked `ack`, `Socket.state`, `Conn.ref`)
  instead of burning the eval timeout as UNKNOWN. Read the diagnosis
  knowing the state fields lie in both directions: `Socket.state` reads
  `CONNECTED` on a dead wire, and `Conn.ref` is `false` even on a healthy
  tab — the parked `ack: 0` is the tell. Duplicates can no longer be
  minted by the engine: `browser_session` target resolution enforces a
  single-tab invariant per target in the engine-owned profiles
  (find-or-reuse by adopted tab id + registrable domain, extras closed on
  attach, a wedged `/json` is an error rather than an empty tab list).
  Recovery from a dead socket: `browser_session` `verb: reload` for
  `web.whatsapp.com` — a fresh page load re-negotiates the session.
  Closing only a duplicate does NOT hand the socket back to the surviving
  tab (verified live 2026-07-06); a fresh load of the sole tab does.
  Engine restarts don't clear a steal on their own (the background
  browser survives them). Was `issue-whatsapp-tab-steal-silent-send-park`
  on the product board.

## Entity Model

- **person** — the human, with name and phone from WhatsApp contacts
- **account** — their WhatsApp identity (JID), on `person.accounts` /
  `message.from` / `conversation.participant`
- **conversation** — a chat thread (`isGroup`, `isArchived`, `unreadCount`);
  `image` is a blob-store path + `mimeType` when a profile thumb is warm
  (fetched in-page from `ProfilePicThumb`, staged via `blobs.put`)
- **message** — `content`, `published`, `isOutgoing`, `author`,
  `conversationId`, `type`
- **post** — a Status story item (`postType: story`, `expiresAt` =
  published+24h, `authorId` / `ringTotal` / `ringUnread` for the rail,
  `mediaType`, `viewed`). About-bio text is NOT a post.

## Internals (for maintainers)

Payloads use WhatsApp Web's module system: `WAWebCollections` (Chat / Msg /
Contact / **Status**), `WAWebChatLoadMessages.loadEarlierMsgs({ chat })` (object arg —
bare model throws `waitForChatLoading undefined`), `WAWebSendMsgChatAction.
addAndSendMsgToChat`, `WAWebSendReactionMsgAction.sendReactionToMsg`,
`WAWebCmd.Cmd.openChatAt({ chat })` (`Cmd` is nested under the module export,
not a top-level export; call shape is `{ chat }`, not bare model),
`WAWebMsgKey.newId/fromString` (send ids),
`WAWebUserPrefsMeUser.getMaybeMePnUser` (login probe — the phone WID;
`getMaybeMeLidUser` is its LID twin),
`WAWebFindChatAction.findOrCreateLatestChat(wid)` (send-side chat
creation for a JID with no existing thread — returns `{chat}`; the bare
`Chat.find(wid)` throws `findImpl is not a function` on current builds),
`WAWebChatStateBridge.sendChatStateComposing/Recording/Paused(wid)`
(typing), `WAWebPresenceChatAction.sendPresenceAvailable/Unavailable()`
(online dot), `Msg.search(query, page, count, remote)` (server-side
search — positional args, 1-based page, `remote` = chat JID string or
undefined), **Status stories** via `WAWebCollections.Status.getModelsArray()`
(each model is one author's ring; `status.msgs` holds the items — wwebjs
`getBroadcasts` / `Broadcast`), send/revoke later via
`WAWebSendStatusMsgAction` / `WAWebRevokeStatusAction`, and the media-send pipeline `WAWebMediaOpaqueData.
createFromData → WAWebPrepRawMedia.prepRawMedia → WAWebMediaStorage.
getOrCreateMediaObject → WAWebMmsMediaTypes.msgToMediaType →
WAWebMediaMmsV4Upload.uploadMedia → mediaData.set(entry) → spread
`mediaData.toJSON()` into the full message construct`. Model fields
carry the `__x_` prefix. If WhatsApp ships a breaking Web update, the
whatsapp-web.js project is the reference for re-deriving module names
and call shapes. Clone it locally for fast grep access — no install,
no execution:

    git clone https://github.com/pedroslopez/whatsapp-web.js ~/dev/vendor/whatsapp-web.js

When bindings drift: `git -C ~/dev/vendor/whatsapp-web.js pull`, then
grep `src/util/Injected/Store.js` and `src/util/Injected/Utils.js` for
the current call shapes. `git log --oneline --since="2 weeks ago" -- src/util/Injected/` surfaces what changed and when.

**Linked-device branding** (`login` applies this before pairing): the
phone's Linked Devices entry derives from `WAWebBrowserInfo()` at
registration — `name` maps to the platform icon via a fixed enum
(Chrome / Firefox / Opera / Safari / Edge; anything else renders the
gray "?" + "Other device"), `os` is the parenthetical. The headless UA
(`HeadlessChrome/…`) parses as "Chrome Headless", which is outside the
enum — so an unbranded pairing registers as "Other device". `login`
swaps the module's `defaultExport` in `__debug.modulesMap` (note:
`window.require()` serves `defaultExport`, not `exports`) so the entry
registers as "Google Chrome (AgentOS)". The name is minted server-side
once at pairing and cannot be changed after; re-link to re-brand.

Drift traps already survived (patterns to keep):

- **`getMeUser()` was deleted (~2026-07 Web refresh)** — the me-user probe
  is `getMaybeMePnUser()` (phone WID) / `getMaybeMeLidUser()` (LID). The
  old symptom was the nastiest kind: every op reported `auth_required` /
  `authenticated: false` for a perfectly linked session (the prelude
  conflated "no me-user" with "logged out"). The prelude now separates
  them: QR on screen → `NeedsAuth`; live Store with no me-user →
  `BindingDrift`, loud.
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
- **`loadEarlierMsgs` and `openChatAt` both take `{ chat }` objects**, not
  bare chat models. Passing the model directly causes `waitForChatLoading
  undefined` (loadEarlierMsgs) or `Cannot read properties of undefined
  (reading 'id')` (openChatAt) — both are the same drift: the function
  signature changed from a positional model arg to a destructured options
  object. `Cmd` itself is nested: `const { Cmd } = window.require('WAWebCmd')`.
- **`__debug.modulesMap` only enumerates LOADED modules** —
  `window.require` lazy-loads on demand. A module missing from the map
  (e.g. `WAWebDownloadManager`) may still require() fine; probe by name
  before concluding drift.
- **Status ≠ About bio.** `Contact.__x_status` / `WAWebContactStatusBridge`
  is the profile "Hey there!" string. Stories live on
  `WAWebCollections.Status` (`status@broadcast` / `isStatusV3`). Expiry is
  computed (`unixTime - status.t > 86400`), not a stored field —
  `list_posts` stamps `expiresAt = published + 24h`. `status.msgs` is
  usually warm after sync; `loadMore` is a no-op stub on current builds.
  `msg.viewed` + `sendReadStatus` exist for view receipts (not wired yet).
  Author faces: warm `ProfilePicThumb.get` first; cold authors use
  `ProfilePicThumb.find(WidFactory.createWid(jid))`, then in-page fetch +
  `blobs.put` (same staging path as chat-list avatars).
