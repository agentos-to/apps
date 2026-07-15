---
id: icloud
name: iCloud
description: Browse iCloud as a mounted volume — the local CloudDocs mirror at ~/Library/Mobile Documents, including per-app backup containers. Files sync as metadata stubs (dataless) and stream their bytes on demand.
color: "#3693F3"
website: https://www.icloud.com/
---

# iCloud

Mounts **iCloud** as a browsable volume by serving the macOS CloudDocs
mirror — `~/Library/Mobile Documents/` — through the `volume_transport`
contract (`list_volumes` / `list_contents` / `read_node`). No Apple ID
auth, no web API: macOS's own `bird` daemon already synced the tree to
disk and handled the sign-in. This app just reads the filesystem and
presents it with friendly names.

## Why this seam (not pyicloud)

| seam | reach | auth | sees app containers? |
|---|---|---|---|
| pyicloud / iCloud web API | from anywhere | Apple ID + 2FA each session | ❌ Drive root only |
| **CloudDocs local mirror** (this app) | this Mac (signed in) | none — OS holds it | ✅ WhatsApp, every app |

For a local OS running on the user's own Mac, the CloudDocs mirror is
strictly better: zero credentials in our hands (security by
architecture), and it exposes the per-app backup containers the web API
can't.

## Dataless files

Every iCloud file syncs as a **dataless** stub: name, size, and dates
are local; the bytes live in iCloud until something opens the file (or
`brctl download` forces it). Listings report `dataless: true` on
un-materialized files — browsing is always cheap (metadata only), never
a multi-GB download. `read_node` stats without hydrating.

## Friendly names

The CloudDocs root holds ugly container ids
(`57T9237FN3~net~whatsapp~WhatsApp`, `com~apple~CloudDocs`). At the root,
entries are relabeled to the owning app (`WhatsApp`, `iCloud Drive`)
while their `id`/`path` stay the real on-disk path so navigation works.

## What's here

- **iCloud Drive** (`com~apple~CloudDocs`) — the user-facing Drive.
- **App containers** — per-app iCloud storage, including WhatsApp's chat
  backup (`Accounts/<phone>/backup/ChatStorage.sqlite.enc` + media tars).

## Mounting

The app `@provides("volume_transport")`, so the engine's boot reconcile
(`discover_transport_volumes`) calls `list_volumes`, lands one **iCloud**
volume in the System mount registry (`kind: filesystem`, `scope: system`
→ filed under **Drives**, beside Macintosh HD), and stamps this app as
its `provider`. Every browse dispatches back here by name.

## Limits

- Read-only by construction (the transport write verbs don't exist yet).
- macOS only — CloudDocs is an Apple path. Google Drive / OneDrive are
  future sibling transports, same contract.
</content>
</invoke>
