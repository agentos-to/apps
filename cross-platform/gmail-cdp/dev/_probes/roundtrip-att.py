#!/usr/bin/env python3
"""Fresh attach round-trip: reset → send → search → get_email → get_attachment → byte match."""
import base64
import hashlib
import json
import pathlib
import subprocess
import sys
import time

MARKER = f"AOS-RT-{int(time.time())}"
CONTENT = f"roundtrip-bytes-{MARKER}\n".encode()
TMP = pathlib.Path("/tmp/aos-rt.txt")
TMP.write_bytes(CONTENT)


def call(tool, payload):
    r = subprocess.run(
        ["agentos", "call", tool, "--json", json.dumps(payload)],
        capture_output=True,
        text=True,
    )
    if not r.stdout.strip():
        print("EMPTY", r.stderr[:500], file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        print("BAD JSON", r.stdout[:500], file=sys.stderr)
        return None


def dig(o, pred):
    if isinstance(o, dict):
        if pred(o):
            return o
        for v in o.values():
            hit = dig(v, pred)
            if hit:
                return hit
    elif isinstance(o, list):
        for v in o:
            hit = dig(v, pred)
            if hit:
                return hit
    return None


print("marker", MARKER)

# Skip navigate if already healthy — only reset when moles > 0
chk0 = call(
    "services",
    {
        "op": "browser_session",
        "params": {
            "verb": "eval",
            "target": "mail.google.com",
            "mode": "background",
            "js": "(async()=>{try{const api=await new Promise(r=>gmonkey.load('2',r));"
            "return {moles:(api.getMainWindow().getOpenDraftMessages()||[]).length};}"
            "catch(e){return {err:String(e)};}})()",
        },
    },
)
moles = (chk0 or {}).get("moles", -1)
print("moles0", moles if chk0 else None, chk0 and {k: chk0[k] for k in chk0 if not str(k).startswith("_")})
if moles and moles > 0:
    call(
        "services",
        {
            "op": "browser_session",
            "params": {
                "verb": "navigate",
                "target": "mail.google.com",
                "mode": "background",
                "url": f"https://mail.google.com/mail/u/0/?aosc={int(time.time())}#inbox",
            },
        },
    )
    time.sleep(14)

# 3. send
send = call(
    "plugins",
    {
        "op": "run",
        "params": {
            "app": "gmail-cdp",
            "tool": "send_email",
            "params": {
                "to": ["efisio@gmail.com"],
                "subject": MARKER,
                "body": "<div>rt body</div>",
                "attachments": [
                    {
                        "filename": "rt.txt",
                        "mimeType": "text/plain",
                        "content": base64.b64encode(CONTENT).decode(),
                    }
                ],
            },
        },
    },
)
sobj = dig(send, lambda o: "error" in o or o.get("status") or o.get("code"))
print("send", {k: sobj.get(k) for k in ("status", "error", "code", "id", "name") if sobj and sobj.get(k)} if sobj else send)

if sobj and sobj.get("error"):
    sys.exit(2)

time.sleep(3)

# 4. search
search = call(
    "plugins",
    {
        "op": "run",
        "params": {
            "app": "gmail-cdp",
            "tool": "search_emails",
            "params": {"query": f"subject:{MARKER}", "limit": 3},
        },
    },
)
row = dig(
    search,
    lambda o: o.get("name") == MARKER or (isinstance(o.get("name"), str) and MARKER in o.get("name", "")),
)
print(
    "list",
    {
        k: row.get(k)
        for k in (
            "id",
            "name",
            "legacyThreadId",
            "messageHex",
            "hasAttachments",
            "url",
        )
        if row and row.get(k) is not None
    }
    if row
    else None,
)
if not row or not row.get("messageHex"):
    print("FAIL: no messageHex on list row")
    sys.exit(3)

# 5. get_email via thread-a id (forces resolve)
em = call(
    "plugins",
    {
        "op": "run",
        "params": {
            "app": "gmail-cdp",
            "tool": "get_email",
            "params": {"id": row["id"]},
        },
    },
)
email = dig(em, lambda o: o.get("attachments") is not None or o.get("error"))
print(
    "get_email",
    {
        k: email.get(k)
        for k in (
            "name",
            "messageHex",
            "legacyThreadId",
            "hasAttachments",
            "attachments",
            "error",
        )
        if email and email.get(k) is not None
    },
)
if not email or email.get("error") or not email.get("attachments"):
    sys.exit(4)

att_id = email["attachments"][0]["id"]
om = email.get("messageHex") or row["messageHex"]

# 6. get_attachment
att = call(
    "plugins",
    {
        "op": "run",
        "params": {
            "app": "gmail-cdp",
            "tool": "get_attachment",
            "params": {
                "id": om,
                "attachment_id": att_id,
                "filename": "rt.txt",
                "mime_type": "text/plain",
            },
        },
    },
)
aobj = dig(att, lambda o: o.get("path") or o.get("error") or o.get("sha"))
print(
    "get_attachment",
    {
        k: aobj.get(k)
        for k in ("path", "sha", "size", "filename", "error", "_omToken")
        if aobj and aobj.get(k) is not None
    },
)
if not aobj or not aobj.get("path"):
    sys.exit(5)

got = pathlib.Path(aobj["path"]).read_bytes()
print("bytes_match", got == CONTENT, "got", got, "sha", hashlib.sha256(got).hexdigest())
sys.exit(0 if got == CONTENT else 6)
