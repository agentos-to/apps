---
id: imessage
services:
  - shell
  - sql
  - blobs
capabilities:
  - full_disk_access
name: iMessage
description: Read, watch live, and send iMessages/SMS through macOS Messages — a Messaging provider with push inbound and chat.db-verified sends
color: "#34C759"
website: "https://support.apple.com/messages"
product:
  name: iMessage
  website: https://support.apple.com/messages
  developer: Apple Inc.
---

# iMessage

A Messaging provider: `@provides` **chats**, **message_watch**,
**message_send**, **message_send_media**, **message_typing**, **get_message**.
The Messaging app renders these conversations next to WhatsApp's with zero app
changes.

**Media renders inline (2026-07-07).** `get_message` `@provides("get_message")`
and hydrates a message's attachments — images + voice notes (`.caf` → the
voice-note player) — into the content-addressed blob store (`_stage_attachment`
reads the on-disk `~/Library/Messages/Attachments/…` file via `base64` through
`shell.run`, then `blobs.put`; the engine's `/file` serves only the blob store
or a mounted volume, never a raw Attachments path). `_map_message` stamps a
render `type` (image / video / ptt / audio) so the thread shows a media bubble
that lazily hydrates on scroll — same provider-uniform path as WhatsApp/Instagram
(needs `blobs` in the frontmatter `services`). HEIC photos render via the shell's
QuickLook `/thumb`; a raw-bytes lightbox of a HEIC won't display in-browser (a
known follow-up — transcode on hydrate).

Reads come from `~/Library/Messages/chat.db` (read-only SQL). macOS daemons
(imagent / IMDPersistenceAgent) keep it fresh **whether Messages.app is open
or not** — receiving needs no app running. Sends go through the
[imsg](https://github.com/steipete/imsg) CLI (AppleScript into Messages.app,
public APIs only; it launches the app in the background as needed).

## Requirements

- **macOS only**; signed into Messages once (Messages ▸ Settings ▸ iMessage)
- **Full Disk Access** — engine-brokered (`capabilities: full_disk_access`)
- **Automation permission** — the engine controlling Messages.app (one-time
  TCC prompt on first send)
- **imsg CLI** — `brew tap steipete/tap && brew install imsg` (send/typing only;
  reads and watch never touch it)

## Live inbound — `watch`

`watch` arms the engine's `file_watch` transport on the Messages directory:
a chat.db WAL write fires a native FSEvent → the engine dispatches
`pull_changes` → the row delta lands as `message` entities (graph write +
transport-neutral `subscription.entity` observer event — the same event
WhatsApp's browser hook emits). Durable and idempotent: the intent persists
as a `subscription` node and boot re-arms it. Arm once, ever.

`pull_changes` owns a ROWID cursor in app data — replay is impossible; the
first arm stamps the current high-water mark (history is the read path).
Tapback rows and system events are skipped. Inbound latency is sub-second:
FSEvents when it fires, and the transport's 500ms stat probe as the
guarantee (macOS coalesces repeat WAL events — a silent stream is normal).

## Sends are verified against chat.db — never trusted from the CLI

`imsg` exiting 0 proves AppleScript ran, nothing more. `send_message` /
`send_media` poll chat.db (≤8s) for the new outgoing row and return its
mapped entity only when `is_sent = 1` with `error = 0`; anything else is a
typed `SendFailed`. This is what turns the **macOS 26 regressions** into
loud failures instead of silent drops:

- AppleScript rejects modern chat guids (chat.db now stores service-agnostic
  `any;-;…` / `any;+;…` ids; `chat id` still wants `iMessage;…`). Direct
  chats therefore send by **participant handle** (`--to`), never by chat id.
- Group sends (`--chat-id`) are upstream-broken territory (imsg #90) — the
  receipt read-back reports them honestly.
- Outgoing rows store their body in `attributedBody` (text column NULL), so
  receipt matching decodes the mapped entity, never `m.text` in SQL.

## Honestly absent

No public path exists — these verbs are simply not provided, and surfaces
degrade gracefully:

- **message_mark_read** — the phone's unread badge persists after reading here
- **inbound typing / presence** — not observable
- **reactions** — `imsg react` exists but is UI-automation (Accessibility
  permission, most-recent-message only); too fragile to declare

## IDs

- **Conversations: `chat.guid`** (`any;-;+15125550000`; groups `any;+;chat…`).
  Every op also accepts an integer ROWID or a fuzzy name/handle substring —
  exact handle identity wins over substrings, and direct chats win over
  groups containing the same person.
- **Messages: `message.guid`** (UUID, stable across devices).
- `accountEmail` on every row is the signed-in identity from
  `chat.account_login` (`E:joe@… → joe@…`) — the same account grammar email
  and WhatsApp stamp.

## Tools

### Reads
- `list_conversations(limit=200)` — most recent first; guid ids, resolved
  names (contact → chat display → raw handle; never blank), `participant`
  typed refs, `isGroup` (chat.style 43), `accountEmail`. 1:1 rows carry
  `image` + `mimeType` when macOS Contacts has a photo for the peer
  (full `imageData` when present, else thumbnail; staged via `blobs.put`
  with the real PNG/JPEG mime — AddressBook sqlite has no photo table).
- `get_conversation(id)` — one conversation by guid/ROWID/fuzzy name.
- `list_messages(conversation_id, limit=200, since=None, until=None, order="desc")`
  — decodes post-Ventura `attributedBody`; attachment rows carry
  `mediaPath`/`sizeBytes`/`mime`.
- `get_message(id)` — by guid or ROWID.
- `search_messages(query, limit=200)` — LIKE over message text.
- `summarize_conversation(conversation_id, n=15)` — the relationship-arc
  tool: one call returns count + bounds + oldest/newest `n` as a readable
  timeline in `content`.
- `list_voice_notes(conversation_id=None, limit=200)` — audio messages as
  interval events.

### Live
- `watch()` — arm the standing inbound stream (see above).
- `pull_changes()` — the transport's delta op; never call it as an agent.

### Actions
- `send_message(to, text)` — `to` is a guid/ROWID/fuzzy name, or an E.164
  number / Apple-ID email for a brand-new thread (fuzzy names never create).
  Returns the chat.db receipt entity.
- `send_media(to, path, caption=None)` — any readable file path (blob-store
  paths included); same receipt contract.
- `send_typing(chat, kind="typing")` — `typing` or `paused` (no recording
  indicator on iMessage).

### Account
- `check_session` — identity from `chat.account_login`; `authenticated:false`
  when no account is signed in.
- `login` — cannot be driven: signing into Messages on the Mac IS the login;
  returns instructions.
- `logout` — cannot be driven either; says so (`NotSupported`), never fakes it.
