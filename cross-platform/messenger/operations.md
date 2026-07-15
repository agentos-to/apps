# Facebook Messenger (web) — reverse-engineering reference

The durable RE reference for a Messenger DM connector, learned by live CDP
probing of Joe's real logged-in `facebook.com/messages` on **2026-07-08**.
Companion to `readme.md`/`messenger.py`. This file is the
operation-level map: where the decrypted data lives, the exact hook, the decode,
and the transports.

> **Status: connector built; read/decrypt proven.** The browser plane targets
> workers by behavior when Chromium omits title/url, and `browser.targets`
> reports inventory + bounded liveness in one call. Live watch capture and the
> navigation-free LS-task send remain separate follow-ups.

## The one-sentence architecture

Modern Messenger web is **not** Instagram's Slide/Relay client — it's Meta's
**Lightspeed / MSYS** E2EE client (codename **Armadillo**), and its decrypted
message store + Signal crypto + realtime wire all run inside a **shared worker**
(`MAWMainV4WebWorkerBundle`), not the page. So the IG plugin's fiber-walk →
Relay-`SlideMessage` read path **does not port**. Instead we walk the *worker's*
module registry, wrap one stable stored-procedure map for `watch`, and read the
worker's Dexie tables for history — the same "hook the client's own decrypted
store, never the wire" philosophy as WhatsApp/IG, one level deeper.

## Surface & session (deltas from Instagram)

| | Instagram plugin | Messenger |
|---|---|---|
| Logged-in domain | `instagram.com` | **`facebook.com`** (messenger.com is a SEPARATE session — logged out even when facebook.com is in) |
| DM URL | `instagram.com/direct/inbox/` | **`facebook.com/messages/`** (threads redirect to `/messages/e2ee/t/<threadKey>/`) |
| Session cookie (true auth signal, httpOnly) | `sessionid` | **`xs`** |
| Viewer id | `ds_user_id` cookie (IGID) → fiber-scan for FBID | **`c_user` cookie IS the viewer FBID** (plain, non-httpOnly) — no scan, the IG `__viewerFbid` hack deletes |
| Login gate seen | username/pw + 2FA | facebook.com "Continue <name>" → **password re-confirm** step even with `xs` present |
| Where plaintext lives | page Relay store (`SlideMessage`) | **the `MAWMainV4WebWorkerBundle` shared worker** (page Relay store has ZERO message records — proven) |

## The technique that cracked it — worker require-registry walk

(General method: system doc `reverse-engineering-runtime-internals`, Technique 1.
Two Messenger-specific extensions: target the **shared worker**, and read module
**factory source** to follow the require chain.)

1. `browser.targets({kinds:['worker'], probe:'typeof self.require'})` → find the
   responsive worker by behavior even if Chromium reports empty title/url.
2. `Runtime.enable` + **`Debugger.enable`** (required — `[[Scopes]]` only
   materializes with the Debugger domain on).
3. `Runtime.evaluate` `self.require` → objectId → `getProperties` →
   `internalProperties["[[Scopes]]"]` → walk scopes → **the biggest plain object
   in require's closure is the module registry** (6761 modules). Enumerate keys.
4. `registry[name].factory` → `Function.prototype.toString(...)` reads the
   module's (minified but readable) SOURCE; `.dependencies` names what it pulls.
   Chaining "read source → see its requires → read those" is how the hook point
   below was found without running anything.

Registry entry shape (Metro/Haste): `{id, exports, defaultExport, factory,
dependencies, ...}`. Cached export lives at `.exports` AND `.defaultExport`.

The worker is a `worker_type=MODULE` Emscripten/WASM bundle (`require`/`__d`
present, `__r` undefined; WebGL/Emscripten globals = the MSYS SQLite WASM).

## WATCH — the hook (proven installable; the WhatsApp-`WAWebCollections` analog)

The datascript pipeline (from reading factory source):

```
LSDatascriptEvaluator            (thin wrapper, 1485 chars)
  → LSFactory(db, opts)
      → require("LS")(1, LSMetadata.schema, LSDynamicDependencies.cachedModules, db, …)
                                             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  → new LSDatascriptJSONEval(script, runtime).eval()   runs the pushed procedure tree
```

**`LSDynamicDependencies.cachedModules`** is a stable plain object mapping
**camelCase sproc name → function** (81 entries), read by the LS interpreter on
every eval. This is the hook point — patch the functions in it once; every future
message/reaction/receipt flows through the wrapper. **No ref-capture problem** (it
is read dynamically per-call, unlike the individual `LSUpsertMessage` module which
the evaluator captures at init — wrapping THAT via the registry cache does NOT
intercept, confirmed: `evals=0`). **No birth-injection needed** (wrapping at
worker birth via `waitForDebuggerOnStart` destabilized the E2EE sync — 7 worker
respawns, "Couldn't load E2EE chats" — do NOT go that route).

```js
const d = require('LSDynamicDependencies').default || require('LSDynamicDependencies');
const cm = d.cachedModules;                       // {sprocName: fn}, 81 entries
const orig = cm.upsertMessage;
cm.upsertMessage = function(){ emit(arguments); return orig.apply(this, arguments); };
```

Keys to wrap: `upsertMessage`, `upsertReaction`, `updateReadReceipt`,
`deleteThenInsertThread` (+ `deleteThenInsertMessageRequest`, `updateThreadSnippet`
as needed). Verified installable live: `upsertWrapped:true` on the running worker.

### The decode (positional args — CONFIRMED against pristine sproc source)

✅ **Confirmed 2026-07-08** by reading the pristine `upsertMessage`/
`upsertReaction` source directly (a prior probe left `sink(...)` wrappers on the
three captured sprocs; the real function is `orig` in the wrapper's closure —
extracted via a targeted Debugger `[[Scopes]]` read on the wrapper, see
`probe_orig.py`). The `.add({...})`/`.put({...})` object keys ARE the Dexie
column names, so this table validates BOTH the watch decode (positional) AND the
read decode (row field names) at once.

`upsertMessage` → `db.table(12).add({...})` (`.put` on an authority-newer
update), verbatim from source:

| arg | field | | arg | field |
|---|---|---|---|---|
| `[0]` | text | | `[11]` | stickerId |
| `[1]` | subscriptErrorMessage | | `[12]` | isAdminMessage |
| `[2]` | authorityLevel | | `[13]` | messageRenderingType |
| `[3]` | threadKey (== conversationId) | | `[15]` | sendStatus |
| `[5]` | timestampMs | | `[16]` | sendStatusV2 |
| `[6]` | primarySortKey | | `[17]` | isUnsent |
| `[7]` | secondarySortKey | | `[23]` | replySourceId |
| `[8]` | messageId | | `[27]` | replySnippet |
| `[9]` | offlineThreadingId (otid) | | `[29]` | replyToUserId |
| `[10]` | senderId (FBID) | | `[43]` | isForwarded |
| last | the transaction (`t`, has `t.db.table(N)`) | | `[68]` | editCount |

(matches mautrix-meta's `LSInsertMessage` index tags exactly; fields run to
`[81]` — reply media, ephemeral/expiry, admin-signature, subthread, translation.)

`upsertReaction` → `db.table(8).put({...})`, verbatim: `[0]`=threadKey,
`[1]`=timestampMs, `[2]`=messageId, `[3]`=actorId, `[4]`=reaction(emoji),
`[5]`=authorityLevel, `[6]`=reactionCreationTimestampMs.

`sender==c_user` ⇒ `isOutgoing`. Threads: `deleteThenInsertThread` →
`db.table(9)` — `threadKey:e[7], threadName:e[3], threadPictureUrl:e[4],
threadType:e[9], folderName:e[10], lastActivityTimestampMs:e[0],
unreadMessageCount:e[89], memberCount:e[77]` (also read directly, unwrapped).

## READ — plaintext under encryption-at-rest (SOLVED — the maw_ear codec)

✅ **Solved & validated live 2026-07-08** (300/300 messages + all threads
decrypted, 0 errors). General technique: [[reverse-engineering-runtime-internals]]
Technique 1c + "requireable decrypt module"; toolkit `__re.ear()` / `decryptEAR()`.

**The store is EAR (encrypted-at-rest).** Every row of the encrypted tables
(`messages`, `threads`, `participants`) is one sealed blob in an
**`_encryptedContent`** column; only routing/index fields stay plaintext
(`threadJid, msgId, externalId, sortOrderMs, protocolMsgId, keyVersion_S456130,
randomisedVersion_S456130, encryptedWith_S456130, updatedAt_S456130`). Message
text/type/author/ts/quote are ALL inside the blob — so a raw Dexie/IndexedDB read
yields no body. (Correction to a prior note: there is **no** plaintext
`msgContent`/`text` column; the sproc write-keys are unrelated to the at-rest
schema, and `getDBExn()` returns a custom transactor, not a `.table()` Dexie db.)

**The decrypt is the client's own codec, requireable by name** (found via the
registry walk — the module registry is `require`'s closure var `o`, 6743 name→
descriptor entries; grep `o[name].factory` sources for `_encryptedContent`):

| module | role |
|---|---|
| **`MAWDbObjEncryption`** | the EAR codec — `decryptDbObj(row, table, keyResolver)` → fully decrypted row; `encryptDbObj` (write); `ENCRYPTED_COLUMN_NAME='_encryptedContent'` |
| **`MWEARKeychainV3`** | the keychain — **`getDbEncryptionKey` IS the keyResolver** (reads the in-memory per-version key `Map`, "settled" post-PIN-restore) |
| `MAWKeychainNaClCrypto` | the NaCl decrypt (`decryptArrayBuffer`), called inside the codec |
| `MAWDbObjDecode` | decodes the decrypted bytes → the message object |
| `MAWDbMiddlewareMutations` | the Dexie read/write hook wrapping the codec (`getValue`=decrypt, `mutateValue`=encrypt) — reads through the *hooked* Dexie table auto-decrypt |

**The call — synchronous, pure, no transaction, no lock, no `[[Scopes]]` at runtime:**
```js
const enc = require('MAWDbObjEncryption');
const resolver = require('MWEARKeychainV3').getDbEncryptionKey;
const plain = enc.decryptDbObj(rawRow, 'messages', resolver);   // or 'threads'
```
`decryptDbObj` returns a non-encrypted row unchanged (guards on
`TABLES_TO_ENCRYPT.includes(table) && '_encryptedContent' in row`), so it's safe
over every row.

**Decrypted plaintext schema** (validated):
- **message**: `type` (`Text`|`Admin`|`Image`|`XMA`|`Ptt`), `ts` (unix **SECONDS**,
  not ms; **`0` = unset** — never publish as ISO, it paints epoch day chips),
  `author` (`'@me'` ⇒ outgoing, else `'<fbid>@msgr'`),
  **`msgContent.content`** (the text string), `quote` (reply →
  `{content:{…}, remoteJid}`), `isForwarded`, `editCount`, `externalId`, `msgId`,
  `threadJid`, `serverTs` / `sortOrderMs` (fallbacks when `ts` is 0; ms or sec —
  values `>1e12` are already ms), expiry/delete ts. (XMA/media rows carry no
  `msgContent.content` — payload lives elsewhere; handle separately.)
- **thread**: `jid` (threadJid), `authoritativeThreadKey` (numeric threadKey),
  `folder`, `newestMsgTs` (same `0`/missing → null published discipline),
  `lastReadMsg`, `snippetMsg`, `threadOrder`. (The
  `participants` table was **empty** in the live store — 1:1 names come from a
  profile/contacts source, TBD; not an encryption blocker.)

**Wire contract — `list_messages` is newest-first.** Same as WhatsApp/Instagram:
sort by `published` descending, `slice(0, limit)`. Messaging UI sorts ascending
for display (oldest top → newest bottom); do not return oldest-first or a blind
`.reverse()` will invert the thread.

**Cold threads (blank Messaging pane).** The EAR `messages` store is only
hydrated for threads the E2EE client has opened/synced. Older chats still
appear in `list_conversations` (thread row + snippet + `newestMsgTs`) but EAR
holds only an Admin/cutover row (`ts=0`, empty body) — so Messaging looked
empty. `list_messages` detects Admin-only EAR **or** worker `not_ready` and
**hydrates from the page**: navigate to `/messages/t/<threadKey>/` (confirm
the URL actually contains the threadKey), then parse accessibility-tree labels
(`Message sent <when> by <who>: <text>`). Page LS `messages.text` is still
ciphertext; the rendered a11y tree is the plaintext surface. Hot E2EE threads
with a live worker stay on the EAR path. Note: classic-page history for an
active E2EE thread can be a stale pre-cutover archive — prefer EAR whenever it
has real bodies.

**Read recipe (read-only, deadlock-free).** Read raw rows via a **read-only**
IndexedDB cursor on the `messages` store's `threadJid` index (derive the db name
with `require('MAWIndexedDbMetadata').dbName(require('MAWCurrentUser').getID())` —
never hardcode), then `decryptDbObj` each. This never touches the client's
transactor, takes no lock, and readonly IDB txns don't contend — the external
deadlock warning does not apply. (Alternative: read through the client's *hooked*
Dexie table `stores.MessagesStore.db.messages` inside `runInTransaction` — the
`MAWDbMiddlewareMutations` reading-hook auto-decrypts — but that takes a client
transaction; the direct-codec path is simpler and lock-free.)

**Engine capability: NONE new.** Reads use the connector's existing worker-`eval`
(`browser_session.eval(_WORKER, …)`) calling `require(...)`. The registry walk is
a *discovery-time* tool, not a runtime dependency — the connector hardcodes the
two module names (re-derive via the walk only if a Meta deploy renames them; they
are stable API-wise).

- Alternative read = arm the `cachedModules` hook, trigger a client resync, collect
  every `upsertMessage` as it re-inserts (unifies read+watch through one safe path,
  no DB driving) — still valid, but the direct decrypt above is the primary path.

## SEND / react / typing / mark-read — LS tasks

Outbound actions are **tasks** issued via `LSIssueNewTask` and serialized by
`LSTaskSerializer*` (found in the registry): `LSTaskSerializerSendMessageV2`
(text), plus mute/remove-thread/update-name serializers; reactions/typing/read are
their own tasks (mautrix labels: send=46, react=29/604, typing=3, markRead=21 —
verify against this client's serializers). `otid` (offline threading id) correlates
the optimistic row with the authoritative `upsertMessage`.

**Two send options** (decide at build): (a) **composer-UI drive** like IG — open
`/messages/e2ee/t/<threadKey>/`, type into the `role:textbox` "Write to …", press
Enter (zero API surface, human-indistinguishable, safest); (b) **issue the task**
via the client's own `LSIssueNewTask` + `LSTaskSerializerSendMessageV2` (deeper,
no view-yank, but more RE). Start with (a).

## Engine worker transport — shipped

The engine browser plane now supplies two generic primitives:

1. `browser.targets({probe,kinds,timeoutMs})` uses the browser-level CDP socket,
   flattened target sessions, a built-in `1+1` liveness check, and an optional
   in-target behavior probe. All targets run concurrently. This replaces the
   four throwaway CDP scripts used during the original diagnosis.
2. `worker:<hint>?probe=<urlencoded js>` resolves by title/url first, then picks
   the one responsive worker where the probe is truthy. The selected `targetId`
   is cached and invalidated by respawn; empty title/url is no longer fatal.

Worker `subscribe` still evaluates directly in the worker (there is no Page
domain), and the standing reconnect loop re-resolves + re-arms on respawn.
Console marker routing remains the shared `route_console_event` →
`live_entity::write` pipeline.

## Entity model (provider-uniform, same as IG/WhatsApp/iMessage)

- **conversation** — `id` = threadKey (numeric string), `name`, `isGroup`,
  `unreadCount`, participants (from `participants` table, join FBID → name).
- **message** — `id` = messageId, `content` = text, `published` = timestampMs,
  `conversationId` = threadKey, `isOutgoing` = (senderId == c_user), `from` =
  account, `reactions`, `replyTo`, `type`.
- **account** — FBID (`c_user` for viewer); handle/name/photo from `participants`.

## Prior art

mautrix-meta (`go.mau.fi/mautrix-meta`, `pkg/messagix`) reimplements this wire
**server-side** (MQTT `/ls_req`+`/ls_resp`, the same LS-table stored procedures,
Armadillo/whatsmeow E2EE). It is the field-layout reference (`table/messages.go`
index tags matched our factory-source decode). No prior art hooks the *browser*
client — this connector is new ground. E2EE delivery = "Armadillo" protobufs over
a whatsmeow-based Noise socket to `web-chat-e2ee.facebook.com`; decryption happens
in-worker, then a client-side insert lands plaintext in the Dexie tables above —
which is exactly why hooking the worker's sproc map works.

## Live verification findings (2026-07-08, bg + attach)

Proven live: the engine `worker:` target attaches + evals in the worker (both
attach and the bg profile, 81 sprocs); `check_session`/`login` (headed flip →
sign-in → session persists → `authenticated` + FBID); the `watch` hook ARMS in
bg (`upsertWrapped:true`); the watch decode (above) is confirmed from source.
What the live drive then surfaced — the real depth beyond the engine gate:

- **E2EE restore-PIN gate.** A *fresh* login (bg profile) is a new E2EE device →
  Messenger shows **"Enter your PIN to restore your chats"** on `/messages/`
  (with a "use a one-time code instead" option). Until it's entered: the
  `messages` table holds only **Admin/cutover** rows (no real bodies), no
  realtime arrives, and `getDbStatus()` sits at `migrating`/limited. The daily
  browser (already restored) doesn't hit this. **`login` must drive/surface the
  PIN step** (it's a `role:textbox` with question "Enter your PIN…").

- **The real read API (getDBExn ≠ Dexie).** `getDBExn()` throws outside a
  transaction and, when it returns, is a **custom transactor** (ctor `t`;
  `.tables[52]`, `.transact(mode, names, cb)`, `.stores`), NOT a Dexie db — no
  `.table(name)`. The Dexie db with named tables is reachable as
  **`stores.MessagesStore.db`** inside
  `require('MAWRunInTransaction').runInTransaction({MessagesStore:true}, async
  (stores)=>{…}, "name")` (read-only — the transactor filters out READWRITE
  stores, so it can't deadlock). `MessagesStore` methods:
  `create/get/update/deleteReactionsForMsg/bulkDelete/bulkGetInGroup`;
  `bulkGetInGroup(threadJid)` **projects only `{author,chat,externalId}`** (not
  full messages).

- **Named-table schemas ≠ sproc write-keys, and are ENCRYPTED AT REST.** The
  sproc `table(N)` numbers (12/8/9) are an internal numbering, NOT the named-table
  order. Real columns — `db.messages`: `msgId, threadJid, author, msgContent,
  ts, type, quote, externalId, sortOrderMs, …`; `db.threads`: `jid,
  authoritativeThreadKey, snippetMsg, newestMsgTs, folder, archived, lastReadMsg,
  newestMsg, …`. `msgContent` carries `encryptedWith_S456130: maw_ear` — it's
  **ciphertext** in IndexedDB. So a raw Dexie read never yields plaintext text.
  **Read path options:** (a) find the client's decrypt-on-read method / the
  `msgContent` codec, or (b) the **hook-unified read** — open a thread so the
  client decrypts + re-inserts, and capture the plaintext `upsertMessage` (the
  hook's args are plaintext). (b) reuses the proven watch path.

- **Send composer-drive respawns the worker.** `browser_session.navigate` to a
  thread reloads the page → the shared worker **respawns/dies** → it kills the
  watch AND the receipt eval hits a booting worker (`require` undefined). Do NOT
  full-navigate to send. Use the **LS-task path** (`LSIssueNewTask` +
  `LSTaskSerializerSendMessageV2`, issued IN the worker, no navigation).

- **Booting-worker guard.** A respawned/booting worker answers a trivial eval but
  has no `require` yet, and a bare `require` reference THROWS. The connector
  guards on `typeof self.require === 'function'` (→ `not_ready`) and uses a
  synchronous `getDBExn`-only `getDB()` (the async `getDB()` never resolves
  mid-migration → 45s hang).

The original throwaway scratchpad probes are superseded by `browser.targets`,
`browser.scopes`, `browser.inspect`, `browser.source`, and `browser.call`; do
not recreate them.

## Contact names + faces (PAGE Lightspeed join, 2026-07-09)

E2EE message **bodies** live in the worker's MAW EAR store
(`messenger_web_v1_<fbid>` `messages`/`threads` — ciphertext + routing, no
display name). Contact **display** lives in the **page** Lightspeed ReStore:

| surface | what | contact data? |
|---|---|---|
| worker MAW Dexie | E2EE threads/messages | empty `participants`; decrypted threads have no name |
| worker `GetLsDatabaseDeferredForDisplay` | Lightspeed ReStore | empty tables in this profile |
| **page** `GetLsDatabaseDeferredForDisplay.get()` | live Lightspeed ReStore | **contacts (~100+), participants, threads** |

`MWGetOtherContactOrSelf.getOtherContactOrSelf(threadKey, viewerId, tx)` proves
the join (thread → participants → non-viewer contactId → contacts). RE details:

- `store.runInTransaction(cb)` — **callback is the first arg**; defaults follow.
  The callback receives `{table, transactionTable}` (not `.tables`).
- `tx.table('contacts'|'participants'|'threads')` by **name string**. Passing
  numeric id `7` or an `LSDbV1.tables.contacts` schema descriptor throws
  (`primaryKeyIds` missing) — those objects are schema defs, not tx table keys.
- Read with `ReQL.toArrayAsync(ReQL.fromTableAscending(tx.table(...)))`.
- Ids are Meta `I64`; **always** `I64.to_string(v)` (default `String` works
  via the same path). Do not reconstruct from `[high,low]` halves by hand.

1:1 E2EE riff: the MAW thread `jid` is `<peerFbid>@msgr`, so when LS has no
participant rows for that thread the contact still resolves as
`contacts[threadKey]`. Verified: thread `21603067` → **"Kimberly Brown"** and
face URL matches the page chat-list snapshot.

Connector path: one page transaction builds maps; Python joins them onto the
worker-decrypted thread list (`_contact_index` + `_enrich_threads`) — no
per-thread calls. Rows with no LS contact yet keep an empty name (rare).

## Re-derivation on drift

Re-run the worker registry walk (structure-based, survives Meta deploys). Sproc
names (`upsertMessage`, …) and the `cachedModules` map are stable API-wise; only
arg indices / table numbers drift — re-read the sproc factory source
(`Function.prototype.toString`) to refresh the decode. Never hardcode doc_ids.
