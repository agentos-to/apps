---
id: whatsapp
name: WhatsApp
description: WhatsApp messages, contacts, and sending via live WhatsApp Web
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
capability — the engine holds the CDP session; this skill never sees the
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
- **React:** `send_reaction` with `chat` + `emoji` (any Unicode emoji)

## Behavior notes

- `search_messages` searches the **in-memory** Store — recent history per
  chat, not the full archive. For deep history, `list_messages` the
  conversation first to page more into memory.
- First op after an engine restart is slow (~10-30s): browser launch + page
  load + Store init. Warm ops run in well under a second.
- Media messages map with `type` (`image`, `video`, `ptt`, …) and use the
  caption as `content` (never the preview thumbnail WhatsApp stores in the
  body field); media payloads themselves are not downloaded.
- Meta AI responses (`rich_response` type) have their text extracted from
  response fragments automatically.
- `send_message` returns the sent message entity (same shape as
  `list_messages` rows) once WhatsApp's server acks the send — a failed
  send is a `SendFailed` error, never a silent success.
- `send_reaction` reports `dispatched`, not delivered: WhatsApp Web gives
  a headless tab no client-side echo for reactions. Check a phone if
  delivery matters.
- `watch` survives page reloads, session drops, and browser restarts (the
  engine re-installs the hook and reconnects with backoff). Only an engine
  restart clears it — re-arm with one `watch` call.
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
`WAWebUserPrefsMeUser.getMeUser` (login probe). Model fields carry the
`__x_` prefix. If WhatsApp ships a breaking Web update, the whatsapp-web.js
project is the reference for re-deriving module names and call shapes.

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
