"""iMessage — read, watch, and send iMessages/SMS via macOS Messages.

A Messaging provider (the second network next to WhatsApp): the brokered
verbs `chats`, `message_watch`, `message_send`, `message_send_media`, and
`message_typing` all resolve here for the Mac's signed-in iMessage account.

Reads come straight from ~/Library/Messages/chat.db (read-only SQL; macOS
daemons keep it fresh whether Messages.app is open or not). Live inbound
rides the engine's `file_watch` transport: the engine watches the Messages
directory natively (FSEvents) and dispatches `pull_changes`, which returns
the row delta past its own cursor as shape-native `message` entities.

Sends go through the `imsg` CLI (AppleScript into Messages.app — public
APIs only), and a send is never trusted from the CLI's exit code: the
receipt is the outgoing row landing in chat.db with `is_sent` and no
`error` (macOS 26 group sends regress upstream into silent drops — the
read-back turns those into a typed SendFailed).

IDs are chat.db GUIDs throughout: `chat.guid` for conversations
(`any;-;+15125550000`, `any;+;chat…` for groups), `message.guid` for
messages. Ops that take a conversation also accept a ROWID or a fuzzy
name/handle substring. Timestamps convert from Apple's Core Data epoch
(nanoseconds since 2001-01-01) to ISO datetime.

What iMessage cannot do publicly is honestly absent: no mark-read (the
phone badge persists), no inbound typing/presence, no reactions.
"""

import asyncio
import json
import os
import tempfile

from agentos import account, app_error, blobs, connection, provides, returns, services, shell, sql, test, timeout, canonicalize_datetime
from agentos.macos import contacts


# Contacts-framework face dump: given handles (phones/emails), return
# photo bytes (base64) keyed by the same match keys `contacts.handle_key`
# uses. AddressBook sqlite has no photo table on modern macOS — CNContact
# is the only door. Prefer full `imageData` when present; fall back to the
# thumbnail. Detect PNG vs JPEG from magic bytes — Contacts thumbs are
# often PNG even though callers historically assumed JPEG.
_FACE_SWIFT = r"""
import Contacts
import Foundation

var handles: [String] = []
if let inData = try? FileHandle.standardInput.readToEnd(),
   let obj = (try? JSONSerialization.jsonObject(with: inData)) as? [String: Any],
   let hs = obj["handles"] as? [String] {
    handles = hs
}
func phoneKey(_ s: String) -> String {
    let d = s.filter { $0.isNumber }
    return d.count >= 10 ? String(d.suffix(10)) : d
}
func handleKey(_ s: String) -> String {
    let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
    return t.contains("@") ? t.lowercased() : phoneKey(t)
}
func imageMime(_ data: Data) -> String {
    if data.count >= 8 {
        let png: [UInt8] = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]
        if Array(data.prefix(8)) == png { return "image/png" }
    }
    if data.count >= 3 {
        let jpg: [UInt8] = [0xFF, 0xD8, 0xFF]
        if Array(data.prefix(3)) == jpg { return "image/jpeg" }
    }
    return "image/jpeg"
}
let wanted = Set(handles.map { handleKey($0) }.filter { !$0.isEmpty })
guard !wanted.isEmpty else {
    print("[]")
    exit(0)
}
let store = CNContactStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
store.requestAccess(for: .contacts) { ok, _ in granted = ok; sem.signal() }
sem.wait()
guard granted else {
    FileHandle.standardError.write("PERMISSION_DENIED\n".data(using: .utf8)!)
    exit(2)
}
let keys: [CNKeyDescriptor] = [
    CNContactPhoneNumbersKey as CNKeyDescriptor,
    CNContactEmailAddressesKey as CNKeyDescriptor,
    CNContactThumbnailImageDataKey as CNKeyDescriptor,
    CNContactImageDataKey as CNKeyDescriptor,
]
let req = CNContactFetchRequest(keysToFetch: keys)
var out: [[String: Any]] = []
var seen = Set<String>()
try store.enumerateContacts(with: req) { c, stop in
    let data = (c.imageData?.isEmpty == false ? c.imageData : nil)
        ?? (c.thumbnailImageData?.isEmpty == false ? c.thumbnailImageData : nil)
    guard let data, !data.isEmpty else { return }
    var keysForContact: [String] = []
    for p in c.phoneNumbers {
        let k = phoneKey(p.value.stringValue)
        if wanted.contains(k) { keysForContact.append(k) }
    }
    for e in c.emailAddresses {
        let k = (e.value as String).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if wanted.contains(k) { keysForContact.append(k) }
    }
    guard !keysForContact.isEmpty else { return }
    let mime = imageMime(data)
    let b64 = data.base64EncodedString()
    for k in keysForContact where !seen.contains(k) {
        seen.insert(k)
        out.append(["key": k, "data": b64, "mime": mime])
    }
    if seen.count >= wanted.count { stop.pointee = true }
}
let json = try JSONSerialization.data(withJSONObject: out)
FileHandle.standardOutput.write(json)
"""

# In-process cache: handle_key → {path, mime}. Contacts photos rarely change
# mid-session; a miss just means no face for that handle.
_FACE_CACHE: dict[str, dict] = {}


def _ext_for_mime(mime: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if m == "image/png":
        return "png"
    if m in ("image/jpeg", "image/jpg"):
        return "jpg"
    if m == "image/webp":
        return "webp"
    if m == "image/gif":
        return "gif"
    return "jpg"


async def _contact_faces(handles: list[str]) -> dict[str, dict]:
    """Resolve macOS Contacts photos for the given handles → blob paths.

    Returns `{handle_key: {path, mime}}`. Cached in-process; `blobs.put` is
    content-addressed so re-staging identical bytes is free.
    """
    needed = []
    for h in handles:
        key = contacts.handle_key(h)
        if key and key not in _FACE_CACHE:
            needed.append(h)
    if needed:
        result = await shell.run(
            "swift", args=["-e", _FACE_SWIFT],
            input=json.dumps({"handles": needed}), timeout=60,
        )
        if isinstance(result, dict) and result.get("exit_code") == 0:
            try:
                rows = json.loads((result.get("stdout") or "").strip() or "[]")
            except json.JSONDecodeError:
                rows = []
            for row in rows:
                if not isinstance(row, dict) or not row.get("data") or not row.get("key"):
                    continue
                mime = row.get("mime") or "image/jpeg"
                try:
                    blob = await blobs.put(row["data"], ext=_ext_for_mime(mime))
                except Exception:
                    continue
                _FACE_CACHE[row["key"]] = {"path": blob["path"], "mime": mime}
    out = {}
    for h in handles:
        key = contacts.handle_key(h)
        if key and key in _FACE_CACHE:
            out[key] = _FACE_CACHE[key]
    return out


async def _attach_faces(convs):
    """Stamp `image` + `mimeType` on 1:1 conversations from Contacts photos."""
    single = isinstance(convs, dict)
    items = [convs] if single else (convs if isinstance(convs, list) else None)
    if items is None:
        return convs
    handles = []
    for c in items:
        if not isinstance(c, dict) or c.get("isGroup"):
            continue
        parts = c.get("participant") or []
        if parts and parts[0].get("handle"):
            handles.append(parts[0]["handle"])
    if not handles:
        return convs
    faces = await _contact_faces(handles)
    for c in items:
        if not isinstance(c, dict) or c.get("isGroup"):
            continue
        parts = c.get("participant") or []
        h = parts[0].get("handle") if parts else None
        key = contacts.handle_key(h)
        face = faces.get(key) if key else None
        if face:
            c["image"] = face["path"]
            c["mimeType"] = face["mime"]
            if parts and isinstance(parts[0], dict):
                parts[0]["image"] = face["path"]
    return items[0] if single else convs


connection(
    'db',
    sqlite='~/Library/Messages/chat.db')


DB_PATH = "~/Library/Messages/chat.db"

# What the engine's file_watch transport watches. The WAL lives next to
# chat.db; watching the directory survives checkpoint rotation.
WATCH_TARGET = "~/Library/Messages"

# One pull returns at most this many rows; a bigger backlog drains across
# the trailing re-run / the ~30s heartbeat. Keeps a week-long gap from
# flooding the observer bus in one burst.
PULL_LIMIT = 200


# ==============================================================================
# Shape mapping
# ==============================================================================


def _handle_to_account(handle):
    """Convert a handle string (phone or email) to an account typed ref dict."""
    if not handle:
        return None
    if handle.startswith("+") or handle.replace("+", "").isdigit():
        return {"handle": handle, "platform": "phone"}
    if "@" in handle:
        return {"handle": handle, "platform": "email"}
    return {"handle": handle, "platform": "imessage"}


def _account_email(account_login):
    """chat.account_login → the accountEmail grammar.

    chat.db stores the signed-in identity as `E:joe@example.com` (Apple-ID
    email) or `P:+1512…` (phone). The prefix is storage detail; the bare
    identity is the same field email/WhatsApp stamp as accountEmail — one
    grammar for "which account does this row belong to".
    """
    if not account_login:
        return None
    if account_login[:2] in ("E:", "P:", "e:", "p:"):
        return account_login[2:] or None
    return account_login


async def _own_handles():
    """The signed-in account's own handles — every identity it has been
    signed in as, read from DISTINCT chat.account_login.

    One Apple ID owns several aliases (an email AND one or more phone
    numbers), and chat.db records whichever the account presented as on
    each chat — so the email-only login of any single chat is not the
    whole set. A DM whose sole participant is one of these is a
    note-to-self chat (Joe's own `+1512…` self-thread is addressed by
    phone while most chats sign in as `E:joe@…`).
    """
    rows = await sql.query(
        "SELECT DISTINCT account_login FROM chat "
        "WHERE account_login IS NOT NULL AND account_login != ''",
        db=DB_PATH, params={})
    return {e.lower() for r in rows if (e := _account_email(r.get("account_login")))}


def _join_names(parts):
    """`["A"] → A · ["A","B"] → A & B · ["A","B","C"] → A, B & C` — the
    label Messages itself composes for an unnamed group."""
    if len(parts) <= 1:
        return parts[0] if parts else None
    return ", ".join(parts[:-1]) + " & " + parts[-1]


def _map_conversation(row, names=None):
    """Map a SQL row to the conversation shape.

    `id` is chat.guid — the durable, send-routable id. A conversation is
    never a raw identifier when a human name is derivable. Best first:
      1. the chat's own display_name (a group someone actually named)
      2. participant contact names — full name for a direct chat, first
         names joined Messages-style for an unnamed group ("Peter & Noe";
         most groups have NO display_name, and their chat_identifier is
         an opaque hex string no human recognizes)
      3. the raw participant handles
      4. the chat_identifier, as the last resort only
    """
    result = {
        "id": row["guid"],
        "published": canonicalize_datetime(row.get("updated_at")),
        # chat.style is the canonical flag: 43 = group, 45 = direct.
        "isGroup": row.get("style") == 43,
    }
    email = _account_email(row.get("account_login"))
    if email:
        result["accountEmail"] = email

    # Participants as typed refs
    accounts = []
    handles_str = row.get("participant_handles")
    if handles_str:
        for h in handles_str.split(","):
            h = h.strip()
            acct = _handle_to_account(h)
            if acct:
                accounts.append(acct)
        if accounts:
            result["participant"] = accounts

    name = (row.get("display_name") or "").strip()
    if not name and accounts:
        if result["isGroup"]:
            firsts = []
            for a in accounts:
                nm = contacts.resolve(a["handle"], names) if names else None
                firsts.append(nm.split()[0] if nm else a["handle"])
            name = _join_names(firsts)
        else:
            nm = contacts.resolve(accounts[0]["handle"], names) if names else None
            # A miss means this handle isn't in Contacts — `name` falls back
            # to the raw phone/email, same "~name" case WhatsApp surfaces.
            result["isSavedContact"] = nm is not None
            accounts[0]["isSavedContact"] = nm is not None
            name = nm or accounts[0]["handle"]
    if not name:
        name = (row.get("chat_identifier") or "").strip()
    result["name"] = name or None

    return result


def _decode_attributed_body(attr_hex):
    """Extract plain text from a serialized NSAttributedString (`streamtyped`) blob.

    Since macOS Ventura, Messages stores body text in `message.attributedBody`
    rather than `message.text`. The body is an NSArchiver streamtyped stream;
    the message string sits right after the `NSString` class token, preceded by
    5 structural bytes and a length token (single byte, or 0x81/0x82 prefix for
    longer strings). chat.db is read-only here, so we never write decoded text
    back — we decode on read.
    """
    if not attr_hex:
        return None
    try:
        data = bytes.fromhex(attr_hex)
    except (ValueError, TypeError):
        return None
    marker = data.find(b"NSString")
    if marker == -1:
        return None
    p = marker + len(b"NSString") + 5  # skip the 5 structural bytes (\x01\x94\x84\x01+)
    if p >= len(data):
        return None
    tok = data[p]
    if tok == 0x81:        # 2-byte little-endian length
        length = int.from_bytes(data[p + 1:p + 3], "little"); start = p + 3
    elif tok == 0x82:      # 4-byte little-endian length (very long messages)
        length = int.from_bytes(data[p + 1:p + 5], "little"); start = p + 5
    else:                  # single-byte length
        length = tok; start = p + 1
    text = data[start:start + length]
    decoded = text.decode("utf-8", errors="replace").strip("\x00").strip()
    return decoded or None


async def _resolve_conversation_rowid(id):
    """Accept a chat GUID, an integer ROWID, or a fuzzy display-name substring.

    Returns the internal ROWID every query keys on. A GUID (contains ';')
    resolves exactly; an integer/all-digit string is a ROWID; anything else
    resolves the way list_conversations names chats: join chat → handle, and
    fuzzy/case-insensitive substring-match the needle against the chat's
    display name, the resolved contact name, and the raw handle itself
    (phone digits or email). First match wins. Pass a `+`-prefixed number
    (or any non-digit) to force handle matching over ROWID.
    """
    if isinstance(id, str) and ";" in id:
        rows = await sql.query(
            "SELECT ROWID as id FROM chat WHERE guid = :guid",
            db=DB_PATH, params={"guid": id})
        if not rows:
            raise ValueError(f"No conversation with guid {id!r}")
        return rows[0]["id"]
    if isinstance(id, int) or (isinstance(id, str) and id.isdigit()):
        return int(id)
    needle = str(id).strip().lower()
    needle_digits = contacts.phone_key(needle)
    # Direct chats first (style 45 before 43): a handle needle must hit
    # the 1:1 thread, never a group that happens to contain the person.
    rows = await sql.query("""
        SELECT
          c.ROWID as id,
          c.style as style,
          COALESCE(NULLIF(c.display_name, ''), NULLIF(c.chat_identifier, '')) as name,
          (SELECT GROUP_CONCAT(h.id, ',')
           FROM handle h
           JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
           WHERE chj.chat_id = c.ROWID) as participant_handles
        FROM chat c
        WHERE EXISTS (
          SELECT 1 FROM message m
          JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
          WHERE cmj.chat_id = c.ROWID
        )
        ORDER BY c.style DESC
    """, db=DB_PATH, params={})
    names = await contacts.load()
    # Exact handle identity beats any substring: two passes.
    for r in rows:
        for h in (r.get("participant_handles") or "").split(","):
            h = h.strip()
            if not h:
                continue
            if needle == h.lower():
                return r["id"]
            if needle_digits and needle_digits == contacts.phone_key(h):
                return r["id"]
    for r in rows:
        name = r.get("name") or ""
        if needle in name.lower():
            return r["id"]
        for h in (r.get("participant_handles") or "").split(","):
            h = h.strip()
            if not h:
                continue
            if needle in h.lower():
                return r["id"]
            if needle_digits and needle_digits in contacts.phone_key(h):
                return r["id"]
            resolved = contacts.resolve(h, names)
            if resolved and needle in resolved.lower():
                return r["id"]
    raise ValueError(f"No conversation matching {id!r}")


_UTI_MIME = {
    "com.apple.coreaudio-format": "audio/x-caf",
    "public.mpeg-4-audio": "audio/mp4",
    "com.apple.protected-mpeg-4-audio": "audio/mp4",
    "org.xiph.ogg-vorbis": "audio/ogg",
    "public.ogg-vorbis-audio": "audio/ogg",
    "public.mp3": "audio/mpeg",
    "com.microsoft.waveform-audio": "audio/wav",
}

_EXT_MIME = {
    ".caf": "audio/x-caf",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg; codecs=opus",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}


def _resolve_mime(uti, filename, mime_type):
    if mime_type:
        return mime_type
    if uti and uti in _UTI_MIME:
        return _UTI_MIME[uti]
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        return _EXT_MIME.get(ext)
    return None


def _expand_path(path):
    if not path:
        return None
    return os.path.expanduser(path)


# Media larger than this stays a kind-chip (url-less): the bytes cross the
# dispatch channel as base64, so cap it the way WhatsApp/Instagram cap theirs.
_MEDIA_HYDRATION_CAP = 25 * 1024 * 1024


def _media_type(mime, uti=None):
    """The Messaging render kind (image / video / ptt / audio) from an
    attachment's mime — what `isRenderKind` gates inline rendering on. iMessage's
    recorded voice messages are CoreAudio (`.caf`, UTI `com.apple.coreaudio-format`)
    → `ptt` (the voice-note player); any other audio → `audio`. A non-media
    attachment (pdf, vcf) returns None and stays a chip."""
    if not mime:
        return None
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "ptt" if uti == "com.apple.coreaudio-format" or mime in ("audio/x-caf", "audio/amr") else "audio"
    return None


def _media_shape(mime):
    """The blob attachment's file shape — widest is `file`."""
    if not mime:
        return "file"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "sound"
    return "file"


# iMessage voice messages are CoreAudio (`.caf`), which browsers can't play via
# <audio>. Transcode them to AAC-in-M4A with `afconvert` (a macOS built-in) on
# hydrate so the voice-note player actually plays them. Keyed by extension.
_TRANSCODE_TO_M4A = {".caf", ".amr"}


async def _stage_attachment(filename):
    """Copy an on-disk iMessage attachment into the content-addressed blob store
    so the shell's `/file` + `/thumb` can serve it (they serve only the blob
    store or a mounted volume — never a raw `~/Library/Messages/Attachments`
    path). Bytes are read via `base64` through `shell.run` (the sanctioned I/O
    path — no raw file reads) and handed to `blobs.put`, the same staging
    WhatsApp/Instagram do for their downloaded bytes. A `.caf`/`.amr` voice
    message is transcoded to browser-playable AAC/M4A first (`afconvert`).
    Returns `{path, sha, size, mime}` (mime set only when transcode changed it)
    or None (missing / empty / over the cap)."""
    fp = _expand_path(filename)
    if not fp or not os.path.isfile(fp):
        return None
    try:
        size = os.path.getsize(fp)
    except OSError:
        return None
    if size <= 0 or size > _MEDIA_HYDRATION_CAP:
        return None
    src, ext, mime = fp, os.path.splitext(fp)[1].lstrip(".").lower() or "bin", None
    if os.path.splitext(fp)[1].lower() in _TRANSCODE_TO_M4A:
        out = os.path.join(tempfile.gettempdir(), f"agentos-imsg-{os.path.basename(fp)}.m4a")
        conv = await shell.run("afconvert", args=["-f", "mp4f", "-d", "aac", fp, out], timeout=60)
        if isinstance(conv, dict) and conv.get("exit_code") == 0 and os.path.isfile(out):
            src, ext, mime = out, "m4a", "audio/mp4"
    res = await shell.run("base64", args=["-i", src], timeout=30)
    if isinstance(res, dict) and res.get("exit_code") == 0:
        b64 = "".join((res.get("stdout") or "").split())  # BSD base64 may wrap lines
    else:
        b64 = None
    if src != fp:
        try:
            os.remove(src)  # drop the transcode temp; the blob store owns the bytes now
        except OSError:
            pass
    if not b64:
        return None
    blob = await blobs.put(b64, ext=ext)
    return {"path": blob["path"], "sha": blob["sha256"], "size": blob.get("size", size), "mime": mime}


# The SELECT fragment every message mapping expects — always joined to its
# chat so guid / style / account_login travel with the row.
_MSG_COLS = """
          m.guid as guid,
          c.guid as chat_guid,
          c.style as style,
          c.account_login as account_login,
          m.text as content,
          hex(m.attributedBody) as attr_hex,
          m.is_from_me as is_outgoing,
          CASE m.is_from_me
            WHEN 1 THEN NULL
            ELSE h.id
          END as sender_handle,
          m.thread_originator_guid as reply_to_guid,
          datetime(m.date / 1000000000 + 978307200, 'unixepoch') as timestamp
"""


def _map_message(row):
    """Map a SQL row to the message shape — guid ids, chat-guid conversation."""
    content = row.get("content")
    if not content:
        content = _decode_attributed_body(row.get("attr_hex"))
    result = {
        "id": row["guid"],
        "name": row.get("conversation_name"),
        "content": content,
        "published": canonicalize_datetime(row.get("timestamp")),
        "conversationId": row.get("chat_guid"),
        "isOutgoing": bool(row.get("is_outgoing")),
        "isGroup": row.get("style") == 43,
    }
    email = _account_email(row.get("account_login"))
    if email:
        result["accountEmail"] = email

    # Sender as typed ref (only for incoming messages)
    sender = row.get("sender_handle")
    if sender and not row.get("is_outgoing"):
        result["author"] = sender
        acct = _handle_to_account(sender)
        if acct:
            result["from"] = acct

    # Attachment metadata (audio messages have no text body)
    att_filename = row.get("att_filename")
    att_bytes = row.get("att_bytes")
    att_uti = row.get("att_uti")
    att_mime = row.get("att_mime")
    if att_filename or att_bytes:
        result["mediaPath"] = _expand_path(att_filename)
        result["sizeBytes"] = att_bytes or 0
        mime = _resolve_mime(att_uti, att_filename, att_mime)
        result["mime"] = mime
        # The render kind so the thread shows a media bubble (which triggers the
        # lazy get_message hydration on scroll), not a text-less blank.
        mtype = _media_type(mime, att_uti)
        if mtype:
            result["type"] = mtype

    return result


def _name_messages(rows, names):
    """Map message rows + graft the sender's contact display name when known."""
    msgs = []
    for r in rows:
        m = _map_message(r)
        if m.get("author"):
            nm = contacts.resolve(m["author"], names)
            if nm:
                m["name"] = nm
        msgs.append(m)
    return msgs


async def _attach_replies(rows, mapped, names=None):
    """Batched reply-quote resolution — `thread_originator_guid` on a row
    names the parent message's GUID; one lookup query resolves every quoted
    message in the batch at once (the same batched-post-pass shape
    WhatsApp's `attachReactions` uses, for the same reason: resolving one
    row at a time inside a sync mapper isn't an option here either)."""
    pairs = [(r["guid"], r["reply_to_guid"]) for r in rows if r.get("reply_to_guid")]
    if not pairs:
        return mapped
    parent_guids = sorted({g for _, g in pairs})
    placeholders = ",".join(f":g{i}" for i in range(len(parent_guids)))
    parent_rows = await sql.query(f"""
        SELECT m.guid as guid, m.text as content, hex(m.attributedBody) as attr_hex,
               m.is_from_me as is_outgoing, h.id as sender_handle
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.guid IN ({placeholders})
    """, db=DB_PATH, params={f"g{i}": g for i, g in enumerate(parent_guids)})
    by_guid = {}
    for pr in parent_rows:
        text = pr.get("content") or _decode_attributed_body(pr.get("attr_hex"))
        is_outgoing = bool(pr.get("is_outgoing"))
        author = None
        if not is_outgoing and pr.get("sender_handle"):
            author = contacts.resolve(pr["sender_handle"], names) if names else None
            author = author or pr["sender_handle"]
        by_guid[pr["guid"]] = {
            "id": pr["guid"],
            "isOutgoing": is_outgoing,
            **({"author": author} if author else {}),
            **({"snippet": text[:120]} if text else {}),
        }
    by_id = {m["id"]: m for m in mapped}
    for child_guid, parent_guid in pairs:
        parent = by_guid.get(parent_guid)
        child = by_id.get(child_guid)
        if parent and child:
            child["replyTo"] = parent
    return mapped


# ==============================================================================
# The account — check / login (the Mac's own Messages sign-in)
# ==============================================================================


@account.check
@returns("account")
@timeout(20)
async def check_session(**params):
    """Identify the signed-in iMessage account from chat.db.

    The "session" is the Mac's own Messages sign-in — there is no cookie
    and no link flow. chat.account_login carries the identity every chat
    rides on (`E:` Apple-ID email / `P:` phone); its presence IS the
    signed-in state. A missing Full Disk Access grant surfaces from the
    SQL open as the engine's NeedsCapability error, not from here.
    """
    rows = await sql.query("""
        SELECT account_login, COUNT(*) as chats
        FROM chat
        WHERE account_login IS NOT NULL AND account_login != ''
        GROUP BY account_login
        ORDER BY chats DESC
    """, db=DB_PATH, params={})
    logins = [r["account_login"] for r in rows if r.get("account_login")]
    if not logins:
        return {"authenticated": False}
    identifier = next(
        (_account_email(l) for l in logins if l.upper().startswith("E:")),
        _account_email(logins[0]),
    )
    acct = {
        "authenticated": True,
        "at": {"shape": "product", "name": "iMessage",
               "url": "https://support.apple.com/messages"},
        "platform": "imessage",
        "identifier": identifier,
        "handle": identifier,
    }
    handles = [_account_email(l) for l in logins if _account_email(l)]
    phone = next((h for h in handles if h.startswith("+")), None)
    if phone:
        acct["phone"] = phone
    return acct


@account.login
@returns("account")
@timeout(20)
async def login(**params):
    """Report the account, or say what signing in actually takes.

    iMessage cannot be linked from here — signing into Messages on this
    Mac (Messages ▸ Settings ▸ iMessage) IS the login, done once by the
    human. This op exists so the account resolver has a truthful answer,
    never a fake challenge.
    """
    session = await check_session()
    if isinstance(session, dict) and session.get("authenticated"):
        return session
    return app_error(
        "No iMessage account in chat.db — sign into Messages on this Mac "
        "(Messages ▸ Settings ▸ iMessage) with your Apple ID, then retry.",
        code="NeedsAuth",
    )


@account.logout
@returns({"ok": "boolean", "message": "string"})
@timeout(10)
async def logout(**params):
    """Signing out of iMessage is the human's move, not ours — say so.

    The session is the Mac's own Apple-ID sign-in; there is no public
    surface to revoke it programmatically, and pretending otherwise
    (wiping a cache, hiding the account) would be a fake logout. The
    honest op points at the one real control.
    """
    return app_error(
        "iMessage sign-out can't be driven programmatically — the session "
        "is the Mac's own Apple-ID sign-in. Sign out in Messages ▸ "
        "Settings ▸ iMessage (or System Settings ▸ Apple ID) yourself.",
        code="NotSupported",
    )


# ==============================================================================
# Conversation operations
# ==============================================================================


# Conversation SELECT shared by list/get — guid ids, style flag, account.
# display_name and chat_identifier stay separate columns: the mapper must
# know whether a name is real before composing one from participants.
_CONV_SELECT = """
        SELECT
          c.ROWID as rowid,
          c.guid as guid,
          c.style as style,
          c.account_login as account_login,
          NULLIF(c.display_name, '') as display_name,
          NULLIF(c.chat_identifier, '') as chat_identifier,
          c.service_name as platform,
          datetime(
            (SELECT MAX(m.date) FROM message m
             JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
             WHERE cmj.chat_id = c.ROWID) / 1000000000 + 978307200,
            'unixepoch'
          ) as updated_at,
          (SELECT GROUP_CONCAT(h.id, ',')
           FROM handle h
           JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
           WHERE chj.chat_id = c.ROWID) as participant_handles
        FROM chat c
"""


@test(params={"limit": 3})
@returns("conversation[]")
@provides("chats", account_param="account")
@timeout(90)
async def list_conversations(*, limit=200, **params):
    """List all iMessage/SMS conversations, most recent first.

    1:1 rows carry `image` when macOS Contacts has a thumbnail for the
    other party's handle — staged into the blob store with `mimeType`
    `image/jpeg` so Messaging renders the face via `/thumb`.
    """
    rows = await sql.query(_CONV_SELECT + """
        WHERE EXISTS (
          SELECT 1 FROM message m
          JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
          WHERE cmj.chat_id = c.ROWID
        )
        ORDER BY updated_at DESC
        LIMIT :limit
    """, db=DB_PATH, params={"limit": limit})
    names = await contacts.load()
    return await _attach_faces([_map_conversation(r, names) for r in rows])


@returns("person[]")
async def list_persons(*, limit=200, **params):
    """List the people a new chat can be started with.

    Derived from your direct (non-group) conversations — the send-routable set
    the New-Chat picker needs. Each person's `id` is their handle (E.164 number
    or Apple-ID email), which `send_message`'s `to` accepts for both existing
    and brand-new threads. The Messaging picker reads this via the brokered
    `list_persons` verb on `chats`; without it iMessage showed zero matches.
    """
    rows = await sql.query(_CONV_SELECT + """
        WHERE c.style = 45 AND EXISTS (
          SELECT 1 FROM message m
          JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
          WHERE cmj.chat_id = c.ROWID
        )
        ORDER BY updated_at DESC
        LIMIT :limit
    """, db=DB_PATH, params={"limit": limit})
    names = await contacts.load()
    out, seen = [], set()
    for r in rows:
        handles = (r.get("participant_handles") or "").split(",")
        h = handles[0].strip() if handles and handles[0] else ""
        if not h or h in seen:
            continue
        seen.add(h)
        out.append({"id": h, "name": contacts.resolve(h, names) or h})
    return out


@returns("conversation")
@timeout(60)
async def get_conversation(*, id, **params):
    """Get a specific conversation by GUID, ROWID, or fuzzy name."""
    rowid = await _resolve_conversation_rowid(id)
    rows = await sql.query(_CONV_SELECT + """
        WHERE c.ROWID = :id
    """, db=DB_PATH, params={"id": rowid})
    if not rows:
        return None
    names = await contacts.load()
    return await _attach_faces(_map_conversation(rows[0], names))


# ==============================================================================
# Message operations
# ==============================================================================


def _date_filter(since, until):
    """Build a (sql_fragment, params) pair for since/until on message.date.

    since/until are ISO date/datetime strings; they compare against the computed
    ISO `timestamp` (SQLite orders ISO strings lexically, so >=/<= suffice).
    """
    frag, params = "", {}
    if since:
        frag += " AND datetime(m.date / 1000000000 + 978307200, 'unixepoch') >= :since"
        params["since"] = since
    if until:
        # A bare date as the upper bound means "through the end of that day".
        # Timestamps carry a time, so lexically '2026-01-11 12:35' > '2026-01-11'
        # — a date-only `until` would otherwise drop the whole day it names.
        if len(until) == 10:
            until = until + " 23:59:59"
        frag += " AND datetime(m.date / 1000000000 + 978307200, 'unixepoch') <= :until"
        params["until"] = until
    return frag, params


@returns("message[]")
async def list_messages(*, conversation_id, limit=200,
                        since=None, until=None, order="desc", **params):
    """List messages in a conversation, by GUID, ROWID, or fuzzy name.

    since / until: ISO date/datetime bounds (inclusive), e.g. "2024-01-01".
    order:         "desc" (newest first, default) or "asc" (oldest first).
    """
    rowid = await _resolve_conversation_rowid(conversation_id)
    date_filter, date_params = _date_filter(since, until)
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    rows = await sql.query(f"""
        SELECT
          {_MSG_COLS},
          att.filename as att_filename,
          att.total_bytes as att_bytes,
          att.uti as att_uti,
          att.mime_type as att_mime
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        LEFT JOIN (
          SELECT maj.message_id, a.filename, a.total_bytes, a.uti, a.mime_type
          FROM message_attachment_join maj
          JOIN attachment a ON a.ROWID = maj.attachment_id
          GROUP BY maj.message_id
        ) att ON att.message_id = m.ROWID
        WHERE cmj.chat_id = :conversation_id
          AND (
            m.text IS NOT NULL AND m.text != ''
            OR m.attributedBody IS NOT NULL
            OR att.total_bytes IS NOT NULL
          )
          {date_filter}
        ORDER BY m.date {direction}
        LIMIT :limit
    """, db=DB_PATH, params={
        "conversation_id": rowid,
        "limit": limit,
        **date_params,
    })
    names = await contacts.load()
    mapped = _name_messages(rows, names)
    return await _attach_replies(rows, mapped, names)


@returns("message")
@provides("get_message", account_param="account")
@timeout(90)
async def get_message(*, id, **params):
    """Get a message by GUID or ROWID, hydrating its media into the blob store.

    Declared as a brokered capability (`@provides("get_message")`) exactly like
    WhatsApp/Instagram, so the Messaging app renders iMessage images + voice
    notes inline instead of a kind-chip — provider-uniform, no plugin id. Each
    of the message's attachments (a message can carry several) is a real file
    under `~/Library/Messages/Attachments/…`; the engine's `/file` endpoint
    serves only the blob store or a mounted volume, so we stage the bytes into
    the content-addressed blob store (`_stage_attachment` → `blobs.put`) and
    return `attaches[].path`. Items over the cap stay url-less (a chip).
    """
    key = "m.guid = :id" if isinstance(id, str) and not str(id).isdigit() else "m.ROWID = :id"
    rows = await sql.query(f"""
        SELECT
          {_MSG_COLS},
          COALESCE(NULLIF(c.display_name, ''), NULLIF(c.chat_identifier, '')) as conversation_name
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        LEFT JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE {key}
    """, db=DB_PATH, params={"id": id})
    if not rows:
        return None
    entity = _map_message(rows[0])
    names = await contacts.load()
    mapped = await _attach_replies(rows, [entity], names)
    entity = mapped[0]

    # Every attachment on this message (chat.db allows more than one), in order.
    atts = await sql.query(f"""
        SELECT a.filename, a.mime_type, a.uti, a.total_bytes
        FROM message m
        JOIN message_attachment_join maj ON m.ROWID = maj.message_id
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE {key}
        ORDER BY maj.ROWID
    """, db=DB_PATH, params={"id": id})
    attaches, first_type = [], None
    for a in atts:
        mime = _resolve_mime(a.get("uti"), a.get("filename"), a.get("mime_type"))
        staged = await _stage_attachment(a.get("filename"))
        if not staged:
            continue
        attaches.append({
            # A transcoded voice note serves as its new type (audio/mp4); the
            # render kind below stays 'ptt' off the ORIGINAL mime.
            "shape": _media_shape(staged.get("mime") or mime),
            "name": os.path.basename(_expand_path(a.get("filename")) or "") or "attachment",
            "mimeType": staged.get("mime") or mime or "application/octet-stream",
            "size": staged["size"],
            "path": staged["path"],
            "sha": staged["sha"],
        })
        if not first_type:
            first_type = _media_type(mime, a.get("uti"))
    if attaches:
        entity["attaches"] = attaches
        if first_type:
            entity["type"] = first_type
    return entity


@returns("message[]")
async def search_messages(*, query, limit=200, **params):
    """Search messages by text content."""
    rows = await sql.query(f"""
        SELECT
          {_MSG_COLS},
          COALESCE(NULLIF(c.display_name, ''), NULLIF(c.chat_identifier, '')) as conversation_name
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        LEFT JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE m.text LIKE '%' || :query || '%'
        ORDER BY m.date DESC
        LIMIT :limit
    """, db=DB_PATH, params={
        "query": query,
        "limit": limit,
    })
    names = await contacts.load()
    return _name_messages(rows, names)


# ==============================================================================
# Live inbound — the file_watch subscription
# ==============================================================================


@returns({"watching": "boolean", "stream": "string"})
@provides("message_watch", account_param="account")
@timeout(30)
async def watch(**params):
    """Stream new iMessages into the graph in real time.

    Arms the engine's `file_watch` transport on the Messages directory:
    macOS daemons write every inbound (and outbound, any device) message
    into chat.db whether Messages.app is open or not; the WAL write fires
    a native FSEvent; the engine dispatches `pull_changes`; the delta
    lands as `message` entities — graph write, then a transport-neutral
    `subscription.entity` observer event, same as WhatsApp's hook.

    Durable and idempotent: the intent persists on the graph and boot
    re-arms it. Arm once, ever. The cursor initializes to the current
    high-water mark — history is the read path, never a replay.
    """
    data = params.get("data") or {}
    result = {"watching": True, "stream": "message"}
    envelope = {"__result__": result}
    if not data.get("watchCursor"):
        rows = await sql.query(
            "SELECT MAX(ROWID) as top FROM message", db=DB_PATH, params={})
        envelope["__data__"] = {"watchCursor": rows[0]["top"] or 0}
    await services.call("file_watch", verb="subscribe", params={
        "target": WATCH_TARGET,
        "subscriber": "imessage",
        "op": "watch",
        "pull": "pull_changes",
        "shape": "message",
    })
    return envelope


@returns("message[]")
async def pull_changes(**params):
    """The watch delta: every message row past the cursor, shape-mapped.

    Dispatched by the engine's file_watch transport (never by agents —
    arm `watch` instead). Owns its cursor in app data, so transport
    restarts, re-arms, and heartbeat pulls can never replay. Skips
    tapback rows and system events (group renames, call rows) — those
    are row *updates* and non-messages the live lane must not surface.
    A backlog bigger than one pull drains across the transport's
    trailing re-run and heartbeat.
    """
    data = params.get("data") or {}
    cursor = data.get("watchCursor")
    if not cursor:
        # Never armed — stamp the high-water mark, emit nothing.
        rows = await sql.query(
            "SELECT MAX(ROWID) as top FROM message", db=DB_PATH, params={})
        return {"__data__": {"watchCursor": rows[0]["top"] or 0}, "__result__": []}

    rows = await sql.query(f"""
        SELECT
          m.ROWID as rowid,
          cmj.chat_id as chat_rowid,
          {_MSG_COLS},
          att.filename as att_filename,
          att.total_bytes as att_bytes,
          att.uti as att_uti,
          att.mime_type as att_mime
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        LEFT JOIN (
          SELECT maj.message_id, a.filename, a.total_bytes, a.uti, a.mime_type
          FROM message_attachment_join maj
          JOIN attachment a ON a.ROWID = maj.attachment_id
          GROUP BY maj.message_id
        ) att ON att.message_id = m.ROWID
        WHERE m.ROWID > :cursor
          AND m.item_type = 0
          AND IFNULL(m.associated_message_type, 0) = 0
          AND (
            m.text IS NOT NULL AND m.text != ''
            OR m.attributedBody IS NOT NULL
            OR att.total_bytes IS NOT NULL
          )
        ORDER BY m.ROWID ASC
        LIMIT :limit
    """, db=DB_PATH, params={"cursor": cursor, "limit": PULL_LIMIT})

    if not rows:
        return []
    names = await contacts.load()

    # A live-watch entity is self-describing (the WhatsApp hook's
    # contract): `name` is the CONVERSATION's display name — the
    # Messaging app synthesizes a chat-list row from it for a
    # first-ever message — and `author` is the readable sender.
    # A pull's delta spans only a few chats; name each once.
    conv_names = {}
    for cid in sorted({r["chat_rowid"] for r in rows}):
        crows = await sql.query(
            _CONV_SELECT + " WHERE c.ROWID = :id", db=DB_PATH, params={"id": cid})
        if crows:
            conv_names[cid] = _map_conversation(crows[0], names).get("name")

    entities = []
    for r in rows:
        m = _map_message(r)
        if m.get("author"):
            m["author"] = contacts.resolve(m["author"], names) or m["author"]
        cname = conv_names.get(r["chat_rowid"])
        if cname:
            m["name"] = cname
        entities.append(m)
    new_cursor = max(r["rowid"] for r in rows)
    return {"__data__": {"watchCursor": new_cursor}, "__result__": entities}


# ==============================================================================
# Sends — imsg dispatches, chat.db is the receipt
# ==============================================================================


def _sendable_handle(to):
    """A raw handle a NEW thread may be minted for — E.164 or email.

    Mirrors WhatsApp's rule: an exact address can create a conversation,
    a fuzzy name never does.
    """
    s = str(to).strip()
    if s.startswith("+") and s[1:].replace(" ", "").isdigit():
        return s
    if "@" in s and ";" not in s:
        return s
    return None


async def _resolve_send_route(to):
    """Resolve `to` into how `imsg` must address it: (rowid, handle, self_chat).

    A direct chat sends by participant handle (`--to`) — macOS 26 stores
    service-agnostic `any;…` chat guids that AppleScript's `chat id`
    rejects, so the person is the only stable address. A group has no
    handle; only `--chat-id` can reach it (rowid, None). A recipient with
    no existing chat routes as (None, handle); an unresolvable non-handle
    is (None, None).

    `self_chat` is the note-to-self case — a DM whose only participant is
    the account's own handle. It has no external recipient, so the send
    never earns an `is_sent=1` server handoff; the receipt read-back keys
    off that flag (see `_await_receipt`).
    """
    try:
        rowid = await _resolve_conversation_rowid(to)
    except ValueError:
        return None, _sendable_handle(to), False
    rows = await sql.query("""
        SELECT
          c.style as style,
          (SELECT GROUP_CONCAT(h.id, ',')
           FROM handle h
           JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
           WHERE chj.chat_id = c.ROWID) as participant_handles
        FROM chat c
        WHERE c.ROWID = :id
    """, db=DB_PATH, params={"id": rowid})
    row = rows[0] if rows else {}
    handles = [h.strip() for h in (row.get("participant_handles") or "").split(",") if h.strip()]
    if row.get("style") == 45 and len(handles) == 1:
        self_chat = handles[0].lower() in await _own_handles()
        return rowid, handles[0], self_chat
    return rowid, None, False


async def _await_receipt(*, rowid, pre_top, text=None, want_attachment=False, self_chat=False):
    """Poll chat.db for the outgoing row a send must land — the receipt.

    `imsg` exiting 0 proves AppleScript ran, nothing more (macOS 26 group
    sends regress into silent drops upstream). The truth is the new
    `is_from_me` row in this chat: `error != 0` is a typed failure,
    `is_sent = 1` is the server handoff. Returns the mapped entity.

    `self_chat` — a note-to-self send has no external recipient, so Apple
    never flips `is_sent` on the row (there is no server handoff to
    another party to acknowledge). The message IS delivered the instant
    the outgoing row lands with `error = 0` — that row appearing in your
    own thread is the whole of "delivered." Requiring `is_sent=1` there
    reported a delivered message as a SendFailed timeout (the bubble
    honestly showed "failed" for mail that arrived). For a self-chat the
    honest receipt is therefore `error = 0`, is_sent not required.

    Content matching happens on the MAPPED entity, never on `m.text` in
    SQL — modern macOS stores an outgoing body only in `attributedBody`,
    so the text column is NULL right when the receipt matters most.
    """
    deadline = asyncio.get_event_loop().time() + 8.0
    row = None
    att_join = """
        JOIN message_attachment_join maj ON m.ROWID = maj.message_id
    """ if want_attachment else ""
    while asyncio.get_event_loop().time() < deadline:
        rows = await sql.query(f"""
            SELECT
              m.ROWID as rowid,
              m.error as send_error,
              m.is_sent as is_sent,
              {_MSG_COLS}
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            {att_join}
            WHERE m.ROWID > :pre_top
              AND m.is_from_me = 1
              AND cmj.chat_id = :chat
            ORDER BY m.ROWID DESC
            LIMIT 3
        """, db=DB_PATH, params={"pre_top": pre_top, "chat": rowid})
        row = None
        for r in rows:
            if text is not None:
                mapped = _map_message(r)
                if (mapped.get("content") or "") != text:
                    continue
            row = r
            break
        if row:
            if row["send_error"]:
                return app_error(
                    f"Messages reported send error {row['send_error']} for the "
                    f"outgoing row (guid {row['guid']}). The message did not go "
                    "out — check the recipient handle and the account's service.",
                    code="SendFailed",
                )
            # A real recipient earns is_sent=1; a self-chat never will, so
            # the landed error-free row is itself the delivery proof.
            if row["is_sent"] or self_chat:
                return _map_message(row)
        await asyncio.sleep(0.3)
    if row:
        return app_error(
            "The outgoing row landed in chat.db but never reached is_sent=1 "
            f"within 8s (guid {row['guid']}, error=0). Messages may be offline "
            "or the handoff is stuck — check Messages.app.",
            code="SendFailed",
        )
    return app_error(
        "imsg exited 0 but no outgoing row landed in chat.db within 8s — the "
        "send was silently dropped (the macOS 26 AppleScript group-send "
        "regression does exactly this). Send from a phone, or retry to a "
        "direct handle.",
        code="SendFailed",
    )


@test.skip(reason="destructive — sends real iMessage")
@returns("message")
@provides("message_send", account_param="account")
@timeout(45)
async def send_message(*, to, text, **params):
    """Send an iMessage/SMS; the returned entity is chat.db's own receipt.

    Args:
        to: Conversation GUID (from any conversation-returning op), ROWID,
            fuzzy name — or, for a brand-new thread, an E.164 number or
            Apple-ID email (a fuzzy name never creates a conversation).
        text: Message text to send.

    Dispatch is `imsg` (AppleScript into Messages.app — it launches the
    app in the background if needed); success is only ever read back from
    chat.db (`is_sent`, no `error`).
    """
    rowid, handle, self_chat = await _resolve_send_route(to)
    if rowid is None and handle is None:
        return app_error(
            f"No conversation matching {to!r}, and it isn't an E.164 "
            "number or email a new thread could be started for.",
            code="NotFound",
        )

    top = await sql.query("SELECT MAX(ROWID) as top FROM message", db=DB_PATH, params={})
    pre_top = top[0]["top"] or 0

    if handle is not None:
        # Direct (or brand-new) thread — address the person, never the
        # chat id: macOS 26 stores service-agnostic `any;…` guids that
        # AppleScript's `chat id` no longer accepts.
        args = ["send", "--to", handle, "--text", text, "--service", "auto", "--json"]
    else:
        # Group — only a chat id can address it. Upstream regression
        # territory (imsg #90 + the `any;…` guid drift); the receipt
        # read-back below is what keeps a silent drop impossible.
        args = ["send", "--chat-id", str(rowid), "--text", text, "--json"]
    result = await shell.run("imsg", args=args, timeout=20)
    if result["exit_code"] != 0:
        return app_error(
            f"imsg send failed: {(result['stderr'] or result['stdout']).strip()}",
            code="SendFailed",
        )

    if rowid is None:
        # New thread — Messages minted the chat row during the send.
        rowid = await _resolve_conversation_rowid(handle)
    return await _await_receipt(rowid=rowid, pre_top=pre_top, text=text, self_chat=self_chat)


@test.skip(reason="destructive — sends real iMessage")
@returns("message")
@provides("message_send_media", account_param="account")
@timeout(60)
async def send_media(*, to, path=None, bytes=None, filename=None, caption=None, **params):
    """Send a file attachment (image, video, document, audio).

    Two ways in. Point at a file already on disk by `path` (a blob-store
    path or any readable file), or hand raw `bytes` (base64) + a
    `filename` — the fresh-upload door for the UI's attach button and an
    MCP-only agent, neither of which can reach the kernel-private
    `blobs.put`. Inline bytes stage into the blob store here; `imsg`
    reads the resulting file the same way either route.

    (`bytes`, not `data`: the engine reserves `data`/`cache` for an app's
    injected persistent storage, so a `data` tool param is overwritten.)

    Args:
        to: Conversation GUID, ROWID, fuzzy name, or a new-thread handle.
        path: Absolute path to the file — a blob-store path
            (`~/.agentos/blobs/…`) or any readable file. Mutually
            exclusive with `bytes`.
        bytes: Base64 bytes to send — staged into the blob store here.
            Requires `filename`. Mutually exclusive with `path`.
        filename: Original filename for `bytes` (types the payload by
            extension).
        caption: Optional text sent with the attachment.

    Same receipt contract as send_message: chat.db's outgoing row with an
    attachment is the proof, never the CLI exit code.
    """
    if bytes is not None:
        if not filename:
            return app_error(
                "send_media with `bytes` needs a `filename` (for its extension).",
                code="BadParams",
            )
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
        stored = await blobs.put(bytes, ext=ext)
        path = stored["path"]
    elif not path:
        return app_error(
            "send_media needs either `path` (a file path) or "
            "`bytes` (base64) + `filename`.",
            code="BadParams",
        )
    file_path = os.path.expanduser(path)
    if not os.path.isfile(file_path):
        return app_error(f"No file at {path!r}.", code="NotFound")
    rowid, handle, self_chat = await _resolve_send_route(to)
    if rowid is None and handle is None:
        return app_error(
            f"No conversation matching {to!r}, and it isn't an E.164 "
            "number or email a new thread could be started for.",
            code="NotFound",
        )

    top = await sql.query("SELECT MAX(ROWID) as top FROM message", db=DB_PATH, params={})
    pre_top = top[0]["top"] or 0

    args = ["send", "--file", file_path, "--json"]
    if caption:
        args += ["--text", caption]
    if handle is not None:
        args += ["--to", handle, "--service", "auto"]
    else:
        args += ["--chat-id", str(rowid)]
    result = await shell.run("imsg", args=args, timeout=40)
    if result["exit_code"] != 0:
        return app_error(
            f"imsg send failed: {(result['stderr'] or result['stdout']).strip()}",
            code="SendFailed",
        )

    if rowid is None:
        rowid = await _resolve_conversation_rowid(handle)
    return await _await_receipt(
        rowid=rowid, pre_top=pre_top, want_attachment=True, self_chat=self_chat
    )


@returns({"state": "string", "conversationName": "string"})
@provides("message_typing", account_param="account")
@timeout(20)
async def send_typing(*, chat, kind="typing", **params):
    """Show the typing indicator in a chat — or clear it.

    Fire it only when a real send follows (presence is honesty, not
    theater — the same rule WhatsApp enforces). iMessage has no
    "recording" indicator; kinds are `typing` and `paused`.

    Args:
        chat: Conversation GUID, ROWID, or fuzzy name.
        kind: `typing` (default) or `paused` (clears the indicator).
    """
    if kind not in ("typing", "paused"):
        return app_error(
            f"Unknown chat-state kind {kind!r} — iMessage supports typing | paused.",
            code="BadParams",
        )
    rowid = await _resolve_conversation_rowid(chat)
    args = ["typing", "--chat-id", str(rowid), "--json"]
    if kind == "paused":
        args += ["--stop", "true"]
    result = await shell.run("imsg", args=args, timeout=15)
    if result["exit_code"] != 0:
        return app_error(
            f"imsg typing failed: {(result['stderr'] or result['stdout']).strip()}",
            code="SendFailed",
        )
    conv = await get_conversation(id=rowid)
    return {"state": kind, "conversationName": (conv or {}).get("name") or ""}


# ==============================================================================
# Relationship-arc tools (agent reads, not Messaging-app surface)
# ==============================================================================


def _arc_line(m):
    """One timeline line: `YYYY-MM-DD HH:MM  → me / ← Name   body`.

    Direction marker carries who spoke; media (no text body) shows its mime.
    """
    ts = (m.get("published") or "")[:16]
    if m.get("isOutgoing"):
        who = "→ me"
    else:
        who = "← " + (m.get("name") or m.get("author") or "them")
    body = m.get("content")
    if not body:
        body = "[" + (m.get("mime") or "media") + "]"
    return f"{ts}  {who:<16}  {body.replace(chr(10), ' ')}"


def _arc_content(first_n, last_n, *, message_count, first_at, last_at, call_count):
    """Render the relationship arc as a readable timeline string.

    A short thread (total ≤ shown) collapses to one chronological block; a long
    one shows the oldest `n`, a gap, then the newest `n`. This is the body the
    agent reads in one call — `conversation.display.preview` keeps it unclipped.
    """
    head = f"{message_count} messages"
    if first_at and last_at:
        head += f" · {first_at[:10]} → {last_at[:10]}"
    if call_count:
        head += f" · {call_count} call" + ("s" if call_count != 1 else "")

    if message_count <= len(first_n) + len(last_n):
        seen, merged = set(), []
        for m in first_n + last_n:
            if m.get("id") not in seen:
                seen.add(m.get("id"))
                merged.append(m)
        merged.sort(key=lambda m: m.get("published") or "")
        lines = [head, ""] + [_arc_line(m) for m in merged]
    else:
        lines = [head, "", f"oldest {len(first_n)}:"]
        lines += [_arc_line(m) for m in first_n]
        lines += ["", "  ⋯", "", f"newest {len(last_n)}:"]
        lines += [_arc_line(m) for m in last_n]
    return "\n".join(lines)


@returns("conversation")
async def summarize_conversation(*, conversation_id, n=15, **params):
    """One-call relationship arc, rendered into `content` as a readable timeline.

    `content` carries the arc: a `{total} messages · {first} → {last}` header,
    then the oldest `n` and newest `n` messages as dated, direction-marked lines
    (→ you, ← them). Collapses the read-500-and-parse workflow into one call.
    For structured per-message data (programmatic timeline-building) use
    `list_messages` with `order` / `since` / `until`.

    No call line — chat.db carries no call log; macOS keeps calls in a separate
    CallHistory.storedata, surfaced by the `call-history` app's `list_calls`.
    """
    rowid = await _resolve_conversation_rowid(conversation_id)
    names = await contacts.load()

    agg = await sql.query("""
        SELECT
          COUNT(*) as message_count,
          MIN(datetime(m.date / 1000000000 + 978307200, 'unixepoch')) as first_at,
          MAX(datetime(m.date / 1000000000 + 978307200, 'unixepoch')) as last_at
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        WHERE cmj.chat_id = :cid
          AND (m.text IS NOT NULL AND m.text != '' OR m.attributedBody IS NOT NULL)
    """, db=DB_PATH, params={"cid": rowid})
    a = agg[0] if agg else {}

    conv = await get_conversation(id=rowid)

    async def _ends(direction):
        rows = await sql.query(f"""
            SELECT
              {_MSG_COLS}
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE cmj.chat_id = :cid
              AND (m.text IS NOT NULL AND m.text != '' OR m.attributedBody IS NOT NULL)
            ORDER BY m.date {direction}
            LIMIT :n
        """, db=DB_PATH, params={"cid": rowid, "n": n})
        return _name_messages(rows, names)

    first_n = await _ends("ASC")
    last_n = list(reversed(await _ends("DESC")))  # chronological

    return {
        "id": (conv or {}).get("id"),
        "name": (conv or {}).get("name"),
        "isGroup": (conv or {}).get("isGroup") or False,
        "messageCount": a.get("message_count") or 0,
        "content": _arc_content(
            first_n, last_n,
            message_count=a.get("message_count") or 0,
            first_at=a.get("first_at"), last_at=a.get("last_at"),
            call_count=0,
        ),
    }


@test(params={"limit": 5})
@returns({"voice_notes": "list"})
async def list_voice_notes(*, conversation_id=None, limit=200, **params):
    """List iMessage audio/voice-note messages as interval events.

    Returns messages with audio attachments (Audio Message.caf or any audio UTI).
    Includes on-disk mediaPath, sizeBytes, mime. Duration is not stored in chat.db.
    """
    cid_filter = ""
    cid = None
    if conversation_id is not None:
        cid = await _resolve_conversation_rowid(conversation_id)
        cid_filter = "AND cmj.chat_id = :conversation_id"
    rows = await sql.query(f"""
        SELECT
          m.ROWID as id,
          c.guid as chat_guid,
          m.is_from_me as is_outgoing,
          CASE m.is_from_me
            WHEN 1 THEN NULL
            ELSE h.id
          END as sender_handle,
          datetime(m.date / 1000000000 + 978307200, 'unixepoch') as timestamp,
          a.filename as att_filename,
          a.total_bytes as att_bytes,
          a.uti as att_uti,
          a.mime_type as att_mime,
          a.transfer_name as transfer_name
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        JOIN message_attachment_join maj ON m.ROWID = maj.message_id
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE (
          a.uti = 'com.apple.coreaudio-format'
          OR a.uti LIKE 'public.%audio%'
          OR a.mime_type LIKE 'audio/%'
          OR a.transfer_name = 'Audio Message.caf'
        )
        {cid_filter}
        ORDER BY m.date DESC
        LIMIT :limit
    """, db=DB_PATH, params={
        "conversation_id": cid,
        "limit": limit,
    })
    names = await contacts.load()
    results = []
    for r in rows:
        sender = r.get("sender_handle")
        sender_name = contacts.resolve(sender, names) if sender else None
        entry = {
            "id": r["id"],
            "conversationId": r["chat_guid"],
            "start": r["timestamp"],
            "kind": "audio",
            "isOutgoing": bool(r.get("is_outgoing")),
            "mediaPath": _expand_path(r.get("att_filename")),
            "sizeBytes": r.get("att_bytes") or 0,
            "mime": _resolve_mime(r.get("att_uti"), r.get("att_filename"), r.get("att_mime")),
        }
        if sender:
            entry["by"] = sender_name or sender
        results.append(entry)
    return {"voice_notes": results}
