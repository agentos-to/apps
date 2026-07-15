"""Gmail app — all operations implemented via client.get()/client.post()/etc.

All public functions take **params. Auth token lives in
params["auth"]["access_token"], injected by the engine from OAuth resolution.
"""

import asyncio
import base64
import json
import re
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from agentos import connection, provides, returns, timeout, client, blobs, iso_from_ms

connection(
    'gmail',
    base_url='https://gmail.googleapis.com/gmail/v1/users/me',
    domain='gmail.googleapis.com',
    auth={'type': 'oauth', 'service': 'google', 'scopes': ['https://mail.google.com/']})


BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


# ==============================================================================
# Internal helpers
# ==============================================================================


def _auth_header(params):
    auth = params.get("auth", {})
    # Engine injects "bearer" (full header) and "access_token" (raw token)
    bearer = auth.get("bearer")
    if bearer:
        return {"Authorization": bearer}
    token = auth.get("access_token", "")
    return {"Authorization": f"Bearer {token}"}


def _get_header(headers, name):
    """Find a header by name (case-insensitive), return its value or None."""
    name_lower = name.lower()
    for h in headers or []:
        if h.get("name", "").lower() == name_lower:
            return h.get("value")
    return None


def _parse_addresses(header_val):
    """Parse 'Name <email>, Name2 <email2>' into list of {handle, display_name}.

    Splits on '>, ' first to avoid breaking names that contain commas.
    """
    if not header_val:
        return []
    results = []
    # Split on '>, ' keeping the '>' with the preceding segment
    parts = re.split(r">,\s*", header_val)
    for part in parts:
        part = part.strip().rstrip(">")
        if not part:
            continue
        if "<" in part:
            name_part, email_part = part.split("<", 1)
            email = email_part.rstrip(">").strip()
            display_name = name_part.strip().strip('"').strip("'").strip()
        elif "@" in part:
            email = part.strip()
            display_name = ""
        else:
            continue
        results.append(
            {
                "handle": email,
                "platform": "email",
                "displayName": display_name or None,
            }
        )
    return results


def _decode_body_text(payload):
    """Find the text/plain part and base64url-decode its content.

    Handles flat payloads (single part) and nested multipart structures.
    """
    if not payload:
        return ""

    def _find_plain(part):
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            data = (part.get("body") or {}).get("data", "")
            if data:
                # Add padding if needed
                padded = data + "=" * ((4 - len(data) % 4) % 4)
                try:
                    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                except Exception:
                    return ""
        if mime.startswith("multipart/"):
            for sub in part.get("parts") or []:
                result = _find_plain(sub)
                if result:
                    return result
        return ""

    # Try parts first (multipart), then the payload itself
    for sub in payload.get("parts") or []:
        result = _find_plain(sub)
        if result:
            return result
    return _find_plain(payload)


def _decode_body_html(payload):
    """Find the text/html part and base64url-decode its content."""
    if not payload:
        return ""

    def _find_html(part):
        mime = part.get("mimeType", "")
        if mime == "text/html":
            data = (part.get("body") or {}).get("data", "")
            if data:
                padded = data + "=" * ((4 - len(data) % 4) % 4)
                try:
                    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                except Exception:
                    return ""
        if mime.startswith("multipart/"):
            for sub in part.get("parts") or []:
                result = _find_html(sub)
                if result:
                    return result
        return ""

    for sub in payload.get("parts") or []:
        result = _find_html(sub)
        if result:
            return result
    return _find_html(payload)


def _extract_manage_subscription_url(html):
    """Extract manage subscription/preferences URL from HTML using lxml.

    Looks for links by href patterns and anchor text patterns,
    language-independent where possible.
    """
    if not html:
        return None
    try:
        from lxml import html as lxml_html
    except ImportError:
        return None

    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        return None

    # Strategy 1: href patterns — these are URL-based, language-independent
    href_patterns = [
        "manage_subscription", "manage-subscription",
        "manage_preferences", "manage-preferences",
        "subscription_preferences", "subscription-preferences",
        "email_preferences", "email-preferences",
        "communication_preferences", "communication-preferences",
        "notification_preferences", "notification-preferences",
        "update_preferences", "update-preferences",
        "mailing_preferences", "mailing-preferences",
        "list-manage.com/profile",     # Mailchimp
        "manage_subscription_preferences",  # Customer.io
        "email-preferences",           # HubSpot
        "subscription-center",         # Salesforce
        "preference-center",           # Generic
    ]
    for link in doc.cssselect("a[href]"):
        href = link.get("href", "")
        href_lower = href.lower()
        for pattern in href_patterns:
            if pattern in href_lower:
                return href

    # Strategy 2: anchor text (English fallback)
    text_patterns = [
        "manage preferences", "update preferences",
        "manage your preferences", "update your preferences",
        "email preferences", "subscription preferences",
        "communication preferences", "notification preferences",
        "manage subscription", "manage your subscription",
    ]
    for link in doc.cssselect("a[href]"):
        text = (link.text_content() or "").strip().lower()
        for pattern in text_patterns:
            if pattern in text:
                return link.get("href", "")

    return None


def _collect_attachments(payload):
    """Recursively collect attachment metadata as file-shaped objects."""
    results = []
    if not payload:
        return results

    def _mime_to_format(mime_type):
        """Derive human-readable format from MIME type."""
        formats = {
            "application/pdf": "PDF",
            "application/zip": "ZIP",
            "application/gzip": "GZIP",
            "text/plain": "TXT",
            "text/csv": "CSV",
            "text/html": "HTML",
            "image/png": "PNG",
            "image/jpeg": "JPEG",
            "image/gif": "GIF",
            "image/webp": "WebP",
        }
        if not mime_type:
            return None
        return formats.get(mime_type)

    def _walk(part):
        filename = part.get("filename")
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        if filename and attachment_id:
            mime_type = part.get("mimeType")
            results.append(
                {
                    "id": attachment_id,
                    "name": filename,
                    "filename": filename,
                    "mimeType": mime_type,
                    "format": _mime_to_format(mime_type),
                    "size": body.get("size"),
                    "encoding": "base64url",
                }
            )
        for sub in part.get("parts") or []:
            _walk(sub)

    _walk(payload)
    return results


def _attachment_ext(filename, mime_type):
    """Extension (no dot) naming a hydrated attachment in the blob store —
    the filename's own suffix first, else the MIME subtype, else 'bin'.
    (`blobs.put` sanitizes further; this just gives it a sensible hint.)"""
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1]
        if ext.isalnum() and len(ext) <= 8:
            return ext.lower()
    if mime_type and "/" in mime_type:
        sub = mime_type.split("/", 1)[1].split("+", 1)[0]
        if sub.isalnum():
            return sub.lower()
    return "bin"


# ==============================================================================
# Calendar payload (RFC 5545 VEVENT) — hand-rolled reader for the invite
# subset. Deterministic only: a sender who didn't embed text/calendar or an
# .ics attachment yields nothing here, ever (email-event-to-calendar outcome).
# ==============================================================================


_PARTSTAT_TO_STATUS = {
    "NEEDS-ACTION": "pending",
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "DELEGATED": "delegated",
}


def _strip_mailto(value):
    return re.sub(r"(?i)^mailto:", "", value or "").strip()


def _unfold_ical(text):
    """Unfold RFC 5545 line continuations — a line starting with a space
    or tab is a continuation of the previous line."""
    unfolded = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        elif line:
            unfolded.append(line)
    return unfolded


def _ical_unescape(value):
    """Unescape RFC 5545 TEXT value escaping (\\\\, \\; \\n)."""
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _parse_ical_line(line):
    """Split 'NAME;PARAM=VAL;...:VALUE' into (name, params_dict, value)."""
    name_part, _, value = line.partition(":")
    segments = name_part.split(";")
    params = {}
    for seg in segments[1:]:
        key, _, val = seg.partition("=")
        params[key.upper()] = val
    return segments[0].upper(), params, value


def _ical_datetime(value, params):
    """RFC 5545 DATE-TIME/DATE value -> (iso_string, timezone, all_day)."""
    if len(value) == 8:  # YYYYMMDD — all-day
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}", None, True
    date_part, time_part = value[:8], value[9:]
    iso = (f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}T"
           f"{time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}")
    if value.endswith("Z"):
        return iso, "UTC", False
    return iso, params.get("TZID"), False


def _parse_vevent(ical_text, self_email):
    """Read the invite subset of a VCALENDAR/VEVENT: identity, when/where,
    organizer, and — the whole point — the self attendee's PARTSTAT."""
    method = None
    vevent = {}
    attendees = []
    in_event = False
    for line in _unfold_ical(ical_text):
        name, params, value = _parse_ical_line(line)
        if name == "METHOD":
            method = value.upper()
        elif name == "BEGIN" and value == "VEVENT":
            in_event = True
        elif name == "END" and value == "VEVENT":
            break
        elif not in_event:
            continue
        elif name == "UID":
            vevent["uid"] = value
        elif name == "SUMMARY":
            vevent["summary"] = _ical_unescape(value)
        elif name == "DESCRIPTION":
            vevent["description"] = _ical_unescape(value)
        elif name == "LOCATION":
            vevent["location"] = _ical_unescape(value)
        elif name == "STATUS":
            vevent["status"] = value.lower()
        elif name == "DTSTART":
            vevent["start"], vevent["tz"], vevent["all_day"] = _ical_datetime(value, params)
        elif name == "DTEND":
            vevent["end"], _, _ = _ical_datetime(value, params)
        elif name == "ORGANIZER":
            vevent["organizer_email"] = _strip_mailto(value)
            vevent["organizer_name"] = params.get("CN")
        elif name == "ATTENDEE":
            attendees.append({"email": _strip_mailto(value), "partstat": params.get("PARTSTAT")})
        elif name == "X-GOOGLE-CONFERENCE":
            vevent["conference_url"] = value

    if "uid" not in vevent or "start" not in vevent:
        return None
    vevent["method"] = method
    self_attendee = next(
        (a for a in attendees if self_email and a["email"].lower() == self_email.lower()), None,
    )
    vevent["self_partstat"] = self_attendee["partstat"] if self_attendee else None
    return vevent


async def _parse_calendar_parts(payload, *, message_id, self_email, **params):
    """Find a text/calendar VEVENT — inline part or .ics attachment — and
    decode it into a `meeting` dict (+ `invitation` when METHOD:REQUEST).
    Returns None for markup-less mail — never a guessed event."""
    if not payload:
        return None

    candidates = []

    def _walk(part):
        mime = part.get("mimeType", "")
        filename = (part.get("filename") or "")
        if mime == "text/calendar" or filename.lower().endswith(".ics"):
            candidates.append(part.get("body") or {})
        for sub in part.get("parts") or []:
            _walk(sub)

    _walk(payload)

    ical_bytes = None
    for body in candidates:
        data = body.get("data")
        if not data:
            attachment_id = body.get("attachmentId")
            if attachment_id:
                attachment = await get_attachment(message_id=message_id, attachment_id=attachment_id, **params)
                data = (attachment or {}).get("content")
        if data:
            padded = data + "=" * ((4 - len(data) % 4) % 4)
            ical_bytes = base64.urlsafe_b64decode(padded)
            break

    if not ical_bytes:
        return None

    vevent = _parse_vevent(ical_bytes.decode("utf-8", errors="replace"), self_email)
    if not vevent:
        return None

    conference_url = vevent.get("conference_url")
    location = vevent.get("location")
    virtual_url = conference_url or (location if location and location.lower().startswith("http") else None)

    meeting = {
        "shape": "meeting",
        "id": vevent["uid"],
        "name": vevent.get("summary") or "(No title)",
        "content": vevent.get("description"),
        "startDate": vevent["start"],
        "endDate": vevent.get("end"),
        "timezone": vevent.get("tz"),
        "allDay": vevent.get("all_day", False),
        "status": vevent.get("status"),
        "icalUid": vevent["uid"],
        "isVirtual": bool(virtual_url),
        "meetingUrl": virtual_url,
        "email": self_email,  # the mailbox this arrived via — disambiguates which Google Calendar account "Add to Calendar" files under
        "organized_by": {
            "shape": "person",
            "name": vevent.get("organizer_name") or vevent.get("organizer_email"),
            "handle": vevent.get("organizer_email"),
        } if vevent.get("organizer_email") else None,
    }
    if location and not virtual_url:
        meeting["held_at"] = {"shape": "place", "name": location}

    if vevent.get("method") != "REQUEST":
        return meeting

    invitation = {
        "shape": "invitation",
        "id": vevent["uid"],
        "name": meeting["name"],
        "startDate": meeting["startDate"],
        "endDate": meeting.get("endDate"),
        "icalUid": vevent["uid"],
        "invitationType": "event",
        "email": self_email,
        "status": _PARTSTAT_TO_STATUS.get(vevent.get("self_partstat"), vevent.get("self_partstat")),
    }
    return [meeting, invitation]


def _extract_domain(email_addr):
    """Extract domain from an email address."""
    if not email_addr or "@" not in email_addr:
        return None
    return email_addr.rsplit("@", 1)[1].lower()


def _domains_from_accounts(accounts):
    """Extract unique domain objects from a list of parsed account dicts."""
    seen = set()
    domains = []
    for acct in accounts:
        domain = _extract_domain(acct.get("handle", ""))
        if domain and domain not in seen:
            seen.add(domain)
            domains.append({"name": domain})
    return domains


def _internaldate_to_iso(internal_date):
    """Convert Gmail internalDate (ms since epoch string) to UTC ISO 8601."""
    if not internal_date:
        return None
    try:
        return iso_from_ms(int(internal_date))
    except (ValueError, TypeError):
        return None


async def _map_email(msg, **params):
    """Map a Gmail message object to the agentOS email shape."""
    if not msg:
        return msg
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    label_ids = msg.get("labelIds") or []

    subject = _get_header(headers, "Subject") or "(no subject)"
    from_raw = _get_header(headers, "From") or ""

    # Parse From for display_name and email
    from_parsed = _parse_addresses(from_raw)
    from_obj = from_parsed[0] if from_parsed else {"handle": from_raw, "platform": "email", "displayName": None}

    # Author: display name before '<', or None
    if "<" in from_raw:
        author = from_raw.split("<")[0].strip().strip('"').strip("'").strip() or None
    else:
        author = None

    to_accounts = _parse_addresses(_get_header(headers, "To"))
    cc_accounts = _parse_addresses(_get_header(headers, "Cc"))
    bcc_accounts = _parse_addresses(_get_header(headers, "Bcc"))
    attachments = _collect_attachments(payload)

    # Extract List-Unsubscribe header (RFC 2369) — prefer URL over mailto
    # RFC 8058: List-Unsubscribe-Post enables one-click unsubscribe via POST
    unsubscribe = None
    unsubscribe_one_click = False
    unsub_raw = _get_header(headers, "List-Unsubscribe") or ""
    if unsub_raw:
        urls = re.findall(r"<([^>]+)>", unsub_raw)
        for url in urls:
            if url.startswith("http"):
                unsubscribe = url
                break
        if not unsubscribe and urls:
            unsubscribe = urls[0]
    if _get_header(headers, "List-Unsubscribe-Post") and unsubscribe and unsubscribe.startswith("http"):
        unsubscribe_one_click = True

    # Body — store the single richest representation so the engine renders
    # it through the MIME pipeline: the HTML part when present (text/html),
    # else the plain-text part (text/plain). The producer declares the type
    # (`content_mime`); the engine never guesses. (html-content outcome,
    # Principle 2 — "store the richest; derive the rest".)
    html_body = _decode_body_html(payload)
    if html_body:
        body, body_mime = html_body, "text/html"
    else:
        body, body_mime = _decode_body_text(payload), "text/plain"

    # List-Id (RFC 2919) — extract the identifier from angle brackets
    list_id_raw = _get_header(headers, "List-Id") or ""
    list_id_match = re.search(r"<([^>]+)>", list_id_raw)
    list_id = list_id_match.group(1) if list_id_match else (list_id_raw.strip() or None)

    # Auto-Submitted (RFC 3834) — anything other than "no" means automated
    auto_submitted = _get_header(headers, "Auto-Submitted")
    is_automated = auto_submitted is not None and auto_submitted.lower() != "no"

    # Manage subscription URL — look for List-Subscribe header first,
    # then scan body for common preference center patterns
    manage_sub = None
    list_subscribe_raw = _get_header(headers, "List-Subscribe") or ""
    if list_subscribe_raw:
        sub_urls = re.findall(r"<([^>]+)>", list_subscribe_raw)
        for url in sub_urls:
            if url.startswith("http"):
                manage_sub = url
                break
    if not manage_sub:
        # Parse HTML body with lxml — href patterns are language-independent
        body_html = _decode_body_html(payload)
        manage_sub = _extract_manage_subscription_url(body_html)

    # Calendar payload (email-event-to-calendar outcome) — deterministic
    # only: nothing here for markup-less mail, ever.
    calendar_child = await _parse_calendar_parts(
        payload,
        message_id=msg.get("id"),
        self_email=_get_header(headers, "Delivered-To") or params.get("account"),
        **params,
    )

    email = {
        "id": msg.get("id"),
        "name": subject,
        "content": body,
        "content_mime": body_mime,
        "author": author,
        "published": _internaldate_to_iso(msg.get("internalDate")),
        "isStarred": "STARRED" in label_ids,
        "isUnread": "UNREAD" in label_ids,
        # Folder membership (INBOX/TRASH/SPAM/SENT/DRAFT) is NOT stamped as a
        # flag — it's the read scope, recorded as `mailbox` mirror-list
        # membership. `labelIds` below keeps the raw labels as provenance.
        "isAutomated": is_automated,
        "hasAttachments": len(attachments) > 0,
        "messageId": _get_header(headers, "Message-ID") or "",
        "inReplyTo": _get_header(headers, "In-Reply-To"),
        "conversationId": msg.get("threadId", ""),
        "labelIds": label_ids,
        "sizeEstimate": msg.get("sizeEstimate"),
        "historyId": msg.get("historyId"),
        "references": _get_header(headers, "References"),
        "replyTo": _get_header(headers, "Reply-To"),
        "deliveredTo": _get_header(headers, "Delivered-To"),
        "returnPath": _get_header(headers, "Return-Path"),
        "listId": list_id,
        "precedence": _get_header(headers, "Precedence"),
        "mailer": _get_header(headers, "X-Mailer") or _get_header(headers, "User-Agent"),
        "authResults": _get_header(headers, "Authentication-Results"),
        "feedbackId": _get_header(headers, "Feedback-ID"),
        "unsubscribe": unsubscribe,
        "unsubscribeOneClick": unsubscribe_one_click,
        "manageSubscription": manage_sub,
        "attachments": attachments,
        # Relations
        "from": from_obj,
        "to": to_accounts,
        "copied_to": cc_accounts,
        "bcc": bcc_accounts,
        "domain": {"name": _extract_domain(from_obj.get("handle", ""))} if _extract_domain(from_obj.get("handle", "")) else None,
        "toDomain": _domains_from_accounts(to_accounts),
        "ccDomain": _domains_from_accounts(cc_accounts),
    }
    if calendar_child:
        # email —regards→ meeting (+ invitation when it's a real invite).
        # Not "references": email.yaml's `references` field is already the
        # RFC 2822 References header — same key would collide.
        email["regards"] = calendar_child
    return email


async def _map_conversation(thread, **params):
    """Map a Gmail thread object to the agentOS conversation shape."""
    if not thread:
        return thread
    raw_messages = thread.get("messages") or []
    snippet = thread.get("snippet", "")

    # Subject from first message
    name = None
    if raw_messages:
        first_headers = (raw_messages[0].get("payload") or {}).get("headers") or []
        name = _get_header(first_headers, "Subject")
    if not name:
        name = snippet[:120] + ("…" if len(snippet) > 120 else "")

    # published from last message
    date_published = None
    if raw_messages:
        date_published = _internaldate_to_iso(raw_messages[-1].get("internalDate"))

    # Map messages through _map_email and extract unique participants
    mapped_messages = (
        list(await asyncio.gather(*(_map_email(m, **params) for m in raw_messages)))
        if raw_messages else []
    )
    participants = _extract_participants(mapped_messages)

    # Unread if any message is unread
    unread_count = sum(1 for m in mapped_messages if m and m.get("is_unread"))

    return {
        "id": thread.get("id"),
        "name": name,
        "content": snippet,
        "published": date_published,
        "messageCount": len(mapped_messages) if mapped_messages else None,
        "unreadCount": unread_count,
        "historyId": thread.get("historyId"),
        # Relations
        "message": mapped_messages,
        "participant": participants,
    }


def _extract_participants(mapped_emails):
    """Extract unique participant accounts from a list of mapped emails."""
    seen = set()
    participants = []
    for email in mapped_emails:
        if not email:
            continue
        # Collect from, to, cc, bcc
        accounts = []
        from_obj = email.get("from")
        if from_obj:
            accounts.append(from_obj)
        accounts.extend(email.get("to") or [])
        accounts.extend(email.get("cc") or [])
        accounts.extend(email.get("bcc") or [])
        for acct in accounts:
            handle = acct.get("handle", "")
            if handle and handle not in seen:
                seen.add(handle)
                participants.append(acct)
    return participants


async def _resolve_attachments(attachments):
    """Resolve outbound attachment refs → [(filename, mime_type, raw_bytes)].

    Two ref shapes, one contract — because the web shell can't reach the blob
    store directly (only a worker can), a fresh local file rides up as bytes
    through the send verb, while a file already ON the graph rides as a path:

      - `{path}`     — a file already in the blob store (an inbound attachment
                       hydrated by `get_attachment`, or any graph file). We
                       read the bytes engine-side via `blobs.get` — no
                       re-upload, dedup for free.
      - `{content}`  — base64 bytes the compose UI just read off a picked/
                       dropped/pasted file (the shell has no blob-store reach,
                       so the bytes come through the verb, mirroring
                       `message_send_media`).

    A ref carrying neither is skipped, never guessed. `path` wins when both are
    present (the stored bytes are canonical)."""
    resolved = []
    for att in attachments or []:
        filename = att.get("filename") or att.get("name") or "attachment"
        mime_type = att.get("mimeType") or "application/octet-stream"
        path = att.get("path")
        content = att.get("content")
        if path:
            raw = base64.b64decode((await blobs.get(path=path))["data"])
        elif content:
            raw = base64.b64decode(content)
        else:
            continue
        resolved.append((filename, mime_type, raw))
    return resolved


async def _stage_attachments(attachments):
    """Like `_resolve_attachments`, but also PERSISTS every `content` (base64)
    ref into the blob store, returning both the byte tuples `_build_raw` needs
    AND the resulting path-refs.

    Only the DRAFT autosave path uses this. A compose window autosaves every
    ~1.5s; without staging it would re-ship the full base64 bytes over the
    shell→engine verb on every save. By storing the bytes once and echoing the
    `path`, the UI swaps its `content` ref for the `path` after the first save,
    so every later autosave carries a tiny path — the bytes never cross the
    wire twice. `path` refs pass through untouched (already stored)."""
    resolved = []  # [(filename, mime_type, raw_bytes)] for _build_raw
    refs = []      # [{filename, mimeType, path}] echoed back to the compose UI
    for att in attachments or []:
        filename = att.get("filename") or att.get("name") or "attachment"
        mime_type = att.get("mimeType") or "application/octet-stream"
        path = att.get("path")
        content = att.get("content")
        if path:
            raw = base64.b64decode((await blobs.get(path=path))["data"])
        elif content:
            raw = base64.b64decode(content)
            path = (await blobs.put(
                data=content, ext=_attachment_ext(filename, mime_type),
            ))["path"]
        else:
            continue
        resolved.append((filename, mime_type, raw))
        refs.append({"filename": filename, "mimeType": mime_type, "path": path})
    return resolved, refs


def _build_raw(to, subject, body_text, html_body=None, cc=None, bcc=None,
               in_reply_to=None, references=None, thread_id=None, attachments=None):
    """Build a base64url-encoded RFC 2822 message for the Gmail API 'raw' field.

    `attachments` is the already-resolved list from `_resolve_attachments`
    ([(filename, mime_type, raw_bytes)]). With attachments the message is a
    `multipart/mixed` whose first part is the body (itself a
    `multipart/alternative` when HTML is present); without, the body IS the
    message — byte-identical to the pre-attachment build."""
    if html_body:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body_text or "", "plain", "utf-8"))
        body_part.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        body_part = MIMEText(body_text or "", "plain", "utf-8")

    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(body_part)
        for filename, mime_type, raw in attachments:
            maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(raw)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    else:
        msg = body_part

    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    return raw


# ==============================================================================
# Read operations
# ==============================================================================


@returns("email[]")
@connection("gmail")
@timeout(60)
async def list_email_stubs(*, query="", limit=20, label_ids=None, page_token=None,
                           include_spam_trash=False, **params):
    """List email IDs/stubs only — no full message content."""
    headers = _auth_header(params)
    query_params = {"maxResults": str(limit)}
    if query:
        query_params["q"] = query
    if label_ids:
        query_params["labelIds"] = label_ids
    if include_spam_trash:
        query_params["includeSpamTrash"] = "true"
    if page_token:
        query_params["pageToken"] = page_token

    resp = await client.get(f"{BASE_URL}/messages", params=query_params, headers=headers)
    return resp["json"].get("messages", [])


@returns("email")
@provides("web_fetch", urls=["mail.google.com/*"])
@connection("gmail")
async def get_email(*, id=None, url=None, **params):
    """Get a specific email with full body content, headers, and attachment metadata."""
    # Extract ID from URL if provided
    if url and not id:
        # Fragment is after '#', then last path segment
        fragment = url.split("#")[-1] if "#" in url else url
        id = [seg for seg in fragment.split("/") if seg][-1]

    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/messages/{id}", params={"format": "full"}, headers=headers)
    email = await _map_email(resp["json"], **params)
    account = params.get("account")
    if account and email and not email.get("accountEmail"):
        email["accountEmail"] = account
    return email


def _stamp_account(emails, params):
    """Stamp accountEmail — the mailbox a message arrived on — from the
    account this call ran as. Delivered-To is a header (absent on lists,
    an alias on forwards); the authenticated account is the truth."""
    account = params.get("account")
    if not account:
        return emails
    for e in emails:
        if e and not e.get("accountEmail"):
            e["accountEmail"] = account
    return emails


# The mailbox service's folder vocabulary → Gmail search queries. Queries,
# not labelIds: a list-valued query param doesn't survive the HTTP client's
# serialization, and `archive` has no label anyway (it is All Mail minus
# every labeled place). Trash/spam additionally need includeSpamTrash —
# messages.list silently excludes both by default, whatever the query says.
_MAILBOX_QUERIES = {"inbox": "in:inbox", "sent": "in:sent", "drafts": "in:drafts",
                    "trash": "in:trash", "spam": "in:spam",
                    "archive": "-in:inbox -in:sent -in:drafts -in:trash -in:spam"}


def _gmail_date(v):
    """A date predicate value (ISO date or datetime) → Gmail's YYYY/MM/DD."""
    return str(v)[:10].replace("-", "/")


def _gmail_predicate(f):
    """One structured predicate `{key, op, value}` → a Gmail search operator.
    The provider-agnostic field vocabulary (searchQuery.ts) mapped to native
    Gmail syntax. Booleans carry value "true"/"false"; size values are bytes
    (Gmail accepts a raw byte count for larger:/smaller:)."""
    key, op = f.get("key"), f.get("op")
    v = str(f.get("value", "")).strip()
    if not key:
        return ""
    if key == "from":
        return f"from:({v})" if v else ""
    if key == "to":
        return f"to:({v})" if v else ""
    if key == "subject":
        return f"subject:({v})" if v else ""
    if key == "hasAttachment":
        return "has:attachment" if v.lower() == "true" else "-has:attachment"
    if key == "isUnread":
        return "is:unread" if v.lower() == "true" else "is:read"
    if key == "isStarred":
        return "is:starred" if v.lower() == "true" else "-is:starred"
    if key == "date":
        if not v:
            return ""
        return f"after:{_gmail_date(v)}" if op == "gte" else f"before:{_gmail_date(v)}"
    if key == "size":
        if not v:
            return ""
        return f"larger:{v}" if op == "gte" else f"smaller:{v}"
    return ""


def _build_search_query(search, mailbox):
    """Translate the provider-agnostic `search` object (free text + typed
    predicates + trash/spam inclusion) plus the folder `mailbox` into one
    Gmail `q=` string. Returns (query, include_spam_trash). This is the Gmail
    half of the two-way grammar — the graph mirror evaluates the SAME
    predicates via `filter_by_vals` when a provider is offline."""
    parts = []
    include_st = False
    include_trash = bool(search.get("includeTrash")) if search else False
    include_spam = bool(search.get("includeSpam")) if search else False

    if mailbox and mailbox != "all":
        folder_q = _MAILBOX_QUERIES.get(mailbox, "")
        if folder_q:
            parts.append(folder_q)
        if mailbox in ("trash", "spam"):
            include_st = True
    else:
        # Across folders — Gmail excludes trash+spam by default; honor toggles.
        if include_trash and include_spam:
            include_st = True
        elif include_trash:
            parts.append("-in:spam")
            include_st = True
        elif include_spam:
            parts.append("-in:trash")
            include_st = True

    if search:
        text = str(search.get("text") or "").strip()
        if text:
            parts.append(text)
        for f in search.get("filters", []) or []:
            frag = _gmail_predicate(f)
            if frag:
                parts.append(frag)
    return " ".join(parts).strip(), include_st


@returns("email[]")
@provides("mailbox", account_param="account", search=["text", "from", "to", "subject", "hasAttachment", "isUnread", "isStarred", "date", "size"])
@connection("gmail")
@timeout(120)
async def list_emails(*, query="", limit=20, label_ids=None, mailbox=None,
                      search=None, page_token=None, **params):
    """List emails with full content — fetches stubs then hydrates each via get_email.

    `mailbox` is the service-level folder filter (inbox · sent · drafts ·
    trash · spam · archive) — provider-agnostic vocabulary shared with the
    other mailbox providers. `search` is the structured mail-search object
    the mail UI sends (free text + typed predicates); both are translated to
    Gmail `q=` operators here. An explicit raw `query` (Gmail syntax) is
    ANDed on top for agent power-use; `label_ids` wins independently."""
    folder_q, include_spam_trash = _build_search_query(search, mailbox)
    q = " ".join(p for p in (str(query).strip(), folder_q) if p).strip()
    stubs = await list_email_stubs(query=q, limit=limit, label_ids=label_ids,
                                   include_spam_trash=include_spam_trash,
                                   page_token=page_token, **params)
    if not stubs:
        return []
    emails = await asyncio.gather(*(get_email(id=s["id"], **params) for s in stubs))
    return _stamp_account(list(emails), params)


@returns("email[]")
@connection("gmail")
@timeout(120)
async def search_emails(*, query, limit=20, **params):
    """Search emails with full content using Gmail query syntax."""
    stubs = await list_email_stubs(query=query, limit=limit, **params)
    if not stubs:
        return []
    emails = await asyncio.gather(*(get_email(id=s["id"], **params) for s in stubs))
    return _stamp_account(list(emails), params)


@returns("conversation[]")
@connection("gmail")
@timeout(60)
async def list_conversations(*, query="", label_ids=None, limit=20, page_token=None, **params):
    """List email threads with snippets."""
    headers = _auth_header(params)
    query_params = {"maxResults": str(limit)}
    if query:
        query_params["q"] = query
    if label_ids:
        query_params["labelIds"] = label_ids
    if page_token:
        query_params["pageToken"] = page_token

    resp = await client.get(f"{BASE_URL}/threads", params=query_params, headers=headers)
    threads = resp["json"].get("threads", [])
    # Threads from list API only have id/snippet/historyId — map what's available
    return list(await asyncio.gather(*(_map_conversation(t, **params) for t in threads)))


@returns("conversation")
@connection("gmail")
async def get_conversation(*, id, **params):
    """Get a full email thread with all messages, headers, and body content."""
    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/threads/{id}", params={"format": "full"}, headers=headers)
    return await _map_conversation(resp["json"], **params)


@returns({"emailAddress": "string", "messagesTotal": "integer", "threadsTotal": "integer", "historyId": "string"})
@connection("gmail")
@timeout(15)
async def get_profile(**params):
    """Get Gmail account profile (email address, message count, history ID)."""
    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/profile", headers=headers)
    return resp["json"]


def _map_label(label):
    """Map a Gmail label to the tag shape."""
    return {
        "id": label.get("id"),
        "name": label.get("name"),
        "tagType": label.get("type", "").lower() or None,  # system, user
        "color": (label.get("color") or {}).get("backgroundColor"),
    }


@returns("tag[]")
@connection("gmail")
@timeout(15)
async def list_labels(**params):
    """List all Gmail labels (system and user-created) as tags."""
    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/labels", headers=headers)
    return [_map_label(l) for l in resp["json"].get("labels", [])]


@returns("file")
@connection("gmail")
async def get_attachment(*, message_id, attachment_id, filename=None, mime_type=None, **params):
    """Download an email attachment and hydrate it into the blob store.

    Gmail serves attachment bytes base64url-encoded; we decode and hand them
    to `blobs.put`, so the attachment lands in the content-addressed store
    like any other file. The returned `file` carries its blob `path` — the one
    byte home every consumer shares: the reading pane saves it to disk, and a
    reply re-attaches it (`_resolve_attachments` reads the same path). Pass
    `filename`/`mime_type` (the caller has them from the email's attachment
    metadata) so the stored blob and the returned file are honestly typed."""
    headers = _auth_header(params)
    resp = await client.get(
        f"{BASE_URL}/messages/{message_id}/attachments/{attachment_id}", headers=headers,
    )
    b64url = resp["json"].get("data", "")
    raw = base64.urlsafe_b64decode(b64url + "=" * (-len(b64url) % 4))
    blob = await blobs.put(
        data=base64.b64encode(raw).decode(),
        ext=_attachment_ext(filename, mime_type),
    )
    return {
        "id": attachment_id,
        "name": filename or attachment_id,
        "filename": filename,
        "mimeType": mime_type,
        "path": blob["path"],
        "sha": blob["sha256"],
        "size": blob.get("size") or len(raw),
    }


@returns({"raw": "string", "id": "string", "threadId": "string"})
@connection("gmail")
async def get_raw(*, id, **params):
    """Get the full RFC 2822 raw source of an email (base64url-encoded)."""
    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/messages/{id}", params={"format": "raw"}, headers=headers)
    return resp["json"]


@returns({"history": "array", "historyId": "string", "nextPageToken": "string"})
@connection("gmail")
async def get_history(*, start_history_id, label_id=None, history_types=None,
                limit=100, page_token=None, **params):
    """Get incremental changes since a history ID."""
    headers = _auth_header(params)
    query_params = {"startHistoryId": str(start_history_id), "maxResults": str(limit)}
    if label_id:
        query_params["labelId"] = label_id
    if history_types:
        query_params["historyTypes"] = history_types
    if page_token:
        query_params["pageToken"] = page_token

    resp = await client.get(f"{BASE_URL}/history", params=query_params, headers=headers)
    return resp["json"]


@returns({"enableAutoReply": "boolean", "responseSubject": "string", "responseBodyPlainText": "string"})
@connection("gmail")
@timeout(15)
async def get_vacation(**params):
    """Get vacation/auto-reply settings."""
    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/settings/vacation", headers=headers)
    return resp["json"]


@returns("email[]")
@connection("gmail")
async def list_drafts(*, query="", limit=20, page_token=None, **params):
    """List drafts with full email content — fetches stubs then hydrates each via get_draft."""
    headers = _auth_header(params)
    query_params = {"maxResults": str(limit)}
    if query:
        query_params["q"] = query
    if page_token:
        query_params["pageToken"] = page_token

    resp = await client.get(f"{BASE_URL}/drafts", params=query_params, headers=headers)
    stubs = resp["json"].get("drafts", [])
    if not stubs:
        return []
    return [await get_draft(id=s["id"], **params) for s in stubs]


@returns("email")
@connection("gmail")
async def get_draft(*, id, **params):
    """Get a draft with full message content, mapped to the email shape."""
    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/drafts/{id}", params={"format": "full"}, headers=headers)
    draft = resp["json"]
    email = await _map_email(draft.get("message", {}), **params)
    if email:
        email["draftId"] = draft.get("id")
    return email


@returns({"id": "string", "criteria": "string", "action": "string"})
@connection("gmail")
@timeout(15)
async def list_filters(**params):
    """List all server-side email filters/rules."""
    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/settings/filters", headers=headers)
    return resp["json"].get("filter", [])


@returns({"sendAsEmail": "string", "displayName": "string", "isDefault": "boolean", "isPrimary": "boolean"})
@connection("gmail")
@timeout(15)
async def list_send_as(**params):
    """List send-as aliases (email addresses you can send from)."""
    headers = _auth_header(params)
    resp = await client.get(f"{BASE_URL}/settings/sendAs", headers=headers)
    return resp["json"].get("sendAs", [])


# ==============================================================================
# Unsubscribe (RFC 8058 one-click)
# ==============================================================================


@returns({"status": "string", "threadId": "string", "messageId": "string"})
@connection("gmail")
async def unsubscribe_email(*, id, **params):
    """Unsubscribe from a mailing list using RFC 8058 one-click.

    Fetches the email, checks for List-Unsubscribe + List-Unsubscribe-Post
    headers, and fires the POST. No browser or cookies needed.
    """
    email = await get_email(id=id, **params)
    if not email:
        raise ValueError("Email not found")

    unsub_url = email.get("unsubscribe")
    one_click = email.get("unsubscribe_one_click")

    if not unsub_url:
        raise ValueError(
            f"No List-Unsubscribe header on this email (from: {email.get('author') or email.get('from', {}).get('handle')}). "
            f"Manual unsubscribe may be required — check the email body for a link."
        )

    if not one_click:
        return {
            "status": "manual_required",
            "unsubscribeUrl": unsub_url,
            "message": "This sender doesn't support one-click unsubscribe. Open this URL in a browser to unsubscribe.",
        }

    # RFC 8058: POST with form data List-Unsubscribe=One-Click
    resp = await client.post(unsub_url, data={"List-Unsubscribe": "One-Click"})
    status_code = resp.get("status", 0)

    return {
        "status": "unsubscribed" if 200 <= status_code < 300 else "failed",
        "statusCode": status_code,
        "from": email.get("from", {}).get("handle"),
        "domain": (email.get("domain") or {}).get("name"),
        "subject": email.get("name"),
        "threadId": email.get("conversation_id"),
        "messageId": email.get("message_id"),
    }


# ==============================================================================
# Write operations
# ==============================================================================


@returns("email")
@provides("email_send", account_param="account")
@connection("gmail")
async def send_email(*, to, subject, body, html_body=None, cc=None, bcc=None,
                     attachments=None, **params):
    """Send a new email (plain text or HTML)."""
    raw = _build_raw(to, subject, body, html_body=html_body, cc=cc, bcc=bcc,
                     attachments=await _resolve_attachments(attachments))
    headers = _auth_header(params)
    resp = await client.post(
        f"{BASE_URL}/messages/send",
        json={"raw": raw}, headers=headers,
    )
    return _stamp_account([await _map_email(resp["json"], **params)], params)[0]


@returns("email")
@provides("email_reply", account_param="account")
@connection("gmail")
async def reply_email(*, to, in_reply_to, subject, body, thread_id=None, html_body=None,
                cc=None, bcc=None, references=None, attachments=None, **params):
    """Reply to an email (stays in the same thread).

    `thread_id` is Gmail's own thread key — pass it when the original was
    read via Gmail. A reply to mail read elsewhere (Mimestream, another
    provider) has no Gmail threadId; the RFC 2822 In-Reply-To/References
    headers carry the threading on their own."""
    raw = _build_raw(
        to, subject, body,
        html_body=html_body, cc=cc, bcc=bcc,
        in_reply_to=in_reply_to, references=references,
        attachments=await _resolve_attachments(attachments),
    )
    headers = _auth_header(params)
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    resp = await client.post(
        f"{BASE_URL}/messages/send",
        json=payload, headers=headers,
    )
    return _stamp_account([await _map_email(resp["json"], **params)], params)[0]


@returns("email")
@connection("gmail")
async def forward_email(*, to, subject, body, html_body=None, cc=None, bcc=None,
                  thread_id=None, attachments=None, **params):
    """Forward an email."""
    raw = _build_raw(to, subject, body, html_body=html_body, cc=cc, bcc=bcc,
                     attachments=await _resolve_attachments(attachments))
    headers = _auth_header(params)
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    resp = await client.post(f"{BASE_URL}/messages/send", json=payload, headers=headers)
    return await _map_email(resp["json"], **params)


@returns("email")
@connection("gmail")
async def modify_email(*, id, add_labels=None, remove_labels=None, **params):
    """Modify email labels — mark read/unread, star/unstar, archive, move to spam."""
    headers = _auth_header(params)
    body = {
        "addLabelIds": add_labels or [],
        "removeLabelIds": remove_labels or [],
    }
    resp = await client.post(f"{BASE_URL}/messages/{id}/modify", json=body, headers=headers)
    return await _map_email(resp["json"], **params)


@returns("email")
@provides("email_archive", account_param="account")
@connection("gmail")
async def archive_email(*, id, **params):
    """Archive an email — drop it from the inbox; it keeps living in All Mail."""
    headers = _auth_header(params)
    body = {"addLabelIds": [], "removeLabelIds": ["INBOX"]}
    resp = await client.post(f"{BASE_URL}/messages/{id}/modify", json=body, headers=headers)
    return _stamp_account([await _map_email(resp["json"], **params)], params)[0]


@returns("email")
@provides("email_trash", account_param="account")
@connection("gmail")
async def trash_email(*, id, **params):
    """Move an email to trash."""
    headers = _auth_header(params)
    resp = await client.post(f"{BASE_URL}/messages/{id}/trash", headers=headers)
    return _stamp_account([await _map_email(resp["json"], **params)], params)[0]


@returns("email")
@connection("gmail")
async def untrash_email(*, id, **params):
    """Remove an email from trash."""
    headers = _auth_header(params)
    resp = await client.post(f"{BASE_URL}/messages/{id}/untrash", headers=headers)
    return await _map_email(resp["json"], **params)


@returns({"ok": "boolean"})
@connection("gmail")
async def batch_modify_email(*, ids, add_labels=None, remove_labels=None, **params):
    """Modify labels on multiple emails at once (max 1000 IDs)."""
    headers = _auth_header(params)
    body = {
        "ids": ids,
        "addLabelIds": add_labels or [],
        "removeLabelIds": remove_labels or [],
    }
    resp = await client.post(f"{BASE_URL}/messages/batchModify", json=body, headers=headers)
    # 204 No Content on success
    return {}


@returns({"ok": "boolean"})
@connection("gmail")
async def batch_delete_email(*, ids, **params):
    """Permanently delete multiple emails (max 1000 IDs). CANNOT BE UNDONE."""
    headers = _auth_header(params)
    resp = await client.post(
        f"{BASE_URL}/messages/batchDelete",
        json={"ids": ids}, headers=headers,
    )
    return {}


@returns("email")
@connection("gmail")
async def create_draft(*, to, subject, body, html_body=None, cc=None, bcc=None,
                 thread_id=None, attachments=None, **params):
    """Create a new draft email."""
    raw = _build_raw(to, subject, body, html_body=html_body, cc=cc, bcc=bcc,
                     attachments=await _resolve_attachments(attachments))
    headers = _auth_header(params)
    message = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    resp = await client.post(
        f"{BASE_URL}/drafts",
        json={"message": message}, headers=headers,
    )
    draft = resp["json"]
    email = await _map_email(draft.get("message", {}), **params)
    if email:
        email["draftId"] = draft.get("id")
    return email


@returns("email")
@connection("gmail")
async def update_draft(*, id, to, subject, body, html_body=None, cc=None, bcc=None,
                       attachments=None, **params):
    """Update an existing draft."""
    raw = _build_raw(to, subject, body, html_body=html_body, cc=cc, bcc=bcc,
                     attachments=await _resolve_attachments(attachments))
    headers = _auth_header(params)
    resp = await client.put(
        f"{BASE_URL}/drafts/{id}",
        json={"message": {"raw": raw}}, headers=headers,
    )
    draft = resp["json"]
    email = await _map_email(draft.get("message", {}), **params)
    if email:
        email["draftId"] = draft.get("id")
    return email


@returns("email")
@provides("email_draft", account_param="account")
@connection("gmail")
async def save_draft(*, to="", subject="", body="", html_body=None, cc=None, bcc=None,
                     draft_id=None, in_reply_to=None, thread_id=None, attachments=None, **params):
    """Save a draft to the account's Drafts folder — the brokered `email_draft`.

    Create when `draft_id` is absent, UPDATE in place when present: a compose
    window's autosave calls this repeatedly with the id it got back, so one
    compose session is ONE draft, never a pile of duplicates. Carries HTML
    (`html_body`) and reply threading (`in_reply_to`) into the raw MIME.
    Returns the `email` with its provider `draftId` stamped, plus the staged
    `attachments` (path-refs): the autosave persists any picked bytes ONCE and
    echoes the paths back, so the compose UI swaps `content`→`path` and later
    saves never re-ship the bytes."""
    resolved, refs = await _stage_attachments(attachments)
    raw = _build_raw(to, subject, body, html_body=html_body, cc=cc, bcc=bcc,
                     in_reply_to=in_reply_to, references=in_reply_to,
                     attachments=resolved)
    headers = _auth_header(params)
    if draft_id:
        resp = await client.put(
            f"{BASE_URL}/drafts/{draft_id}",
            json={"message": {"raw": raw}}, headers=headers,
        )
    else:
        message = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        resp = await client.post(
            f"{BASE_URL}/drafts",
            json={"message": message}, headers=headers,
        )
    draft = resp["json"]
    email = await _map_email(draft.get("message", {}), **params)
    if email:
        email["draftId"] = draft.get("id")
        if refs:
            email["attachments"] = refs
    return email


@returns("email")
@connection("gmail")
async def send_draft(*, id, **params):
    """Send an existing draft."""
    headers = _auth_header(params)
    resp = await client.post(
        f"{BASE_URL}/drafts/send",
        json={"id": id}, headers=headers,
    )
    return await _map_email(resp["json"], **params)


@returns({"status": "string"})
@connection("gmail")
async def delete_draft(*, id, **params):
    """Permanently delete a draft."""
    headers = _auth_header(params)
    resp = await client.delete(f"{BASE_URL}/drafts/{id}", headers=headers)
    return {"status": "deleted"}


@returns({"enableAutoReply": "boolean", "responseSubject": "string"})
@connection("gmail")
@timeout(15)
async def set_vacation(*, enabled, subject=None, body=None, html_body=None,
                 contacts_only=False, domain_only=False,
                 start_time=None, end_time=None, **params):
    """Set or disable vacation/auto-reply."""
    headers = _auth_header(params)
    payload = {
        "enableAutoReply": enabled,
        "responseSubject": subject or "",
        "responseBodyPlainText": body or "",
        "responseBodyHtml": html_body or "",
        "restrictToContacts": contacts_only or False,
        "restrictToDomain": domain_only or False,
    }
    if start_time is not None:
        payload["startTime"] = start_time
    if end_time is not None:
        payload["endTime"] = end_time

    resp = await client.put(f"{BASE_URL}/settings/vacation", json=payload, headers=headers)
    return resp["json"]


@returns("tag")
@connection("gmail")
@timeout(15)
async def create_label(*, name, show_in_label_list=None, show_in_message_list=None, **params):
    """Create a new Gmail label, returned as a tag."""
    headers = _auth_header(params)
    payload = {
        "name": name,
        "labelListVisibility": show_in_label_list or "labelShow",
        "messageListVisibility": show_in_message_list or "show",
    }
    resp = await client.post(f"{BASE_URL}/labels", json=payload, headers=headers)
    return _map_label(resp["json"])


@returns("tag")
@connection("gmail")
@timeout(15)
async def update_label(*, id, name=None, show_in_label_list=None, show_in_message_list=None, **params):
    """Update a Gmail label (name, visibility)."""
    headers = _auth_header(params)
    payload = {}
    if name is not None:
        payload["name"] = name
    if show_in_label_list is not None:
        payload["labelListVisibility"] = show_in_label_list
    if show_in_message_list is not None:
        payload["messageListVisibility"] = show_in_message_list

    resp = await client.patch(f"{BASE_URL}/labels/{id}", json=payload, headers=headers)
    return _map_label(resp["json"])


@returns({"status": "string"})
@connection("gmail")
@timeout(15)
async def delete_label(*, id, **params):
    """Delete a Gmail label (does not delete emails, just removes the label)."""
    headers = _auth_header(params)
    resp = await client.delete(f"{BASE_URL}/labels/{id}", headers=headers)
    return {"status": "deleted"}


@returns({"id": "string"})
@connection("gmail")
@timeout(15)
async def create_filter(*, from_addr=None, to=None, subject=None, query=None,
                  has_attachment=False, add_labels=None, remove_labels=None,
                  forward_to=None, **params):
    """Create a server-side email filter/rule."""
    headers = _auth_header(params)
    criteria = {}
    if from_addr:
        criteria["from"] = from_addr
    if to:
        criteria["to"] = to
    if subject:
        criteria["subject"] = subject
    if query:
        criteria["query"] = query
    if has_attachment:
        criteria["hasAttachment"] = True

    action = {
        "addLabelIds": add_labels or [],
        "removeLabelIds": remove_labels or [],
    }
    if forward_to:
        action["forward"] = forward_to

    payload = {"criteria": criteria, "action": action}
    resp = await client.post(f"{BASE_URL}/settings/filters", json=payload, headers=headers)
    return resp["json"]


@returns({"status": "string"})
@connection("gmail")
@timeout(15)
async def delete_filter(*, id, **params):
    """Delete a server-side email filter/rule."""
    headers = _auth_header(params)
    resp = await client.delete(f"{BASE_URL}/settings/filters/{id}", headers=headers)
    return {"status": "deleted"}
