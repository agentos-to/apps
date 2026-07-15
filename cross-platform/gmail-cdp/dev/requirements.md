# Gmail (Live) — operations & reverse-engineering log

The hard-won map of how Gmail Web's internals actually behave, so the next
agent doesn't re-derive it. The connector code (`gmail_cdp.py`) is the *what*;
this is the *why* and the *dead ends*. Keep it current — ids/positions rot, the
mechanisms don't.

Everything below was established **live** against `efisio@gmail.com` in the
engine's background profile via `services.browser_session` `eval` (`mode:
"background"`), with `commons/re/toolkit.js` (`window.__re`) injected for capture.

---

## 1. The transports Gmail actually uses

| Surface | Endpoint | Shape | Used for |
|---|---|---|---|
| Sync (list) | `POST /sync/u/N/i/bv` | positional-array | thread list (browse-view) |
| Sync (bodies) | `POST /sync/u/N/i/fd` | positional-array | message fetch-data |
| **Sync (action)** | `POST /sync/u/N/i/s` | positional-array | **every mutation + send** |
| Message source | `GET /mail/u/N/?ik=GM_ID_KEY&view=om&th=<hex>` | RFC822 in `<pre>` | full body (`get_email`) |
| Account map | `GET /mail/u/N/feed/atom` | Atom XML | signed-in accounts + a **fresh** unread/inbox oracle |
| Frontend API | `clients6.google.com/gmail/v1fpa_gmail_frontend_gwt/...` | protojson | **calendar chips + id mapping ONLY** — see §5 |

`bv`/`fd`/`s` all POST with per-account headers: `X-Framework-Xsrf-Token`,
`X-Gmail-BTAI` (a big client-state blob carrying `GM_ID_KEY`, tz, server
version), `X-Gmail-Storage-Request`, `X-Google-BTD: 1`, `Content-Type:
application/json`.

**Why a hand-built LIST (`bv`) request 400s but a hand-built ACTION (`s`) works:**
the *state* Gmail needs is in those **headers**, not the body. A `bv` body
additionally carries per-view cursor state we can't forge → 400. An `s` action
body is just `[thread id, [add, remove, msg ids]]` — small and forgeable. So the
recipe for any write is **reuse Gmail's own live headers + forge the tiny body**,
never reconstruct the headers.

---

## 2. Mutations (archive / trash / star / mark-read) — SOLVED ✅

`gmonkey` has **no** mutation verb (`GmailThread`/`GmailMessage` are read-only —
`isStarred`/`isRead`/`getThreadId`, no setters). The native write is the
`/sync/i/s` **action code 13** (modify labels), the same POST the UI fires.

**Body** (what `__modifyLabels` builds):
```
[null,
 [[[13, [<thread id>, [null×6, [[<add labels>], [<remove labels>], [<msg ids>]]]]]]],
 [1, <seq>, null, null, [null,0], null, 1],
 [<ts>, 1, <ts>, 0, 35],
 2]
```
POST to `/sync/u/N/i/s?hl=en&c=<n>&rt=r&pt=ji` with Gmail's live headers
(`__g.hdrs`, captured by the XHR hook off any `/sync/` POST).

**System label codes:** `^i` inbox · `^t` starred · `^u` unread · `^k` trash ·
`^s` spam · `^f` sent · `^r` draft · `^all` all-mail. Verbs:

| verb | delta |
|---|---|
| archive | remove `^i` |
| trash (Delete) | add `^k` (Gmail drops it from every other folder) |
| star / unstar | add / remove `^t` |
| mark read / unread | remove / add `^u` |

**The id gotcha (this cost the most time):** the mutation needs **both** the
thread id **and its message ids**, and they must be the **same id family**, both
taken straight off the **bv stub the thread was listed in**:
- `bv` returns two disjoint stub sections: recently-synced threads as the perm
  form (`thread-a:` / `msg-a:`), older ones as the legacy form (`thread-f:` /
  `msg-f:`). No pairing between them in the bv.
- **Empty msg-ids → the action 200s but no-ops** (this is what made an early
  `thread-f` test look like "thread-f is rejected" — it wasn't; the empty msg
  list was). With the stub's msg-ids included, **both `thread-f`+`msg-f` and
  `thread-a`+`msg-a` apply.** No `thread-a` resolution needed.
- So `__mapStubs` matches `/^thread-[af]:/` (both forms → complete list), and
  `__modifyLabels` finds the target stub in `__g.bv` and lifts its msg-ids.

**Verification oracle:** the `s` response echoes the account's per-label count
summary `[["^i",unread,total],["^t",…],…]`. Watching a single label's total
move (e.g. `^t` 5→4 on unstar, `^i` −1 on archive, `^k` +1 on trash) is the
lag-immune proof the action reached storage. `list_emails` on the destination
folder (Trash after a trash) confirms end-to-end.

**Serialization:** mutations drive the one shared background tab (navigate +
eval). **They MUST run serially** — two in parallel collide (one's `#inbox`
refresh clobbers the other) and one fails `no_thread_msgs`. Observed live.

---

## 3. Send — SOLVED ✅ (root cause: compose moles; recipe wired into `send_email`)

`gmonkey` **is** the right tool for send (it encodes Gmail's elaborate positional
draft correctly — forging the two-step `s` action-3 [save draft] → action-4
[send, carries a base64 send-token from action 3] would reinvent that and rot).

**The delivery flakiness was NOT undo-send** (the prior theory). Proven live,
then re-proven end-to-end when wiring the fix (2026-07-09):

- From a **clean (0 open compose "mole") state**, gmonkey `createNewCompose →
  setTo/setSubject/setBody → send()` delivers in **well under 2s** (the send
  `/sync/i/s` 200 fired **52 ms** after `send()`; the marker then appears in the
  atom feed). Confirmed repeatedly.
- The draft must **autosave first** (a `/sync/i/s` draft-save POST carrying the
  body — its request body literally contains the subject string, verified live;
  ~5–10s after the last edit); `send()` before autosave silently no-ops (a
  just-created draft has no server id to send).
- **Every send leaves its compose mole open** — it never self-closes (verified:
  `getOpenDraftMessages().length == 1` right after a confirmed send). Moles
  therefore **accumulate**, and at a few stale moles the compose subsystem
  **wedges**: autosave stops firing, `send()` no-ops, nothing hits the wire.
  **Even 1 stale mole blocks the next autosave.**
- Synthetic DOM discard (`.click()`, full mouse/pointer sequences) on the mole's
  Discard button **does not close it** (untrusted events). And the mole is the
  **source of truth for the URL**, not vice-versa: it lives in the #fragment as
  `#inbox?compose=<id>`, Gmail re-syncs the fragment *from* the open mole (so
  `location.hash = '#inbox'` reverts), and a **same-URL `Page.reload` RE-OPENS
  the compose the fragment names**. The only reset that clears moles is a
  **cross-document reload to a compose-free URL** — navigate to
  `/mail/u/N/?aosc=<nonce>#inbox`; the `?aosc` query makes the pre-#fragment part
  differ, forcing a real document load that lands with no compose param → 0
  moles. Verified live. (gmonkey exposes no compose close/discard verb.)
- `document.visibilityState` is already `visible` and `document.hasFocus()` is
  `true` in the background tab — visibility/focus are NOT the problem.

**The recipe — now implemented in `send_email` / `_compose_send_js`:**
1. CLEAN GATE: `getOpenDraftMessages() > 0` → bail `moles_open`; the caller
   clears moles via the cross-document reload above (`_reset_compose`) + retries.
2. `createNewCompose` → poll `getOpenDraftMessages` for the draft → fill it.
3. AUTOSAVE GATE: wait for the draft-save `/sync/i/s` whose body carries the
   subject (from the `__g.actions` XHR capture) — NOT a fixed sleep. Its body
   also carries the To field, so the **recipient check is against wire truth**
   (`getToEmails()` lags `setTo` and is only the fallback).
4. `send()`.
5. SEND GATE: wait for the post-send `/sync/i/s` **200** — the on-the-wire
   delivery proof. A new action that never 200s ⇒ `sent_unconfirmed` (POST left;
   don't retry — a duplicate is worse). No action at all ⇒ `send_no_confirm`
   (no-op wedge — safe to reset + retry).

The whole thing runs under a **reactive retry** (max 3): a recoverable status
resets the tab (cross-doc reload) and re-composes from clean. Result: happy path
~5–7s, self-heal past a leftover mole ~15–34s (a full Gmail reload), all
delivered. The old "5s settle + 7s undo hold" sleeps are **deleted** — they
compensated for the wrong cause and no-op'd whenever stale moles were present.

**Readiness gate:** don't require `document.readyState === 'complete'` — a
headless Gmail tab often sits at `interactive` indefinitely (a long-poll never
settles) though the app is fully usable. `window.gmonkey` / `GM_APP_NAME` present
IS the readiness signal; gate on `readyState !== 'loading'` + that. (This was a
latent hang affecting **every** op, surfaced as intermittent `NotReady`.)

**Lag-immune verification oracles** (bv is served from the SW cache on repeat
views — do NOT trust it fresh): the **atom feed** (`/mail/u/N/feed/atom`,
server-rendered every fetch, lists inbox-unread subjects — perfect for
self-sends) and the **`s` response label counts** (§2).

---

## 4. gmonkey surface (window.gmonkey.load('2', cb) → api)

```
GmailMainWindow : createNewCompose · getOpenDraftMessages · getActiveMessage
GmailDraftMessage: setTo/setCc/setBcc(STRING) · setToEmails([{address,name}]) ·
                   setSubject · setBody(html) · getTo/getToEmails/… · send()   ← WRITE
GmailThread      : getThreadId · getLabels · isStarred · isRead · get*Html      ← READ only
GmailMessage     : getMessageId · getFromAddress · getPlainTextContent · isRead ← READ only
gmonkey (top)    : getCurrentThread · getMainWindow · register*StateChangeCallback (live watch)
```
`setTo` wants a **comma-separated STRING**; `setToEmails` wants address
**objects**. gmonkey is a deliberately **frozen legacy shim** (old embedded-gadget
API) — its public surface has NO reply/forward/thread-set. **`forward_email` is
done** (a forward is a standalone new Fwd: — plain gmonkey compose, no threading).
**Threaded `reply`/`reply_all` is SOLVED** (jLn-capture → EQn → gmonkey mole; open
via list-row DOM click — see below).

### The threaded-reply RE — walls, the deep hook, and the tool that cracks it

Established live 2026-07-09. Two things a threaded reply needs — land in the
original thread, and carry `In-Reply-To`/`References` — and neither is reachable
from gmonkey's public surface.

**Walls (do NOT re-walk):**
- **Forging the `/sync/i/s` SEND (action 4) is out.** The send carries a ~1300-char
  **opaque, client-minted send-token** (`[threadId,[null×25,[1,"<blob>",[msgIds]]]]`);
  two captured blobs share session-derived constants + per-draft variable parts →
  it's Gmail's anti-CSRF send authorization, minted by compiled code. Reproducing
  it reverses Gmail's send crypto and rots. (Draft-SAVE, action 3, has NO blob and
  IS forgeable — but a saved reply draft still has to be *sent*, which needs the
  blob.) So **the send must ride gmonkey** (`draft.send()` mints the token).
- **gmonkey can't thread** (verified): no thread setter, and it can't even see a
  reply draft as a mole (0 moles, inline or popped-out).
- **`fC(threadId)`/`YAc(spec)` on a FRESH `createNewCompose` don't reparent** — the
  draft's thread is already registered server-side; you get a new thread anyway.
- **UI-driving the native reply works** (trusted CDP `click` Reply → `type` → `Send`
  threads correctly — verified) but was rejected: fragile + not sync-level.

**The deep hook (Joe's bet — confirmed):** gmonkey's `GmailDraftMessage` is a thin
shim over the **internal compose object `draft.ha`** (proof: `gmonkey.send` is
`function(){…this.ha.send()}`). `.ha`'s Closure-private methods DO thread — found
by STABLE STRING, not minified name (`__re.grepMethods(draft.ha, "initNewReplyOrForward")`):
- **`U5a(a, opts)`** = `initNewReplyOrForward` — inits the compose as a reply/forward
  to reply-context `a` (`a.Mm()` returns `"r"`/`"a"` reply, `"f"`/`"t"` forward).
- **`fC(threadId)`** = set thread id · **`Aa`** = mode field (`"n"`/`"r"`/`"f"`).
Decoded from `YAc`'s source: `mKb(0,EB,draftId,threadId,…)` (server draft↔thread
attach) then `fC(threadId)`. `mKb` closes over the private `_.rG` service, so it
can't be called standalone — you must go through Gmail's own reply init.

**SOLVED — no click, no forge (2026-07-09).** The `draft.ha`/`U5a` path above was
the wrong altitude: you don't need to build the reply-context `a` yourself. Gmail's
reply-init `_m.EQn(CTRL, mode)` does it, given a live **message-view controller**
CTRL. The whole problem was getting a CTRL with no click — cracked by **wrapping the
shared base class**: every Gmail controller extends `_m.jLn`, and a subclass ctor
calls it as `_.jLn.call(this,…)` through the shared `_m` ref (CTRL `instanceof
_m.jLn`). So wrap `_m.jLn` in page JS (a `_m.<subclass>` wrap never fires — the
internal `new` uses a module-local binding), navigate to render the conversation,
and filter the captured instances to the message-view CTRL (`typeof c.Za==='function'
&& c.Ca && c.ha`). Then page-JS `_m.EQn(ctrl,'r')` opens Gmail's own thread-attached
reply compose — which, contrary to §4's earlier claim, **IS a gmonkey mole**:
`getOpenDraftMessages()` returns it with `Re:` set, recipient filled, and full
`setBody`/`send` (inner `.ha` has `U5a`+`fC`). `setBody` → `send()` (mints the token)
→ the reply lands in-thread. Verified live (REPLYVERIFY-J1, same conversationId).

**Wall 4 SOLVED (opening the conversation headless, 2026-07-09):** `#all/{hex}` /
`#inbox/{hex}` still show the LIST (hex ignored); only `#inbox/<permId>` is Gmail's
URL form. permId is never in the bv stub or the DOM row (rows carry only
`data-legacy-thread-id` + `data-thread-id=#thread-a:…`; `_m` has the RPC *name*
tokens `nHl`/`mHl` but no free callable — and list-row controllers do **not**
reconstruct through `_m.jLn`, so that path is a dead end). Open instead with a page-JS
DOM click Gmail accepts: surface the row (`#search/{hex}` or `#search/in:anywhere`
for a `thread-a:` id), then
`document.querySelector('[data-legacy-thread-id=hex]').click()` (or
`[data-thread-id=#thread-a:…]`). Lands on `#inbox/<permId>`, mints message-view
CTRLs for the jLn wrap → EQn → mole flow. Wired into `reply_email` /
`reply_all_email`. New CDP verbs this RE drove: `browser.source`, `browser.call`,
`browser.breakpoint` / `source` / `call` / `script_source` / `waitPause`. Ladder:
`commons/re/toolkit.md` + connector `gmail_re.md` § "Cracking sealed Gmail JS"
(`__re.captureBase`); detail: `gmail_cdp.py` §reply.


---

## 5. Dead ends (don't re-walk these)

- **`clients6.google.com/gmail/v1fpa_gmail_frontend_gwt`** is a *curated*
  frontend API — only the methods the GWT client calls (`threads/
  batchListCalendarEvents`, `threads/itemServerPermIdFromLegacyThreadStorageId`).
  It has **no** `labels`/`messages.modify`/`messages.send`. It's **cross-origin**
  from `mail.google.com` and **CORS-walls** an in-page `fetch` (even with the
  exact `gapi.auth.getAuthHeaderValueForFirstParty([])` SAPISIDHASH header set
  the page itself uses, and even via `gapi.client.request`). Not a path to the
  full gmail/v1 surface. The same-origin `/sync/` protocol (§1–2) is the way.
- **Reconstructing `X-Gmail-BTAI` / `X-Framework-Xsrf-Token`** — they're not in
  page globals; capture them off live `/sync/` traffic instead (the XHR hook).
- **DOM-driving compose discard / toolbar** — synthetic events are untrusted;
  Gmail ignores them. Reload to reset; `/sync/` action 13 for mutations.

---

## 6. list_labels source (not yet built)

Every `/sync/i/s` response carries the full per-label count summary
`[["^i",u,t],["^t",u,t],["^smartlabel_*",…],["Label_<id>",…]]`. That's the label
**inventory + counts** in one place — map the `^`-codes to display names
(Inbox/Starred/…) and surface user `Label_*` ids. A no-op `modify_email(add=[],
remove=[])` (or any list navigation's action) yields it without a dedicated
endpoint (`/i/s` is what fires it; `/i/s?…` incremental-sync alone is unreliable).

---

## 7. The toolkit additions this connector drove (`commons/re/toolkit.js`)

- `__re.net` now captures **request headers** (XHR `setRequestHeader` + fetch
  init) and **`sendBeacon`**, and gained `net.detail(needle)` — the "watch an
  action on the wire" view (drive a send/star, read exactly what left, headers
  and all). This is how the `/sync/` action shapes + session headers here were
  lifted.
- `__re.mut(selector)` — a DOM-mutation timeline (add/remove/attr), the visual
  sibling of `net`. `mut('[role=dialog]')` is how the compose-mole lifecycle
  (opens, never closes) was pinned by observation instead of a fixed sleep.

---

## 8. Attachments — OUTBOUND SOLVED ✅ · INBOUND SOLVED ✅ (message hex)

**Outbound (send/forward with `attachments:[{filename,mimeType,path|content}]`):**
Page-JS `File` + `DataTransfer` into Gmail's hidden
`input[type=file][name=Filedata]` (last one = newest compose). Gmail shows
"Attachment X added" / "Uploading…", wires the file into the draft autosave,
and `send()` ships it. **No engine `setFileInputFiles` verb required** (CDP path
would also work but page JS is enough). Durable helper: `__agmail.composeSend`.
Verified live (AOS-ATT-*).

**Inbound (get_email / get_attachment) — the self-sent om crack (2026-07-10):**

`view=om&th=<hex>` serves the raw RFC822 in `<pre id="raw_message_text">`. The
gotcha: **`th` wants the MESSAGE legacy hex, not the thread hex.**

| Id | Where | Self-sent AOS-ATT | Received (single-msg) |
|---|---|---|---|
| thread hex | bv stub `t[19]`, DOM `data-legacy-thread-id` | `19f4c3f47ee6d85d` | `19f48238253ee251` |
| message hex | bv msg stub `msg[55]`, DOM `data-legacy-message-id`, att URL `th=` | `19f4c3f4dacb9969` ✅ om | same as thread ✅ |
| `thread-a:` / `msg-a:` | bv `t[3]` / `msg[0]` | om 200s "does not exist" | n/a (thread-f) |

Thread hex on a fresh self-sent → om HTML title "Original Message" body *"The
message you requested does not exist"* (no `<pre>`). Message hex → full RFC822
+ attachments. Received mail often has thread hex == message hex, which is why
om "just worked" there and looked like a self-sent lag bug.

**Recipe (wired into `get_email` / `get_attachment`):**
1. `__mapStubs` lifts `legacyThreadId` (`t[19]`), `messageHex`/`_omToken`
   (`msg[55]`), and `hasAttachments` (non-empty `msg[11]` att arrays).
2. `__agmail.resolveOmToken(token)` finds the message hex from a captured bv
   (match thread-a / thread hex / message hex), else opens the conversation and
   reads `data-legacy-message-id`.
3. `view=om&th=<messageHex>` → parse RFC822 → metadata + `blobs.put` on
   `get_attachment`. Direct `view=att&th=<messageHex>&attid=0.1` also returns
   bytes (same content) — om path kept for OAuth-parity part indexing.

**Do not** claim round-trip with thread-a alone or thread hex on self-sents —
list now surfaces `messageHex` for that.

---

## 9. Filters — LIST/CREATE/DELETE SOLVED ✅ (UI drive; sync RE in progress)

OAuth `gmail.list_filters` / `create_filter` / `delete_filter` hit
`gmail.googleapis.com/.../settings/filters`. CDP has no *hand-forgeable* sync
action for filters yet — Settings UI remains the production path. Under the
hood the UI posts:

| Endpoint | Role |
|---|---|
| `POST /sync/u/N/st/s` | Durable settings write — body nests an opaque `522465311` token + new filter id. **Required for create to persist.** |
| `POST /sync/u/N/i/s` | Filter action announce: `[[[1,…,[id,[null,[]]]]]` ≈ delete, `[[[2,…,[id,[[row]]]]]` ≈ upsert. Forging i/s alone returns 200 but does **not** remove/create the filter. |

Captured live (not hand-forged). `__agmail.settingsActions` captures `st/s`;
`__agmail.actions` captures `i/s`. RE notes: `__gre.filterSyncCaps()`.

**Sealed-path RE (partial):** durable write is `_m.V1k.prototype.E0b(a)` —
builds `new _.snd` (proto type for field `522465311`), fills fields from
protobuf `a`, then `this.ha.oa.call(this.ha, _.jtd, _.nq(snd))`. Bundle:
`cb=loaded_3` … `_.x.E0b=function…` (same fn; exported as `_m.V1k.prototype.E0b`).

**How to crack it (agents — do not forge tokens):** use CDP Debugger verbs.
Full recipe: `gmail_re.md` § "Cracking sealed Gmail JS". Short path:
`gbreak wait expr '_m.V1k.prototype.E0b' --trigger '…createFilter(AOS-…)…'` →
inspect paused `this` / local scope objectIds → `browser.call` E0b. Also:
`setBreakpointByUrl` via `urlRegex`+`line` (1-based stack), `script_source`,
`__re.spy` / `captureBase(_m,'V1k').take()`. `browser.scopes` on `_`-closing
fns soft-fails (huge Closure `_`); prefer waitPause locals. Protocol strings:
`GHl="getFiltersList"`, `sEm="Error creating filter"`, `WHa="create-filter/"`.
Serializer below E0b still sealed: `JSON.stringify → _.C.ld → _.x.O7a → uvm`.

**List (wired):** navigate `#settings/filters`, scrape table rows:
`Matches: <criteria> Do this: <action>` + edit/delete link ids embedding
`z…*…` filter tokens. Durable: `__agmail.listFilters()` →
`[{id, criteria, action, text}]`.

**Create (wired):** `__agmail.createFilter(opts)` opens "Create a new filter":
1. Criteria — From (`aQa`)/To (`aQf`)/Subject (`aQd`)/Has the words (`aQb`)/
   Doesn't have / Has attachment. Waits for an *enabled* Create filter button.
2. Actions — Skip Inbox, Mark as read, Star, Apply label, Forward, Delete,
   Never spam, Always/Never important. Submit → "Your filter was created."
   Checkboxes are matched only among *visible* filter-dialog rows (not Settings
   IMAP checkboxes — that false-positive caused `unconfirmed` no-ops).
3. Sensitive-action reauth: if "We need to verify it's you" covers the dialog,
   returns `__error: 'reauth_required'` → `NeedsAuth`. Fix with
   `browser.login_window` on the bg profile → Continue → Google popup → retry.
OAuth-parity tool: `create_filter(from_addr, to, subject, query,
has_attachment, add_labels, remove_labels, forward_to)`. `remove_labels:
["INBOX"]` → Skip Inbox; `["UNREAD"]` → Mark as read. `add_labels` of
`STARRED`/`TRASH`/`IMPORTANT` map to checkboxes; other names use the Apply
label listbox. Validate with throwaway `AOS-FILTER-*` subjects — do **not**
delete Joe's real `modernist.club → joe@contini.co` filter without asking.

**Delete (wired):** `__agmail.deleteFilter(id)` clicks the matching delete
`span[role=link]` (+ visible OK confirm). Prefer listing first.

---

## 10. Labels — CREATE/DELETE SOLVED ✅

OAuth `gmail.create_label` / `delete_label` hit `gmail.googleapis.com/.../labels`.
CDP drives `#settings/labels` (same surface as the human "Create new label" /
per-row remove). `list_labels` already scrapes the left nav (user id = display
name — no OAuth `Label_<n>` token from the UI).

**Create (wired):** `__agmail.createLabel({name})` → Create new label dialog →
native value-setter on the name input → Create → toast/row confirm. Returns
`{id, name, tagType:'user'}` with `id === name` (matches `list_labels`).

**Delete (wired):** `__agmail.deleteLabel(id)` finds the settings row with a
`remove` link (`act=lpe`, `flid`), confirms with **Delete** (not OK). Validate
with throwaway `AOS-LABEL-*` only.

---

## 11. unsubscribe_email — SOLVED ✅ (RFC 8058)

OAuth posts `List-Unsubscribe=One-Click` to the URL from the message headers.
CDP: `get_email` (view=om) → `_map_rfc822` lifts `List-Unsubscribe` /
`List-Unsubscribe-Post` → `unsubscribe` + `unsubscribeOneClick`. Tool
`unsubscribe_email(id, confirm=False)` defaults to **dry_run** (parse+report);
`confirm=True` fires the POST via the engine `http` client (plugin declares
`http` service). No one-click → `manual_required` + URL (incl. mailto).

Verified on efisio: Eventbrite campaign invite (`199e3a19f179f4c1`) dry_run →
confirm → HTTP 204. Tesla newsletter = mailto-only → `manual_required`.
Do not fire on lists Joe cares about without asking.

---

## 12. list_send_as / get_vacation / set_vacation — SOLVED ✅

Cheap Settings scrapes (OAuth parity):

| Tool | Surface | Notes |
|---|---|---|
| `list_send_as` | `#settings/accounts` "Send mail as" | Rows with `edit info`; `isPrimary` = matches account email |
| `get_vacation` | `#settings/general` Vacation responder | Radios `bx_ve` off/on; subject input; contenteditable message |
| `set_vacation` | same + Save Changes | `execCommand('insertText')` (TrustedHTML blocks `innerHTML`); flip OFF **after** editing fields so Save persists off |

Verified on efisio: list_send_as → Joe Contini \<efisio@gmail.com\> reply-to joe@contini.co; get_vacation off + saved body; set AOS-VACATION-TEST on → restore off + original body.
