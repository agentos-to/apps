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

import base64
import json

from agentos import (
    account, app_error, blobs, browser_session, client, credentials,
    provides, returns, services, timeout,
)

# Pin the real host Instagram redirects to — engine also treats www↔apex as
# one surface, but opening www avoids a mint→redirect→orphan cycle.
_TARGET = "www.instagram.com"
_INBOX_URL = "https://www.instagram.com/direct/inbox/"
_HOME_URL = "https://www.instagram.com/"
_LOGIN_URL = "https://www.instagram.com/accounts/login/"

# The TRUE session signal. `sessionid` is httpOnly — invisible to in-page
# document.cookie — so it's read via the browser plane's `cookies` verb, never
# an eval. Present ⇒ writes will work (a stale in-page fb_dtsg is a self-heal,
# not a logout); absent ⇒ genuinely logged out. The Relay env + ds_user_id BOTH
# survive an expired session, which is why the old check_session lied.
_SESSION_COOKIE = "sessionid"

# The DM UI + its Relay records only exist once /direct has loaded — the site
# root doesn't populate the message store. Ops ensure the tab is here.
#
# _MODE: BACKGROUND — every op runs in the engine's HEADLESS background profile
# (rule 19's connector default): DM payloads surface in the Messaging app, so no
# window is needed for a read. The one headed moment is `login`, which flips this
# same profile headed (`browser_session.login_window` → the engine's `--app`
# flip) for the sign-in and returns it to the daemon when done — so the session
# lands in the exact profile every headless read uses.
#
# The old ATTACH rationale (drive Joe's daily Brave, no separate login) is dead:
# its objections were "IG has no one-time QR" (the headed flip IS the login path,
# no QR needed), "can't reuse the daily profile headless" (we don't — we log in
# fresh in the bg profile), and "cookie-copy risks IG's device-fingerprint check"
# (we don't copy — the login is native in the bg profile, so its fingerprint is
# consistent from sign-in onward). Flipped to background 2026-07-08.
_MODE = "background"

# ──────────────────────────────────────────────────────────────────────
# Reverse-engineering recipe — how to find the transport for a new action
# ──────────────────────────────────────────────────────────────────────
# Proven on `send_reaction` (2026-07-06). When the UI does something but you
# don't know how it travels the wire, DON'T guess module names — instrument the
# live transport and watch what IG's own client fires:
#
#   1. In the page, wrap all four egress points into a `window` buffer:
#        env.getNetwork().execute            ← Relay / GraphQL ops (the network
#                                              object's method is reachable — it
#                                              is NOT closure-private)
#        window.fetch, XMLHttpRequest.send   ← REST / GraphQL-over-HTTP
#        WebSocket.prototype.send            ← the E2EE realtime wire (edge-chat
#                                              MQTT / gateway realtime)
#      Capture {name, params.id (== doc_id), variables} for Relay; {url, body}
#      for fetch/xhr; {size, ts} for ws.
#   2. Do the action ONCE through the real UI (click react, type, mark read …).
#   3. Read the buffer — which surface fired IS the transport:
#        • Relay `execute` fired → it's a GraphQL mutation. Grab params.name +
#          params.id (doc_id) + variables, then REPLAY durably: the GLOBAL
#          `require('<Name>.graphql')` returns the compiled ConcreteRequest (the
#          global require resolves any module BY ID), and
#          `require('relay-runtime').commitMutation(env, {mutation, variables,
#          onCompleted, onError})` fires it — IG injects fb_dtsg/lsd/doc_id
#          itself; you supply only variables; onCompleted w/ no errors = server
#          ack. (This is exactly how `send_reaction` works.)
#        • ONLY WebSocket fired → it rides the E2EE realtime wire, as text sends
#          and typing indicators do. Not GraphQL-replayable; needs IG's own
#          client publish fn (for text, that was the composer-UI drive).
#
# What did NOT work: enumerating Haste's module registry to discover names. The
# __d/require modules map is closure-private — not on require/__d/window (a full
# window scan finds only WebGL enums), and each module factory receives a LOCAL
# `require`, so wrapping the global one doesn't intercept internal calls. The
# global require can LOOK UP a known id but can't LIST ids. Full enumeration
# would need __d wrapped at document-start + a reload (or the CDP Debugger domain
# reading the runtime closure) — more work than instrumenting the transport,
# which names the op directly.
#
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
// IG hands numbers over the wire as STRINGS in the Relay store — timestamp_ms,
// widths, counts all arrive like "1783394485069" (proven live 2026-07-06). Coerce
// before any Number.isFinite check or Date() — a raw string fails isFinite and
// silently nulls the field (this bit `published` for every message).
const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : null; };
const isoMs = (ms) => { const n = num(ms); return n != null ? new Date(n).toISOString() : null; };
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
// The thread record's participants (`t.users {__refs}`) are XDTUserDict
// records DIRECTLY — the @username is inline, no SlideUser hop (verified live
// 2026-07-07). Build an account-shaped ref from one so a conversation can
// carry its other party's handle, the same `participant` child iMessage +
// WhatsApp already attach — provider-uniform, and the @handle lands on the
// graph + is available to the UI (to show handles, not just display names).
const udAccount = (ud) => {
  if (!ud) return null;
  const a = { platform: 'instagram' };
  if (ud.id != null) a.id = String(ud.id);
  const handle = str(ud.username); if (handle) a.handle = handle;
  const name = str(ud.full_name) || handle; if (name) a.display_name = name;
  const pic = str(ud.profile_pic_url); if (pic) a.image = pic;
  return (a.handle || a.id) ? a : null;
};
// A direct thread's participants, resolved to account refs (handle-bearing).
// A 1:1 carries exactly the other party; a group carries all of them.
const threadParticipants = (t) => {
  const refs = (t.users && t.users.__refs) || [];
  const out = [];
  for (const uid of refs) { const a = udAccount(source.get(uid)); if (a) out.push(a); }
  return out;
};
// Fold a message's reactions -> [{emoji, count}] (same shape WhatsApp emits,
// so the Messaging app stays provider-uniform). PROVEN: SlideMessage.reactions
// {__refs} -> XFBSlideReaction {reaction: emoji, sender_fbid}; the sibling
// msg_reactions -> MessagingReaction carries only sender ids (no emoji), so the
// emoji-bearing `reactions` list is the source. Aggregate by emoji, folding the
// variation selectors (U+FE0E/FE0F) so ❤ and ❤️ count as one — the way IG's own
// bubble does — while keeping the color-emoji variant to display. A blank
// `reaction` is a removed reaction — skip it.
const VS = /[\\uFE0E\\uFE0F]/g;
const reactionsOf = (m) => {
  const refs = (m.reactions && m.reactions.__refs) || [];
  if (!refs.length) return null;
  const agg = new Map();
  for (const rid of refs) {
    const r = source.get(rid);
    const emoji = r ? str(r.reaction) : '';
    if (!emoji) continue;
    const key = emoji.replace(VS, '');
    const cur = agg.get(key) || { emoji, count: 0 };
    cur.count += 1;
    if (emoji.length > cur.emoji.length) cur.emoji = emoji;
    agg.set(key, cur);
  }
  if (!agg.size) return null;
  return [...agg.values()].sort((a, b) => b.count - a.count);
};
// XMA = a rich attachment card: a shared post/reel, a story reply, a link
// preview, or an "unavailable" placeholder. Field names differ per XMA subtype
// (SlideMessagePortraitXMA carries header_title_text/eyebrow_text/target_url;
// SlideMessagePlaceholderXMA carries title_text/caption_body_text), so read
// both. PROVEN on the @ksubedi thread (story reply + placeholder). Yields
// {kind,title,subtitle,eyebrow,targetUrl,previewUrl}.
const mapXma = (x) => {
  if (!x) return null;
  const a = {};
  const kind = str(x.__typename).replace(/^SlideMessage/, '').replace(/XMA$/, '');
  if (kind) a.kind = kind;
  const title = str(x.header_title_text) || str(x.title_text);
  if (title) a.title = title;
  // subtitle_text is what LayeredXMA (reel/post share) carries; PROVEN live.
  const subtitle = str(x.header_subtitle_text) || str(x.subtitle_text) || str(x.caption_body_text);
  if (subtitle) a.subtitle = subtitle;
  if (str(x.eyebrow_text)) a.eyebrow = str(x.eyebrow_text);
  if (str(x.target_url)) a.targetUrl = str(x.target_url);
  const prev = deref(x.preview_image);
  if (prev) { const u = str(prev.url) || str(prev.fallback_url); if (u) a.previewUrl = u; }
  return Object.keys(a).length ? a : null;
};
// A media connection -> [{type,url,previewUrl,width,height,durationMs}]. Accepts
// either a {__refs} connection OR a single {__ref} (Raven view-once carries one
// `attachment` ref, not a list). PROVEN live 2026-07-06 on all four media dicts:
//   videos  (SlideMessageVideosContent.videos)          attachment_cdn_url + preview_cdn_url + preview_width/height
//   images  (SlideMessageImageContent.attachments)      attachment_cdn_url + preview_cdn_url + preview_width/height
//   audios  (SlideMessageAudiosContent.audio_attachments) attachment_cdn_url + playable_duration_ms (+ waveform_data)
//   animated(SlideMessageAnimatedMediaContent.animated_media) attachment_mp4_url / attachment_webp_url + preview_cdn_url
// (Instagram numbers arrive as strings — num() coerces width/height/duration.)
const mediaList = (conn, kind) => {
  const ids = (conn && conn.__refs) ? conn.__refs : (conn && conn.__ref ? [conn.__ref] : []);
  const out = [];
  for (const id of ids) {
    const md = source.get(id); if (!md) continue;
    const url = str(md.attachment_cdn_url) || str(md.attachment_cdn_fallback_url)
             || str(md.attachment_mp4_url) || str(md.attachment_webp_url)
             || str(md.playable_url) || str(md.url);
    if (!url) continue;
    const item = { type: kind, url };
    const pv = str(md.preview_cdn_url) || str(md.preview_cdn_fallback_url); if (pv) item.previewUrl = pv;
    const w = num(md.preview_width); if (w != null) item.width = w;
    const h = num(md.preview_height); if (h != null) item.height = h;
    const dur = num(md.playable_duration_ms); if (dur != null) item.durationMs = dur;
    out.push(item);
  }
  return out;
};
// The message a reply is threaded onto — replied_to_message is a full nested
// SlideMessage. PROVEN on the @ksubedi thread. -> {id,author,snippet,isOutgoing}.
const replyOf = (m) => {
  const r = deref(m.replied_to_message);
  if (!r) return str(m.replied_to_message_id) ? { id: str(m.replied_to_message_id) } : null;
  const ra = account(deref(r.sender));
  const mine = meFbid != null && String(r.sender_fbid) === String(meFbid);
  let body = str(r.text_body);
  if (!body) { const rc = deref(r.content); if (rc) body = str(rc.text_body) || str(rc.xma_text_body); }
  const o = { id: str(r.message_id) || str(m.replied_to_message_id), isOutgoing: mine };
  o.author = mine ? 'Me' : (ra ? ra.display_name : null);
  if (body) o.snippet = body.slice(0, 120);
  return o;
};
// Map a SlideMessage record -> a `message` entity (same shape the Messaging app
// consumes from WhatsApp/iMessage). Branches on the CONTENT record's typename
// (more robust than the content_type enum) to attach media / share / system
// bodies. PROVEN fields; see readme "Content model".
// A reaction/like system admin line — the whole body IS the echo (optionally
// prefixed by the reactor's name). Dropped in mapMsg; see the note there.
// NOTE: this lives in the non-raw `_HELPERS` Python string, so every regex
// backslash must be DOUBLED (\\b, \\s) — a single \b is eaten by Python (→
// backspace) before the browser sees it. The rest of _HELPERS follows the
// same convention (\\d in __viewerFbid).
const isReactionEcho = (t) => {
  const s = (t || '').trim();
  return /^(.*\\s)?reacted\\b[^]*\\bto (your|a|this) message$/i.test(s)
      || /^(.*\\s)?liked (a|your|this) message$/i.test(s);
};
const mapMsg = (m) => {
  if (!m || m.__typename !== 'SlideMessage') return null;
  const acct = account(deref(m.sender));
  const c = deref(m.content);
  const cType = c ? c.__typename : null;
  // Text is inlined on the SlideMessage; fall back to the content record.
  let content = str(m.text_body);
  if (!content && c) content = str(c.text_body) || str(c.xma_text_body);
  const out = {
    id: str(m.message_id) || str(m.id),
    content,
    published: isoMs(m.timestamp_ms),   // timestamp_ms is MILLISECONDS
    conversationId: String(m.thread_fbid || ''),
    type: 'text',
  };
  if (cType === 'SlideMessageAdminText') {
    out.type = 'system';
    const frags = (c.text_fragments && c.text_fragments.__refs) || [];
    const t = frags.map(id => { const f = source.get(id); return f ? str(f.plaintext) : ''; }).join('');
    if (t) out.content = t;
    // Drop reaction/like echoes. IG mints a system admin line ("Reacted 😆 to
    // your message", "Liked a message") for every reaction — but the reaction
    // is ALSO folded onto its target message (reactionsOf → the chip the UI
    // shows). The admin line is a lossy duplicate: noise in the thread and on
    // the graph. Matched by English pattern — the record carries no typed
    // reaction-log marker yet; when IG exposes one, key on that instead.
    if (isReactionEcho(out.content)) return null;
  } else if (cType === 'SlideMessageXMAContent') {
    out.type = 'share';
    const att = mapXma(deref(c.xma)); if (att) out.attachment = att;
  } else if (cType === 'SlideMessageVideosContent') {
    out.type = 'video';
    const md = mediaList(c.videos, 'video'); if (md.length) out.media = md;
  } else if (cType === 'SlideMessageImageContent') {
    out.type = 'image';
    const md = mediaList(c.attachments, 'image'); if (md.length) out.media = md;
  } else if (cType === 'SlideMessageAudiosContent') {
    out.type = 'audio';
    const md = mediaList(c.audio_attachments, 'audio'); if (md.length) out.media = md;
  } else if (cType === 'SlideMessageAnimatedMediaContent') {
    // GIF / sticker (GIPHY-backed). animated_media dict carries attachment_mp4_url
    // + attachment_webp_url; is_sticker distinguishes a sticker from a GIF.
    out.type = 'animated';
    const md = mediaList(c.animated_media, 'animated'); if (md.length) out.media = md;
  } else if (cType === 'SlideMessageRavenImageContent' || cType === 'SlideMessageRavenVideoContent') {
    // "Raven" = Meta's codename for view-once / disappearing media. The single
    // `attachment` ref is null once consumed/expired. Shape captured live (a
    // consumed one); the media dict inside is UNVERIFIED (none unconsumed to read).
    out.type = cType.indexOf('Video') >= 0 ? 'video' : 'image';
    out.isViewOnce = true;
    const md = mediaList(c.attachment, out.type); if (md.length) out.media = md;
  } else if (cType && cType !== 'SlideMessageText') {
    out.type = (str(m.content_type) || 'text').toLowerCase();
  }
  if (meFbid != null && m.sender_fbid != null) {
    out.isOutgoing = String(m.sender_fbid) === String(meFbid);
    out.author = out.isOutgoing ? 'Me' : (acct ? acct.display_name : null);
  } else if (acct) {
    out.author = acct.display_name;
  }
  if (acct && out.isOutgoing !== true) out.from = acct;
  const rx = reactionsOf(m); if (rx) out.reactions = rx;
  const rep = replyOf(m); if (rep) out.replyTo = rep;
  // Flags — set only when true, to keep the entity lean.
  if (m.igd_is_forwarded === true) out.isForwarded = true;
  if (m.is_pinned === true) out.isPinned = true;
  if (m.is_ai_generated === true) out.isAiGenerated = true;
  if (m.tombstone_reason != null) out.isDeleted = true;
  if (m.slide_edit_history && m.slide_edit_history.__refs && m.slide_edit_history.__refs.length) out.isEdited = true;
  // Vanish-mode / view-once expiries — field names confirmed present, but a
  // normal message carries 0 (the sentinel), not null, so guard on > 0 before
  // surfacing (no live non-zero example to verify semantics against).
  const vexp = num(m.view_expiration_timestamp_ms); if (vexp) out.viewExpiresAt = isoMs(vexp);
  const exp = num(m.expiration_timestamp_ms); if (exp) out.expiresAt = isoMs(exp);
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
  const conv = {
    id: String(t.thread_fbid),
    name: str(t.thread_title),
    isGroup: t.is_group === true,
    unreadCount: t.marked_as_unread === true ? 1 : 0,
    isMuted: t.is_muted === true,
    published: isoMs(t.last_activity_timestamp_ms),
  };
  // The other party (or group members) as handle-bearing account refs — the
  // same `participant` child WhatsApp/iMessage attach. For a 1:1 this is the
  // one @handle the UI can show in place of the display name.
  const parts = threadParticipants(t);
  if (parts.length) conv.participant = parts;
  // Promote a face onto the conversation row — 1:1 uses the other party's
  // profile_pic_url; a group uses the first member with a pic (interim until
  // a real group avatar lands). Messaging renders `conversation.image`.
  if (!conv.isGroup && parts.length && parts[0].image) {
    conv.image = parts[0].image;
  } else if (conv.isGroup) {
    for (const p of parts) {
      if (p.image) { conv.image = p.image; break; }
    }
  }
  return conv;
};
// Collect every SlideMessage record, optionally filtered to one thread.
const allMessages = (threadFbid) => {
  // Re-resolve the viewer fbid now the store is warm. _PRELUDE resolves it once
  // up front, but on a COLD thread open the viewer's SlideUser isn't hydrated
  // yet → meFbid stays null → mapMsg can't flag isOutgoing → every bubble lands
  // on the left until a refetch. By here loadThreadHistory has pulled the
  // thread (viewer is a participant), so the scan succeeds on first paint.
  if (meFbid == null) { try { meFbid = __viewerFbid(source); } catch (e) {} }
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
// Profile face URL on an XDTUserDict — often a Relay ref to XDTProfilePicUrlInfo.
const picUrlOfUser = (ud) => {
  if (!ud) return null;
  if (typeof ud.profile_pic_url === 'string' && /^https?:/i.test(ud.profile_pic_url)) return ud.profile_pic_url;
  const info = deref(ud.profile_pic_url) || deref(ud.hd_profile_pic_url_info);
  if (info) return str(info.url) || str(info.uri) || null;
  return null;
};
const isoSec = (t) => { const n = num(t); return n != null ? new Date(n * 1000).toISOString() : null; };
// Best image URL from XDTMediaDict.image_versions2.candidates (largest first).
const mediaImageUrl = (m) => {
  if (!m) return null;
  const iv = deref(m.image_versions2);
  if (!iv) return null;
  const refs = (iv.candidates && iv.candidates.__refs) || [];
  let best = null, bestW = -1;
  for (const rid of refs) {
    const c = source.get(rid);
    if (!c || !c.url) continue;
    const w = num(c.width) || 0;
    if (w >= bestW) { bestW = w; best = str(c.url); }
  }
  return best;
};
// Best video URL from XDTMediaDict.video_versions (type 101/102/103 — pick widest).
const mediaVideoUrl = (m) => {
  if (!m) return null;
  const vv = m.video_versions;
  const refs = (vv && vv.__refs) || [];
  let best = null, bestW = -1;
  for (const rid of refs) {
    const c = source.get(rid);
    if (!c || !c.url) continue;
    const w = num(c.width) || 0;
    if (w >= bestW) { bestW = w; best = str(c.url); }
  }
  return best;
};
// media_type: 1=image, 2=video (proven live on story items).
const mapStoryItem = (reel, media, ringTotal, ringUnread) => {
  if (!media || media.__typename !== 'XDTMediaDict') return null;
  const user = deref(reel.user);
  const username = user ? str(user.username) : '';
  const authorId = String((user && (user.pk || user.id)) || reel.id || '');
  const mediaPk = String(media.pk || media.id || '');
  if (!authorId || !mediaPk) return null;
  const isVideo = Number(media.media_type) === 2 || !!(media.video_versions && media.video_versions.__refs && media.video_versions.__refs.length);
  const mediaUrl = isVideo ? (mediaVideoUrl(media) || mediaImageUrl(media)) : mediaImageUrl(media);
  const out = {
    id: 'story:' + authorId + ':' + mediaPk,
    postType: 'story',
    author: username || (user ? str(user.full_name) : '') || 'Unknown',
    authorId,
    published: isoSec(media.taken_at) || isoSec(reel.latest_reel_media),
    expiresAt: isoSec(reel.expiring_at),
    viewed: !!(num(reel.seen) > 0 && num(reel.latest_reel_media) != null && num(reel.seen) >= num(reel.latest_reel_media)),
    ringTotal: ringTotal || 1,
    ringUnread: ringUnread != null ? ringUnread : 1,
    mediaType: isVideo ? 'video' : 'image',
    mediaPk,
  };
  if (mediaUrl) out.mediaUrl = mediaUrl;
  const cap = str(media.accessibility_caption);
  if (cap) out.content = cap;
  const pic = picUrlOfUser(user);
  if (pic) out.image = pic;
  if (username) {
    out.from = {
      id: authorId, platform: 'instagram', handle: username,
      display_name: (user && str(user.full_name)) || username,
    };
    if (pic) out.from.image = pic;
  }
  const dsid = (document.cookie.match(/ds_user_id=(\\d+)/) || [])[1];
  if (dsid) out.accountEmail = dsid;
  return out;
};
const storyItemsForReel = (reel) => {
  if (!reel || reel.__typename !== 'XDTReelDict') return [];
  const refs = (reel.items && reel.items.__refs) || [];
  const fullySeen = (num(reel.seen) || 0) > 0 && num(reel.latest_reel_media) != null
    && num(reel.seen) >= num(reel.latest_reel_media);
  const total = refs.length || 1;
  const unread = fullySeen ? 0 : total;
  const out = [];
  for (const rid of refs) {
    const item = mapStoryItem(reel, source.get(rid), total, unread);
    if (item) out.push(item);
  }
  return out;
};
const findReelByAuthor = (authorId) => {
  const want = String(authorId);
  const direct = source.get('XDTReelDict:' + want) || source.get(want);
  if (direct && direct.__typename === 'XDTReelDict') return direct;
  for (const id of (source.getRecordIDs ? source.getRecordIDs() : [])) {
    const r = source.get(id);
    if (!r || r.__typename !== 'XDTReelDict') continue;
    if (r.reel_type && r.reel_type !== 'user_reel') continue;
    const user = deref(r.user);
    const pk = String((user && (user.pk || user.id)) || r.id || '');
    if (pk === want) return r;
  }
  return null;
};
// One tray reel → one story post (ring). Individual story MEDIA items are not
// on the tray record — only reel metadata + face. get_post hydrates media via
// PolarisStoriesV3ReelPageStandaloneQuery; Social's rail only needs face/seen.
const mapReelPost = (reel) => {
  if (!reel || reel.__typename !== 'XDTReelDict') return null;
  if (reel.reel_type && reel.reel_type !== 'user_reel') return null;
  const user = deref(reel.user);
  const username = user ? str(user.username) : '';
  const latest = num(reel.latest_reel_media);
  const seenAt = num(reel.seen) || 0;
  const fullySeen = seenAt > 0 && latest != null && seenAt >= latest;
  const authorId = String((user && (user.pk || user.id)) || reel.id || '');
  if (!authorId) return null;
  const itemCount = (reel.items && reel.items.__refs && reel.items.__refs.length) || 0;
  const out = {
    id: 'story:' + authorId,
    postType: 'story',
    author: username || (user ? str(user.full_name) : '') || 'Unknown',
    authorId,
    published: isoSec(latest),
    expiresAt: isoSec(reel.expiring_at),
    viewed: fullySeen,
    // Tray usually has no items — placeholder 1 until get_post loads media.
    ringTotal: itemCount || 1,
    ringUnread: fullySeen ? 0 : (itemCount || 1),
    mediaType: 'story',
  };
  const pic = picUrlOfUser(user);
  if (pic) out.image = pic;
  if (username) {
    out.from = {
      id: authorId, platform: 'instagram', handle: username,
      display_name: (user && str(user.full_name)) || username,
    };
    if (pic) out.from.image = pic;
  }
  const dsid = (document.cookie.match(/ds_user_id=(\\d+)/) || [])[1];
  if (dsid) out.accountEmail = dsid;
  return out;
};
// Home-feed stories tray — XDTMaterialTray from xdt_api__v1__feed__reels_tray.
// Fully-viewed rings (seen >= latest_reel_media) sort to the end, matching IG.
const allStoryPosts = () => {
  const ids = source.getRecordIDs ? source.getRecordIDs() : [];
  let tray = null;
  for (const id of ids) {
    const r = source.get(id);
    if (r && r.__typename === 'XDTMaterialTray') { tray = r; break; }
  }
  if (!tray) return [];
  const refs = (tray.tray && tray.tray.__refs) || [];
  const unseen = [], seen = [];
  for (const rid of refs) {
    const post = mapReelPost(source.get(rid));
    if (!post) continue;
    (post.viewed ? seen : unseen).push(post);
  }
  return unseen.concat(seen);
};
// Stage profile faces in-page → `__face` for Python `blobs.put`. CDN URLs
// often work as bare <img> srcs, but caching into the blob store matches
// how message media lands and keeps remember:true durable without re-fetch.
const hydrateFaces = async (rows) => {
  const list = Array.isArray(rows) ? rows : (rows ? [rows] : []);
  await Promise.all(list.map(async (c) => {
    if (!c || !c.image) return;
    try {
      const r = await fetch(c.image);
      if (!r.ok) return;
      const ab = await r.arrayBuffer();
      if (!ab.byteLength || ab.byteLength > 500000) return;
      const bytes = new Uint8Array(ab);
      let bin = '';
      const CHUNK = 0x8000;
      for (let i = 0; i < bytes.length; i += CHUNK) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
      }
      c.__face = {
        data: btoa(bin),
        mime: (r.headers.get('content-type') || 'image/jpeg').split(';')[0],
      };
    } catch (e) {}
  }));
  return rows;
};
"""


# ──────────────────────────────────────────────────────────────────────
# History pagination — replay IG's OWN messages-connection loadNext
# ──────────────────────────────────────────────────────────────────────
# PROVEN live 2026-07-06 (@ksubedi thread 20→40→60): scrolling the thread up
# fires the Relay query `IGDMessageListOffMsysQuery` with variables
# {after: <cursor>, first: 20, id: <thread_fbid>}. We replay it DURABLY — no
# scroll, no open thread, no view-yank — exactly the send_reaction pattern:
#   require('IGDMessageListOffMsysQuery.graphql')  → the compiled ConcreteRequest
#   require('relay-runtime').createOperationDescriptor(node, vars)
#   env.execute({operation}).subscribe({complete})  → Relay normalises the
#     older SlideMessages straight into the live store (the same `source` the
#     _PRELUDE captured — verified it sees the growth in place), appending edges
#     to the thread's __IGDMessagesList_slide_messages_connection via IG's own
#     @connection handler.
# The advancing cursor lives on that connection's page_info {end_cursor,
# has_next_page}; we read it, fire, re-read, until `want` messages are loaded or
# has_next_page is false. A no-growth page also breaks the loop (guards against a
# stale/incompatible cursor when a thread was never opened — the inbox keeps only
# a first:5 preview connection for warm threads).
_PAGINATE_JS = """
const loadThreadHistory = async (TID, want) => {
  TID = String(TID);
  let node, createOp;
  try { node = require('IGDMessageListOffMsysQuery.graphql'); }
  catch (e) { return { loaded: false, reason: 'no_query_module' }; }
  try { createOp = require('relay-runtime').createOperationDescriptor; }
  catch (e) { return { loaded: false, reason: 'no_relay_runtime' }; }
  if (typeof createOp !== 'function') return { loaded: false, reason: 'no_createOperationDescriptor' };
  const findThread = () => {
    for (const id of source.getRecordIDs()) {
      const r = source.get(id);
      if (r && r.__typename === 'XFBIGDirectViewerThread' && String(r.thread_fbid || '') === TID) return r;
    }
    return null;
  };
  // Read the messages-connection cursor for this thread. Prefer the paginating
  // handle connection (__IGDMessagesList…); fall back to any slide_messages
  // connection (e.g. the inbox first:5 preview) so pagination works even when
  // the thread was never opened this session.
  const cursorOf = () => {
    const t = findThread();
    if (!t) return null;
    const keys = Object.keys(t).filter(k => /slide_messages/i.test(k))
      .sort((a, b) => (b.startsWith('__IGDMessagesList') ? 1 : 0) - (a.startsWith('__IGDMessagesList') ? 1 : 0));
    for (const k of keys) {
      const ref = t[k];
      const c = (ref && ref.__ref) ? source.get(ref.__ref) : null;
      if (!c) continue;
      const pi = (c.page_info && c.page_info.__ref) ? source.get(c.page_info.__ref) : c.page_info;
      if (pi && pi.end_cursor) return { cursor: pi.end_cursor, hasNext: pi.has_next_page !== false };
    }
    return null;
  };
  const countThread = () => {
    let n = 0;
    for (const id of source.getRecordIDs()) {
      const r = source.get(id);
      if (r && r.__typename === 'SlideMessage' && String(r.thread_fbid || '') === TID) n++;
    }
    return n;
  };
  let pages = 0;
  for (let i = 0; i < 60; i++) {
    const have = countThread();
    if (have >= want) break;
    const cur = cursorOf();
    if (!cur || !cur.hasNext) break;
    const op = createOp(node, { after: cur.cursor, first: 20, id: TID });
    const ok = await new Promise((resolve) => {
      let done = false; const fin = (o) => { if (!done) { done = true; resolve(o); } };
      try { env.execute({ operation: op }).subscribe({ next: () => {}, error: () => fin(false), complete: () => fin(true) }); }
      catch (e) { fin(false); }
      setTimeout(() => fin(false), 15000);
    });
    pages++;
    if (!ok) break;
    if (countThread() <= have) break;   // no-growth guard (stale/incompatible cursor)
  }
  return { loaded: true, pages, count: countThread() };
};
"""


# ──────────────────────────────────────────────────────────────────────
# Story media hydrate — replay IG's own reel-page query (same as opening a
# story). Tray XDTReelDict has no items; PolarisStoriesV3ReelPageStandaloneQuery
# lands XDTMediaDict refs on reel.items (proven live 2026-07-13).
# ──────────────────────────────────────────────────────────────────────
_REEL_MEDIA_JS = """
const loadReelMedia = async (authorId) => {
  const pk = String(authorId);
  const existing = findReelByAuthor(pk);
  if (existing && existing.items && existing.items.__refs && existing.items.__refs.length) {
    return { loaded: true, cached: true, count: existing.items.__refs.length };
  }
  let node, createOp;
  try { node = require('PolarisStoriesV3ReelPageStandaloneQuery.graphql'); }
  catch (e) { return { loaded: false, reason: 'no_query_module' }; }
  try { createOp = require('relay-runtime').createOperationDescriptor; }
  catch (e) { return { loaded: false, reason: 'no_relay_runtime' }; }
  if (typeof createOp !== 'function') return { loaded: false, reason: 'no_createOperationDescriptor' };
  // Provider flags mirror the live XHR capture when opening a story.
  const op = createOp(node, {
    reel_ids_arr: [pk],
    __relay_internal__pv__PolarisCommunityNoteStoriesLabelEnabledrelayprovider: true,
    __relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider: false,
  });
  const ok = await new Promise((resolve) => {
    let done = false; const fin = (v) => { if (!done) { done = true; resolve(v); } };
    try { env.execute({ operation: op }).subscribe({ next: () => {}, error: () => fin(false), complete: () => fin(true) }); }
    catch (e) { fin(false); }
    setTimeout(() => fin(false), 15000);
  });
  if (!ok) return { loaded: false, reason: 'execute_failed' };
  const reel = findReelByAuthor(pk);
  const n = (reel && reel.items && reel.items.__refs && reel.items.__refs.length) || 0;
  return { loaded: n > 0, count: n };
};
"""


# ──────────────────────────────────────────────────────────────────────
# Mutation helper — fire a compiled Relay node, classify a logged-out write
# ──────────────────────────────────────────────────────────────────────
# Every write op (reaction, mark_read, mark_unread) replays IG's OWN compiled
# mutation via commitMutation — IG injects fb_dtsg/lsd/doc_id, we supply only the
# variables. When fb_dtsg has gone stale the POST /api/graphql is rejected with
# GraphQL error 1675002 "Unauthorized logged out query" EVEN ON A LIVE SESSION.
# So a write's failure must distinguish "logged out" (fb_dtsg stale → recoverable
# by the connector's tiered self-heal) from a generic send failure. This helper
# returns {ok:true} | {ok:false, loggedOut:bool, error:str}; the numeric 1675002
# and the "logged out" summary both appear in IG's GraphQL error object, so we
# match the stringified payload rather than guessing a field name.
_MUTATE_JS = """
const commitAndConfirm = (node, variables, ms) => {
  ms = ms || 12000;
  let commit;
  try { commit = require('relay-runtime').commitMutation; }
  catch (e) { try { commit = require('RelayModern').commitMutation; }
    catch (e2) { return Promise.resolve({ ok: false, error: 'commitMutation unavailable: ' + e2 }); } }
  return new Promise((resolve) => {
    let done = false; const fin = (o) => { if (!done) { done = true; resolve(o); } };
    try {
      commit(env, { mutation: node, variables,
        onCompleted: (r, e) => fin({ ok: !(e && e.length), errors: e || null }),
        onError: (e) => fin({ ok: false, transport: String(e) }) });
    } catch (e) { fin({ ok: false, transport: String(e) }); }
    setTimeout(() => fin({ ok: false, timeout: true }), ms);
  }).then((res) => {
    if (res.ok) return { ok: true };
    const blob = JSON.stringify(res.errors || '') + '|' + (res.transport || '');
    const loggedOut = /1675002/.test(blob) || /logged out/i.test(blob);
    return { ok: false, loggedOut,
             error: res.timeout ? 'mutation timed out' : (res.transport || blob) };
  });
};
"""


def _payload(body: str, *, wait_ms: int) -> str:
    """Wrap an op body in the async IIFE with readiness wait + helpers."""
    return ("(async () => {"
            + (_PRELUDE % {"wait_ms": wait_ms})
            + _HELPERS
            + _PAGINATE_JS
            + _REEL_MEDIA_JS
            + _MUTATE_JS
            + body
            + "})()")


async def _run(body: str, *, wait_ms: int = 15000, timeout_s: int = 45):
    """Run an op body in the Instagram tab; return the RAW JS value.

    No error translation — the write path inspects the raw `{__error: ...}`
    sentinel to self-heal before it becomes a user-facing error.
    """
    return await services.call("browser_session", verb="eval", params={
        "target": _TARGET,
        "mode": _MODE,
        "js": _payload(body, wait_ms=wait_ms),
        "timeout": timeout_s,
    })


def _translate(value):
    """Map a raw op result's `__error` sentinel to a user-facing app_error."""
    if isinstance(value, dict) and "__error" in value:
        code = value["__error"]
        if code == "auth_required":
            return browser_session.needs_auth(
                "Instagram isn't logged in on the engine browser.",
                login_op="instagram.login",
                login_url=_LOGIN_URL,
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
        if code == "logged_out":
            # A write reached here without going through _write's recovery — the
            # session is logged out for writes (GraphQL 1675002). Never surface
            # the raw code; the agent acts on NeedsAuth.
            return browser_session.needs_auth(
                "Instagram rejected the write as logged out (GraphQL 1675002).",
                login_op="instagram.login",
                login_url=_LOGIN_URL,
            )
        if code == "send_failed":
            return app_error(f"Instagram send failed: {value.get('what')}", code="SendFailed")
        return app_error(f"Instagram payload error: {code}", code="PayloadError")

    return value


async def _eval(body: str, *, wait_ms: int = 15000, timeout_s: int = 45):
    """Run a READ op body in the Instagram tab; surface structured errors."""
    return _translate(await _run(body, wait_ms=wait_ms, timeout_s=timeout_s))


async def _stage_faces(rows):
    """Move in-page `__face` bytes onto `conversation.image` as a blob path."""
    single = isinstance(rows, dict)
    items = [rows] if single else (rows if isinstance(rows, list) else None)
    if items is None:
        return rows
    for r in items:
        if not isinstance(r, dict):
            continue
        face = r.pop("__face", None)
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
        r["image"] = blob["path"]
        r["mimeType"] = mime
    return items[0] if single else rows


def _is_logged_out_write(raw) -> bool:
    """The write mutation classified this failure as a logged-out session
    (GraphQL 1675002) — the signal to self-heal, not surface."""
    return isinstance(raw, dict) and raw.get("__error") == "logged_out"


async def _ensure_writable():
    """Gate a write on the TRUE session signal (Principle 1: `authenticated`
    means writes). Returns None when writable — sessionid present, so a stale
    in-page fb_dtsg is a self-heal at commit time — or a typed NeedsAuth
    app_error when genuinely logged out (sessionid absent), so no write op ever
    emits a raw platform error for a logout.
    """
    present = await browser_session.session_cookie_present(_TARGET, _SESSION_COOKIE, mode=_MODE)
    if not present:
        return browser_session.needs_auth(
            "Instagram is logged out (no session cookie).",
            login_op="instagram.login",
            login_url=_LOGIN_URL,
        )
    return None


async def _refresh_fb_dtsg():
    """Refresh IG's fb_dtsg for a LIVE session whose in-page token went stale.

    Mechanism: reload the /direct tab — re-bootstraps the Relay store with a
    fresh fb_dtsg, and re-arms the standing watch hook on the new document (Page
    domain stays enabled, so the subscription's on-new-document script re-runs).
    The spec's blessed rare recovery — proven to re-mint the token.

    View-preserving alternative — IG's own async DTSG refresh
    (`require('DTSGInitialData')` / `DTSG_ASYNC_GET_REQUEST_HEADER`) — avoids the
    reload's view-yank but its module API drifts; it's the optimization to wire
    once a stale-fb_dtsg repro exists to verify against. Reload is what we can
    prove today.
    """
    await services.call("browser_session", verb="reload", params={
        "target": _TARGET, "mode": _MODE, "timeout": 30,
    })


async def _write(body: str, *, wait_ms: int = 15000, timeout_s: int = 45):
    """Run a WRITE mutation body, self-healing a logged-out write.

    The body returns `{__error: 'logged_out'}` on GraphQL 1675002. Tiered
    recovery (Principle 2: self-heal before asking):
      • sessionid live  → fb_dtsg stale: refresh the token, retry once.
      • sessionid gone   → genuinely logged out: typed NeedsAuth (the agent
                           drives instagram.login) — never a raw 1675002.
    """
    raw = await _run(body, wait_ms=wait_ms, timeout_s=timeout_s)
    if not _is_logged_out_write(raw):
        return _translate(raw)

    # Classify against the true signal (the session may have died since the
    # op's up-front _ensure_writable check).
    if await browser_session.session_cookie_present(_TARGET, _SESSION_COOKIE, mode=_MODE):
        await _refresh_fb_dtsg()
        raw = await _run(body, wait_ms=wait_ms, timeout_s=timeout_s)
        if not _is_logged_out_write(raw):
            return _translate(raw)

    # Logged out (or the refresh didn't take) → surface NeedsAuth, never 1675002.
    return browser_session.needs_auth(
        "Instagram is logged out — the write was rejected (GraphQL 1675002) and "
        "a token refresh didn't restore it.",
        login_op="instagram.login",
        login_url=_LOGIN_URL,
    )


async def _ensure_dm():
    """Ensure the tab is on SOME DM surface (so the Relay env + reaction module
    are loaded) without forcing a page load — an action on an open thread must
    not yank the human's view, and a hard navigate wipes the normalized Relay
    store (env, thread list, decrypted messages) and re-runs the whole client
    bootstrap. Only hard-navigates when the tab is OFF /direct entirely.

    The Relay store is cumulative and survives the site's own client-side
    routing (inbox ↔ thread is an SPA transition, not a reload), and the armed
    `watch` keeps the tab parked on /direct/inbox — so once warm, every read is
    a pure in-page eval with no navigation. This is BOTH the read and write
    guard: reads need the same warm store writes do."""
    on_dm = await services.call("browser_session", verb="eval", params={
        "target": _TARGET, "mode": _MODE, "timeout": 10,
        "js": "location.pathname.startsWith('/direct')",
    })
    if on_dm is not True:
        await services.call("browser_session", verb="navigate", params={
            "target": _TARGET, "mode": _MODE, "url": _INBOX_URL, "timeout": 30,
        })


# Reads want the inbox thread list warm; writes only want the DM surface. Both
# are satisfied by "be on /direct with a cumulative store" — one guard now, no
# per-read reload (the previous unconditional navigate was the refresh storm).
_ensure_inbox = _ensure_dm


async def _ensure_home():
    """Ensure the home-feed stories tray (`XDTMaterialTray`) is in the Relay
    store. The tray is populated by `xdt_api__v1__feed__reels_tray` on `/` —
    not on `/direct`. Soft-skip when the tray is already warm (a prior Social
    read); otherwise navigate home and wait. Note: leaving `/direct` colds the
    DM store until the next Messaging `_ensure_dm` — same tradeoff as any
    cross-surface connector share of one tab."""
    warm = await services.call("browser_session", verb="eval", params={
        "target": _TARGET, "mode": _MODE, "timeout": 15,
        "js": """(() => {
          const cands = [document.getElementById('react-root'), document.body,
                         ...document.querySelectorAll('body > div')].filter(Boolean);
          let key = null, el = null;
          for (const c of cands) {
            key = Object.keys(c).find(k => k.startsWith('__reactContainer$') || k.startsWith('__reactFiber$'));
            if (key) { el = c; break; }
          }
          if (!key) return false;
          const isEnv = (o) => { try { return o && typeof o.getStore === 'function'
            && typeof o.getNetwork === 'function'; } catch (e) { return false; } };
          let root = el[key]; if (root && root.current) root = root.current;
          const stack = [root], seen = new Set(); let visited = 0;
          while (stack.length && visited < 40000) {
            const f = stack.pop(); if (!f || seen.has(f)) continue; seen.add(f); visited++;
            for (const p of ['memoizedProps', 'memoizedState']) {
              const v = f[p]; if (!v || typeof v !== 'object') continue;
              for (const k in v) {
                try {
                  const val = v[k];
                  let env = null;
                  if (isEnv(val)) env = val;
                  else if (val && typeof val === 'object' && isEnv(val.environment)) env = val.environment;
                  if (env) {
                    const src = env.getStore().getSource();
                    for (const id of src.getRecordIDs()) {
                      const r = src.get(id);
                      if (r && r.__typename === 'XDTMaterialTray'
                          && r.tray && r.tray.__refs && r.tray.__refs.length) return true;
                    }
                  }
                } catch (e) {}
              }
            }
            if (f.child) stack.push(f.child);
            if (f.sibling) stack.push(f.sibling);
          }
          return false;
        })()""",
    })
    if warm is True:
        return
    await services.call("browser_session", verb="navigate", params={
        "target": _TARGET, "mode": _MODE, "url": _HOME_URL, "timeout": 45,
    })
    # Wait for the tray to land after home bootstrap.
    await services.call("browser_session", verb="eval", params={
        "target": _TARGET, "mode": _MODE, "timeout": 60,
        "js": """(async () => {
          const deadline = Date.now() + 45000;
          while (Date.now() < deadline) {
            if (document.querySelector('input[name="username"]') && /accounts\\/login/.test(location.pathname)) {
              return { __error: 'auth_required' };
            }
            const hit = document.querySelector('[aria-label^="Story by "]');
            if (hit) return true;
            await new Promise(r => setTimeout(r, 400));
          }
          return { __error: 'not_ready', what: 'stories tray' };
        })()""",
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

    `authenticated: true` means WRITES WILL WORK — proven by the httpOnly
    `sessionid` cookie (the true session signal, read via the browser plane),
    NOT the cached Relay env or `ds_user_id` (both survive an expired session
    and lied in the old probe). `sessionid` absent → `authenticated: false` →
    the login/recovery flow takes over.

    Identity (`identifier`) is the viewer's IG numeric id, read straight off the
    `ds_user_id` cookie in the same jar — no extra eval, no page navigation.
    """
    jar = await browser_session.read_cookies(_TARGET, mode=_MODE)
    if _SESSION_COOKIE not in jar:
        return {"authenticated": False}
    acct = {
        "authenticated": True,
        "platform": "instagram",
        "at": {"shape": "product", "name": "Instagram", "url": "https://www.instagram.com/"},
    }
    dsid = (jar.get("ds_user_id") or {}).get("value")
    if dsid:
        acct["identifier"] = dsid
    return acct


@account.login
@returns("account | auth_challenge")
@timeout(60)
async def login(**params):
    """Sign in to Instagram (or report the account if already in).

    Instagram login is username/password + frequent 2FA/checkpoint fronted by
    device-fingerprint anti-bot, so the sign-in itself is completed in the HEADED
    window — a chromeless flip of the engine's background profile (the human
    watches, we don't type creds blind into IG's bot checks). What we self-heal:
      • already live → return the account (no window).
      • resolve stored creds (1Password → vault) so the sign-in is fast, and
        return a `login_window` `auth_challenge` whose `retrieval` hint points the
        agent at Joe's email for the login code (standing email-auth) — so a 2FA
        step finishes autonomously, human only for a genuine checkpoint.
    The session lands in the bg profile every headless read uses.
    """
    existing = await check_session()
    if isinstance(existing, dict) and existing.get("authenticated"):
        return existing

    # Resolve creds so the sign-in can be completed fast; vault first (~ms, no
    # prompt), 1Password on a miss. We surface that they exist, not the values.
    creds = await credentials.retrieve(domain=".instagram.com", required=["password"])
    have_creds = bool(creds and creds.get("found"))

    return await browser_session.login_window(
        _LOGIN_URL,
        label="Instagram",
        instructions=(
            "Sign in to Instagram in the window that opened on the engine's "
            "background profile"
            + (" (your saved credentials are in the vault)" if have_creds else "")
            + ". Complete any 2FA / checkpoint, then poll instagram.check_session. "
            "Call the login_window service with close=true when done."
        ),
        retrieval={
            "via": "email",
            "look_for": "an Instagram login / verification code, or a "
                        "'was this you' sign-in confirmation",
        },
    )


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
@timeout(90)
async def list_conversations(*, limit=50, **params):
    """List Instagram DM threads (most recent first).

    PROVEN path: maps `XFBIGDirectViewerThread` records straight out of the
    Relay store — real titles, group flags, mute/unread. Profile faces from
    `participant.image` are promoted onto `conversation.image` and staged into
    the blob store (same path message media uses). Only the threads the
    client has loaded into the store appear (the inbox is warm after
    `_ensure_inbox`); pagination for deeper history is TODO(verify).
    """
    await _ensure_inbox()
    rows = await _eval(f"""
    return await hydrateFaces(allConversations().slice(0, {int(limit)}));
    """, timeout_s=75)
    return await _stage_faces(rows)


@returns("post[]")
@provides("feeds", account_param="account")
@timeout(90)
async def list_posts(*, limit=100, **params):
    """List Instagram stories as `post` rows (postType: story).

    Brokered as `feeds` for the Social app. Reads the home-feed Relay tray
    (`XDTMaterialTray` / `XDTReelDict` from `xdt_api__v1__feed__reels_tray`) —
    one row per author ring. Fully-viewed rings (`seen >= latest_reel_media`)
    sort to the end, matching Instagram's own tray. Profile faces stage into
    the blob store. Per-story media items are NOT on the tray; use `get_post`
    (`PolarisStoriesV3ReelPageStandaloneQuery`) to hydrate.
    """
    await _ensure_home()
    rows = await _eval(f"""
    return await hydrateFaces(allStoryPosts().slice(0, {int(limit)}));
    """, wait_ms=25000, timeout_s=75)
    return await _stage_faces(rows)


@returns("post")
@provides("get_post", account_param="account")
@timeout(120)
async def get_post(*, id, **params):
    """Get one story ring/item and hydrate media into the blob store.

    Ids from `list_posts`: `story:<user_pk>` (ring). After media load, items
    are also addressable as `story:<user_pk>:<media_pk>`.

    Tray records have no items — we replay IG's own
    `PolarisStoriesV3ReelPageStandaloneQuery` (same op opening a story fires)
    via `createOperationDescriptor` + `env.execute`, then stage CDN bytes with
    `browser_session.response_body` (CDP; in-page fetch is a secondary path).
    Returns `ringPosts` (item metadata) so Social can expand multi-item nav.
    """
    await _ensure_home()
    raw = str(id)
    body = raw[6:] if raw.startswith("story:") else raw
    parts = body.split(":")
    author_id = parts[0] if parts else ""
    media_pk = parts[1] if len(parts) > 1 else None
    if not author_id:
        return app_error(f"Bad story id {raw!r}.", code="NotFound")

    entity = await _eval(f"""
    const authorId = {json.dumps(author_id)};
    const mediaPk = {json.dumps(media_pk)};
    const load = await loadReelMedia(authorId);
    const reel = findReelByAuthor(authorId);
    if (!reel) return {{ __error: 'not_found', what: 'post', ref: {json.dumps(raw)} }};
    const items = storyItemsForReel(reel);
    if (!items.length) {{
      const tray = mapReelPost(reel);
      if (!tray) return {{ __error: 'not_found', what: 'post', ref: {json.dumps(raw)} }};
      tray.__load = load;
      return await hydrateFaces(tray);
    }}
    let hit = mediaPk ? items.find(p => p.mediaPk === String(mediaPk)) : items[0];
    if (!hit) hit = items[0];
    // Keep ring id when the caller asked for the tray id so Social's cache key matches.
    if (!mediaPk) hit = Object.assign({{}}, hit, {{ id: 'story:' + authorId }});
    hit.ringPosts = items.map(p => {{
      const meta = Object.assign({{}}, p);
      delete meta.mediaUrl;
      return meta;
    }});
    hit.__load = load;
    return await hydrateFaces(hit);
    """, wait_ms=25000, timeout_s=90)

    if isinstance(entity, dict) and entity.get("__error"):
        return _translate(entity)
    if not isinstance(entity, dict):
        return entity

    entity = await _stage_faces(entity)
    if not isinstance(entity, dict):
        return entity
    entity.pop("__load", None)
    media_url = entity.pop("mediaUrl", None)
    # ringPosts stay on the entity for Social; strip any leaked mediaUrl.
    if isinstance(entity.get("ringPosts"), list):
        for p in entity["ringPosts"]:
            if isinstance(p, dict):
                p.pop("mediaUrl", None)

    if media_url:
        got = await _fetch_story_media(media_url)
        if got and got.get("data"):
            mime = got.get("mime") or (
                "video/mp4" if entity.get("mediaType") == "video" else "image/jpeg")
            ext = "mp4" if mime.startswith("video/") else (
                "png" if "png" in mime else ("webp" if "webp" in mime else "jpg"))
            blob = await blobs.put(got["data"], ext=ext)
            shape = "video" if mime.startswith("video/") or entity.get("mediaType") == "video" else "image"
            entity["attaches"] = [{
                "shape": shape,
                "name": entity.get("author") or "Story",
                "mimeType": mime,
                "size": got["size"],
                "path": blob["path"],
                "sha": blob["sha256"],
            }]
            if shape == "video":
                entity["mediaType"] = "video"
            elif entity.get("mediaType") == "story":
                entity["mediaType"] = "image"
        else:
            entity["externalUrl"] = media_url
    return entity


async def _fetch_story_media(url: str):
    """CDN bytes for a story item — CDP `response_body` first, http last resort.

    Same lane as Facebook Stories: in-page `fetch(scontent)` sometimes works
    (DM media path), but story/video hosts can CORS-block; pixels already ride
    Chromium's network stack via `<img>`/`<video>`.
    """
    try:
        resp = await browser_session.response_body(_TARGET, url, timeout=60)
        if isinstance(resp, dict) and resp.get("data"):
            return {
                "data": resp["data"],
                "mime": (resp.get("mime") or "application/octet-stream").split(";")[0],
                "size": resp.get("size") or 0,
            }
    except Exception:
        pass
    # Secondary: in-page fetch (works for many scontent image hosts).
    try:
        got = await services.call("browser_session", verb="eval", params={
            "target": _TARGET,
            "mode": _MODE,
            "timeout": 45,
            "js": f"""(async () => {{
              const url = {json.dumps(url)};
              try {{
                const r = await fetch(url);
                if (!r.ok) return {{ __error: 'http_' + r.status }};
                const ab = await r.arrayBuffer();
                if (!ab.byteLength || ab.byteLength > {_MEDIA_HYDRATION_CAP}) {{
                  return {{ __error: 'too_large', size: ab.byteLength }};
                }}
                const bytes = new Uint8Array(ab);
                let bin = '';
                const CHUNK = 0x8000;
                for (let i = 0; i < bytes.length; i += CHUNK) {{
                  bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
                }}
                return {{
                  data: btoa(bin),
                  mime: (r.headers.get('content-type') || 'application/octet-stream').split(';')[0],
                  size: bytes.length,
                }};
              }} catch (e) {{ return {{ __error: 'fetch_failed', what: String(e) }}; }}
            }})()""",
        })
        if isinstance(got, dict) and got.get("data"):
            return got
    except Exception:
        pass
    return await _engine_fetch_media(url)


async def _engine_fetch_media(url: str):
    """Last-resort CDN fetch via engine http.request."""
    try:
        resp = await client.get(
            url,
            client="browser",
            headers={
                "Referer": "https://www.instagram.com/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
    except Exception:
        return None
    if not isinstance(resp, dict):
        return None
    if resp.get("status") not in (200, 206):
        return None
    hexbody = resp.get("body_bytes")
    if not hexbody:
        body = resp.get("body")
        if not body:
            return None
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {
            "data": base64.b64encode(raw).decode("ascii"),
            "mime": (resp.get("headers") or {}).get("content-type", "").split(";")[0],
            "size": len(raw),
        }
    try:
        raw_bytes = bytes.fromhex(hexbody)
    except ValueError:
        return None
    headers = resp.get("headers") or {}
    ct = ""
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == "content-type":
                ct = str(v).split(";")[0]
                break
    return {
        "data": base64.b64encode(raw_bytes).decode("ascii"),
        "mime": ct,
        "size": len(raw_bytes),
    }


@returns("person[]")
@timeout(60)
async def list_persons(*, limit=200, **params):
    """List the people a new chat can be started with.

    Instagram's web client exposes no separate address book — the reachable
    set IS your DM threads. So contacts derive from the warm thread list: each
    direct (non-group) thread becomes a person whose `id` is the thread_fbid,
    which `send_message`'s `to` accepts directly. This is what the Messaging
    app's New-Chat picker reads (the brokered `list_persons` verb on `chats`);
    without it the picker showed zero matches.
    """
    await _ensure_inbox()
    return await _eval(f"""
    return allConversations()
      .filter(c => !c.isGroup && c.id && c.name)
      .slice(0, {int(limit)})
      .map(c => {{
        const p = (c.participant && c.participant[0]) || null;
        const out = {{ id: String(c.id), name: c.name }};
        // The person's @handle rides a nested account (same as WhatsApp's
        // list_persons) so the identity lands on the graph and the UI can
        // show handles; `id` stays the thread_fbid `send_message` routes on.
        if (p) out.accounts = [p];
        return out;
      }});
    """)


@returns("message[]")
@provides("chats", account_param="account")
@timeout(150)
async def list_messages(*, conversation_id=None, limit=100, **params):
    """List Instagram DM messages, optionally scoped to one thread.

    PROVEN path: reads decrypted `SlideMessage` records straight out of the
    Relay store. With `conversation_id`, reaches deeper than the warm window by
    replaying IG's own messages-connection pagination (`IGDMessageListOffMsysQuery`)
    until `limit` messages are loaded or history is exhausted — the durable
    equivalent of WhatsApp's `loadEarlierMsgs` loop, fired straight into the
    store (no scroll, no open thread). Without `conversation_id`, returns the
    recent window across all warm threads (no pagination — raise `limit` per
    thread instead).
    """
    await _ensure_inbox()
    if conversation_id:
        thread = json.dumps(str(conversation_id))
        return await _eval(f"""
        await loadThreadHistory({thread}, {int(limit)});
        return allMessages({thread}).slice(0, {int(limit)});
        """, wait_ms=15000, timeout_s=120)
    return await _eval(f"""
    return allMessages(null).slice(0, {int(limit)});
    """)


# Media bytes bigger than this stay un-hydrated (url-only): the base64 payload
# crosses the CDP eval channel inside one JSON value; ~10MB binary ≈ 13.7MB
# base64, comfortably under the worker's 16MB stdin line cap. We also cap the
# TOTAL hydrated across a multi-media message so the whole return stays under it.
_MEDIA_HYDRATION_CAP = 10 * 1024 * 1024


def _media_shape(mime: str, msg_type: str) -> str:
    """The concrete file subtype for an attachment — widest type `file`.

    The mapped `msg_type` wins for audio: IG voice notes are audio inside an
    mp4 container (mime `video/mp4`), but they're a voice note, not a video.
    """
    if msg_type == "audio" or mime.startswith("audio/"):
        return "sound"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    return "file"


@returns("message")
@provides("get_message", account_param="account")
@timeout(120)
async def get_message(*, id, conversation_id=None, **params):
    """Get one message by id, hydrating its media bytes into the blob store.

    Declared as a brokered capability (`@provides("get_message")`) exactly like
    WhatsApp's, so the Messaging app gates inbound-media *rendering* on "who
    provides get_message" — provider-uniform, no plugin id. Instagram DM media
    CDN urls (`attachment_cdn_url` on video/image/audio dicts, `attachment_mp4_url`
    on GIFs) are plain signed links — **not E2EE at the CDN** — so no decryption
    (unlike WhatsApp's `downloadAndMaybeDecrypt`). Two-stage hydration → base64 →
    engine `blobs.put`:
      1. same-context in-page `fetch` — works for `fbcdn`/`scontent`/`external`
         hosts (image, video, GIF): PROVEN live reading `video/mp4`/`image/jpeg`.
      2. engine-side `http.request` fallback for hosts the browser can't read
         cross-origin — `cdn.fbsbx.com` voice notes are CORS-blocked; CORS is a
         browser concept, so the engine reaches them (PROVEN live).
    Items over 10MB (or once the message's cumulative bytes pass 10MB) stay
    url-only.

    Args:
        id: the `mid.$…` message id (from any message-returning op).
        conversation_id: thread_fbid — optional; only needed to load the thread
            into the store first when the message isn't already warm.
    """
    await _ensure_inbox()
    if conversation_id:
        # Ensure the thread (and thus the message + its media dicts) is loaded.
        await _eval(f"""await loadThreadHistory({json.dumps(str(conversation_id))}, 200); return true;""",
                    wait_ms=15000, timeout_s=90)
    entity = await _eval(f"""
    const MID = {json.dumps(str(id))};
    let msg = null;
    for (const rid of source.getRecordIDs()) {{
      const r = source.get(rid);
      if (r && r.__typename === 'SlideMessage' && str(r.message_id) === MID) {{ msg = r; break; }}
    }}
    if (!msg) return {{ __error: 'not_found', what: 'message (pass conversation_id so the thread loads)', ref: MID }};
    const out = mapMsg(msg);
    const CAP = {_MEDIA_HYDRATION_CAP};
    // Hydrate the mapped media[] in-page. fbcdn/scontent/external hosts serve
    // media CORS-readable from this origin (PROVEN: image/video/animated); the
    // cdn.fbsbx.com voice-note host does NOT (CORS-blocked), so those fail here
    // and Python falls back to an engine-side http.request. Each item carries its
    // url + metadata regardless of fetch outcome so the fallback can run.
    const hydrated = [];
    let total = 0;
    for (const item of (out.media || [])) {{
      if (!item || !item.url) continue;
      const meta = {{ url: item.url, type: item.type, width: item.width, height: item.height, durationMs: item.durationMs }};
      try {{
        const r = await fetch(item.url, {{ method: 'GET' }});
        if (!r.ok) {{ hydrated.push({{ ...meta, error: 'http_' + r.status }}); continue; }}
        const ab = await r.arrayBuffer();
        if (ab.byteLength > CAP || total + ab.byteLength > CAP) {{
          hydrated.push({{ ...meta, error: 'too_large', size: ab.byteLength }});
          continue;
        }}
        total += ab.byteLength;
        const bytes = new Uint8Array(ab);
        let bin = ''; const CHUNK = 0x8000;
        for (let i = 0; i < bytes.length; i += CHUNK) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        hydrated.push({{ ...meta, data: btoa(bin), mime: r.headers.get('content-type') || '', size: bytes.length }});
      }} catch (e) {{ hydrated.push({{ ...meta, error: 'fetch_failed:' + e }}); }}
    }}
    out.__media = hydrated;
    return out;
    """, wait_ms=15000, timeout_s=100)

    if not isinstance(entity, dict):
        return entity
    hydrated = entity.pop("__media", None) or []
    attaches = []
    for i, m in enumerate(hydrated):
        data = m.get("data")            # browser-fetched base64
        mime = m.get("mime") or ""
        size = m.get("size")
        # Engine-side fallback for what the browser couldn't read cross-origin
        # (cdn.fbsbx.com voice notes) or a transient failure — CORS is a browser
        # concept, so http.request reaches it. Skip items the browser skipped for
        # size (already over the cap).
        if not data and m.get("url") and m.get("error") != "too_large":
            got = await _engine_fetch_media(m["url"], m.get("type"))
            if got:
                data, mime, size = got["data"], mime or got["mime"], got["size"]
        if not data:
            continue
        mime = mime or "application/octet-stream"
        ext = mime.split("/")[-1].split(";")[0] or "bin"
        blob = await blobs.put(data, ext=ext)
        att = {
            "shape": _media_shape(mime, m.get("type") or entity.get("type", "")),
            "name": f"{entity.get('type', 'media')} {entity.get('published', '') or ''}".strip() + (f" ({i + 1})" if len(hydrated) > 1 else ""),
            "mimeType": mime,
            "size": size,
            "path": blob["path"],
            "sha": blob["sha256"],
        }
        if m.get("width"):
            att["width"] = m["width"]
        if m.get("height"):
            att["height"] = m["height"]
        if m.get("durationMs"):
            att["durationMs"] = m["durationMs"]
        attaches.append(att)
    if attaches:
        entity["attaches"] = attaches
    return entity


# Guess a mime from the mapped media `type` when the CDN response omits a
# content-type header — enough to name the extension and pick the file shape.
_TYPE_MIME = {"video": "video/mp4", "image": "image/jpeg", "audio": "audio/mp4", "animated": "video/mp4"}


async def _engine_fetch_media(url: str, media_type=None):
    """Fetch one media url engine-side (http.request) — the CORS-immune path for
    hosts the in-page fetch can't read (cdn.fbsbx.com). Returns
    {data(base64), mime, size} or None. Bytes come back hex-encoded (2× on the
    wire), so cap at the hydration limit to stay under the worker's line cap."""
    try:
        resp = await client.get(url, client="browser")
    except Exception:
        return None
    if not isinstance(resp, dict) or resp.get("status") not in (200, 206):
        return None
    hexbody = resp.get("body_bytes")
    if not hexbody:
        # Some hosts return a UTF-8 body field even for bytes; fall back to it.
        body = resp.get("body")
        if not body:
            return None
        raw = body.encode("latin-1", "ignore")
    else:
        try:
            raw = bytes.fromhex(hexbody)
        except ValueError:
            return None
    if not raw or len(raw) > _MEDIA_HYDRATION_CAP:
        return None
    headers = resp.get("headers") or {}
    mime = headers.get("content-type") or _TYPE_MIME.get(media_type or "", "application/octet-stream")
    return {"data": base64.b64encode(raw).decode("ascii"), "mime": mime.split(";")[0], "size": len(raw)}


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
  // Only stream messages newer than arm-time. Scroll-loaded / backfilled HISTORY
  // enters the Relay store too (a human scrolling up, or a thread hydrating) and
  // fires publish — but it is NOT a live arrival and must not masquerade as the
  // newest message. Each message carries its own real timestamp_ms, so ordering
  // is by time, never by store-arrival; deep history is a separate pull
  // (list_messages), never streamed here. (Joe hit this live scrolling the
  // @ksubedi thread on mode:attach, 2026-07-06.)
  const __armedAt = Date.now();
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
                // Backfill/scroll-loaded history — carries an old timestamp, so
                // it is not a live arrival. Mark seen and skip; 30s slack absorbs
                // server/browser clock skew.
                const __ts = Number(full.timestamp_ms);
                if (Number.isFinite(__ts) && __ts < __armedAt - 30000) { __seen.add(mid); continue; }
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
    # 0. Honest gate: a logged-out session redirects /direct to the login page —
    #    no composer to drive. Fail as NeedsAuth, not a cryptic "no composer".
    guard = await _ensure_writable()
    if guard:
        return guard
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


@provides("message_react", account_param="account")
@returns({"status": "string", "emoji": "string", "messageId": "string",
          "conversationId": "string", "reactedTo": "string"})
@timeout(90)
async def send_reaction(*, message_id, emoji="❤️", conversation_id=None, remove=False, **params):
    """React to (or un-react from) a message with an emoji (default heart).

    Declared as a brokered capability (`@provides("message_react")`), the same
    way WhatsApp declares it — so the provider-agnostic Messaging app lights
    its reaction strip on any thread whose origin provider answers
    `message_react`, never on a per-plugin sniff. Reachable both brokered and
    verb-routed (`services.chats {verb: send_reaction}`).

    VERIFIED live 2026-07-06: replays Instagram's OWN reaction mutation through
    its Relay network — the same `IGDirectReactionSendMutation` (doc_id
    24374451552236906) the web client fires when you pick an emoji from a
    message's toolbar. NOT the E2EE MQTT wire (text sends ride that; reactions
    are a plain GraphQL mutation over POST /api/graphql) and NOT the fragile
    UI-picker drive. We `require()` IG's compiled operation node and
    `commitMutation` it on the live env: IG injects every token (fb_dtsg, lsd,
    doc_id) itself, we supply only the variables. `onCompleted` with no GraphQL
    errors is Instagram's own acknowledgment, so we report a confirmed
    `reacted`/`removed` (unlike WhatsApp's headless `dispatched`).

    IG stores reaction emoji as bare codepoints — the picker's ❤️ (U+2764 U+FE0F)
    is sent as U+2764 — so we strip variation selectors to match.

    Args:
        message_id: the `mid.$…` message id (from any message-returning op).
        emoji: any single emoji (default ❤️). Variation selectors are stripped.
        conversation_id: thread_fbid. Optional — derived from the message's own
            store record when omitted (needs the thread loaded in the store).
        remove: True un-reacts (reaction_status=deleted) instead of adding.
    """
    norm = emoji.replace("\ufe0f", "").replace("\ufe0e", "")
    status = "deleted" if remove else "created"
    guard = await _ensure_writable()
    if guard:
        return guard
    await _ensure_dm()
    return await _write(f"""
    const MID = {json.dumps(message_id)};
    let TID = {json.dumps(str(conversation_id)) if conversation_id else "null"};
    if (!TID) {{
      for (const id of source.getRecordIDs()) {{
        const r = source.get(id);
        if (r && r.__typename === 'SlideMessage' && str(r.message_id) === MID) {{ TID = String(r.thread_fbid || ''); break; }}
      }}
    }}
    if (!TID) return {{ __error: 'not_found', what: 'thread for that message (pass conversation_id, or open the thread so it loads)', ref: MID }};
    let node;
    try {{ node = require('IGDirectReactionSendMutation.graphql'); }}
    catch (e) {{ return {{ __error: 'send_failed', what: 'IG reaction mutation module not loaded: ' + e }}; }}
    const variables = {{ input: {{ emoji: {json.dumps(norm)}, item_id: '', message_id: MID, reaction_status: {json.dumps(status)}, thread_id: TID }} }};
    const res = await commitAndConfirm(node, variables);
    if (!res.ok) return {{ __error: res.loggedOut ? 'logged_out' : 'send_failed', what: res.error }};
    let body = '';
    for (const id of source.getRecordIDs()) {{ const r = source.get(id); if (r && r.__typename === 'SlideMessage' && str(r.message_id) === MID) {{ body = str(r.text_body); break; }} }}
    return {{ status: {json.dumps("removed" if remove else "reacted")}, emoji: {json.dumps(norm)}, messageId: MID, conversationId: TID, reactedTo: body.slice(0, 80) }};
    """, wait_ms=15000, timeout_s=45)


@returns({"state": "string"})
@provides("message_typing", account_param="account")
@timeout(60)
async def send_typing(*, chat, kind="typing", **params):
    """Emit a typing indicator in a thread (`chat` = thread_fbid).

    Rides IG's own MQTT wire by calling the client's `IGDMAPISendTypingIndicator`
    (`MqttBypassDGWClient.send('/ig_send_message', {action:'indicate_activity',
    activity_status, thread_id})`) — the exact fn its composer uses, so there's
    no wire to reimplement (why the readme's transport probe saw no Relay/fetch/
    XHR). `kind`: 'typing' starts, 'paused' stops. The MQTT payload keys on the
    thread's ig `thread_id`, not the `thread_fbid` the app passes — resolve it
    from the store. Signature lifted from the live module (2026-07-07)."""
    await _ensure_dm()
    is_typing = "true" if kind != "paused" else "false"
    return await _eval(f"""
    const TID_FBID = {json.dumps(str(chat))};
    let igid = null;
    for (const id of source.getRecordIDs()) {{
      const r = source.get(id);
      if (r && r.__typename === 'XFBIGDirectViewerThread' && String(r.thread_fbid || '') === TID_FBID) {{ igid = String(r.thread_id || ''); break; }}
    }}
    if (!igid) igid = TID_FBID;
    let fn;
    try {{ fn = require('IGDMAPISendTypingIndicator'); }}
    catch (e) {{ return {{ __error: 'send_failed', what: 'typing module not loaded: ' + e }}; }}
    try {{ await fn(igid, {is_typing}); }}
    catch (e) {{ return {{ __error: 'send_failed', what: 'typing send failed: ' + e }}; }}
    return {{ state: {json.dumps('typing' if is_typing == 'true' else 'paused')} }};
    """, wait_ms=15000, timeout_s=30)


@returns("conversation")
@provides("message_mark_read", account_param="account")
@timeout(60)
async def mark_read(*, conversation_id, **params):
    """Mark a thread's latest message seen — clears its unread badge everywhere.

    Replays IG's own `useIGDMarkThreadAsReadMutation` via `commitMutation`,
    exactly like `send_reaction` replays the reaction mutation (no UI drive, no
    view-yank). Variable shape lifted verbatim from the live hook module
    (`useIGDMarkThreadAsRead`, 2026-07-07 — see operations.md):
    `{data:{item_id:'', message_id:<newest mid>}, metadata:{ig_thread_igid:<thread.thread_id>}}`.
    Note `ig_thread_igid` is the thread's `thread_id` field, NOT `thread_fbid`.
    """
    guard = await _ensure_writable()
    if guard:
        return guard
    await _ensure_dm()
    return await _write(f"""
    const TID = {json.dumps(str(conversation_id))};
    // Resolve the thread record (by thread_fbid) → its ig `thread_id` (the
    // metadata key) + the newest loaded message_id (the read watermark).
    let thread = null, newest = null, newestTs = -1;
    for (const id of source.getRecordIDs()) {{
      const r = source.get(id);
      if (!r) continue;
      if (r.__typename === 'XFBIGDirectViewerThread' && String(r.thread_fbid || '') === TID) thread = r;
      if (r.__typename === 'SlideMessage' && String(r.thread_fbid || '') === TID) {{
        const ts = num(r.timestamp_ms) || 0;
        if (ts > newestTs) {{ newestTs = ts; newest = r; }}
      }}
    }}
    if (!thread) return {{ __error: 'not_found', what: 'thread not loaded (open it or pass a warm conversation_id)', ref: TID }};
    if (!newest) return {{ __error: 'not_found', what: 'no loaded message to mark read', ref: TID }};
    const igid = String(thread.thread_id || '');
    const mid = String(newest.message_id || '');
    let node;
    try {{ node = require('useIGDMarkThreadAsReadMutation.graphql'); }}
    catch (e) {{ return {{ __error: 'send_failed', what: 'mark-read mutation module not loaded: ' + e }}; }}
    const variables = {{ data: {{ item_id: '', message_id: mid }}, metadata: {{ ig_thread_igid: igid }} }};
    const res = await commitAndConfirm(node, variables);
    if (!res.ok) return {{ __error: res.loggedOut ? 'logged_out' : 'send_failed', what: res.error }};
    return {{ id: TID, unreadCount: 0 }};
    """, wait_ms=15000, timeout_s=45)


@returns("conversation")
@provides("message_mark_unread", account_param="account")
@timeout(60)
async def mark_unread(*, conversation_id, **params):
    """Mark a thread unread — sets the blue unread dot, the inverse of mark_read.

    Replays IG's own `IGDThreadListActionsMarkUnreadOptionOffMsysMutation` via
    `commitMutation`, exactly like `mark_read` (no UI drive, no view-yank). The
    variable is `thread_fbid` — the numeric `conversationId` — NOT the ig
    `thread_id` that mark_read uses (RE-confirmed against the live "…" → "Mark
    as unread" request, 2026-07-07).

    The one wrinkle vs mark_read: this mutation module is **lazy**. It only
    registers once the thread-row "…" menu chunk downloads, so `require(...)`
    returns undefined in a cold session (mark_read's module ships in the main
    Direct bundle). We force-load it **headlessly** via the menu's Relay
    entrypoint — `IGDThreadListActionsPopoverOffMsys.entrypoint` is in the main
    bundle even when its chunk isn't, and its `.root` is a `JSResource` whose
    `.load()` pulls the chunk (registering the mutation + its compiled doc_id).
    No real-mouse menu drive, no hardcoded doc_id (Meta rotates it — we read it
    off the compiled node at runtime, like every other op here).

    Args:
        conversation_id: thread_fbid (the numeric conversationId from
            list_conversations).
    """
    guard = await _ensure_writable()
    if guard:
        return guard
    await _ensure_dm()
    return await _write(f"""
    const TID = {json.dumps(str(conversation_id))};
    const MUT = 'IGDThreadListActionsMarkUnreadOptionOffMsysMutation.graphql';
    const EP  = 'IGDThreadListActionsPopoverOffMsys.entrypoint';
    const req = window.__r || window.require;
    let node = null;
    try {{ node = req(MUT); }} catch (e) {{}}
    if (!node) {{
      // Lazy module: force-load the thread-row menu chunk via its entrypoint's
      // JSResource, which transitively registers this mutation. PROVEN cold
      // 2026-07-07: entrypoint.root.load() lands the doc_id where a bare
      // require(MUT) returns undefined.
      try {{
        const ep = req(EP);
        if (ep && ep.root && typeof ep.root.load === 'function') await ep.root.load();
        node = req(MUT);
      }} catch (e) {{ return {{ __error: 'send_failed', what: 'could not force-load mark-unread mutation module: ' + e }}; }}
    }}
    if (!node) return {{ __error: 'send_failed', what: 'mark-unread mutation module unavailable after entrypoint force-load' }};
    const variables = {{ thread_fbid: TID, marked: true }};
    const res = await commitAndConfirm(node, variables);
    if (!res.ok) return {{ __error: res.loggedOut ? 'logged_out' : 'send_failed', what: res.error }};
    return {{ id: TID, unreadCount: 1 }};
    """, wait_ms=15000, timeout_s=45)
