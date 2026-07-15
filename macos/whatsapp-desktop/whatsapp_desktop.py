"""WhatsApp Desktop app — read local on-device message history.

Reads the WhatsApp macOS app's Core Data SQLite database directly.
Timestamps: ZMESSAGEDATE is seconds since 2001-01-01 (Core Data epoch);
add 978307200 to get Unix time. No browser, no auth, no sync limit.

Message type codes (ZMESSAGETYPE):
  0=text  1=image  2=audio  3=video-or-opus  4=vcard  5=location
  6=system  7=url-preview  8=gif  9=document  10=voice-call  11=video-call
  14=deleted  15=broadcast  59=call-event  66=sticker

Call records come from CallHistory.sqlite (ZWACDCALLEVENT) which carries
duration; call rows in ChatStorage.sqlite carry status via ZGROUPEVENTTYPE.

Voice notes are type 3 (.opus). Type 59 is NOT a voice note — it's a
call-event row mirroring CallHistory (ZSTANZAID == the call's ZCALLIDSTRING;
ZGROUPEVENTTYPE = call status). Media (incl. undownloaded voice notes) can be
fetched + decrypted offline from ZMEDIAURL + ZMEDIAKEY while the URL signature
is live — see "Media download & decryption" in readme.md.
"""

import base64
import os
import re
import tarfile
import time

from agentos import blobs, client, connection, crypto, returns, shell, sql, test, timeout, canonicalize_datetime
from agentos.results import app_error


connection(
    'db',
    sqlite='~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite')

DB_PATH = "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
CALLHISTORY_DB = "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/CallHistory.sqlite"
MEDIA_ROOT = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/Message")

# ZGROUPEVENTTYPE values for call messages (type 10/11) in ChatStorage
_CALL_STATUS = {
    2: "answered",
    3: "ringing",
    4: "declined",
    5: "missed",
    34: "group-offered",
}

# Human labels for message types (for filtering / display)
_MSG_TYPE_LABEL = {
    0: "text",
    1: "image",
    2: "audio",
    3: "video",      # also .opus voice notes in older format
    4: "contact",
    5: "location",
    6: "system",
    7: "link",
    8: "gif",
    9: "document",
    10: "call-voice",
    11: "call-video",
    14: "deleted",
    15: "broadcast",
    19: "ephemeral",
    54: "poll",
    59: "call-event",
    66: "sticker",
}

# System/housekeeping types to hide by default
_SYSTEM_TYPES = {6, 14, 15, 19, 28, 46, 75, 76}


# ==============================================================================
# Helpers
# ==============================================================================


async def _load_push_names():
    """Load ZWAPROFILEPUSHNAME into a {jid -> name} dict (all @lid entries)."""
    rows = await sql.query(
        "SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZPUSHNAME IS NOT NULL",
        db=DB_PATH, params={})
    return {r["ZJID"]: r["ZPUSHNAME"] for r in rows}


def _full_media_path(local_path):
    """Expand a ZMEDIALOCALPATH relative to the group container."""
    if not local_path:
        return None
    return os.path.join(MEDIA_ROOT, local_path)


def _mime_for_path(local_path):
    if not local_path:
        return None
    ext = os.path.splitext(local_path)[1].lower()
    return {
        ".opus": "audio/ogg; codecs=opus",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
    }.get(ext)


# ------------------------------------------------------------------------------
# Media decryption (see readme.md → "Media download & decryption")
# ------------------------------------------------------------------------------

# WhatsApp HKDF application-info string, keyed by media kind. The kind drives the
# key expansion — a wrong info yields right-length garbage, so resolve it from
# the on-disk extension when present, else the CDN class / message type.
_HKDF_INFO = {
    "audio": "WhatsApp Audio Keys",
    "video": "WhatsApp Video Keys",
    "image": "WhatsApp Image Keys",
    "document": "WhatsApp Document Keys",
}

_EXT_KIND = {
    ".opus": "audio", ".ogg": "audio", ".m4a": "audio", ".mp3": "audio", ".aac": "audio",
    ".mp4": "video", ".mov": "video", ".3gp": "video",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image", ".gif": "image",
    ".pdf": "document", ".doc": "document", ".docx": "document", ".zip": "document",
}

# Default output extension per kind, when the source path doesn't name one.
_KIND_EXT = {"audio": "opus", "video": "mp4", "image": "jpg", "document": "bin"}


def _media_kind(local_path, url, msg_type):
    """Resolve media kind (audio/video/image/document) for HKDF + output ext."""
    if local_path:
        k = _EXT_KIND.get(os.path.splitext(local_path)[1].lower())
        if k:
            return k
    u = url or ""
    if "t62.7117-24" in u:        # WhatsApp audio/PTT CDN class
        return "audio"
    if "t62.7161-24" in u:        # WhatsApp video CDN class
        return "video"
    return {1: "image", 2: "audio", 3: "audio", 9: "document"}.get(msg_type, "audio")


def _media_key_from_protobuf(key_hex):
    """ZMEDIAKEY is a protobuf; field 1 (length-delimited) is the 32-byte mediaKey."""
    blob = bytes.fromhex(key_hex)
    if not blob or blob[0] != 0x0A:   # 0x0A = field 1, wire-type 2
        raise ValueError("unexpected ZMEDIAKEY protobuf layout")
    i, length, shift = 1, 0, 0
    while True:
        b = blob[i]; i += 1
        length |= (b & 0x7F) << shift; shift += 7
        if not b & 0x80:
            break
    return blob[i:i + length]


def _url_expiry(url):
    """Unix expiry from the signed CDN URL's `oe=` (hex), or None."""
    m = re.search(r"oe=([0-9A-Fa-f]+)", url or "")
    return int(m.group(1), 16) if m else None


def _map_conversation(row):
    return {
        "id": str(row["id"]),
        "name": row.get("name") or row.get("contact_jid"),
        "published": canonicalize_datetime(row.get("last_message_at")),
        "isGroup": bool(row.get("is_group")),
        "isArchived": bool(row.get("archived")),
        "unreadCount": row.get("unread_count"),
        "contactJid": row.get("contact_jid"),
    }


def _map_message(row, push_names=None):
    """Map a raw SQL row to a message dict.

    Sender resolution (replaces the broken ZPUSHNAME blob):
    - 1:1 chats, incoming → ZPARTNERNAME from the session (already joined)
    - group chats, incoming → ZWAPROFILEPUSHNAME lookup by ZFROMJID
    Both paths avoid reading the binary ZPUSHNAME column on ZWAMESSAGE.
    """
    msg_type = row.get("message_type")
    type_label = _MSG_TYPE_LABEL.get(msg_type)

    result = {
        "id": row["id"],
        "conversationId": row.get("conversation_id"),
        "content": row.get("content"),
        "published": canonicalize_datetime(row.get("timestamp")),
        "isOutgoing": bool(row.get("is_from_me")),
        "typeCode": msg_type,
        "typeLabel": type_label,
    }

    # Call rows (10/11/59) carry no ZTEXT — their status lives in
    # ZGROUPEVENTTYPE instead. Surface it so a consumer can render "Missed
    # voice call" inline rather than an empty message.
    if msg_type in (10, 11, 59):
        result["callStatus"] = _CALL_STATUS.get(row.get("group_event_type"))
        result["callKind"] = "video" if msg_type == 11 else "voice"

    # Sender
    if not row.get("is_from_me"):
        from_jid = row.get("from_jid")
        result["author"] = from_jid

        # Name: prefer session partner name (1:1), fall back to push-name table
        partner_name = row.get("partner_name")
        if partner_name:
            result["authorName"] = partner_name
        elif push_names and from_jid:
            pn = push_names.get(from_jid)
            if pn:
                result["authorName"] = pn

    # Media metadata (from ZWAMEDIAITEM join)
    local_path = row.get("media_local_path")
    duration_secs = row.get("media_duration")
    file_size = row.get("media_size")

    if local_path or file_size:
        full_path = _full_media_path(local_path)
        result["mediaPath"] = full_path
        result["sizeBytes"] = file_size or 0
        result["mime"] = _mime_for_path(local_path)
        if duration_secs:
            result["durationMin"] = round(duration_secs / 60, 3)
            result["durationSecs"] = duration_secs

    return result


def _map_call(row):
    """Map a ZWACDCALLEVENT + ZWAAGGREGATECALLEVENT join row to an interval event."""
    duration_secs = row.get("duration") or 0
    is_incoming = bool(row.get("incoming"))
    missed = bool(row.get("missed"))
    is_video = bool(row.get("video"))

    if missed:
        status = "missed"
    elif duration_secs > 0:
        status = "answered"
    else:
        status = "declined"

    partner_jid = row.get("partner_jid") or ""

    return {
        "id": row.get("call_id"),
        "kind": "video" if is_video else "voice",
        "status": status,
        "start": row.get("start"),
        "durationSecs": duration_secs,
        "durationMin": round(duration_secs / 60, 3) if duration_secs else None,
        "isIncoming": is_incoming,
        "by": "me" if not is_incoming else partner_jid,
        "partnerJid": partner_jid,
        "partnerName": row.get("partner_name"),
        "bytesReceived": row.get("bytes_received"),
        "bytesSent": row.get("bytes_sent"),
    }


async def _resolve_conversation_id(conversation_id):
    """Accept an integer Z_PK or a fuzzy ZPARTNERNAME substring."""
    if isinstance(conversation_id, int) or (
        isinstance(conversation_id, str) and conversation_id.isdigit()
    ):
        return int(conversation_id)
    rows = await sql.query(
        "SELECT Z_PK FROM ZWACHATSESSION WHERE ZPARTNERNAME LIKE :pat LIMIT 1",
        db=DB_PATH,
        params={"pat": f"%{conversation_id}%"},
    )
    if not rows:
        raise ValueError(f"No conversation matching {conversation_id!r}")
    return rows[0]["Z_PK"]


# ==============================================================================
# Conversation operations
# ==============================================================================


@test(params={"limit": 3})
@returns("conversation[]")
async def list_conversations(*, limit=200, archived=False, **params):
    """List WhatsApp conversations from local storage, most recent first."""
    rows = await sql.query("""
        SELECT
          s.Z_PK                                                         AS id,
          s.ZPARTNERNAME                                                  AS name,
          s.ZCONTACTJID                                                   AS contact_jid,
          CASE WHEN s.ZSESSIONTYPE = 1 THEN 1 ELSE 0 END                 AS is_group,
          s.ZARCHIVED                                                     AS archived,
          s.ZUNREADCOUNT                                                  AS unread_count,
          datetime(s.ZLASTMESSAGEDATE + 978307200, 'unixepoch')          AS last_message_at
        FROM ZWACHATSESSION s
        WHERE s.ZARCHIVED = :archived
          AND s.ZLASTMESSAGEDATE IS NOT NULL
        ORDER BY s.ZLASTMESSAGEDATE DESC
        LIMIT :limit
    """, db=DB_PATH, params={"archived": 1 if archived else 0, "limit": limit})
    return [_map_conversation(r) for r in rows]


@returns("conversation")
async def get_conversation(*, id, **params):
    """Get a specific conversation by ID or fuzzy name."""
    cid = await _resolve_conversation_id(id)
    rows = await sql.query("""
        SELECT
          s.Z_PK                                                         AS id,
          s.ZPARTNERNAME                                                  AS name,
          s.ZCONTACTJID                                                   AS contact_jid,
          CASE WHEN s.ZSESSIONTYPE = 1 THEN 1 ELSE 0 END                 AS is_group,
          s.ZARCHIVED                                                     AS archived,
          s.ZUNREADCOUNT                                                  AS unread_count,
          datetime(s.ZLASTMESSAGEDATE + 978307200, 'unixepoch')          AS last_message_at
        FROM ZWACHATSESSION s
        WHERE s.Z_PK = :id
    """, db=DB_PATH, params={"id": cid})
    return _map_conversation(rows[0]) if rows else None


# ==============================================================================
# Message operations
# ==============================================================================


def _date_filter(since, until):
    """Build a (sql_fragment, params) pair for since/until on ZMESSAGEDATE.

    since/until are ISO date or datetime strings (e.g. "2024-01-01"). They
    compare against the computed ISO `timestamp` column; SQLite orders ISO
    strings lexically, so a plain >=/<= works without epoch math.
    """
    frag, params = "", {}
    if since:
        frag += " AND datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch') >= :since"
        params["since"] = since
    if until:
        # A bare date as the upper bound means "through the end of that day".
        # Timestamps carry a time, so lexically '2026-01-11 12:35' > '2026-01-11'
        # — a date-only `until` would otherwise drop the whole day it names.
        if len(until) == 10:
            until = until + " 23:59:59"
        frag += " AND datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch') <= :until"
        params["until"] = until
    return frag, params


@test(params={"conversation_id": 1, "limit": 3})
@returns("message[]")
async def list_messages(*, conversation_id, limit=200, include_system=False,
                        since=None, until=None, order="desc", **params):
    """List messages in a conversation.

    conversation_id: integer Z_PK or fuzzy ZPARTNERNAME substring.
    include_system: if False (default), hides system/housekeeping rows
                    (group-join, deleted, ephemeral, etc.).
    since / until:  ISO date/datetime bounds (inclusive), e.g. "2024-01-01".
    order:          "desc" (newest first, default) or "asc" (oldest first —
                    read a relationship from its beginning).

    Each message includes:
      typeCode / typeLabel — numeric code + human label (text/image/voice-note/…)
      author / authorName  — sender JID + resolved display name (incoming only)
      mediaPath            — full on-disk path when the file is locally cached
      sizeBytes / mime     — file size + MIME type for media messages
      durationSecs / durationMin — for audio/video messages
    """
    cid = await _resolve_conversation_id(conversation_id)

    # Fetch push-name map once (for group chats); cheap for 1:1 but harmless
    push_names = await _load_push_names()

    system_filter = "" if include_system else (
        f"AND m.ZMESSAGETYPE NOT IN ({','.join(str(t) for t in _SYSTEM_TYPES)})"
    )
    date_filter, date_params = _date_filter(since, until)
    direction = "ASC" if str(order).lower() == "asc" else "DESC"

    rows = await sql.query(f"""
        SELECT
          m.Z_PK                                                         AS id,
          m.ZCHATSESSION                                                  AS conversation_id,
          m.ZTEXT                                                         AS content,
          m.ZISFROMME                                                     AS is_from_me,
          m.ZFROMJID                                                      AS from_jid,
          m.ZMESSAGETYPE                                                  AS message_type,
          m.ZGROUPEVENTTYPE                                               AS group_event_type,
          datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch')              AS timestamp,
          -- Sender name: ZPARTNERNAME for 1:1 chats (session join)
          CASE WHEN s.ZSESSIONTYPE = 0 AND m.ZISFROMME = 0
               THEN s.ZPARTNERNAME
               ELSE NULL
          END                                                             AS partner_name,
          -- Media metadata from ZWAMEDIAITEM
          mi.ZMEDIALOCALPATH                                              AS media_local_path,
          mi.ZMOVIEDURATION                                               AS media_duration,
          mi.ZFILESIZE                                                    AS media_size
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
        LEFT JOIN ZWAMEDIAITEM mi ON m.ZMEDIAITEM = mi.Z_PK
        WHERE m.ZCHATSESSION = :cid
          {system_filter}
          {date_filter}
        ORDER BY m.ZMESSAGEDATE {direction}
        LIMIT :limit
    """, db=DB_PATH, params={"cid": cid, "limit": limit, **date_params})

    return [_map_message(r, push_names) for r in rows]


def _arc_line(m):
    """One timeline line: `YYYY-MM-DD HH:MM  → me / ← Name   body`.

    Direction marker carries who spoke; media (no text body) shows its typeLabel.
    """
    ts = (m.get("published") or "")[:16]
    if m.get("isOutgoing"):
        who = "→ me"
    else:
        who = "← " + (m.get("authorName") or m.get("author") or "them")
    body = m.get("content")
    if not body:
        body = "[" + (m.get("typeLabel") or "media") + "]"
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
async def summarize_conversation(*, conversation_id, n=15, include_system=False, **params):
    """One-call relationship arc, rendered into `content` as a readable timeline.

    `content` carries the arc: a `{total} messages · {first} → {last} · {calls}`
    header, then the oldest `n` and newest `n` messages as dated, direction-marked
    lines (→ you, ← them). Collapses the read-500-and-parse workflow into one call.
    For structured per-message data (programmatic timeline-building) use
    `list_messages` with `order` / `since` / `until`.
    """
    cid = await _resolve_conversation_id(conversation_id)
    push_names = await _load_push_names()
    system_filter = "" if include_system else (
        f"AND m.ZMESSAGETYPE NOT IN ({','.join(str(t) for t in _SYSTEM_TYPES)})"
    )

    agg = await sql.query(f"""
        SELECT
          COUNT(*)                                              AS message_count,
          MIN(datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch')) AS first_at,
          MAX(datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch')) AS last_at,
          s.ZPARTNERNAME                                        AS name,
          s.ZCONTACTJID                                         AS contact_jid
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
        WHERE m.ZCHATSESSION = :cid {system_filter}
    """, db=DB_PATH, params={"cid": cid})
    a = agg[0] if agg else {}

    async def _ends(direction):
        rows = await sql.query(f"""
            SELECT
              m.Z_PK AS id, m.ZCHATSESSION AS conversation_id, m.ZTEXT AS content,
              m.ZISFROMME AS is_from_me, m.ZFROMJID AS from_jid,
              m.ZMESSAGETYPE AS message_type,
              datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch') AS timestamp,
              CASE WHEN s.ZSESSIONTYPE = 0 AND m.ZISFROMME = 0
                   THEN s.ZPARTNERNAME ELSE NULL END AS partner_name,
              mi.ZMEDIALOCALPATH AS media_local_path,
              mi.ZMOVIEDURATION AS media_duration, mi.ZFILESIZE AS media_size
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
            LEFT JOIN ZWAMEDIAITEM mi ON m.ZMEDIAITEM = mi.Z_PK
            WHERE m.ZCHATSESSION = :cid {system_filter}
            ORDER BY m.ZMESSAGEDATE {direction}
            LIMIT :n
        """, db=DB_PATH, params={"cid": cid, "n": n})
        return [_map_message(r, push_names) for r in rows]

    first_n = await _ends("ASC")
    last_n = list(reversed(await _ends("DESC")))  # chronological

    # Call count: CallHistory rows whose partner matches this session's contact.
    call_count = 0
    contact_jid = a.get("contact_jid")
    if contact_jid:
        bare = contact_jid.split("@")[0]
        crows = await sql.query("""
            SELECT COUNT(*) AS n FROM ZWACDCALLEVENT
            WHERE ZGROUPCALLCREATORUSERJIDSTRING IN (:jid, :bare)
        """, db=CALLHISTORY_DB, params={"jid": contact_jid, "bare": bare})
        call_count = (crows[0].get("n") if crows else 0) or 0

    return {
        "id": str(cid),
        "name": a.get("name"),
        "isGroup": bool(a.get("contact_jid") and "@g.us" in (a.get("contact_jid") or "")),
        "messageCount": a.get("message_count") or 0,
        "content": _arc_content(
            first_n, last_n,
            message_count=a.get("message_count") or 0,
            first_at=a.get("first_at"), last_at=a.get("last_at"),
            call_count=call_count,
        ),
    }


@returns("message")
async def get_message(*, id, **params):
    """Get a specific message by Z_PK."""
    push_names = await _load_push_names()
    rows = await sql.query("""
        SELECT
          m.Z_PK                                                         AS id,
          m.ZCHATSESSION                                                  AS conversation_id,
          m.ZTEXT                                                         AS content,
          m.ZISFROMME                                                     AS is_from_me,
          m.ZFROMJID                                                      AS from_jid,
          m.ZMESSAGETYPE                                                  AS message_type,
          m.ZGROUPEVENTTYPE                                               AS group_event_type,
          datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch')              AS timestamp,
          CASE WHEN s.ZSESSIONTYPE = 0 AND m.ZISFROMME = 0
               THEN s.ZPARTNERNAME ELSE NULL END                         AS partner_name,
          mi.ZMEDIALOCALPATH                                              AS media_local_path,
          mi.ZMOVIEDURATION                                               AS media_duration,
          mi.ZFILESIZE                                                    AS media_size
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
        LEFT JOIN ZWAMEDIAITEM mi ON m.ZMEDIAITEM = mi.Z_PK
        WHERE m.Z_PK = :id
    """, db=DB_PATH, params={"id": id})
    return _map_message(rows[0], push_names) if rows else None


@test(params={"query": "hello", "limit": 3})
@returns("message[]")
async def search_messages(*, query, conversation_id=None, limit=200, **params):
    """Search messages by text content across all chats, or within one chat.

    conversation_id: optional integer Z_PK or fuzzy name to scope the search.
    """
    push_names = await _load_push_names()
    if conversation_id is not None:
        cid = await _resolve_conversation_id(conversation_id)
        rows = await sql.query("""
            SELECT
              m.Z_PK                                                     AS id,
              m.ZCHATSESSION                                              AS conversation_id,
              s.ZPARTNERNAME                                              AS conversation_name,
              m.ZTEXT                                                     AS content,
              m.ZISFROMME                                                 AS is_from_me,
              m.ZFROMJID                                                  AS from_jid,
              m.ZMESSAGETYPE                                              AS message_type,
              datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch')          AS timestamp,
              CASE WHEN s.ZSESSIONTYPE = 0 AND m.ZISFROMME = 0
                   THEN s.ZPARTNERNAME ELSE NULL END                     AS partner_name,
              mi.ZMEDIALOCALPATH                                          AS media_local_path,
              mi.ZMOVIEDURATION                                           AS media_duration,
              mi.ZFILESIZE                                                AS media_size
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
            LEFT JOIN ZWAMEDIAITEM mi ON m.ZMEDIAITEM = mi.Z_PK
            WHERE m.ZCHATSESSION = :cid
              AND m.ZTEXT LIKE '%' || :query || '%'
            ORDER BY m.ZMESSAGEDATE DESC
            LIMIT :limit
        """, db=DB_PATH, params={"cid": cid, "query": query, "limit": limit})
    else:
        rows = await sql.query("""
            SELECT
              m.Z_PK                                                     AS id,
              m.ZCHATSESSION                                              AS conversation_id,
              s.ZPARTNERNAME                                              AS conversation_name,
              m.ZTEXT                                                     AS content,
              m.ZISFROMME                                                 AS is_from_me,
              m.ZFROMJID                                                  AS from_jid,
              m.ZMESSAGETYPE                                              AS message_type,
              datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch')          AS timestamp,
              CASE WHEN s.ZSESSIONTYPE = 0 AND m.ZISFROMME = 0
                   THEN s.ZPARTNERNAME ELSE NULL END                     AS partner_name,
              mi.ZMEDIALOCALPATH                                          AS media_local_path,
              mi.ZMOVIEDURATION                                           AS media_duration,
              mi.ZFILESIZE                                                AS media_size
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
            LEFT JOIN ZWAMEDIAITEM mi ON m.ZMEDIAITEM = mi.Z_PK
            WHERE m.ZTEXT LIKE '%' || :query || '%'
            ORDER BY m.ZMESSAGEDATE DESC
            LIMIT :limit
        """, db=DB_PATH, params={"query": query, "limit": limit})
    return [_map_message(r, push_names) for r in rows]


# ==============================================================================
# Call operations (CallHistory.sqlite)
# ==============================================================================


@test(params={"limit": 5})
@returns({"calls": "list"})
async def list_calls(*, limit=200, conversation_id=None, **params):
    """List WhatsApp call history as interval events, most recent first.

    Sources: ZWACDCALLEVENT (duration, outcome, timestamps, partner JID)
    joined with ZWAAGGREGATECALLEVENT (direction, missed flag, video flag).

    Each call event:
      id           — ZCALLIDSTRING from CallHistory.sqlite
      kind         — "voice" | "video"
      status       — "answered" | "missed" | "declined"
      start        — ISO datetime of call start
      durationSecs — call duration in seconds (0 for missed/declined)
      durationMin  — duration in minutes (rounded to 3 dp)
      isIncoming   — True if they called me, False if I called them
      by           — "me" for outgoing; partner JID for incoming
      partnerJid   — the other person's JID (always)
      partnerName  — resolved display name if known
      bytesReceived / bytesSent — network usage
    """
    push_names = await _load_push_names()

    # Partner filter: if conversation_id is given, look up the session's contact JID
    partner_filter = ""
    partner_jid_filter = None
    if conversation_id is not None:
        cid = await _resolve_conversation_id(conversation_id)
        session = await sql.query(
            "SELECT ZCONTACTJID FROM ZWACHATSESSION WHERE Z_PK = :id",
            db=DB_PATH, params={"id": cid})
        if session:
            partner_jid_filter = session[0].get("ZCONTACTJID")

    rows = await sql.query("""
        SELECT
          c.ZCALLIDSTRING                                                AS call_id,
          datetime(c.ZDATE + 978307200, 'unixepoch')                    AS start,
          c.ZDURATION                                                    AS duration,
          c.ZOUTCOME                                                     AS outcome,
          c.ZGROUPCALLCREATORUSERJIDSTRING                               AS partner_jid,
          c.ZGROUPJIDSTRING                                              AS group_jid,
          c.ZBYTESRECEIVED                                               AS bytes_received,
          c.ZBYTESSENT                                                   AS bytes_sent,
          a.ZINCOMING                                                    AS incoming,
          a.ZMISSED                                                      AS missed,
          a.ZVIDEO                                                       AS video
        FROM ZWACDCALLEVENT c
        LEFT JOIN ZWAAGGREGATECALLEVENT a ON c.Z1CALLEVENTS = a.Z_PK
        ORDER BY c.ZDATE DESC
        LIMIT :limit
    """, db=CALLHISTORY_DB, params={"limit": limit})

    calls = []
    for r in rows:
        partner_jid = r.get("partner_jid") or ""
        # Apply partner filter if requested
        if partner_jid_filter and partner_jid_filter not in (partner_jid, partner_jid + "@s.whatsapp.net"):
            continue
        c = _map_call(r)
        # Resolve name from push-names table (already keyed by @lid JID)
        if partner_jid and not c.get("partnerName"):
            c["partnerName"] = push_names.get(partner_jid)
        calls.append(c)

    return {"calls": calls}


# ==============================================================================
# Voice note listing
# ==============================================================================


@test(params={"conversation_id": 591, "limit": 5})
@returns({"voice_notes": "list"})
async def list_voice_notes(*, conversation_id=None, limit=200, **params):
    """List voice notes, newest first.

    Voice notes are ZMESSAGETYPE = 3 (`.opus`, the `t62.7117-24` audio/ptt CDN
    class). NOT type 59 — that's a call-event row (see module docstring). Covers
    both downloaded (mediaPath set) and not-yet-downloaded (mediaPath None, but
    ZMEDIAURL + ZMEDIAKEY present for `download_media`).

    Each voice note:
      id, conversationId, published, isOutgoing
      author / authorName — sender info
      durationSecs / durationMin — duration (ZMOVIEDURATION)
      sizeBytes / mime — file metadata
      mediaPath — full on-disk path (None if not yet downloaded)
    """
    push_names = await _load_push_names()

    cid_filter = ""
    filter_params: dict = {"limit": limit}
    if conversation_id is not None:
        cid = await _resolve_conversation_id(conversation_id)
        cid_filter = "AND m.ZCHATSESSION = :cid"
        filter_params["cid"] = cid

    rows = await sql.query(f"""
        SELECT
          m.Z_PK                                                         AS id,
          m.ZCHATSESSION                                                  AS conversation_id,
          m.Zisfromme                                                     AS is_from_me,
          m.ZFROMJID                                                      AS from_jid,
          m.ZMESSAGETYPE                                                  AS message_type,
          datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch')              AS timestamp,
          CASE WHEN s.ZSESSIONTYPE = 0 AND m.ZISFROMME = 0
               THEN s.ZPARTNERNAME ELSE NULL END                         AS partner_name,
          mi.ZMEDIALOCALPATH                                              AS media_local_path,
          mi.ZMOVIEDURATION                                               AS media_duration,
          mi.ZFILESIZE                                                    AS media_size
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
        LEFT JOIN ZWAMEDIAITEM mi ON m.ZMEDIAITEM = mi.Z_PK
        WHERE m.ZMESSAGETYPE = 3
          AND (
            mi.ZMEDIALOCALPATH LIKE '%.opus'
            OR mi.ZMEDIAURL LIKE '%t62.7117-24%'
            OR mi.ZMOVIEDURATION > 0
          )
        {cid_filter}
        ORDER BY m.ZMESSAGEDATE DESC
        LIMIT :limit
    """, db=DB_PATH, params=filter_params)

    notes = []
    for r in rows:
        from_jid = r.get("from_jid")
        is_outgoing = bool(r.get("is_from_me"))
        local_path = r.get("media_local_path")
        duration_secs = r.get("media_duration") or 0
        file_size = r.get("media_size") or 0

        note = {
            "id": r["id"],
            "conversationId": r.get("conversation_id"),
            "published": canonicalize_datetime(r.get("timestamp")),
            "isOutgoing": is_outgoing,
            "typeCode": r.get("message_type"),
        }

        if not is_outgoing:
            note["author"] = from_jid
            name = r.get("partner_name") or push_names.get(from_jid or "")
            if name:
                note["authorName"] = name

        full_path = _full_media_path(local_path)
        note["mediaPath"] = full_path
        note["sizeBytes"] = file_size
        note["mime"] = _mime_for_path(local_path) or "audio/ogg; codecs=opus"
        if duration_secs:
            note["durationSecs"] = duration_secs
            note["durationMin"] = round(duration_secs / 60, 3)

        notes.append(note)

    return {"voice_notes": notes}


# ==============================================================================
# Media download
# ==============================================================================


@returns({
    "messageId": "int", "status": "string", "path": "string",
    "mime": "string", "sizeBytes": "int", "kind": "string",
})
async def download_media(*, message_id, **params):
    """Download + decrypt a message's media to disk, returning the on-disk path.

    WhatsApp media is end-to-end encrypted on Meta's CDN; the local DB holds a
    signed URL (ZMEDIAURL) and the key (ZMEDIAKEY). This fetches the encrypted
    blob and decrypts it offline — no WhatsApp session — via the engine's
    crypto.hkdf + crypto.aes (see readme.md → "Media download & decryption").

    Primary target: voice notes (type 3, `.opus`). Works for any media kind.

    Returns `status`:
      already_on_disk — WhatsApp already cached it; `path` is its location.
      downloaded      — fetched + decrypted just now; `path` is the blob store.
      expired         — the URL signature has lapsed (~25-day window); the bytes
                        can't be re-fetched offline. Open the message in the
                        WhatsApp app (or the live `whatsapp` connector) to refresh
                        it, then it lands on disk and re-calling returns its path.
    """
    rows = await sql.query("""
        SELECT
          m.ZMESSAGETYPE                                   AS message_type,
          mi.ZMEDIALOCALPATH                               AS media_local_path,
          mi.ZMEDIAURL                                     AS media_url,
          hex(mi.ZMEDIAKEY)                                AS media_key,
          mi.ZFILESIZE                                     AS media_size
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAMEDIAITEM mi ON m.ZMEDIAITEM = mi.Z_PK
        WHERE m.Z_PK = :id
    """, db=DB_PATH, params={"id": int(message_id)})
    if not rows:
        return app_error(code="NotFound", message=f"No message with Z_PK {message_id}")
    row = rows[0]

    msg_type = row.get("message_type")
    local_path = row.get("media_local_path")
    url = row.get("media_url")
    key_hex = row.get("media_key")
    kind = _media_kind(local_path, url, msg_type)

    # 1) Already cached by WhatsApp → just return the on-disk path.
    if local_path:
        full = _full_media_path(local_path)
        if full and os.path.exists(full):
            return {
                "messageId": int(message_id), "status": "already_on_disk",
                "path": full, "mime": _mime_for_path(local_path) or "",
                "sizeBytes": os.path.getsize(full), "kind": kind,
            }

    if not url or not key_hex:
        return app_error(
            code="NoMedia",
            message=f"Message {message_id} has no downloadable media "
                    f"(type {msg_type}); not a media message, or never synced.")

    # 2) Signature expired → can't fetch offline.
    expiry = _url_expiry(url)
    if expiry and expiry < time.time():
        return {
            "messageId": int(message_id), "status": "expired", "path": "",
            "mime": "", "sizeBytes": 0, "kind": kind,
        }

    # 3) Fetch the encrypted blob + decrypt offline.
    media_key = _media_key_from_protobuf(key_hex)
    info = _HKDF_INFO[kind]
    expanded = await crypto.hkdf(key=media_key.hex(), info=info, length=112)
    iv, cipher_key = expanded[:16], expanded[16:48]

    resp = await client.get(url)
    enc_hex = resp.get("body_bytes")
    if not enc_hex:
        return app_error(
            code="FetchFailed",
            message=f"CDN fetch returned no binary body (status {resp.get('status')}).")
    enc = bytes.fromhex(enc_hex)
    ciphertext = enc[:-10]   # last 10 bytes are the MAC, not ciphertext

    plaintext = await crypto.aes(
        key=cipher_key.hex(), data=ciphertext.hex(), iv=iv.hex())

    out_ext = (os.path.splitext(local_path)[1].lstrip(".").lower()
               if local_path else _KIND_EXT[kind])
    stored = await blobs.put(base64.b64encode(plaintext).decode(), ext=out_ext)

    return {
        "messageId": int(message_id), "status": "downloaded",
        "path": stored["path"], "mime": _mime_for_path(f"x.{out_ext}") or "",
        "sizeBytes": stored["size"], "kind": kind,
    }


# ==============================================================================
# Diagnostic (dev tool — not for production use)
# ==============================================================================


@returns({"tables": "string", "columns": "string", "sample": "string"})
async def diag(*, table="ZWAMESSAGE", conversation_id=None, query=None, db=None, **params):
    """Diagnostic: dump table schema + raw rows, or run a custom SQL query.

    db: optional path to a different SQLite file (e.g. CALLHISTORY_DB).
    """
    import json
    target_db = db or DB_PATH
    tables_rows = await sql.query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        db=target_db, params={})
    tables = [r["name"] for r in tables_rows]
    cols = []
    if table in tables:
        cols_rows = await sql.query(f"PRAGMA table_info({table})", db=target_db, params={})
        cols = [r["name"] for r in cols_rows]
    if query:
        rows = await sql.query(query, db=target_db, params={})
    elif conversation_id and table == "ZWAMESSAGE":
        rows = await sql.query(
            f"SELECT * FROM {table} WHERE ZCHATSESSION = :cid LIMIT 10",
            db=target_db, params={"cid": conversation_id})
    else:
        rows = await sql.query(f"SELECT * FROM {table} LIMIT 5", db=target_db, params={}) if tables else []
    return {
        "tables": json.dumps(tables),
        "columns": json.dumps(cols),
        "sample": json.dumps(rows, default=str),
    }


# ==============================================================================
# iCloud backup media import  (see readme.md → "Importing media from the iCloud backup")
# ==============================================================================
#
# The Desktop db (above) only holds the recent slice synced to this companion —
# the *deep* history lives in the phone's iCloud backup. WhatsApp's iCloud
# backup is a regular iCloud Drive container that macOS syncs to disk as
# dataless stubs. Its media archives (Media.tar / Video.tar / Document.tar) are
# **plaintext** POSIX tars organised by conversation — only the message-text db
# (ChatStorage.sqlite.enc) is encrypted. So media from any chat, full history,
# extracts with no key: hydrate the tar (brctl) → extract that conversation's
# members → land in the blob store. Conversation→folder ids come from the same
# Desktop db (ZWACHATSESSION: phone-JID + the @lid in ZCONTACTIDENTIFIER); the
# full-history Media.tar keys DMs by phone-JID, recent media by @lid, so we
# match both. Groups key by @g.us in both.

# The iCloud Drive container WhatsApp backs up into (Team ID ~ reverse-DNS).
ICLOUD_WA_ACCOUNTS = os.path.expanduser(
    "~/Library/Mobile Documents/57T9237FN3~net~whatsapp~WhatsApp/Accounts")

# <sys/stat.h> SF_DATALESS — the file is an iCloud stub; bytes not resident.
SF_DATALESS = 0x40000000

# Media-bearing archives in a backup, in import-priority order.
_MEDIA_TARS = ("Media.tar", "Video.tar", "Document.tar", "GIFs.tar")


def _backup_dirs():
    """Every `<account>/backup` dir under the iCloud WhatsApp container."""
    if not os.path.isdir(ICLOUD_WA_ACCOUNTS):
        return []
    out = []
    for acct in sorted(os.listdir(ICLOUD_WA_ACCOUNTS)):
        path = os.path.join(ICLOUD_WA_ACCOUNTS, acct, "backup")
        if os.path.isdir(path):
            out.append((acct, path))
    return out


def _is_dataless(path):
    try:
        return bool(os.stat(path).st_flags & SF_DATALESS)
    except OSError:
        return False


@test(params={})
@returns({"present": "boolean", "accounts": "list"})
async def backup_status(**params):
    """Report the iCloud WhatsApp backup(s) on this Mac and what's hydrated.

    Reads metadata only — never triggers a download. For each backed-up
    account it lists the archive files with size, whether the bytes are
    downloaded locally (`hydrated`) or still an iCloud stub, and whether the
    file is encrypted (`.enc`, the message dbs) vs plaintext (the media tars).
    """
    accounts = []
    for acct, b in _backup_dirs():
        files = []
        for name in (*_MEDIA_TARS, "ChatStorage.sqlite.enc", "Backup.plist"):
            p = os.path.join(b, name)
            if not os.path.exists(p):
                continue
            st = os.stat(p)
            files.append({
                "file": name,
                "sizeBytes": st.st_size,
                "hydrated": not bool(st.st_flags & SF_DATALESS),
                "encrypted": name.endswith(".enc"),
            })
        accounts.append({"account": acct, "path": b, "files": files})
    return {"present": bool(accounts), "accounts": accounts}


@returns({"status": "string", "hydrated": "list"})
@timeout(600)
async def hydrate_backup(*, account=None, media=True, video=True, documents=False, gifs=False, **params):
    """Download the chosen iCloud backup archives to local disk (brctl).

    iCloud syncs the backup as dataless stubs; the media tars must be
    materialised before `import_backup_media` can read them. This shells out to
    macOS's own `brctl download` — no auth, the OS already holds the session.
    Big archives (Media/Video ≈ 4–5 GB each) can take minutes.

    Args:
        account: restrict to one account folder (phone number); default all.
        media/video/documents/gifs: which archives to pull.
    """
    dirs = _backup_dirs()
    if account:
        dirs = [(a, b) for a, b in dirs if a == account] or dirs
    if not dirs:
        return app_error(code="NoBackup",
                         message=f"No WhatsApp iCloud backup found under {ICLOUD_WA_ACCOUNTS}")
    wanted = [n for n, on in (("Media.tar", media), ("Video.tar", video),
                              ("Document.tar", documents), ("GIFs.tar", gifs)) if on]
    targets = [os.path.join(b, n) for _, b in dirs for n in wanted
               if os.path.exists(os.path.join(b, n))]
    if not targets:
        return app_error(code="NoArchives", message="None of the requested archives exist in the backup.")
    await shell.run("brctl", args=["download", *targets], timeout=580)
    hydrated = [{"file": t, "sizeBytes": os.path.getsize(t), "hydrated": not _is_dataless(t)}
                for t in targets]
    ok = all(h["hydrated"] for h in hydrated)
    return {"status": "ok" if ok else "partial", "hydrated": hydrated}


@returns("file[]")
@timeout(300)
async def import_backup_media(*, conversation, include_video=True, include_documents=False,
                              limit=None, account=None, **params):
    """Extract one conversation's media from the iCloud backup into the blob store.

    Resolves the conversation (Z_PK or fuzzy name) to its folder ids via the
    Desktop db, then pulls every matching file out of the plaintext media tars
    and lands it content-addressed in the engine blob store — returning `file`
    entities (sha-identified, deduped by bytes). No key, no WhatsApp session.

    Requires the archives to be hydrated first (`hydrate_backup`); raises
    `NeedsHydration` if a needed tar is still an iCloud stub.

    Args:
        conversation: Z_PK integer or fuzzy ZPARTNERNAME substring (e.g. "Alice").
        include_video / include_documents: also pull Video.tar / Document.tar.
        limit: cap the number of files imported.
        account: restrict to one backup account (phone number); default all.
    """
    cid = await _resolve_conversation_id(conversation)
    rows = await sql.query(
        "SELECT ZCONTACTJID AS jid, ZCONTACTIDENTIFIER AS lid, ZPARTNERNAME AS name "
        "FROM ZWACHATSESSION WHERE Z_PK = :id", db=DB_PATH, params={"id": cid})
    if not rows:
        return app_error(code="NotFound", message=f"No conversation {conversation!r}")
    ids = {v for v in (rows[0].get("jid"), rows[0].get("lid")) if v}
    if not ids:
        return app_error(code="NoIds", message=f"Conversation {conversation!r} has no resolvable folder id.")
    # Tar member paths are `/Media/<id>/...`, `/Video/<id>/...` — match the id token.
    needles = [f"/{i}/" for i in ids]

    dirs = _backup_dirs()
    if account:
        dirs = [(a, b) for a, b in dirs if a == account] or dirs
    tar_names = ["Media.tar"] + (["Video.tar"] if include_video else []) \
        + (["Document.tar"] if include_documents else [])

    files = []
    for _, b in dirs:
        for tn in tar_names:
            tp = os.path.join(b, tn)
            if not os.path.exists(tp):
                continue
            if _is_dataless(tp):
                return app_error(
                    code="NeedsHydration",
                    message=f"{tn} is still an iCloud stub — run hydrate_backup first.")
            with tarfile.open(tp, "r") as tf:
                for m in tf:
                    if not m.isfile() or not any(n in m.name for n in needles):
                        continue
                    payload = tf.extractfile(m)
                    if payload is None:
                        continue
                    ext = os.path.splitext(m.name)[1].lstrip(".").lower() or "bin"
                    stored = await blobs.put(base64.b64encode(payload.read()).decode(), ext=ext)
                    files.append({
                        "sha": stored["sha256"],
                        "filename": os.path.basename(m.name),
                        "path": stored["path"],
                        "size": stored["size"],
                        "mimeType": _mime_for_path(m.name) or "",
                        "format": ext.upper(),
                        "kind": "file",
                    })
                    if limit and len(files) >= int(limit):
                        return files
    return files
