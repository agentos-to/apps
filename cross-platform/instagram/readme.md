---
id: instagram
name: Instagram
description: Live Instagram DMs via the logged-in web client — read, send, and watch new direct messages in real time by hooking Instagram's own React/Relay store
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

> **Status: working (2026-07-06).** Read (`list_conversations`/`list_messages`),
> live `watch` (real-time receive), and `send_message` are all *proven
> end-to-end* against Joe's real logged-in session on `mode:attach` (see
> Internals → "What's proven"). Remaining niceties: reactions, typing,
> mark-read. This readme is the durable reference.

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
- **Chat history:** `list_messages` with `conversation_id`
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
| **Read ops run end-to-end through real engine dispatch** (not just probes) | ✅ proven | `plugins.run` `mode:attach`: `check_session` (111ms), `list_messages`, `list_conversations` all return correct entities |
| **`watch` push — full pipeline: live DM → graph `message` node** | ✅ **proven end-to-end** | armed via `plugins.run instagram.watch`; a live "pineapple" DM surfaced as graph node `wkeqg3` (`author:"Me"`, `isOutgoing:true`, thread + ts) ~instantly. Path: store `publish` → CDP console marker → `live_entity::write` → graph. |
| **`send_message` — composer-UI drive + event-driven receipt** | ✅ **proven end-to-end** | 4 live DMs to @ksubedi via `plugins.run`; navigate `/direct/t/<thread_fbid>/` → type into `role:textbox` → `key Enter` (IG does the E2EE send); receipt resolves on `store.publish` |
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

## Reactions — reverse-engineering notes (IN PROGRESS, not built)

`send_reaction` is stubbed. Reactions are harder than text send (no simple
composer surface). Learned probing live 2026-07-06:

**UI path (fragile — not the recommended final impl):**
- Hover/click a message → toolbar: face (React), reply, kebab (more). The face
  button's `aria-label` = `React to message from <user>`; its clickable ancestor
  (`div[role=button][aria-haspopup=dialog]`, fiber chain `IGDSIconButton →
  BaseButton → PressableText`) has a React `onClick` that opens an emoji-picker
  popover. Calling that `onClick` natively DID open it (confirmed visually) — but
  a mock-event call is timing-flaky (a second call toggles it shut).
- The picker is reachable via the face button's `aria-controls` id (or
  heuristically: a container with 5–8 small ~48px clickable children). Its emoji
  options are **unlabeled sprites** (no `aria-label`/text) — can't target ❤️ by
  label; it's the **first** option (❤️ 😂 😮 😢 😡 👍, then "+"). Invoking the
  first child's `onPress`/`onClick` should fire ❤️, but reliably finding the
  container + surviving the picker's open/close is brittle.

**Recommended durable path — call IG's own reaction action, skip the UI:**
1. Find the TRANSPORT first: instrument BOTH `env.getNetwork().execute` (Relay
   mutations route through it) AND `fetch`/`XHR`, then **react ❤️ manually once**
   and see which fires.
2. Relay mutation / GraphQL POST → capture `doc_id` + variables (`message_id`,
   `emoji`, `thread_fbid`) and replay (`commitMutation` on the env, or a
   same-origin fetch). Very native.
3. Nothing on fetch/Relay → reactions ride the **E2EE MQTT websocket** (same
   channel text sends use) → need IG's client reaction fn (deeper dig, the way
   the composer was the answer for text). Ids: `SlideMessage.message_id` +
   `thread_fbid`; reaction record = `MessagingReaction`/`XFBSlideReaction`.

**Shared-browser gotcha:** the attach browser is shared — another agent's tab can
refresh instagram.com mid-probe and wipe picker/hook state (happened 2026-07-06).
`watch` auto-reinstalls on reload; ad-hoc probe state does not.

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
reference, `go.mau.fi/mautrix-meta/pkg/messagix` reverse-engineers the same schema.

## Open questions / next steps

Read, live `watch`, AND `send_message` are all **done + proven end-to-end** on
`mode:attach` (Joe's real Brave — decided 2026-07-06; the `_MODE` comment in
`instagram.py` has the why). Remaining:

1. **Reactions** — `send_reaction` is stubbed; see "Reactions — reverse-engineering
   notes" above. Next: instrument transport (Relay `execute` + fetch/XHR), react
   ❤️ manually once, see what fires, then replay/call it.
2. **Typing + mark-read** — `send_typing`/`mark_read` stubbed; try the
   `/direct_v2/threads/{id}/activity/` and `/items/{id}/seen/` in-page fetches (or
   the reaction-style action once found). Also fold READ reactions on inbound
   messages (`msg_reactions` `{__refs}` → `MessagingReaction`) into `mapMsg`.
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
