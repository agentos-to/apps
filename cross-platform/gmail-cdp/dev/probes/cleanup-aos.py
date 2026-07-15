#!/usr/bin/env python3
"""Trash AOS-* test mail; keep REPLYVERIFY-J1.

Uses gmail-cdp search + batch_delete (trash). Prints what was kept/trashed.
"""
import json
import subprocess
import sys

KEEP = ("REPLYVERIFY-J1",)
QUERY = "subject:(AOS-ATT OR AOS-VRFY OR AOS-RT OR AOS-PLAIN OR AOS-ATT2 OR REPLYVERIFY)"


def call(tool, payload):
    r = subprocess.run(
        ["agentos", "call", tool, "--json", json.dumps(payload)],
        capture_output=True,
        text=True,
    )
    if not r.stdout.strip():
        print("EMPTY stderr:", (r.stderr or "")[:400], file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        print("BAD:", (r.stdout or "")[:400], file=sys.stderr)
        return None


def walk_emails(o, acc=None):
    if acc is None:
        acc = []
    if isinstance(o, dict):
        tid = o.get("id") or ""
        name = o.get("name") or ""
        if isinstance(tid, str) and tid.startswith("thread") and name:
            acc.append(
                {
                    "id": tid,
                    "name": name,
                    "legacyThreadId": o.get("legacyThreadId"),
                    "messageHex": o.get("messageHex"),
                    "hasAttachments": o.get("hasAttachments"),
                }
            )
        for v in o.values():
            walk_emails(v, acc)
    elif isinstance(o, list):
        for v in o:
            walk_emails(v, acc)
    return acc


def main():
    print("searching…")
    d = call(
        "plugins",
        {
            "op": "run",
            "params": {
                "app": "gmail-cdp",
                "tool": "search_emails",
                "params": {"query": QUERY, "limit": 50},
            },
        },
    )
    rows = walk_emails(d or {})
    # dedupe by id
    seen = {}
    for r in rows:
        seen[r["id"]] = r
    rows = list(seen.values())
    print(f"found {len(rows)}")
    keep, trash = [], []
    for r in rows:
        if any(k in (r["name"] or "") for k in KEEP):
            keep.append(r)
        elif (r["name"] or "").startswith("AOS-") or "AOS-" in (r["name"] or ""):
            trash.append(r)
        elif any(x in (r["name"] or "") for x in ("Fwd: AOS-", "Re: AOS-")):
            trash.append(r)
        else:
            # REPLYVERIFY variants already handled; leave unknowns
            keep.append(r)

    print("--- KEEP ---")
    for r in keep:
        print(f"  {r['name'][:60]}  {r['id']}")
    print("--- TRASH ---")
    for r in trash:
        print(f"  {r['name'][:60]}  {r['id']}")

    if not trash:
        print("nothing to trash")
        return 0

    ids = [r["id"] for r in trash]
    print(f"batch_delete {len(ids)}…")
    out = call(
        "plugins",
        {
            "op": "run",
            "params": {
                "app": "gmail-cdp",
                "tool": "batch_delete_email",
                "params": {"ids": ids},
            },
        },
    )
    # summarize
    applied = 0
    if isinstance(out, dict):
        items = out.get("_items") or []
        if not items and "id" in out:
            items = [out]
        # dig list results
        def dig(o, acc):
            if isinstance(o, dict):
                if o.get("applied") or o.get("id"):
                    acc.append(o)
                for v in o.values():
                    dig(v, acc)
            elif isinstance(o, list):
                for v in o:
                    dig(v, acc)

        acc = []
        dig(out, acc)
        # unique by id
        by = {}
        for a in acc:
            if a.get("id"):
                by[a["id"]] = a
        applied = len(by)
        print(f"mutated ~{applied} threads")
        for tid, a in list(by.items())[:15]:
            err = a.get("error") or a.get("__error")
            print(f"  {tid}: {'ERR '+str(err)[:60] if err else 'ok'}")
    else:
        print("unexpected", type(out), str(out)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
