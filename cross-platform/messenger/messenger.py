"""Facebook Messenger — live DMs via the engine-held browser, hooking Meta's
E2EE **Lightspeed/MSYS** client (codename *Armadillo*) that runs in the
`MAWMainV4WebWorkerBundle` **shared worker** — NOT the page.

Read `operations.md` first — it is the durable reverse-engineering reference
(the worker require-registry, the `cachedModules` sproc hook, the positional
decode, the Dexie read tables, the LS-task send path, the engine gate). This
file is the connector; the same "hook the client's own decrypted store, never
the wire" philosophy as WhatsApp/Instagram, one level deeper (the worker).

WHY the worker and not the page (the deciding fact): Messenger web is Meta's
Signal-based E2EE client. Decryption happens **in the shared worker**; the page
Relay store holds ZERO message records (proven). So — unlike Instagram, whose
plaintext lives in the page's Relay store — the only place Messenger plaintext
exists is inside the worker, post-decrypt. We attach the engine browser plane to
the *worker* CDP target (the `worker:<title>` target kind added to
`browser.rs`), read the worker's Dexie tables read-only, and arm the worker's
own stored-procedure map (`LSDynamicDependencies.cachedModules`) for `watch`.

IDs: conversation = `threadKey` (numeric string); message = `messageId`;
account = FBID (viewer = the `c_user` cookie, plain/non-httpOnly).

Ops that are NOT yet exercised end-to-end against a live upsert are marked
`TODO(verify)` — do not trust them until the readme's status table says proven.
"""

import json
import re
from datetime import datetime, timezone
from hashlib import sha1

from agentos import (
    account, app_error, blobs, browser_session,
    provides, returns, services, timeout, url,
)

# ──────────────────────────────────────────────────────────────────────
# Surface & session (deltas from Instagram — see operations.md "Surface")
# ──────────────────────────────────────────────────────────────────────
# TWO CDP targets, one profile:
#   • the PAGE (`facebook.com`) — cookies (check_session), and the composer for
#     send (the DOM textbox lives on the page).
#   • the WORKER (`worker:MAWMainV4WebWorkerBundle`) — every message read + the
#     `watch` hook + outbound LS tasks (the E2EE store + logic live here).
# The `worker:<title>?probe=…` selector is resolved by the engine browser plane
# (`resolve_worker_ws` in browser.rs) by name first, behavior second; eval runs
# IN the worker's own context (its event loop), so read-only Dexie transactions
# are safe — the external-CDP deadlock warning in operations.md is about
# readwrite locks, which
# we never take.
_PAGE = "facebook.com"
_WORKER_PROBE = (
    "(typeof self.require==='function' && "
    "(()=>{try{return !!self.require('MAWCurrentUser')}catch(e){return false}})())"
)
_WORKER = url.build(
    "worker:MAWMainV4WebWorkerBundle", params={"probe": _WORKER_PROBE})
_MESSAGES_URL = "https://www.facebook.com/messages/"
_LOGIN_URL = "https://www.facebook.com/login/"

# The TRUE session signal (httpOnly — read via the cookies plane, never page
# JS): `xs`. Present ⇒ writes will work. The viewer FBID is the `c_user`
# cookie (plain), so identity + isOutgoing need no scan (unlike IG's igid hop).
_SESSION_COOKIE = "xs"
_VIEWER_COOKIE = "c_user"

# Mode: the connector runs headless in the engine's background profile (rule
# 19) — reads surface in the Messaging app, so no window opens. Every
# `browser_session` verb now defaults to `background` at the engine layer (and
# the `eval/navigate/read_cookies` SDK helpers pin `CONNECTOR_MODE="background"`
# too), so a mode is never required. We still pass `_BG` explicitly on the raw
# verbs (`subscribe`/`type`/`key`) as a readable assertion that this session is
# headless — never a foreground leak into the human's daily browser.
_BG = "background"

_WATCH_MARKER = "__agentos_entity__"


# ──────────────────────────────────────────────────────────────────────
# JS building blocks — all run in the WORKER context (`self`, `self.require`;
# no `window`, no `document`). Kept self-contained so the watch hook (which the
# engine re-arms on every worker respawn) carries its own mappers.
# ──────────────────────────────────────────────────────────────────────

# Coercion. LS i64 values reach JS as number | numeric-string | {low,high} |
# [low,high]; coerce before any Date()/compare so a wrapped id never nulls a
# field (the bug that bit IG's `published` when timestamps arrived as strings).
_HELPERS = r"""
// Safe accessor: a freshly-respawned/booting worker has neither `self.require`
// nor a global `require` yet — a bare `require` reference THROWS (not undefined),
// so never touch it directly. Ops guard on REQ being null (→ not_ready).
const REQ = (typeof self !== 'undefined' && self.require)
  || (typeof require !== 'undefined' ? require : null);
const req = (n) => { const m = REQ(n); return (m && m.default) ? m.default : m; };
const str = (v) => (typeof v === 'string' ? v : (v == null ? '' : String(v)));
const num = (v) => {
  if (v == null) return null;
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string') { const n = Number(v); return Number.isFinite(n) ? n : null; }
  if (typeof v === 'object') {
    if ('low' in v && 'high' in v) return v.high * 4294967296 + (v.low >>> 0);
    if (Array.isArray(v) && v.length === 2) return v[1] * 4294967296 + (v[0] >>> 0);
    try { const n = Number(v.toString()); if (Number.isFinite(n)) return n; } catch (e) {}
  }
  return null;
};
const isoMs = (v) => { const n = num(v); return n != null ? new Date(n).toISOString() : null; };
// Unix time → ISO. Only positive values publish; 0/missing stay null (ts===0
// is a real sentinel on empty/admin rows — `ts != null` is true for 0 and used
// to paint "Wed, Dec 31, 1969" day chips). Values >1e12 are already ms;
// otherwise treat as seconds. Prefer explicit fallbacks when primary is 0.
const isoUnix = (v) => {
  const n = num(v);
  if (n == null || n <= 0) return null;
  return isoMs(n > 1e12 ? n : n * 1000);
};
const msgPublished = (d) =>
  isoUnix(d.ts) || isoUnix(d.serverTs) || isoUnix(d.sortOrderMs);

// Map a DECRYPTED message row → a `message` entity. Input is the output of
// require('MAWDbObjEncryption').decryptDbObj(rawRow,'messages',resolver): the
// text lives in `msgContent.content`, `ts` is unix SECONDS, and `author` is
// '@me' for the viewer (⇒ isOutgoing) else '<fbid>@msgr'. See operations.md "READ".
const mapMsgRow = (d) => {
  if (!d) return null;
  const id = str(d.msgId);
  if (!id) return null;
  const mc = (d.msgContent && typeof d.msgContent === 'object') ? d.msgContent : {};
  const isOut = d.author === '@me';
  const out = {
    id,
    content: str(mc.content),
    published: msgPublished(d),
    conversationId: str(d.threadJid).split('@')[0],
    type: 'text',
  };
  if (isOut) { out.isOutgoing = true; out.author = 'Me'; }
  else {
    out.isOutgoing = false;
    const fb = str(d.author).split('@')[0];
    if (fb) out.from = { platform: 'messenger', id: fb };
  }
  const t = str(d.type);
  if (t === 'Admin') out.type = 'system';
  else if (t === 'Image') out.type = 'image';
  else if (t === 'Ptt') out.type = 'audio';
  else if (t === 'XMA') out.type = 'share';
  // reply: quote.content is the quoted message; its .content is the quoted text.
  if (d.quote && d.quote.content) {
    const q = d.quote.content;
    out.replyTo = {};
    if (q.content) out.replyTo.snippet = str(q.content).slice(0, 120);
    if (d.quote.remoteJid) out.replyTo.from = str(d.quote.remoteJid).split('@')[0];
  }
  if (d.isForwarded) out.isForwarded = true;
  if (d.isUnsent) out.isDeleted = true;
  return out;
};

// Map upsertMessage's POSITIONAL args (the watch hook — the sproc receives a
// flat arg list, decoded per operations.md, cross-checked vs mautrix-meta's
// LSInsertMessage index tags). Carries __ts so the hook can drop backfill.
// TODO(verify): validate every index against a captured live upsertMessage.
const mapMsgArgs = (a) => {
  if (!a || !a.length) return null;
  const id = str(a[8]);
  if (!id) return null;
  const sender = str(a[10]);
  const out = {
    id,
    content: str(a[0]),
    published: isoUnix(a[5]),
    conversationId: str(a[3]),
    type: 'text',
    __ts: num(a[5]),
  };
  if (typeof ME !== 'undefined' && ME && sender) {
    out.isOutgoing = (sender === ME);
    out.author = out.isOutgoing ? 'Me' : null;
    if (!out.isOutgoing) out.from = { platform: 'messenger', id: sender };
  } else if (sender) {
    out.from = { platform: 'messenger', id: sender };
  }
  if (num(a[11])) out.type = 'sticker';
  if (a[12] === 1 || a[12] === true) out.type = 'system';
  // Confirmed positional fields (from the upsertMessage sproc source).
  const rsnip = str(a[27]), rsrc = str(a[23]);
  if (rsnip || rsrc) { out.replyTo = {}; if (rsrc) out.replyTo.id = rsrc; if (rsnip) out.replyTo.snippet = rsnip.slice(0, 120); }
  if (a[43] === 1 || a[43] === true) out.isForwarded = true;
  if (a[17] === 1 || a[17] === true) out.isDeleted = true;
  return out;
};

// Map a DECRYPTED thread row → a `conversation` entity. Input is
// decryptDbObj(rawRow,'threads',resolver): `jid` is the thread's routing id
// (matches messages.threadJid), `newestMsgTs` is recency. Display name/face
// come from the PAGE Lightspeed ReStore join (see `_contact_index`) — this
// table itself only carries routing + recency for E2EE threads.
const mapThreadRow = (t) => {
  if (!t) return null;
  const id = str(t.jid).split('@')[0];
  if (!id) return null;
  const out = {
    id,
    name: str(t.threadName || t.name),
    unreadCount: 0,
    published: isoUnix(t.newestMsgTs),
  };
  if (t.folder != null && num(t.folder) !== 0) out.folder = num(t.folder);
  return out;
};
"""

# Robust read-only handle on the worker's Dexie DB. `getDBExn()` throws while
# Meta is mid-migration (getDbStatus()==='migrating') — surface that as a clean
# not_ready sentinel, never a raw throw. Read-only only (.toArray / .where /
# .get) — never a readwrite transaction (would contend with the client).
_GET_DB = r"""
const dbStatus = () => { try { const M = REQ('MAWIndexedDb'); return M.getDbStatus ? M.getDbStatus() : 'unknown'; } catch (e) { return 'unknown'; } };
// SYNCHRONOUS only. getDBExn() returns the Dexie db when ready and THROWS while
// the client is initializing/migrating — caught here → null → a fast not_ready.
// Never call the async getDB(): its promise never resolves during a migration,
// which hangs the whole eval to the command timeout (the 45s bug).
const getDB = () => {
  try { const db = REQ('MAWIndexedDb').getDBExn(); return (db && db.tables) ? db : null; }
  catch (e) { return null; }
};
"""

# EAR (encryption-at-rest) read path — the deciding mechanism. The MAW store keeps
# only routing/index columns in the clear; the message body is a sealed
# `_encryptedContent` blob. Decrypt with the CLIENT'S OWN codec — requireable by
# name, SYNCHRONOUS, no transaction/lock (found + validated live 2026-07-08:
# 300/300 msgs, 0 err). The registry walk that discovered the module names is a
# discovery-time tool, not a runtime dependency — see operations.md "READ" and
# reverse-engineering-runtime-internals Technique 1c.
_EAR = r"""
// decrypt one raw row (row has `_encryptedContent`) → the full plaintext row.
// decryptDbObj returns non-encrypted rows unchanged, so it's safe on any row.
const decryptRow = (row, table) => {
  try {
    const enc = REQ('MAWDbObjEncryption');                        // the EAR codec
    const resolver = REQ('MWEARKeychainV3').getDbEncryptionKey;   // in-memory key resolver
    return enc.decryptDbObj(Object.assign({}, row), table, resolver);
  } catch (e) { return null; }   // unsettled keychain / undecryptable → skip
};
// The MAW db name, derived from the client (never hardcode a user-scoped name).
const _dbName = () => { try { return REQ('MAWIndexedDbMetadata').dbName(REQ('MAWCurrentUser').getID()); } catch (e) { return null; } };
// Raw READ-ONLY IndexedDB read of an encrypted store — bypasses the client's
// transactor entirely (readonly IDB txns don't contend → no lock, no deadlock).
// `threadJid` (optional) uses the store's threadJid index for a scoped read.
const rawRows = (store, threadJid) => new Promise((resolve) => {
  const name = _dbName();
  if (!name) return resolve([]);
  let rq; try { rq = indexedDB.open(name); } catch (e) { return resolve([]); }
  rq.onerror = () => resolve([]);
  rq.onsuccess = () => {
    const db = rq.result;
    if (!db.objectStoreNames.contains(store)) { db.close(); return resolve([]); }
    const rows = [];
    let cur;
    try {
      const os = db.transaction(store, 'readonly').objectStore(store);
      cur = (threadJid && os.indexNames.contains('threadJid'))
        ? os.index('threadJid').openCursor(IDBKeyRange.only(threadJid))
        : os.openCursor();
    } catch (e) { db.close(); return resolve([]); }
    cur.onerror = () => { db.close(); resolve(rows); };
    cur.onsuccess = (e) => {
      const c = e.target.result;
      if (c) { rows.push(c.value); c.continue(); }
      else { db.close(); resolve(rows); }
    };
  };
});
"""


def _worker_body(body: str, *, me: str = "") -> str:
    """Wrap a worker-op body in an async IIFE with helpers + DB accessor +
    the EAR decrypt/read path + the injected viewer FBID (`ME`)."""
    return ("(async () => {"
            + f"const ME = {json.dumps(me)};"
            + _HELPERS
            + _GET_DB
            + _EAR
            + body
            + "})()")


def _evidence(workers=None, *, db_status=None, has_key=None, error=None):
    """Stable not-ready evidence carried from browser transport to the UI."""
    workers = workers or []
    evidence = {
        "workers": workers,
        "responsive": any(w.get("responsive") is True for w in workers),
        "titlesEmpty": bool(workers) and all(
            not w.get("title") and not w.get("url") for w in workers),
        "dbStatus": db_status,
        "hasKey": has_key,
    }
    if error:
        evidence["error"] = str(error)
    return evidence


def _not_ready(cause, evidence):
    return {"__error": "not_ready", "status": cause, "evidence": evidence}


async def _worker_targets():
    """Inventory + behavior-probe every worker without touching a cached
    connector session. `responsive` is the built-in 1+1 liveness result;
    `probe` identifies the MAW worker even when Chromium leaves title/url empty.
    """
    result = await services.call("browser_session", verb="targets", params={
        "mode": _BG,
        "kinds": ["worker"],
        "probe": _WORKER_PROBE,
        "timeoutMs": 1500,
    })
    rows = result.get("targets", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


async def _worker_health():
    """Read Messenger-specific DB/key evidence in the behavior-selected worker.

    `hasKey` is proven against a real encrypted row through Meta's own codec;
    merely finding `getDbEncryptionKey` would only prove the function exists.
    """
    return await browser_session.eval(_WORKER, _worker_body(r"""
    const status = dbStatus();
    let raw = await rawRows('threads');
    let table = 'threads';
    let sample = raw.find((row) => row && row._encryptedContent != null);
    if (!sample) {
      raw = await rawRows('messages'); table = 'messages';
      sample = raw.find((row) => row && row._encryptedContent != null);
    }
    let hasKey = null;
    if (sample) {
      try {
        const enc = REQ('MAWDbObjEncryption');
        const resolver = REQ('MWEARKeychainV3').getDbEncryptionKey;
        hasKey = !!enc.decryptDbObj(Object.assign({}, sample), table, resolver);
      } catch (e) { hasKey = false; }
    }
    return { dbStatus: status, hasKey };
    """), timeout=5)


async def _worker_eval(body: str, *, me: str = "", timeout_s: int = 45):
    """Run a body in the WORKER context; return the raw JS value.

    Ensures the page (which spawns the worker) is loaded first, then evals on
    the `worker:` target (background profile — the SDK helper pins it). No error
    translation — the caller inspects the raw `{__error}` sentinel.

    Classifies transport and E2EE-store readiness before the full operation. The
    generic browser plane supplies target identity/liveness; this connector owns
    Messenger-specific meaning and user-facing causes.
    """
    try:
        await _ensure_messages()
    except Exception as e:
        return _not_ready("dead_session", _evidence(error=e))

    try:
        workers = await _worker_targets()
    except Exception as e:
        return _not_ready("dead_session", _evidence(error=e))
    evidence = _evidence(workers)
    if not workers:
        return _not_ready("no_target", evidence)
    responsive = [w for w in workers if w.get("responsive") is True]
    if not responsive:
        return _not_ready("wedged", evidence)
    matched = [w for w in responsive if w.get("probe") is True]
    titled = [w for w in responsive if
              "MAWMainV4WebWorkerBundle" in str(w.get("title") or "") or
              "MAWMainV4WebWorkerBundle" in str(w.get("url") or "")]
    if not matched and titled:
        return _not_ready("booting", evidence)
    if not matched:
        return _not_ready("no_target", evidence)

    try:
        health = await _worker_health()
    except Exception as e:
        return _not_ready("dead_session", _evidence(workers, error=e))
    if not isinstance(health, dict):
        return _not_ready("booting", evidence)
    evidence = _evidence(
        workers,
        db_status=health.get("dbStatus"),
        has_key=health.get("hasKey"),
    )
    if health.get("hasKey") is False and health.get("dbStatus") == "migrating":
        return _not_ready("needs_pin", evidence)

    try:
        value = await browser_session.eval(
            _WORKER, _worker_body(body, me=me), timeout=timeout_s)
    except Exception as e:
        # Inventory proved a responsive logical target immediately before this
        # eval; failure here is the cached/direct session dying underneath it.
        return _not_ready("dead_session", _evidence(
            workers,
            db_status=health.get("dbStatus"),
            has_key=health.get("hasKey"),
            error=e,
        ))
    if isinstance(value, dict) and value.get("__error") == "not_ready":
        value["status"] = "booting"
        value["evidence"] = evidence
    return value


def _translate(value):
    """Map a raw op result's `__error` sentinel to a user-facing app_error."""
    if isinstance(value, dict) and "__error" in value:
        code = value["__error"]
        if code == "auth_required":
            return browser_session.needs_auth(
                "Messenger isn't logged in on the engine browser. Run "
                "messenger.login to sign in, then retry.",
                login_url=_LOGIN_URL,
            )
        if code == "not_ready":
            cause = value.get("status", "booting")
            messages = {
                "no_target": (
                    "Messenger's decrypted store worker isn't running — reload /messages."
                ),
                "booting": "Messenger is still loading (a few seconds).",
                "wedged": (
                    "Messenger's worker is overloaded (E2EE sync) — retrying."
                ),
                "dead_session": "Reconnecting to Messenger…",
                "needs_pin": "Messenger needs its restore PIN — sign in to restore chats.",
            }
            extra = {
                "notReady": cause,
                "evidence": value.get("evidence") or _evidence(),
            }
            if cause == "needs_pin":
                extra["action"] = {
                    "label": "Sign in",
                    "route": "plugins/messenger?tool=login",
                }
            return app_error(
                messages.get(cause, "Messenger's decrypted store is unavailable."),
                code="NotReady",
                **extra,
            )
        if code == "not_found":
            return app_error(
                f"No match for {value.get('ref')!r} ({value.get('what', 'item')}).",
                code="NotFound",
            )
        return app_error(f"Messenger payload error: {code}: {value.get('what', '')}",
                         code="PayloadError")
    return value


async def _eval(body: str, *, me: str = "", timeout_s: int = 45):
    """Run a WORKER read op; surface structured errors."""
    return _translate(await _worker_eval(body, me=me, timeout_s=timeout_s))


async def _read_cookies():
    """Read facebook.com cookies (httpOnly included) via the page plane."""
    return await browser_session.read_cookies(_PAGE)


async def _viewer_fbid() -> str:
    """The viewer's FBID — the `c_user` cookie, read straight off the jar."""
    jar = await _read_cookies()
    return (jar.get(_VIEWER_COOKIE) or {}).get("value") or ""


async def _ensure_messages():
    """Ensure facebook.com/messages is loaded on the PAGE target so the shared
    worker exists to attach to. Only navigates when off a messages surface —
    the worker + its store are cumulative and the armed watch parks the tab
    there, so warm reads never reload (the IG refresh-storm lesson)."""
    on_msgs = await browser_session.eval(
        _PAGE, "location.pathname.startsWith('/messages')", timeout=10)
    if on_msgs is not True:
        await browser_session.navigate(_PAGE, _MESSAGES_URL, timeout=30)


# One page ReStore transaction → contacts + participants + threads index.
# Names/faces live in the PAGE Lightspeed store (GetLsDatabaseDeferredForDisplay),
# not the worker MAW EAR store (which holds E2EE bodies only). The worker LS
# tables are empty; do not point this at the worker. TX API: runInTransaction(cb)
# receives {table, transactionTable} — pass table NAMES ('contacts'), never
# numeric ids or LSDbV1 schema objects (those throw on primaryKeyIds). ReQL
# iterates the transaction tables; I64.to_string is the only safe id stringify.
_CONTACT_INDEX = r"""
(async () => {
  const R = (typeof require === 'function') ? require : null;
  if (!R) return { __error: 'no_require' };
  let store;
  try { store = await R('GetLsDatabaseDeferredForDisplay').get(); }
  catch (e) { return { __error: 'no_store', what: String(e && e.message || e) }; }
  if (!store || typeof store.runInTransaction !== 'function') {
    return { __error: 'no_store' };
  }
  const ReQL = R('ReQL');
  const I64 = R('I64');
  let me = '';
  try { me = String(R('MAWCurrentUser').getID()); } catch (e) {}
  const idStr = (v) => {
    if (v == null) return '';
    try { if (I64 && I64.to_string) return String(I64.to_string(v)); } catch (e) {}
    if (typeof v === 'number' && Number.isFinite(v)) return String(v);
    if (typeof v === 'string') return v.split('@')[0];
    try { return String(v).split('@')[0]; } catch (e) { return ''; }
  };
  try {
    return await store.runInTransaction(async (tx) => {
      const contacts = await ReQL.toArrayAsync(
        ReQL.fromTableAscending(tx.table('contacts')));
      const participants = await ReQL.toArrayAsync(
        ReQL.fromTableAscending(tx.table('participants')));
      const threads = await ReQL.toArrayAsync(
        ReQL.fromTableAscending(tx.table('threads')));
      const contactById = {};
      for (const c of contacts) {
        if (!c) continue;
        const id = idStr(c.id);
        if (!id) continue;
        const name = c.name
          || [c.firstName, c.secondaryName].filter(Boolean).join(' ')
          || '';
        contactById[id] = {
          name: name || null,
          image: c.profilePictureUrl || c.profilePictureLargeUrl || null,
        };
      }
      const partsByThread = {};
      for (const p of participants) {
        if (!p) continue;
        const tk = idStr(p.threadKey);
        const cid = idStr(p.contactId);
        if (!tk || !cid) continue;
        (partsByThread[tk] ||= []).push(cid);
      }
      const threadMeta = {};
      for (const t of threads) {
        if (!t) continue;
        const tk = idStr(t.threadKey);
        if (!tk) continue;
        threadMeta[tk] = {
          name: t.threadName || null,
          image: t.threadPictureUrl || null,
          // I64 threadType: 1=1:1, higher values include groups / marketplace.
          type: idStr(t.threadType),
        };
      }
      return {
        me,
        contacts: contactById,
        participants: partsByThread,
        threads: threadMeta,
      };
    });
  } catch (e) {
    return { __error: 'tx_failed', what: String(e && e.message || e) };
  }
})()
"""


async def _contact_index():
    """Batched read-only Lightspeed contacts/participants/threads from the PAGE.

    One transaction, no per-thread round trips. Returns maps keyed by plain
    FBID/threadKey strings, or None if the page store is unavailable (E2EE
    threads still list; they just keep numeric names).
    """
    await _ensure_messages()
    try:
        idx = await browser_session.eval(
            _PAGE, _CONTACT_INDEX, timeout=30)
    except Exception:
        return None
    if not isinstance(idx, dict) or idx.get("__error") or "contacts" not in idx:
        return None
    return idx


def _enrich_threads(threads, index):
    """Join MAW-decrypted threads with the page ReStore contact index.

    Join order (first hit wins for display name/image):
      1. LS threadName / threadPictureUrl (groups, marketplace rows).
      2. Participants → non-viewer contact(s) via contacts table
         (MWGetOtherContactOrSelf join, batched).
      3. 1:1 E2EE: threadKey IS the peer FBID → contacts[threadKey].
      4. Notes-to-self: threadKey == viewer → contacts[me].
    """
    if not isinstance(threads, list) or not isinstance(index, dict):
        return threads
    contacts = index.get("contacts") or {}
    parts_by = index.get("participants") or {}
    meta_by = index.get("threads") or {}
    me = str(index.get("me") or "")
    out = []
    for row in threads:
        if not isinstance(row, dict) or not row.get("id"):
            out.append(row)
            continue
        c = dict(row)
        tid = str(c["id"])
        meta = meta_by.get(tid) or {}
        part_ids = list(parts_by.get(tid) or [])
        others = [p for p in part_ids if p and p != me]
        name = (meta.get("name") or c.get("name") or "").strip() or None
        image = meta.get("image") or None
        # Participants join (classic LS / multi-party).
        if not name and len(others) == 1:
            peer = contacts.get(others[0]) or {}
            name = peer.get("name") or name
            image = image or peer.get("image")
        elif not name and len(others) > 1:
            names = [(contacts.get(p) or {}).get("name") for p in others]
            names = [n for n in names if n]
            if names:
                name = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
        # E2EE 1:1: the thread jid identity IS the peer FBID.
        if not name:
            peer = contacts.get(tid) or {}
            if peer.get("name") and tid != me:
                name = peer["name"]
                image = image or peer.get("image")
            elif tid == me:
                self_c = contacts.get(me) or {}
                name = self_c.get("name") or "Me"
                image = image or self_c.get("image")
        # Fill image from peer contact when only name came from meta elsewhere.
        if not image:
            if len(others) == 1:
                image = (contacts.get(others[0]) or {}).get("image")
            elif tid != me:
                image = (contacts.get(tid) or {}).get("image")
        # isGroup: >1 other participant, or a named multi-party LS row that is
        # not classic 1:1 type "1". Type "1" is ONE_TO_ONE; leave unknown
        # E2EE (no LS meta/parts) as direct.
        ttype = str(meta.get("type") or "")
        is_group = (
            len(others) > 1
            or (bool(name) and len(part_ids) > 2)
            or (ttype not in ("", "1") and len(others) != 1 and bool(meta.get("name")))
        )
        if name:
            c["name"] = name
        if image:
            c["image"] = image
        c["isGroup"] = bool(is_group)
        # participant[] — same grammar as WhatsApp/IG (1:1 peer as account).
        if not is_group:
            peer_id = others[0] if others else (tid if tid != me else None)
            if peer_id:
                peer = contacts.get(peer_id) or {}
                part = {"platform": "messenger", "id": peer_id}
                if peer.get("name"):
                    part["display_name"] = peer["name"]
                if image:
                    part["image"] = image
                c["participant"] = [part]
        else:
            parts = []
            for pid in (others or part_ids):
                if not pid or pid == me:
                    continue
                peer = contacts.get(pid) or {}
                part = {"platform": "messenger", "id": pid}
                if peer.get("name"):
                    part["display_name"] = peer["name"]
                peer_img = peer.get("image")
                if peer_img:
                    part["image"] = peer_img
                parts.append(part)
            if parts:
                c["participant"] = parts
        out.append(c)
    return out


async def _stage_conversation_faces(threads):
    """Fetch CDN face URLs in the facebook.com page (session cookies) → blobs.

    Messenger already joins profilePictureUrl onto `conversation.image`; this
    stages those bytes so Messaging `/thumb` can serve them and remember:true
    persists a durable path + mimeType instead of a hot CDN link.
    """
    if not isinstance(threads, list) or not threads:
        return threads
    urls = []
    for t in threads:
        if not isinstance(t, dict):
            continue
        img = t.get("image")
        if isinstance(img, str) and img.startswith("http"):
            urls.append(img)
    unique = list(dict.fromkeys(urls))
    if not unique:
        return threads
    try:
        fetched = await browser_session.eval(
            _PAGE,
            "(async () => {"
            f"const urls = {json.dumps(unique)};"
            "const out = {};"
            "await Promise.all(urls.map(async (u) => {"
            "  try {"
            "    const r = await fetch(u);"
            "    if (!r.ok) return;"
            "    const ab = await r.arrayBuffer();"
            "    if (!ab.byteLength || ab.byteLength > 500000) return;"
            "    const bytes = new Uint8Array(ab);"
            "    let bin = ''; const CHUNK = 0x8000;"
            "    for (let i = 0; i < bytes.length; i += CHUNK)"
            "      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));"
            "    out[u] = { data: btoa(bin),"
            "      mime: (r.headers.get('content-type') || 'image/jpeg').split(';')[0] };"
            "  } catch (e) {}"
            "}));"
            "return out;"
            "})()",
            timeout=60,
        )
    except Exception:
        return threads
    if not isinstance(fetched, dict):
        return threads
    for t in threads:
        if not isinstance(t, dict):
            continue
        img = t.get("image")
        face = fetched.get(img) if isinstance(img, str) else None
        if not isinstance(face, dict) or not face.get("data"):
            continue
        mime = (face.get("mime") or "image/jpeg").split(";")[0].strip() or "image/jpeg"
        ext = mime.split("/")[-1] or "jpg"
        if ext == "jpeg":
            ext = "jpg"
        try:
            blob = await blobs.put(face["data"], ext=ext)
        except Exception:
            continue
        t["image"] = blob["path"]
        t["mimeType"] = mime
    return threads


# ──────────────────────────────────────────────────────────────────────
# The account trio — check / login / logout
# ──────────────────────────────────────────────────────────────────────

@account.check
@returns("account")
@timeout(45)
async def check_session(**params):
    """Verify the Messenger login and identify the viewer.

    `authenticated: true` means WRITES WILL WORK — proven by the httpOnly `xs`
    cookie (the true session signal, read via the cookies plane), NOT cached
    page/worker state (which survives an expired session and lies). Identity
    (`identifier`) is the viewer FBID straight off the `c_user` cookie.
    """
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return {"authenticated": False}
    acct = {
        "authenticated": True,
        "platform": "messenger",
        "at": {"shape": "product", "name": "Messenger",
               "url": "https://www.facebook.com/messages/"},
    }
    fbid = (jar.get(_VIEWER_COOKIE) or {}).get("value")
    if fbid:
        acct["identifier"] = fbid
    return acct


@account.login
@returns("account | auth_challenge")
@timeout(60)
async def login(**params):
    """Sign in to Facebook (for Messenger), or report the account if already in.

    Facebook login is username/password fronted by a **password re-confirm**
    step even when `xs` is present, plus optional 2FA/checkpoint — so the
    sign-in is completed in a HEADED flip of the engine's background profile
    (the human watches and signs in; we don't type creds blind into Meta's bot
    checks). The session lands in the exact bg profile every headless read uses.
    No credential pre-resolve — the human signs in at the window (the
    login_window pattern; ★ ref outlook.py).
    """
    existing = await check_session()
    if isinstance(existing, dict) and existing.get("authenticated"):
        return existing

    return await browser_session.login_window(
        _LOGIN_URL,
        label="Facebook Messenger",
        instructions=(
            "Sign in to Facebook in the window that opened on the engine's "
            "background profile. Complete the password re-confirm and any 2FA / "
            "checkpoint, then poll messenger.check_session. Call the "
            "login_window service with close=true when done."
        ),
        retrieval={
            "via": "email",
            "look_for": "a Facebook login code or a 'was this you' sign-in "
                        "confirmation",
        },
    )


@account.logout
@returns({"ok": "boolean", "message": "string"})
@timeout(45)
async def logout(**params):
    """Log out of Facebook in the engine browser.

    TODO(verify): drive the real logout (menu → Log out) or clear the session
    cookies. Stubbed to keep the account trio complete (validator requires it).
    """
    return {"ok": False, "message": "logout not yet implemented — see readme"}


# ──────────────────────────────────────────────────────────────────────
# Read — the worker's Dexie tables (read-only)
# ──────────────────────────────────────────────────────────────────────

@returns("conversation[]")
@provides("chats", account_param="account")
@timeout(90)
async def list_conversations(*, limit=50, **params):
    """List Messenger threads (most recent first).

    Reads the worker's encrypted `threads` store read-only and decrypts each row
    with the client's own EAR codec (require('MAWDbObjEncryption').decryptDbObj).
    Display name/face come from one batched PAGE Lightspeed ReStore transaction
    (contacts ⨝ participants ⨝ threads) — not the worker MAW body store.
    Profile faces are staged into the blob store (`image` + `mimeType`).
    """
    raw = await _eval(f"""
    const status = dbStatus();
    if (typeof REQ !== 'function') return {{ __error: 'not_ready', status }};
    let raw;
    try {{ raw = await rawRows('threads'); }}
    catch (e) {{ return {{ __error: 'not_ready', status, what: String(e) }}; }}
    const out = [];
    for (const row of raw) {{
      const d = decryptRow(row, 'threads'); if (!d) continue;
      const c = mapThreadRow(d); if (c && c.id) out.push(c);
    }}
    out.sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0));
    return out.slice(0, {int(limit)});
    """)
    if not isinstance(raw, list):
        return raw
    idx = await _contact_index()
    enriched = _enrich_threads(raw, idx) if idx else raw
    return await _stage_conversation_faces(enriched)


# A11y label Messenger stamps on each bubble when a thread is open on the page:
# "Enter, Message sent June 22, 2026, 11:10 AM by You: Hello" (or "by David: …").
_MSG_A11Y = re.compile(
    r"Message sent\s+(.+?)\s+by\s+([^:]+):\s*(.*)\s*$",
    re.DOTALL,
)
_MSG_TS_FMT = "%B %d, %Y, %I:%M %p"


def _has_real_messages(rows) -> bool:
    """True when EAR returned at least one user-visible body (not Admin/empty)."""
    if not isinstance(rows, list):
        return False
    for m in rows:
        if not isinstance(m, dict):
            continue
        if (m.get("content") or "").strip():
            return True
        # Non-system rows with a real timestamp count even without text (media).
        if m.get("published") and m.get("type") not in ("system", None):
            return True
    return False


def _parse_msg_a11y_ts(when: str):
    """Parse Messenger's a11y timestamp → ISO, or None if relative/unparseable."""
    s = (when or "").replace("\u202f", " ").replace("\xa0", " ").strip()
    # Normalize "5:08AM" → "5:08 AM"
    s = re.sub(r"(\d)([AP]M)\b", r"\1 \2", s, flags=re.I)
    try:
        dt = datetime.strptime(s, _MSG_TS_FMT).replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", ".000Z")
    except ValueError:
        return None


def _messages_from_snapshot(tree, *, conversation_id: str, me: str, limit: int):
    """Map an open-thread accessibility tree → newest-first `message` entities."""
    if not isinstance(tree, list):
        return []
    out = []
    seen = set()
    for el in tree:
        if not isinstance(el, dict):
            continue
        name = str(el.get("name") or "")
        m = _MSG_A11Y.search(name)
        if not m:
            continue
        when, who, text = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if not text:
            continue
        published = _parse_msg_a11y_ts(when)
        # Stable-enough id for the Messaging UI (dedupe / react gate); not a
        # wire mid — cold classic threads never expose one on the a11y tree.
        dig = sha1(f"{conversation_id}|{published}|{who}|{text}".encode()).hexdigest()[:16]
        mid = f"a11y:{conversation_id}:{dig}"
        if mid in seen:
            continue
        seen.add(mid)
        is_out = who.lower() in ("you", "me") or (me and who == me)
        row = {
            "id": mid,
            "content": text,
            "published": published,
            "conversationId": conversation_id,
            "type": "text",
            "isOutgoing": is_out,
        }
        if is_out:
            row["author"] = "Me"
        else:
            row["from"] = {"platform": "messenger", "id": conversation_id}
            if who:
                row["author"] = who
        out.append(row)
    out.sort(key=lambda r: r.get("published") or "", reverse=True)
    return out[: int(limit)]


async def _hydrate_messages_from_page(conversation_id: str, *, limit: int, me: str):
    """Cold-thread fallback: open the classic thread URL and read plaintext
    bubbles from the page accessibility tree.

    Why: the worker EAR store only keeps messages for threads the E2EE client
    has hydrated. Older/cutover threads list with a snippet + newestMsgTs but
    only an Admin row in `messages` — Messaging then shows a blank pane.
    Opening `/messages/t/<threadKey>/` makes Meta render the history; the
    a11y tree carries plaintext ("Message sent … by X: …"). Full navigate can
    respawn the shared worker — acceptable for cold reads; hot threads stay
    on the EAR path when it has real bodies.
    """
    cid = str(conversation_id).split("@")[0]
    await _ensure_messages()
    nav = await browser_session.navigate(
        _PAGE, f"https://www.facebook.com/messages/t/{cid}/", timeout=45)
    # Refuse a stale tree from a prior thread (pushState-style URL lies).
    href = ""
    if isinstance(nav, dict):
        href = str((nav.get("snapshot") or {}).get("url") or nav.get("url") or "")
    if cid not in href:
        where = await browser_session.eval(
            _PAGE, "location.pathname + location.hash", timeout=10)
        href = str(where or "")
    if cid not in href:
        return []
    tree = []
    if isinstance(nav, dict):
        tree = (nav.get("snapshot") or {}).get("tree") or []
    if not tree:
        snap = await browser_session.snapshot(_PAGE, timeout=30)
        if isinstance(snap, dict):
            tree = snap.get("tree") or []
            href2 = str(snap.get("url") or "")
            if cid not in href2 and cid not in href:
                return []
    return _messages_from_snapshot(
        tree, conversation_id=cid, me=me or "", limit=limit)


@returns("message[]")
@provides("chats", account_param="account")
@timeout(120)
async def list_messages(*, conversation_id=None, limit=100, **params):
    """List Messenger messages, optionally scoped to one thread.

    Hot path: worker EAR decrypt (newest-first, WhatsApp/IG contract).
    Cold path (scoped thread only): when EAR is empty or Admin-only, open the
    classic thread on the page and parse the accessibility tree — older chats
    keep history there until E2EE hydrates the worker store.
    """
    me = await _viewer_fbid()
    # conversation_id is the numeric threadJid part; the store keys by '<id>@msgr'.
    tjid = "null"
    cid = None
    if conversation_id is not None:
        cid = str(conversation_id).split("@")[0]
        tjid = json.dumps(f"{cid}@msgr")
    raw = await _worker_eval(f"""
    const status = dbStatus();
    if (typeof REQ !== 'function') return {{ __error: 'not_ready', status }};
    let raw;
    try {{ raw = await rawRows('messages', {tjid}); }}
    catch (e) {{ return {{ __error: 'not_ready', status, what: String(e) }}; }}
    const out = [];
    for (const row of raw) {{
      const d = decryptRow(row, 'messages'); if (!d) continue;
      const e = mapMsgRow(d); if (e) out.push(e);
    }}
    out.sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0));
    return out.slice(0, {int(limit)});
    """, me=me, timeout_s=90)
    if cid:
        cold = isinstance(raw, list) and not _has_real_messages(raw)
        not_ready = isinstance(raw, dict) and raw.get("__error") == "not_ready"
        if cold or not_ready:
            page = await _hydrate_messages_from_page(cid, limit=int(limit), me=me)
            if page:
                return page
    return _translate(raw) if not isinstance(raw, list) else raw


@returns("message")
@provides("get_message", account_param="account")
@timeout(90)
async def get_message(*, id, conversation_id=None, **params):
    """Get one message by id from the worker store.

    Declared as a brokered capability (`@provides("get_message")`) like WhatsApp/
    IG so the Messaging app gates inbound-media rendering on the provider.
    TODO(verify): media hydration — Messenger attachments are E2EE, so bytes
    need the client's own decrypt path (not a plain CDN fetch like IG); v1
    returns the text/metadata entity without `attaches`.
    """
    me = await _viewer_fbid()
    tjid = "null"
    if conversation_id is not None:
        cid = str(conversation_id)
        tjid = json.dumps(cid if "@" in cid else f"{cid}@msgr")
    return await _eval(f"""
    const status = dbStatus();
    if (typeof REQ !== 'function') return {{ __error: 'not_ready', status }};
    const MID = {json.dumps(str(id))};
    let raw;
    try {{ raw = await rawRows('messages', {tjid}); }}
    catch (e) {{ return {{ __error: 'not_ready', status, what: String(e) }}; }}
    for (const row of raw) {{
      if (str(row.msgId) !== MID) continue;         // msgId is a plaintext index column
      const d = decryptRow(row, 'messages'); if (!d) break;
      return mapMsgRow(d);
    }}
    return {{ __error: 'not_found', what: 'message', ref: MID }};
    """, me=me, timeout_s=60)


# ──────────────────────────────────────────────────────────────────────
# Watch — the cachedModules hook (the core; the proven-safe read+watch path)
# ──────────────────────────────────────────────────────────────────────
# Discipline (same as WhatsApp/IG): install-once BY CONTROL FLOW (self flag),
# own the seen-set IN the hook, emit one shape-native `message` per new record
# via a marker console.log. The engine routes marker lines through
# route_console_event → live_entity::write → the Messaging app. Re-armed on
# every worker RESPAWN by the engine's reconnect loop (install_hook re-runs).
_WATCH_HOOK = r"""
(function () {
  if (self.__agentos_msgr_watch__) return;
  self.__agentos_msgr_watch__ = true;
  const ME = %(me)s;
  const MARKER = %(marker)s;
  %(helpers)s
  const armedAt = Date.now();
  const seen = new Set();

  const emit = (entity) => {
    if (!entity || !entity.id || seen.has(entity.id)) return;
    // Backfill / resync re-inserts history (old timestamp) — not a live
    // arrival. 30s slack absorbs clock skew. Deep history is list_messages.
    if (entity.__ts != null && entity.__ts < armedAt - 30000) { seen.add(entity.id); return; }
    seen.add(entity.id);
    delete entity.__ts;
    entity.__shape__ = 'message';
    console.log(MARKER + JSON.stringify(entity));
  };

  const arm = () => {
    try {
      const d = req('LSDynamicDependencies');
      const cm = d && d.cachedModules;
      if (!cm || !cm.upsertMessage) return false;
      const wrap = (name, handler) => {
        const orig = cm[name];
        if (!orig || orig.__agentosWrapped) return;
        const w = function () {
          try { handler(Array.prototype.slice.call(arguments)); } catch (e) {}
          return orig.apply(this, arguments);
        };
        w.__agentosWrapped = true;
        cm[name] = w;
      };
      // Prime the seen-set with messages already in the store WITHOUT emitting,
      // so a warm arm doesn't replay history. (Cheap read-only best-effort.)
      wrap('upsertMessage', (a) => emit(mapMsgArgs(a)));
      return true;
    } catch (e) { return false; }
  };

  (async () => {
    for (let i = 0; i < 240; i++) { if (arm()) return; await new Promise((r) => setTimeout(r, 500)); }
  })();
})()
"""


@returns({"watching": "boolean", "stream": "string"})
@provides("message_watch", account_param="account")
@timeout(60)
async def watch(**params):
    """Stream new Messenger DMs into the graph in real time.

    Arms a durable hook on the shared worker's `cachedModules.upsertMessage`
    (the stable dynamic-dispatch sproc map — no ref-capture, no birth-inject).
    Each decrypted inbound/outbound message lands as a `message` entity the
    instant Meta's client writes it. Survives worker respawns (the engine
    re-arms via Target auto-attach's reconnect loop) and engine restarts (re-
    dispatched from the durable subscription node). Arm once. Idempotent.
    """
    await _ensure_messages()
    me = await _viewer_fbid()
    hook = _WATCH_HOOK % {
        "me": json.dumps(me),
        "marker": json.dumps(_WATCH_MARKER),
        "helpers": _HELPERS,
    }
    await services.call("browser_session", verb="subscribe", params={
        "target": _WORKER,
        "mode": _BG,   # subscribe has no SDK helper; raw layer defaults to attach
        "js": hook,
        "marker": _WATCH_MARKER,
        "subscriber": "messenger",
        "op": "watch",
    })
    return {"watching": True, "stream": "message"}


# ──────────────────────────────────────────────────────────────────────
# Send — composer-UI drive on the PAGE (the safest path; zero API surface)
# ──────────────────────────────────────────────────────────────────────

@returns("message")
@provides("message_send", account_param="account")
@timeout(120)
async def send_message(*, to, text, **params):
    """Send a Messenger DM to a thread; returns the sent message entity.

    Composer-UI drive (same discipline as IG): opens the thread on the PAGE,
    types into Meta's own message box, presses Enter — the client does the E2EE
    send. Receipt is read back from the worker store (the outgoing message
    landing is the truth), never a bare keypress-ok.

    Args:
        to: threadKey (the numeric conversationId from list_conversations).
        text: message text to send.
    """
    jar = await _read_cookies()
    if _SESSION_COOKIE not in jar:
        return browser_session.needs_auth(
            "Messenger is logged out (no xs cookie). Run messenger.login.",
            login_url=_LOGIN_URL,
        )
    me = (jar.get(_VIEWER_COOKIE) or {}).get("value") or ""

    # 1. Open the thread on the page. threadKey works directly as the URL id.
    nav = await browser_session.navigate(
        _PAGE, f"https://www.facebook.com/messages/e2ee/t/{to}/", timeout=30)
    # 2. Find Meta's composer (a role:textbox) in the returned snapshot.
    tree = nav.get("snapshot", {}).get("tree", []) if isinstance(nav, dict) else []
    box = next((el for el in tree
                if el.get("role") == "textbox" and el.get("ref")), None)
    if not box:
        return app_error(
            f"No message composer opened for thread {to} — is `to` a valid "
            "threadKey (the numeric conversationId from list_conversations)?",
            code="NotFound",
        )
    # 3. Type + Enter. Meta's own composer handles the E2EE send on Enter.
    #    (type/key have no SDK helper → pass the bg mode explicitly.)
    await services.call("browser_session", verb="type", params={
        "target": _PAGE, "mode": _BG, "ref": box["ref"], "text": text, "clear": True,
    })
    await services.call("browser_session", verb="key", params={
        "target": _PAGE, "mode": _BG, "keys": "Enter",
    })
    # 4. Receipt: poll the worker store for our outgoing message (best-effort).
    to_jid = str(to) if "@" in str(to) else f"{to}@msgr"
    receipt = await _worker_eval(f"""
    if (typeof REQ !== 'function') return null;
    const want = {json.dumps(text)}.trim();
    let raw;
    try {{ raw = await rawRows('messages', {json.dumps(to_jid)}); }} catch (e) {{ return null; }}
    let best = null, bestTs = -1;
    for (const row of raw) {{
      const d = decryptRow(row, 'messages'); if (!d) continue;
      const c = (d.msgContent && d.msgContent.content) || '';
      if (str(c).trim() !== want) continue;
      const ts = num(d.ts) || 0;
      if (ts > bestTs) {{ bestTs = ts; best = d; }}
    }}
    if (!best) return null;
    const e = mapMsgRow(best); if (e) {{ e.isOutgoing = true; e.author = 'Me'; }}
    return e;
    """, me=me, timeout_s=30)

    if isinstance(receipt, dict) and receipt.get("id"):
        return receipt
    # Store not readable (migrating) or receipt not yet landed. The send WAS
    # typed + Entered (the client does the E2EE send on Enter), but we can't
    # confirm it landed — return a minimal, honest receipt (no fabricated id /
    # timestamp), never a success-shaped entity we didn't verify.
    return {
        "content": text, "conversationId": str(to),
        "isOutgoing": True, "author": "Me",
    }


@provides("message_react", account_param="account")
@returns({"status": "string", "emoji": "string", "messageId": "string",
          "conversationId": "string"})
@timeout(90)
async def send_reaction(*, message_id, emoji="👍", conversation_id=None, remove=False, **params):
    """React to (or un-react from) a message with an emoji.

    TODO(verify): reactions ride an outbound **LS task** (`LSIssueNewTask` +
    `LSTaskSerializer*`, mautrix label 29/604), issued from the worker. The
    task shape must be RE'd against a live client-fired reaction before this is
    trusted (see operations.md "SEND / react"). Declared now so the Messaging
    app's reaction strip lights up on Messenger threads (brokered
    `message_react`); the wire is the next RE step.
    """
    return app_error(
        "messenger.send_reaction is not yet wired — the LS reaction task shape "
        "(LSIssueNewTask + serializer) still needs a live RE capture. See "
        "operations.md 'SEND / react'.",
        code="NotImplemented",
    )


@returns("conversation")
@provides("message_mark_read", account_param="account")
@timeout(60)
async def mark_read(*, conversation_id, **params):
    """Mark a thread read — clears its unread badge everywhere.

    TODO(verify): mark-read is an outbound **LS task** (mautrix label 21). The
    task shape needs a live RE capture before this is trusted. Declared now for
    the brokered `message_mark_read` capability; opening a thread in the UI also
    marks it read, which is the interim fallback if a caller needs it.
    """
    return app_error(
        "messenger.mark_read is not yet wired — the LS mark-read task shape "
        "still needs a live RE capture. See operations.md 'SEND / react / "
        "mark-read'.",
        code="NotImplemented",
    )
