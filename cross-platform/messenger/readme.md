---
id: messenger
name: Messenger
description: Live Facebook Messenger DMs via the logged-in web client — read, send, and watch new messages in real time by hooking Meta's E2EE shared-worker store (Lightspeed/MSYS · Armadillo), never the wire
services:
  - blobs
color: "#0084FF"
website: "https://www.facebook.com/messages/"
product:
  name: Messenger
  website: https://www.messenger.com/
  developer: Meta Platforms, Inc.
---

# Messenger

Read and send Facebook Messenger direct messages through a live
`facebook.com/messages` session in an engine-held browser. Ops run as JS in the
engine-owned browser via the `browser_session` service (CDP) — the engine holds
the session; this app never sees the encrypted wire.

**The whole design in one sentence:** modern Messenger web is Meta's Signal-based
**E2EE** client (Lightspeed/MSYS, codename *Armadillo*) whose decrypted store,
crypto, and realtime wire all run inside the **`MAWMainV4WebWorkerBundle` shared
worker** — so we attach the engine browser plane to the *worker* (not the page),
read its Dexie tables read-only for history, and hook its own stored-procedure
map (`LSDynamicDependencies.cachedModules`) for live `watch` — the same "hook the
client's own decrypted store, never the wire" pattern as WhatsApp/Instagram, one
level deeper.

> **Mode: `background`.** Runs headless in the engine's background profile like
> WhatsApp/Instagram — DMs surface in the Messaging app, so no window is needed
> for a read (rule 19). Login is the `login_window` kind: a headed flip of that
> same profile for the sign-in, so the session lands in the exact profile every
> headless read uses.

## Why the worker, not the page

Instagram's plaintext lives in the **page's** Relay store, so the IG plugin walks
the React fiber tree there. Messenger is different: the page Relay store holds
**zero** message records (proven) — decryption happens in the shared worker, and
plaintext only ever exists there. That single fact is why this plugin needs the
engine's **`worker:<title>` target** (added to `browser.rs`): page JavaScript
cannot reach a SharedWorker's scope, and only the engine can hold a CDP socket to
the worker target. See [`operations.md`](./operations.md) for the full mechanism.

## IDs

- **conversation** (`conversationId`) — `threadKey`, a numeric string.
- **message** (`id`) — `messageId`.
- **account** — FBID. The viewer's is the `c_user` cookie (plain, non-httpOnly);
  it drives `isOutgoing` with no scan (unlike IG's igid→fbid hop).

## Entity model

Mirrors the WhatsApp/Instagram/iMessage shapes so the Messaging app consumes it
unchanged:

- **conversation** — `id`=threadKey (identity, never the display), `name` +
  `image` from the page Lightspeed contacts join, `isGroup`, `unreadCount`,
  `participant[]` (1:1 peer as `{platform:"messenger", id, display_name}`)
- **message** — `content`, `published`, `isOutgoing`, `author`, `from` (account),
  `conversationId`, `type`
- **account** — the Messenger identity (FBID)

## Requirements

- An engine-held browser with a **logged-in Facebook session** on `facebook.com`
  (messenger.com is a SEPARATE, logged-out session — do not use it).
- The DM UI at `https://www.facebook.com/messages/`, which spawns the shared
  worker; ops navigate the page there first so the worker exists to attach to.
- **⚠️ E2EE restore PIN.** A *fresh* login (e.g. the engine's bg profile) is a new
  E2EE device — Messenger gates chat restore on the **secure-storage PIN** ("Enter
  your PIN to restore your chats", or a one-time code). Until it's entered, only
  thread metadata + admin/cutover rows sync, message bodies stay encrypted at rest
  (`encryptedWith_S456130: maw_ear`), and no realtime chats arrive. This is the
  Messenger analog of WhatsApp device-linking. The login flow should surface this
  PIN step (headed) — see "Known gaps" below. On an *already-restored* session
  (the human's daily browser) it's a non-issue.

## Common tasks

- **Active chats:** `list_conversations`
- **Chat history:** `list_messages` with `conversation_id`
- **Send:** `send_message` with `to` (threadKey) + `text`
- **Watch (live push):** `watch` — arm once; new DMs stream into the graph

## Linking (for the agent)

When ops return `NeedsAuth`, the engine profile isn't logged into Facebook:

1. `messenger.login` → opens a headed sign-in window on the bg profile.
2. Joe signs in (password re-confirm + any 2FA / checkpoint himself).
3. Poll `messenger.check_session` until `authenticated: true`.
4. `browser.login_window(close=true)`.

---

# Status (what's proven vs pending)

`operations.md` is the durable reverse-engineering reference (the worker
registry, the exact hook, the positional decode, the transports). Read it before
re-deriving anything. Op-level status:

| Op | Mechanism | Status |
|---|---|---|
| engine gate (`worker:` target) | attach to shared worker by title, eval in worker context | ✅ **proven** via `browser_session eval target=worker:MAWMainV4WebWorkerBundle` (81 sprocs reachable), both attach + bg |
| **watch decode** | `upsertMessage`/`upsertReaction` POSITIONAL args | ✅ **confirmed against Meta's pristine sproc source** — this is what `watch` uses |
| `check_session` / `login` | `xs`/`c_user` cookies · `login_window` headed flip | ✅ **proven live in bg** — login flips headed, session persists, check_session returns `authenticated`+FBID |
| `logout` | — | 🚧 stubbed |
| `watch` | hook worker `cachedModules.upsertMessage` → console marker → graph | ⏳ **hook arms live in bg** (`upsertWrapped:true`) + decode confirmed; the bg profile is now PIN-restored and reads plaintext, but end-to-end graph capture still needs a real new inbound message |
| `list_conversations` / `list_messages` / `get_message` | raw read-only IDB read + the client's **EAR codec** (`require('MAWDbObjEncryption').decryptDbObj`) | ✅ **decrypt + transport solved** — browser target inventory/liveness is first-class, unnamed workers resolve by behavior, and failures carry a typed cause + evidence through MCP and Messaging |
| `send_message` | composer-UI drive on the page | 🚧 **fragile** — a full page navigate to the thread respawns/kills the shared worker (and the watch); needs the LS-task send path (no navigation) instead |
| `send_reaction` | LS task (`LSIssueNewTask` + serializer) | 🚧 not wired — task shape needs a live RE capture |
| `mark_read` | LS task (`LSMarkThreadReadV2` sproc located; outbound task shape pending) | 🚧 not wired — task shape needs a live RE capture |

**Watch decode — CONFIRMED (2026-07-08).** The `upsertMessage`/`upsertReaction`
sproc sources were read verbatim from the running worker (they were behind a
prior probe's `sink` wrapper — the pristine `orig` was extracted from the wrapper
closure via a targeted Debugger `[[Scopes]]` read), matching mautrix-meta's
`LSInsertMessage` tags exactly. Those args are **plaintext** (the sproc receives
the decrypted message), which is why the hook is the reliable read+watch path.

**Read path — SOLVED (the EAR codec).** The whole message row is sealed at rest
in a single **`_encryptedContent`** blob (only routing fields — `threadJid,
msgId, externalId, sortOrderMs, keyVersion_S456130` — stay plaintext); there is
NO plaintext `msgContent`/`text` column, and `getDBExn()` returns a custom
transactor, not a `.table()` Dexie db. The decrypt is the client's own codec,
**requireable by name**:
`require('MAWDbObjEncryption').decryptDbObj(row, table, require('MWEARKeychainV3').getDbEncryptionKey)`
→ the full plaintext row (`msgContent.content` = text, `ts` = unix SECONDS,
`author` = `'@me'` for outgoing else `'<fbid>@msgr'`, `quote` = reply). The
connector reads raw rows read-only (by the `messages` `threadJid` index) and
decrypts each — synchronous, no transaction, no lock, no new engine capability.
Full mechanism + schema in `operations.md` "READ"; the general technique
(registry walk → requireable codec) is
[`reverse-engineering-runtime-internals`](../../../core/system-docs/apps/reverse-engineering/runtime-internals.md)
Technique 1c and the toolkit's `__re.ear()` / `__re.decryptEAR()`. The watch
decode is unaffected (it never touches the DB).

## Known gaps (the real follow-up work)

1. **E2EE PIN restore in login.** A fresh bg login needs the secure-storage PIN
   (or one-time code) to restore chats before any message body / realtime exists.
   `login` should drive/surface this headed step (the PIN dialog is on the
   `/messages/` page after sign-in).
2. **Reads under encryption-at-rest — SOLVED (codec wired).** ✅ The decrypt-on-
   read path is implemented (`require('MAWDbObjEncryption').decryptDbObj`, see
   above) and verified end-to-end via direct CDP. Remaining polish: conversation
   NAMES (the `participants` table is empty; 1:1 names live in the encrypted
   `contacts` store — decrypt it the same way), and XMA/media payload mapping
   (flatten into the shared Messaging share contract — system doc
   `messaging-shares`; URL previews → `kind:'link'`, rich posts → other/omit).
3. **Engine worker-eval transport — solved.** `browser.targets` reports every
   worker's id/type/title/url, bounded `1+1` liveness, and optional behavior
   probe. Messenger resolves `worker:MAWMainV4WebWorkerBundle?probe=…` by title
   first, behavior second, caches the `targetId`, and invalidates it on respawn.
   Not-ready errors are typed as `no_target`, `booting`, `wedged`,
   `dead_session`, or `needs_pin`, with the worker/DB/key evidence preserved.
4. **Send without navigation.** Replace the composer full-navigate (which
   respawns the worker) with the LS-task send path (`LSIssueNewTask` +
   `LSTaskSerializerSendMessageV2`, issued in-worker) so a send never disturbs
   the store or the watch.

## Internals & re-derivation

Everything — the worker require-registry walk, the `cachedModules` hook point,
the positional decode, the Dexie tables, the LS-task send path, the engine gate —
is in [`operations.md`](./operations.md). On a Meta deploy, re-run the worker
registry walk (structure-based; survives deploys). Sproc names + the
`cachedModules` map are stable; only arg indices / table numbers drift — re-read
the sproc factory source to refresh. Never hardcode doc_ids / table numbers.

Prior art: `mautrix-meta` (`go.mau.fi/mautrix-meta`, `pkg/messagix`) reimplements
this wire server-side and is the field-layout reference. No prior art hooks the
*browser* client — this connector is new ground.
