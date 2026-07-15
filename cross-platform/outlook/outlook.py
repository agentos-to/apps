"""Outlook — live Outlook.com mail via the engine-held browser session.

Every op is one JS payload evaluated in the outlook.live.com tab of the
engine's HEADLESS background profile, through the `browser_session` service
(verb `eval` — the engine holds CDP; this app never sees the protocol). It is
the WhatsApp/Exa model: the browser profile *is* the session, requests
originate from the real browser, and we never extract a cookie or a token. No
window opens for a read — the mail surfaces in the Mail app (rule 19).

Unlike Exa (same-origin `fetch()`), Outlook Web's transport is NOT usable by
a hand-rolled fetch: its `service.svc` endpoint authenticates on a *rotating*
`MSAuth1.0 usertoken` header (not a cookie), minted and refreshed by OWA's own
request layer — a raw fetch with any sessionStorage token 401s. So, like
WhatsApp calling `WAWebCollections`, we call **OWA's own EWS operation
functions** (`findItem` / `getItem`), which route through OWA's authed
pipeline. Auth becomes a non-problem: OWA attaches the fresh token itself.

Mapping to the `email` shape happens *in JS* (see `mapEmail`) — the item is
already in the page — so Python stays a thin dispatch/error layer, and the
mapping mirrors `gmail.py::_map_email` key-for-key so the Mail app renders
Outlook and Gmail identically.

╔══════════════════════════════════════════════════════════════════════════╗
║  REVERSE-ENGINEERING PLAYBOOK — how this connector reaches into OWA        ║
║  (reusable for ANY webpack/minified SPA; keep this current — the ids rot,  ║
║   the *method* doesn't. Fuller writeup: system doc                         ║
║   `reverse-engineering-runtime-internals`.)                               ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  1. GRAB THE MODULE `require` VIA A CHUNK PUSH (no [[Scopes]] walk).       ║
║     A webpack app exposes `window.webpackChunkOwa` — an array whose        ║
║     `.push` is the chunk registrar. Push a chunk whose *runtime* callback  ║
║     receives `__webpack_require__`, and keep it:                          ║
║                                                                            ║
║         let req;                                                           ║
║         webpackChunkOwa.push([['probe'], {}, (r) => { req = r; }]);        ║
║                                                                            ║
║     Now `req.m` is the module-factory map (id → factory fn). This is the   ║
║     simplest registry grab there is — it beats the CDP `[[Scopes]]`        ║
║     technique whenever the runtime is a webpack push-array (it usually     ║
║     is). Metro/RN-web is the same idea via `__d`/`__r`.                    ║
║                                                                            ║
║  2. FIND THE OP YOU WANT BY A PROTOCOL-STABLE STRING, NOT A MODULE ID.     ║
║     Module ids change every OWA deploy; the EWS action names ("FindItem",  ║
║     "GetItem") and type names ("FindItemJsonRequest:#Exchange") do NOT —   ║
║     they're the wire protocol. Grep each factory's *source*                ║
║     (`Function.prototype.toString`) for the action passed as a call arg,   ║
║     e.g. the regex  /\\(["']FindItem["']\\s*,/  matches the operation      ║
║     wrapper `(0,a.X)("FindItem", e, r)` and skips the enum `="FindItem"`.  ║
║     `require(id)` the match, take the export whose own source also matches ║
║     — that's the operation function. `resolveOp()` below does exactly this ║
║     and caches on `window`, so the connector SELF-HEALS across redeploys.  ║
║                                                                            ║
║  3. THE OPERATION WRAPPERS ALL FUNNEL THROUGH ONE AUTHED CALLER.           ║
║     `findItem(req)` and `getItem(req)` each just add a `__type` and call a ║
║     generic `X(action, jsonRequest, opts)` — OWA's own service caller that ║
║     assembles headers, the rotating `MSAuth1.0` token, and the            ║
║     `x-owa-urlpostdata` payload, then POSTs to `/service.svc`. Calling the ║
║     wrapper means we never touch auth. Pass `opts = { mailboxInfo }` to    ║
║     route the request at the right account (read `mailboxInfo` off any     ║
║     folder in the Satchel store).                                          ║
║                                                                            ║
║  4. READ IDENTITY & FOLDER IDS FROM THE SATCHEL STORE (read-only, stable). ║
║     OWA is Satchel/MobX: `window.__satchelGlobalContext.rootStore` is a    ║
║     registry of named stores. We DON'T read mail rows from it (they're     ║
║     conversation-level, body-less, and only hold loaded rows) — we use it  ║
║     for the two stable facts it owns: identity                            ║
║     (`owaSessionStore.userConfiguration.SessionSettings`) and the concrete ║
║     per-folder `FolderId` + `mailboxInfo` (`folderStore.folderTable`,      ║
║     keyed by `distinguishedFolderType`: inbox / sentitems / drafts /       ║
║     deleteditems / junkemail / archive).                                   ║
║                                                                            ║
║  5. REQUEST SHAPE — LET `BaseShape` DO THE WORK, DON'T HAND-ROLL props.    ║
║     B2Service's serializer rejects EWS `AdditionalProperties` /            ║
║     `SortOrder` `PropertyUri` enums it doesn't know (→ 500                 ║
║     `OwaSerializationException`). Two rules learned the hard way:          ║
║       • Ask for fields via `BaseShape:'AllProperties'` (+ `BodyType:HTML`  ║
║         on GetItem for the body) — never `AdditionalProperties`.           ║
║       • The only sort `FieldURI` that serializes is `ItemLastModifiedTime` ║
║         (received/sent enums 500). So we server-sort by that to bound the  ║
║         page, then client-sort by `DateTimeReceived` for exact recency.    ║
║     `GetItem` takes an ARRAY of `ItemIds` — one batched call hydrates a    ║
║     whole page of bodies.                                                  ║
║                                                                            ║
║  HOW TO RE-DERIVE WHEN OWA SHIPS A BREAKING BUILD (do this, don't guess):  ║
║     Drive the live tab over CDP and re-run the probes. Node ≥ 21 has a     ║
║     global `WebSocket`, so a zero-dependency CDP driver is ~15 lines:      ║
║     connect to a page's `webSocketDebuggerUrl` from                        ║
║     `http://localhost:9222/json`, then `Network.enable` + `Page.reload`    ║
║     and read `Network.requestWillBeSent` (url, headers, `x-owa-urlpostdata`║
║     decoded) + `Network.getResponseBody` for the EXACT working request     ║
║     shape OWA itself sends — replay that, don't reconstruct. (This is how  ║
║     the rules above were found; the driver template lives in the RE doc.)  ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import json

from agentos import account, app_error, blobs, browser_session, connection, provides, returns, test, timeout

# One declaration says where this connector lives: `_HOME`, the signed-in
# landing page. The tab-match hostname `_TARGET` derives from it. We land on the
# inbox directly because bare `outlook.live.com/` is a marketing splash that
# bounces through several redirects first; the inbox SSOs straight in for an
# authenticated profile. Both the engine and the standalone export read these,
# so both reach the same page the fast way.
_HOME = "https://outlook.live.com/mail/"
_TARGET = _HOME.split("/")[2]  # "outlook.live.com" — the tab-match hostname, from _HOME

# `_SSO_PRIME` — the Microsoft-account identity provider. On a cold start (a
# freshly launched profile that still holds the persistent MSA cookie but no live
# OWA session), OWA's MSAL runs its silent SSO in a hidden iframe, which cannot
# complete the MSA→AAD federation there and falls back to an interactive prompt.
# A single *top-level* visit to the IdP re-establishes the account session from
# that persistent cookie with zero clicks; OWA then SSOs on the next navigation.
# The standalone host visits this before ever asking for a password.
_SSO_PRIME = "https://login.live.com/"

# Service-level mailbox vocabulary → OWA's `distinguishedFolderType`. Shared
# with the other mailbox providers (gmail's `_MAILBOX_QUERIES`); the connector
# resolves each to a concrete FolderId out of the live folder store.
_MAILBOX_TO_DFT = {
    "inbox": "inbox",
    "sent": "sentitems",
    "drafts": "drafts",
    "trash": "deleteditems",
    "spam": "junkemail",
    "archive": "archive",
}


# ──────────────────────────────────────────────────────────────────────
# JS building blocks
# ──────────────────────────────────────────────────────────────────────

# Readiness + logged-out detection. A freshly opened tab is at about:blank;
# a logged-out session redirects to login.live.com. We branch on where the tab
# lands (the redirect IS the auth signal, exa-style), then wait for OWA's
# runtime AND for the EWS operation functions we actually need to resolve.
#
# Why "runtime present" is NOT enough: `webpackChunkOwa` appears early, but the
# mail chunk that DEFINES findItem/getItem loads a beat later — so on a freshly
# opened (or freshly attached, or cold-duplicate) tab, resolving ops the instant
# the array exists returns null and the op body bails with `ops_unresolved`,
# surfaced as a spurious, permanent-sounding `BindingDrift`. Instead we poll
# `__buildOwa()` (idempotent, caches on window) every tick until the ops resolve
# — so a cold tab self-heals inside THIS one call, no agent retry, and attaching
# to a duplicate tab that happens to be cold just means we wait for it to warm.
# The deadline then distinguishes a runtime that never came up (`tab_not_ready`)
# from one that came up but never exposed the ops (`ops_unresolved` → the real,
# permanent BindingDrift that genuinely means "re-derive the bindings").
_PRELUDE = """
const __t0 = Date.now();
const __deadline = __t0 + %(wait_ms)d;
let __owa = null, __sawRuntime = false, __ticks = 0;
while (true) {
  __ticks++;
  const h = location.hostname;
  if (h.indexOf('login.live.com') !== -1 || h.indexOf('login.microsoftonline') !== -1
      || h.indexOf('account.microsoft') !== -1 || h.indexOf('login.srf') !== -1) {
    return { __error: 'auth_required' };
  }
  const __runtimeUp =
      (h.indexOf('outlook.live.com') !== -1 || h.indexOf('outlook.office') !== -1)
      && document.readyState === 'complete'
      && window.__satchelGlobalContext && window.webpackChunkOwa;
  if (__runtimeUp) {
    __sawRuntime = true;
    __owa = __buildOwa();
    if (__owa.findItem && __owa.getItem) break;
  }
  if (Date.now() > __deadline) {
    return { __error: __sawRuntime ? 'ops_unresolved' : 'tab_not_ready' };
  }
  await new Promise(r => setTimeout(r, 200));
}
// Diagnostics: how long the gate polled before the ops resolved. warm ⇒ ~0ms /
// 1 tick; a cold tab that self-healed shows real wait_ms and >1 tick — the proof
// that "ready" now means "ops resolvable", not merely "framework present".
window.__agentosGateStats = { wait_ms: Date.now() - __t0, ticks: __ticks, sawRuntime: __sawRuntime };
"""

# The reverse-engineering bootstrap — see the file header. Grabs
# __webpack_require__ via a chunk push, resolves OWA's own findItem/getItem
# operation functions by a protocol-stable string marker, and caches them
# (self-healing across OWA redeploys). Also exposes Satchel-store readers for
# identity, folders, and mailboxInfo. Everything hangs off the returned `o`.
#
# Defined as an idempotent function (not an IIFE) so the readiness gate can call
# it every poll tick while a cold tab's mail chunk finishes loading: the chunk
# push and each op-resolution run at most once (guarded on the window-cached
# `o`), so re-calling is cheap and self-healing.
_RUNTIME = r"""
function __buildOwa() {
  const o = (window.__agentosOwa = window.__agentosOwa || {});
  if (!o.req) {
    try {
      window.webpackChunkOwa.push([
        ['__agentos_' + Math.random().toString(36).slice(2)], {},
        (r) => { o.req = r; },
      ]);
    } catch (e) { o.pushErr = String(e); }
  }
  const req = o.req;
  // Resolve an EWS operation wrapper by the action name it passes to OWA's
  // generic service caller: `(...)("FindItem", req, opts)`. The `(` before
  // the quote skips enum members (`="FindItem"`); we confirm the chosen
  // export's OWN source matches too, pinning the operation fn (arity 2).
  const resolveOp = (action) => {
    if (!req || !req.m) return null;
    const re = new RegExp('\\(\\s*["\']' + action + '["\']\\s*,');
    for (const id in req.m) {
      let src;
      try { src = Function.prototype.toString.call(req.m[id]); } catch (e) { continue; }
      if (!re.test(src)) continue;
      let ex;
      try { ex = req(id); } catch (e) { continue; }
      for (const k in ex) {
        try {
          const f = ex[k];
          if (typeof f === 'function' && re.test(Function.prototype.toString.call(f))) return f;
        } catch (e) {}
      }
    }
    return null;
  };
  // Generic, cached resolver — call any EWS action's wrapper by name. Cache
  // ONLY a successful resolution: a null (the module/chunk not loaded yet on a
  // cold OWA — e.g. the first op right after a fresh sign-in) must not poison
  // the cache, or every later call returns that stale null. Re-resolve until it
  // sticks; once OWA has warmed the op function is found and cached for good.
  o.ops = o.ops || {};
  o.op = (action) => {
    if (o.ops[action]) return o.ops[action];
    const fn = resolveOp(action);
    if (fn) o.ops[action] = fn;
    return fn;
  };
  if (!o.findItem) o.findItem = o.op('FindItem');
  if (!o.getItem) o.getItem = o.op('GetItem');
  // Satchel store readers (read-only, stable).
  const rs = window.__satchelGlobalContext.rootStore;
  o.store = (n) => (rs.get ? rs.get(n) : rs[n]);
  o.folders = () => {
    try { return [...o.store('folderStore').folderTable.values()]; } catch (e) { return []; }
  };
  o.folderByType = (dft) =>
    o.folders().find((f) => (f.distinguishedFolderType || '') === dft) || null;
  o.identity = () => {
    try { return o.store('owaSessionStore').userConfiguration.SessionSettings; }
    catch (e) { return null; }
  };
  return o;
}

// Minimal EWS request header — RequestServerVersion is the only field the
// server requires; everything else rides in the wrapper/caller.
const __EWS_HEADER = { __type: 'JsonRequestHeaders:#Exchange', RequestServerVersion: 'V2018_01_18' };
"""

# Shape mapper: an EWS item (GetItem AllProperties + HTML body) → the `email`
# shape. Keys mirror gmail.py::_map_email so both providers render identically.
# `dft` (the folder we listed) drives the boolean folder flags; `smtp` is the
# mailbox this arrived on (stamped as accountEmail, gmail-style).
_HELPERS = r"""
const __domainOf = (email) => {
  const at = (email || '').lastIndexOf('@');
  return at > 0 ? email.slice(at + 1).toLowerCase() : null;
};
const __acct = (mb) => {
  if (!mb) return null;
  const handle = mb.EmailAddress || '';
  if (!handle) return null;
  return { handle, platform: 'email', displayName: mb.Name || null };
};
// EWS recipient collections are `{ Mailbox: [ {Name,EmailAddress}, ... ] }`;
// singles are `{ Mailbox: {...} }`. Normalize both (and a bare array) to a list.
const __recips = (coll) => {
  if (!coll) return [];
  let arr = (coll.Mailbox !== undefined) ? coll.Mailbox : coll;
  if (!Array.isArray(arr)) arr = [arr];
  return arr.map((m) => __acct(m.Mailbox || m)).filter(Boolean);
};
const __ts = (s) => { const t = Date.parse(s || ''); return Number.isFinite(t) ? t : 0; };
function mapEmail(it, dft, smtp) {
  const fromMb = it.From && it.From.Mailbox;
  const from = __acct(fromMb);
  const body = (it.Body && typeof it.Body.Value === 'string') ? it.Body.Value : '';
  const flagged = !!(it.Flag && it.Flag.FlagStatus === 'Flagged');
  const dom = from ? __domainOf(from.handle) : null;
  const out = {
    id: it.ItemId && it.ItemId.Id,
    name: it.Subject || '(no subject)',
    content: body,
    content_mime: body ? 'text/html' : 'text/plain',
    author: fromMb ? (fromMb.Name || null) : null,
    published: it.DateTimeReceived || it.DateTimeSent || null,
    isUnread: it.IsRead === false,
    // Folder membership (inbox/sent/drafts/deleteditems/junkemail) is the read
    // scope `dft`, recorded as `mailbox` mirror-list membership — not a flag.
    isStarred: flagged,
    hasAttachments: it.HasAttachments === true,
    importance: it.Importance || null,
    messageId: it.InternetMessageId || '',
    conversationId: (it.ConversationId && it.ConversationId.Id) || '',
    changeKey: it.ItemId && it.ItemId.ChangeKey,
    // Relations
    from: from,
    to: __recips(it.ToRecipients),
    copied_to: __recips(it.CcRecipients),
    bcc: __recips(it.BccRecipients),
    domain: dom ? { name: dom } : null,
  };
  if (smtp) out.accountEmail = smtp;
  out.__ts = __ts(it.DateTimeReceived || it.DateTimeSent);
  return out;
}
// Shared list body: reads __dft / __limit / __offset injected by Python.
// findItem (IdOnly, ordered) → one batched getItem (AllProperties + HTML).
async function __listEmails() {
  const owa = __owa;
  if (!owa.findItem || !owa.getItem) return { __error: 'ops_unresolved' };
  const folder = owa.folderByType(__dft);
  if (!folder) return { __error: 'folder_not_found', dft: __dft,
    available: owa.folders().map((f) => f.distinguishedFolderType) };
  const mailboxInfo = folder.mailboxInfo;
  const smtp = (mailboxInfo && mailboxInfo.mailboxSmtpAddress) || null;
  const findReq = { Header: __EWS_HEADER, Body: {
    __type: 'FindItemRequest:#Exchange',
    ParentFolderIds: [{ __type: 'FolderId:#Exchange', Id: folder.id }],
    ItemShape: { __type: 'ItemResponseShape:#Exchange', BaseShape: 'IdOnly' },
    Traversal: 'Shallow', ViewFilter: 'All',
    Paging: { __type: 'IndexedPageView:#Exchange', BasePoint: 'Beginning',
              Offset: __offset, MaxEntriesReturned: __limit },
    // ItemLastModifiedTime is the ONLY sort FieldURI B2Service accepts.
    SortOrder: [{ __type: 'SortResults:#Exchange', Order: 'Descending',
      Path: { __type: 'PropertyUri:#Exchange', FieldURI: 'ItemLastModifiedTime' } }],
  }};
  let fResp;
  try { fResp = await owa.findItem(findReq, { mailboxInfo }); }
  catch (e) { return { __error: 'find_failed', what: String(e).slice(-120) }; }
  const rm = fResp && fResp.Body && fResp.Body.ResponseMessages
    && fResp.Body.ResponseMessages.Items && fResp.Body.ResponseMessages.Items[0];
  const rootItems = (rm && rm.RootFolder && rm.RootFolder.Items) || [];
  const ids = rootItems
    .map((it) => it.ItemId && { __type: 'ItemId:#Exchange', Id: it.ItemId.Id, ChangeKey: it.ItemId.ChangeKey })
    .filter((x) => x && x.Id);
  if (!ids.length) return [];
  return await __hydrate(ids, __dft, smtp, mailboxInfo);
}
// Batched GetItem → full items with HTML bodies → mapped + client-sorted.
async function __hydrate(ids, dft, smtp, mailboxInfo) {
  let gResp;
  try {
    gResp = await __owa.getItem({ Header: __EWS_HEADER, Body: {
      __type: 'GetItemRequest:#Exchange',
      ItemShape: { __type: 'ItemResponseShape:#Exchange', BaseShape: 'AllProperties', BodyType: 'HTML' },
      ItemIds: ids,
    }}, { mailboxInfo });
  } catch (e) { return { __error: 'get_failed', what: String(e).slice(-120) }; }
  const items = ((gResp && gResp.Body && gResp.Body.ResponseMessages
    && gResp.Body.ResponseMessages.Items) || [])
    .map((x) => x && x.Items && x.Items[0]).filter(Boolean);
  const mapped = items.map((it) => mapEmail(it, dft, smtp));
  mapped.sort((a, b) => (b.__ts || 0) - (a.__ts || 0));
  mapped.forEach((m) => { delete m.__ts; });
  return mapped;
}
// ── Write path: CreateItem through OWA's own authed pipeline ──────────
// Recipients in OWS JSON are EmailAddressWrapper (what OWA's own compose
// POSTs in x-owa-urlpostdata), never the SOAP Mailbox nesting.
const __wrap = (addr) => ({ __type: 'EmailAddressWrapper:#Exchange',
  EmailAddress: addr, RoutingType: 'SMTP' });
const __recipList = (csv) => String(csv || '').split(/[,;]/)
  .map((s) => s.trim()).filter(Boolean).map(__wrap);
const __bodyContent = (text, html) => ({ __type: 'BodyContentType:#Exchange',
  BodyType: html ? 'HTML' : 'Text', Value: html || text || '' });
// One CreateItem call — the mechanism behind Send (a fresh Message) and
// Reply (a ReplyToItem referencing the original). MessageDisposition
// 'SendAndSaveCopy' sends via the account's own pipeline and files the
// copy in Sent Items; OWA attaches the rotating MSAuth token itself.
async function __createItem(item, disposition) {
  const owa = __owa;
  const createItem = owa.op('CreateItem');
  if (!createItem) return { __error: 'ops_unresolved' };
  const inbox = owa.folderByType('inbox') || owa.folders()[0];
  const mailboxInfo = inbox && inbox.mailboxInfo;
  const smtp = (mailboxInfo && mailboxInfo.mailboxSmtpAddress) || null;
  let resp;
  try {
    resp = await createItem({ Header: __EWS_HEADER, Body: {
      __type: 'CreateItemRequest:#Exchange',
      Items: [item],
      MessageDisposition: disposition,
    }}, { mailboxInfo });
  } catch (e) { return { __error: 'create_failed', what: String(e).slice(-160) }; }
  const rm = resp && resp.Body && resp.Body.ResponseMessages
    && resp.Body.ResponseMessages.Items && resp.Body.ResponseMessages.Items[0];
  if (!rm || rm.ResponseClass !== 'Success') {
    return { __error: 'create_failed',
      what: (rm && (rm.ResponseCode || rm.MessageText)) || 'unknown' };
  }
  const created = rm.Items && rm.Items[0];
  return { shape: 'email',
    id: (created && created.ItemId && created.ItemId.Id) || null,
    accountEmail: smtp, isStarred: false };
}
// Resolve an RFC 2822 Message-ID to the mailbox's EWS ItemId — the reply
// contract is keyed on In-Reply-To (provider-agnostic), so the provider
// adapts it to its own id space. FindItem restriction on
// message:InternetMessageId, searched across the folders a reply target
// can live in (newest folders first; first hit wins).
async function __itemIdByMessageId(mid) {
  const owa = __owa;
  if (!owa.findItem) return null;
  for (const dft of ['inbox', 'archive', 'sentitems', 'junkemail', 'deleteditems']) {
    const folder = owa.folderByType(dft);
    if (!folder) continue;
    let resp;
    try {
      resp = await owa.findItem({ Header: __EWS_HEADER, Body: {
        __type: 'FindItemRequest:#Exchange',
        ParentFolderIds: [{ __type: 'FolderId:#Exchange', Id: folder.id }],
        ItemShape: { __type: 'ItemResponseShape:#Exchange', BaseShape: 'IdOnly' },
        Traversal: 'Shallow', ViewFilter: 'All',
        Paging: { __type: 'IndexedPageView:#Exchange', BasePoint: 'Beginning',
                  Offset: 0, MaxEntriesReturned: 1 },
        Restriction: { __type: 'RestrictionType:#Exchange', Item: {
          __type: 'IsEqualTo:#Exchange',
          Item: { __type: 'PropertyUri:#Exchange', FieldURI: 'message:InternetMessageId' },
          FieldURIOrConstant: { __type: 'FieldURIOrConstantType:#Exchange',
            Item: { __type: 'Constant:#Exchange', Value: mid } },
        }},
      }}, { mailboxInfo: folder.mailboxInfo });
    } catch (e) { continue; }
    const rm = resp && resp.Body && resp.Body.ResponseMessages
      && resp.Body.ResponseMessages.Items && resp.Body.ResponseMessages.Items[0];
    const items = (rm && rm.RootFolder && rm.RootFolder.Items) || [];
    if (items.length && items[0].ItemId) {
      return { __type: 'ItemId:#Exchange', Id: items[0].ItemId.Id,
        ChangeKey: items[0].ItemId.ChangeKey };
    }
  }
  return null;
}
// MoveItem an item into a distinguished folder — the mechanism behind both
// Delete (→ deleteditems, recoverable, like gmail trash) and Archive
// (→ archive). Returns a minimal `email` reflecting the new folder; the app
// optimistically drops the row, so success + the new id is all it needs.
async function __move(id, targetDft) {
  const owa = __owa;
  const moveItem = owa.op('MoveItem');
  if (!moveItem) return { __error: 'ops_unresolved' };
  const target = owa.folderByType(targetDft);
  if (!target) return { __error: 'folder_not_found', dft: targetDft,
    available: owa.folders().map((f) => f.distinguishedFolderType) };
  const inbox = owa.folderByType('inbox') || owa.folders()[0];
  const mailboxInfo = inbox && inbox.mailboxInfo;
  const smtp = (mailboxInfo && mailboxInfo.mailboxSmtpAddress) || null;
  let mr;
  try {
    mr = await moveItem({ Header: __EWS_HEADER, Body: {
      __type: 'MoveItemRequest:#Exchange',
      ItemIds: [{ __type: 'ItemId:#Exchange', Id: id }],
      ToFolderId: { __type: 'TargetFolderId:#Exchange',
        BaseFolderId: { __type: 'FolderId:#Exchange', Id: target.id } },
    }}, { mailboxInfo });
  } catch (e) { return { __error: 'move_failed', what: String(e).slice(-120) }; }
  const rm = mr && mr.Body && mr.Body.ResponseMessages
    && mr.Body.ResponseMessages.Items && mr.Body.ResponseMessages.Items[0];
  if (!rm || rm.ResponseClass !== 'Success') {
    return { __error: 'move_failed', what: (rm && (rm.ResponseCode || rm.MessageText)) || 'unknown' };
  }
  const newId = (rm.Items && rm.Items[0] && rm.Items[0].ItemId && rm.Items[0].ItemId.Id) || id;
  // The move changed folder membership; the target mailbox's next live read
  // reconciles it in the `mailbox` mirror-list — no flag to stamp here.
  return { shape: 'email', id: newId, accountEmail: smtp, isStarred: false };
}
// Permanently delete items — EWS DeleteItem with DeleteType 'HardDelete',
// which skips Deleted Items entirely (unlike __move → deleteditems, the
// recoverable trash). The purge half of the write path: emptying trash, or
// removing something for good. Batched — one call takes an ItemId array.
async function __delete(ids) {
  const owa = __owa;
  const deleteItem = owa.op('DeleteItem');
  if (!deleteItem) return { __error: 'ops_unresolved' };
  const inbox = owa.folderByType('inbox') || owa.folders()[0];
  const mailboxInfo = inbox && inbox.mailboxInfo;
  let dr;
  try {
    dr = await deleteItem({ Header: __EWS_HEADER, Body: {
      __type: 'DeleteItemRequest:#Exchange',
      ItemIds: ids.map((id) => ({ __type: 'ItemId:#Exchange', Id: id })),
      DeleteType: 'HardDelete',
    }}, { mailboxInfo });
  } catch (e) { return { __error: 'delete_failed', what: String(e).slice(-120) }; }
  const items = (dr && dr.Body && dr.Body.ResponseMessages
    && dr.Body.ResponseMessages.Items) || [];
  const failed = items.filter((r) => !r || r.ResponseClass !== 'Success')
    .map((r) => (r && (r.ResponseCode || r.MessageText)) || 'unknown');
  return { ok: failed.length === 0, deleted: ids.length - failed.length, failed };
}
"""


async def _ews_attachments(attachments, *, stage=False):
    """Resolve outbound attachment refs → EWS `FileAttachment` JSON dicts, so an
    Outlook Message item carries them inline on `CreateItem`. Same two-shape
    contract gmail's `_resolve_attachments` speaks — the shared attachment wire
    the compose UI and CDP path all send:

      - `{path}`    — a blob already on the graph (an inbound attachment a
                      forward re-attaches, any graph file). Its bytes read
                      engine-side via `blobs.get`.
      - `{content}` — base64 bytes the compose UI just read off a picked/
                      dropped/pasted local file.

    EWS `Content` IS base64, and a blob's `data` is already base64, so bytes
    ride through with no decode/re-encode round-trip. A ref carrying neither is
    skipped, never guessed; `path` wins when both are present.

    Returns `(ews_file_attachments, path_refs)`. With `stage=True` (the draft
    autosave), a `content` ref is ALSO persisted to the blob store and its
    `path` echoed in `path_refs`, so the compose UI swaps content→path and later
    autosaves never re-ship the bytes (mirrors gmail's `_stage_attachments`)."""
    ews, refs = [], []
    for att in attachments or []:
        name = att.get("filename") or att.get("name") or "attachment"
        ctype = att.get("mimeType") or "application/octet-stream"
        path = att.get("path")
        content = att.get("content")
        if path:
            b64 = (await blobs.get(path=path))["data"]
        elif content:
            b64 = content
            if stage:
                ext = (name.rsplit(".", 1)[-1].lower() if "." in name
                       else (ctype.split("/")[-1] if "/" in ctype else ""))
                path = (await blobs.put(data=content, ext=ext))["path"]
        else:
            continue
        ews.append({
            "__type": "FileAttachment:#Exchange",
            "Name": name, "ContentType": ctype, "Content": b64,
        })
        if path:
            refs.append({"filename": name, "mimeType": ctype, "path": path})
    return ews, refs


def _payload(body: str, *, wait_ms: int) -> str:
    """Wrap an op body in the async IIFE: runtime-bootstrap defs → readiness
    gate (polls the bootstrap until the ops resolve) → helpers → body. The
    bootstrap (`__buildOwa`) is defined before the gate because the gate calls
    it; it defines `__owa`, which the helpers and body then use."""
    return ("(async () => {"
            + _RUNTIME
            + (_PRELUDE % {"wait_ms": wait_ms})
            + _HELPERS
            + body
            + "})()")


async def _eval(body: str, *, wait_ms: int = 30000, timeout_s: int = 45):
    """Run an op body in the outlook.live.com tab; surface structured errors.

    Headless by default — `browser_session.eval` pins the engine's background
    profile (`CONNECTOR_MODE`), where the mail this fetches surfaces in the Mail
    app. No browser window opens for a read.
    """
    value = await browser_session.eval(
        _TARGET, _payload(body, wait_ms=wait_ms), timeout=timeout_s
    )

    if isinstance(value, dict) and "__error" in value:
        code = value["__error"]
        if code == "auth_required":
            return app_error(
                "Outlook Web is signed out (the tab is on a Microsoft sign-in "
                "page). Run outlook.login and complete sign-in in the browser "
                "window, then retry.",
                code="NeedsAuth",
            )
        if code == "tab_not_ready":
            return app_error(
                "The outlook.live.com tab never became ready — the page may "
                "still be loading, or OWA's runtime didn't come up.",
                code="NotReady",
            )
        if code == "ops_unresolved":
            return app_error(
                "Couldn't resolve OWA's findItem/getItem functions from the "
                "webpack registry — OWA likely shipped a breaking build. "
                "Re-derive the bindings (see the connector's reverse-"
                "engineering header + the runtime-internals system doc).",
                code="BindingDrift",
            )
        if code == "folder_not_found":
            return app_error(
                f"No folder of type {value.get('dft')!r} in this mailbox. "
                f"Available: {value.get('available')}.",
                code="NotFound",
            )
        if code in ("find_failed", "get_failed", "move_failed", "create_failed"):
            return app_error(
                f"Outlook EWS {code}: {value.get('what')}",
                code="ProviderError",
            )
        return app_error(f"Outlook payload error: {code}", code="PayloadError")

    return value


# ──────────────────────────────────────────────────────────────────────
# The account trio — check / login / logout
# ──────────────────────────────────────────────────────────────────────
#
# ★ REFERENCE: the `login_window` sign-in pattern — copy this for ANY connector
#   whose login can't be driven headless (OAuth / SSO / MFA / CAPTCHA / a
#   password form you shouldn't type blind). Outlook is the reference for this
#   kind, the way WhatsApp is the reference for `qr`. The whole pattern is ONE
#   SDK call:
#
#       return await browser_session.login_window(SIGN_IN_URL, label="Outlook")
#
#   which (see `browser_session.login_window` + `chromium.rs::login_window`):
#     1. flips the engine's HEADLESS background profile → HEADED, chromeless
#        `--app=<url>` — a real, native-feeling sign-in window (no tabs/omnibox);
#     2. the human signs in ONCE, in that window;
#     3. `login_window(close=true)` flips the profile back to `--headless=new`.
#   Because it's the SAME `--user-data-dir` the headless daemon reads, the
#   session persists straight through — every subsequent op runs headless in that
#   profile. Never open the human's daily browser for this; never copy cookies.
#   Full write-up: `read({id:"apps-browser-driven", volume:"system"})` §"the
#   `login_window` flip".
#
# Outlook.com is a Microsoft account (MSA) login — OAuth/MFA the connector
# can't drive. So `login` hands the human off to `login_window`, which flips the
# engine's background profile headed for the sign-in and returns the session to
# its headless daemon when done — the session lands in the exact profile every
# headless read uses. `check_session` reads identity straight out of OWA's
# session store; `logout` performs the real MSA sign-out. All three are
# `@connection("none")` — the session is the browser profile, not a credential.

_IDENTITY_JS = """
  const id = __owa.identity();
  if (!id || !id.UserEmailAddress) return { authenticated: false };
  return {
    authenticated: true,
    at: { shape: 'product', name: 'Outlook', url: 'https://outlook.live.com/' },
    platform: 'email',
    identifier: id.UserEmailAddress,
    email: id.UserEmailAddress,
    handle: id.UserEmailAddress,
    displayName: id.UserDisplayName || null,
  };
"""


@account.check
@returns("account")
@connection("none")
@timeout(45)
async def check_session(**params):
    """Verify the Outlook Web session and identify the account.

    Reads the logged-in identity (email + display name) out of OWA's own
    session store — no network call. On a signed-out tab (redirected to the
    Microsoft sign-in page) returns `{authenticated: false}` so the resolver
    knows to drive `login`.
    """
    value = await _eval(_IDENTITY_JS, wait_ms=12000, timeout_s=30)
    # `_eval` maps the signed-out redirect to a NeedsAuth app_error; for
    # check_session that's just `authenticated: false`, not an error.
    if isinstance(value, dict) and value.get("code") == "NeedsAuth":
        return {"authenticated": False}
    return value


@account.login
@returns("account | auth_challenge")
@connection("none")
@timeout(60)
async def login(**params):
    """Sign in to Outlook.com — or report the already-live session.

    Returns the `account` when the background profile already holds a live
    Outlook session. Otherwise Outlook.com's Microsoft-account sign-in
    (OAuth + MFA) can't be driven programmatically, so this opens a headed
    sign-in window on the engine's background profile and returns a
    `login_window`-kind `auth_challenge`: the human signs in once, the agent
    polls `check_session`, and the session persists in the profile every
    headless read uses.
    """
    session = await check_session(**params)
    if isinstance(session, dict) and session.get("authenticated"):
        return session
    # `/mail/0/?nlp=1` skips the marketing landing page a logged-out visitor
    # otherwise lands on (nlp = "no landing page") and goes straight to the MSAL
    # sign-in, which redirects back to the inbox after login. The bare
    # `https://outlook.live.com/` dumps a signed-out user on the product page.
    return await browser_session.login_window(
        "https://outlook.live.com/mail/0/?nlp=1", label="Outlook"
    )


@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(45)
async def logout(**params):
    """Sign out of Outlook.com — the real Microsoft-account sign-out.

    Navigates the tab to the MSA logout endpoint, which clears the session
    from the engine's background profile. NOTE: this signs out the whole
    Microsoft account in that profile (any other MS surface the engine drives
    there), not just Outlook — the session is the shared profile. The next
    `login` re-signs in.
    """
    await browser_session.navigate(_TARGET, "https://login.live.com/logout.srf")
    return {
        "status": "logged_out",
        "hint": "Navigated to the Microsoft sign-out; the session is cleared "
                "from the engine's background profile. Re-run login to sign "
                "back in.",
    }


# ──────────────────────────────────────────────────────────────────────
# Mail
# ──────────────────────────────────────────────────────────────────────


@returns("email[]")
@provides("mailbox", account_param="account")
@connection("none")
@timeout(120)
async def list_emails(*, mailbox="inbox", limit=25, offset=0, **params):
    """List emails with full HTML bodies from a mailbox folder.

    Calls OWA's own `findItem` (ordered ids) then one batched `getItem`
    (full items + HTML bodies), mapped to the `email` shape like gmail.

    Args:
        mailbox: Folder — inbox · sent · drafts · trash · spam · archive.
            Provider-agnostic vocabulary shared with the other mailbox
            providers.
        limit: Max emails to return (most recent first).
        offset: Paging offset into the folder (0-based).
    """
    dft = _MAILBOX_TO_DFT.get(mailbox)
    if not dft:
        return app_error(
            f"Unknown mailbox {mailbox!r} — use one of {sorted(_MAILBOX_TO_DFT)}.",
            code="BadParams",
        )
    body = (f"const __dft = {json.dumps(dft)};"
            f"const __limit = {int(limit)};"
            f"const __offset = {int(offset)};"
            "return await __listEmails();")
    return await _eval(body, timeout_s=90)


@returns("email")
@provides("web_fetch", urls=["outlook.live.com/mail/*"])
@connection("none")
@timeout(90)
async def get_email(*, id=None, url=None, **params):
    """Get one email with full HTML body, headers, and recipients.

    Args:
        id: EWS ItemId (from `list_emails` results). When a mail URL is
            passed instead, the id is read from its path.
    """
    if url and not id:
        # outlook.live.com/mail/0/inbox/id/<ItemId> — the last path segment.
        tail = url.split("/id/", 1)[-1] if "/id/" in url else url
        id = [seg for seg in tail.split("/") if seg][0].split("?")[0]
    if not id:
        return app_error("Pass an email `id` (or a mail `url`).", code="BadParams")
    body = (f"const __id = {json.dumps(id)};"
            "const owa = __owa;"
            "if (!owa.getItem) return { __error: 'ops_unresolved' };"
            # Route via the primary mailbox (inbox folder carries mailboxInfo).
            "const inbox = owa.folderByType('inbox') || owa.folders()[0];"
            "const mailboxInfo = inbox && inbox.mailboxInfo;"
            "const smtp = (mailboxInfo && mailboxInfo.mailboxSmtpAddress) || null;"
            "let gResp;"
            "try { gResp = await owa.getItem({ Header: __EWS_HEADER, Body: {"
            "  __type: 'GetItemRequest:#Exchange',"
            "  ItemShape: { __type: 'ItemResponseShape:#Exchange', BaseShape: 'AllProperties', BodyType: 'HTML' },"
            "  ItemIds: [{ __type: 'ItemId:#Exchange', Id: __id }] } }, { mailboxInfo }); }"
            "catch (e) { return { __error: 'get_failed', what: String(e).slice(-120) }; }"
            "const it = gResp && gResp.Body && gResp.Body.ResponseMessages"
            "  && gResp.Body.ResponseMessages.Items && gResp.Body.ResponseMessages.Items[0]"
            "  && gResp.Body.ResponseMessages.Items[0].Items && gResp.Body.ResponseMessages.Items[0].Items[0];"
            "if (!it) return { __error: 'not_found' };"
            "const email = mapEmail(it, null, smtp); delete email.__ts; return email;")
    value = await _eval(body, timeout_s=75)
    if isinstance(value, dict) and value.get("__error") == "not_found":
        return app_error(f"No message with id {id!r}.", code="NotFound")
    return value


@test.skip(reason="sends real mail")
@returns("email")
@provides("email_send", account_param="account")
@connection("none")
@timeout(90)
async def send_email(*, to, subject, body, html_body=None, cc=None, bcc=None,
                     attachments=None, **params):
    """Send a new email as the Outlook.com account.

    Fires OWA's own `CreateItem` (MessageDisposition `SendAndSaveCopy`) —
    the request rides OWA's authed pipeline like every other op, and the
    sent copy lands in Sent Items. Mirrors gmail's `email_send` contract,
    attachments included (a forward's carried-over files ride in as
    `FileAttachment`s inline on the Message).

    Args:
        to: Recipient address(es), comma/semicolon separated.
        subject: Subject line.
        body: Plain-text body (used when html_body is absent).
        html_body: HTML body — wins over body when present.
        cc: Cc address(es), comma/semicolon separated.
        bcc: Bcc address(es), comma/semicolon separated.
        attachments: [{filename, mimeType, path?|content?}] — the shared
            attachment wire (see `_ews_attachments`).
    """
    ews, _refs = await _ews_attachments(attachments)
    msg = {"to": to, "subject": subject, "body": body,
           "html": html_body, "cc": cc, "bcc": bcc, "atts": ews}
    js = (f"const __msg = {json.dumps(msg)};"
          "const item = {"
          "  __type: 'Message:#Exchange',"
          "  Subject: __msg.subject || '',"
          "  Body: __bodyContent(__msg.body, __msg.html),"
          "  ToRecipients: __recipList(__msg.to),"
          "};"
          "if (__msg.cc) item.CcRecipients = __recipList(__msg.cc);"
          "if (__msg.bcc) item.BccRecipients = __recipList(__msg.bcc);"
          "if (__msg.atts && __msg.atts.length) item.Attachments = __msg.atts;"
          "return await __createItem(item, 'SendAndSaveCopy');")
    return await _eval(js, timeout_s=90)


@test.skip(reason="sends real mail")
@returns("email")
@provides("email_reply", account_param="account")
@connection("none")
@timeout(90)
async def reply_email(*, to, in_reply_to, subject, body, html_body=None,
                      cc=None, bcc=None, references=None, **params):
    """Reply to an email — stays in the original's thread.

    The reply contract is keyed on the RFC 2822 Message-ID (`in_reply_to`),
    provider-agnostic like gmail's. This adapter resolves it to the
    mailbox's own EWS ItemId (FindItem restriction on
    message:InternetMessageId), then fires OWA's `CreateItem` with a
    `ReplyToItem` referencing the original — Exchange threads it and
    quotes the reference body itself. Explicit To/Cc override the
    defaults, so reply-all is the caller widening the recipient set.

    Args:
        to: Recipient address(es), comma/semicolon separated.
        in_reply_to: RFC 2822 Message-ID of the message being replied to.
        subject: Subject line (Re: …).
        body: Plain-text reply body (used when html_body is absent).
        html_body: HTML reply body — wins over body when present.
        cc: Cc address(es), comma/semicolon separated.
        bcc: Bcc address(es), comma/semicolon separated.
        references: RFC 2822 References chain — unused (Exchange threads
            via the reference item), accepted for contract parity.
    """
    msg = {"to": to, "subject": subject, "body": body,
           "html": html_body, "cc": cc, "bcc": bcc, "mid": in_reply_to}
    js = (f"const __msg = {json.dumps(msg)};"
          "const refId = await __itemIdByMessageId(__msg.mid);"
          "if (!refId) return { __error: 'reply_target_not_found', mid: __msg.mid };"
          "const item = {"
          "  __type: 'ReplyToItem:#Exchange',"
          "  ReferenceItemId: refId,"
          "  Subject: __msg.subject || '',"
          "  NewBodyContent: __bodyContent(__msg.body, __msg.html),"
          "  ToRecipients: __recipList(__msg.to),"
          "};"
          "if (__msg.cc) item.CcRecipients = __recipList(__msg.cc);"
          "if (__msg.bcc) item.BccRecipients = __recipList(__msg.bcc);"
          "return await __createItem(item, 'SendAndSaveCopy');")
    value = await _eval(js, timeout_s=75)
    if isinstance(value, dict) and value.get("__error") == "reply_target_not_found":
        return app_error(
            f"No message with Message-ID {in_reply_to!r} in this mailbox — "
            "the reply target must exist in the account being replied from.",
            code="NotFound",
        )
    return value


@test.skip(reason="writes a real draft")
@returns("email")
@provides("email_draft", account_param="account")
@connection("none")
@timeout(90)
async def save_draft(*, to="", subject="", body="", html_body=None, cc=None, bcc=None,
                     draft_id=None, in_reply_to=None, attachments=None, **params):
    """Save a draft to the account's Drafts folder — the brokered `email_draft`.

    Fires OWA's own `CreateItem` with MessageDisposition `SaveOnly` (saved to
    Drafts, not sent). Updating a prior draft (`draft_id` present) creates the
    new version, then moves the superseded one to Deleted Items — one live
    draft per compose session (EWS has no cheap in-place message-body update
    without the item's ChangeKey). Mirrors gmail's `email_draft`; returns the
    `email` with its `draftId` stamped, plus the staged `attachments`
    (path-refs) so the compose UI swaps content→path and later autosaves never
    re-ship the bytes.

    Args:
        to: Recipient address(es), comma/semicolon separated.
        subject: Subject line.
        body: Plain-text body (used when html_body is absent).
        html_body: HTML body — wins over body when present.
        cc / bcc: Address(es), comma/semicolon separated.
        draft_id: The prior draft's ItemId, when updating (autosave).
        in_reply_to: RFC 2822 Message-ID this draft answers (parity with
            gmail; Exchange threads the eventual send off the reply item).
        attachments: [{filename, mimeType, path?|content?}] — the shared
            attachment wire (see `_ews_attachments`).
    """
    ews, refs = await _ews_attachments(attachments, stage=True)
    msg = {"to": to, "subject": subject, "body": body, "html": html_body,
           "cc": cc, "bcc": bcc, "prev": draft_id, "atts": ews, "refs": refs}
    js = (f"const __msg = {json.dumps(msg)};"
          "const item = {"
          "  __type: 'Message:#Exchange',"
          "  Subject: __msg.subject || '',"
          "  Body: __bodyContent(__msg.body, __msg.html),"
          "  ToRecipients: __recipList(__msg.to),"
          "};"
          "if (__msg.cc) item.CcRecipients = __recipList(__msg.cc);"
          "if (__msg.bcc) item.BccRecipients = __recipList(__msg.bcc);"
          "if (__msg.atts && __msg.atts.length) item.Attachments = __msg.atts;"
          "const created = await __createItem(item, 'SaveOnly');"
          "if (created && created.id) {"
          "  created.draftId = created.id;"
          "  if (__msg.refs && __msg.refs.length) created.attachments = __msg.refs;"
          # Retire the superseded version so an update leaves ONE live draft.
          # (No `//` comment inside this concatenated JS — the pieces join
          # with no newlines, so a line comment would swallow the rest of the
          # script through to the IIFE close: "Unexpected end of input".)
          "  if (__msg.prev && __msg.prev !== created.id) {"
          "    try { await __move(__msg.prev, 'deleteditems'); } catch (e) {}"
          "  }"
          "}"
          "return created;")
    return await _eval(js, timeout_s=75)


@returns("email")
@provides("email_trash", account_param="account")
@connection("none")
@timeout(60)
async def trash_email(*, id, **params):
    """Delete an email — move it to Deleted Items (recoverable, like gmail's
    trash). Fires OWA's own `MoveItem` to the Deleted Items folder.

    Args:
        id: EWS ItemId (from `list_emails` results).
    """
    body = f"const __id = {json.dumps(id)}; return await __move(__id, 'deleteditems');"
    return await _eval(body, timeout_s=45)


@returns("email")
@provides("email_archive", account_param="account")
@connection("none")
@timeout(60)
async def archive_email(*, id, **params):
    """Archive an email — move it out of the inbox to the Archive folder via
    OWA's own `MoveItem`.

    Args:
        id: EWS ItemId (from `list_emails` results).
    """
    body = f"const __id = {json.dumps(id)}; return await __move(__id, 'archive');"
    return await _eval(body, timeout_s=45)


@returns({"ok": "boolean"})
@connection("none")
@timeout(60)
async def batch_delete_email(*, ids, **params):
    """Permanently delete emails — EWS `DeleteItem` with `HardDelete`, so they
    skip Deleted Items entirely (unlike `email_trash`, a recoverable move to
    Deleted Items). This is the delete-from-trash / delete-for-good half,
    matching gmail's `batch_delete_email`. CANNOT BE UNDONE.

    Args:
        ids: EWS ItemIds to delete (from `list_emails` results).
    """
    body = f"const __ids = {json.dumps(ids)}; return await __delete(__ids);"
    return await _eval(body, timeout_s=45)
