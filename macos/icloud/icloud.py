"""iCloud volume transport — serve the macOS CloudDocs mirror as a volume.

macOS's `bird` daemon syncs every iCloud file the user is entitled to
into `~/Library/Mobile Documents/` as a **dataless** stub (metadata
local, bytes-on-demand). This app reads that tree and serves it through
the fixed three-verb `volume_transport` contract:

    list_volumes()              → announce one "iCloud" volume (ANNOUNCE)
    list_contents(id, cursor?)  → typed child nodes + nextCursor (SERVE)
    read_node(id)               → one node's detail (STAT)

Node id == absolute path (the filesystem-transport convention). Read-only
by construction — the write verbs don't exist. Mirrors the filesystem
transport in `macos-control`, specialized for CloudDocs: it announces a
single volume rooted at Mobile Documents, relabels the root's container
ids to their owning app, and flags dataless files so browsing never
triggers a download.
"""

import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path

from agentos import provides, returns, test, timeout

# The CloudDocs mirror — iCloud's local front door. Every iCloud-backed
# app container and the user's Drive hang under here.
ICLOUD_ROOT = os.path.realpath(os.path.expanduser("~/Library/Mobile Documents"))

# macOS file flag (<sys/stat.h>): the bytes are not resident — the file
# is an iCloud stub. `st_flags & SF_DATALESS` is true until hydration.
SF_DATALESS = 0x40000000

# First-page size requested from providers (mirrors the transport default).
PAGE_SIZE = 500

# Container ids whose owning app deserves a hand-picked label; everything
# else falls back to the last reverse-DNS component (see `_friendly_name`).
KNOWN_CONTAINERS = {
    "com~apple~CloudDocs": "iCloud Drive",
}


def _stat_to_iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _mime_for(name):
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def _friendly_name(container_id):
    """Relabel a CloudDocs root container id to its owning app.

    `57T9237FN3~net~whatsapp~WhatsApp` → `WhatsApp`,
    `com~apple~CloudDocs`            → `iCloud Drive`,
    `iCloud~ca~illusive~openphopne`  → `openphopne`.
    The id/path stay the real on-disk name; only the label changes.
    """
    if container_id in KNOWN_CONTAINERS:
        return KNOWN_CONTAINERS[container_id]
    # Apple's container ids are `~`-joined reverse-DNS; the app name is
    # the final component (team-id / `iCloud` prefixes drop out for free).
    tail = container_id.split("~")[-1]
    return tail or container_id


def _within_root(path):
    """Confine this transport to the iCloud tree — a node id is an
    absolute path, and we only serve paths under Mobile Documents."""
    resolved = os.path.realpath(os.path.expanduser(path))
    if resolved != ICLOUD_ROOT and not resolved.startswith(ICLOUD_ROOT + os.sep):
        raise ValueError(f"Outside iCloud: {resolved}")
    return resolved


def _entry(path, name, st, *, at_root):
    """Build a shape-compatible child node from a path + its stat."""
    is_dir = os.path.isdir(path)
    # `.foo.icloud` placeholder stubs (fully-evicted files on older macOS)
    # present as their base name; dataless files keep their real name.
    display = name
    if not is_dir and name.startswith(".") and name.endswith(".icloud"):
        display = name[1:-len(".icloud")]
    if at_root and is_dir:
        display = _friendly_name(name)

    dataless = bool(getattr(st, "st_flags", 0) & SF_DATALESS)
    entry = {
        "id": path,
        "name": display,
        "shape": "list" if is_dir else "file",
        "listType": "folder" if is_dir else None,
        "ordering_mode": "unordered" if is_dir else None,
        "kind": "dir" if is_dir else "file",
        "path": path,
        "size": None if is_dir else st.st_size,
        "modified": _stat_to_iso(st.st_mtime),
        "dataless": dataless,
    }
    if not is_dir:
        entry["mimeType"] = _mime_for(display)
        ext = Path(display).suffix.lower()
        if ext:
            entry["format"] = ext.lstrip(".").upper()
    return entry


@test(params={})
@returns({"volumes": "{'type': 'array', 'description': 'Transport announce rows: name, kind, address'}", "count": "integer"})
@provides("volume_transport")
@timeout(15)
async def list_volumes(**_kwargs):
    """Announce iCloud as one mountable volume — the CloudDocs mirror.

    Deliberately NOT @returns("volume[]"): these are transport announce
    rows, and the engine's reconcile (`discover_transport_volumes`) is
    the only writer of `volume` nodes. `kind: filesystem` + the reconcile's
    `scope: system` file it under Drives, beside Macintosh HD.
    """
    present = os.path.isdir(ICLOUD_ROOT)
    return {
        "volumes": [
            {
                "name": "iCloud",
                "kind": "filesystem",
                "address": ICLOUD_ROOT,
                "readOnly": True,
                "removable": False,
                "totalBytes": None,
                "freeBytes": None,
            }
        ] if present else [],
        "count": 1 if present else 0,
    }


@test(params={})
@returns({"id": "string", "entries": "{'type': 'array', 'description': 'Typed child nodes (file/folder), folders first, name-sorted; dataless flagged'}", "count": "integer", "nextCursor": "string"})
@timeout(15)
async def list_contents(*, id=None, cursor=None, show_hidden=False, **_kwargs):
    """Serve the children of an iCloud node — the listing verb.

    Reads metadata only (`os.scandir` + `stat`), so listing a folder of
    dataless files never triggers a download.

    Args:
        id: Node id — the absolute path. Defaults to the iCloud root.
        cursor: Opaque pagination cursor from a prior nextCursor.
        show_hidden: Include dotfiles.
    """
    resolved = _within_root(id or ICLOUD_ROOT)
    if not os.path.isdir(resolved):
        raise ValueError(f"Not a directory: {resolved}")
    at_root = resolved == ICLOUD_ROOT

    entries = []
    with os.scandir(resolved) as scanner:
        for de in scanner:
            if not show_hidden and de.name.startswith(".") and not de.name.endswith(".icloud"):
                continue
            try:
                st = de.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append(_entry(de.path, de.name, st, at_root=at_root))

    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))

    offset = int(cursor) if cursor else 0
    page = entries[offset:offset + PAGE_SIZE]
    next_cursor = offset + PAGE_SIZE
    return {
        "id": resolved,
        "entries": page,
        "count": len(entries),
        "nextCursor": str(next_cursor) if next_cursor < len(entries) else None,
    }


@test(params={})
@returns({"id": "string", "name": "string", "path": "string", "kind": "string", "size": "integer", "modified": "string", "mimeType": "string", "dataless": "boolean", "readOnly": "boolean"})
@timeout(10)
async def read_node(*, id=None, **_kwargs):
    """Stat one iCloud node — the detail verb. Does NOT hydrate.

    Args:
        id: Node id — the absolute path.
    """
    resolved = _within_root(id or ICLOUD_ROOT)
    st = os.stat(resolved, follow_symlinks=False)
    parent = os.path.dirname(resolved)
    entry = _entry(resolved, os.path.basename(resolved), st, at_root=parent == ICLOUD_ROOT)
    entry["readOnly"] = True
    return entry
