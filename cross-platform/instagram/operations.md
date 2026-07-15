# Instagram Direct — operation catalog & transport map

The concrete GraphQL operations, persisted **doc_id**s, and dispatch transports
behind Instagram Direct, extracted from the live web client's module registry.
Companion to [`readme.md`](./readme.md) (the pipeline reference); this file is the
**operation-level** reference — what to replay and how each action actually
travels.

**How this was derived:** IG web is a module-bundler SPA whose registry is
closure-private (`require(name)` resolves a known module but can't list them).
Dumped the full registry — **10,591 modules, 598 `*.graphql` operations** — by
reading it out of the `require` function's V8 `[[Scopes]]` closure over CDP (the
general method, bundler-agnostic, is documented at
`read({id:"reverse-engineering-runtime-internals", volume:"system"})`). Then
`require(name).params.id` on each operation module yields its live doc_id, and a
module's **dependency names** (kept un-minified by the bundler) reveal how each
action dispatches — GraphQL vs the realtime wire — without triggering it.

> Re-derive on drift: for a known operation name, `require('<Name>.graphql').params.id`
> via `browser.eval` gives the current doc_id. To re-enumerate names after a
> breaking build, re-run the `[[Scopes]]` registry dump (see the system doc above).
> doc_ids rotate on Meta deploys — never hardcode them in Python; resolve at
> runtime from the operation module, exactly as `send_reaction` does.

## Transport map — how each Direct action travels

The deciding question for any new write op: **GraphQL mutation (replayable by
doc_id) or realtime MQTT wire (drive the client's own send fn)?** Read the
dispatching module's deps to know before you build.

| Action | Transport | Module / how it dispatches | Status |
|---|---|---|---|
| **Stories tray (home)** | Relay store read | `XDTMaterialTray` / `XDTReelDict` from `xdt_api__v1__feed__reels_tray` (home `/`) | ✅ shipped (`list_posts` → `feeds`, 2026-07-13) |
| **Story media hydrate** | GraphQL query replay | `PolarisStoriesV3ReelPageStandaloneQuery` `{reel_ids_arr}` → `XDTReelDict.items` / `XDTMediaDict` | ✅ shipped (`get_post`, 2026-07-13) |
| **Send reaction** | GraphQL mutation | `IGDirectReactionSendMutation` (doc_id below) via `commitMutation` | ✅ shipped (`send_reaction`) |
| **Mark thread read** | **GraphQL mutation** | `useIGDMarkThreadAsReadMutation` replayed via `commitMutation`; vars `{data:{item_id:'', message_id:<newest mid>}, metadata:{ig_thread_igid:<thread.thread_id>}}` | ✅ **shipped** (`mark_read`, 2026-07-07) |
| **Mark thread UNREAD** | GraphQL mutation | `IGDThreadListActionsMarkUnreadOptionOffMsysMutation` replayed via `commitMutation`; vars `{thread_fbid:<thread_fbid>, marked:true}` (`false` un-marks). NB `thread_fbid`, **not** the ig `thread_id` mark_read uses. Module is lazy — force-load via its entrypoint first (see below) | ✅ **shipped** (`mark_unread`, 2026-07-07) |
| **Send typing** | **MQTT wire** | call `IGDMAPISendTypingIndicator(<thread.thread_id>, isTyping)` — it publishes `indicate_activity` over `MqttBypassDGWClient` | ✅ **shipped** (`send_typing`, 2026-07-07) |
| **Receive typing** | GraphQL subscription | `IGDTypingIndicatorClientSubscription` (doc_id below) | catalogued |
| **Send text** | E2EE realtime wire | IG client does the Signal send (plugin drives the composer UI) | ✅ shipped (`send_message`) |

Both `mark_read` and `send_typing` shipped 2026-07-07 — verified end-to-end
against Joe's real @ksubedi thread (mark-read got a clean Relay `onCompleted`
ack; typing published over MQTT with no error). The variable shapes below were
lifted from the live hook modules, not guessed.

### Implementation notes (as shipped)

- **`mark_read`** — replays `useIGDMarkThreadAsReadMutation` via `commitMutation`
  exactly like `send_reaction`. Variables (from the live `useIGDMarkThreadAsRead`
  hook source): `{data:{item_id:'', message_id:<newest loaded mid>}, metadata:{
  ig_thread_igid:<thread.thread_id>}}`. **`ig_thread_igid` is the thread's
  `thread_id` field, NOT `thread_fbid`** — resolve the thread record by
  thread_fbid, read its `thread_id`. The client also fires a
  `…ValidationMutation` (`35211594988486314`) alongside; the primary alone was
  sufficient for the server to accept the read (no validation mutation needed).
- **`send_typing`** — calls IG's own `require('IGDMAPISendTypingIndicator')(
  thread_id, isTyping)`; the fn wraps the `{action:'indicate_activity',
  activity_status, client_context:'', thread_id}` payload and sends it over the
  client's existing `MqttBypassDGWClient` (`/ig_send_message`). `thread_id` is
  again the ig `thread_id`, not `thread_fbid`. No wire to reimplement — the
  `usePublishTypingIndicator` React wrapper is the only extra layer, and it just
  gates on group/blocking/self before calling the same fn.

### `mark_unread` — shipped (2026-07-07)

Replays IG's own `IGDThreadListActionsMarkUnreadOptionOffMsysMutation` via
`commitMutation`, exactly like `mark_read` — no UI drive, no view-yank, doc_id
resolved from the compiled node at runtime. Variables
`{thread_fbid:<thread_fbid>, marked:true}` (`false` clears). **`thread_fbid`,
not** the ig `thread_id` `mark_read` uses.

**The lazy-module wrinkle (the one real difference vs mark_read).** This
mutation module ships in the thread-row "…" menu chunk, which is code-split, so
`require('IGDThreadListActionsMarkUnreadOptionOffMsysMutation.graphql')` returns
`undefined` in a cold session (mark_read's module is in the main Direct bundle,
always present). The connector force-loads it **headlessly** — no real-mouse menu
drive — via the menu's Relay entrypoint:

```js
const ep = require('IGDThreadListActionsPopoverOffMsys.entrypoint');
await ep.root.load();   // JSResource for IGDThreadListActionsPopoverOffMsys.react
require('IGDThreadListActionsMarkUnreadOptionOffMsysMutation.graphql'); // now registered
```

The `.entrypoint` descriptor stays in the main bundle even when its chunk is
lazy; its `.root` is a `JSResource` whose `.load()` pulls the chunk and
transitively registers the mutation + its doc_id. PROVEN cold (after a hard
reload): a bare `require(MUT)` is `undefined`; `ep.root.load()` then lands doc_id
`25709394752070378`. (`JSResource(MUT).load()` alone does **not** work — the
`.graphql` isn't its own chunk; you must load the component entrypoint.)

**Correcting the old "headless replay blocked" diagnosis.** The earlier writeup
claimed a bare `commitMutation` was rejected (`1675002 Unauthorized logged out
query`) where IG's own `useMutation` succeeded — i.e. a commit-mechanism gap.
That was a **misdiagnosis**. On 2026-07-07 the engine's IG session had
silently logged out (no `sessionid` cookie; stale `fb_dtsg`), and in that state
**every** write fails identically — the shipped `mark_read` *and* a real
human's "…" → "Mark as unread" click both return `1675002`. Reads kept working
only because they replay the already-hydrated Relay store (no `fb_dtsg`
needed). Once the session was re-authenticated, a plain `commitMutation` on the
single live `PolarisRelayEnvironment` is accepted for this mutation exactly like
any other. There is no special "OffMsys" actor context, no forActor/MultiActor
env — the fiber holds one env, reachable via props *and* context. Actor `av` =
`CurrentUserInitialData.NON_FACEBOOK_USER_ID` (a pure-IG account reports
`USER_ID:"0"` — the "logged out" trap) is injected by IG's own network layer;
we supply only variables. Lesson: when *all* writes return `1675002` but reads
work, suspect the session, not the mutation.

## Direct messaging doc_ids (proven live)

Pulled via `require(name).params.id` against a logged-in session. **Reference
snapshot — resolve at runtime, do not hardcode** (Meta rotates these per deploy).

| doc_id | kind | operation |
|---|---|---|
| `24374451552236906` | mutation | `IGDirectReactionSendMutation.graphql` |
| `27356881703909995` | mutation | `useIGDMarkThreadAsReadMutation.graphql` |
| `25709394752070378` | mutation | `IGDThreadListActionsMarkUnreadOptionOffMsysMutation.graphql` (lazy — force-load its entrypoint) |
| `35211594988486314` | mutation | `useIGDMarkThreadAsReadValidationMutation.graphql` |
| `27563068933278040` | subscription | `IGDTypingIndicatorClientSubscription.graphql` |
| `26911679871773184` | mutation | `IGDirectTextSendMutation.graphql` |
| `25766288509716264` | mutation | `IGDirectMediaSendMutation.graphql` |
| `27442850591982122` | mutation | `IGDirectMediaShareMutation.graphql` |
| `32480262318254796` | mutation | `IGDirectEditMessageMutation.graphql` |
| `32089613413987432` | mutation | `IGDirectAnimatedMediaSendMutation.graphql` |
| `24914262484908659` | mutation | `IGDirectAvatarStickerSendMutation.graphql` |
| `26601474479505807` | mutation | `IGDirectCutoutStickerShareMutation.graphql` |
| `25056930583969517` | mutation | `IGDirectGenericXMAShareMutation.graphql` |
| `27277149101947973` | mutation | `IGDirectLinkShareMutation.graphql` |
| `26883421864608852` | mutation | `IGDirectMusicStickerShareMutation.graphql` |
| `26212720875066239` | mutation | `IGDirectReelShareMutation.graphql` |
| `24640017885680534` | mutation | `IGDirectStoreStickerSendMutation.graphql` |
| `26219781510996267` | mutation | `IGDirectStoryShareMutation.graphql` |
| `26536543495958378` | mutation | `IGDirectStoryShareReplyMutation.graphql` |

> `IGDirectTextSendMutation` exists as a GraphQL mutation, but the shipped client
> sends text over the **E2EE Signal wire**, not this mutation — the plugin drives
> the composer UI for `send_message` for that reason. Treat a GraphQL text-send
> as unverified until a transport probe shows the client actually using it.

## Stories tray (home feed) — shipped 2026-07-13

Home `/` loads `xdt_api__v1__feed__reels_tray` into Relay as:

| Typename | Role |
|---|---|
| `XDTMaterialTray` | Tray root — `tray.__refs` → reel ids |
| `XDTReelDict` | One author's ring — `seen`, `latest_reel_media`, `expiring_at`, `ranked_position`, `user` |
| `XDTUserDict` | Author — `username`, `pk`, `profile_pic_url` → `XDTProfilePicUrlInfo` |

**Seen → end:** `seen >= latest_reel_media` (both unix seconds) means the ring
is fully viewed; `list_posts` emits those after unread rings (Instagram's tray
order). Partial views (`0 < seen < latest`) stay with unread.

**Surface:** stories need `/` (or a warm tray already in the store). DMs need
`/direct`. One shared background tab — each surface's `_ensure_*` navigates when
its records aren't warm.

## Story media hydrate — shipped 2026-07-13

Tray `XDTReelDict` has **no `items`**. Opening a story in the UI fires
`PolarisStoriesV3ReelPageStandaloneQuery` (XHR `/graphql/query`, friendly name
captured live) with:

```json
{
  "reel_ids_arr": ["<user_pk>"],
  "__relay_internal__pv__PolarisCommunityNoteStoriesLabelEnabledrelayprovider": true,
  "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": false
}
```

Replay (same pattern as DM `IGDMessageListOffMsysQuery` pagination):

```js
require('PolarisStoriesV3ReelPageStandaloneQuery.graphql')  // resolve doc_id at runtime
createOperationDescriptor(node, vars)
env.execute({operation}).subscribe({complete})
```

Normalizes into:

| Typename / id | Role |
|---|---|
| `XDTReelsMedia` at `client:root:xdt_api__v1__feed__reels_media(reel_ids_arr:[…])` | query root |
| `XDTReelDict.items.__refs` | story cards |
| `XDTMediaDict` | one card — `media_type` (1=image, 2=video), `taken_at`, `image_versions2.candidates`, `video_versions` |

`get_post(story:<pk>)` loads the reel, returns the first item with `attaches`
(CDN via `browser_session.response_body` → blobs; in-page fetch / engine http
as fallbacks) plus `ringPosts` for multi-item Social nav. Item ids:
`story:<pk>:<media_pk>`.

Also captured (not yet wired): `PolarisStoriesV3SeenMutation` — mark viewed.

## Operation-object shape (Relay)

Each `*.graphql` module exports a compiled `ConcreteRequest`:

```
require('X.graphql')            → ConcreteRequest
  .params.id                    → persisted doc_id (what you POST)
  .params.name                  → operation name
  .params.operationKind         → "mutation" | "query" | "subscription"
  .params.text                  → null (persisted; text lives server-side by id)
```

Shape taxonomy seen in the registry (so you know what's replayable):

| Name suffix | What | Has doc_id? |
|---|---|---|
| `*Mutation.graphql`, `*Subscription.graphql`, `*Query.graphql` | a `ConcreteRequest` (an **operation**) | ✅ `.params.id` |
| `*_XFBIGDirectViewerThread.graphql` (and other `_XFB…`/`_XDT…`) | a Reader **fragment** (a selection, not a request) | ❌ |
| `*_instagramRelayOperation` | a string (IG's operation wrapper) | — |
| `useIGD*`, `IGDMAPISend*` | the action **function/hook** (drive or read its deps) | — |

## Replay endpoint

Persisted operations POST to `/api/graphql` (same-origin from the logged-in tab —
cookie + `x-csrftoken` already present; see `send_reaction` in `instagram.py`).
Prefer driving Relay's own `commitMutation(env, {mutation, variables, onCompleted,
onError})` over a hand-built POST — it fills every ambient field and `onCompleted`
carries Instagram's own server ack (a confirmed result, not a fire-and-forget).
`env` is the Relay environment the plugin already locates via the fiber walk
(readme → "Relay environment discovery").
