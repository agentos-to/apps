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

from agentos import app_error, blobs, returns, services, timeout

_TARGET = "web.whatsapp.com"

# ──────────────────────────────────────────────────────────────────────
# JS building blocks
# ──────────────────────────────────────────────────────────────────────

# Wait for the Store to exist and the user to be logged in. On a fresh
# (or expired) session WhatsApp shows the QR screen and getMeUser()
# stays empty — that's `auth_required`, not a timeout.
_PRELUDE = """
const __deadline = Date.now() + %(wait_ms)d;
let C = null, me = null;
while (Date.now() < __deadline) {
  try {
    C = window.require('WAWebCollections');
    me = window.require('WAWebUserPrefsMeUser').getMeUser();
    if (C && C.Chat && me) break;
  } catch (e) {}
  me = null;
  // Fast-fail: the QR login screen renders a [data-ref] element —
  // no point waiting out the deadline when we're plainly logged out.
  if (C && C.Chat && document.querySelector('[data-ref]')) {
    return { __error: 'auth_required' };
  }
  await new Promise(r => setTimeout(r, 250));
}
if (!C || !C.Chat) return { __error: 'not_ready' };
if (!me) return { __error: 'auth_required' };
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
const account = (jid, displayName) => {
  if (!jid) return null;
  const acct = { id: jid, platform: 'whatsapp', handle: jidPhone(jid) || jid };
  if (displayName) acct.display_name = displayName;
  return acct;
};
const iso = (t) => Number.isFinite(t) ? new Date(t * 1000).toISOString() : null;
const chatName = (c) => str(c.__x_name) || str(c.__x_formattedTitle);
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
  if (!out.isGroup && jid) {
    const acct = account(jid, out.name);
    if (acct) out.participant = [acct];
  }
  return out;
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
    type: str(m.__x_type) || 'chat',
  };
  const c = Chat.get(chatId);
  if (c) out.name = chatName(c);
  if (isOutgoing) {
    out.author = 'Me';
  } else {
    const sender = m.__x_senderObj;
    const senderName = str(sender?.__x_name) || str(sender?.__x_pushname) || str(m.__x_notifyName) || null;
    if (senderName) out.author = senderName;
    const senderJid = m.__x_author?._serialized || m.__x_from?._serialized || null;
    const acct = account(senderJid, senderName);
    if (acct) out.from = acct;
  }
  if (m.__x_star === true) out.isStarred = true;
  return out;
};
const findChat = (ref) => {
  const exact = Chat.getModelsArray().find(c => c.__x_id?._serialized === ref);
  if (exact) return exact;
  const q = ref.toLowerCase();
  return Chat.getModelsArray().find(c => chatName(c).toLowerCase().includes(q)) || null;
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
    value = await services.call(services.browser_session, params={
        "target": _TARGET,
        "js": _payload(body, wait_ms=wait_ms),
        "timeout": timeout_s,
    })

    if isinstance(value, dict) and "__error" in value:
        code = value["__error"]
        if code == "auth_required":
            return app_error(
                "WhatsApp Web is not linked in the engine-owned browser. "
                "Open the WhatsApp Web tab in the AgentOS Brave instance "
                "and scan the QR code with your phone (one-time setup).",
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
                f"WhatsApp did not accept the message: {value.get('what')}",
                code="SendFailed",
            )
        return app_error(f"WhatsApp payload error: {code}", code="PayloadError")

    return value


# ──────────────────────────────────────────────────────────────────────
# Conversations
# ──────────────────────────────────────────────────────────────────────


@returns("conversation[]")
@timeout(60)
async def list_conversations(*, archived=False, limit=200, **params):
    """List WhatsApp conversations from the live Web session.

    Args:
        archived: When true, list archived chats instead of active ones.
        limit: Maximum conversations to return (most recent first).
    """
    return await _eval(f"""
    const chats = Chat.getModelsArray()
      .filter(c => !!c.__x_archive === {json.dumps(bool(archived))})
      .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))
      .slice(0, {int(limit)});
    return chats.map(mapChat);
    """)


@returns("conversation")
@timeout(60)
async def get_conversation(*, id, **params):
    """Get one conversation by JID or fuzzy name.

    Args:
        id: Chat JID (`...@c.us` / `...@g.us`) or a name substring.
    """
    return await _eval(f"""
    const chat = findChat({json.dumps(id)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(id)} }};
    return mapChat(chat);
    """)


@returns("conversation")
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
          try {{ await loader.loadEarlierMsgs(chat); }} catch (e) {{ break; }}
          if (inChat().length === before) break;
        }}
        return inChat()
          .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))
          .slice(0, {limit})
          .map(mapMsg);
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
@timeout(120)
async def get_message(*, id, **params):
    """Get one message by its serialized id.

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
        try {{ await Cmd.openChatAt(chat); }} catch (e) {{}}
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
@timeout(60)
async def send_message(*, to, text, **params):
    """Send a WhatsApp message; returns the sent message entity.

    Args:
        to: Chat JID or contact/group name substring.
        text: Message text to send.
    """
    # A minimal {body, type} dict no longer sends — WhatsApp's model layer
    # builds an empty husk from it, addAndSendMsgToChat resolves anyway,
    # and the inner send promise rejects out of sight. Build the full
    # message the way whatsapp-web.js does, and await BOTH promises:
    # addAndSendMsgToChat resolves to [msg, sendPromise], and only the
    # inner promise carries wire-level success/failure.
    return await _eval(f"""
    const chat = findChat({json.dumps(to)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(to)} }};
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
    }});
    const result = await sendPromise;
    if (result?.messageSendResult !== 'OK') {{
      return {{ __error: 'send_failed', what: JSON.stringify(result ?? null) }};
    }}
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
@timeout(180)
async def send_media(*, to, path, caption=None, ptt=False, **params):
    """Send media — an image, video, document, or voice note.

    The file must already live in the engine's blob store
    (`~/.agentos/blobs/…`): inbound media hydrated by `get_message` is
    there, and any agent stages new bytes with `blobs.put`. The engine
    reads the bytes back (`blobs.get` — apps never open files); the
    payload then runs WhatsApp's own media pipeline — prep, encrypt,
    upload, send — exactly like the UI's attach button. Returns the
    sent message entity with its attachment block, same shape as
    inbound media.

    Args:
        to: Chat JID or contact/group name substring.
        path: Blob-store path (from `blobs.put` or a hydrated
            attachment's `path`).
        caption: Optional text shown under the media.
        ptt: Send audio as a voice note (push-to-talk bubble).
            Requires an ogg/opus file.
    """
    blob = await blobs.get(path)
    if blob.get("size", 0) > _MEDIA_HYDRATION_CAP:
        return app_error(
            f"Blob is {blob.get('size')} bytes — payloads over "
            f"{_MEDIA_HYDRATION_CAP} can't cross the eval channel "
            "(same cap as inbound hydration).",
            code="TooLarge",
        )
    filename = path.rsplit("/", 1)[-1]
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    entity = await _eval(f"""
    const chat = findChat({json.dumps(to)});
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
    const result = await sendPromise;
    if (result?.messageSendResult !== 'OK') {{
      return {{ __error: 'send_failed', what: JSON.stringify(result ?? null) }};
    }}
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
_WATCH_MARKER = "__agentos_entity__"

_WATCH_HOOK = """
(function () {
  if (window.__agentos_wa_watch__) return;
  window.__agentos_wa_watch__ = true;
  const install = setInterval(() => {
    try {
      const C = window.require('WAWebCollections');
      const me = window.require('WAWebUserPrefsMeUser').getMeUser();
      if (!C || !C.Chat || !me) return;
      clearInterval(install);
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
    } catch (e) {}
  }, 500);
})()
"""


@returns({"watching": "boolean", "stream": "string"})
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
        "helpers": _HELPERS,
        "marker": json.dumps(_WATCH_MARKER),
    }
    await services.call(services.browser_session, verb="subscribe", params={
        "target": _TARGET,
        "js": hook,
        "marker": _WATCH_MARKER,
        "subscriber": "whatsapp",
        "op": "watch",
    })
    return {"watching": True, "stream": "message"}


@returns({"status": "string", "reactedTo": "string", "messageId": "string", "conversationName": "string"})
@timeout(60)
async def send_reaction(*, emoji, chat=None, message_id=None, **params):
    """React to a message — the latest in a chat, or a specific one by id.

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
@timeout(60)
async def set_presence(*, state="available", **params):
    """Set the account's online presence — the green "online" dot.

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
