"""WhatsApp — live WhatsApp Web via the engine-held browser session.

Every op is one JS payload evaluated in the WhatsApp Web tab of the
engine-owned Brave instance, through the `browser_session` service
(`services.call` — the engine matchmakes the provider; this app
never sees CDP). WhatsApp's own code has already done both crypto
layers (Noise + Signal); the payloads read the decrypted JS Store
collections (`WAWebCollections`) and call the same action modules the
UI uses (`WAWebSendMsgChatAction`, `WAWebSendReactionMsgAction`, …).

Mapping to shapes happens *in JS* — payloads return shape-native
camelCase dicts so Python stays a thin escape/dispatch/error layer.

IDs are WhatsApp JIDs throughout (`15125555309@c.us`, `…@g.us` for
groups). Ops that take a chat accept a JID or a fuzzy name match.
"""

import json
import mimetypes

from agentos import account, app_error, blobs, provides, qr, returns, services, timeout

_TARGET = "web.whatsapp.com"

# WhatsApp is a standing background subscription, not interactive driving:
# every op runs in the engine's HEADLESS daemon profile (rule 19's sanctioned
# exception). No window ever paints — the messages surface in the Messaging
# app, which IS the shared surface. Pinning the mode on EVERY browser_session
# call keeps web.whatsapp.com on one profile (a stray headed call would spawn a
# second Brave instance + a second QR link). The linked-device entry brands as
# "Google Chrome (AgentOS)" (see login) so headless never reads "Other device".
_MODE = "background"

# ──────────────────────────────────────────────────────────────────────
# JS building blocks
# ──────────────────────────────────────────────────────────────────────

# The me-user probe. getMeUser() was DELETED in WhatsApp's ~mid-2026 Web
# refresh — the phone WID now comes from getMaybeMePnUser() (its LID twin
# is getMaybeMeLidUser). Same return shape as before: a WID with `user`
# (bare phone) and `_serialized` ("…@c.us").
_GET_ME_JS = """
const __getMe = () => {
  try {
    const M = window.require('WAWebUserPrefsMeUser');
    return (M.getMaybeMePnUser && M.getMaybeMePnUser()) || null;
  } catch (e) { return null; }
};
"""

# Wait for the Store to exist and the user to be logged in. On a fresh
# (or expired) session WhatsApp shows the QR screen — that (and only
# that) is `auth_required`. A live Store with no me-user is BINDING
# DRIFT, not logout — fail loud, never a silent auth error (the drift
# class that once masqueraded as "not linked" for a linked session).
_PRELUDE = """
const __deadline = Date.now() + %(wait_ms)d;
""" + _GET_ME_JS + """
let C = null, me = null;
while (Date.now() < __deadline) {
  try { C = window.require('WAWebCollections'); } catch (e) {}
  me = __getMe();
  if (C && C.Chat && me) break;
  // Fast-fail: the QR login screen renders a [data-ref] element —
  // no point waiting out the deadline when we're plainly logged out.
  if (document.querySelector('[data-ref]')) {
    return { __error: 'auth_required' };
  }
  await new Promise(r => setTimeout(r, 250));
}
if (!C || !C.Chat) return { __error: 'not_ready' };
if (!me) return { __error: 'binding_drift',
  what: 'WAWebUserPrefsMeUser.getMaybeMePnUser returned nothing with a live Store' };
const { Chat, Msg, Contact } = C;
"""

# Shape mappers + chat resolution, shared by most payloads.
#
# Sentinel-proofing: unset `__x_` model fields are NOT undefined — they
# are truthy placeholder objects ({sentinel: 'DEFAULT VALUE PLACEHOLDER'}).
# Never branch on truthiness of a model field: string fields go through
# str(), numbers through Number.isFinite/isInteger, booleans compare
# `=== true`.
_HELPERS = """
const str = (v) => typeof v === 'string' ? v : '';
const jidPhone = (jid) => {
  if (!jid) return null;
  const [num, host] = jid.split('@');
  return (host === 'c.us' || host === 's.whatsapp.net') ? '+' + num : null;
};
const account = (jid, displayName, isSaved) => {
  if (!jid) return null;
  // A lid JID (WhatsApp's privacy id, used inside groups) has no phone in
  // the id itself — but the Contact record still carries the real number
  // in __x_phoneNumber (verified via live CDP capture: Contact.get(lidJid)
  // .__x_phoneNumber._serialized is a real '<digits>@c.us', even for a
  // contact with no saved name). Resolve it so the raw lid string never
  // leaks into the UI as a fake "handle".
  let handle = jidPhone(jid);
  if (!handle && jid.endsWith('@lid')) {
    const pn = Contact.get(jid)?.__x_phoneNumber?._serialized;
    handle = pn ? jidPhone(String(pn)) : null;
  }
  const acct = { id: jid, platform: 'whatsapp', handle: handle || jid };
  if (displayName) acct.display_name = displayName;
  // Only stamped when the caller actually resolved it (true/false) — never
  // guessed, so a chat/message where saved-status is unknown just omits
  // the field rather than asserting a wrong default.
  if (typeof isSaved === 'boolean') acct.isSavedContact = isSaved;
  return acct;
};
const iso = (t) => Number.isFinite(t) ? new Date(t * 1000).toISOString() : null;
const chatName = (c) => str(c.__x_name) || str(c.__x_formattedTitle);
// Profile face URL from the in-memory ProfilePicThumb collection (same source
// whatsapp-web.js reads). Prefer the small preview (`img` / previewEurl) for
// the chat-list avatar; skip empty / default-silhouette placeholders. A miss
// is fine — the Messaging monogram is the honest fallback. Server fetch
// (`requestProfilePicFromServer`) is deliberately NOT done on list: too slow
// for a page of chats; warm thumbs already cover recent contacts.
const faceUrlFromThumb = (t) => {
  if (!t) return null;
  const url = str(t.__x_img) || str(t.__x_previewEurl) || str(t.__x_eurl)
    || str(t.img) || str(t.previewEurl) || str(t.eurl);
  if (!url) return null;
  if (/default|avatar_contact|img\\/.*silhouette/i.test(url)) return null;
  return url;
};
const faceUrl = (jid) => {
  if (!jid) return null;
  try {
    const Thumb = C.ProfilePicThumb;
    if (!Thumb) return null;
    let t = typeof Thumb.get === 'function' ? Thumb.get(jid) : null;
    if (!t && typeof Thumb.find === 'function') {
      // find() is async on some builds; ignore a Promise — list stays sync.
      const maybe = Thumb.find(jid);
      if (maybe && typeof maybe.then !== 'function') t = maybe;
    }
    return faceUrlFromThumb(t);
  } catch (e) { return null; }
};
// Status authors often lack a warm ProfilePicThumb — Thumb.find(Wid) pulls
// the CDN URL the same way opening a chat face does. Deduped per jid so a
// ring of N stories only hits the Store once.
const ensureFaceUrl = async (jid) => {
  const warm = faceUrl(jid);
  if (warm) return warm;
  if (!jid) return null;
  try {
    const Thumb = C.ProfilePicThumb;
    const WidFactory = window.require('WAWebWidFactory');
    if (!Thumb || !WidFactory?.createWid) return null;
    const model = await Thumb.find(WidFactory.createWid(jid));
    return faceUrlFromThumb(model);
  } catch (e) { return null; }
};
const stampMissingFaces = async (rows) => {
  const list = Array.isArray(rows) ? rows : (rows ? [rows] : []);
  const need = new Map();
  for (const r of list) {
    if (!r || r.image) continue;
    const jid = str(r.authorId) || str(r.from?.id);
    if (!jid) continue;
    if (!need.has(jid)) need.set(jid, []);
    need.get(jid).push(r);
  }
  await Promise.all([...need.entries()].map(async ([jid, group]) => {
    let url = await ensureFaceUrl(jid);
    if (!url) {
      const phone = group[0]?.from?.id;
      if (phone && phone !== jid) url = await ensureFaceUrl(String(phone));
    }
    if (!url) return;
    for (const r of group) r.image = url;
  }));
  return rows;
};
const mapChat = (c) => {
  const jid = c.__x_id?._serialized || '';
  const out = {
    id: jid,
    name: chatName(c),
    published: iso(c.__x_t),
    isGroup: jid.endsWith('@g.us'),
    isArchived: c.__x_archive === true,
    unreadCount: Number.isInteger(c.__x_unreadCount) ? c.__x_unreadCount : 0,
  };
  // The account this conversation lives on — the linked phone. Same
  // field email uses (conversation.accountEmail): one grammar for
  // "which account does this row belong to" across the message family.
  if (me?.user) out.accountEmail = '+' + me.user;
  const face = faceUrl(jid);
  if (face) out.image = face;
  if (!out.isGroup && jid) {
    // `__x_name` is NOT a reliable saved-contact signal — a lid JID's
    // Contact record mirrors the real name onto __x_name even when it's
    // the counterparty's own self-set profile name (verified via a live
    // CDP capture: an @lid Contact for someone NOT in Joe's address book
    // still carries a real-looking __x_name/__x_formattedTitle). The
    // actual flag WhatsApp Web's own client uses is Contact.__x_
    // syncToAddressbook — true only when synced from the phone's real
    // address book; unset (a sentinel placeholder, never plain false)
    // for a bare WhatsApp profile. Same field the "~name" prefix and raw
    // number in WhatsApp's own UI key off.
    const contact = Contact.get(jid);
    const isSaved = contact?.__x_syncToAddressbook === true;
    out.isSavedContact = isSaved;
    const acct = account(jid, out.name, isSaved);
    if (acct) {
      if (face) acct.image = face;
      out.participant = [acct];
    }
  }
  return out;
};
// Fetch each conversation's face URL in-page (pps.whatsapp.net needs the
// WhatsApp session cookies — a bare <img> from the Messaging app at
// localhost can't). Bytes ride back as `__face` for Python `blobs.put`;
// the CDN URL stays on `image` as a fallback if staging fails.
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
const msgBody = (m) => {
  // For media, __x_body holds the preview thumbnail (base64), never
  // text — the caption is a media message's only text.
  let body = m.__x_type === 'chat' ? str(m.__x_body) : str(m.__x_caption);
  if (!body && m.__x_richResponse?.fragments) {
    body = m.__x_richResponse.fragments
      .filter(f => f.type === 'Text').map(f => f.text).join('\\n');
  }
  return body;
};
const mapMsg = (m) => {
  const chatId = m.__x_id?.remote?._serialized || '';
  const isOutgoing = m.__x_id?.fromMe === true;
  const out = {
    id: m.__x_id?._serialized || '',
    content: msgBody(m),
    published: iso(m.__x_t),
    conversationId: chatId,
    isOutgoing,
    isGroup: chatId.endsWith('@g.us'),
    type: str(m.__x_type) || 'chat',
  };
  // Same account grammar as mapChat: which linked account this message
  // belongs to — a live-watch entity must be self-describing enough to
  // place (and, for a brand-new conversation, to group) without a
  // follow-up read.
  if (me?.user) out.accountEmail = '+' + me.user;
  const c = Chat.get(chatId);
  if (c) out.name = chatName(c);
  if (isOutgoing) {
    out.author = 'Me';
  } else {
    const sender = m.__x_senderObj;
    const senderName = str(sender?.__x_name) || str(sender?.__x_pushname) || str(m.__x_notifyName) || null;
    if (senderName) out.author = senderName;
    const senderJid = m.__x_author?._serialized || m.__x_from?._serialized || null;
    // See mapChat's comment — syncToAddressbook, not __x_name, is the real
    // saved-contact signal. Matters most here: a group's per-message
    // sender is exactly where WhatsApp's own client shows "~Name" for an
    // unsaved member (the header/sidebar only cover 1:1 chats).
    const acct = account(senderJid, senderName, sender?.__x_syncToAddressbook === true);
    if (acct) out.from = acct;
  }
  if (m.__x_star === true) out.isStarred = true;
  // Quoted/reply parent — WhatsApp stores the stanza id on the child and
  // resolves the parent via WAWebQuotedMsgModelUtils. Same shape Instagram/
  // iMessage already emit (`replyTo {id, author, snippet, isOutgoing}`), so
  // the Messaging bubble's quote card lights up without a per-provider branch.
  try {
    const q = window.require('WAWebQuotedMsgModelUtils').getQuotedMsgObj(m);
    if (q) {
      const qOut = q.__x_id?.fromMe === true;
      const qBody = q.__x_type === 'chat' ? str(q.__x_body) : str(q.__x_caption);
      out.replyTo = {
        id: q.__x_id?._serialized || '',
        isOutgoing: qOut,
        snippet: (qBody || '').slice(0, 120),
      };
      if (qOut) {
        out.replyTo.author = 'Me';
      } else {
        const qName = str(q.__x_senderObj?.__x_name)
          || str(q.__x_senderObj?.__x_pushname)
          || str(q.__x_notifyName) || null;
        if (qName) out.replyTo.author = qName;
      }
    }
  } catch (e) {}
  return out;
};
const findChat = (ref) => {
  const exact = Chat.getModelsArray().find(c => c.__x_id?._serialized === ref);
  if (exact) return exact;
  const q = ref.toLowerCase();
  return Chat.getModelsArray().find(c => chatName(c).toLowerCase().includes(q)) || null;
};
// WhatsApp Status (24h stories) — NOT the About bio. One Status model is
// an author's ring; each msg in status.msgs is a story item → shape:post
// with postType:'story'. Expiry is computed client-side (unixTime - t > d,
// d ≈ 86400); we stamp expiresAt = published + 24h. Verified live 2026-07-11.
const STATUS_TTL_S = 86400;
const statusAuthor = (authorJid) => {
  if (!authorJid) return { name: null, contact: null };
  let contact = Contact.get(authorJid) || null;
  // Status rings are often keyed by @lid — resolve the phone Contact too so
  // we get the address-book / push name instead of a bare number or lid.
  const phoneJid = contact?.__x_phoneNumber?._serialized
    || (String(authorJid).endsWith('@c.us') ? authorJid : null);
  if (phoneJid) {
    const byPhone = Contact.get(String(phoneJid));
    if (byPhone) contact = contact || byPhone;
  }
  let name = str(contact?.__x_name) || str(contact?.__x_pushname) || null;
  if (name && (name === authorJid || /@/.test(name))) name = null;
  return { name, contact };
};
const mapStatusPost = (m, status) => {
  const id = m.id?._serialized || m.__x_id?._serialized || '';
  const t = Number.isFinite(m.t) ? m.t : (Number.isFinite(m.__x_t) ? m.__x_t : null);
  const type = str(m.type) || str(m.__x_type) || 'chat';
  // Media: body is the preview thumb — caption (or chat body) is the text.
  const content = type === 'chat' ? str(m.body ?? m.__x_body) : str(m.caption ?? m.__x_caption);
  const authorJid = status?.id?._serialized || status?.__x_id?._serialized
    || m.author?._serialized || m.__x_author?._serialized || '';
  const { name: authorName, contact } = statusAuthor(authorJid);
  const isOutgoing = m.id?.fromMe === true || m.__x_id?.fromMe === true;
  const out = {
    id,
    postType: 'story',
    content,
    published: iso(t),
    expiresAt: Number.isFinite(t) ? iso(t + STATUS_TTL_S) : null,
    isOutgoing,
    mediaType: type,
    viewed: m.viewed === true || m.__x_viewed === true,
    authorId: authorJid,
    ringTotal: Number.isInteger(status?.totalCount ?? status?.__x_totalCount)
      ? (status.totalCount ?? status.__x_totalCount) : null,
    ringUnread: Number.isInteger(status?.unreadCount ?? status?.__x_unreadCount)
      ? (status.unreadCount ?? status.__x_unreadCount) : null,
  };
  if (me?.user) out.accountEmail = '+' + me.user;
  if (isOutgoing) {
    out.author = 'Me';
    out.name = content ? content.slice(0, 80) : 'My status';
  } else {
    if (authorName) out.author = authorName;
    out.name = authorName || content.slice(0, 80) || 'Status';
    const phoneJid = contact?.__x_phoneNumber?._serialized || null;
    const acct = account(phoneJid || authorJid, authorName,
      contact?.__x_syncToAddressbook === true);
    if (acct) out.from = acct;
  }
  const face = faceUrl(authorJid) || (contact?.__x_phoneNumber?._serialized
    ? faceUrl(String(contact.__x_phoneNumber._serialized)) : null);
  if (face) out.image = face;
  return out;
};
// Received reactions live in a separate IndexedDB table keyed by
// parentMsgKey (the reacted-to message's serialized id) — NOT on the
// message model (which only carries __x_hasReaction). mapMsg stays
// synchronous (the watch hook's Msg.on('add') can't await), so
// reactions are a batched post-pass: one DB read for a whole thread,
// aggregated by emoji, merged onto the mapped rows. A blank
// reactionText (or orphan) is a removed reaction — skip it.
const attachReactions = async (mapped, reactedIds) => {
  if (!reactedIds.length) return mapped;
  let rows = [];
  try {
    rows = await window.require('WAWebDBGetReactions')
      .getAllReactionsFromParentMsgs(reactedIds) || [];
  } catch (e) { return mapped; }
  // Aggregate by emoji, folding the variation selectors (U+FE0E/FE0F)
  // so ❤ and ❤️ count as one — the way WhatsApp's own bubble does —
  // while keeping the color-emoji variant (the longer string) to display.
  const VS = new RegExp('[\\uFE0E\\uFE0F]', 'g');
  const norm = (e) => e.replace(VS, '');
  const byParent = new Map();
  for (const r of rows) {
    const emoji = str(r.reactionText);
    if (!emoji || r.orphan) continue;
    const pk = str(r.parentMsgKey);
    if (!byParent.has(pk)) byParent.set(pk, new Map());
    const agg = byParent.get(pk);
    const key = norm(emoji);
    const cur = agg.get(key) || { emoji, count: 0 };
    cur.count += 1;
    if (emoji.length > cur.emoji.length) cur.emoji = emoji;
    agg.set(key, cur);
  }
  const byId = new Map(mapped.map(x => [x.id, x]));
  for (const [pk, agg] of byParent) {
    const row = byId.get(pk);
    if (!row || !agg.size) continue;
    row.reactions = [...agg.values()].sort((a, b) => b.count - a.count);
  }
  return mapped;
};
// Send-side resolution: an exact JID with no existing chat CREATES the
// conversation (WAWebFindChatAction.findOrCreateLatestChat — the same
// action the UI's "new chat" runs). Read ops keep strict findChat; only
// sends may mint a thread. Fuzzy names never create.
const findOrCreateChat = async (ref) => {
  const existing = findChat(ref);
  if (existing) return existing;
  if (!ref.includes('@')) return null;
  try {
    const wid = window.require('WAWebWidFactory').createWid(ref);
    const r = await window.require('WAWebFindChatAction').findOrCreateLatestChat(wid);
    return (r && r.chat) || null;
  } catch (e) { return null; }
};
// Wire-level send result, bounded. addAndSendMsgToChat's inner promise is
// the only wire truth — and on a tab whose session socket died (the
// second-tab steal: another web.whatsapp.com tab claimed the session while
// this one still answers evals and check_session stays green) it never
// settles at all: the message parks at ack 0 and the eval would burn its
// full 45s timeout as UNKNOWN. Race it against a 15s clock — a healthy
// send settles in well under a second — and turn the park into a typed
// send_failed carrying the live diagnosis. Returns null on wire success.
const awaitSend = async (sendPromise, chat, key) => {
  const parked = Symbol('parked');
  const result = await Promise.race([
    sendPromise,
    new Promise(r => setTimeout(() => r(parked), 15000)),
  ]);
  if (result === parked) {
    let connRef = null, socketState = null;
    try { connRef = !!window.require('WAWebConnModel').Conn?.ref; } catch (e) {}
    try { socketState = String(window.require('WAWebSocketModel').Socket?.state ?? null); } catch (e) {}
    const msg = chat.msgs.getModelsArray().find(m => m.__x_id?._serialized === key._serialized);
    const ack = msg && Number.isInteger(msg.__x_ack) ? msg.__x_ack : null;
    return { __error: 'send_failed', what:
      `the send promise did not settle within 15s — the message is parked at ` +
      `ack ${ack} (Socket.state: ${socketState}, Conn.ref: ${connRef}; the ` +
      `parked ack is the tell — Socket.state reads CONNECTED even on a dead ` +
      `wire). The tab's session socket is dead even though the page answers ` +
      `evals. Run browser_session verb:reload for target web.whatsapp.com — ` +
      `a fresh page load re-negotiates the socket — then retry. If it parks ` +
      `again, a duplicate web.whatsapp.com tab may hold the session; the ` +
      `engine closes duplicates when the session next reattaches ` +
      `(browser_session close, then retry the send).` };
  }
  if (result?.messageSendResult !== 'OK') {
    return { __error: 'send_failed', what: JSON.stringify(result ?? null) };
  }
  return null;
};
"""


def _payload(body: str, *, wait_ms: int) -> str:
    """Wrap an op body in the async IIFE with readiness wait + helpers."""
    return ("(async () => {"
            + (_PRELUDE % {"wait_ms": wait_ms})
            + _HELPERS
            + body
            + "})()")


async def _eval(body: str, *, wait_ms: int = 20000, timeout_s: int = 45):
    """Run an op body in the WhatsApp Web tab; surface structured errors."""
    # The provider returns `{value: <js value>}`; the engine's
    # value-envelope unwrap hands us the JS value directly.
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
                "WhatsApp Web is not linked. Run whatsapp.login to get a QR "
                "challenge — scan it with your phone (Settings → Linked "
                "Devices), then retry this op.",
                code="NeedsAuth",
            )
        if code == "not_ready":
            return app_error(
                "WhatsApp Web's module system never came up — the page may "
                "still be loading, or a WhatsApp update changed the internals.",
                code="NotReady",
            )
        if code == "not_found":
            return app_error(
                f"No match for {value.get('ref')!r} ({value.get('what', 'item')}).",
                code="NotFound",
            )
        if code == "send_failed":
            return app_error(
                f"WhatsApp send failed: {value.get('what')}",
                code="SendFailed",
            )
        if code == "binding_drift":
            return app_error(
                "WhatsApp Web shipped a breaking update: "
                f"{value.get('what')}. Re-derive the module bindings from "
                "whatsapp-web.js (see the readme's Internals section) — this "
                "is drift, not an auth problem.",
                code="BindingDrift",
            )
        return app_error(f"WhatsApp payload error: {code}", code="PayloadError")

    return value


async def _stage_faces(rows):
    """Move in-page `__face` bytes onto `conversation.image` as a blob path.

    Content-addressed (`blobs.put`): re-listing the same face is a free
    dedup. `mimeType` rides alongside so the graph remembers a real image,
    not a bare URL. Failures leave the CDN URL (if any) for the UI fallback.
    """
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


# ──────────────────────────────────────────────────────────────────────
# The account trio — check / login / logout
# ──────────────────────────────────────────────────────────────────────
#
# WhatsApp Web has no cookie credential and no api key: the "session" is
# the linked-device state inside the engine-owned Brave profile. So these
# ops don't ride a cookie connection — they call `browser_session` like
# every other op. They tolerate the logged-out QR screen (the shared
# `_PRELUDE` fast-fails on it); each carries its own readiness wait.

# Identity JS — shared by check_session and login's logged-in arm. Both
# callers already hold `me` (the WID) in scope; `me.user` is the phone
# (no '+'); pushname is the device's display name.
_IDENTITY_JS = """
  let pushname = '';
  try { pushname = window.require('WAWebConnModel').Conn?.pushname || ''; } catch (e) {}
  const phone = me?.user ? '+' + me.user : null;
  const acct = {
    authenticated: true,
    at: { shape: 'product', name: 'WhatsApp', url: 'https://www.whatsapp.com/' },
    platform: 'whatsapp',
  };
  if (phone) { acct.identifier = phone; acct.handle = phone; acct.phone = phone; }
  if (pushname) acct.displayName = pushname;
"""


@account.check
@returns("account")
@timeout(45)
async def check_session(**params):
    """Verify the WhatsApp Web link and identify the account.

    Returns the `account` (phone → identifier, pushname → displayName)
    when a device is linked. On the QR screen — no link — returns
    `{authenticated: false}` so the resolver knows to drive `login`.
    """
    js = """(async () => {
  const deadline = Date.now() + 12000;
""" + _GET_ME_JS + """
  let C = null, me = null;
  while (Date.now() < deadline) {
    try { C = window.require('WAWebCollections'); } catch (e) {}
    me = __getMe();
    if (C && C.Chat && me) break;
    if (document.querySelector('[data-ref]')) return { authenticated: false };
    await new Promise(r => setTimeout(r, 250));
  }
  // A live Store with no me-user is binding drift, not logout — say so
  // instead of sending the resolver on a phantom re-link.
  if (!me && C && C.Chat) return { __error: 'binding_drift' };
  if (!me) return { authenticated: false };
""" + _IDENTITY_JS + """
  return acct;
})()"""
    value = await services.call("browser_session", verb="eval", params={
        "target": _TARGET, "mode": _MODE, "js": js, "timeout": 30,
    })
    if isinstance(value, dict) and value.get("__error") == "binding_drift":
        return app_error(
            "WhatsApp Web shipped a breaking update: the me-user probe "
            "returned nothing with a live Store. Re-derive the bindings "
            "from whatsapp-web.js — this is drift, not a missing link.",
            code="BindingDrift",
        )
    return value


@account.login
@returns("account | auth_challenge")
@timeout(60)
async def login(**params):
    """Link this WhatsApp — or report the account if already linked.

    Returns the `account` when a device is already linked. Otherwise
    reads the linked-device QR off the page (`div[data-ref]`, clicking
    the stale-refresh overlay if WhatsApp has parked one), renders it as
    a scannable Unicode block, and returns an `auth_challenge{kind: qr}`.
    The human scans it from any surface (chat, desktop act window); poll
    `check_session` to confirm the link took.
    """
    js = """(async () => {
  // Brand the linked-device entry before pairing. WhatsApp derives the
  // phone's Linked Devices entry from WAWebBrowserInfo() at registration
  // time: `name` maps to the platform icon (Chrome|Firefox|Opera|Safari|
  // Edge — anything else renders the gray "?" / "Other device"), `os` is
  // the parenthetical. Headless UA parses as "Chrome Headless", which is
  // outside that enum — hence "Other device". Swap the module export so
  // the entry reads "Google Chrome (AgentOS)" with the Chrome icon.
  try {
    const reg = window.require('__debug').modulesMap.WAWebBrowserInfo;
    const orig = reg.defaultExport;  // require() serves defaultExport
    if (typeof orig === 'function' && !orig.__agentos) {
      const branded = () => ({ ...orig(), name: 'Chrome', os: 'AgentOS' });
      branded.__agentos = true;
      reg.defaultExport = branded;
      if (reg.exports && typeof reg.exports === 'object') reg.exports.default = branded;
    }
    window.require('WAWebUA').UA.browser = 'chrome';
  } catch (e) {}
  const deadline = Date.now() + 25000;
""" + _GET_ME_JS + """
  let C = null, me = null, ref = null;
  while (Date.now() < deadline) {
    try { C = window.require('WAWebCollections'); } catch (e) {}
    me = __getMe();
    if (C && C.Chat && me) break;
    // The QR canvas carries the linking payload as `data-ref`. WhatsApp
    // parks a click-to-refresh overlay over a stale code after ~5
    // rotations — click it so a live ref renders, then read it.
    const stale = document.querySelector('[data-ref] button, button[aria-label*="Reload" i]');
    if (stale) { try { stale.click(); } catch (e) {} await new Promise(r => setTimeout(r, 600)); }
    const el = document.querySelector('[data-ref]');
    const candidate = el && el.getAttribute('data-ref');
    if (candidate) { ref = candidate; break; }
    await new Promise(r => setTimeout(r, 300));
  }
  if (me) {
""" + _IDENTITY_JS + """
    return acct;
  }
  if (ref) return { __challenge: ref };
  return { __error: 'not_ready' };
})()"""
    value = await services.call("browser_session", verb="eval", params={
        "target": _TARGET, "mode": _MODE, "js": js, "timeout": 45,
    })

    if isinstance(value, dict) and value.get("__error") == "not_ready":
        return app_error(
            "WhatsApp Web hasn't shown a QR code yet — the page may still "
            "be loading, or the engine-owned browser has no web.whatsapp.com "
            "tab open.",
            code="NotReady",
        )
    if isinstance(value, dict) and "__challenge" in value:
        ref = value["__challenge"]
        return {
            "name": "Link WhatsApp",
            "kind": "qr",
            "payload": ref,
            "artifact": qr.text(ref),
            "instructions": (
                "Scan this QR with WhatsApp on your phone — "
                "Settings → Linked Devices → Link a Device. The code "
                "rotates about every 20 seconds; re-run login for a fresh "
                "one. Poll check_session to confirm the link took."
            ),
            "continueWith": "check_session",
        }
    # Already linked — the account arm.
    return value


@account.logout
@returns({"ok": "boolean", "message": "string"})
@timeout(45)
async def logout(**params):
    """Unlink this device — WhatsApp Web's own "Log out".

    Calls `socketLogout(LogoutReason.UserInitiated)` — the exact action
    the Linked Devices "Log out" button fires. The link is revoked
    server-side (the phone drops this device), so it's a real logout, not
    a local-cache wipe; the next `login` renders a fresh QR.
    """
    js = """(async () => {
  try {
    const reason = window.require('WAWebLogoutReasonConstants').LogoutReason.UserInitiated;
    await window.require('WAWebSocketLogoutJob').socketLogout(reason);
    return { ok: true, message: 'WhatsApp device unlinked.' };
  } catch (e) {
    return { ok: false, message: 'Logout failed: ' + String(e) };
  }
})()"""
    return await services.call("browser_session", verb="eval", params={
        "target": _TARGET, "mode": _MODE, "js": js, "timeout": 30,
    })


# ──────────────────────────────────────────────────────────────────────
# Conversations
# ──────────────────────────────────────────────────────────────────────


@returns("conversation[]")
@provides("chats", account_param="account")
@timeout(90)
async def list_conversations(*, archived=False, limit=200, **params):
    """List WhatsApp conversations from the live Web session.

    Each row carries `image` when a profile thumb is warm in the Store —
    fetched in-page (session cookies) and staged into the blob store so
    Messaging can render it via `/thumb` (pps.whatsapp.net URLs aren't
    reachable from the shell origin).

    Mirrors WhatsApp's own chat list: a Chat model with no sort timestamp
    (`__x_t`) is a local stub — minted by `findOrCreateLatestChat` when
    Messaging starts a new chat, carrying only e2e/notification rows —
    and the official UI never shows it. Dumping the raw Store here used
    to surface those as phantom number rows.

    Args:
        archived: When true, list archived chats instead of active ones.
        limit: Maximum conversations to return (most recent first).
    """
    rows = await _eval(f"""
    const chats = Chat.getModelsArray()
      .filter(c => !!c.__x_archive === {json.dumps(bool(archived))})
      // Real threads always have a finite sort time; createdLocally stubs
      // from new-chat (no message yet) leave `__x_t` as a sentinel / unset.
      .filter(c => Number.isFinite(c.__x_t))
      .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))
      .slice(0, {int(limit)});
    return await hydrateFaces(chats.map(mapChat));
    """, timeout_s=75)
    return await _stage_faces(rows)


@returns("conversation")
@timeout(60)
async def get_conversation(*, id, **params):
    """Get one conversation by JID or fuzzy name.

    Args:
        id: Chat JID (`...@c.us` / `...@g.us`) or a name substring.
    """
    row = await _eval(f"""
    const chat = findChat({json.dumps(id)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(id)} }};
    return await hydrateFaces(mapChat(chat));
    """)
    return await _stage_faces(row)


@returns("conversation")
@provides("message_mark_read", account_param="account")
@timeout(60)
async def mark_read(*, conversation_id, **params):
    """Mark a conversation read — the human-world side of reading.

    Sends WhatsApp's own read receipt (the action the UI fires when a
    chat is opened): the counterparty sees blue ticks per their privacy
    settings, and the unread badge clears on every linked device.
    Reading a chat on the user's behalf isn't finished until this runs.

    Args:
        conversation_id: Chat JID or name substring.
    """
    return await _eval(f"""
    const chat = findChat({json.dumps(conversation_id)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(conversation_id)} }};
    // Mirror the UI's mark-as-read exactly: clear the manual
    // marked-unread flag (the source of unreadCount: -1), then send
    // the receipt — which also zeroes a real unread count. sendSeen
    // takes an options object with a `chat` key, NOT the bare model
    // whatsapp-web.js passes; afterAvailable: false sends through the
    // headless tab's "unavailable" stream instead of deferring the
    // receipt until the tab becomes visible (which it never does).
    chat.markedUnread = false;
    await window.require('WAWebUpdateUnreadChatAction')
      .sendSeen({{ chat, afterAvailable: false }});
    return mapChat(chat);
    """)


@returns("conversation")
@provides("message_mark_unread", account_param="account")
@timeout(60)
async def mark_unread(*, conversation_id, **params):
    """Mark a conversation unread — WhatsApp's own "Mark as unread" toggle
    (the chat-list right-click action), not an un-send of a read receipt.

    Fires the exact command the UI runs, `WAWebCmd.Cmd.markChatUnread(chat,
    true)` — the sibling of `archiveChat` — so the state SYNCS to the phone
    and every linked device (not just this headless tab). WhatsApp stores a
    manual mark-unread as the **`__x_unreadCount = -1` sentinel** (there's no
    real message count, just the flag), so a consumer must treat unread as
    `!== 0`, not `> 0`. The command resolves BEFORE the model reflects the
    new count, so we poll the chat until `__x_unreadCount` settles to nonzero
    before mapping — otherwise the return (and any read-back) races the
    mutation and reports a stale `0`, and the badge flickers off.

    Args:
        conversation_id: Chat JID or name substring.
    """
    return await _eval(f"""
    const chat = findChat({json.dumps(conversation_id)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(conversation_id)} }};
    const {{ Cmd }} = window.require('WAWebCmd');
    await Cmd.markChatUnread(chat, true);
    // The count mutates a tick after the command resolves — wait for the
    // -1 sentinel so mapChat (and the consumer's read-back) sees it land.
    const settled = Date.now() + 3000;
    while (Date.now() < settled && !(Number.isInteger(chat.__x_unreadCount) && chat.__x_unreadCount !== 0)) {{
      await new Promise(r => setTimeout(r, 50));
    }}
    return mapChat(chat);
    """)


@returns("conversation")
@provides("message_archive", account_param="account")
@timeout(60)
async def set_archived(*, conversation_id, archived=True, **params):
    """Archive or unarchive a conversation — WhatsApp Web's own action.

    Fires the exact action the UI's Archive / Unarchive runs
    (`WAWebCmd.Cmd.archiveChat(chat, archived)`): the chat moves to (or
    out of) the Archived shelf on every linked device. Fully reversible
    and silent — the counterparty is never notified either way.

    Declared as a brokered capability (`@provides("message_archive")`), the
    same way `message_mark_read` / `message_react` are: the Messaging app's
    Archive verb exists iff a provider declares it for the chat's account, so
    a network with no archive concept (iMessage) never shows the button —
    the app names no plugin. One capability, both directions: `archived`
    picks the direction (a provider can't archive but-not-unarchive), so
    this is ONE `@provides`, not a mark-read/mark-unread-style pair.

    Args:
        conversation_id: Chat JID or name substring.
        archived: True to archive (default), False to unarchive.
    """
    return await _eval(f"""
    const chat = findChat({json.dumps(conversation_id)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(conversation_id)} }};
    // Cmd is nested under the module export, same as openChatAt. The second
    // arg is the target state (true archives, false unarchives). __x_archive
    // flips a tick AFTER archiveChat resolves — the same race as markChatUnread
    // (NOT "as the action resolves", verified 2026-07-09) — so poll until the
    // model settles to the target before mapping. Otherwise mapChat (and the
    // caller's read-back) races the mutation and returns the stale pre-toggle
    // isArchived. The predicate mirrors mapChat's own `__x_archive === true`
    // derivation, so an unset sentinel resolves correctly in both directions.
    const want = {json.dumps(bool(archived))};
    const {{ Cmd }} = window.require('WAWebCmd');
    await Cmd.archiveChat(chat, want);
    const settled = Date.now() + 3000;
    while (Date.now() < settled && (chat.__x_archive === true) !== want) {{
      await new Promise(r => setTimeout(r, 50));
    }}
    return mapChat(chat);
    """)


# ──────────────────────────────────────────────────────────────────────
# Messages
# ──────────────────────────────────────────────────────────────────────


@returns("message[]")
@timeout(90)
async def list_messages(*, conversation_id=None, is_unread=None, limit=200, **params):
    """List messages — for one conversation, unread across chats, or recent.

    Args:
        conversation_id: Chat JID or name substring. Loads earlier history
            until `limit` messages are in memory (or history is exhausted).
        is_unread: When true (and no conversation_id), return unread
            messages across all chats.
        limit: Maximum messages to return (newest first).
    """
    limit = int(limit)
    if conversation_id is not None:
        return await _eval(f"""
        const chat = findChat({json.dumps(conversation_id)});
        if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(conversation_id)} }};
        const chatId = chat.__x_id?._serialized;
        const loader = window.require('WAWebChatLoadMessages');
        const inChat = () => Msg.getModelsArray()
          .filter(m => (m.__x_id?.remote?._serialized || '') === chatId);
        for (let i = 0; i < 15 && inChat().length < {limit}; i++) {{
          let before = inChat().length;
          try {{ await loader.loadEarlierMsgs({{ chat }}); }} catch (e) {{ break; }}
          if (inChat().length === before) break;
        }}
        const models = inChat()
          .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))
          .slice(0, {limit});
        const mapped = models.map(mapMsg);
        // Received reactions: one batched DB read for the messages that
        // carry any, merged onto the mapped rows as reactions[].
        const reactedIds = models
          .filter(m => m.__x_hasReaction === true)
          .map(m => m.__x_id?._serialized)
          .filter(Boolean);
        await attachReactions(mapped, reactedIds);
        return mapped;
        """, timeout_s=75)

    if is_unread:
        return await _eval(f"""
        const out = [];
        for (const chat of Chat.getModelsArray()) {{
          const unread = chat.__x_unreadCount ?? 0;
          if (unread <= 0) continue;
          const chatId = chat.__x_id?._serialized;
          const msgs = Msg.getModelsArray()
            .filter(m => (m.__x_id?.remote?._serialized || '') === chatId && !m.__x_id?.fromMe)
            .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))
            .slice(0, unread);
          out.push(...msgs);
        }}
        return out
          .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))
          .slice(0, {limit})
          .map(mapMsg);
        """)

    return await _eval(f"""
    return Msg.getModelsArray()
      .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))
      .slice(0, {limit})
      .map(mapMsg);
    """)


# Media payloads bigger than this stay un-hydrated: the bytes cross the
# CDP eval channel as base64 inside one JSON value, and the Python
# worker's stdin reader caps a line at 16MB. ~10MB binary ≈ 13.7MB
# base64 — comfortably under with room for the rest of the envelope.
_MEDIA_HYDRATION_CAP = 10 * 1024 * 1024

_MEDIA_TYPES = ("image", "video", "ptt", "audio", "document", "sticker")


def _media_shape(mime: str, msg_type: str) -> str:
    """The concrete file subtype for an attachment — widest type `file`."""
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/") or msg_type == "ptt":
        return "sound"
    return "file"


@returns("message")
@provides("get_message", account_param="account")
@timeout(120)
async def get_message(*, id, **params):
    """Get one message by its serialized id.

    Declared as a brokered capability (`@provides`), the same way
    `set_presence` / `message_send_media` are: a provider that can hydrate
    a message's media bytes on demand declares `get_message`, so the
    Messaging app gates inbound-media *rendering* on "who provides
    get_message" (WhatsApp today, iMessage too, any future connector that
    declares it) rather than on a plugin id. Routing is unchanged — the app
    still reaches it as `services.chats {verb: get_message}`; the `@provides`
    only lights it up for `useServiceProviders('get_message')`.

    Media messages (image / video / voice note / document / sticker)
    hydrate on read: the decrypted payload is downloaded in-page,
    stored in the engine's content-addressed blob store, and returned
    as a file entity attached to the message — `attaches[0].path` is
    the on-disk file. Payloads over 10MB stay caption-only (the readme
    documents the cap).

    Args:
        id: Serialized message id (from `list_messages` results).
    """
    entity = await _eval(f"""
    const msg = Msg.get({json.dumps(id)});
    if (!msg) return {{ __error: 'not_found', what: 'message', ref: {json.dumps(id)} }};
    const out = mapMsg(msg);
    const mediaTypes = {json.dumps(list(_MEDIA_TYPES))};
    const size = Number.isFinite(msg.__x_size) ? msg.__x_size : 0;
    if (mediaTypes.includes(out.type) && size > 0 && size <= {_MEDIA_HYDRATION_CAP}) {{
      // Ensure the encrypted payload is fetched (no-op when already
      // RESOLVED), then decrypt it the way the UI does on click.
      if (msg.mediaData && msg.mediaData.mediaStage !== 'RESOLVED') {{
        await msg.downloadMedia({{ downloadEvenIfExpensive: true, rmrReason: 1 }});
      }}
      const buf = await window.require('WAWebDownloadManager').downloadManager
        .downloadAndMaybeDecrypt({{
          directPath: msg.directPath,
          encFilehash: msg.encFilehash,
          filehash: msg.filehash,
          mediaKey: msg.mediaKey,
          mediaKeyTimestamp: msg.mediaKeyTimestamp,
          type: msg.type,
          signal: new AbortController().signal,
          downloadQpl: {{ addAnnotations() {{ return this; }}, addPoint() {{ return this; }} }},
        }});
      const bytes = new Uint8Array(buf);
      let bin = '';
      const CHUNK = 0x8000;
      for (let i = 0; i < bytes.length; i += CHUNK) {{
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
      }}
      // __x_duration is a string of seconds on ptt/audio/video models.
      const dur = Number(str(msg.__x_duration));
      out.__media = {{
        data: btoa(bin),
        mime: str(msg.__x_mimetype),
        filename: str(msg.__x_filename),
        size: bytes.length,
        duration: Number.isFinite(dur) && dur > 0 ? dur : null,
      }};
    }}
    return out;
    """, timeout_s=90)

    media = entity.pop("__media", None) if isinstance(entity, dict) else None
    if media and media.get("data"):
        mime = media.get("mime") or "application/octet-stream"
        ext = (media.get("filename") or "").rsplit(".", 1)[-1] if "." in (media.get("filename") or "") \
            else (mime.split("/")[-1].split(";")[0] or "bin")
        blob = await blobs.put(media["data"], ext=ext)
        attachment = {
            "shape": _media_shape(mime, entity.get("type", "")),
            "name": media.get("filename") or f"{entity.get('type', 'media')} {entity.get('published', '')}".strip(),
            "filename": media.get("filename") or None,
            "mimeType": mime,
            "size": media.get("size"),
            "path": blob["path"],
            "sha": blob["sha256"],
        }
        if media.get("duration"):
            attachment["durationMs"] = int(media["duration"] * 1000)
        entity["attaches"] = [attachment]
    return entity


@returns("message[]")
@timeout(90)
async def search_messages(*, query, conversation_id=None, limit=100, page=1, **params):
    """Search messages server-side — WhatsApp's own search, full history.

    The same search the Web UI's search box runs: results come from
    the server, not just messages loaded in memory, so history from
    weeks or years back is reachable. Optionally scoped to one chat.

    Args:
        query: Search text (WhatsApp's own matching — words, not regex).
        conversation_id: Chat JID or name substring to scope the search.
        limit: Maximum messages per page (newest first).
        page: 1-based result page — raise it to walk deeper history.
    """
    scope = ""
    remote = "undefined"
    if conversation_id is not None:
        scope = f"""
    const scopeChat = findChat({json.dumps(conversation_id)});
    if (!scopeChat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(conversation_id)} }};
    """
        remote = "scopeChat.__x_id?._serialized"
    return await _eval(scope + f"""
    const {{ messages }} = await Msg.search(
      {json.dumps(query)}, {int(page)}, {int(limit)}, {remote});
    return (messages || []).map(mapMsg);
    """, timeout_s=75)


# ──────────────────────────────────────────────────────────────────────
# People
# ──────────────────────────────────────────────────────────────────────


@returns("person[]")
@timeout(90)
async def list_persons(*, conversation_id=None, limit=200, **params):
    """List contacts, or a group conversation's participants.

    Args:
        conversation_id: Group JID or name substring. Opens the chat to
            trigger WhatsApp's lazy participant load, then resolves each
            participant (LIDs included) to name + phone via Contacts.
        limit: Maximum people to return.
    """
    limit = int(limit)
    if conversation_id is not None:
        return await _eval(f"""
        const chat = findChat({json.dumps(conversation_id)});
        if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(conversation_id)} }};
        // Participants are lazy-loaded — opening the chat populates them.
        // (WAWebCmd exports the Cmd singleton since ~mid-2026; openChatAt
        // is a method on it, no longer a top-level export.)
        const {{ Cmd }} = window.require('WAWebCmd');
        try {{ await Cmd.openChatAt({{ chat }}); }} catch (e) {{}}
        for (let i = 0; i < 10; i++) {{
          if (chat.__x_groupMetadata?.participants?.getModelsArray?.()?.length) break;
          await new Promise(r => setTimeout(r, 500));
        }}
        const parts = chat.__x_groupMetadata?.participants?.getModelsArray?.() || [];
        return parts.slice(0, {limit}).map(p => {{
          const pid = p.__x_id?._serialized || '';
          let contact = null;
          try {{ contact = Contact.get(pid); }} catch (e) {{}}
          const name = contact?.__x_name || contact?.__x_pushname || pid;
          const phoneJid = contact?.__x_phoneNumber?._serialized || (pid.endsWith('@c.us') ? pid : null);
          const out = {{ id: pid, name }};
          const acct = account(phoneJid || pid, name);
          if (acct) out.accounts = [acct];
          if (p.__x_isAdmin || p.__x_isSuperAdmin) out.content = 'group admin';
          return out;
        }});
        """, timeout_s=75)

    return await _eval(f"""
    return Contact.getModelsArray()
      .filter(c => c.__x_name && (c.__x_id?._serialized || '').endsWith('@c.us'))
      .slice(0, {limit})
      .map(c => {{
        const jid = c.__x_id?._serialized || '';
        const out = {{ id: jid, name: c.__x_name }};
        if (c.__x_pushname && c.__x_pushname !== c.__x_name) out.nickname = c.__x_pushname;
        const status = c.__x_status?.__x_status;
        if (status) out.content = status;
        const acct = account(jid, c.__x_name);
        if (acct) {{
          if (status) acct.bio = status;
          out.accounts = [acct];
        }}
        return out;
      }});
    """)


# ──────────────────────────────────────────────────────────────────────
# Actions
# ──────────────────────────────────────────────────────────────────────


@returns("message")
@provides("message_send", account_param="account")
@provides("message_reply", account_param="account")
@timeout(60)
async def send_message(*, to, text, reply_to=None, **params):
    """Send a WhatsApp message; returns the sent message entity.

    Also `@provides("message_reply")` — the same tool, gated as a capability
    so the Messaging app's hover-reply affordance lights up only for
    providers that can thread a send onto a parent message. Pass `reply_to`
    (a serialized message id from any message-returning op) to quote it.

    Args:
        to: Chat JID or contact/group name substring. An exact JID with
            no existing chat starts the conversation (a name never does).
        text: Message text to send.
        reply_to: Optional serialized message id to quote (WhatsApp's own
            reply/quote). The parent must be loaded in the chat.
    """
    # A minimal {body, type} dict no longer sends — WhatsApp's model layer
    # builds an empty husk from it, addAndSendMsgToChat resolves anyway,
    # and the inner send promise rejects out of sight. Build the full
    # message the way whatsapp-web.js does, and await BOTH promises:
    # addAndSendMsgToChat resolves to [msg, sendPromise], and only the
    # inner promise carries wire-level success/failure.
    # Quoted reply: resolve the parent, then spread msgContextInfo(chat)
    # into the construct — the same fields whatsapp-web.js's
    # quotedMessageId path attaches (quotedMsg / quotedStanzaID /
    # quotedParticipant / quotedRemoteJid).
    reply_js = "null"
    if reply_to:
        reply_js = json.dumps(reply_to)
    return await _eval(f"""
    const chat = await findOrCreateChat({json.dumps(to)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(to)} }};
    const replyTo = {reply_js};
    let quoted = {{}};
    if (replyTo) {{
      const parent = chat.msgs.getModelsArray()
        .find(m => m.__x_id?._serialized === replyTo)
        || Msg.get(replyTo)
        || (await window.require('WAWebCollections').Msg.getMessagesById([replyTo]))?.messages?.[0];
      if (!parent) return {{ __error: 'not_found', what: 'message', ref: replyTo }};
      const ReplyUtils = window.require('WAWebMsgReply');
      const canReply = ReplyUtils
        ? ReplyUtils.canReplyMsg(parent.unsafe ? parent.unsafe() : parent)
        : (parent.canReply ? parent.canReply() : true);
      if (!canReply) return {{ __error: 'send_failed', what:
        'WhatsApp says that message cannot be replied to' }};
      quoted = parent.msgContextInfo(chat) || {{}};
    }}
    const {{ addAndSendMsgToChat }} = window.require('WAWebSendMsgChatAction');
    const mk = window.require('WAWebMsgKey');
    const key = mk.fromString('true_' + chat.__x_id._serialized + '_' + (await mk.newId()) + '_out');
    const [, sendPromise] = await addAndSendMsgToChat(chat, {{
      id: key,
      body: {json.dumps(text)},
      type: 'chat',
      t: Math.floor(Date.now() / 1000),
      from: me,
      to: chat.__x_id,
      self: 'out',
      ack: 0,
      isNewMsg: true,
      local: true,
      ...quoted,
    }});
    const failed = await awaitSend(sendPromise, chat, key);
    if (failed) return failed;
    // The tuple's msg element is a detached husk; the live model lands in
    // the chat's own collection under the key we minted.
    const sent = chat.msgs.getModelsArray().find(m => m.__x_id?._serialized === key._serialized);
    if (sent) return mapMsg(sent);
    return {{
      id: key._serialized,
      content: {json.dumps(text)},
      published: new Date().toISOString(),
      conversationId: chat.__x_id._serialized,
      isOutgoing: true,
      type: 'chat',
      name: chatName(chat),
      author: 'Me',
    }};
    """)


@returns("message")
@provides("message_send_media", account_param="account")
@timeout(180)
async def send_media(*, to, path=None, bytes=None, filename=None, caption=None, ptt=False, **params):
    """Send media — an image, video, document, or voice note.

    Two ways in, one send path. Forward an already-stored blob by
    `path` (inbound media hydrated by `get_message` lives in the store,
    as does anything an agent staged with `blobs.put`). Or hand raw
    `bytes` (base64) + a `filename` — the fresh-upload door for the UI's
    attach button and for an MCP-only agent, neither of which can reach
    the kernel-private `blobs.put`. Inline bytes stage into the store
    here (the worker has blob access), so from that point on both routes
    are identical: the engine reads the bytes (`blobs.get` — apps never
    open files), then WhatsApp's own media pipeline runs — prep, encrypt,
    upload, send. Returns the sent message entity with its attachment
    block, same shape as inbound media.

    (`bytes`, not `data`: the engine reserves `data`/`cache` as the
    param names it injects an app's own persistent storage under, so a
    tool param named `data` is silently overwritten.)

    Args:
        to: Chat JID or contact/group name substring.
        path: Blob-store path (from `blobs.put` or a hydrated
            attachment's `path`). Mutually exclusive with `bytes`.
        bytes: Base64 bytes to send — staged into the blob store here.
            Requires `filename`. Mutually exclusive with `path`.
        filename: Original filename for `bytes` — names the file the
            recipient sees (a document keeps its name) and types the
            payload by extension.
        caption: Optional text shown under the media.
        ptt: Send audio as a voice note (push-to-talk bubble).
            Requires an ogg/opus file.
    """
    if bytes is not None:
        if not filename:
            return app_error(
                "send_media with `bytes` needs a `filename` (for type + extension).",
                code="BadParams",
            )
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        stored = await blobs.put(bytes, ext=ext)
        path = stored["path"]
    elif not path:
        return app_error(
            "send_media needs either `path` (a blob-store path) or "
            "`bytes` (base64) + `filename`.",
            code="BadParams",
        )
    blob = await blobs.get(path)
    if blob.get("size", 0) > _MEDIA_HYDRATION_CAP:
        return app_error(
            f"Blob is {blob.get('size')} bytes — payloads over "
            f"{_MEDIA_HYDRATION_CAP} can't cross the eval channel "
            "(same cap as inbound hydration).",
            code="TooLarge",
        )
    # A passed filename keeps the recipient-visible name (documents);
    # else the blob's sha-name stands in (fine for images/voice notes).
    filename = filename or path.rsplit("/", 1)[-1]
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    entity = await _eval(f"""
    const chat = await findOrCreateChat({json.dumps(to)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(to)} }};

    // Rebuild a File from the blob bytes — WhatsApp's pipeline starts
    // from the same object the attach button hands it.
    const bin = atob({json.dumps(blob["data"])});
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const file = new File([bytes], {json.dumps(filename)}, {{ type: {json.dumps(mime)} }});

    // Prep (transcode/thumbnail/hash) exactly as the UI does.
    const OpaqueData = window.require('WAWebMediaOpaqueData');
    const opaqueData = await OpaqueData.createFromData(file, {json.dumps(mime)});
    const prep = window.require('WAWebPrepRawMedia')
      .prepRawMedia(opaqueData, {{ isPtt: {json.dumps(bool(ptt))} }});
    const mediaData = await prep.waitForPrep();
    const mediaObject = window.require('WAWebMediaStorage')
      .getOrCreateMediaObject(mediaData.filehash);
    if (!mediaData.filehash) return {{ __error: 'send_failed', what: 'media prep produced no filehash' }};
    if (!(mediaData.mediaBlob instanceof OpaqueData)) {{
      mediaData.mediaBlob = await OpaqueData.createFromData(
        mediaData.mediaBlob, mediaData.mediaBlob.type);
    }}
    mediaData.renderableUrl = mediaData.mediaBlob.url();
    mediaObject.consolidate(mediaData.toJSON());
    mediaData.mediaBlob.autorelease();

    // Encrypt + upload to WhatsApp's media servers; the entry carries
    // the urls/keys the message references.
    const mediaType = window.require('WAWebMmsMediaTypes')
      .msgToMediaType({{ type: mediaData.type, isGif: false }});
    const {{ uploadMedia }} = window.require('WAWebMediaMmsV4Upload');
    const uploaded = await uploadMedia({{
      mimetype: mediaData.mimetype, mediaObject, mediaType }});
    const entry = uploaded.mediaEntry;
    if (!entry) return {{ __error: 'send_failed', what: 'media upload returned no entry' }};
    mediaData.set({{
      clientUrl: entry.mmsUrl,
      deprecatedMms3Url: entry.deprecatedMms3Url,
      directPath: entry.directPath,
      mediaKey: entry.mediaKey,
      mediaKeyTimestamp: entry.mediaKeyTimestamp,
      filehash: mediaObject.filehash,
      encFilehash: entry.encFilehash,
      uploadhash: entry.uploadHash,
      size: mediaObject.size,
      streamingSidecar: entry.sidecar,
      firstFrameSidecar: entry.firstFrameSidecar,
    }});

    // Full message construct + both promises — same traps as
    // send_message (a husk never reaches the wire; only the inner
    // promise carries wire success). The media fields spread in over
    // type: 'chat', so mediaData.type (image/video/ptt/…) wins.
    const {{ addAndSendMsgToChat }} = window.require('WAWebSendMsgChatAction');
    const mk = window.require('WAWebMsgKey');
    const key = mk.fromString('true_' + chat.__x_id._serialized + '_' + (await mk.newId()) + '_out');
    const mj = mediaData.toJSON();
    const [, sendPromise] = await addAndSendMsgToChat(chat, {{
      id: key,
      ack: 0,
      from: me,
      to: chat.__x_id,
      local: true,
      self: 'out',
      t: Math.floor(Date.now() / 1000),
      isNewMsg: true,
      type: 'chat',
      ...mj,
      caption: {json.dumps(caption)} ?? undefined,
      body: typeof mj.preview === 'string' ? mj.preview : undefined,
    }});
    const failed = await awaitSend(sendPromise, chat, key);
    if (failed) return failed;
    const sent = chat.msgs.getModelsArray().find(m => m.__x_id?._serialized === key._serialized);
    if (sent) return mapMsg(sent);
    return {{
      id: key._serialized,
      content: {json.dumps(caption or "")},
      published: new Date().toISOString(),
      conversationId: chat.__x_id._serialized,
      isOutgoing: true,
      type: str(mj.type) || 'chat',
      name: chatName(chat),
      author: 'Me',
    }};
    """, timeout_s=150)

    # The sent message's attachment is the blob we just read — no
    # re-download needed; hydrate from local truth.
    if isinstance(entity, dict) and entity.get("id"):
        entity["attaches"] = [{
            "shape": _media_shape(mime, entity.get("type", "")),
            "name": filename,
            "filename": filename,
            "mimeType": mime,
            "size": blob["size"],
            "path": path,
            "sha": blob["sha256"],
        }]
    return entity


# Live hook: self-installing, idempotent, waits for the Store on its
# own (it also runs on future page loads, where nothing is ready yet).
# Emits one shape-native entity per new message; the engine routes
# marker-tagged console lines through the extraction pipeline.
#
# Install-once is enforced BY CONTROL FLOW (an async loop that returns
# after installing), never by clearInterval: WhatsApp swaps the global
# timers for JSScheduler wrappers after boot, so a hook injected pre-boot
# (Page.addScriptToEvaluateOnNewDocument runs before page scripts) mints
# a NATIVE interval id the wrapper's clearInterval can't find in its map
# — a silent no-op that made the installer immortal, stacking one
# Msg.on('add') listener per 500ms tick (~9k listeners in 77 minutes,
# thousands of duplicate writes per inbound message, 2026-07-06).
_WATCH_MARKER = "__agentos_entity__"

_WATCH_HOOK = """
(function () {
  if (window.__agentos_wa_watch__) return;
  window.__agentos_wa_watch__ = true;
  %(get_me)s
  (async () => {
    while (true) {
      try {
        const C = window.require('WAWebCollections');
        const me = __getMe();
        if (C && C.Chat && me) {
          const { Chat, Msg, Contact } = C;
          %(helpers)s
          Msg.on('add', (m) => {
            try {
              if (m.__x_isNewMsg !== true) return;
              const entity = mapMsg(m);
              entity.__shape__ = 'message';
              console.log(%(marker)s + JSON.stringify(entity));
            } catch (e) {}
          });
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
    """Stream new WhatsApp messages into the graph in real time.

    Installs a hook on the live session; each incoming or outgoing
    message lands in the graph as a `message` entity the moment WhatsApp
    receives it — observers (SSE) fire as with any other graph write.
    The subscription is durable: it survives page reloads, session
    drops, browser restarts (the engine reconnects with backoff), and
    engine restarts (boot re-arms it from the graph). Arm once, ever.
    Idempotent: safe to call repeatedly.
    """
    hook = _WATCH_HOOK % {
        "get_me": _GET_ME_JS,
        "helpers": _HELPERS,
        "marker": json.dumps(_WATCH_MARKER),
    }
    await services.call("browser_session", verb="subscribe", params={
        "target": _TARGET,
        "mode": _MODE,
        "js": hook,
        "marker": _WATCH_MARKER,
        "subscriber": "whatsapp",
        "op": "watch",
    })
    return {"watching": True, "stream": "message"}


@returns({"status": "string", "reactedTo": "string", "messageId": "string", "conversationName": "string"})
@provides("message_react", account_param="account")
@timeout(60)
async def send_reaction(*, emoji, chat=None, message_id=None, **params):
    """React to a message — the latest in a chat, or a specific one by id.

    Declared as a brokered capability (`@provides("message_react")`), the same
    way `set_presence` / `message_mark_unread` are — so a provider-agnostic app
    gates its reaction strip on "who provides message_react" (WhatsApp today),
    never on sniffing whether a `send_reaction` tool happens to exist. A
    provider without reactions (iMessage — `imsg react` is fragile UI
    automation) simply doesn't declare it, and no react affordance shows.
    Still reachable verb-routed as `services.chats {verb: send_reaction}`.

    Reports `status: dispatched`, not `sent`: the reaction call is the
    same one whatsapp-web.js uses, but WhatsApp Web gives a headless tab
    no client-side echo to confirm delivery against — a phone is the
    only ground truth.

    Args:
        emoji: Any Unicode emoji (e.g. 🚀).
        chat: Chat JID or name substring — reacts to its latest message.
        message_id: Serialized message id (from any message-returning
            op). The chat rides inside the id; `chat` is not needed.
    """
    if message_id is None and chat is None:
        return app_error(
            "Pass `chat` (react to its latest message) or `message_id` "
            "(react to that specific message).",
            code="BadParams",
        )
    if message_id is not None:
        find_msg = f"""
    // The chat JID rides inside the serialized id — resolve the chat
    // first and search its own collection (authoritative; the global
    // Msg one only sees loaded/synced messages), then fall back to it.
    const key = window.require('WAWebMsgKey').fromString({json.dumps(message_id)});
    const target = Chat.get(key.remote?._serialized) || null;
    const msg = (target?.msgs.getModelsArray()
      .find(m => m.__x_id?._serialized === {json.dumps(message_id)}))
      || Msg.get({json.dumps(message_id)});
    if (!msg) return {{ __error: 'not_found', what: 'message', ref: {json.dumps(message_id)} }};
    """
    else:
        find_msg = f"""
    const target = findChat({json.dumps(chat)});
    if (!target) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(chat)} }};
    // The chat's own collection, not the global Msg one — in a headless
    // tab the global collection only sees loaded/synced messages.
    const msg = target.msgs.getModelsArray()
      .slice()
      .sort((a, b) => (Number.isFinite(b.__x_t) ? b.__x_t : 0) - (Number.isFinite(a.__x_t) ? a.__x_t : 0))[0];
    if (!msg) return {{ __error: 'not_found', what: 'message', ref: 'last message in chat' }};
    """
    return await _eval(find_msg + f"""
    const {{ sendReactionToMsg }} = window.require('WAWebSendReactionMsgAction');
    await sendReactionToMsg(msg, {json.dumps(emoji)});
    const chatModel = typeof target !== 'undefined' && target
      ? target : Chat.get(msg.__x_id?.remote?._serialized);
    return {{
      status: 'dispatched',
      reactedTo: msgBody(msg).substring(0, 80),
      messageId: msg.__x_id?._serialized || '',
      conversationName: chatModel ? chatName(chatModel) : '',
    }};
    """)


@returns({"state": "string", "conversationName": "string"})
@provides("message_typing", account_param="account")
@timeout(60)
async def send_typing(*, chat, kind="typing", **params):
    """Show a live chat-state indicator — "typing…" or "recording audio…".

    Fires the same chat-state WhatsApp's composer fires; the
    counterparty's chat header shows it until WhatsApp's own decay
    clears it or a send lands. Presence is honesty, not theater: fire
    it only when a real send follows.

    Args:
        chat: Chat JID or name substring.
        kind: `typing` (default), `recording`, or `paused` (clears an
            indicator without sending).
    """
    methods = {
        "typing": "sendChatStateComposing",
        "recording": "sendChatStateRecording",
        "paused": "sendChatStatePaused",
    }
    method = methods.get(kind)
    if method is None:
        return app_error(
            f"Unknown chat-state kind {kind!r} — use typing | recording | paused.",
            code="BadParams",
        )
    return await _eval(f"""
    const target = findChat({json.dumps(chat)});
    if (!target) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(chat)} }};
    await window.require('WAWebChatStateBridge').{method}(target.__x_id);
    return {{ state: {json.dumps(kind)}, conversationName: chatName(target) }};
    """)


@returns({"presence": "string"})
@provides("set_presence", account_param="account")
@timeout(60)
async def set_presence(*, state="available", **params):
    """Set the account's online presence — the green "online" dot.

    Declared as a brokered capability (`@provides`), same as the other
    `message_*` actions, so a provider-agnostic app can gate a presence
    control on "who provides set_presence" — WhatsApp today, and any
    future connector that declares it. A provider without a presence
    concept (iMessage) simply doesn't declare it, and no toggle shows.

    Args:
        state: `available` (online) or `unavailable` (offline).
    """
    if state not in ("available", "unavailable"):
        return app_error(
            f"Unknown presence state {state!r} — use available | unavailable.",
            code="BadParams",
        )
    method = "sendPresenceAvailable" if state == "available" else "sendPresenceUnavailable"
    return await _eval(f"""
    await window.require('WAWebPresenceChatAction').{method}();
    return {{ presence: {json.dumps(state)} }};
    """)


# ──────────────────────────────────────────────────────────────────────
# Status / stories → feeds (Social app)
# ──────────────────────────────────────────────────────────────────────


@returns("post[]")
@provides("feeds", account_param="account")
@timeout(90)
async def list_posts(*, limit=200, include_viewed=True, **params):
    """List WhatsApp Status stories as `post` rows (postType: story).

    Brokered as the `feeds` capability — the Social Commons app fans this
    out per connected account, never naming WhatsApp. Each Status *message*
    is one post; ring metadata (`authorId`, `ringTotal`, `ringUnread`) lets
    the stories rail group by author and draw segment counts. About-bio
    text (`Contact.__x_status`) is NOT included — that's a profile string,
    not a story.

    Args:
        limit: Maximum story items to return (newest rings first, then
            newest msgs within a ring).
        include_viewed: When false, skip msgs already marked viewed.
    """
    rows = await _eval(f"""
    const Status = C.Status;
    if (!Status || typeof Status.getModelsArray !== 'function') {{
      return {{ __error: 'binding_drift',
        what: 'WAWebCollections.Status.getModelsArray missing' }};
    }}
    const includeViewed = {json.dumps(bool(include_viewed))};
    const limit = {int(limit)};
    const rings = Status.getModelsArray()
      .slice()
      .sort((a, b) => (b.__x_t || b.t || 0) - (a.__x_t || a.t || 0));
    const out = [];
    for (const status of rings) {{
      if (out.length >= limit) break;
      // Skip fully expired rings (status.t older than TTL).
      const st = status.__x_t ?? status.t;
      if (Number.isFinite(st)) {{
        try {{
          if (typeof status.isExpired === 'function' && status.isExpired()) continue;
        }} catch (e) {{
          if (window.require('WATimeUtils').unixTime() - st > STATUS_TTL_S) continue;
        }}
      }}
      let msgs = [];
      try {{ msgs = status.msgs?.getModelsArray?.() || []; }} catch (e) {{ msgs = []; }}
      const sorted = msgs.slice().sort((a, b) => (a.t || a.__x_t || 0) - (b.t || b.__x_t || 0));
      for (const m of sorted) {{
        if (out.length >= limit) break;
        const viewed = m.viewed === true || m.__x_viewed === true;
        if (!includeViewed && viewed) continue;
        out.push(mapStatusPost(m, status));
      }}
    }}
    await stampMissingFaces(out);
    return await hydrateFaces(out);
    """, timeout_s=75)
    return await _stage_faces(rows)


@returns("post")
@provides("get_post", account_param="account")
@timeout(120)
async def get_post(*, id, **params):
    """Get one Status story by serialized message id — hydrates media.

    Declared as `@provides("get_post")` so the Social app gates story-media
    rendering on capability (same pattern as Messaging's `get_message`),
    never a plugin id. Still reachable verb-routed as
    `services.feeds {verb: get_post}`.

    Args:
        id: Serialized status message id (from `list_posts`).
    """
    entity = await _eval(f"""
    const Status = C.Status;
    const msg = Msg.get({json.dumps(id)});
    if (!msg) return {{ __error: 'not_found', what: 'post', ref: {json.dumps(id)} }};
    // Prefer the Status ring that owns this author; fall back to bare msg.
    let status = null;
    const authorJid = msg.author?._serialized || msg.__x_author?._serialized
      || (msg.id?.fromMe ? (Status.getMyStatus?.()?.id?._serialized) : null);
    if (authorJid && Status) {{
      try {{ status = Status.get(authorJid) || null; }} catch (e) {{}}
      if (!status && typeof Status.find === 'function') {{
        try {{ status = await Status.find(authorJid); }} catch (e) {{}}
      }}
    }}
    if (!status && Status?.getMyStatus && msg.id?.fromMe) {{
      try {{ status = Status.getMyStatus(); }} catch (e) {{}}
    }}
    const out = mapStatusPost(msg, status || {{
      id: {{ _serialized: authorJid || '' }},
      totalCount: 1,
      unreadCount: 0,
    }});
    const mediaTypes = ['image', 'video', 'ptt', 'audio', 'sticker', 'document'];
    const size = Number.isFinite(msg.size) ? msg.size
      : (Number.isFinite(msg.__x_size) ? msg.__x_size : 0);
    if (mediaTypes.includes(out.mediaType) && size > 0 && size <= {_MEDIA_HYDRATION_CAP}) {{
      if (msg.mediaData && msg.mediaData.mediaStage !== 'RESOLVED') {{
        await msg.downloadMedia({{ downloadEvenIfExpensive: true, rmrReason: 1 }});
      }}
      const buf = await window.require('WAWebDownloadManager').downloadManager
        .downloadAndMaybeDecrypt({{
          directPath: msg.directPath || msg.__x_directPath,
          encFilehash: msg.encFilehash || msg.__x_encFilehash,
          filehash: msg.filehash || msg.__x_filehash,
          mediaKey: msg.mediaKey || msg.__x_mediaKey,
          mediaKeyTimestamp: msg.mediaKeyTimestamp || msg.__x_mediaKeyTimestamp,
          type: msg.type || msg.__x_type,
          signal: new AbortController().signal,
          downloadQpl: {{ addAnnotations() {{ return this; }}, addPoint() {{ return this; }} }},
        }});
      const bytes = new Uint8Array(buf);
      let bin = '';
      const CHUNK = 0x8000;
      for (let i = 0; i < bytes.length; i += CHUNK) {{
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
      }}
      const dur = Number(str(msg.duration ?? msg.__x_duration));
      out.__media = {{
        data: btoa(bin),
        mime: str(msg.mimetype || msg.__x_mimetype),
        filename: str(msg.filename || msg.__x_filename),
        size: bytes.length,
        duration: Number.isFinite(dur) && dur > 0 ? dur : null,
      }};
    }}
    await stampMissingFaces([out]);
    return await hydrateFaces(out);
    """, timeout_s=90)

    if isinstance(entity, dict):
        entity = await _stage_faces(entity)
        media = entity.pop("__media", None) if isinstance(entity, dict) else None
        if media and media.get("data"):
            mime = media.get("mime") or "application/octet-stream"
            ext = (media.get("filename") or "").rsplit(".", 1)[-1] if "." in (media.get("filename") or "") \
                else (mime.split("/")[-1].split(";")[0] or "bin")
            blob = await blobs.put(media["data"], ext=ext)
            attachment = {
                "shape": _media_shape(mime, entity.get("mediaType", "")),
                "name": media.get("filename") or f"{entity.get('mediaType', 'media')} {entity.get('published', '')}".strip(),
                "filename": media.get("filename") or None,
                "mimeType": mime,
                "size": media.get("size"),
                "path": blob["path"],
                "sha": blob["sha256"],
            }
            if media.get("duration"):
                attachment["durationMs"] = int(media["duration"] * 1000)
            entity["attaches"] = [attachment]
    return entity
