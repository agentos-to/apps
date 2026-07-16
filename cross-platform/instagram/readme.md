---
id: instagram
name: Instagram
description: Live Instagram DMs via the logged-in web client — read, send, and watch new direct messages in real time by hooking Instagram's own React/Relay store
services:
  - blobs
  - http
color: "#E1306C"
website: "https://www.instagram.com/"
product:
  name: Instagram
  website: https://instagram.com
  developer: Meta Platforms, Inc.
---

# Instagram

Read and send Instagram direct messages through a live `instagram.com/direct`
tab in an engine-held browser. Ops run as JS payloads in the engine-owned
browser via the `browser_session` service (CDP) — the engine holds the
session; this app never sees the wire.

**The whole design in one sentence:** Instagram's own JS client already does
the hard part (Signal decryption + Thrift decoding), landing every decrypted
message as a normalized record in its **React/Relay store**; we hook that
store — exactly as the WhatsApp plugin hooks `WAWebCollections.Msg` — and never
touch the encrypted wire ourselves.

> **Mode: `background` (migrated 2026-07-08).** The connector now runs headless
> in the engine's background profile like WhatsApp — DMs surface in the Messaging
> app, so no window is needed for a read (CLAUDE.md rule 19). Login is the
> `login_window` kind: a headed flip of that same profile for the sign-in
> (`browser_session.login_window`), so IG's device-fingerprint check sees one
> consistent profile from sign-in onward — no cookie-copy, no cross-profile
> mismatch. The RE proof-log below (the `mode:attach` notes) is the 2026-07-07
> baseline that established the verbs; the transport is identical either mode.
>
> **RE baseline (2026-07-07).** The full verb set was proven end-to-end
> against Joe's real logged-in session on `mode:attach`: read
> (`list_conversations`/`list_messages`/`list_persons`), live `watch`,
> `send_message`, `send_reaction` (add/remove), `get_message` media hydration,
> **`mark_read`** (Relay `useIGDMarkThreadAsReadMutation` replay), and
> **`send_typing`** (IG's own `IGDMAPISendTypingIndicator` over MQTT) — the last
> two shipped 2026-07-07 (variable shapes lifted from the live modules; see
> [`operations.md`](./operations.md), which is the durable reverse-engineering
> reference for every op's transport, doc_id, and capture recipe — read it
> before re-deriving anything). Conversations + persons now also carry the other
> party's `@handle` as a nested `participant`/`accounts` account. Remaining: a
> few rare read content types (mentions, view-once — see "Content model &
> backlog"). This readme is the durable reference.
>
> **Reads never navigate.** `_ensure_dm` only hard-loads /direct when the tab is
> OFF a DM surface; the Relay store is cumulative and the armed `watch` parks the
> tab on the inbox, so every read is a pure in-page eval (~180ms). The old
> per-read `navigate` wiped the store on every call — a refresh storm that made
> reads slow AND intermittently empty (a read landing mid-reload saw a cold
> store). Never reintroduce an unconditional navigate in a read path.

## Why this approach (and not the alternatives)

Instagram DMs are now **end-to-end encrypted** (Signal protocol — the
`messenger_web_signal_v3_*` IndexedDB stores prove it). That single fact
collapses the option space:

| Approach | Verdict |
|---|---|
| ❌ Private REST API (`/api/v1/direct_v2/…`, cookie-spoof) — the *old* `_joe/apps/.needs-work/instagram` stub | Stale: E2EE means message bodies no longer flow through REST cleanly; highest ban risk (mobile-signature spoof). Its endpoint inventory survives here as a send fallback only. |
| ❌ `mautrix-meta` / `instagram_mqtt` / `instagrapi` — reimplement the `wss://edge-chat.instagram.com` MQTToT wire | Must now also reimplement the Signal ratchet + device keys. mautrix's IG support is mid-restructure (June 2026 Lightspeed split); `instagram_mqtt` unmaintained. High ban risk (headless protocol client). |
| ✅ **Hook the logged-in web client's Relay store** | The *only* place plaintext exists is inside the running client, post-decrypt. Lowest ban risk by construction — it's the real browser, real session, real IP, zero extra network signature. Same pattern as our WhatsApp plugin. |

## Requirements

- An engine-held browser with a **logged-in Instagram session** on
  `instagram.com`. Login is username/password + optional 2FA (not a QR link
  like WhatsApp) — driven via `browser.login_window`.
- The DM UI lives at `https://www.instagram.com/direct/inbox/`, **not** the
  site root — the Relay store only holds message records once the inbox (or a
  thread) has loaded. Ops navigate there first.

## Linking (for the agent)

When ops return `NeedsAuth`, the engine profile isn't logged in:

1. `browser.login_window(url="https://www.instagram.com/accounts/login/", label="Instagram")` — opens a visible window.
2. Joe signs in (handles 2FA / any checkpoint himself).
3. Poll `instagram.check_session` until `authenticated: true`.
4. `browser.login_window(close=true)`.

The session persists in the engine-owned browser profile.

## IDs

- **thread** (`conversationId`) — `thread_fbid`, a numeric string (e.g. `2598973956829176`).
- **message** (`id`) — `message_id`, `mid.$gAA…` (a base64-ish opaque id).
- **account** — a user carries **two** ids: `sender_igid` (Instagram-scoped,
  matches the `ds_user_id` cookie for the viewer) and `sender_fbid`
  (Facebook/Messenger-scoped). Reactions carry both; messages carry `sender_fbid`.
  Resolving one to the other is an open question (see Internals).

## Entity Model

Mirrors the WhatsApp/iMessage `message` shape so the Messaging app consumes it
unchanged:

- **conversation** — a DM thread (`id`=thread_fbid, `name`, `isGroup`, `unreadCount`)
- **message** — `content`, `published`, `isOutgoing`, `author`, `from` (account), `conversationId`, `type`, `reactions`
- **account** — the Instagram identity (`handle`=@username, ids above)

## Common Tasks (target API — some unbuilt)

- **Active chats:** `list_conversations`
- **Chat history:** `list_messages` with `conversation_id` (reaches full history — paginates on demand)
- **Stories (feeds):** `list_posts` — home-tray rings as `post` (`postType: story`); brokered as `feeds` for Social
- **One story ring / item:** `get_post` with `story:<user_pk>` or `story:<user_pk>:<media_pk>` — replays `PolarisStoriesV3ReelPageStandaloneQuery`, hydrates media → `attaches` + `ringPosts`
- **Media bytes:** `get_message` with `id` — downloads a message's image/video/GIF/voice-note into the blob store (`attaches[]`)
- **Send:** `send_message` with `to` + `text`
- **Watch (live push):** `watch` — arm once; new DMs stream into the graph as `message` entities
- **React / typing / mark-read:** `send_reaction` / `send_typing` / `mark_read`

---

# Internals (for maintainers) — the reference

Everything below was learned by live CDP probing of Joe's real logged-in
Instagram on 2026-07-06. Re-probe with `browser.eval` (mode `attach`, target
`instagram.com`) if bindings drift.

## What's proven vs unverified

| Claim | Status | Evidence |
|---|---|---|
| State mgmt is React + Relay (not Redux/Zustand/Context) | ✅ proven | Relay `environment` found on a React fiber's `memoizedProps.environment` |
| Relay env reachable by fiber-walk in ~141 nodes from a `__reactContainer$` element | ✅ proven | walk found `getStore()`+`getNetwork()` object |
| Store holds decrypted plaintext DMs | ✅ proven | pulled `SlideMessage.text_body` = real message text, `sender_fbid`, `thread_fbid`, `timestamp_ms`, reactions |
| Full READ pipeline — sender join + `isOutgoing` + thread mapping | ✅ proven | 253 msgs → **93 outgoing / 160 incoming** all mapped, `@handle`+name resolved; 16 threads with real titles (2026-07-06) |
| Sender join: `SlideMessage.sender` → `SlideUser {name,id=fbid,igid,user_dict}` → `XDTUserDict {username,full_name,profile_pic_url}` | ✅ proven | all 253 resolve to @username |
| Viewer fbid = `SlideUser` where `igid == ds_user_id` cookie | ✅ proven | dsid `27323288` → fbid `111600823566513`; correctly flags own messages |
| `list_conversations` from `XFBIGDirectViewerThread {thread_fbid, thread_title, is_group, marked_as_unread, last_activity_timestamp_ms}` | ✅ proven | 16 threads, real names ("Grove St", …) |
| **Stories tray → `list_posts` / `feeds`** | ✅ **proven** | `XDTMaterialTray` / `XDTReelDict` on home `/` (`xdt_api__v1__feed__reels_tray`); seen≥latest sorts to end; faces staged |
| **Story media → `get_post`** | ✅ **proven** | `PolarisStoriesV3ReelPageStandaloneQuery` `{reel_ids_arr}` → `XDTMediaDict` items; CDP `response_body` → blobs (image + video) |
| **Read ops run end-to-end through real engine dispatch** (not just probes) | ✅ proven | `plugins.run` `mode:attach`: `check_session` (111ms), `list_messages`, `list_conversations` all return correct entities |
| **`watch` push — full pipeline: live DM → graph `message` node** | ✅ **proven end-to-end** | armed via `plugins.run instagram.watch`; a live "pineapple" DM surfaced as graph node `wkeqg3` (`author:"Me"`, `isOutgoing:true`, thread + ts) ~instantly. Path: store `publish` → CDP console marker → `live_entity::write` → graph. |
| **`send_message` — composer-UI drive + event-driven receipt** | ✅ **proven end-to-end** | 4 live DMs to @ksubedi via `plugins.run`; navigate `/direct/t/<thread_fbid>/` → type into `role:textbox` → `key Enter` (IG does the E2EE send); receipt resolves on `store.publish` |
| **`send_reaction` — Relay `commitMutation` replay (NOT UI, NOT websocket)** | ✅ **proven end-to-end** | `IGDirectReactionSendMutation` (doc_id `24374451552236906`) over POST `/api/graphql`; add ❤️/😂/👍, `remove`, and un-react all verified on @ksubedi via `plugins.run` + rendered-badge check; `conversation_id` derived from the message record |
| **Inbound reactions folded into `message.reactions`** | ✅ proven | `SlideMessage.reactions {__refs}` → `XFBSlideReaction {reaction: emoji}` → `[{emoji, count}]` (WhatsApp's shape); 3 reacted msgs in the @ksubedi thread mapped |
| **History pagination — `list_messages` replays IG's own loadNext** | ✅ **proven end-to-end** | `IGDMessageListOffMsysQuery {after,first:20,id}` via `createOperationDescriptor` + `env.execute`; @ksubedi 20→87 (full thread), @massimilianohasan 20→320. Cursor on `__IGDMessagesList_slide_messages_connection.page_info`. |
| **Media download — `get_message` hydrates bytes → `blobs.put`** | ✅ **proven end-to-end** | image/video/GIF via in-page `fetch` (`*.fbcdn`/`scontent`/`external`, CORS-readable); voice notes via engine `http.request` fallback (`cdn.fbsbx.com` is CORS-blocked). All 4 verified as valid ISO-MP4/JPEG on disk. |
| **Content typenames — image/audio/GIF/XMA subtypes** | ✅ verified live | `SlideMessageImageContent.attachments`, `…AudiosContent.audio_attachments`, `…AnimatedMediaContent.animated_media`, 5 XMA `kind`s — the earlier inferred `Photos`/`Audio`/`Voice` names were wrong, corrected against real messages |
| `timestamp_ms` is a STRING, not a number | ✅ proven (bug fixed) | `"1783394485069"` → `Number.isFinite` false → `published` was null on every message; `num()` coercion added |
| Reactions are a plain GraphQL mutation, NOT the E2EE MQTT wire | ✅ proven | transport probe (wrap `execute`+fetch+xhr+ws): a manual ❤️ fired ONLY Relay `execute` — no ws frame carried it |
| Watch + send both ride Relay's `store.publish(delta)` change signal — no polls, no full scans | ✅ proven | `publish` carries a RecordSource of ONLY the changed records (`getRecordIDs`/`get`); 43 deltas in 4s. Reads Relay's own change-set. |
| `store.subscribe` / `store.notify` exist and are callable | ✅ proven | `typeof === 'function'` on the live store |
| Nothing decoded is exposed on `window` (no global store, no Relay/React devtools hook in prod) | ✅ proven | `window` scan; `__RELAY_DEVTOOLS_HOOK__`/`__REACT_DEVTOOLS_GLOBAL_HOOK__` absent |
| DMs are E2E-encrypted (Signal) | ✅ proven | `messenger_web_signal_v3_*` IndexedDB: identity/prekey/signedPrekey/senderKeySessions/session |
| `store.subscribe` fires globally (off-screen threads too), not just the open thread | ❓ **unverified** | must test by receiving a DM in a non-open thread |
| React mounts in the headless background daemon profile (Brave) — parity *would* hold | ✅ proven | `reactFiber=true`, 114 divs headless — but we chose `attach` (see decision below), so this path is unused |
| Send path (UI-drive or in-page fetch) | ❓ **unverified** | not attempted |
| IndexedDB message stores are readable plaintext (vs encrypted-at-rest) | ❓ **unverified** | `_worm_ear_` store in every DB ⇒ likely ciphertext-at-rest; the in-memory Relay store is the guaranteed-plaintext source |

## Relay environment discovery (the core recipe)

```js
// Find a React fiber root, walk it, return the first object that looks like
// a RelayModernEnvironment (has getStore + getNetwork). ~141 fibers on IG.
function findRelayEnv() {
  const cands = [document.getElementById('react-root'), document.body,
                 ...document.querySelectorAll('body > div')].filter(Boolean);
  let key, el;
  for (const c of cands) {
    key = Object.keys(c).find(k => k.startsWith('__reactContainer$') || k.startsWith('__reactFiber$'));
    if (key) { el = c; break; }
  }
  if (!key) return null;
  const isEnv = o => { try { return o && typeof o === 'object'
    && typeof o.getStore === 'function' && typeof o.getNetwork === 'function'; } catch(e){ return false; } };
  let root = el[key]; if (root && root.current) root = root.current;
  const stack = [root], seen = new Set();
  while (stack.length) {
    const f = stack.pop(); if (!f || seen.has(f)) continue; seen.add(f);
    for (const p of ['memoizedProps', 'memoizedState']) {
      const v = f[p]; if (v && typeof v === 'object')
        for (const k in v) { try {
          const val = v[k];
          if (isEnv(val)) return val;
          if (val && typeof val === 'object' && isEnv(val.environment)) return val.environment;
        } catch(e){} }
    }
    if (f.child) stack.push(f.child);
    if (f.sibling) stack.push(f.sibling);
    if (f.alternate && !seen.has(f.alternate)) stack.push(f.alternate);
  }
  return null;
}
// Read: const src = env.getStore().getSource();
//       src.getRecordIDs().map(id => src.get(id))   // every normalized record
```

**Alternative env grab (for the watch hook, to catch it deterministically at
boot):** install our own `window.__REACT_DEVTOOLS_GLOBAL_HOOK__` at
document-start (before IG's React loads — `browser_session.subscribe` injects
via `Page.addScriptToEvaluateOnNewDocument`, which runs first). React then
registers its renderer with our hook and calls `onCommitFiberRoot(id, root)` on
every commit — walk that root to grab the env on first commit, then attach the
store subscription. This is literally how the React DevTools extension works.

## Relay record types (the decoded message model)

From a live store snapshot (2113 records). `__typename` → what it is:

| `__typename` | count | role |
|---|---:|---|
| `SlideMessage` | 253 | **a DM** — the record to watch/map |
| `SlideMessageText` | 132 | text content (`{text_body}`); also inlined on `SlideMessage.text_body` |
| `XFBIGDirectViewerThreadSlideMessagesEdge` | 710 | thread→message connection edges |
| `XFBIGDirectViewerThreadSlideMessagesConnection` | 60 | the messages connection per thread |
| `SlideMailboxThreadsByFolderEdge` | 62 | **inbox thread list** edges → `list_conversations` source |
| `SlideMessageXMAContent` | 72 | XMA = shared post/reel/link attachment |
| `MessagingReaction` / `XFBSlideReaction` | 42 / 42 | reactions (`{sender_fbid, sender_igid}`) |
| `XFBSlideReadReceipt` | 32 | read receipts |
| `XDTUserDict` | 29 | a user (deref `SlideMessage.sender` `__ref` → username) |
| `SlideMessageAdminText` | 24 | system messages |
| `XDTMediaDict` / `XDTImageVersion2` / `XDTImageCandidate` | 23 / 23 / 45 | media payloads |

**A `SlideMessage` record (proven fields):**

```
__typename:          "SlideMessage"
message_id:          "mid.$gAAk7wMd6L_ilax7QmWfOdmHvmYI3"   → entity.id
text_body:           "…plaintext…"                          → entity.content (inlined, no deref needed for text)
content:             {__ref → SlideMessageText}             (deref for non-text bodies)
content_type:        "TEXT"                                 → entity.type
sender_fbid:         "114832043240762"                      → sender (FB-scoped)
sender:              {__ref → XDTUserDict}                  (deref → username)
thread_fbid:         "2598973956829176"                     → entity.conversationId
timestamp_ms:        1783381985433   (MILLISECONDS)         → new Date(ms).toISOString()
igd_snippet:         "sender_username: text…"               (has the sender handle prefixed)
reactions/msg_reactions/mentions:  {__refs}
is_ai_generated / is_pinned / is_reported / igd_is_forwarded: booleans
offline_threading_id: "7480046194112692791"
```

> ⚠️ `timestamp_ms` is **milliseconds** (WhatsApp's `__x_t` was seconds — don't
> reuse the `iso(t*1000)` helper). Relay refs are `{__ref: id}` / `{__refs: [ids]}`
> — deref via `source.get(ref.__ref)`.

## Watch hook strategy (the push wire) — VERIFIED

Hook Relay's OWN change signal — **no polling, no full-store scan**:

1. **Install-once by control flow**, never `clearInterval` (WhatsApp lesson:
   stacked ~9k listeners in 77 min). Guard: `if (window.__agentos_ig_watch__) return;`.
2. Wait for `findRelayEnv()` (null until the inbox loads), then
   `store = env.getStore()`, `source = store.getSource()`.
3. **Wrap `store.publish(delta)`.** Relay calls `publish` with a RecordSource of
   EXACTLY the records that just changed (`delta.getRecordIDs()`/`get()`) — that
   IS the change event, fired the instant it changes. Read the changed ids from
   the delta; read the hydrated record from the merged `source`. (`store.notify`
   is NOT the tap — it carries no delta and forced a full scan. `store.subscribe(
   snapshot, cb)` is the fully-official API but wants a per-query selector; the
   publish hook catches ALL threads in one place.)
4. Skeleton guard + dedup: Relay publishes an empty placeholder (no `message_id`)
   first, then hydrates it — emit only records with `message_id` + `thread_fbid`,
   deduped on `message_id`; own the seen-set **in the hook**.
5. `console.log(MARKER + JSON.stringify({...entity, __shape__:'message'}))` →
   `browser_session.subscribe` routes it → `live_entity::write` →
   `subscription.entity` observer → Messaging app. Durable; arm once.

**Off-screen granularity — resolved:** verified live that a DM to a NON-open
thread reaches the store via `publish` (IG's own inbox subscription keeps recent
threads warm). No inbox-poll backstop needed for loaded threads.

## Send strategy — VERIFIED (composer-UI drive)

- **Primary — IMPLEMENTED: drive the composer UI.** `send_message` navigates to
  `/direct/t/<thread_fbid>/` (thread_fbid works directly as the URL id), finds
  IG's `role:textbox` composer in the nav snapshot, `type`s the text, presses
  `key Enter` — IG's own client does the E2EE send. Receipt: an **event-driven
  wait on `store.publish`** resolves the instant our outgoing `SlideMessage`
  lands in the store (no poll), returned as the `message` entity. Zero API
  surface, human-indistinguishable.
- **Fallback: in-page `fetch` to IG's own web API** (same-origin, page's own cookies
  + `x-csrftoken`, no spoofing). Endpoint inventory harvested from the old stub —
  **verify these still work under E2EE before relying on them:**

  | Action | Method | Endpoint |
  |---|---|---|
  | Send text | POST | `/api/v1/direct_v2/threads/broadcast/text/` (body: `thread_ids=[id]`, `text`, `action=send_item`, `client_context`+`mutation_token`+`offline_threading_id`=uuids, `_uuid`) |
  | Send to new user | POST | `…/broadcast/text/` with `recipient_users=[[user_id]]` |
  | React | POST | `/api/v1/direct_v2/threads/{id}/items/{item}/reactions/` (`reaction_status=created`, `emoji`) |
  | Mark seen | POST | `/api/v1/direct_v2/threads/{id}/items/{item}/seen/` |
  | Typing | POST | `/api/v1/direct_v2/threads/{id}/activity/` (`activity_status=1`) |
  | Delete/unsend | POST | `/api/v1/direct_v2/threads/{id}/items/{item}/delete/` |

  Required headers on these: `X-IG-App-ID: 936619743392459`, `X-CSRFToken:
  {csrftoken cookie}`, `X-Requested-With: XMLHttpRequest`.

## Reactions — PROVEN (send + read)

Reactions ride a plain **Relay GraphQL mutation**, not the E2EE MQTT wire — so
`send_reaction` replays IG's own operation; no UI drive needed. (Transport found
with the recipe documented at the top of `instagram.py`.)

**Send — `IGDirectReactionSendMutation`** (doc_id `24374451552236906`, POST
`/api/graphql`):
- variables: `{ input: { emoji, item_id: "", message_id, reaction_status, thread_id } }`
  - `emoji` — **bare codepoints**: the picker's ❤️ (U+2764 U+FE0F) is sent as
    U+2764. `send_reaction` strips variation selectors (U+FE0E/FE0F) to match.
  - `reaction_status` — `"created"` to add, `"deleted"` to remove (`remove=True`).
  - `item_id` — always `""` in every capture.
  - `thread_id` — the thread_fbid; derived from the message's own store record
    when `conversation_id` is omitted.
- Replay: `require('IGDirectReactionSendMutation.graphql')` → the compiled node;
  `require('relay-runtime').commitMutation(env, {mutation, variables, onCompleted,
  onError})`. `onCompleted` returns `xig_direct_reaction_send_with_slide_messaging_response`
  with no errors = Instagram's own ack (so we report a confirmed `reacted`, unlike
  WhatsApp's headless `dispatched`). The reaction module + `commitMutation` are
  present on the cold inbox, so `send_reaction` only needs `_ensure_dm` (no thread
  navigation — never yanks the human's open thread).

**Read — folded into every `message`:** `SlideMessage.reactions {__refs}` →
`XFBSlideReaction {reaction: emoji, sender_fbid}` (the sibling `msg_reactions` →
`MessagingReaction` carries only sender ids, no emoji). Aggregated by emoji into
`[{emoji, count}]` — the same shape WhatsApp emits. Reactions hydrate with the
thread (a cold read before the thread's reaction connection loads shows none).

**UI path (rejected — fallback note only):** click a message bubble → toolbar
(React/Reply/More); the React button (`aria-label "React to message from
<user>"`) opens an emoji picker whose quick emoji are **now labeled** (aria-label
= the emoji itself, ❤️ first) — the "unlabeled sprites" note was stale. It works,
but the mutation replay is cleaner: no snapshot/scroll, hits off-screen messages,
one call, real server ack.

**Shared-browser gotcha:** the attach browser is shared — another agent's tab can
refresh instagram.com mid-probe and wipe hook/probe state (happened repeatedly
2026-07-06). `watch` auto-reinstalls on reload; ad-hoc probe state does not, so
instrument → trigger → read in a tight window.

## IndexedDB map (history / persistence layer)

IG web runs Meta's unified **Messenger Web** encrypted client (`maw`/`wmi`/
`reverb`/`Slide` codenames), persisted to IndexedDB. Two mailbox-id suffixes:
`17843744700049874` (IG-scoped) + `111600823566513` (FB/Messenger-scoped).

| DB | notable stores | use |
|---|---|---|
| `reverb_v1_*` | `message`, `deleted`, `supplemental`, `tags` | local message cache — candidate history/backfill (⚠️ likely enc-at-rest) |
| `messenger_web_fts_v1_*` (v50) | `ftsIndexV3`, `ftsBackloggedMessages` | local full-text search index |
| `messenger_web_metadata_v1_*` | `threads` | thread list |
| `messenger_web_v1_*` (v1260) | `media`, `editMsgHistory`, `deletedMessages`, `groupInfo`, … | the big kitchen-sink DB |
| `messenger_web_signal_v3_*` | `identity`, `prekey`, `signedPrekey`, `senderKeySessions`, `session` | **Signal E2EE keystore (proof of E2EE)** |
| `messenger_web_ebdb_v1_*` | `encrypted_backups`, `secure_encrypted_backups_epochs` | encrypted history backup + PIN |

Every DB carries a `_worm_ear_` store — **E**ncryption **A**t **R**est — so the
IndexedDB payloads are probably ciphertext keyed by `browserEncryptionMeta`. Treat
the **in-memory Relay store as the only guaranteed-plaintext source**; only mine
IndexedDB if a probe confirms `reverb.message` is decrypted (would give a clean,
iMessage-`chat.db`-style history read).

## Drift & re-derivation

If IG ships a breaking Web update: re-run the fiber-walk probe (Relay's env-on-fiber
location is stable API-wise; only `__typename`s / field names drift). For protocol
reference, `go.mau.fi/mautrix-meta/pkg/messagix` reverse-engineers the same schema
(but note: messagix *hardcodes* doc_ids in a Go map — they rot on every Meta deploy).

**Operation doc_ids — never hardcode.** Resolve at runtime:
`require('<Op>.graphql').params.id` (via `browser.eval`), exactly as
`send_reaction` does. To rediscover operation *names* after a breaking build,
re-dump the client module registry — the general method (bundler-agnostic, reads
the registry out of `require`'s V8 `[[Scopes]]` closure over CDP) is at
`read({id:"reverse-engineering-runtime-internals", volume:"system"})`; the
resulting IG Direct operation catalog + transport map is in
[`operations.md`](./operations.md).

## Content model & backlog

`mapMsg` branches on the CONTENT record's `__typename` (more robust than the
`content_type` enum) and attaches media / share / system bodies + reply + flags.
**These typenames were VERIFIED against real messages (2026-07-06/07, @ksubedi +
@massimilianohasan threads) — the earlier inferred names (`SlideMessagePhotosContent`,
`SlideMessageAudioContent`/`VoiceContent`, `c.photos`/`c.audios`) were WRONG and
never fired.** The real map:

| content `__typename` | what | media field | read status |
|---|---|---|---|
| `SlideMessageText` | plain text (also inlined on `text_body`) | — | ✅ verified |
| `SlideMessageAdminText` | system log line → `type:'system'` (folds `text_fragments`) | — | ✅ verified |
| `SlideMessageVideosContent` | video → `media[]` | `videos` | ✅ verified + downloaded |
| `SlideMessageImageContent` | photo → `media[]` | `attachments` | ✅ verified + downloaded |
| `SlideMessageAudiosContent` | voice note → `media[]` (`durationMs` from `playable_duration_ms`) | `audio_attachments` | ✅ verified + downloaded |
| `SlideMessageAnimatedMediaContent` | GIF/sticker → `media[]` (`attachment_mp4_url`/`webp_url`) | `animated_media` | ✅ verified + downloaded |
| `SlideMessageXMAContent` | rich share → `attachment` (`xma` deref) | — | ✅ verified (5 XMA subtypes) |
| `SlideMessageRavenImageContent`/`…VideoContent` | **view-once ("Raven")** → `isViewOnce`, single `attachment` ref | `attachment` | ⚠️ shape captured (consumed=null); inner dict UNVERIFIED |

**XMA subtypes** (`mapXma`, strips `SlideMessage…XMA` → `kind`): `Portrait` (story
reply, `eyebrow_text`), `Standard` (link/group share, `title_text`+`target_url`),
`Layered` (reel/post share, `title_text`+`subtitle_text`+`target_url`), `Placeholder`
+ `ExpiredPlaceholder` (unavailable/expired). All 5 verified live. `subtitle` reads
`header_subtitle_text` ‖ `subtitle_text` ‖ `caption_body_text`.

**Media download (`get_message`)** — two-stage, both PROVEN live: (1) in-page
`fetch` for CORS-readable hosts (`*.fbcdn.net` / `scontent` / `external` — image,
video, GIF); (2) engine-side `http.request` fallback for `cdn.fbsbx.com` **voice
notes, which are CORS-blocked** in the browser. Bytes → base64 → `blobs.put`; no
decryption (CDN urls are plain signed links, not E2EE-at-rest). 10MB per-item +
cumulative cap. Needs the `blobs` + `http` services (frontmatter).

`mapMsg` output additions (set only when present): `type`
(`text`/`share`/`video`/`image`/`audio`/`animated`/`system`), `attachment {kind,
title,subtitle,eyebrow,targetUrl,previewUrl}` — the shared Messaging share
contract (system doc `messaging-shares`; XMA subtypes stay as non-`link`
`kind` values → outside-bubble rich share), `media [{type,url,previewUrl,width,
height,durationMs}]`, `replyTo {id,author,snippet,isOutgoing}`, `reactions
[{emoji,count}]`, `isViewOnce`, `viewExpiresAt`/`expiresAt`, and flags
`isForwarded`/`isPinned`/`isAiGenerated`/`isDeleted`/`isEdited`.

> ⚠️ **`timestamp_ms` is a STRING** in the store (`"1783394485069"`), not a
> number — `Number.isFinite(ms)` is false on it, so the old `isoMs` silently
> nulled `published` on EVERY message (and broke time-ordering). The `num()`
> helper coerces before any numeric read (timestamps, widths, durations, the
> `0`-sentinel view/vanish expiries).

**History pagination (DONE):** `list_messages(conversation_id, limit)` replays
IG's own messages-connection loadNext — the Relay query `IGDMessageListOffMsysQuery`
`{after: cursor, first: 20, id: thread_fbid}` via `createOperationDescriptor` +
`env.execute` — looping until `limit` loaded or `has_next_page` is false (cursor on
the thread's `__IGDMessagesList_slide_messages_connection` `page_info`). No scroll,
no open thread. PROVEN: @ksubedi 20→87 (full thread), @massimilianohasan 20→320.
The message list is a **`column-reverse`** scroller (scrollTop 0 = newest; older
history is *negative* scrollTop) — that's how the query was captured.

**Ordering (deciding rule):** never trust store-arrival order — a message's real
`timestamp_ms` (→ `published`) is the sequence key. `watch` only streams messages
newer than arm-time; **backfill and pagination-loaded history are NOT live
arrivals** (they enter the store and fire `publish` too) — pull deep history via
`list_messages`, never let it masquerade as "new". This "loaded ≠ newest" rule
applies to any store-hook watcher (e.g. WhatsApp); IG is fixed, audit the others.

Remaining (UNVERIFIED — no real example found in the @ksubedi/@massimilianohasan
threads; coded defensively, confirm before trusting):
- **Mentions** — `SlideMessage.mentions {__refs}`: field present but never populated
  in probed threads; not extracted into the entity yet.
- **Raven view-once media** — the `attachment` ref is null once consumed, so the
  inner media dict shape is unconfirmed; `isViewOnce` + type are set, media may not
  hydrate.
- **Vanish/view expiries** — `expiration_timestamp_ms` / `view_expiration_timestamp_ms`
  carry `0` (the sentinel) on normal messages; `>0` surfaces as `expiresAt`/
  `viewExpiresAt` but no non-zero example was seen to confirm semantics.

## Open questions / next steps

Read, live `watch`, `send_message`, AND `send_reaction` (add/remove) are **done +
proven end-to-end** on `mode:attach` (Joe's real Brave — decided 2026-07-06; the
`_MODE` comment in `instagram.py` has the why). Remaining:

1. **Typing + mark-read** — `send_typing`/`mark_read` still stubbed, but their
   transports are now **resolved** (module-registry dump, 2026-07-06 — full detail
   in [`operations.md`](./operations.md)):
   - **mark-read is a Relay GraphQL mutation** — `useIGDMarkThreadAsReadMutation`
     (doc_id `27356881703909995`); replay via `commitMutation` exactly like
     `send_reaction`. (The earlier "*might* be a mutation" is confirmed: the
     `useIGDMarkThreadAsRead` module depends on `CometRelay` +
     `useIGDMarkThreadAsReadMutation.graphql` and its body calls `.useMutation()`.)
   - **typing-send rides MQTT** — `IGDMAPISendTypingIndicator` publishes over
     `MqttBypassDGWClient` (`indicate_activity`) — which is *why* typing fired no
     Relay/fetch/XHR. Cleanest impl: call IG's own `IGDMAPISendTypingIndicator`
     rather than reimplement the wire. (Receive-typing is a GraphQL subscription.)

   Both mechanisms were confirmed by reading the dispatching modules' deps (no
   live send fired); each still needs one end-to-end send test before shipping.
   Do NOT ship the stale `/direct_v2/…` REST endpoints unverified.
2. **Richer read content** — see "Content model & backlog" above.
3. Delete the old `_joe/apps/.needs-work/instagram` stub (endpoint inventory now
   captured here under "Send strategy").

**Backfill note (by design):** receiving/opening a thread makes Relay load that thread's recent history, so `watch` emits those messages too (not just the one that just arrived). The seen-set dedups and every message carries its real `timestamp_ms`, so ordering is correct — treat it as free history import. If ever undesirable, filter `emit()` to `timestamp_ms > armedAt`.

### Resolved (proven 2026-07-06)
- ~~Run surface (attach vs background)~~ → **`attach`** (Joe's real Brave): reuses his real IG cookies — no login/QR/copy. Background daemon rejected: a separate profile can't reuse the live session (Chromium locks a profile to one process; cookie-copy trips IG's device-fingerprint checks). Backend browser = **Brave**.
- ~~Full `watch` pipeline (in-page → graph)~~ → a live "pineapple" DM landed as a `message` node via `plugins.run instagram.watch`.
- ~~`store.notify` push granularity / which tap~~ → **`store.publish` fires first** on a live inbound (even to a non-open thread); hook publish+notify, dedup on `message_id`, skip empty skeleton records.
- ~~Engine registration / real-dispatch~~ → new plugin dir loads from disk with no restart; `check_session`/`list_messages`/`list_conversations` verified via `plugins.run`.
- ~~Viewer `sender_fbid` for `isOutgoing`~~ → `SlideUser.igid == ds_user_id` → its `.id`.
- ~~Thread-list mapping~~ → `XFBIGDirectViewerThread` records.
- ~~Sender → username~~ → `SlideMessage.sender` → `SlideUser.user_dict` → `XDTUserDict.username`.
