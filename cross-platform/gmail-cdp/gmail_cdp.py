"""Gmail (Live) — read any signed-in Google account's mail via the browser.

Every op runs as one JS payload evaluated in the mail.google.com tab of the
human's daily browser, through the `browser_session` service (the engine holds
CDP; this app never sees the protocol). It is the WhatsApp/Outlook model — the
browser profile *is* the session, requests originate from the real browser, and
we never extract a cookie or a token. Unlike the `gmail` (OAuth) plugin, this
needs no OAuth client / Google Cloud project — only that the human is signed in
to Gmail in their browser, so it works for anyone who just installed AgentOS.

╔══════════════════════════════════════════════════════════════════════════╗
║  REVERSE-ENGINEERING PLAYBOOK — how this connector reaches into Gmail      ║
║  (the ids/positions rot, the method doesn't; keep this current.)          ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║  Gmail's data transport is its internal /sync/u/N/i/{bv,fd,s} API — a      ║
║  POSITIONAL-ARRAY protocol POSTed with a per-account X-Framework-Xsrf-     ║
║  Token + X-Gmail-BTAI state headers.                                       ║
║                                                                            ║
║  READS: hand-building a `bv` (LIST) body 400s/500s — the body carries      ║
║  per-view cursor state we can't forge. So — like InboxSDK / Streak /       ║
║  Gmail.js — we let GMAIL issue its own request and INTERCEPT the response: ║
║  navigating to a search view (`#search/<query>`) forces a fresh `bv`; an   ║
║  injected XHR hook captures the response text.                            ║
║                                                                            ║
║  WRITES: the `s` (ACTION) endpoint IS forgeable — an action body is just   ║
║  [thread id, [add, remove, msg ids]] (no per-view state), and the session  ║
║  state lives in the HEADERS, which we lift off Gmail's own live /sync/     ║
║  traffic (the XHR hook keeps the latest set). So label mutations           ║
║  (archive/trash/star/mark-read) POST a forged `s` "action 13" — see        ║
║  __modifyLabels + dev/requirements.md §2. gmonkey has NO mutation verb (it's     ║
║  read + compose only: getCurrentThread, createNewCompose→send). SEND rides ║
║  gmonkey; its lifecycle + the delivery gotcha are in dev/requirements.md §3.     ║
║                                                                            ║
║  Gmail is Closure/Wiz-compiled (no `webpackChunk`), so there's no module   ║
║  registry to grab its request fns the way outlook.py calls OWA's — and the ║
║  clients6 gmail/v1 frontend API is a CORS-walled calendar-only stub, NOT a ║
║  path to modify/send (dev/requirements.md §5). The same-origin /sync/ IS the way.║
║                                                                            ║
║  ACCOUNT MAP. `/mail/u/N/feed/atom` is a simple cookie-authed GET whose    ║
║  <title> is "…for <email>". The session is multiplexed, so fetching any    ║
║  /u/N/ works from any Google tab; a redirect to /u/0/ terminates the scan. ║
║  N in the path selects the account. (feed/atom = unread only — used ONLY   ║
║  for the map, never for mail data.)                                        ║
║                                                                            ║
║  bv RESPONSE LAYOUT (locked live). Thread stubs live somewhere in the      ║
║  nested arrays; we find them STRUCTURALLY (any array whose [3] is a        ║
║  "thread-f:" id and whose [0] is a string) rather than by a fixed path, so ║
║  Gmail reshuffling the container around them doesn't break us. A stub is:  ║
║    [ subject, snippet, dateMs, "thread-f:<id>", [ messages… ] ]            ║
║  and a message inside [4] carries the sender triple [_, email, name] and a ║
║  "^"-prefixed system-label array (^u unread, etc.) — both extracted by     ║
║  shape, not index.                                                        ║
║                                                                            ║
║  RE-DERIVE WHEN GMAIL SHIPS A BREAKING BUILD: capture a live bv response   ║
║  (drive the tab, XHR hook, read responseText), inspect the stub layout,    ║
║  adjust the by-shape extractors. Full method: the browser-driven +         ║
║  reverse-engineering system docs.                                          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import base64
import email as emaillib
import json
import re
import tempfile
from pathlib import Path as _Path
from datetime import timezone
from email import policy

from lxml import html as lxml_html

from agentos import account, app_error, blobs, browser_session, client, connection, provides, returns, test, timeout
from agentos.identity import normalize_email

# A browser-driven connector rides no credential — the session is the daily
# browser profile. All ops bind @connection("none"). `domain=` is the platform
# identity namespace for session registration (not the browse target — that
# stays in browser_session `target=`). Do not use base_url here unless tools
# call client.* with relative URLs.
connection("none", domain="google.com")

_TARGET = "mail.google.com"

# Service-level mailbox vocabulary → the Gmail search query that scopes it.
# Search-driven because a search navigation is what reliably fires a fresh `bv`
# (the inbox is served from cache on boot, so we'd miss the network capture).
_MAILBOX_TO_QUERY = {
    "inbox": "in:inbox",
    "sent": "in:sent",
    "drafts": "in:drafts",
    "trash": "in:trash",
    "spam": "in:spam",
    "starred": "is:starred",
    "unread": "in:inbox is:unread",
    "all": "in:anywhere",
}

# Gmail's system labels are a stable, universal vocabulary (like Gmail's API
# label ids) — seeded so the inventory is complete even for the ones the web
# left-nav hides by default (Spam / Trash). User labels + categories are read
# live off the nav (see `list_labels`). (id, display name).
_SYSTEM_LABELS = [
    ("INBOX", "Inbox"), ("STARRED", "Starred"), ("SNOOZED", "Snoozed"),
    ("SENT", "Sent"), ("DRAFT", "Drafts"), ("IMPORTANT", "Important"),
    ("ALL", "All Mail"), ("SPAM", "Spam"), ("TRASH", "Trash"),
]


# ──────────────────────────────────────────────────────────────────────
# JS building blocks
# ──────────────────────────────────────────────────────────────────────

# Readiness + logged-out detection. A signed-out session redirects to
# accounts.google.com; a freshly-opened/attached tab may still be booting
# Gmail's app. We branch on where the tab lands, then wait for Gmail's runtime.
_READY = """
const __deadline = Date.now() + %(wait_ms)d;
while (true) {
  const h = location.hostname, p = location.pathname;
  if (h.indexOf('accounts.google.com') !== -1 || p.indexOf('/ServiceLogin') !== -1
      || p.indexOf('/signin') !== -1) {
    return { __error: 'auth_required' };
  }
  // gmonkey / GM_APP_NAME present IS the readiness signal — Gmail's app JS has
  // booted. Don't also require readyState 'complete': a headless Gmail tab often
  // sits at 'interactive' indefinitely (a long-poll / pending sub-resource never
  // settles), which would hang EVERY op though the app is fully usable. Accept
  // anything past 'loading'.
  if (h.indexOf('mail.google.com') !== -1 && document.readyState !== 'loading'
      && (window.GM_APP_NAME || window.gmonkey)) break;
  if (Date.now() > __deadline) return { __error: 'tab_not_ready' };
  await new Promise(r => setTimeout(r, 200));
}
"""

# Shared library: the XHR interceptor (idempotent), the account enumerator, and
# the by-shape stub mapper. Everything hangs off `window.__agmail`.
_LIB = r"""
const __g = (window.__agmail = window.__agmail || {});
__g.bv = __g.bv || [];
__g.fd = __g.fd || [];
__g.actions = __g.actions || [];
__g.settingsActions = __g.settingsActions || [];
if (!__g.hooked || __g.hooked < 2) {
  __g.hooked = 2;
  __g.bv = [];   // captured browse-view (list) response texts
  __g.fd = [];   // captured fetch-data (bodies) response texts
  __g.actions = []; // captured /sync/i/s action POSTs: {body, ts, status} — draft-saves + sends
  __g.settingsActions = []; // captured /sync/st/s POSTs — filters/labels/vacation settings writes
  __g.hdrs = null; // latest /sync/ POST request headers — reused to forge an authed mutation
  const oOpen = XMLHttpRequest.prototype.open, oSend = XMLHttpRequest.prototype.send,
        oSet = XMLHttpRequest.prototype.setRequestHeader;
  // Avoid double-wrapping if a prior hooked=1 wrap is already on the prototype —
  // still install st/s capture by chaining through the current send.
  XMLHttpRequest.prototype.open = function (m, u, ...r) { this.__u = u; this.__h = {}; return oOpen.call(this, m, u, ...r); };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) { try { this.__h[k] = v; } catch (e) {} return oSet.call(this, k, v); };
  XMLHttpRequest.prototype.send = function (b) {
    const x = this, u = String(this.__u || '');
    if (u.indexOf('/i/bv') !== -1 || u.indexOf('/i/fd') !== -1) {
      const bucket = u.indexOf('/i/bv') !== -1 ? __g.bv : __g.fd;
      this.addEventListener('load', function () { try { bucket.push(x.responseText); } catch (e) {} });
    }
    // The /sync/i/s ACTION endpoint carries every draft-save + send POST gmonkey
    // fires. Capture body + status + ts so send_email can gate deterministically:
    // the draft AUTOSAVE (a save POST whose body carries the subject — a fresh
    // draft has no server id, so send() before it silently no-ops) and the
    // post-send 200 (the on-the-wire delivery proof) — never a blind sleep.
    // Filter create/delete ALSO emits an i/s action (codes 2=upsert, 1=delete) —
    // but the durable settings write is /sync/st/s (opaque token; see dev/requirements.md §9).
    if (/\/i\/s(\?|$)/.test(u)) {
      const rec = { body: (typeof b === 'string' ? b : ''), ts: Date.now(), status: null };
      __g.actions.push(rec);
      this.addEventListener('load', function () { try { rec.status = x.status; } catch (e) {} });
    }
    if (/\/st\/s(\?|$)/.test(u)) {
      const rec = { body: (typeof b === 'string' ? b : ''), ts: Date.now(), status: null };
      __g.settingsActions.push(rec);
      this.addEventListener('load', function () { try { rec.status = x.status; } catch (e) {} });
    }
    // Every /sync/ POST carries the per-session action headers (X-Framework-Xsrf-
    // Token + X-Gmail-BTAI state). Gmail fires these constantly (bv/fd on every
    // navigation); we keep the latest set so a forged label mutation rides
    // Gmail's OWN auth. The state a hand-built request can't reconstruct lives in
    // these HEADERS — the action body itself (thread + labels) is small + forgeable.
    if (u.indexOf('/sync/') !== -1 && this.__h && this.__h['X-Framework-Xsrf-Token']) { __g.hdrs = this.__h; }
    return oSend.call(this, b);
  };
}

// Enumerate signed-in accounts via the per-account atom feed title. Same-origin,
// multiplexed session, so any /u/N/ is readable from here; a redirect (or an
// index resolving to a different /u/) terminates the scan.
async function __enumAccounts() {
  const out = [], seen = new Set();
  for (let n = 0; n < 8; n++) {
    try {
      const r = await fetch('/mail/u/' + n + '/feed/atom', { credentials: 'include' });
      if (!r.ok) break;
      const landed = (r.url.match(/\/u\/(\d+)\//) || [])[1];
      if (r.redirected || String(landed) !== String(n)) break;
      const txt = await r.text();
      const email = (txt.match(/for ([^<]+@[^<]+)</) || [])[1] || null;
      if (!email) break;
      // A single-account profile does NOT redirect /u/1/ → it re-serves /u/0/'s
      // account, so the same email reappears at every index. The first repeat is
      // the real end of the account list (without this, one account maps to 8).
      const key = email.toLowerCase();
      if (seen.has(key)) break;
      seen.add(key);
      const unread = (txt.match(/<fullcount>(\d+)<\/fullcount>/) || [])[1];
      out.push({ index: n, email: email, unread: unread ? parseInt(unread, 10) : 0 });
    } catch (e) { break; }
  }
  return out;
}

// Hex helpers — view=om needs the MESSAGE legacy hex (msg stub index 55), NOT
// the thread hex (stub index 19). On received single-msg threads they often
// match; on fresh self-sents they diverge and th=<threadHex> 200s "does not
// exist". Attachment URLs also carry th=<messageHex>.
function __isHex(s) {
  return typeof s === 'string' && /^[0-9a-f]{14,18}$/i.test(s);
}
function __msgHex(msg) {
  if (Array.isArray(msg) && __isHex(msg[55])) return String(msg[55]).toLowerCase();
  const m = JSON.stringify(msg || []).match(/[?&]th=([0-9a-f]{14,18})/i);
  return m ? m[1].toLowerCase() : null;
}
function __msgAtts(msg) {
  // msg[11] = [[mime, filename, size, partId, null, view=att url, …], …]
  const atts = Array.isArray(msg) && Array.isArray(msg[11]) ? msg[11] : [];
  const out = [];
  for (const a of atts) {
    if (!Array.isArray(a) || typeof a[1] !== 'string') continue;
    if (typeof a[0] === 'string' && a[0].indexOf('/') !== -1) {
      out.push({ mimeType: a[0], filename: a[1], size: a[2], partId: a[3],
                 url: (typeof a[5] === 'string' ? a[5] : null) });
    }
  }
  return out;
}

// Map Gmail's bv thread stubs → the `email` shape (keys mirror gmail.py so the
// Mail app renders both identically). Stubs are found BY SHAPE, fields by shape.
function __mapStubs(parsed, smtp) {
  const stubs = [];
  const find = (n, d) => {
    if (d > 10 || !Array.isArray(n)) return;
    // Gmail hands recently-synced threads as the perm form (`thread-a:`) and
    // older ones as the legacy form (`thread-f:`), in disjoint sections of the
    // same bv — match BOTH so the list is complete (either id form round-trips
    // through the mutation verbs, which key off whatever the stub carries).
    if (typeof n[0] === 'string' && typeof n[3] === 'string' && /^thread-[af]:/.test(n[3])) { stubs.push(n); return; }
    for (const e of n) find(e, d + 1);
  };
  find(parsed, 0);
  return stubs.map((t) => {
    const msgs = Array.isArray(t[4]) ? t[4] : [];
    const first = msgs[0] || [];
    let from = null, labels = [];
    for (const el of first) {
      if (Array.isArray(el) && el.length >= 3 && typeof el[1] === 'string' && el[1].indexOf('@') !== -1 && !from) {
        from = { handle: el[1], platform: 'email', displayName: (typeof el[2] === 'string' ? el[2] : null) || null };
      }
      if (Array.isArray(el) && el.length && typeof el[0] === 'string' && el[0][0] === '^' && !labels.length) {
        labels = el;
      }
    }
    const ts = typeof t[2] === 'number' ? t[2] : 0;
    const threadHex = __isHex(t[19]) ? String(t[19]).toLowerCase() : null;
    // Newest message hex wins — that's what view=om / get_attachment need.
    let messageHex = null, hasAtt = false;
    for (const m of msgs) {
      const h = __msgHex(m);
      if (h) messageHex = h;
      if (__msgAtts(m).length) hasAtt = true;
    }
    const out = {
      id: t[3],
      name: t[0] || '(no subject)',
      content: t[1] || '',
      content_mime: 'text/plain',
      published: ts ? new Date(ts).toISOString() : null,
      isUnread: labels.indexOf('^u') !== -1,
      isStarred: labels.indexOf('^t') !== -1,
      hasAttachments: hasAtt,
      from: from,
      conversationId: t[3],
      messageCount: msgs.length,
      __ts: ts,
    };
    if (threadHex) out.legacyThreadId = threadHex;
    if (messageHex) { out.messageHex = messageHex; out._omToken = messageHex; }
    if (smtp) out.accountEmail = smtp;
    return out;
  });
}

// Resolve a caller id (thread-a / thread-f / thread-hex / message-hex) → the
// MESSAGE legacy hex view=om actually serves. Prefer bv stub; fall back to
// opening the conversation and reading data-legacy-message-id.
__g.resolveOmToken = async function (token) {
  if (!token || typeof token !== 'string') return { __error: 'bad_token' };
  const want = token.replace(/^#/, '');
  const fromBv = () => {
    for (const txt of (__g.bv || [])) {
      let p; try { p = JSON.parse(txt); } catch (e) { continue; }
      const stubs = [];
      const find = (n, d) => {
        if (d > 12 || !Array.isArray(n)) return;
        if (typeof n[0] === 'string' && typeof n[3] === 'string' && /^thread-[af]:/.test(n[3])) { stubs.push(n); return; }
        for (const e of n) find(e, d + 1);
      };
      find(p, 0);
      for (const t of stubs) {
        const th = __isHex(t[19]) ? String(t[19]).toLowerCase() : null;
        const tid = t[3];
        let last = null, all = [];
        for (const m of (t[4] || [])) {
          const h = __msgHex(m);
          if (h) { last = h; all.push(h); }
        }
        const hit = (want === tid) ||
          (__isHex(want) && th === want.toLowerCase()) ||
          (__isHex(want) && all.indexOf(want.toLowerCase()) !== -1);
        if (hit && last) return { omToken: last, threadHex: th, threadId: tid, all: all };
      }
    }
    return null;
  };
  let hit = fromBv();
  if (hit) return hit;
  // Seed a bv by searching (hex) or in:anywhere (thread-a), then retry.
  if (want.indexOf('thread-') === 0 || __isHex(want)) {
    location.hash = '#search/' + encodeURIComponent(__isHex(want) ? want : 'in:anywhere');
    const dl = Date.now() + 8000;
    while (Date.now() < dl) {
      await new Promise((r) => setTimeout(r, 200));
      hit = fromBv();
      if (hit) return hit;
      if (want.indexOf('thread-') === 0) break;
    }
  }
  // DOM path: open the conversation, read message hexes off the rendered view.
  let kind = 'bad', openTok = want;
  if (__isHex(want)) { kind = 'hex'; openTok = want.toLowerCase(); }
  else if (want.indexOf('thread-a:') === 0 || want.indexOf('thread-f:') === 0) { kind = 'thread'; openTok = want; }
  if (kind === 'bad') return { __error: 'bad_token', token: want };
  await __g.openThread(kind, openTok);
  const ddl = Date.now() + 10000;
  let ids = [];
  while (Date.now() < ddl) {
    ids = [...document.querySelectorAll('[data-legacy-message-id]')]
      .map((el) => el.getAttribute('data-legacy-message-id'))
      .filter(__isHex)
      .map((h) => h.toLowerCase());
    if (ids.length) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  if (!ids.length) return { __error: 'no_message_hex', token: want };
  const row = document.querySelector('[data-legacy-thread-id]');
  return { omToken: ids[ids.length - 1], fromDom: true, all: ids,
           threadHex: row ? row.getAttribute('data-legacy-thread-id') : null };
};

// Forge a label mutation (archive / trash / star / mark-read) as Gmail's own
// /sync/i/s "action 13" — the ONE write the sync protocol takes that a hand-
// built request can. A LIST body carries per-view state we can't forge (it
// 400s); an ACTION body is just [thread id, [add, remove, msg ids]], and the
// per-session auth rides the reused headers (`__g.hdrs`). Both ids are the
// LEGACY forms (thread-f / msg-f) straight off the bv stub the thread was
// listed in — msg ids are REQUIRED (an empty set no-ops server-side). The
// response echoes the account's per-label counts, which we parse to confirm.
async function __modifyLabels(idx, threadId, add, remove) {
  const H = __g.hdrs;
  if (!H || !H['X-Framework-Xsrf-Token']) return { __error: 'no_sync_headers' };
  let mids = [];
  const find = (n, d) => {
    if (mids.length || d > 13 || !Array.isArray(n)) return;
    if (n[3] === threadId && Array.isArray(n[4])) {
      for (const m of n[4]) { if (Array.isArray(m) && typeof m[0] === 'string' && m[0].indexOf('msg-') === 0) mids.push(m[0]); }
      return;
    }
    for (const e of n) find(e, d + 1);
  };
  for (const txt of __g.bv) { try { find(JSON.parse(txt), 0); } catch (e) {} if (mids.length) break; }
  if (!mids.length) return { __error: 'no_thread_msgs' };
  const c = Date.now() % 100000;
  const body = JSON.stringify([null, [[[13, [threadId, [null, null, null, null, null, null, [add, remove, mids]]]]]],
      [1, c, null, null, [null, 0], null, 1], [Date.now(), 1, Date.now(), 0, 35], 2]);
  let r;
  try { r = await fetch('/sync/u/' + idx + '/i/s?hl=en&c=' + c + '&rt=r&pt=ji', { method: 'POST', credentials: 'include', headers: H, body: body }); }
  catch (e) { return { __error: 'action_failed', what: String(e).slice(-80) }; }
  const txt = await r.text();
  if (r.status !== 200) return { __error: 'action_failed', status: r.status };
  // The response's per-label count summary (`["^i",unread,total]`) — proof the
  // action reached storage, and the fresh state we hand back to the caller.
  const labels = {};
  for (const s of (txt.match(/\["\^[\w]+",\d+,\d+\]/g) || [])) { try { const p = JSON.parse(s); labels[p[0]] = { unread: p[1], total: p[2] }; } catch (e) {} }
  return { applied: true, msgIds: mids, labels: labels };
}

// ── Persistent reply surface (window.__agmail) ───────────────────────
// Survives across evals. Python calls thin wrappers; RE agents call
// __agmail.reply / .openThread / .wrapJln live without re-pasting the
// proven flow. NO // mid-minified single-liners outside this multi-line lib.
__g.wrapJln = function () {
  if (typeof _m === 'undefined' || typeof _m.jLn !== 'function') return { ok: false, err: 'no_jln' };
  const base = (_m.jLn && _m.jLn.__orig) || _m.jLn;
  const W = function (...a) {
    (window.__agmCtrl = window.__agmCtrl || []).push(this);
    return base.apply(this, a);
  };
  W.prototype = base.prototype;
  Object.setPrototypeOf(W, base);
  for (const k of Object.getOwnPropertyNames(base)) {
    if (!['length', 'name', 'prototype'].includes(k)) try { W[k] = base[k]; } catch (e) {}
  }
  W.__agmCap = true;
  W.__orig = base;
  _m.jLn = W;
  window.__agmCtrl = window.__agmCtrl || [];
  return { ok: true, n: window.__agmCtrl.length };
};

__g.msgViews = function () {
  return (window.__agmCtrl || []).filter(
    (c) => typeof c.Za === 'function' && c.Ca !== undefined && c.ha !== undefined
  );
};

__g.openThread = async function (kind, token) {
  __g.wrapJln();
  window.__agmCtrl = [];
  if (kind === 'perm') {
    location.hash = '#inbox/' + token;
  } else if (kind === 'hex' || kind === 'thread') {
    location.hash = (kind === 'hex') ? ('#search/' + token) : '#search/in:anywhere';
    let rdl = Date.now() + 10000, row = null;
    while (Date.now() < rdl) {
      if (kind === 'hex') {
        row = document.querySelector('[data-legacy-thread-id="' + token + '"]');
      } else {
        row = document.querySelector('[data-thread-id="#' + token + '"]')
          || document.querySelector('[data-thread-id="' + token + '"]');
      }
      if (row) break;
      await new Promise((r) => setTimeout(r, 200));
    }
    if (!row) return { status: 'no_row', token: token };
    window.__agmCtrl = [];
    row.click();
  } else {
    return { status: 'bad_open', kind: kind };
  }
  let ctrl = null;
  let odl = Date.now() + 9000;
  while (Date.now() < odl) {
    await new Promise((r) => setTimeout(r, 200));
    const mvs = __g.msgViews();
    if (mvs.length) { ctrl = mvs[mvs.length - 1]; break; }
  }
  if (!ctrl) {
    const h = location.hash;
    if (h && h.indexOf('/') !== -1) {
      location.hash = '#inbox';
      await new Promise((r) => setTimeout(r, 500));
      window.__agmCtrl = [];
      location.hash = h;
      odl = Date.now() + 8000;
      while (Date.now() < odl) {
        await new Promise((r) => setTimeout(r, 200));
        const mvs = __g.msgViews();
        if (mvs.length) { ctrl = mvs[mvs.length - 1]; break; }
      }
    }
  }
  if (!ctrl) return { status: 'no_ctrl', hash: location.hash, n: (window.__agmCtrl || []).length };
  return { status: 'open', ctrl: ctrl, hash: location.hash, title: document.title };
};

__g.reply = async function (opts) {
  opts = opts || {};
  const mode = opts.mode || 'r';
  const html = opts.html || '';
  // CLEAN GATE — a leftover mole (e.g. from create_draft) steals setBody/send
  // from the reply EQn opens. Refuse so the caller can _reset_compose + retry.
  const gm0 = window.__agmGm || (window.__agmGm = await new Promise((res) => {
    try { window.gmonkey.load('2', res); } catch (e) { res(null); }
  }));
  if (!gm0) return { __error: 'no_gmonkey' };
  const nMoles = ((gm0.getMainWindow().getOpenDraftMessages && gm0.getMainWindow().getOpenDraftMessages()) || []).length;
  if (nMoles > 0) return { status: 'moles_open', count: nMoles };
  const open = await __g.openThread(opts.openKind, opts.openToken);
  if (open.status !== 'open') return open;
  const ctrl = open.ctrl;
  let EQn = _m.EQn;
  if (typeof _m.__agmEQnOrig === 'function' && _m.EQn !== _m.__agmEQnOrig) {
    EQn = _m.__agmEQnOrig;
    _m.EQn = _m.__agmEQnOrig;
    try { delete _m.__agmEQnOrig; } catch (e) { _m.__agmEQnOrig = undefined; }
  }
  if (typeof EQn !== 'function') return { status: 'no_eqn' };
  try { EQn.call(null, ctrl, mode); } catch (e) {
    return { status: 'eqn_throw', what: String(e).slice(0, 120) };
  }
  const gm = window.__agmGm || (window.__agmGm = await new Promise((res) => {
    try { window.gmonkey.load('2', res); } catch (e) { res(null); }
  }));
  if (!gm) return { __error: 'no_gmonkey' };
  const mw = gm.getMainWindow();
  let draft = null;
  const mdl = Date.now() + 8000;
  while (Date.now() < mdl) {
    await new Promise((r) => setTimeout(r, 200));
    const ds = (mw.getOpenDraftMessages && mw.getOpenDraftMessages()) || [];
    if (!ds.length) continue;
    // Prefer a reply/forward mole (Re:/Fwd:) over a stale plain draft.
    draft = ds.find((d) => {
      try {
        const s = (d.getSubject && d.getSubject()) || '';
        return /^(re|fwd|fw|aw|sv|antw)\s*:/i.test(s);
      } catch (e) { return false; }
    }) || ds[ds.length - 1];
    break;
  }
  if (!draft) return { status: 'no_mole' };
  if (opts.to) draft.setTo(opts.to);
  if (opts.cc) draft.setCc(opts.cc);
  if (opts.bcc) draft.setBcc(opts.bcc);
  if (opts.subject) draft.setSubject(opts.subject);
  const prev = (draft.getBody && draft.getBody()) || '';
  draft.setBody(html + prev);
  const needle = String((opts.subject) || html.replace(/<[^>]+>/g, '').slice(0, 40) || '').trim();
  const saveT0 = Date.now();
  let saveBody = null;
  const adl = Date.now() + 13000;
  while (Date.now() < adl) {
    await new Promise((r) => setTimeout(r, 300));
    const news = (__g.actions || []).filter((a) => a.ts >= saveT0);
    if (needle.length >= 2) {
      const hit = news.find((a) => a.body.indexOf(needle) !== -1);
      if (hit) { saveBody = hit.body; break; }
    } else if (news.length) { saveBody = news[0].body; break; }
  }
  if (saveBody === null) {
    const any = (__g.actions || []).filter((a) => a.ts >= saveT0);
    if (any.length) saveBody = any[any.length - 1].body;
  }
  if (saveBody === null) return { status: 'no_autosave' };
  const sendT0 = Date.now();
  try { draft.send(); } catch (e) {
    return { __error: 'send_failed', what: String(e).slice(0, 80) };
  }
  let confirmed = false, fired = false;
  const sdl = Date.now() + 10000;
  while (Date.now() < sdl) {
    await new Promise((r) => setTimeout(r, 300));
    const sn = (__g.actions || []).filter((a) => a.ts >= sendT0);
    if (sn.length) fired = true;
    if (sn.some((a) => a.status === 200)) { confirmed = true; break; }
  }
  const subj = draft.getSubject ? draft.getSubject() : null;
  const tos = draft.getToEmails ? draft.getToEmails() : null;
  if (confirmed) return { status: 'sent', subject: subj, to: tos, hash: location.hash };
  if (fired) return { status: 'sent_unconfirmed', subject: subj, to: tos, hash: location.hash };
  return { status: 'send_no_confirm' };
};

// Full gmonkey compose+send including optional attachments (File+DataTransfer).
// Proven live: chip appears, autosave carries subject, send 200s.
__g.composeSend = async function (opts) {
  opts = opts || {};
  const to = opts.to || '';
  const cc = opts.cc || '';
  const bcc = opts.bcc || '';
  const subject = opts.subject || '';
  const html = opts.html || '';
  const atts = opts.attachments || [];
  const gm = window.__agmGm || (window.__agmGm = await new Promise((res) => {
    try { window.gmonkey.load('2', res); } catch (e) { res(null); }
  }));
  if (!gm) return { __error: 'no_gmonkey' };
  const mw = gm.getMainWindow();
  const nMoles = ((mw.getOpenDraftMessages && mw.getOpenDraftMessages()) || []).length;
  if (nMoles > 0) return { status: 'moles_open', count: nMoles };
  let draft = mw.createNewCompose();
  const cdl = Date.now() + 6000;
  while ((!draft || typeof draft !== 'object' || typeof draft.setTo !== 'function') && Date.now() < cdl) {
    await new Promise((r) => setTimeout(r, 200));
    const ds = mw.getOpenDraftMessages && mw.getOpenDraftMessages();
    if (Array.isArray(ds) && ds.length) draft = ds[ds.length - 1];
  }
  if (!draft || typeof draft.setTo !== 'function') return { __error: 'no_draft' };
  await new Promise((r) => setTimeout(r, 400));
  draft.setTo(to);
  if (cc) draft.setCc(cc);
  if (bcc) draft.setBcc(bcc);
  draft.setSubject(subject);
  draft.setBody(html);
  await new Promise((r) => setTimeout(r, 500));
  const t0 = Date.now();
  if (atts.length) {
    const inps = [...document.querySelectorAll('input[type=file][name=Filedata]')];
    const inp = inps[inps.length - 1] || document.querySelector('input[type=file]');
    if (!inp) return { status: 'no_file_input' };
    const dt = new DataTransfer();
    for (const a of atts) {
      const bin = atob(a.content);
      const u8 = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
      dt.items.add(new File([u8], a.filename, { type: a.mimeType || 'application/octet-stream' }));
    }
    try {
      inp.files = dt.files;
      inp.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    } catch (e) {
      return { status: 'attach_failed', what: String(e).slice(0, 120) };
    }
    const adl = Date.now() + 18000;
    let have = false;
    while (Date.now() < adl) {
      await new Promise((r) => setTimeout(r, 300));
      const chip = document.body.innerText || '';
      if (/Uploading attachment/i.test(chip)) continue;
      if (atts.every((a) => chip.indexOf(a.filename) !== -1)) { have = true; break; }
    }
    if (!have) return { status: 'attach_not_confirmed', files: atts.map((a) => a.filename) };
    await new Promise((r) => setTimeout(r, 800));
  }
  const needle = String(subject || '').trim();
  const saveT0 = t0;
  let saveBody = null;
  const adl = Date.now() + 20000;
  while (Date.now() < adl) {
    await new Promise((r) => setTimeout(r, 300));
    const news = (__g.actions || []).filter((a) => a.ts >= saveT0);
    if (needle.length >= 2) {
      const h = news.find((a) => a.body.indexOf(needle) !== -1);
      if (h) { saveBody = h.body; break; }
    } else if (news.length) { saveBody = news[0].body; break; }
  }
  if (saveBody === null) {
    const any = (__g.actions || []).filter((a) => a.ts >= saveT0);
    if (any.length) saveBody = any[any.length - 1].body;
  }
  if (saveBody === null) return { status: 'no_autosave' };
  const firstTo = String(to || '').split(/[,;]/)[0].trim().toLowerCase();
  let recipOk = !firstTo || saveBody.toLowerCase().indexOf(firstTo) !== -1;
  if (!recipOk) {
    const te = (draft.getToEmails && draft.getToEmails()) || [];
    recipOk = te.length > 0;
  }
  if (!recipOk) return { __error: 'recipient_not_set' };
  const sendT0 = Date.now();
  try { draft.send(); } catch (e) {
    return { __error: 'send_failed', what: String(e).slice(0, 80) };
  }
  let confirmed = false, fired = false;
  const sdl = Date.now() + 12000;
  while (Date.now() < sdl) {
    await new Promise((r) => setTimeout(r, 300));
    const sn = (__g.actions || []).filter((a) => a.ts >= sendT0);
    if (sn.length) fired = true;
    if (sn.some((a) => a.status === 200)) { confirmed = true; break; }
  }
  if (confirmed) return { status: 'sent' };
  if (fired) return { status: 'sent_unconfirmed' };
  return { status: 'send_no_confirm' };
};

// ── Filters (settings UI — OAuth parity for list/create/delete) ───────
// Gmail Web keeps filters on #settings/filters. No forgeable sync action
// found yet; the table rows carry criteria/action text + edit/delete links
// whose id embeds the filter id (`:NN#z…*…`). list = scrape; create/delete
// drive the two-step "Create a new filter" dialog (criteria → actions).
__g.__filtSleep = (ms) => new Promise((r) => setTimeout(r, ms));
__g.__filtSet = function (el, val) {
  if (!el) return;
  const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  if (desc && desc.set) desc.set.call(el, val);
  else el.value = val;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
};
__g.__filtClickVisible = function (nodes, re, opts) {
  opts = opts || {};
  for (const el of nodes) {
    const t = ((el.textContent || el.value || '') + '').replace(/\s+/g, ' ').trim();
    if (!re.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (opts.requireEnabled && (el.disabled || el.getAttribute('aria-disabled') === 'true')) continue;
    el.click();
    return el;
  }
  return null;
};
__g.__filtFindCb = function (labelRe) {
  return [...document.querySelectorAll('input[type=checkbox]')].find((el) => {
    if (!(el.getBoundingClientRect().width > 0)) return false;
    const row = el.closest('tr') || el.closest('label') || el.parentElement;
    const lab = (row && row.innerText) || el.getAttribute('aria-label') || '';
    if (!labelRe.test(lab)) return false;
    return /Skip the Inbox|Mark as read|Star it|Apply the label|Forward it|Delete it|Never send|important|Categorize|Also apply filter/i.test(lab);
  });
};
__g.__filtCheck = function (labelRe, want) {
  if (!want) return false;
  const cb = __g.__filtFindCb(labelRe);
  if (!cb) return false;
  if (!cb.checked) cb.click();
  return !!cb.checked;
};
__g.__filtVerifyBlocking = function () {
  return /We need to verify it.s you to continue/i.test(document.body.innerText || '');
};
__g.__filtScrape = function () {
  const rows = [...document.querySelectorAll('table tr')].filter((tr) =>
    /Matches:/.test(tr.innerText || '')
  );
  const out = [];
  for (const tr of rows) {
    const txt = (tr.innerText || '').replace(/\s+/g, ' ').trim();
    const m = txt.match(/Matches:\s*(.*?)\s*Do this:\s*(.*?)(?:\s*edit|\s*delete|$)/i);
    const del = [...tr.querySelectorAll('a,button,[role=link]')].find((el) =>
      /^delete$/i.test((el.textContent || '').trim())
    );
    let id = null;
    if (del && del.id) {
      const im = del.id.match(/#(z[\w*]+\*?[\w*]*)/);
      id = im ? im[1] : del.id;
    }
    out.push({
      id: id,
      criteria: m ? m[1].trim() : null,
      action: m ? m[2].trim() : null,
      text: txt.slice(0, 240),
    });
  }
  return out;
};
__g.listFilters = async function () {
  location.hash = '#settings/filters';
  const dl = Date.now() + 10000;
  let rows = [];
  while (Date.now() < dl) {
    await __g.__filtSleep(200);
    rows = [...document.querySelectorAll('table tr')].filter((tr) =>
      /Matches:/.test(tr.innerText || '')
    );
    if (rows.length) break;
    if (/Filters/i.test(document.body.innerText || '')) break;
  }
  return __g.__filtScrape();
};

__g.createFilter = async function (opts) {
  opts = opts || {};
  const before = await __g.listFilters();
  __g.__filtClickVisible(document.querySelectorAll('button,[role=button]'), /^Cancel$/i);
  await __g.__filtSleep(200);
  const beforeIds = new Set(before.map((f) => f.id).filter(Boolean));
  const link = [...document.querySelectorAll('a,[role=link]')].find((el) =>
    /Create a new filter/i.test((el.textContent || '').trim())
  );
  if (!link) return { __error: 'no_create_link' };
  link.click();
  await __g.__filtSleep(1200);
  const field = (aria, cls) =>
    document.querySelector('input[aria-label="' + aria + '"]') ||
    (cls ? document.querySelector('input.' + cls) : null);
  if (opts.from) __g.__filtSet(field('From', 'aQa'), opts.from);
  if (opts.to) __g.__filtSet(field('To', 'aQf'), opts.to);
  if (opts.subject) __g.__filtSet(field('Subject', 'aQd'), opts.subject);
  if (opts.query) __g.__filtSet(field('Has the words', 'aQb'), opts.query);
  if (opts.doesntHave) __g.__filtSet(field("Doesn't have", 'aP9'), opts.doesntHave);
  if (opts.hasAttachment) __g.__filtCheck(/Has attachment/i, true);
  if (!opts.from && !opts.to && !opts.subject && !opts.query && !opts.hasAttachment && !opts.doesntHave) {
    __g.__filtClickVisible(document.querySelectorAll('button,[role=button]'), /^Cancel$/i);
    return { __error: 'no_criteria' };
  }
  await __g.__filtSleep(300);
  let step1 = null;
  for (let i = 0; i < 25; i++) {
    step1 = __g.__filtClickVisible(
      document.querySelectorAll('button,[role=button]'), /^Create filter$/i, { requireEnabled: true }
    );
    if (step1) break;
    await __g.__filtSleep(150);
  }
  if (!step1) return { __error: 'no_create_step1' };
  await __g.__filtSleep(1500);
  if (__g.__filtVerifyBlocking()) {
    return { __error: 'reauth_required', hint: 'Google verify-its-you dialog — open browser.login_window on the bg profile and complete Continue, then retry' };
  }
  const remove = (opts.removeLabels || []).map((x) => String(x).toUpperCase());
  const add = (opts.addLabels || []).map((x) => String(x));
  const addUp = add.map((x) => x.toUpperCase());
  __g.__filtCheck(/Skip the Inbox/i, !!(opts.skipInbox || remove.indexOf('INBOX') !== -1));
  __g.__filtCheck(/Mark as read/i, !!(opts.markRead || remove.indexOf('UNREAD') !== -1));
  __g.__filtCheck(/Star it/i, !!(opts.star || addUp.indexOf('STARRED') !== -1));
  __g.__filtCheck(/Delete it/i, !!(opts.deleteIt || addUp.indexOf('TRASH') !== -1));
  __g.__filtCheck(/Never send it to Spam/i, !!opts.neverSpam);
  __g.__filtCheck(/Always mark it as important/i, !!(opts.alwaysImportant || addUp.indexOf('IMPORTANT') !== -1));
  __g.__filtCheck(/Never mark it as important/i, !!opts.neverImportant);
  if (opts.forwardTo) {
    __g.__filtCheck(/Forward it to/i, true);
    await __g.__filtSleep(400);
    const fwdBox = [...document.querySelectorAll('[role=listbox]')].find((el) =>
      /Choose an address|address/i.test(el.textContent || '')
    );
    if (fwdBox) {
      fwdBox.click();
      await __g.__filtSleep(500);
      const want = String(opts.forwardTo).toLowerCase();
      const item = [...document.querySelectorAll('.J-N, [role=menuitem]')].find((el) => {
        const t = ((el.textContent || '') + '').trim().toLowerCase();
        return t === want || t.indexOf(want) !== -1;
      });
      if (item) item.click();
      else return { __error: 'forward_not_found', want: opts.forwardTo };
      await __g.__filtSleep(300);
    }
  }
  const userLabels = add.filter((x) => !/^(STARRED|TRASH|IMPORTANT)$/i.test(x));
  if (userLabels.length) {
    __g.__filtCheck(/Apply the label/i, true);
    await __g.__filtSleep(400);
    const listbox = [...document.querySelectorAll('[role=listbox]')].find((el) =>
      /Choose label|label/i.test(el.textContent || '')
    );
    if (!listbox) return { __error: 'no_label_listbox' };
    listbox.click();
    await __g.__filtSleep(600);
    const want = userLabels[0];
    const item = [...document.querySelectorAll('.J-N, [role=menuitem]')].find((el) => {
      const t = ((el.textContent || '') + '').trim();
      return t === want || t.toLowerCase() === want.toLowerCase();
    });
    if (!item) return { __error: 'label_not_found', want: want };
    item.click();
    await __g.__filtSleep(300);
  }
  if (opts.alsoApply) __g.__filtCheck(/Also apply filter to/i, true);
  if (__g.__filtVerifyBlocking()) {
    return { __error: 'reauth_required', hint: 'Google verify-its-you dialog — complete browser.login_window Continue, then retry' };
  }
  const anyAction = [...document.querySelectorAll('input[type=checkbox]')].some((el) => {
    if (!el.checked || !(el.getBoundingClientRect().width > 0)) return false;
    const row = el.closest('tr') || el.parentElement;
    return /Skip the Inbox|Mark as read|Star it|Apply the label|Forward it|Delete it|Never send|important|Categorize/i.test(
      (row && row.innerText) || ''
    );
  });
  if (!anyAction) {
    __g.__filtClickVisible(document.querySelectorAll('button,[role=button],a,[role=link]'), /back to search/i);
    return { __error: 'no_action' };
  }
  if (!__g.__filtClickVisible(
    document.querySelectorAll('button,[role=button]'), /^Create filter$/i, { requireEnabled: true }
  )) {
    if (__g.__filtVerifyBlocking()) {
      return { __error: 'reauth_required', hint: 'Create filter covered by verify dialog — login_window then retry' };
    }
    return { __error: 'no_create_step2' };
  }
  const dl = Date.now() + 12000;
  let created = false;
  while (Date.now() < dl) {
    await __g.__filtSleep(300);
    if (__g.__filtVerifyBlocking()) {
      return { __error: 'reauth_required', hint: 'Verify dialog after submit — complete reauth, then list_filters' };
    }
    if (/Your filter was created/i.test(document.body.innerText || '')) { created = true; break; }
    if (/Matches:/.test(document.body.innerText || '') && location.hash.indexOf('settings/filters') !== -1) break;
  }
  await __g.__filtSleep(800);
  location.hash = '#settings/filters';
  await __g.__filtSleep(1000);
  const after = __g.__filtScrape();
  const neu = after.find((f) => f.id && !beforeIds.has(f.id));
  if (!neu) {
    const needle = opts.subject || opts.from || opts.to || opts.query;
    const soft = needle ? after.find((f) => (f.criteria || '').indexOf(needle) !== -1 && !beforeIds.has(f.id)) : null;
    if (soft) return { id: soft.id, criteria: soft.criteria, action: soft.action, status: 'created' };
    return { __error: 'unconfirmed', createdHint: created, remaining: after.length };
  }
  return { id: neu.id, criteria: neu.criteria, action: neu.action, status: 'created' };
};

__g.deleteFilter = async function (filterId) {
  if (!filterId) return { __error: 'no_id' };
  const list = await __g.listFilters();
  const hit = list.find((f) => f.id === filterId || (f.id && f.id.indexOf(filterId) !== -1));
  if (!hit) return { __error: 'not_found', have: list.map((f) => f.id) };
  location.hash = '#settings/filters';
  await __g.__filtSleep(800);
  const del = [...document.querySelectorAll('a,button,[role=link]')].find((el) => {
    if (!/^delete$/i.test((el.textContent || '').trim())) return false;
    return el.id && el.id.indexOf(hit.id) !== -1;
  });
  if (!del) return { __error: 'no_delete_link', id: hit.id };
  del.click();
  await __g.__filtSleep(800);
  const cdl = Date.now() + 8000;
  while (Date.now() < cdl && /Really delete this filter/i.test(document.body.innerText || '')) {
    __g.__filtClickVisible(document.querySelectorAll('button,[role=button]'), /^OK$/i);
    await __g.__filtSleep(600);
  }
  await __g.__filtSleep(800);
  const pdl = Date.now() + 8000;
  let after = [];
  let gone = false;
  while (Date.now() < pdl) {
    after = await __g.listFilters();
    gone = !after.some((f) => f.id === hit.id);
    if (gone) break;
    await __g.__filtSleep(400);
  }
  return { status: gone ? 'deleted' : 'unconfirmed', id: hit.id, remaining: after.length };
};

// ── Labels (settings UI — OAuth parity for create/delete) ─────────────
// #settings/labels "Create new label" dialog + per-row remove/edit.
// User label id in this connector is the display name (same as list_labels
// nav scrape); OAuth uses Label_<n> — CDP has no stable Label_ id from UI.
__g.__labScrapeUser = function () {
  const out = [];
  for (const tr of document.querySelectorAll('table tr')) {
    const remove = [...tr.querySelectorAll('[role=link],a,span')].find((el) =>
      /^remove$/i.test((el.textContent || '').trim())
    );
    if (!remove) continue;
    const nameEl = tr.querySelector('.alC') || tr.querySelector('td');
    const name = ((nameEl && nameEl.textContent) || '').replace(/\s+/g, ' ').trim();
    if (!name) continue;
    const flid = remove.getAttribute('flid') || null;
    out.push({ id: name, name: name, tagType: 'user', flid: flid });
  }
  return out;
};
__g.listLabelsSettings = async function () {
  location.hash = '#settings/labels';
  const dl = Date.now() + 10000;
  while (Date.now() < dl) {
    await __g.__filtSleep(200);
    if (/Create new label/i.test(document.body.innerText || '')) break;
  }
  await __g.__filtSleep(400);
  return __g.__labScrapeUser();
};
__g.createLabel = async function (opts) {
  opts = opts || {};
  const name = (opts.name || '').trim();
  if (!name) return { __error: 'no_name' };
  await __g.listLabelsSettings();
  __g.__filtClickVisible(document.querySelectorAll('button,[role=button]'), /^Cancel$/i);
  await __g.__filtSleep(200);
  if (__g.__labScrapeUser().some((l) => l.name.toLowerCase() === name.toLowerCase())) {
    return { __error: 'exists', id: name, name: name };
  }
  const link = [...document.querySelectorAll('button,[role=button],a,[role=link]')].find((el) =>
    /^Create new label$/i.test((el.textContent || '').trim())
  );
  if (!link) return { __error: 'no_create_link' };
  link.click();
  await __g.__filtSleep(1000);
  const nameInp = [...document.querySelectorAll('input[type=text]')].find((el) => {
    if (!(el.offsetParent || el.getClientRects().length)) return false;
    if (/search/i.test((el.getAttribute('aria-label') || '') + (el.placeholder || ''))) return false;
    const block = ((el.closest('div') && el.parentElement && el.parentElement.parentElement) || el.parentElement || {}).innerText || '';
    return /Please enter a new label name|Nest label/i.test(block) || /qdOxv/.test(el.className || '');
  });
  if (!nameInp) return { __error: 'no_name_input' };
  __g.__filtSet(nameInp, name);
  await __g.__filtSleep(400);
  if (opts.parent) {
    const nestCb = [...document.querySelectorAll('input[type=checkbox]')].find((el) => {
      const block = ((el.closest('div') && el.parentElement && el.parentElement.parentElement) || el.parentElement || {}).innerText || '';
      return /Nest label under/i.test(block);
    });
    if (nestCb && !nestCb.checked) nestCb.click();
    await __g.__filtSleep(400);
  }
  const createBtn = [...document.querySelectorAll('button,[role=button]')].find((el) => {
    if (!/^Create$/i.test((el.textContent || '').trim())) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !el.disabled;
  });
  if (!createBtn) return { __error: 'create_disabled', name: name };
  createBtn.click();
  const dl = Date.now() + 12000;
  let created = false;
  while (Date.now() < dl) {
    await __g.__filtSleep(300);
    const body = document.body.innerText || '';
    if (body.indexOf(name) !== -1 && /was created/i.test(body)) { created = true; break; }
    if (__g.__labScrapeUser().some((l) => l.name === name)) { created = true; break; }
  }
  await __g.__filtSleep(600);
  location.hash = '#settings/labels';
  await __g.__filtSleep(800);
  const hit = __g.__labScrapeUser().find((l) => l.name === name || l.name.toLowerCase() === name.toLowerCase());
  if (!hit) return { __error: 'unconfirmed', createdHint: created, name: name };
  return { id: hit.id, name: hit.name, tagType: 'user', status: 'created', flid: hit.flid };
};
__g.deleteLabel = async function (labelId) {
  if (!labelId) return { __error: 'no_id' };
  const want = String(labelId).trim();
  await __g.listLabelsSettings();
  const list = __g.__labScrapeUser();
  const hit = list.find((l) =>
    l.id === want || l.name === want || l.name.toLowerCase() === want.toLowerCase() ||
    (l.flid && l.flid === want)
  );
  if (!hit) return { __error: 'not_found', have: list.map((l) => l.name) };
  const tr = [...document.querySelectorAll('table tr')].find((row) => {
    const nameEl = row.querySelector('.alC');
    const nm = ((nameEl && nameEl.textContent) || '').replace(/\s+/g, ' ').trim();
    return nm === hit.name;
  });
  if (!tr) return { __error: 'no_row', name: hit.name };
  const remove = [...tr.querySelectorAll('[role=link],a,span')].find((el) =>
    /^remove$/i.test((el.textContent || '').trim())
  );
  if (!remove) return { __error: 'no_remove_link', name: hit.name };
  remove.click();
  await __g.__filtSleep(800);
  const cdl = Date.now() + 8000;
  while (Date.now() < cdl) {
    if (__g.__filtClickVisible(document.querySelectorAll('button,[role=button]'), /^Delete$/i)) break;
    await __g.__filtSleep(400);
  }
  await __g.__filtSleep(800);
  const pdl = Date.now() + 10000;
  let after = [];
  let gone = false;
  while (Date.now() < pdl) {
    after = await __g.listLabelsSettings();
    gone = !after.some((l) => l.name === hit.name);
    if (gone) break;
    await __g.__filtSleep(400);
  }
  return { status: gone ? 'deleted' : 'unconfirmed', id: hit.id, name: hit.name };
};

// ── Send-as + vacation (settings scrape — cheap reads) ───────────────
__g.listSendAs = async function () {
  location.hash = '#settings/accounts';
  const dl = Date.now() + 10000;
  while (Date.now() < dl) {
    await __g.__filtSleep(200);
    if (/Send mail as/i.test(document.body.innerText || '')) break;
  }
  await __g.__filtSleep(400);
  const out = [];
  let inSection = false;
  for (const tr of document.querySelectorAll('table tr')) {
    const t = (tr.innerText || '').replace(/\s+/g, ' ').trim();
    if (/Send mail as/i.test(t)) inSection = true;
    if (inSection && /Check mail from other accounts/i.test(t)) break;
    if (!inSection) continue;
    if (!/edit info/i.test(t)) continue;
    // Skip the section wrapper row that also embeds the first alias.
    if (/Add another email address/i.test(t) || /^Send mail as/i.test(t)) continue;
    const m = t.match(/([^<\n]+?)\s*<([^>\s]+@[^>\s]+)>/);
    if (!m) continue;
    const reply = t.match(/Reply-to address:\s*(\S+@\S+)/i);
    const makeDef = /make default/i.test(t);
    const markedDefault = /\bdefault\b/i.test(t) && !makeDef;
    const email = m[2].trim().toLowerCase();
    if (out.some((x) => (x.sendAsEmail || '').toLowerCase() === email)) continue;
    out.push({
      sendAsEmail: m[2].trim(),
      displayName: m[1].trim(),
      replyToAddress: reply ? reply[1].replace(/[,.]$/, '') : null,
      isDefault: markedDefault,
      isPrimary: false,
      text: t.slice(0, 200),
    });
  }
  if (out.length === 1) out[0].isDefault = true;
  else if (out.length && !out.some((x) => x.isDefault)) {
    const noMake = out.find((x) => !/make default/i.test(x.text || ''));
    if (noMake) noMake.isDefault = true;
  }
  return out;
};

__g.__vacFind = function () {
  const radios = [...document.querySelectorAll('input[type=radio]')].filter((el) =>
    /Vacation responder/i.test(
      (el.getAttribute('aria-label') || '') +
        ((el.closest('tr') || el.parentElement || {}).innerText || '')
    )
  );
  const off = radios.find((el) => /off/i.test((el.getAttribute('aria-label') || '') + ((el.closest('tr') || el.parentElement || {}).innerText || '')));
  const on = radios.find((el) => {
    const t = (el.getAttribute('aria-label') || '') + ((el.closest('tr') || el.parentElement || {}).innerText || '');
    return /on/i.test(t) && !/off/i.test(t);
  });
  const subj =
    document.querySelector('input[aria-label="Subject"]') ||
    [...document.querySelectorAll('input[type=text]')].find((el) =>
      /Subject:/i.test((el.closest('tr') || {}).innerText || '')
    );
  const msg =
    document.querySelector('[aria-label="Vacation responder"][contenteditable="true"]') ||
    document.querySelector('textarea[aria-label="Vacation responder"]');
  const contacts = [...document.querySelectorAll('input[type=checkbox]')].find((el) =>
    /Only send a response to people in my Contacts/i.test(
      ((el.closest('tr') || el.parentElement || {}).innerText || '')
    )
  );
  const domain = [...document.querySelectorAll('input[type=checkbox]')].find((el) =>
    /Only send a response to people in my domain|same domain/i.test(
      ((el.closest('tr') || el.parentElement || {}).innerText || '')
    )
  );
  return { off: off, on: on, subj: subj, msg: msg, contacts: contacts, domain: domain };
};

__g.getVacation = async function () {
  location.hash = '#settings/general';
  const dl = Date.now() + 12000;
  while (Date.now() < dl) {
    await __g.__filtSleep(200);
    if (/Vacation responder/i.test(document.body.innerText || '')) break;
  }
  await __g.__filtSleep(600);
  const v = __g.__vacFind();
  if (!v.off && !v.on) return { __error: 'no_vacation_ui' };
  const enabled = !!(v.on && v.on.checked);
  let body = '';
  if (v.msg) {
    body = (v.msg.value || v.msg.innerText || v.msg.textContent || '').trim();
  }
  return {
    enableAutoReply: enabled,
    responseSubject: v.subj ? (v.subj.value || '') : '',
    responseBodyPlainText: body,
    restrictToContacts: !!(v.contacts && v.contacts.checked),
    restrictToDomain: !!(v.domain && v.domain.checked),
  };
};

__g.setVacation = async function (opts) {
  opts = opts || {};
  await __g.getVacation();
  const v = __g.__vacFind();
  if (!v.off && !v.on) return { __error: 'no_vacation_ui' };
  const wantOn = !!opts.enableAutoReply;
  // Turn ON first when enabling so subject/body editors are live; when
  // disabling, edit fields first then flip OFF so Save persists the off state.
  if (wantOn && v.on && !v.on.checked) {
    v.on.click();
    await __g.__filtSleep(500);
  }
  if (opts.responseSubject != null && v.subj) {
    __g.__filtSet(v.subj, String(opts.responseSubject));
  }
  if (opts.responseBodyPlainText != null && v.msg) {
    const text = String(opts.responseBodyPlainText);
    if (v.msg.getAttribute('contenteditable') === 'true') {
      v.msg.focus();
      try {
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, text);
      } catch (e) {
        v.msg.textContent = text;
        v.msg.dispatchEvent(new Event('input', { bubbles: true }));
      }
    } else {
      __g.__filtSet(v.msg, text);
    }
  }
  if (v.contacts && opts.restrictToContacts != null) {
    if (!!opts.restrictToContacts !== v.contacts.checked) v.contacts.click();
  }
  if (v.domain && opts.restrictToDomain != null) {
    if (!!opts.restrictToDomain !== v.domain.checked) v.domain.click();
  }
  await __g.__filtSleep(300);
  if (!wantOn && v.off && !v.off.checked) {
    v.off.click();
    await __g.__filtSleep(400);
  }
  if (!__g.__filtClickVisible(
    document.querySelectorAll('button,[role=button],input[type=button],input[type=submit]'),
    /^Save Changes$/i
  )) {
    return { __error: 'no_save' };
  }
  await __g.__filtSleep(1500);
  location.hash = '#settings/general';
  await __g.__filtSleep(1000);
  return await __g.getVacation();
};
"""


def _payload(body: str, *, wait_ms: int) -> str:
    """Wrap an op body: library defs → readiness gate → body."""
    return "(async () => {" + _LIB + (_READY % {"wait_ms": wait_ms}) + body + "})()"


def _cdp_pipe_error(exc) -> bool:
    """True when the failure is the browser/CDP pipe, not Gmail business logic."""
    msg = str(exc).lower()
    markers = (
        "session died", "target closed", "inspected target",
        "websocket", "no such target", "not found among", "connection reset",
        "browser not running", "no browser", "broken pipe",
        "connection refused", "econnrefused", "timed out waiting for target",
        "tab was closed", "browser.eval: session", "browser.navigate: session",
        "detached", "no live target", "browsers-bg",
    )
    return any(m in msg for m in markers)


def _map_cdp_exc(exc, *, op: str = "browser"):
    """Typed app_error for a raised browser_session failure."""
    msg = str(exc)
    if " — " in msg:
        msg = msg.rsplit(" — ", 1)[-1].strip()
    if _cdp_pipe_error(exc):
        return app_error(
            f"Browser/CDP pipe failed during {op}: {msg}. The background Gmail "
            "tab may be closed, the browsers-bg daemon dead, or the engine lost "
            "CDP. Retry — the engine relaunches the bg browser on demand; if it "
            "keeps failing, restart the engine.",
            code="BrowserUnavailable",
        )
    low = msg.lower()
    if "js threw" in low:
        detail = msg.split("JS threw:", 1)[-1].strip() if "JS threw:" in msg else msg
        return app_error(
            f"Gmail page JS threw during {op}: {detail}. App-side bug or Gmail "
            "build change — not a closed tab.",
            code="ProviderError",
        )
    return app_error(f"Browser op failed during {op}: {msg}", code="ProviderError")


def _is_err(value) -> bool:
    """True for an app_error (engine-shaped) or a payload __error dict."""
    if not isinstance(value, dict):
        return False
    if "__result__" in value and isinstance(value["__result__"], dict):
        return "error" in value["__result__"]
    return "error" in value or "__error" in value


async def _nav(url: str, *, timeout_s: int = 60, timeout=None, **_kw):
    """Navigate the bg mail tab; CDP failures → typed app_error (else None)."""
    ts = timeout if timeout is not None else timeout_s
    try:
        await browser_session.navigate(_TARGET, url, timeout=ts)
        return None
    except Exception as e:
        return _map_cdp_exc(e, op="navigate")


async def _eval(body: str, *, wait_ms: int = 30000, timeout_s: int = 45):
    """Run an op body in the mail.google.com tab; map structured + CDP errors."""
    try:
        value = await browser_session.eval(
            _TARGET, _payload(body, wait_ms=wait_ms), timeout=timeout_s
        )
    except Exception as e:
        return _map_cdp_exc(e, op="eval")
    if isinstance(value, dict) and "__error" in value:
        code = value["__error"]
        if code == "auth_required":
            return app_error(
                "Gmail is signed out (the tab is on a Google sign-in page). Run "
                "gmail-cdp.login and complete sign-in in the browser, then retry.",
                code="NeedsAuth",
            )
        if code == "tab_not_ready":
            return app_error(
                "The mail.google.com tab never became ready — Gmail may still be "
                "loading.", code="NotReady",
            )
        if code == "no_data":
            return app_error(
                "Gmail didn't fire a browse request for the query — the tab may "
                "not have navigated, or the account has no matching mail.",
                code="NoData",
            )
        if code == "parse_fail":
            return app_error("Couldn't parse Gmail's bv response.", code="ProviderError")
        if code == "no_account":
            return app_error(
                f"No signed-in Gmail account matches {value.get('want')!r}. "
                f"Signed in: {value.get('have')}.", code="NotFound",
            )
        if code == "no_ik":
            return app_error(
                "Gmail's inbox key (GM_ID_KEY) wasn't available — the tab may "
                "not be fully loaded.", code="NotReady",
            )
        if code == "om_failed":
            return app_error(
                "Gmail's message-source endpoint failed — the thread id may be "
                "stale, or the account signed out.", code="ProviderError",
            )
        if code == "no_gmonkey":
            return app_error(
                "Gmail's in-page API (gmonkey) didn't load — the tab may not be "
                "fully ready.", code="NotReady",
            )
        if code == "no_draft":
            return app_error("Couldn't open a Gmail compose window.", code="ProviderError")
        if code == "recipient_not_set":
            return app_error(
                "Gmail's compose didn't accept the recipient — check the address.",
                code="BadParams",
            )
        if code == "send_failed":
            return app_error(f"Gmail send failed: {value.get('what')}", code="ProviderError")
        if code == "no_message":
            return app_error(
                "Couldn't read the message via gmonkey — the thread didn't open.",
                code="ProviderError",
            )
        if code == "no_sync_headers":
            return app_error(
                "Gmail's per-session action headers weren't captured — navigate a "
                "mailbox (list_emails) first so a /sync/ request seeds them.",
                code="NotReady",
            )
        if code == "no_thread_msgs":
            return app_error(
                "Couldn't find the thread's messages to act on — the id may be "
                "stale, or the thread wasn't in the mailbox that was loaded.",
                code="NotFound",
            )
        if code == "not_found":
            return app_error(
                f"Gmail item not found"
                + (f" (have: {value.get('have')})" if value.get("have") is not None else "")
                + ".",
                code="NotFound",
                detail=value,
            )
        if code == "action_failed":
            return app_error(
                f"Gmail rejected the label action (status {value.get('status')}"
                f"{', ' + value['what'] if value.get('what') else ''}).",
                code="ProviderError",
            )
        if code == "reauth_required":
            return app_error(
                "Gmail requires a sensitive-action reauth (\"We need to verify it's you\") "
                "before mutating filters/settings. Open browser.login_window on the "
                "engine bg profile, click Continue, complete Google's popup, then retry. "
                + (str(value.get("hint") or "")),
                code="NeedsAuth",
                detail=value,
            )
        return app_error(f"Gmail payload error: {code}", code="PayloadError")
    return value


async def _accounts() -> list[dict]:
    """Enumerate signed-in Google accounts (email + /u/N/ index)."""
    value = await _eval("return await __enumAccounts();", wait_ms=15000, timeout_s=30)
    return value if isinstance(value, list) else []


async def _resolve_index(account_email: str | None):
    """Resolve an account email → its /u/N/ index. None → the default (0)."""
    accts = await _accounts()
    if not accts:
        return None, []
    if not account_email:
        return accts[0]["index"], accts
    for a in accts:
        if a["email"].lower() == account_email.lower():
            return a["index"], accts
    return None, accts


# ──────────────────────────────────────────────────────────────────────
# The account trio — check / login / logout
# ──────────────────────────────────────────────────────────────────────

@account.check
@returns("account")
@connection("none")
@timeout(45)
async def check_session(**params):
    """Verify the Gmail Web session and identify the default account.

    Reads the signed-in accounts from their atom feeds (no UI navigation). On a
    signed-out tab (redirected to a Google sign-in page) returns
    `{authenticated: false}` so the resolver knows to drive `login`.
    """
    accts = await _accounts()
    if not accts:
        return {"authenticated": False}
    primary = normalize_email(accts[0]["email"])
    return {
        "authenticated": True,
        "at": {"shape": "product", "name": "Gmail", "url": "https://mail.google.com/"},
        "platform": "email",
        "identifier": primary,
        "email": primary,
        "handle": primary,
    }


@account.login
@returns("account | auth_challenge")
@connection("none")
@timeout(60)
async def login(**params):
    """Sign in to Gmail — or report the already-live session.

    Google sign-in (OAuth + MFA + account chooser) can't be driven
    programmatically, so this opens a foreground sign-in tab in the daily
    browser and returns a `login_window` challenge; the human signs in (and can
    Add account for more Google accounts), then the agent polls check_session.
    """
    session = await check_session(**params)
    if isinstance(session, dict) and session.get("authenticated"):
        return session
    return await browser_session.login_window("https://mail.google.com/", label="Gmail")


@account.logout
@returns({"status": "string", "hint": "string"})
@connection("none")
@timeout(45)
async def logout(**params):
    """Sign out of Google — the real account sign-out.

    Navigates the tab to Google's logout endpoint. NOTE: this signs out ALL
    Google accounts in the daily browser profile (Gmail, Drive, Calendar, …),
    not just one — the session is the shared profile. Re-run login to sign in.
    """
    _nav_err = await _nav("https://mail.google.com/mail/u/0/?logout")
    if _nav_err is not None:
        return _nav_err
    return {
        "status": "logged_out",
        "hint": "Navigated to Google sign-out; the session is cleared from the "
                "daily browser profile. Re-run login to sign back in.",
    }


# ──────────────────────────────────────────────────────────────────────
# Accounts + Mail
# ──────────────────────────────────────────────────────────────────────

@returns({"email": "string", "index": "integer", "unread": "integer"})
@connection("none")
@timeout(30)
@test
async def list_accounts(**params):
    """List the Google accounts signed into the browser (email + /u/N/ index)."""
    return await _accounts()


@returns("email[]")
@provides("mailbox", account_param="account")
@connection("none")
@timeout(120)
@test(params={"mailbox": "inbox", "limit": 5})
async def list_emails(*, account=None, query="", mailbox="inbox", limit=25, **params):
    """List emails from a Google account by intercepting Gmail's own browse request.

    Navigates the account's tab to a search view (which fires a fresh `bv`),
    captures the response, and maps the thread stubs to the `email` shape.

    Args:
        account: Which signed-in Google account (email). Defaults to the first.
        query: A raw Gmail search query. Overrides `mailbox` when set
            (e.g. "from:stripe.com", "is:unread newer_than:7d").
        mailbox: Folder vocabulary shared with other mailbox providers —
            inbox · sent · drafts · trash · spam · starred · unread · all.
        limit: Max emails to return (most recent first).
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return app_error(
            f"No signed-in Gmail account matches {account!r}. "
            f"Signed in: {[a['email'] for a in accts]}.", code="NotFound",
        ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")

    smtp = next((normalize_email(a["email"]) for a in accts if a["index"] == idx), None)
    gmail_query = query or _MAILBOX_TO_QUERY.get(mailbox, "in:inbox")

    # Land on the target account (full load if switching accounts; a no-op hash
    # otherwise). The eval's readiness gate then waits for Gmail to be live.
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#inbox")
    if _nav_err is not None:
        return _nav_err

    body = (
        f"const __q = {json.dumps(gmail_query)};"
        f"const __smtp = {json.dumps(smtp)};"
        f"const __limit = {int(limit)};"
        # Force a fresh bv: transition through #inbox then into the search view.
        "__g.bv = [];"
        "location.hash = '#inbox';"
        "await new Promise(r => setTimeout(r, 150));"
        "location.hash = '#search/' + encodeURIComponent(__q);"
        "const __dl = Date.now() + 9000;"
        "while (__g.bv.length === 0 && Date.now() < __dl) { await new Promise(r => setTimeout(r, 150)); }"
        "if (__g.bv.length === 0) return { __error: 'no_data' };"
        "const __text = __g.bv.slice().sort((a, b) => b.length - a.length)[0];"
        "let __parsed; try { __parsed = JSON.parse(__text); } catch (e) { return { __error: 'parse_fail' }; }"
        "const __mapped = __mapStubs(__parsed, __smtp);"
        "__mapped.sort((a, b) => (b.__ts || 0) - (a.__ts || 0));"
        "const __out = __mapped.slice(0, __limit);"
        "__out.forEach((m) => { delete m.__ts; });"
        "return __out;"
    )
    result = await _eval(body, wait_ms=30000, timeout_s=90)
    # Stamp a mail permalink on each node so opening it routes to get_email via
    # the web_fetch provider. Prefer messageHex (view=om token); fall back to
    # legacyThreadId / thread-f→hex. thread-a alone cannot open view=om.
    if isinstance(result, list):
        for m in result:
            om = m.get("messageHex") or m.get("_omToken") or m.get("legacyThreadId")
            tid = m.get("id", "")
            if not om and isinstance(tid, str) and tid.startswith("thread-f:"):
                try:
                    om = format(int(tid.split(":", 1)[1]), "x")
                    m["legacyThreadId"] = om
                except (ValueError, IndexError):
                    pass
            if om and _is_hex(str(om)):
                m["url"] = f"https://mail.google.com/mail/u/{idx}/#all/{str(om).lower()}"
    return result


@returns("email[]")
@connection("none")
@timeout(120)
@test(params={"query": "in:inbox", "limit": 5})
async def search_emails(*, query, limit=25, account=None, **params):
    """Search Gmail with a raw query — a thin alias over list_emails (which is
    already search-driven). Mirrors the gmail plugin's `search_emails`.

    Args:
        query: A raw Gmail search query (from:, subject:, is:unread,
            newer_than:7d, has:attachment, …).
        limit: Max emails to return (most recent first).
        account: Which signed-in Google account (email). Defaults to the first.
    """
    return await list_emails(query=query, limit=limit, account=account, **params)


@returns("tag[]")
@connection("none")
@timeout(30)
@test
async def list_labels(*, account=None, **params):
    """List a Google account's Gmail labels (system + user) as tags.

    The system labels are a stable, known vocabulary (seeded so Spam/Trash —
    hidden from the web left-nav by default — are always present); the account's
    user labels and categories are read live off Gmail Web's own nav (the
    human-visible label list). Mirrors the gmail (OAuth) plugin's `tag[]` output.

    Note: only nav-visible user labels are returned — labels collapsed under
    "More labels" aren't enumerated (a known limit of the web-sourced list).

    Args:
        account: Which signed-in Google account (email). Defaults to the first.
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}. Signed in: "
                f"{[a['email'] for a in accts]}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#inbox")
    if _nav_err is not None:
        return _nav_err
    # The nav's label anchors carry the hash-route token (#label/<name>,
    # #category/<cat>) and the display name (aria-label). System labels have bare
    # tokens (no label/ or category/ prefix), so this loop picks up only the
    # user labels + categories; the system set is seeded below.
    body = (
        "const __out = [], __seen = new Set();"
        "const __nav = document.querySelector('div[role=navigation]') || document;"
        "for (const a of __nav.querySelectorAll('a[href]')) {"
        "  const m = (a.getAttribute('href') || '').match(/#(label|category)\\/([^?]+)/);"
        "  if (!m) continue;"
        "  const kind = m[1], token = decodeURIComponent(m[2]);"
        "  let name = (a.getAttribute('aria-label') || a.textContent || '').trim();"
        "  name = name.replace(/\\s*\\d+ unread$/, '').replace(/\\s+has menu$/, '').trim();"
        "  if (!name) name = token;"
        "  const key = kind + ':' + token;"
        "  if (__seen.has(key)) continue; __seen.add(key);"
        # Categories (Purchases, Social, …) are Gmail built-ins → system; a
        # #label/ route is a user-created label → user.
        "  __out.push({ id: kind === 'category' ? ('CATEGORY_' + token.toUpperCase()) : name,"
        "               name: name, tagType: kind === 'category' ? 'system' : 'user' });"
        "}"
        "return __out;"
    )
    value = await _eval(body, wait_ms=30000, timeout_s=45)
    if not isinstance(value, list):
        return value
    system = [{"id": lid, "name": nm, "tagType": "system"} for lid, nm in _SYSTEM_LABELS]
    return system + value


@returns({"id": "string", "criteria": "string", "action": "string"})
@connection("none")
@timeout(60)
@test
async def list_filters(*, account=None, **params):
    """List server-side Gmail filters (rules) from the Settings UI.

    Navigates `#settings/filters` and scrapes the filter table — same surface
    the human edits. OAuth parity with `gmail.list_filters` (criteria/action
    as display strings; `id` from the edit/delete link).

    Args:
        account: Which signed-in Google account. Defaults to the first.
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#settings/filters")
    if _nav_err is not None:
        return _nav_err
    return await _eval("return await __g.listFilters();", wait_ms=30000, timeout_s=45)


@test.skip(reason="mutates real filters")
@returns({"status": "string"})
@connection("none")
@timeout(60)
async def delete_filter(*, id, account=None, **params):
    """Delete a server-side filter by id (from `list_filters`).

    Clicks the Settings → Filters delete link for that id. Prefer listing
    first — ids are Gmail's `z…*…` tokens embedded in the link.

    Args:
        id: Filter id from `list_filters`.
        account: Which signed-in Google account.
    """
    if not id:
        return app_error("Pass filter `id` from list_filters.", code="BadParams")
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#settings/filters")
    if _nav_err is not None:
        return _nav_err
    body = f"return await __g.deleteFilter({json.dumps(id)});"
    value = await _eval(body, wait_ms=30000, timeout_s=45)
    if isinstance(value, dict) and value.get("__error"):
        return app_error(
            f"delete_filter failed: {value.get('__error')}",
            code="NotFound" if value["__error"] == "not_found" else "ProviderError",
        )
    return value


@test.skip(reason="mutates filters")
@returns({"id": "string", "criteria": "string", "action": "string", "status": "string"})
@connection("none")
@timeout(90)
async def create_filter(*, from_addr=None, to=None, subject=None, query=None,
                        has_attachment=False, add_labels=None, remove_labels=None,
                        forward_to=None, account=None, **params):
    """Create a server-side filter via Settings → Create a new filter.

    OAuth-parity with `gmail.create_filter`. Drives the two-step dialog
    (criteria → actions). `remove_labels` containing `INBOX` skips the inbox;
    `UNREAD` marks as read. `add_labels` of `STARRED`/`TRASH`/`IMPORTANT` map
    to the matching checkboxes; other names pick "Apply the label".

    Args:
        from_addr: Match From.
        to: Match To.
        subject: Match Subject.
        query: Match "Has the words".
        has_attachment: Require an attachment.
        add_labels: Labels to apply (names), or STARRED/TRASH/IMPORTANT.
        remove_labels: Labels to remove — INBOX → skip inbox, UNREAD → mark read.
        forward_to: Forward to an address already configured in Gmail.
        account: Which signed-in Google account.
    """
    if not any([from_addr, to, subject, query, has_attachment]):
        return app_error(
            "Pass at least one criterion: from_addr, to, subject, query, or has_attachment.",
            code="BadParams",
        )
    add_labels = list(add_labels or [])
    remove_labels = list(remove_labels or [])
    if not any([
        add_labels, remove_labels, forward_to,
        params.get("skip_inbox"), params.get("mark_read"), params.get("star"),
        params.get("delete_it"), params.get("never_spam"),
    ]):
        # Default OAuth-ish action when only criteria given: skip inbox is too
        # aggressive; require an explicit action.
        return app_error(
            "Pass an action: add_labels, remove_labels (e.g. ['INBOX']), or forward_to.",
            code="BadParams",
        )
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#settings/filters")
    if _nav_err is not None:
        return _nav_err
    opts = {
        "from": from_addr,
        "to": to,
        "subject": subject,
        "query": query,
        "hasAttachment": bool(has_attachment),
        "addLabels": add_labels,
        "removeLabels": remove_labels,
        "forwardTo": forward_to,
        "skipInbox": bool(params.get("skip_inbox")),
        "markRead": bool(params.get("mark_read")),
        "star": bool(params.get("star")),
        "deleteIt": bool(params.get("delete_it")),
        "neverSpam": bool(params.get("never_spam")),
        "alsoApply": bool(params.get("also_apply")),
    }
    body = f"return await __g.createFilter({json.dumps(opts)});"
    value = await _eval(body, wait_ms=30000, timeout_s=75)
    if isinstance(value, dict) and value.get("__error"):
        err = value["__error"]
        code = "BadParams" if err in ("no_criteria", "no_action") else "ProviderError"
        if err in ("label_not_found", "forward_not_found", "not_found"):
            code = "NotFound"
        if err == "reauth_required":
            code = "NeedsAuth"
        return app_error(f"create_filter failed: {err}", code=code, detail=value)
    return value


@test.skip(reason="mutates labels")
@returns("tag")
@connection("none")
@timeout(60)
async def create_label(*, name, show_in_label_list=None, show_in_message_list=None,
                       account=None, **params):
    """Create a Gmail label via Settings → Labels (OAuth parity).

    Drives "Create new label", waits for the settings row. Returned `id` is
    the display name — same id `list_labels` uses for user labels (CDP has no
    OAuth `Label_<n>` token from the UI). Visibility args are accepted for
    signature parity; the create dialog uses Gmail's defaults (show).

    Args:
        name: Label name (use throwaway `AOS-LABEL-*` for tests).
        show_in_label_list: Unused (OAuth parity); defaults to show.
        show_in_message_list: Unused (OAuth parity); defaults to show.
        account: Which signed-in Google account.
    """
    if not (name or "").strip():
        return app_error("Pass label `name`.", code="BadParams")
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#settings/labels")
    if _nav_err is not None:
        return _nav_err
    opts = {"name": name.strip(), "parent": params.get("parent")}
    body = f"return await __g.createLabel({json.dumps(opts)});"
    value = await _eval(body, wait_ms=30000, timeout_s=60)
    if _is_err(value):
        return value
    if not isinstance(value, dict) or not value.get("name"):
        return app_error("create_label failed: unconfirmed", code="ProviderError", detail=value)
    return {
        "id": value.get("id") or value.get("name"),
        "name": value.get("name"),
        "tagType": value.get("tagType") or "user",
    }


@test.skip(reason="mutates labels")
@returns({"status": "string"})
@connection("none")
@timeout(60)
async def delete_label(*, id, account=None, **params):
    """Delete a user label by id/name (from `list_labels` / `create_label`).

    Clicks Settings → Labels → remove → Delete. Does not delete messages.

    Args:
        id: Label id (display name for user labels).
        account: Which signed-in Google account.
    """
    if not id:
        return app_error("Pass label `id` (display name from list_labels).", code="BadParams")
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#settings/labels")
    if _nav_err is not None:
        return _nav_err
    body = f"return await __g.deleteLabel({json.dumps(id)});"
    value = await _eval(body, wait_ms=30000, timeout_s=60)
    if _is_err(value):
        return value
    if not isinstance(value, dict) or value.get("status") != "deleted":
        return app_error(
            f"delete_label failed: {value.get('status') if isinstance(value, dict) else value}",
            code="ProviderError",
            detail=value,
        )
    return {"status": "deleted", "id": value.get("id") or id}


@returns({
    "sendAsEmail": "string", "displayName": "string",
    "isDefault": "boolean", "isPrimary": "boolean", "replyToAddress": "string",
})
@connection("none")
@timeout(60)
@test
async def list_send_as(*, account=None, **params):
    """List send-as aliases from Settings → Accounts (OAuth parity).

    Scrapes the "Send mail as" section. `isPrimary` is true when the address
    matches the signed-in account email.

    Args:
        account: Which signed-in Google account.
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    smtp = next((normalize_email(a["email"]) for a in accts if a["index"] == idx), None)
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#settings/accounts")
    if _nav_err is not None:
        return _nav_err
    value = await _eval("return await __g.listSendAs();", wait_ms=30000, timeout_s=45)
    if _is_err(value):
        return value
    if not isinstance(value, list):
        return app_error("list_send_as failed.", code="ProviderError", detail=value)
    out = []
    for row in value:
        email = normalize_email(row.get("sendAsEmail") or "")
        out.append({
            "sendAsEmail": row.get("sendAsEmail"),
            "displayName": row.get("displayName"),
            "replyToAddress": row.get("replyToAddress"),
            "isDefault": bool(row.get("isDefault")),
            "isPrimary": bool(smtp and email == smtp),
        })
    return out


@returns({
    "enableAutoReply": "boolean", "responseSubject": "string",
    "responseBodyPlainText": "string",
})
@connection("none")
@timeout(60)
@test
async def get_vacation(*, account=None, **params):
    """Read vacation/auto-reply from Settings → General.

    Args:
        account: Which signed-in Google account.
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#settings/general")
    if _nav_err is not None:
        return _nav_err
    value = await _eval("return await __g.getVacation();", wait_ms=30000, timeout_s=45)
    if _is_err(value):
        return value
    return value


@test.skip(reason="mutates vacation — restore after AOS tests")
@returns({
    "enableAutoReply": "boolean", "responseSubject": "string",
    "responseBodyPlainText": "string",
})
@connection("none")
@timeout(90)
async def set_vacation(*, enabled, subject=None, body=None, html_body=None,
                       contacts_only=False, domain_only=False,
                       account=None, **params):
    """Set vacation/auto-reply via Settings → General (OAuth parity).

    Prefer `get_vacation` first and restore previous state after any test.
    `html_body` is accepted for signature parity; the settings editor is
    plain-text (falls back to `body`).

    Args:
        enabled: Turn vacation responder on/off.
        subject: Auto-reply subject.
        body / html_body: Auto-reply message body.
        contacts_only: Only reply to Contacts.
        domain_only: Only reply to same domain (if the checkbox exists).
        account: Which signed-in Google account.
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#settings/general")
    if _nav_err is not None:
        return _nav_err
    opts = {
        "enableAutoReply": bool(enabled),
        "responseSubject": subject if subject is not None else None,
        "responseBodyPlainText": body if body is not None else html_body,
        "restrictToContacts": bool(contacts_only),
        "restrictToDomain": bool(domain_only),
    }
    body_js = f"return await __g.setVacation({json.dumps(opts)});"
    value = await _eval(body_js, wait_ms=30000, timeout_s=75)
    if _is_err(value):
        return value
    return value


# ──────────────────────────────────────────────────────────────────────
# get_email — the full HTML body via Gmail's own message-source endpoint
# ──────────────────────────────────────────────────────────────────────
#
# The list `bv` carries only subject/snippet/metadata (never the body). For the
# full body we hit Gmail's OWN internal endpoint `?ik=<GM_ID_KEY>&view=om&th=<hex>`
# ("original message") — a same-origin cookie GET, the session carries for free —
# which returns the complete raw RFC822 inside `<pre id="raw_message_text">`.
# Python's `email` stdlib parses it → the original sender HTML (or text) part +
# every header, key-for-key with gmail.py's OAuth mapping. NOT DOM scraping and
# NOT a hand-built POST: Gmail's own message-source, parsed with a real library.
#
# (Why not gmonkey for the body? gmonkey's read surface is display-oriented —
# getDate() is a varying locale string, lossy to parse — so it's the wrong tool
# for a data extraction. gmonkey's precise surface is ACTIONS: see send_email.)


def _thread_token(id=None, url=None):
    """Resolve a caller id/url → Gmail's hex thread token (for `th=`)."""
    src = (id or "").strip()
    if not src and url:
        frag = url.split("#", 1)[1] if "#" in url else url.split("?", 1)[0]
        segs = [s for s in frag.split("/") if s]
        src = segs[-1].split("?")[0] if segs else ""
    if src.startswith("thread-f:") or src.startswith("msg-f:"):
        try:
            return format(int(src.split(":", 1)[1]), "x")
        except ValueError:
            return None
    return src or None


def _is_hex(s):
    return bool(s) and all(c in "0123456789abcdefABCDEF" for c in s)


def _extract_raw_source(om_html):
    """Pull the raw RFC822 out of the view=om page's `pre#raw_message_text`."""
    try:
        pres = lxml_html.fromstring(om_html).xpath('//pre[@id="raw_message_text"]')
        return pres[0].text_content() if pres else None
    except Exception:
        return None


def _domain_of(handle):
    at = (handle or "").rfind("@")
    return handle[at + 1:].lower() if at > 0 else None


def _addr_list(hdr):
    """An RFC822 address header (policy.default AddressHeader) → account dicts."""
    if hdr is None:
        return []
    out = []
    try:
        addrs = hdr.addresses
    except Exception:
        return []
    for a in addrs:
        handle = (a.addr_spec or "").strip()
        if "@" not in handle:
            continue
        out.append({"handle": handle, "platform": "email", "displayName": (a.display_name or None)})
    return out


def _rfc822_date_iso(hdr):
    try:
        dt = hdr.datetime
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.isoformat()
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _attachment_ext(filename, mime_type):
    """Extension (no dot) for blob store naming."""
    name = filename or ""
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()[:16] or "bin"
    mime = (mime_type or "").lower()
    return {
        "text/plain": "txt", "text/html": "html", "application/pdf": "pdf",
        "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
        "application/zip": "zip", "application/json": "json",
    }.get(mime, "bin")


def _collect_rfc822_attachments(msg):
    """Attachment metadata from a raw RFC822 — id is the walk index for re-fetch."""
    out = []
    for i, part in enumerate(msg.walk() if msg.is_multipart() else [msg]):
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type()
        is_att = "attachment" in disp or (
            "inline" in disp and ctype not in ("text/plain", "text/html")
        ) or (
            "filename" in disp and ctype not in ("text/plain", "text/html")
        )
        if not is_att and ctype in ("text/plain", "text/html", "multipart/alternative",
                                     "multipart/related", "multipart/mixed"):
            continue
        filename = part.get_filename()
        if not filename and not is_att:
            continue
        filename = filename or f"attachment-{i}"
        try:
            payload = part.get_payload(decode=True) or b""
            size = len(payload)
        except Exception:
            size = None
        out.append({
            "id": str(i),
            "filename": filename,
            "name": filename,
            "mimeType": ctype or "application/octet-stream",
            "size": size,
        })
    return out


def _extract_rfc822_part(raw, part_id: str):
    """Return (filename, mimeType, raw_bytes) for attachment walk-index `part_id`."""
    msg = emaillib.message_from_string(raw, policy=policy.default)
    want = int(part_id)
    for i, part in enumerate(msg.walk() if msg.is_multipart() else [msg]):
        if i != want:
            continue
        try:
            raw_b = part.get_payload(decode=True) or b""
        except Exception as e:
            raise ValueError(f"decode failed: {e}") from e
        return (
            part.get_filename() or f"attachment-{i}",
            part.get_content_type() or "application/octet-stream",
            raw_b,
        )
    raise ValueError(f"no part {part_id}")


def _map_rfc822(raw, *, smtp, node_id):
    """Raw RFC822 → the `email` shape (keys mirror gmail.py::_map_email)."""
    msg = emaillib.message_from_string(raw, policy=policy.default)
    from_list = _addr_list(msg.get("From"))
    from_obj = from_list[0] if from_list else None
    html_body = text_body = None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        if "attachment" in (part.get("Content-Disposition") or "").lower():
            continue
        ctype = part.get_content_type()
        try:
            if ctype == "text/html" and html_body is None:
                html_body = part.get_content()
            elif ctype == "text/plain" and text_body is None:
                text_body = part.get_content()
        except Exception:
            continue
    content, mime = (html_body, "text/html") if html_body else (text_body or "", "text/plain")
    attachments = _collect_rfc822_attachments(msg)
    dom = _domain_of(from_obj["handle"]) if from_obj else None
    # RFC 2369 List-Unsubscribe + RFC 8058 one-click (List-Unsubscribe-Post)
    unsubscribe = None
    unsubscribe_one_click = False
    unsub_raw = str(msg.get("List-Unsubscribe") or "")
    if unsub_raw:
        urls = re.findall(r"<([^>]+)>", unsub_raw)
        for url in urls:
            if url.startswith("http"):
                unsubscribe = url
                break
        if not unsubscribe and urls:
            unsubscribe = urls[0]
    if msg.get("List-Unsubscribe-Post") and unsubscribe and unsubscribe.startswith("http"):
        unsubscribe_one_click = True
    list_id_raw = str(msg.get("List-Id") or "")
    list_id_m = re.search(r"<([^>]+)>", list_id_raw)
    list_id = list_id_m.group(1) if list_id_m else (list_id_raw.strip() or None)
    out = {
        "id": node_id,
        "name": str(msg.get("Subject") or "").strip() or "(no subject)",
        "content": content,
        "content_mime": mime,
        "author": (from_obj["displayName"] if from_obj else None),
        "published": _rfc822_date_iso(msg.get("Date")),
        "isUnread": False,
        "isStarred": False,
        "hasAttachments": len(attachments) > 0,
        "attachments": attachments,
        "messageId": str(msg.get("Message-ID") or "") or "",
        "inReplyTo": (str(msg.get("In-Reply-To")) if msg.get("In-Reply-To") else None),
        "references": (str(msg.get("References")) if msg.get("References") else None),
        "conversationId": node_id,
        "from": from_obj,
        "to": _addr_list(msg.get("To")),
        "copied_to": _addr_list(msg.get("Cc")),
        "bcc": _addr_list(msg.get("Bcc")),
        "domain": {"name": dom} if dom else None,
        "unsubscribe": unsubscribe,
        "unsubscribeOneClick": unsubscribe_one_click,
        "listId": list_id,
    }
    if smtp:
        out["accountEmail"] = smtp
    return out


@returns("email")
@provides("web_fetch", urls=["mail.google.com/*"])
@connection("none")
@timeout(90)
async def _fetch_raw_rfc822(*, token, idx):
    """Fetch Gmail view=om → raw RFC822 string (or app_error / error dict).

    `token` MUST be a message legacy hex (not thread-a). Self-sent thread hex
    often 200s "does not exist"; message hex (list.messageHex / msg stub [55])
    works. Caller resolves via `__agmail.resolveOmToken`.
    """
    body = (
        f"const __token = {json.dumps(token)};"
        f"const __idx = {int(idx)};"
        "const __ik = window.GM_ID_KEY;"
        "if (!__ik) return { __error: 'no_ik' };"
        "let __r;"
        "try { __r = await fetch('/mail/u/' + __idx + '/?ik=' + encodeURIComponent(__ik)"
        "  + '&view=om&th=' + encodeURIComponent(__token), { credentials: 'include' }); }"
        "catch (e) { return { __error: 'om_failed', what: String(e).slice(-80) }; }"
        "if (!__r.ok) return { __error: 'om_failed', status: __r.status };"
        "return { omHtml: await __r.text() };"
    )
    value = await _eval(body, wait_ms=30000, timeout_s=75)
    if not isinstance(value, dict) or "omHtml" not in value:
        return value
    raw = _extract_raw_source(value["omHtml"])
    if not raw:
        head = (value.get("omHtml") or "")[:180].replace("\n", " ")
        return app_error(
            f"Couldn't read Gmail's message source for {token!r} (view=om returned no "
            f"raw RFC822). Need the MESSAGE legacy hex (list.messageHex / "
            f"data-legacy-message-id) — thread hex fails on many self-sents. Got: {head!r}",
            code="ProviderError",
        )
    return raw


async def _resolve_om_token(*, token, idx):
    """Resolve thread-a / thread hex / message hex → message hex for view=om."""
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#inbox")
    if _nav_err is not None:
        return _nav_err
    body = (
        f"const __tok = {json.dumps(token)};"
        "return await __g.resolveOmToken(__tok);"
    )
    return await _eval(body, wait_ms=45000, timeout_s=90)


@returns("email")
@provides("web_fetch", urls=["mail.google.com/*"])
@connection("none")
@timeout(120)
async def get_email(*, id=None, url=None, account=None, **params):
    """Get one email with full HTML body, headers, and attachment metadata.

    Fetches Gmail's message-source (`view=om`) and parses the raw RFC822.
    Attachment *bytes* hydrate on demand via `get_attachment`.

    Args:
        id: Thread id from `list_emails` (`thread-f:…` / hex / thread-a:…),
            or `messageHex` / `_omToken` from a list row (preferred for om).
        url: A mail.google.com URL — hex/perm token read from path.
        account: Which signed-in Google account. Defaults to the first.
    """
    token = _thread_token(id, url)
    if not token:
        return app_error("Pass an email `id` (thread id) or a mail `url`.", code="BadParams")
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}. Signed in: "
                f"{[a['email'] for a in accts]}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    smtp = next((normalize_email(a["email"]) for a in accts if a["index"] == idx), None)
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#inbox")
    if _nav_err is not None:
        return _nav_err
    # Prefer an explicit messageHex/_omToken the caller already has from list.
    # Also: when `id` is already hex, try it as the om token first — received
    # single-msg threads often have thread hex == message hex, and resolveOmToken
    # can fail on old mail that isn't in a fresh bv / won't open in the DOM.
    om_hint = params.get("messageHex") or params.get("_omToken")
    resolved = {"omToken": None}
    if om_hint and _is_hex(str(om_hint)):
        om_token = str(om_hint).lower()
        resolved = {"omToken": om_token, "all": [om_token]}
    elif _is_hex(token):
        om_token = token.lower()
        resolved = {"omToken": om_token, "all": [om_token], "threadHex": om_token}
    else:
        resolved = await _resolve_om_token(token=token, idx=idx)
        if not isinstance(resolved, dict) or not resolved.get("omToken"):
            if isinstance(resolved, dict) and resolved.get("__error"):
                return app_error(
                    f"Couldn't resolve {token!r} to a message hex for view=om "
                    f"({resolved.get('__error')}).",
                    code="ProviderError",
                )
            return resolved
        om_token = resolved["omToken"]
    raw = await _fetch_raw_rfc822(token=om_token, idx=idx)
    # If hex-as-om failed (self-sent thread hex ≠ message hex), resolve via bv/DOM.
    if not isinstance(raw, str) and _is_hex(token) and not om_hint:
        resolved = await _resolve_om_token(token=token, idx=idx)
        if isinstance(resolved, dict) and resolved.get("omToken"):
            om_token = resolved["omToken"]
            raw = await _fetch_raw_rfc822(token=om_token, idx=idx)
    # Thread hex sometimes equals message hex (received); when resolve guessed
    # wrong, retry every candidate.
    if not isinstance(raw, str) and isinstance(resolved, dict):
        for alt in resolved.get("all") or []:
            if alt == om_token:
                continue
            raw = await _fetch_raw_rfc822(token=alt, idx=idx)
            if isinstance(raw, str):
                om_token = alt
                break
    if not isinstance(raw, str):
        return raw
    node_id = id or (f"thread-f:{int(token, 16)}" if _is_hex(token) else token)
    mapped = _map_rfc822(raw, smtp=smtp, node_id=node_id)
    mapped["_omToken"] = om_token
    mapped["messageHex"] = om_token
    if isinstance(resolved, dict) and resolved.get("threadHex"):
        mapped["legacyThreadId"] = resolved["threadHex"]
    elif _is_hex(token) and token.lower() != om_token:
        mapped["legacyThreadId"] = token.lower()
    return mapped


@test.skip(reason="can unsubscribe for real — use confirm=True")
@returns({
    "status": "string", "unsubscribeUrl": "string", "oneClick": "boolean",
    "from": "string", "subject": "string",
})
@connection("none")
@timeout(120)
async def unsubscribe_email(*, id, confirm=False, account=None, url=None, **params):
    """Unsubscribe via RFC 8058 one-click (OAuth parity), or report the URL.

    Fetches the message (`get_email` / view=om), reads List-Unsubscribe +
    List-Unsubscribe-Post. Default is dry-run (`confirm=False`): parse and
    report only — does NOT POST. Pass `confirm=True` to fire the one-click
    POST (only for throwaway lists you intend to leave).

    Args:
        id: Thread / message hex (same as get_email).
        confirm: If True and one-click is available, POST List-Unsubscribe=One-Click.
        account: Which signed-in Google account.
        url: Optional mail.google.com URL.
    """
    if not id and not url:
        return app_error("Pass email `id` or `url`.", code="BadParams")
    email = await get_email(id=id, url=url, account=account, **params)
    if _is_err(email):
        return email
    if not isinstance(email, dict):
        return app_error("get_email returned no message.", code="ProviderError")

    unsub_url = email.get("unsubscribe")
    one_click = bool(
        email.get("unsubscribeOneClick")
        or email.get("unsubscribe_one_click")
    )
    from_handle = ((email.get("from") or {}) or {}).get("handle")
    subject = email.get("name")
    base = {
        "unsubscribeUrl": unsub_url,
        "oneClick": one_click,
        "from": from_handle,
        "subject": subject,
        "listId": email.get("listId"),
        "threadId": email.get("conversationId") or id,
        "messageId": email.get("messageId"),
        "messageHex": email.get("messageHex") or email.get("_omToken"),
    }

    if not unsub_url:
        return app_error(
            f"No List-Unsubscribe header on this email (from: {from_handle}). "
            "Manual unsubscribe may be required — check the email body for a link.",
            code="NotFound",
            detail=base,
        )

    if not one_click:
        return {
            **base,
            "status": "manual_required",
            "message": (
                "This sender doesn't support one-click unsubscribe. "
                "Open unsubscribeUrl in a browser to unsubscribe."
            ),
        }

    if not confirm:
        return {
            **base,
            "status": "dry_run",
            "message": (
                "One-click unsubscribe available. Re-call with confirm=True to POST "
                "(RFC 8058). Only confirm for lists you intend to leave."
            ),
        }

    # RFC 8058: POST form body List-Unsubscribe=One-Click (no Gmail session needed).
    try:
        resp = await client.post(unsub_url, data={"List-Unsubscribe": "One-Click"})
    except Exception as e:
        return app_error(
            f"One-click unsubscribe POST failed: {e}",
            code="ProviderError",
            detail=base,
        )
    status_code = resp.get("status", 0) if isinstance(resp, dict) else 0
    ok = 200 <= int(status_code or 0) < 300
    return {
        **base,
        "status": "unsubscribed" if ok else "failed",
        "statusCode": status_code,
    }


@returns("file")
@connection("none")
@timeout(120)
async def get_attachment(*, message_id=None, attachment_id=None, filename=None,
                         mime_type=None, account=None, id=None, url=None, **params):
    """Download one attachment off a message and hydrate into the blob store.

    Re-fetches the message's raw RFC822 (`view=om` with message hex), extracts
    the part named by `attachment_id` (walk-index from `get_email`), and
    `blobs.put`s the bytes — same contract as the OAuth plugin.

    Args:
        message_id / id: Thread id, messageHex, or hex. `url` also accepted.
        attachment_id: Part index string from `get_email` attachments list.
        filename / mime_type: Hint for blob naming (from metadata).
        account: Which signed-in Google account.
    """
    mid = message_id or id
    token = _thread_token(mid, url)
    if not token or attachment_id is None:
        return app_error(
            "Pass message_id/id (thread) and attachment_id (from get_email).",
            code="BadParams",
        )
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    om_hint = params.get("messageHex") or params.get("_omToken")
    if om_hint and _is_hex(str(om_hint)):
        om_token = str(om_hint).lower()
    else:
        resolved = await _resolve_om_token(token=token, idx=idx)
        if not isinstance(resolved, dict) or not resolved.get("omToken"):
            return app_error(
                f"get_attachment needs a message hex (got {token!r}). "
                "Pass messageHex from get_email / list_emails.",
                code="BadParams",
            )
        om_token = resolved["omToken"]
    raw = await _fetch_raw_rfc822(token=om_token, idx=idx)
    if not isinstance(raw, str):
        return raw
    try:
        fname, mime, raw_b = _extract_rfc822_part(raw, str(attachment_id))
    except ValueError as e:
        return app_error(f"Attachment part not found: {e}", code="NotFound")
    fname = filename or fname
    mime = mime_type or mime
    blob = await blobs.put(
        data=base64.b64encode(raw_b).decode(),
        ext=_attachment_ext(fname, mime),
    )
    return {
        "id": str(attachment_id),
        "name": fname,
        "filename": fname,
        "mimeType": mime,
        "path": blob["path"],
        "sha": blob.get("sha256") or blob.get("sha"),
        "size": blob.get("size") or len(raw_b),
        "_omToken": om_token,
    }


# ──────────────────────────────────────────────────────────────────────
# Label mutations — archive / trash / star / mark-read
# ──────────────────────────────────────────────────────────────────────
#
# gmonkey has NO mutation verb (GmailThread/GmailMessage are read-only), and
# the clients6 gmail/v1 frontend API is a curated calendar-only surface (no
# messages.modify) walled behind cross-origin CORS. The native write is Gmail's
# own /sync/i/s "action 13" — the same POST the UI fires when you star or
# archive. Unlike a LIST request (whose body carries per-view state we can't
# forge → 400), an ACTION body is just the thread id + label delta + its message
# ids; the per-session auth rides the reused headers (`__g.hdrs`). See
# `__modifyLabels` in `_LIB`. Label codes are Gmail's system-label vocabulary.

_L_INBOX, _L_STARRED, _L_UNREAD, _L_TRASH, _L_SPAM = "^i", "^t", "^u", "^k", "^s"


async def _mutate(*, id=None, url=None, account=None, add=(), remove=()):
    """Apply a label delta to a thread via Gmail's own action, return the email.

    The thread id + its message ids come from the bv stub the thread was listed
    in (both legacy `thread-f`/`msg-f` forms), so the natural flow is list →
    act. A hex token also lets us open the thread cold to (re)load its stub.
    """
    token = _thread_token(id, url)
    if isinstance(id, str) and id.startswith("thread-"):
        thread_id = id
    elif token and _is_hex(token):
        thread_id = f"thread-f:{int(token, 16)}"
    elif token and token.startswith("thread-"):
        thread_id = token
    else:
        thread_id = None
    if not thread_id:
        return app_error("Pass an email `id` (a thread id) to act on.", code="BadParams")

    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}. Signed in: "
                f"{[a['email'] for a in accts]}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    smtp = next((normalize_email(a["email"]) for a in accts if a["index"] == idx), None)

    # Load the thread so its stub (message ids) + fresh action headers are live
    # in the tab. A hex token opens the exact thread (cold-safe); otherwise we
    # ride the bv the caller's list already captured.
    if token and _is_hex(token):
        _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#all/{token}")
        if _nav_err is not None:
            return _nav_err

    body = (
        f"const __idx = {int(idx)};"
        f"const __tid = {json.dumps(thread_id)};"
        f"const __add = {json.dumps(list(add))};"
        f"const __rem = {json.dumps(list(remove))};"
        "const __has = () => __g.bv.some((t) => { try { return String(t).indexOf(__tid) !== -1; } catch (e) { return false; } });"
        # Wait for this thread's stub (its message ids) to be in a captured bv —
        # from the caller's list, or the permalink we just opened.
        "let __dl = Date.now() + 3000;"
        "while (!__has() && Date.now() < __dl) { await new Promise(r => setTimeout(r, 200)); }"
        # Cold fallback: no stub yet (a `thread-a` id has no permalink to open) —
        # force a fresh inbox bv, which carries the recently-synced thread-a
        # stubs. Covers acting on just-arrived mail without a prior list.
        "if (!__has()) {"
        "  __g.bv = []; location.hash = '#inbox'; await new Promise(r => setTimeout(r, 300));"
        "  location.hash = '#search/in%3Aanywhere'; __dl = Date.now() + 6000;"
        "  while (!__has() && Date.now() < __dl) { await new Promise(r => setTimeout(r, 250)); }"
        "}"
        "return await __modifyLabels(__idx, __tid, __add, __rem);"
    )
    value = await _eval(body, wait_ms=30000, timeout_s=75)
    if not (isinstance(value, dict) and value.get("applied")):
        return value

    out = {"id": thread_id, "conversationId": thread_id}
    if smtp:
        out["accountEmail"] = smtp
    # Report only the flags this action actually flipped (honest post-state).
    if _L_STARRED in add:
        out["isStarred"] = True
    if _L_STARRED in remove:
        out["isStarred"] = False
    if _L_UNREAD in add:
        out["isUnread"] = True
    if _L_UNREAD in remove:
        out["isUnread"] = False
    return out


@test.skip(reason="mutates real mail")
@returns("email")
@provides("email_trash", account_param="account")
@connection("none")
@timeout(90)
async def trash_email(*, id=None, url=None, account=None, **params):
    """Move a thread to Trash — the brokered `email_trash` (Gmail's Delete).

    Adds the trash label (`^k`); Gmail drops it from Inbox and every other
    folder server-side. Recoverable from Trash for 30 days (not a hard delete).

    Args:
        id: A thread id from `list_emails` (`thread-f:…`), or Gmail's hex.
        url: A mail.google.com URL — the thread token is read from its path.
        account: Which signed-in Google account. Defaults to the first.
    """
    return await _mutate(id=id, url=url, account=account, add=[_L_TRASH])


@returns("email")
@provides("email_archive", account_param="account")
@connection("none")
@timeout(90)
@test.skip(reason="mutates real mail")
async def archive_email(*, id=None, url=None, account=None, **params):
    """Archive a thread — drop it from the Inbox; it keeps living in All Mail.

    Removes the Inbox label (`^i`) — the brokered `email_archive`.

    Args:
        id: A thread id from `list_emails` (`thread-f:…`), or Gmail's hex.
        url: A mail.google.com URL — the thread token is read from its path.
        account: Which signed-in Google account. Defaults to the first.
    """
    return await _mutate(id=id, url=url, account=account, remove=[_L_INBOX])


@returns("email")
@connection("none")
@timeout(90)
@test.skip(reason="mutates real mail")
async def modify_email(*, id=None, url=None, add_labels=None, remove_labels=None, account=None, **params):
    """Add/remove labels on a thread — the general label verb (mirrors gmail.py).

    Star (`^t`), unread (`^u`), inbox (`^i`), trash (`^k`), spam (`^s`), or any
    user label id. `archive`/`trash`/`star`/`mark_read` are named shortcuts.

    Args:
        id: A thread id from `list_emails` (`thread-f:…`), or Gmail's hex.
        add_labels: Label codes to add (e.g. `["^t"]` to star).
        remove_labels: Label codes to remove (e.g. `["^u"]` to mark read).
        account: Which signed-in Google account. Defaults to the first.
    """
    return await _mutate(id=id, url=url, account=account,
                         add=add_labels or [], remove=remove_labels or [])


@returns("email")
@connection("none")
@timeout(90)
@test.skip(reason="mutates real mail")
async def mark_read(*, id=None, url=None, account=None, unread=False, **params):
    """Mark a thread read (removes `^u`) — or unread when `unread=true`."""
    lbl = [_L_UNREAD]
    return await _mutate(id=id, url=url, account=account, **({"add": lbl} if unread else {"remove": lbl}))


@returns("email")
@connection("none")
@timeout(90)
@test.skip(reason="mutates real mail")
async def star_email(*, id=None, url=None, account=None, unstar=False, **params):
    """Star a thread (adds `^t`) — or unstar when `unstar=true`."""
    lbl = [_L_STARRED]
    return await _mutate(id=id, url=url, account=account, **({"remove": lbl} if unstar else {"add": lbl}))



# ──────────────────────────────────────────────────────────────────────
# Parity mutations — untrash / batch_* / draft lifecycle
# ──────────────────────────────────────────────────────────────────────

@test.skip(reason="mutates real mail")
@returns("email")
@connection("none")
@timeout(90)
async def untrash_email(*, id=None, url=None, account=None, **params):
    """Restore a thread from Trash — removes the trash label (`^k`)."""
    return await _mutate(id=id, url=url, account=account, remove=[_L_TRASH])


@test.skip(reason="mutates real mail")
@returns("list")
@connection("none")
@timeout(180)
async def batch_modify_email(*, ids, add_labels=None, remove_labels=None, account=None, **params):
    """Apply a label delta to many threads — loops `modify_email` per id.

    Args:
        ids: Thread ids (hex / `thread-f:…`).
        add_labels / remove_labels: Label codes (e.g. `["^t"]`, `["^u"]`).
        account: Which signed-in Google account.
    """
    if not ids:
        return app_error("Pass `ids` (a list of thread ids).", code="BadParams")
    out = []
    for i in ids:
        r = await _mutate(id=i, account=account, add=add_labels or [], remove=remove_labels or [])
        out.append(r)
    return out


@test.skip(reason="mutates real mail permanently")
@returns("list")
@connection("none")
@timeout(180)
async def batch_delete_email(*, ids, account=None, **params):
    """Move many threads to Trash (Gmail has no CDP hard-delete). Loops trash.

    Permanent delete is not exposed by the sync action surface we forge; this
    is the recoverable equivalent of Gmail Web's Delete. For true purge, open
    Trash and empty it in the UI.
    """
    if not ids:
        return app_error("Pass `ids` (a list of thread ids).", code="BadParams")
    out = []
    for i in ids:
        r = await _mutate(id=i, account=account, add=[_L_TRASH])
        out.append(r)
    return out


@test.skip(reason="writes real draft")
@returns("email")
@provides("email_draft", account_param="account")
@connection("none")
@timeout(120)
async def create_draft(*, to=None, subject=None, body=None, html_body=None, cc=None, bcc=None,
                       account=None, **params):
    """Save a draft via gmonkey compose (no send) — brokered `email_draft`.

    Opens a clean compose, fills it, waits for the draft autosave `/sync/i/s`
    (proof it has a server id), then leaves the mole. Gmail keeps it in Drafts.
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}. Signed in: "
                f"{[a['email'] for a in accts]}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    smtp = next((normalize_email(a["email"]) for a in accts if a["index"] == idx), None)
    content_html = html_body if html_body else (_text_to_html(body) if body else "<div></div>")
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#inbox")
    if _nav_err is not None:
        return _nav_err
    js = (
        f"const __to = {json.dumps(to)};"
        f"const __cc = {json.dumps(cc)};"
        f"const __bcc = {json.dumps(bcc)};"
        f"const __subject = {json.dumps(subject or '')};"
        f"const __html = {json.dumps(content_html)};"
        + _GM_LOAD +
        "const __mw = __gm.getMainWindow();"
        "const __moles = (__mw.getOpenDraftMessages && __mw.getOpenDraftMessages() || []).length;"
        "if (__moles > 0) return { status: 'moles_open', count: __moles };"
        "let __draft = __mw.createNewCompose();"
        "const __cdl = Date.now() + 6000;"
        "while ((!__draft || typeof __draft !== 'object') && Date.now() < __cdl) {"
        "  await new Promise(r => setTimeout(r, 200));"
        "  const __ds = __mw.getOpenDraftMessages && __mw.getOpenDraftMessages();"
        "  if (Array.isArray(__ds) && __ds.length) __draft = __ds[__ds.length - 1];"
        "}"
        "if (!__draft || typeof __draft !== 'object') return { __error: 'no_draft' };"
        "await new Promise(r => setTimeout(r, 300));"
        "if (__to) __draft.setTo(__to);"
        "if (__cc) __draft.setCc(__cc);"
        "if (__bcc) __draft.setBcc(__bcc);"
        "if (__subject) __draft.setSubject(__subject);"
        "__draft.setBody(__html);"
        "const __needle = String(__subject || '').trim();"
        "const __saveT0 = Date.now();"
        "let __saveBody = null;"
        "const __adl = Date.now() + 13000;"
        "while (Date.now() < __adl) {"
        "  await new Promise(r => setTimeout(r, 300));"
        "  const __news = (__g.actions || []).filter(a => a.ts >= __saveT0);"
        "  if (__needle.length >= 2) {"
        "    const __h = __news.find(a => a.body.indexOf(__needle) !== -1);"
        "    if (__h) { __saveBody = __h.body; break; }"
        "  } else if (__news.length) { __saveBody = __news[0].body; break; }"
        "}"
        "if (__saveBody === null) {"
        "  const __any = (__g.actions || []).filter(a => a.ts >= __saveT0);"
        "  if (__any.length) __saveBody = __any[__any.length - 1].body;"
        "}"
        "if (__saveBody === null) return { status: 'no_autosave' };"
        "return { status: 'drafted', subject: __draft.getSubject ? __draft.getSubject() : __subject,"
        "         to: __draft.getToEmails ? __draft.getToEmails() : null };"
    )
    for attempt in range(3):
        value = await _eval(js, wait_ms=30000, timeout_s=75)
        status = value.get("status") if isinstance(value, dict) else None
        if status == "drafted":
            out = {
                "id": None,
                "name": (value.get("subject") if isinstance(value, dict) else None) or subject or "(no subject)",
                "content": content_html,
                "content_mime": "text/html",
                "isDraft": True,
                "accountEmail": smtp,
            }
            if to:
                out["to"] = _recip_list(to)
            return out
        if status == "moles_open" or status == "no_autosave":
            await _reset_compose(idx, attempt)
            continue
        return value if value is not None else app_error("Draft create failed.", code="ProviderError")
    return app_error("Draft create couldn't autosave after clearing moles.", code="ProviderError")




@returns("email[]")
@connection("none")
@timeout(90)
async def list_drafts(*, query="", limit=25, account=None, **params):
    """List drafts — `search_emails` scoped to `in:drafts` (parity with OAuth)."""
    q = "in:drafts" + (f" {query}" if query else "")
    return await search_emails(query=q, limit=limit, account=account)


@test.skip(reason="writes real draft")
@returns("email")
@connection("none")
@timeout(120)
async def save_draft(*, to="", subject="", body="", html_body=None, cc=None, bcc=None,
                     account=None, **params):
    """Alias of `create_draft` — OAuth `email_draft` parity."""
    return await create_draft(
        to=to or None, subject=subject or None, body=body or None,
        html_body=html_body, cc=cc, bcc=bcc, account=account,
    )


@test.skip(reason="sends real mail")
@returns("email")
@connection("none")
@timeout(120)
async def send_draft(*, id=None, account=None, **params):
    """Send the currently open draft mole (gmonkey has no draft-id open).

    Gmail Live keeps drafts as open moles or Drafts-folder rows. This sends
    the newest open mole after navigating to Drafts. Pass no id — gmonkey
    drafts aren't addressable bythesync `thread-a`/`msg-a` id toward. Prefer
    `send_email` for a fresh send; this is the save→send companion to
    `create_draft` when the mole is still open.
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    smtp = next((normalize_email(a["email"]) for a in accts if a["index"] == idx), None)
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#drafts")
    if _nav_err is not None:
        return _nav_err
    js = (
        _GM_LOAD +
        "const __mw = __gm.getMainWindow();"
        "const __ds = (__mw.getOpenDraftMessages && __mw.getOpenDraftMessages()) || [];"
        "if (!__ds.length) return { status: 'no_open_draft' };"
        "const __draft = __ds[__ds.length - 1];"
        "const __subj = __draft.getSubject ? __draft.getSubject() : '';"
        "const __saveT0 = Date.now();"
        # send gate only — draft already autosaved when open
        "const __sendT0 = Date.now();"
        "try { __draft.send(); } catch (e) {"
        "  return { __error: 'send_failed', what: String(e).slice(0, 80) };"
        "}"
        "let __confirmed = false, __fired = false;"
        "const __sdl = Date.now() + 10000;"
        "while (Date.now() < __sdl) {"
        "  await new Promise(r => setTimeout(r, 300));"
        "  const __sn = (__g.actions || []).filter(a => a.ts >= __sendT0);"
        "  if (__sn.length) __fired = true;"
        "  if (__sn.some(a => a.status === 200)) { __confirmed = true; break; }"
        "}"
        "if (__confirmed) return { status: 'sent', subject: __subj };"
        "if (__fired) return { status: 'sent_unconfirmed', subject: __subj };"
        "return { status: 'send_no_confirm' };"
    )
    value = await _eval(js, wait_ms=30000, timeout_s=75)
    status = value.get("status") if isinstance(value, dict) else None
    if status in ("sent", "sent_unconfirmed"):
        return {
            "id": id,
            "name": (value.get("subject") if isinstance(value, dict) else None) or "(no subject)",
            "accountEmail": smtp,
        }
    if status == "no_open_draft":
        return app_error(
            "No open draft mole to send — create_draft leaves a mole open; if "
            "it was dismissed, re-open the draft in Gmail and retry, or use "
            "send_email for a fresh compose.",
            code="NotFound",
        )
    return value if value is not None else app_error("send_draft failed.", code="ProviderError")


@test.skip(reason="mutates real mail")
@returns("email")
@connection("none")
@timeout(90)
async def delete_draft(*, id=None, account=None, **params):
    """Discard the newest open draft mole (gmonkey has no draft close by id).

    Reloads the tab to drop every mole (same clear as compose-wedge recovery).
    Prefer `trash_email` once a Drafts list id is known from `list_emails`.
    """
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    await _reset_compose(idx, 0)
    return {"id": id, "deleted": True, "accountEmail": next(
        (normalize_email(a["email"]) for a in accts if a["index"] == idx), None
    )}



# ──────────────────────────────────────────────────────────────────────
# send_email — Gmail's OWN compose+send via gmonkey (the native write hook)
# ──────────────────────────────────────────────────────────────────────
#
# gmonkey is Gmail's official in-page API (window.gmonkey.load('2', cb)). Its
# GmailDraftMessage is the native compose: setTo/setCc/setBcc/setSubject/setBody
# → send(), riding Gmail's own authed pipeline — no DOM typing, no hand-built
# POST (which 400s). setTo takes a STRING (comma-separated); setToEmails wants
# address OBJECTS — the string setter is the one.
#
# DELIVERY — the wedge and the recipe (established live, dev/requirements.md §3).
# The root cause of flaky sends is NOT the undo-send window (the old theory).
# It's compose "moles": every send leaves its compose mole open (it never self-
# closes — verified: molesAfter == 1 right after a confirmed send), moles
# ACCUMULATE, and even ONE stale mole blocks the next draft's autosave — a fresh
# draft then has no server id, so send() silently no-ops with nothing on the
# wire. From a CLEAN (0-mole) state gmonkey delivers in well under 2s. So the
# recipe this function implements — each step gated on Gmail's OWN traffic
# (`__g.actions`, the /sync/i/s capture), never a fixed sleep:
#   1. CLEAN-STATE GATE: if getOpenDraftMessages() > 0, bail → the caller clears
#      moles via a cross-document reload to a compose-free URL (_reset_compose)
#      and retries clean.
#   2. createNewCompose → poll getOpenDraftMessages for the draft → fill it.
#   3. AUTOSAVE GATE: wait for the draft-save /sync/i/s POST (its body carries the
#      subject) — proof the draft has a server id and send() won't no-op.
#   4. send().
#   5. SEND GATE: wait for the post-send /sync/i/s 200 — the on-the-wire proof the
#      message left (validated end-to-end: a self-send then appears in the atom
#      feed). No new action at all after send() ⇒ a no-op wedge ⇒ reload + retry.
# The whole thing runs under a reactive retry: a recoverable status (moles_open /
# no_autosave / send_no_confirm) reloads the tab and re-composes from clean.

# Load + cache gmonkey's API on the window; bail if it never comes up.
_GM_LOAD = (
    "const __gm = window.__agmGm || (window.__agmGm = await new Promise((res) => {"
    "  try { window.gmonkey.load('2', res); } catch (e) { res(null); } }));"
    "if (!__gm) return { __error: 'no_gmonkey' };"
)


def _recip_list(csv):
    out = []
    for part in re.split(r"[,;]", str(csv or "")):
        h = part.strip()
        if "@" in h:
            out.append({"handle": h, "platform": "email", "displayName": None})
    return out


def _text_to_html(text):
    esc = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "<div>" + esc.replace("\n", "<br>") + "</div>"


@test.skip(reason="sends real mail")
@returns("email")
@provides("email_send", account_param="account")
@connection("none")
@timeout(120)
async def send_email(*, to, subject, body, html_body=None, cc=None, bcc=None, account=None,
                     attachments=None, **params):
    """Send a new email as the Google account, via Gmail's own compose (gmonkey).

    Drives GmailDraftMessage (setTo/setSubject/setBody → send) — the request
    rides Gmail's authed pipeline and the copy lands in Sent. Mirrors the gmail
    plugin's `email_send` contract.

    Args:
        to: Recipient address(es), comma/semicolon separated.
        subject: Subject line.
        body: Plain-text body (used when html_body is absent).
        html_body: HTML body — wins over `body` when present.
        cc: Cc address(es), comma/semicolon separated.
        bcc: Bcc address(es), comma/semicolon separated.
        account: Which signed-in Google account to send as. Defaults to the first.
        attachments: [{filename, mimeType, path?|content?}] — shared
            mail-attachments wire. Injected via page-JS File+DataTransfer into
            Gmail's real `input[type=file][name=Filedata]` (Gmail uploads before
            autosave). `path` reads the blob store; `content` is base64 bytes.
    """
    return await _compose_and_deliver(
        to=to, subject=subject, body=body, html_body=html_body,
        cc=cc, bcc=bcc, account=account, attachments=attachments,
    )


@test.skip(reason="sends real mail")
@returns("email")
@connection("none")
@timeout(120)
async def forward_email(*, to, subject, body, html_body=None, cc=None, bcc=None,
                        thread_id=None, account=None, attachments=None, **params):
    """Forward an email as a new "Fwd:" message via Gmail's own compose (gmonkey).

    A forward is a STANDALONE new message, so gmonkey (compose-new + send) is the
    right tool — no threading needed. The caller passes the "Fwd:" subject and the
    quoted original (built from `get_email`) as `body`/`html_body`, exactly like
    the gmail (OAuth) plugin's `forward_email`. Mirrors that signature.

    Args:
        to: Recipient address(es), comma/semicolon separated.
        subject: Subject line (usually "Fwd: …").
        body: Plain-text body — the quoted original + any note (used when
            html_body is absent).
        html_body: HTML body — wins over `body` when present.
        cc / bcc: Additional recipients, comma/semicolon separated.
        thread_id: Accepted for signature parity with the OAuth plugin but NOT
            honored on this path — gmonkey can't thread, and a forward is a new
            thread anyway (the OAuth plugin only keeps it in-thread when a caller
            passes thread_id, which is unusual for a forward).
        account: Which signed-in Google account to send as. Defaults to the first.
        attachments: Same as send_email — File+DataTransfer into Gmail's file input.
    """
    return await _compose_and_deliver(
        to=to, subject=subject, body=body, html_body=html_body,
        cc=cc, bcc=bcc, account=account, attachments=attachments,
    )


async def _resolve_attachments(attachments):
    """Outbound refs → [{filename, mimeType, content_b64}] for page-JS File inject.

    Same contract as the OAuth plugin / mail-attachments: `path` (blob store)
    or `content` (base64). Path wins. Empty refs skipped."""
    out = []
    for att in attachments or []:
        filename = att.get("filename") or att.get("name") or "attachment"
        mime_type = att.get("mimeType") or "application/octet-stream"
        path = att.get("path")
        content = att.get("content")
        if path:
            raw = base64.b64decode((await blobs.get(path=path))["data"])
            content_b64 = base64.b64encode(raw).decode()
        elif content:
            content_b64 = content if isinstance(content, str) else base64.b64encode(content).decode()
        else:
            continue
        out.append({"filename": filename, "mimeType": mime_type, "content": content_b64})
    return out


async def _compose_and_deliver(*, to, subject, body, html_body, cc, bcc, account, attachments):
    """Shared compose+send core for send_email and forward_email: resolve the
    account, drive Gmail's gmonkey compose under the reactive retry, and return
    the `email` shape on a wire-confirmed send (else the hard-error to surface).
    Attachments ride Gmail's real file input via page-JS File+DataTransfer
    (verified live — Gmail uploads them before autosave)."""
    resolved_atts = await _resolve_attachments(attachments) if attachments else []
    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}. Signed in: "
                f"{[a['email'] for a in accts]}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    smtp = next((normalize_email(a["email"]) for a in accts if a["index"] == idx), None)
    content_html = html_body if html_body else _text_to_html(body)
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#inbox")
    if _nav_err is not None:
        return _nav_err
    sent, detail = await _gmonkey_compose_send(
        idx=idx, to=to, subject=subject or "", content_html=content_html, cc=cc, bcc=bcc,
        attachments=resolved_atts,
    )
    out = _sent_email(subject, content_html, smtp, to, cc, bcc) if sent else detail
    if sent and resolved_atts and isinstance(out, dict):
        out["hasAttachments"] = True
        out["attachments"] = [
            {"filename": a["filename"], "mimeType": a["mimeType"]} for a in resolved_atts
        ]
    return out


async def _gmonkey_compose_send(*, idx, to, subject, content_html, cc, bcc, attachments=None):
    """The gated gmonkey compose+send under a reactive retry (dev/requirements.md §3).
    Returns (True, status) once a send confirms on the wire; (False, detail) with
    the hard-error/app_error to surface otherwise. Each wait gates on Gmail's OWN
    /sync/i/s traffic (`__g.actions`), never a fixed sleep; a recoverable wedge
    (moles_open / no_autosave / send_no_confirm) is cleared by a cross-document
    reload (`_reset_compose`) + retry."""
    js = _compose_send_js(to=to, cc=cc, bcc=bcc, subject=subject or "", html=content_html,
                          attachments=attachments or [])
    RECOVERABLE = {"moles_open", "no_autosave", "send_no_confirm", "no_file_input", "attach_not_confirmed", "attach_failed"}
    for attempt in range(3):
        value = await _eval(js, wait_ms=30000, timeout_s=90)
        status = value.get("status") if isinstance(value, dict) else None
        if status in ("sent", "sent_unconfirmed"):
            return True, value
        if status in RECOVERABLE:
            await _reset_compose(idx, attempt)  # discard moles → clean tab
            continue
        # Anything else is a hard error already shaped by `_eval` (no_gmonkey,
        # no_draft, recipient_not_set, send_failed) — surface it, don't retry.
        return False, value
    return False, app_error(
        "Gmail send couldn't be confirmed on the wire after clearing the compose "
        "state and retrying — the compose subsystem may be wedged. Reload "
        "mail.google.com and try again.", code="ProviderError",
    )


async def _reset_compose(idx, nonce):
    """Clear ALL open compose "moles" by forcing a cross-document reload to a
    compose-free URL.

    A mole lives in the URL #fragment (`#inbox?compose=<id>`), and it — not the
    URL — is the source of truth: Gmail re-syncs the fragment FROM the open mole,
    so `location.hash = '#inbox'` gets reverted, and a plain same-URL reload just
    re-opens the compose the fragment names. The only reset that works is a real
    document load landing on a URL with NO compose param. To force a cross-
    document navigation (not a same-document fragment change that wouldn't
    reload), the pre-#fragment part must differ — hence the `?aosc=<nonce>`
    buster, varied per attempt so each retry's reload is genuinely a fresh load.
    Verified live: moles → 0 after this. (gmonkey exposes no compose close/discard.)
    """
    await browser_session.navigate(
        _TARGET, f"https://mail.google.com/mail/u/{idx}/?aosc={nonce}#inbox"
    )


def _sent_email(subject, content_html, smtp, to, cc, bcc):
    """The `email` shape for a confirmed send (keys mirror gmail.py::email_send)."""
    return {
        "id": None,
        "name": subject or "(no subject)",
        "content": content_html,
        "content_mime": "text/html",
        "published": None,
        "isUnread": False,
        "isStarred": False,
        "from": ({"handle": smtp, "platform": "email", "displayName": None} if smtp else None),
        "to": _recip_list(to),
        "copied_to": _recip_list(cc),
        "bcc": _recip_list(bcc),
        "accountEmail": smtp,
    }


# ══════════════════════════════════════════════════════════════════════
# reply_email / reply_all_email — Gmail's OWN threaded-reply init
# ══════════════════════════════════════════════════════════════════════
#
# SOLVED 2026-07-09 (no forge; send token mints via gmonkey). Status:
# mechanism shipwired. Verified live REPLYVERIFY-J1 / REPLYWIRE-* same
# conversationId.
#
# Open (Wall 4): `#all/{hex}` / `#inbox/{hex}` do NOT open a conversation —
# only `#inbox/<permId>` does, and permId is never in the bv/DOM. List-row
# controllers do NOT reconstruct through `_m.jLn` (cold-boot wrap → 0 row
# captures; rows ride a different base). What DOES open, from page JS, is
# a DOM `row.click()` on `[data-legacy-thread-id=hex]` after the list
# surfaces it (`#search/{hex}` keeps the row present). Gmail accepts that
# programmatic click (unlike synthetic Reply), lands on `#inbox/<permId>`,
# and mints message-view controllers. A graph `url` that already carries a
# permId skips the list and hashes straight to `#inbox/<permId>`.
#
# Reply (the proven deep hook): wrap `_m.jLn` → open → filter `__c` for
# message-view CTRLs (`Za`+`Ca`+`ha`) → call CLEAN `_m.EQn(ctrl, mode)`
# (NEVER wrap EQn — double-wrap chains into a stack-overflow cycle) →
# the compose IS a gmonkey mole (`getOpenDraftMessages()[last]`) →
# setBody (prepend to Gmail's quote) → autosave-gate + send-gate (same
# `/sync/i/s` contract as `_compose_send_js`). Full ladder: dev/requirements.md §4,
# commons/re/toolkit.md.
#
# Account: efisio@gmail.com. Re-validation target: REPLYVERIFY-J1
# (thread-a:r-585664302285623793 · hex 19f48a0360c4a115).

_L_REPLY, _L_REPLY_ALL = "r", "a"


def _reply_open_token(token):
    """Classify a resolved token: hex | thread | perm | bad.

    hex    → `#search/{hex}` + click `[data-legacy-thread-id]`
    thread → `#search/in:anywhere` + click `[data-thread-id=#thread-…]`
    perm   → navigate `#inbox/{perm}` (from a prior open / caller url)
    """
    if not token:
        return None, None
    if _is_hex(token):
        return "hex", token.lower()
    if token.startswith("thread-a:") or token.startswith("thread-f:"):
        return "thread", token
    # Gmail permIds are long alnum (Ktbx…); never hex-only.
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{15,}", token):
        return "perm", token
    return "bad", token


def _reply_flow_js(*, mode, html, to=None, cc=None, bcc=None, subject=None,
                   open_kind, open_token):
    """Thin call into the durable page helper `__agmail.reply` (see `_LIB`)."""
    opts = {
        "mode": mode,
        "html": html,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "openKind": open_kind,
        "openToken": open_token,
    }
    return f"return await __g.reply({json.dumps(opts)});"


def _reply_status_error(status, value=None, *, retried=False):
    """Map a reply-flow status code to a specific, actionable app_error."""
    value = value or {}
    extra = f" (after retries)" if retried else ""
    what = value.get("what")
    hash_ = value.get("hash")
    msgs = {
        "no_row": (
            f"Couldn't find a list row for this thread in Gmail{extra}. The id may "
            "be stale, trashed, or not in the first base of `in:anywhere` results. "
            "Pass a hex id from list_emails/get_email or a mail.google.com url.",
            "NotFound",
        ),
        "no_ctrl": (
            f"Opened the conversation but never captured a message-view controller{extra}. "
            f"hash={hash_!r}. Gmail may have reused a pooled view — retry usually works.",
            "ProviderError",
        ),
        "moles_open": (
            f"Open compose mole(s) block the reply path{extra} (count="
            f"{value.get('count')}). Cleared on retry via a compose-free reload.",
            "ProviderError",
        ),
        "no_mole": (
            f"Gmail's reply compose never opened after EQn{extra}. The message-view "
            "controller was held but gmonkey saw no draft mole — compose subsystem "
            "may be wedged (reset happens on retry).",
            "ProviderError",
        ),
        "no_autosave": (
            f"Reply draft never autosaved to the server{extra} — send would no-op. "
            "Usually a leftover compose mole; retried with a clean reload.",
            "ProviderError",
        ),
        "send_no_confirm": (
            f"Reply send produced no /sync/i/s traffic{extra} — Gmail no-op'd the send. "
            "Compose may be wedged; retried with a clean reload.",
            "ProviderError",
        ),
        "eqn_throw": (
            f"Gmail's reply-init (_m.EQn) threw{extra}"
            + (f": {what}" if what else ".")
            + " Possible double-wrap of EQn from a prior RE session — connector unwraps "
            "one layer; a hard tab reload clears it.",
            "ProviderError",
        ),
        "no_jln": (
            "Gmail's controller base (_m.jLn) isn't on the page — the tab isn't a "
            "booted Gmail app (wrong URL or still loading).",
            "NotReady",
        ),
        "no_eqn": (
            "Gmail's reply-init (_m.EQn) is missing — build/change or the tab isn't "
            "the main Gmail surface.",
            "NotReady",
        ),
        "bad_open": (
            f"Internal open kind {value.get('kind')!r} is unsupported.",
            "BadParams",
        ),
    }
    msg, code = msgs.get(
        status,
        (f"Reply failed with status {status!r}{extra}.", "ProviderError"),
    )
    return app_error(msg, code=code, status=status)


async def _reply(*, id=None, url=None, account=None, mode, to=None, cc=None, bcc=None,
                  subject=None, body, html_body=None, **params):
    """Open the thread, drive Gmail's own reply-init, fill, gated-send."""
    token = _thread_token(id, url)
    if not token:
        return app_error(
            "Pass an email `id` (thread id or hex) or `url` to reply to.",
            code="BadParams",
        )
    kind, open_token = _reply_open_token(token)
    if kind not in ("hex", "perm", "thread"):
        return app_error(
            f"Couldn't resolve {token!r} to a Gmail hex, thread id, or conv permId.",
            code="BadParams",
        )

    idx, accts = await _resolve_index(account)
    if idx is None:
        return (
            app_error(
                f"No signed-in Gmail account matches {account!r}. Signed in: "
                f"{[a['email'] for a in accts]}.", code="NotFound",
            ) if account else app_error("No Gmail account is signed in.", code="NeedsAuth")
        )
    smtp = next((normalize_email(a["email"]) for a in accts if a["index"] == idx), None)
    content_html = html_body if html_body else (_text_to_html(body) if body else None)
    if not content_html:
        return app_error("Pass a `body` (or `html_body`) to reply with.", code="BadParams")

    # Land on the right account first.
    _nav_err = await _nav(f"https://mail.google.com/mail/u/{idx}/#inbox")
    if _nav_err is not None:
        return _nav_err

    RECOVERABLE = {"no_row", "no_ctrl", "no_mole", "no_autosave", "send_no_confirm", "eqn_throw", "moles_open"}
    detail = None
    for attempt in range(3):
        js = _reply_flow_js(
            mode=mode, html=content_html, to=to, cc=cc, bcc=bcc, subject=subject,
            open_kind=kind, open_token=open_token,
        )
        value = await _eval(js, wait_ms=30000, timeout_s=90)
        status = value.get("status") if isinstance(value, dict) else None
        if status in ("sent", "sent_unconfirmed"):
            out = {
                "id": open_token if kind in ("hex", "thread") else token,
                "conversationId": open_token if kind in ("hex", "thread") else token,
            }
            if smtp:
                out["accountEmail"] = smtp
            if isinstance(value, dict):
                if value.get("subject"):
                    out["name"] = value["subject"]
                if value.get("to"):
                    out["to"] = [
                        {"handle": t.get("address"), "platform": "email",
                         "displayName": t.get("name") or None}
                        for t in value["to"]
                    ]
            return out
        if _is_err(value):
            return value
        if status in RECOVERABLE:
            await _reset_compose(idx, attempt)
            detail = value
            continue
        if isinstance(value, dict) and status:
            return _reply_status_error(status, value)
        return value if value is not None else app_error(
            "Reply failed with no detail.", code="ProviderError"
        )
    if isinstance(detail, dict) and detail.get("status"):
        return _reply_status_error(detail["status"], detail, retried=True)
    return app_error(
        "Gmail's reply couldn't be confirmed on the wire after retrying. Reload "
        "mail.google.com and try again.",
        code="ProviderError",
    )


@test.skip(reason="sends real mail")
@returns("email")
@provides("email_reply", account_param="account")
@connection("none")
@timeout(150)
async def reply_email(*, id=None, url=None, body, html_body=None, subject=None,
                      to=None, cc=None, bcc=None, thread_id=None, in_reply_to=None,
                      references=None, account=None, **params):
    """Reply to an email, in-thread, via Gmail's own reply-init.

    Opens the conversation (list-row DOM click for hex, or `#inbox/<permId>`
    when a permId url is given), captures the message-view controller by
    wrapping `_m.jLn`, then calls `_m.EQn(ctrl,'r')` so Gmail mints a
    thread-attached gmonkey compose. Fill + autosave/send gates ride the same
    `/sync/i/s` wire-confirm path as `send_email`.

    Args:
        id: Thread id from `list_emails` (`thread-f:…` or legacy hex).
        url: A mail.google.com URL — hex or `#inbox/<permId>` accepted.
        body: Plain-text body (used when html_body is absent).
        html_body: HTML body — wins over `body` when present.
        subject: Overrides Gmail's own "Re: …". Usually omit.
        to / cc / bcc: Overrides; usually omit — Gmail fills from the thread.
        thread_id / in_reply_to / references: Signature parity only — Gmail
            sets real threading headers; not honored here.
        account: Which signed-in Google account. Defaults to the first.
    """
    return await _reply(id=id, url=url, account=account, mode=_L_REPLY,
                        to=to, cc=cc, bcc=bcc, subject=subject, body=body, html_body=html_body)


@test.skip(reason="sends real mail")
@returns("email")
@provides("email_reply", account_param="account")
@connection("none")
@timeout(150)
async def reply_all_email(*, id=None, url=None, body, html_body=None, subject=None,
                          to=None, cc=None, bcc=None, thread_id=None, in_reply_to=None,
                          references=None, account=None, **params):
    """Reply-all in-thread — same mechanism as `reply_email` with mode `a`.

    Args: same as `reply_email`.
    """
    return await _reply(id=id, url=url, account=account, mode=_L_REPLY_ALL,
                        to=to, cc=cc, bcc=bcc, subject=subject, body=body, html_body=html_body)



def _compose_send_js(*, to, cc, bcc, subject, html, attachments=None):
    """Thin call into durable `__agmail.composeSend` (attachments included)."""
    opts = {
        "to": to, "cc": cc, "bcc": bcc, "subject": subject, "html": html,
        "attachments": attachments or [],
    }
    return f"return await __g.composeSend({json.dumps(opts)});"


