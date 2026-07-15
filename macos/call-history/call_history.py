"""Call History app — read the macOS call log (FaceTime + phone + group calls).

Reads ~/Library/Application Support/CallHistoryDB/CallHistory.storedata directly
(a Core Data SQLite store, read-only, needs Full Disk Access). Timestamps:
ZDATE is seconds since 2001-01-01 (Core Data epoch); add 978307200 for Unix time.

Primary table ZCALLRECORD carries both FaceTime and Telephony rows. Enum values
reverse-engineered against the live DB:

  ZCALLTYPE          1 = phone audio · 8 = FaceTime audio · 16 = FaceTime video
                     → video when (ZCALLTYPE & 16); voice otherwise
  ZSERVICE_PROVIDER  com.apple.FaceTime | com.apple.Telephony
  ZORIGINATED        1 = outgoing (I called), 0 = incoming
  ZANSWERED          set only on INCOMING rows; outgoing-answered calls keep
                     ZANSWERED=0 with ZDURATION>0. So status derives from both:
                     answered if (duration > 0 or ZANSWERED); else incoming→missed,
                     outgoing→declined.
  ZHANDLE_TYPE       2 = phone number · 3 = email / Apple ID
  ZCONVERSATIONID    16-byte UUID blob → exposed as an uppercase hex string

Group-call participants live in Z_2REMOTEPARTICIPANTHANDLES, a Core Data join
table (Z_2REMOTEPARTICIPANTCALLS → ZCALLRECORD.Z_PK,
Z_4REMOTEPARTICIPANTHANDLES → ZHANDLE.Z_PK).

Contact names are resolved from the macOS AddressBook the same way the iMessage
app does (glob every *.abcddb source, normalize phones to last-10-digits).
"""

from agentos import connection, returns, sql, test
from agentos.macos import contacts


connection(
    'db',
    sqlite='~/Library/Application Support/CallHistoryDB/CallHistory.storedata')


DB_PATH = "~/Library/Application Support/CallHistoryDB/CallHistory.storedata"

# ZSERVICE_PROVIDER → friendly service name (and the reverse, for filtering).
_SERVICE_LABEL = {
    "com.apple.FaceTime": "facetime",
    "com.apple.Telephony": "phone",
}
_SERVICE_PROVIDER = {v: k for k, v in _SERVICE_LABEL.items()}


# ==============================================================================
# Shape mapping
# ==============================================================================


def _status(duration, answered, is_incoming):
    """Derive call status from duration + ZANSWERED + direction.

    A connected call always has duration > 0. ZANSWERED only flips for incoming
    rows, so it's an extra (not the only) signal. Everything else is unanswered:
    they rang me → missed; I rang them → declined/no-answer.
    """
    if duration > 0 or answered:
        return "answered"
    return "missed" if is_incoming else "declined"


def _map_call(row, names=None):
    """Map a ZCALLRECORD row to a call event dict."""
    duration = row.get("duration") or 0
    is_incoming = not bool(row.get("originated"))
    answered = bool(row.get("answered"))
    is_video = bool((row.get("call_type") or 0) & 16)

    handle = row.get("address") or ""
    name = (contacts.resolve(handle, names) if names else None) or (row.get("name") or None)

    return {
        "id": row["id"],
        "kind": "video" if is_video else "voice",
        "status": _status(duration, answered, is_incoming),
        "start": row.get("start"),
        "durationSecs": round(duration),
        "durationMin": round(duration / 60, 3) if duration else None,
        "isIncoming": is_incoming,
        "by": handle if is_incoming else "me",
        "partnerHandle": handle or None,
        "partnerName": name,
        "service": _SERVICE_LABEL.get(row.get("service"), row.get("service")),
        "isJunk": bool(row.get("junk")) or bool(row.get("blocked")),
        "location": row.get("location") or None,
    }


# The columns every call query selects — kept in one place so list/get/summarize
# stay in lock-step. ZCONVERSATIONID is a UUID blob; hex() makes it addressable.
_CALL_COLUMNS = """
  c.Z_PK                                              AS id,
  datetime(c.ZDATE + 978307200, 'unixepoch')          AS start,
  c.ZDURATION                                         AS duration,
  c.ZORIGINATED                                       AS originated,
  c.ZANSWERED                                         AS answered,
  c.ZCALLTYPE                                         AS call_type,
  c.ZSERVICE_PROVIDER                                 AS service,
  c.ZADDRESS                                          AS address,
  c.ZNAME                                             AS name,
  c.ZUNIQUE_ID                                        AS unique_id,
  upper(hex(c.ZCONVERSATIONID))                       AS conversation_id,
  c.ZJUNKCONFIDENCE                                   AS junk,
  c.ZBLOCKEDBYEXTENSION                               AS blocked,
  c.ZISO_COUNTRY_CODE                                 AS country,
  c.ZLOCATION                                         AS location
"""


def _filters(since, until, conversation_id, service):
    """Build a (sql_fragment, params) pair for the shared call filters.

    since/until are ISO date/datetime bounds (inclusive) on ZDATE — compared
    against the computed ISO `start`, so SQLite's lexical ordering makes >=/<= work.
    """
    frag, params = "", {}
    if since:
        frag += " AND datetime(c.ZDATE + 978307200, 'unixepoch') >= :since"
        params["since"] = since
    if until:
        frag += " AND datetime(c.ZDATE + 978307200, 'unixepoch') <= :until"
        params["until"] = until
    if conversation_id:
        frag += " AND upper(hex(c.ZCONVERSATIONID)) = :cid"
        params["cid"] = str(conversation_id).upper()
    if service:
        provider = _SERVICE_PROVIDER.get(str(service).lower(), service)
        frag += " AND c.ZSERVICE_PROVIDER = :service"
        params["service"] = provider
    return frag, params


# ==============================================================================
# Call operations
# ==============================================================================


@test(params={"limit": 5})
@returns({"calls": "list"})
async def list_calls(*, limit=200, since=None, until=None, conversation_id=None,
                     service=None, **params):
    """List macOS call history as interval events, most recent first.

    Sources ZCALLRECORD (FaceTime + Telephony). Filters:
      since / until      — ISO date/datetime bounds (inclusive) on the call date.
      conversation_id    — hex UUID (from a call's conversationId) to scope a thread.
      service            — "facetime" | "phone".

    Each call event:
      id            — ZCALLRECORD Z_PK (use with get_call)
      kind          — "voice" | "video"
      status        — "answered" | "missed" | "declined"
      start         — ISO datetime of call start
      durationSecs  — call duration in whole seconds (0 for missed/declined)
      durationMin   — duration in minutes (3 dp), None when 0
      isIncoming    — True if they called me, False if I called them
      by            — "me" for outgoing; the partner handle for incoming
      partnerHandle — the remote handle (phone/email)
      partnerName   — resolved contact name (AddressBook), else the stored ZNAME
      service       — "facetime" | "phone"
      isJunk        — flagged junk or blocked by a call-directory extension
      location      — carrier/CNAM location string, if known
    """
    names = await contacts.load()
    frag, fparams = _filters(since, until, conversation_id, service)
    rows = await sql.query(f"""
        SELECT {_CALL_COLUMNS}
        FROM ZCALLRECORD c
        WHERE 1=1 {frag}
        ORDER BY c.ZDATE DESC
        LIMIT :limit
    """, db=DB_PATH, params={"limit": limit, **fparams})
    return {"calls": [_map_call(r, names) for r in rows]}


@returns({
    "id": "int", "kind": "string", "status": "string", "start": "string",
    "durationSecs": "int", "durationMin": "float", "isIncoming": "bool",
    "by": "string", "partnerHandle": "string", "partnerName": "string",
    "service": "string", "isJunk": "bool", "location": "string",
    "uniqueId": "string", "conversationId": "string",
    "countryCode": "string", "participants": "list",
})
async def get_call(*, id, **params):
    """Get one call by id (ZCALLRECORD Z_PK), including group participants.

    Adds, beyond the list_calls fields:
      uniqueId       — ZUNIQUE_ID (the call's stable string identifier)
      conversationId — hex UUID grouping calls in the same thread
      countryCode    — ISO country code of the remote handle
      participants   — for group calls, every remote handle from
                       Z_2REMOTEPARTICIPANTHANDLES → ZHANDLE, each
                       {handle, name, kind: "phone"|"email"}
    """
    rows = await sql.query(f"""
        SELECT {_CALL_COLUMNS}
        FROM ZCALLRECORD c
        WHERE c.Z_PK = :id
    """, db=DB_PATH, params={"id": int(id)})
    if not rows:
        return None

    names = await contacts.load()
    row = rows[0]
    call = _map_call(row, names)
    call["uniqueId"] = row.get("unique_id")
    call["conversationId"] = row.get("conversation_id")
    call["countryCode"] = row.get("country")

    # Group participants — the join table keys ZCALLRECORD.Z_PK to ZHANDLE.Z_PK.
    phandles = await sql.query("""
        SELECT h.ZVALUE AS handle, h.ZNORMALIZEDVALUE AS normalized, h.ZTYPE AS type
        FROM Z_2REMOTEPARTICIPANTHANDLES j
        JOIN ZHANDLE h ON j.Z_4REMOTEPARTICIPANTHANDLES = h.Z_PK
        WHERE j.Z_2REMOTEPARTICIPANTCALLS = :id
    """, db=DB_PATH, params={"id": int(id)})
    participants = []
    for p in phandles:
        h = p.get("handle") or p.get("normalized")
        participants.append({
            "handle": h,
            "name": contacts.resolve(h, names),
            "kind": "email" if p.get("type") == 3 else "phone",
        })
    call["participants"] = participants
    return call


@returns({
    "callCount": "int", "totalDurationSecs": "int",
    "firstCall": "string", "lastCall": "string",
    "byService": "dict", "missedCount": "int",
})
async def summarize_calls(*, handle=None, since=None, until=None, **params):
    """Relationship arc across calls: counts, total talk time, and bounds.

    handle      — optional remote handle (phone/email) to scope to one person;
                  matched format-insensitively (phones by last-10-digits).
    since/until — ISO date/datetime bounds (inclusive) on the call date.

    Returns:
      callCount         — total calls in scope
      totalDurationSecs — sum of ZDURATION (connected talk time)
      firstCall/lastCall— ISO datetimes of the oldest / newest call
      byService         — {"facetime": n, "phone": n}
      missedCount       — incoming calls with no duration and not answered
    """
    frag, fparams = _filters(since, until, None, None)

    # Handle scoping is format-insensitive for phones, so we can't push it into
    # SQL cleanly — fetch the in-window rows and filter in Python.
    rows = await sql.query(f"""
        SELECT {_CALL_COLUMNS}
        FROM ZCALLRECORD c
        WHERE 1=1 {frag}
        ORDER BY c.ZDATE ASC
    """, db=DB_PATH, params=fparams)

    if handle:
        want = contacts.handle_key(handle)
        rows = [r for r in rows if contacts.handle_key(r.get("address")) == want]

    total = 0.0
    by_service = {}
    missed = 0
    for r in rows:
        total += r.get("duration") or 0
        label = _SERVICE_LABEL.get(r.get("service"), r.get("service") or "unknown")
        by_service[label] = by_service.get(label, 0) + 1
        is_incoming = not bool(r.get("originated"))
        if _status(r.get("duration") or 0, bool(r.get("answered")), is_incoming) == "missed":
            missed += 1

    return {
        "callCount": len(rows),
        "totalDurationSecs": round(total),
        "firstCall": rows[0].get("start") if rows else None,
        "lastCall": rows[-1].get("start") if rows else None,
        "byService": by_service,
        "missedCount": missed,
    }


