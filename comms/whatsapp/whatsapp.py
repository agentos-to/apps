"""WhatsApp — live WhatsApp Web via the engine-held browser session.

Every op is one JS payload evaluated in the WhatsApp Web tab of the
engine-owned Brave instance, through the `browser_session` capability
(`capability.call` — the engine matchmakes the provider; this skill
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

from agentos import capability, returns, skill_error, timeout

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
_HELPERS = """
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
const iso = (t) => t ? new Date(t * 1000).toISOString() : null;
const chatName = (c) => c.__x_name || c.__x_formattedTitle || '';
const mapChat = (c) => {
  const jid = c.__x_id?._serialized || '';
  const out = {
    id: jid,
    name: chatName(c),
    published: iso(c.__x_t),
    isGroup: jid.endsWith('@g.us'),
    isArchived: !!c.__x_archive,
    unreadCount: c.__x_unreadCount ?? 0,
  };
  if (!out.isGroup && jid) {
    const acct = account(jid, out.name);
    if (acct) out.participant = [acct];
  }
  return out;
};
const msgBody = (m) => {
  let body = m.__x_body || '';
  if (!body && m.__x_richResponse?.fragments) {
    body = m.__x_richResponse.fragments
      .filter(f => f.type === 'Text').map(f => f.text).join('\\n');
  }
  if (!body && m.__x_caption) body = m.__x_caption;
  return body;
};
const mapMsg = (m) => {
  const chatId = m.__x_id?.remote?._serialized || '';
  const isOutgoing = !!m.__x_id?.fromMe;
  const out = {
    id: m.__x_id?._serialized || '',
    content: msgBody(m),
    published: iso(m.__x_t),
    conversationId: chatId,
    isOutgoing,
    type: m.__x_type || 'chat',
  };
  const c = Chat.get(chatId);
  if (c) out.name = chatName(c);
  if (isOutgoing) {
    out.author = 'Me';
  } else {
    const sender = m.__x_senderObj;
    const senderName = sender?.__x_name || sender?.__x_pushname || m.__x_notifyName || null;
    if (senderName) out.author = senderName;
    const senderJid = (m.__x_author || m.__x_from)?._serialized || null;
    const acct = account(senderJid, senderName);
    if (acct) out.from = acct;
  }
  if (m.__x_star) out.isStarred = true;
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
    value = await capability.call("browser_session", params={
        "target": _TARGET,
        "js": _payload(body, wait_ms=wait_ms),
        "timeout": timeout_s,
    })

    if isinstance(value, dict) and "__error" in value:
        code = value["__error"]
        if code == "auth_required":
            return skill_error(
                "WhatsApp Web is not linked in the engine-owned browser. "
                "Open the WhatsApp Web tab in the AgentOS Brave instance "
                "and scan the QR code with your phone (one-time setup).",
                code="NeedsAuth",
            )
        if code == "not_ready":
            return skill_error(
                "WhatsApp Web's module system never came up — the page may "
                "still be loading, or a WhatsApp update changed the internals.",
                code="NotReady",
            )
        if code == "not_found":
            return skill_error(
                f"No match for {value.get('ref')!r} ({value.get('what', 'item')}).",
                code="NotFound",
            )
        return skill_error(f"WhatsApp payload error: {code}", code="PayloadError")

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


@returns("message")
@timeout(60)
async def get_message(*, id, **params):
    """Get one message by its serialized id.

    Args:
        id: Serialized message id (from `list_messages` results).
    """
    return await _eval(f"""
    const msg = Msg.get({json.dumps(id)});
    if (!msg) return {{ __error: 'not_found', what: 'message', ref: {json.dumps(id)} }};
    return mapMsg(msg);
    """)


@returns("message[]")
@timeout(60)
async def search_messages(*, query, limit=200, **params):
    """Search message text across chats currently loaded in the Web session.

    Searches the in-memory Store (recent history per chat). For deep
    history, call `list_messages` on a conversation first to page more
    messages into memory.

    Args:
        query: Case-insensitive substring to match in message text.
        limit: Maximum messages to return (newest first).
    """
    return await _eval(f"""
    const q = {json.dumps(query)}.toLowerCase();
    return Msg.getModelsArray()
      .filter(m => msgBody(m).toLowerCase().includes(q))
      .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))
      .slice(0, {int(limit)})
      .map(mapMsg);
    """)


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
        const openCmd = window.require('WAWebCmd');
        try {{ await openCmd.openChatAt(chat); }} catch (e) {{}}
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


@returns({"status": "string", "conversationId": "string", "conversationName": "string"})
@timeout(60)
async def send_message(*, to, text, **params):
    """Send a WhatsApp message.

    Args:
        to: Chat JID or contact/group name substring.
        text: Message text to send.
    """
    return await _eval(f"""
    const chat = findChat({json.dumps(to)});
    if (!chat) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(to)} }};
    const {{ addAndSendMsgToChat }} = window.require('WAWebSendMsgChatAction');
    await addAndSendMsgToChat(chat, {{ body: {json.dumps(text)}, type: 'chat' }});
    return {{
      status: 'sent',
      conversationId: chat.__x_id?._serialized || '',
      conversationName: chatName(chat),
    }};
    """)


@returns({"status": "string", "reactedTo": "string", "conversationName": "string"})
@timeout(60)
async def send_reaction(*, chat, emoji, **params):
    """React to the most recent message in a chat with an emoji.

    Args:
        chat: Chat JID or name substring.
        emoji: Any Unicode emoji (e.g. 🚀).
    """
    return await _eval(f"""
    const target = findChat({json.dumps(chat)});
    if (!target) return {{ __error: 'not_found', what: 'conversation', ref: {json.dumps(chat)} }};
    const chatId = target.__x_id?._serialized;
    const lastMsg = Msg.getModelsArray()
      .filter(m => (m.__x_id?.remote?._serialized || '') === chatId)
      .sort((a, b) => (b.__x_t || 0) - (a.__x_t || 0))[0];
    if (!lastMsg) return {{ __error: 'not_found', what: 'message', ref: 'last message in chat' }};
    const {{ sendReactionToMsg }} = window.require('WAWebSendReactionMsgAction');
    await sendReactionToMsg(lastMsg, {json.dumps(emoji)});
    return {{
      status: 'sent',
      reactedTo: msgBody(lastMsg).substring(0, 80),
      conversationName: chatName(target),
    }};
    """)
