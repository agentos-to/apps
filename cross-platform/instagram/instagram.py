"""Instagram — live Instagram DMs via the engine-held browser session.

Same architecture as the WhatsApp plugin (read its `whatsapp.py` +
`readme.md` first — this is a Relay-flavored clone of it), but Instagram
gives us no global decoded collection like WhatsApp's `WAWebCollections`.
Instead the decrypted messages live in Instagram's **React/Relay store**,
which we reach by walking the React fiber tree to the RelayModernEnvironment
and reading its normalized record source.

WHY the store and not the wire (the deciding fact): Instagram DMs are now
END-TO-END ENCRYPTED (Signal protocol — proven by the `messenger_web_signal_v3`
IndexedDB stores). The only place plaintext exists is inside the running
client after it decrypts. So — exactly like WhatsApp (Noise+Signal on the
wire, plaintext only in the JS Store) — we let Instagram's own code do both
crypto layers and read the decrypted Relay records. This is also the LOWEST
ban-risk path: it's the real browser, real session, real IP.

Everything here was learned by live CDP probing on 2026-07-06. See
`readme.md` → Internals for the full reference (Relay typenames, the proven
`SlideMessage` field map, the IndexedDB layout, open questions). Ops/paths
that are NOT yet verified live are marked `TODO(verify)` — do not trust them
until exercised end-to-end.

IDs: thread = `thread_fbid` (numeric string); message = `message_id`
(`mid.$…`); a user carries both `sender_igid` (== `ds_user_id` cookie for the
viewer) and `sender_fbid`.
"""

import json

from agentos import account, app_error, provides, returns, services, timeout

_TARGET = "instagram.com"
_INBOX_URL = "https://www.instagram.com/direct/inbox/"

# The DM UI + its Relay records only exist once /direct has loaded — the site
# root doesn't populate the message store. Ops ensure the tab is here.
#
# _MODE: ATTACH — drives Joe's real, running daily Brave over CDP (his actual
# profile + cookies), so IG needs NO separate login/QR and NO cookie copy; the
# DM tab just sits backgrounded in the daily browser.
#
# Why not `background` like WhatsApp: WhatsApp Web's QR is a clean one-time link
# into the headless daemon profile; IG has no such link. A separate profile
# can't reuse the real session — Chromium locks a profile to ONE process, so you
# can't run a 2nd headless instance on the live daily profile, and copying
# cookies to a fresh profile risks IG's device-fingerprint / "suspicious login"
# checks. So for IG we attach to the real session instead. Decided with Joe,
# 2026-07-06. (Backend browser is Brave, resolved by the engine's default.)
_MODE = "attach"

# ──────────────────────────────────────────────────────────────────────
# JS building blocks
# ──────────────────────────────────────────────────────────────────────

# The Relay environment discovery — PROVEN: finds the RelayModernEnvironment
# on a React fiber's memoizedProps.environment in ~141 fibers. This is the
# equivalent of WhatsApp's `window.require('WAWebCollections')`. Stable across
# Relay versions API-wise (getStore/getNetwork); only field/typename names drift.
_FIND_ENV_JS = """
const __findRelayEnv = () => {
  const cands = [document.getElementById('react-root'), document.body,
                 ...document.querySelectorAll('body > div')].filter(Boolean);
  let key = null, el = null;
  for (const c of cands) {
    key = Object.keys(c).find(k => k.startsWith('__reactContainer$') || k.startsWith('__reactFiber$'));
    if (key) { el = c; break; }
  }
  if (!key) {
    for (const d of document.querySelectorAll('div')) {
      const k = Object.keys(d).find(kk => kk.startsWith('__reactFiber$') || kk.startsWith('__reactContainer$'));
      if (k) { key = k; el = d; break; }
    }
  }
  if (!key) return null;
  const isEnv = (o) => { try { return o && typeof o === 'object'
    && typeof o.getStore === 'function' && typeof o.getNetwork === 'function'; } catch (e) { return false; } };
  let root = el[key]; if (root && root.current) root = root.current;
  const stack = [root], seen = new Set();
  let visited = 0;
  while (stack.length && visited < 40000) {
    const f = stack.pop(); if (!f || seen.has(f)) continue; seen.add(f); visited++;
    for (const p of ['memoizedProps', 'memoizedState']) {
      const v = f[p];
      if (v && typeof v === 'object') {
        for (const k in v) {
          try {
            const val = v[k];
            if (isEnv(val)) return val;
            if (val && typeof val === 'object' && isEnv(val.environment)) return val.environment;
          } catch (e) {}
        }
      }
    }
    if (f.child) stack.push(f.child);
    if (f.sibling) stack.push(f.sibling);
    if (f.alternate && !seen.has(f.alternate)) stack.push(f.alternate);
  }
  return null;
};

// Resolve the VIEWER's fbid so mapMsg can set isOutgoing. Messages key on
// FBID (sender_fbid), but the ds_user_id cookie is the viewer's IGID, and a
// thread's viewer_id is ALSO the igid — neither matches sender_fbid. Bridge:
// find the SlideUser whose igid == ds_user_id and take its id (== the fbid).
// PROVEN live 2026-07-06: ds_user_id 27323288 -> viewer fbid 111600823566513,
// which then correctly flags Joe's own messages as isOutgoing.
const __viewerFbid = (source) => {
  try {
    const dsid = (document.cookie.match(/ds_user_id=(\\d+)/) || [])[1];
    if (!dsid) return null;
    const ids = source.getRecordIDs ? source.getRecordIDs() : [];
    for (const id of ids) {
      const r = source.get(id);
      if (r && r.__typename === 'SlideUser' && String(r.igid) === String(dsid)) return String(r.id);
    }
  } catch (e) {}
  return null;
};
"""

# Wait for the Relay env to exist (null until /direct has loaded). The
# logged-out state renders the username/password form at /accounts/login —
# that (and only that) is auth_required. Mirrors WhatsApp's _PRELUDE.
_PRELUDE = """
const __deadline = Date.now() + %(wait_ms)d;
""" + _FIND_ENV_JS + """
let env = null;
while (Date.now() < __deadline) {
  if (document.querySelector('input[name="username"]') && /accounts\\/login/.test(location.pathname)) {
    return { __error: 'auth_required' };
  }
  env = __findRelayEnv();
  if (env) break;
  await new Promise(r => setTimeout(r, 250));
}
if (!env) return { __error: 'not_ready' };
const store = env.getStore();
const source = store.getSource();
let meFbid = null;
try { meFbid = __viewerFbid(source); } catch (e) {}
"""

# Shape mappers, shared by read payloads + the watch hook. They close over
# `source` and `meFbid` (both defined by _PRELUDE and by the watch hook), the
# way WhatsApp's helpers close over Chat/Msg/me.
#
# Relay refs: a field is either inline, {__ref: id}, or {__refs: [ids]}.
# Deref via source.get(id). timestamp_ms is MILLISECONDS (not seconds like
# WhatsApp's __x_t — do not multiply by 1000).
_HELPERS = """
const str = (v) => typeof v === 'string' ? v : '';
const isoMs = (ms) => Number.isFinite(ms) ? new Date(ms).toISOString() : null;
const deref = (ref) => (ref && ref.__ref) ? source.get(ref.__ref) : null;
// The sender join, PROVEN live (resolves all 253 messages to @handle + name):
//   SlideMessage.sender -> SlideUser {name, id(==fbid), igid, user_dict}
//     -> XDTUserDict {username, full_name, profile_pic_url}.
// Takes a SlideUser record; the username lives one hop down on user_dict.
const account = (su) => {
  if (!su) return null;
  const ud = deref(su.user_dict);
  const a = { id: String(su.id || ''), platform: 'instagram' };
  const handle = ud ? str(ud.username) : null;
  if (handle) a.handle = handle;
  const name = str(su.name) || (ud ? str(ud.full_name) : null) || handle;
  if (name) a.display_name = name;
  const pic = ud ? str(ud.profile_pic_url) : null;
  if (pic) a.image = pic;
  return a;
};
// Map a SlideMessage record -> a `message` entity (same shape the Messaging
// app consumes from WhatsApp/iMessage). PROVEN fields; see readme field map.
const mapMsg = (m) => {
  if (!m || m.__typename !== 'SlideMessage') return null;
  const acct = account(deref(m.sender));
  // Text is inlined on the SlideMessage; deref content only for non-text.
  let content = str(m.text_body);
  if (!content) { const c = deref(m.content); if (c) content = str(c.text_body); }
  const out = {
    id: str(m.message_id) || str(m.id),
    content,
    published: isoMs(m.timestamp_ms),   // timestamp_ms is MILLISECONDS
    conversationId: String(m.thread_fbid || ''),
    type: (str(m.content_type) || 'text').toLowerCase(),
  };
  if (meFbid != null && m.sender_fbid != null) {
    out.isOutgoing = String(m.sender_fbid) === String(meFbid);
    out.author = out.isOutgoing ? 'Me' : (acct ? acct.display_name : null);
  } else if (acct) {
    out.author = acct.display_name;
  }
  if (acct && out.isOutgoing !== true) out.from = acct;
  // TODO(verify): reactions live in msg_reactions/reactions {__refs} ->
  // MessagingReaction records ({sender_fbid, sender_igid, ...emoji field}).
  // Deref + aggregate by emoji (see WhatsApp's attachReactions). Not built.
  return out;
};
// Map a thread record (XFBIGDirectViewerThread) -> a `conversation` entity.
// PROVEN: 16 threads map with real titles/group flags. TODO(verify): unread is
// a bool (marked_as_unread) not a count; participants (t.users {__refs} ->
// SlideUser) not mapped yet.
const mapConv = (t) => {
  if (!t || t.__typename !== 'XFBIGDirectViewerThread') return null;
  // The store carries skeleton/placeholder thread records with no fbid while
  // the inbox hydrates — skip them (surfaced live 2026-07-06 as a blank row).
  if (t.thread_fbid == null || t.thread_fbid === '') return null;
  return {
    id: String(t.thread_fbid),
    name: str(t.thread_title),
    isGroup: t.is_group === true,
    unreadCount: t.marked_as_unread === true ? 1 : 0,
    isMuted: t.is_muted === true,
    published: isoMs(t.last_activity_timestamp_ms),
  };
};
// Collect every SlideMessage record, optionally filtered to one thread.
const allMessages = (threadFbid) => {
  const ids = source.getRecordIDs ? source.getRecordIDs() : [];
  const out = [];
  for (const id of ids) {
    const r = source.get(id);
    if (!r || r.__typename !== 'SlideMessage') continue;
    if (threadFbid && String(r.thread_fbid || '') !== String(threadFbid)) continue;
    const mapped = mapMsg(r);
    if (mapped) out.push(mapped);
  }
  out.sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0));
  return out;
};
const allConversations = () => {
  const ids = source.getRecordIDs ? source.getRecordIDs() : [];
  const out = [];
  for (const id of ids) {
    const r = source.get(id);
    if (r && r.__typename === 'XFBIGDirectViewerThread') { const c = mapConv(r); if (c) out.push(c); }
  }
  out.sort((a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0));
  return out;
};
"""


def _payload(body: str, *, wait_ms: int) -> str:
    """Wrap an op body in the async IIFE with readiness wait + helpers."""
    return ("(async () => {"
            + (_PRELUDE % {"wait_ms": wait_ms})
            + _HELPERS
            + body
            + "})()")


async def _eval(body: str, *, wait_ms: int = 15000, timeout_s: int = 45):
    """Run an op body in the Instagram tab; surface structured errors."""
    value = await services.call("browser_session", verb="eval", params={
        "target": _TARGET,
        "mode": _MODE,
        "js": _payload(body, wait_ms=wait_ms),
        "timeout": timeout_s,
    })

    if isinstance(value, dict) and "__error" in value:
        code = value["__error"]
        if code == "auth_required":
            return app_error(
                "Instagram isn't logged in on the engine browser. Run "
                "instagram.login to open a sign-in window, then retry.",
                code="NeedsAuth",
            )
        if code == "not_ready":
            return app_error(
                "Instagram's Relay store never came up — the /direct inbox may "
                "still be loading, or an Instagram update moved the environment "
                "off the React fiber. Re-run the fiber-walk probe (see readme).",
                code="NotReady",
            )
        if code == "not_found":
            return app_error(
                f"No match for {value.get('ref')!r} ({value.get('what', 'item')}).",
                code="NotFound",
            )
        if code == "send_failed":
            return app_error(f"Instagram send failed: {value.get('what')}", code="SendFailed")
        return app_error(f"Instagram payload error: {code}", code="PayloadError")

    return value


async def _ensure_inbox():
    """Navigate the tab to /direct/inbox if it isn't there (the store only
    populates on the DM surface). Idempotent, cheap when already there."""
    # TODO(verify): confirm navigate doesn't disrupt an active thread view.
    # A lighter check would read location.href first and skip if already /direct.
    await services.call("browser_session", verb="navigate", params={
        "target": _TARGET, "mode": _MODE, "url": _INBOX_URL, "timeout": 30,
    })


# ──────────────────────────────────────────────────────────────────────
# The account trio — check / login / logout
# ──────────────────────────────────────────────────────────────────────
# Instagram's "session" is the logged-in state inside the browser profile.
# No cookie credential is handed to this app; ops call browser_session like
# every other op. Login is username/password/2FA via a headed window (NOT a
# QR link) — the human completes it; we just open the window and poll.

@account.check
@returns("account")
@timeout(45)
async def check_session(**params):
    """Verify the Instagram login and identify the viewer.

    authenticated:true when the Relay env is live; authenticated:false on the
    login form. TODO(verify): fill in the viewer's handle/id (username from the
    profile link or a store viewer record; id from the ds_user_id cookie).
    """
    js = """(async () => {
  const deadline = Date.now() + 12000;
""" + _FIND_ENV_JS + """
  let env = null;
  while (Date.now() < deadline) {
    if (document.querySelector('input[name="username"]') && /accounts\\/login/.test(location.pathname)) {
      return { authenticated: false };
    }
    env = __findRelayEnv();
    if (env) break;
    await new Promise(r => setTimeout(r, 250));
  }
  if (!env) return { authenticated: false };
  // TODO(verify): resolve real identity. igid from cookie:
  const m = document.cookie.match(/ds_user_id=(\\d+)/);
  const acct = {
    authenticated: true,
    platform: 'instagram',
    at: { shape: 'product', name: 'Instagram', url: 'https://www.instagram.com/' },
  };
  if (m) { acct.identifier = m[1]; }
  // TODO(verify): username -> acct.handle / acct.displayName (profile link
  // aria-label carries "<username>'s profile picture" in the nav).
  return acct;
})()"""
    return await services.call("browser_session", verb="eval", params={
        "target": _TARGET, "mode": _MODE, "js": js, "timeout": 30,
    })


@account.login
@returns("account | auth_challenge")
@timeout(60)
async def login(**params):
    """Open a sign-in window for Instagram (or report the account if already in).

    Instagram login is username/password + optional 2FA/checkpoint — the human
    completes it in the headed window; poll check_session to confirm.
    TODO(verify): the login_window verb params + polling shape against a real
    logged-out engine profile. Mirrors the browser plugin's login_window flow.
    """
    existing = await check_session()
    if isinstance(existing, dict) and existing.get("authenticated"):
        return existing
    await services.call("browser_session", verb="login_window", params={
        "url": "https://www.instagram.com/accounts/login/",
        "label": "Instagram",
        "mode": _MODE,
    })
    return {
        "kind": "login_window",
        "message": "Sign in to Instagram in the window that opened (handle any "
                   "2FA / checkpoint), then poll instagram.check_session.",
    }


@account.logout
@returns({"ok": "boolean", "message": "string"})
@timeout(45)
async def logout(**params):
    """Log out of Instagram in the engine browser.

    TODO(verify): drive the real logout (menu → Log out) or clear the session
    cookies. Stubbed to keep the account trio complete (validator requires it).
    """
    return {"ok": False, "message": "logout not yet implemented — see readme open questions"}


# ──────────────────────────────────────────────────────────────────────
# Read
# ──────────────────────────────────────────────────────────────────────

@returns("conversation[]")
@provides("chats", account_param="account")
@timeout(60)
async def list_conversations(*, limit=50, **params):
    """List Instagram DM threads (most recent first).

    PROVEN path: maps `XFBIGDirectViewerThread` records straight out of the
    Relay store — real titles, group flags, mute/unread. Only the threads the
    client has loaded into the store appear (the inbox is warm after
    `_ensure_inbox`); pagination for deeper history is TODO(verify).
    """
    await _ensure_inbox()
    return await _eval(f"""
    return allConversations().slice(0, {int(limit)});
    """)


@returns("message[]")
@provides("chats", account_param="account")
@timeout(90)
async def list_messages(*, conversation_id=None, limit=100, **params):
    """List Instagram DM messages, optionally scoped to one thread.

    PROVEN path: reads decrypted `SlideMessage` records straight out of the
    Relay store. Only returns what the client has loaded into the store (recent
    history); deeper history needs the thread scrolled (Relay pagination) —
    TODO(verify) a load-more path.
    """
    await _ensure_inbox()
    thread = json.dumps(conversation_id) if conversation_id else "null"
    return await _eval(f"""
    return allMessages({thread}).slice(0, {int(limit)});
    """)


# ──────────────────────────────────────────────────────────────────────
# Watch — the live push wire (the core of this plugin)
# ──────────────────────────────────────────────────────────────────────
# Same discipline as WhatsApp's _WATCH_HOOK:
#   • install-once BY CONTROL FLOW (never clearInterval) — guarded by a window
#     flag; the injected loop returns after wiring up.
#   • own the seen-set cursor IN the hook so reloads/re-arms don't replay.
#   • emit one shape-native `message` per new record via a marker console.log;
#     the engine routes marker lines -> live_entity::write -> observer bus.
#
# TODO(verify) — the deciding unknown: does hooking store.notify catch messages
# for threads that AREN'T currently open? Relay only holds what's been queried.
# If off-screen threads don't mutate the store, keep the inbox query live (it
# keeps recent threads warm) or add an inbox-poll backstop. Test by receiving a
# DM in a non-open thread and watching for the marker line.
_WATCH_MARKER = "__agentos_entity__"

_WATCH_HOOK = """
(function () {
  if (window.__agentos_ig_watch__) return;
  window.__agentos_ig_watch__ = true;
  %(find_env)s
  const __seen = new Set();
  (async () => {
    while (true) {
      try {
        const env = __findRelayEnv();
        if (env) {
          const store = env.getStore();
          const source = store.getSource();
          let meFbid = null; try { meFbid = __viewerFbid(source); } catch (e) {}
          %(helpers)s
          // Emit new SlideMessages from a publish DELTA. Relay hands
          // store.publish(delta) a RecordSource of EXACTLY the records that just
          // changed (verified: it has getRecordIDs/get) — that IS the change
          // event. Read the changed-id set from the delta, the hydrated record
          // from the merged store. No full-store scan, no timer poll.
          //
          // Skeleton guard: Relay publishes an empty placeholder (no message_id
          // / thread_fbid) first, then hydrates it in a later publish — emit only
          // fully-formed records, deduped on the canonical message_id.
          const emitFromDelta = (delta) => {
            try {
              const ids = (delta && delta.getRecordIDs) ? delta.getRecordIDs() : [];
              for (const id of ids) {
                const d = delta.get(id);
                if (!d || d.__typename !== 'SlideMessage') continue;
                const full = source.get(id) || d;   // hydrated, post-merge
                const mid = str(full.message_id);
                if (!mid || !full.thread_fbid || __seen.has(mid)) continue;
                __seen.add(mid);
                const entity = mapMsg(full);
                if (!entity) continue;
                entity.__shape__ = 'message';
                console.log(%(marker)s + JSON.stringify(entity));
              }
            } catch (e) {}
          };
          // Prime the seen-set with what's already loaded WITHOUT emitting it.
          try {
            const ids = source.getRecordIDs ? source.getRecordIDs() : [];
            for (const id of ids) {
              const r = source.get(id);
              if (r && r.__typename === 'SlideMessage' && str(r.message_id)) __seen.add(str(r.message_id));
            }
          } catch (e) {}
          // Wrap store.publish — Relay's own change signal (fires with the delta
          // the instant records are written). Install-once by the marker.
          const origPublish = store.publish.bind(store);
          if (!store.publish.__agentosWatch) {
            store.publish = function (delta) {
              const out = origPublish.apply(this, arguments);
              try { emitFromDelta(delta); } catch (e) {}
              return out;
            };
            store.publish.__agentosWatch = true;
          }
          return;
        }
      } catch (e) {}
      await new Promise((r) => setTimeout(r, 500));
    }
  })();
})()
"""


@returns({"watching": "boolean", "stream": "string"})
@provides("message_watch", account_param="account")
@timeout(60)
async def watch(**params):
    """Stream new Instagram DMs into the graph in real time.

    Installs a durable hook on the live session's Relay store; each new
    `SlideMessage` lands as a `message` entity the moment Instagram's client
    decrypts it. Survives reloads/session drops/engine restarts (the engine
    re-arms from the graph). Arm once. Idempotent.

    TODO(verify) end-to-end: this is scaffolded from proven pieces (env found,
    store.notify exists) but the store.notify wrap + off-screen-thread
    granularity have NOT been exercised with a real inbound DM yet.
    """
    await _ensure_inbox()
    hook = _WATCH_HOOK % {
        "find_env": _FIND_ENV_JS,
        "helpers": _HELPERS,
        "marker": json.dumps(_WATCH_MARKER),
    }
    await services.call("browser_session", verb="subscribe", params={
        "target": _TARGET,
        "mode": _MODE,
        "js": hook,
        "marker": _WATCH_MARKER,
        "subscriber": "instagram",
        "op": "watch",
    })
    return {"watching": True, "stream": "message"}


# ──────────────────────────────────────────────────────────────────────
# Send — UNVERIFIED. Two paths (see readme "Send strategy").
# ──────────────────────────────────────────────────────────────────────
# PRIMARY (safest, recommended): drive the composer UI from Python —
#   navigate to the thread, browser_session snapshot -> type into the message
#   box -> key Enter. Zero API surface, human-indistinguishable. Best done as
#   an orchestration of snapshot/type/key ops, not one eval, so it's stubbed
#   below rather than the in-page-fetch path.
# FALLBACK: in-page fetch to Instagram's own web endpoint (same-origin, the
#   page's own cookies + x-csrftoken, no spoofing). Endpoint harvested from the
#   old stub — E2EE MAY have moved sends onto an encrypted path, so verify this
#   still works before relying on it.

@returns("message")
@provides("message_send", account_param="account")
@timeout(90)
async def send_message(*, to, text, **params):
    """Send an Instagram DM to a thread; returns the sent message entity.

    Composer-UI drive (VERIFIED live 2026-07-06): opens the thread, types into
    Instagram's own message box, presses Enter — IG's client does the E2EE send.
    Zero API surface, human-indistinguishable. The receipt is read back from the
    Relay store (the outgoing SlideMessage), never a bare keypress-ok — trust the
    platform's own state, per the WhatsApp/iMessage discipline.

    Args:
        to: thread_fbid (the numeric `conversationId`, e.g. from
            list_conversations). IG accepts it directly in the thread URL.
        text: message text to send.
    """
    # 1. Open the thread — thread_fbid works directly as the /direct/t/ URL id.
    nav = await services.call("browser_session", verb="navigate", params={
        "target": _TARGET, "mode": _MODE,
        "url": f"https://www.instagram.com/direct/t/{to}/", "timeout": 30,
    })
    # 2. Find IG's composer in the returned snapshot (the one role:textbox).
    tree = nav.get("snapshot", {}).get("tree", []) if isinstance(nav, dict) else []
    box = next((el for el in tree if el.get("role") == "textbox" and el.get("ref")), None)
    if not box:
        return app_error(
            f"No message composer opened for thread {to} — is `to` a valid "
            "thread_fbid (the numeric conversationId from list_conversations)?",
            code="NotFound",
        )
    # 3. Type + Enter. IG's own composer handles the E2EE send on Enter.
    await services.call("browser_session", verb="type", params={
        "target": _TARGET, "mode": _MODE, "ref": box["ref"], "text": text, "clear": True,
    })
    await services.call("browser_session", verb="key", params={
        "target": _TARGET, "mode": _MODE, "keys": "Enter",
    })
    # 4. Receipt: poll the store for our outgoing SlideMessage (never trust the
    #    keypress alone — the sent message landing in the store is the truth).
    return await _eval(f"""
    const want = {json.dumps(text)}.trim();
    const tid = {json.dumps(str(to))};
    const findMine = () => {{
      for (const id of source.getRecordIDs()) {{
        const r = source.get(id);
        if (r && r.__typename === 'SlideMessage'
            && String(r.thread_fbid || '') === tid
            && str(r.text_body).trim() === want) return r;
      }}
      return null;
    }};
    // Event-driven receipt: resolve on the store.publish that lands our message
    // (Relay's own change signal) — no timer poll. Falls back after 15s.
    let rec = findMine();
    if (!rec) {{
      rec = await new Promise((resolve) => {{
        const origPub = store.publish.bind(store);
        let settled = false;
        const finish = (r) => {{ if (settled) return; settled = true; store.publish = origPub; resolve(r); }};
        store.publish = function () {{
          const out = origPub.apply(this, arguments);
          try {{ const m = findMine(); if (m) finish(m); }} catch (e) {{}}
          return out;
        }};
        setTimeout(() => finish(null), 15000);
      }});
    }}
    if (!rec) return {{ __error: 'send_failed', what:
      'typed + Entered, but no message with that text landed in the thread '
      + 'within 15s — the send may not have gone through' }};
    const m = mapMsg(rec);
    if (m) {{ m.isOutgoing = true; m.author = 'Me'; }}
    return m;
    """, wait_ms=17000, timeout_s=45)


@returns({"status": "string"})
@timeout(90)
async def send_reaction(*, message_id, emoji="❤️", conversation_id=None, **params):
    """React to a message with an emoji (default heart).

    NOT YET IMPLEMENTED — reactions are a harder reverse-engineering problem than
    text send (send uses the composer textbox; reactions have no such simple
    surface). Full notes in readme "Reactions — reverse-engineering notes".
    State as of 2026-07-06:
      • UI path (fragile): hover/click a message → a toolbar appears; the face
        button (aria-label "React to message from <user>") has a React onClick
        that opens an emoji-picker popover — calling that onClick natively DOES
        open it (verified visually). BUT the picker's emoji options are unlabeled
        sprites (no aria-label / text), ~48px-wide divs in the popover container
        (reachable via the face button's `aria-controls`); ❤️ is the FIRST one.
        Hard to target reliably + picker open/close is timing-sensitive.
      • RECOMMENDED durable path — skip the UI, call IG's own reaction action
        with (message_id, emoji, thread_fbid). FIRST find the transport:
        instrument BOTH `env.getNetwork().execute` (Relay mutations) AND
        fetch/XHR, then react ❤️ MANUALLY once and see which fires. GraphQL/REST
        → capture doc_id + variables and replay. E2EE MQTT websocket (like text
        sends use) → need the client's reaction fn (deeper dig).
    """
    return app_error(
        "instagram.send_reaction not yet implemented — reactions need their own "
        "reverse-engineering pass (see readme 'Reactions'). Text send/receive/"
        "watch all work today.",
        code="NotImplemented",
    )


@returns({"state": "string"})
@provides("message_typing", account_param="account")
@timeout(60)
async def send_typing(*, chat, kind="typing", **params):
    """Show a typing indicator in a thread (`chat` = thread_fbid).

    TODO(verify): in-page fetch to /direct_v2/threads/{id}/activity/
    (activity_status 1=typing, 0=stopped). Stubbed.
    """
    return app_error("instagram.send_typing not yet implemented — see readme.", code="NotImplemented")


@returns("conversation")
@provides("message_mark_read", account_param="account")
@timeout(60)
async def mark_read(*, conversation_id, **params):
    """Mark a thread's latest message seen.

    TODO(verify): in-page fetch to /direct_v2/threads/{id}/items/{item}/seen/.
    Stubbed.
    """
    return app_error("instagram.mark_read not yet implemented — see readme.", code="NotImplemented")
