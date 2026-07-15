---
id: macos-contacts
name: macOS Contacts
description: Read the macOS / iCloud address book as person records, keyed by the durable vCard UID — read-only, no browser
services:
  - shell
color: "#A2845E"
website: "https://support.apple.com/guide/contacts/"
product:
  name: Contacts
  website: https://www.apple.com/macos/
  developer: Apple Inc.
---

# macOS Contacts

Reads the system address book (local + iCloud + any other Contacts account) and
returns each entry as a `person` record carrying its uniform `identities` set —
the second connector, alongside Google Contacts, that feeds the engine's
person-merge. Read-only; no login, no sync limit.

It reads through the **Contacts framework** (`CNContactStore`) via an embedded
Swift helper, not the AddressBook sqlite. That's deliberate — see IDs.

## Requirements

- **Contacts access (TCC), granted to AgentOS itself.** The Swift helper calls
  `CNContactStore.requestAccess`; because AgentOS is the macOS permission
  principal (stable self-signed identity + embedded `NSContactsUsageDescription`,
  and the daemon runs as its own responsible process — see `capabilities-overview`
  on the system volume), the **first** call draws a real *"AgentOS wants to access
  your Contacts"* dialog. Approve it and the grant shows as **AgentOS** under
  **System Settings → Privacy & Security → Contacts** and survives `./dev.sh
  build` (the grant pins to the signing identity, not the per-build hash). Until
  approved, every tool returns a `NeedsPermission` error naming the live
  authorization status. This is a one-time system grant, not a credential — no
  terminal identity is borrowed.

## IDs — the durable key is the vCard UID

A person's `id` here is the contact's **vCard UID**, obtained by serializing the
contact with `CNContactVCardSerialization` and reading its `UID:` line. This is
the *only* identifier that survives an iCloud sync **and** the engine's person
merge, so it's emitted as the `{platform:"icloud", id:<UID>}` identity.

Do **not** reach for the easy alternatives — both are device-local and change on
sync, so re-syncing would mint duplicates instead of finding the same person:

| Tempting key | Why it's wrong |
|---|---|
| `CNContact.identifier` | Device-local; reassigned on iCloud sync |
| AddressBook sqlite `ZUNIQUEID` | Device-local; also `…:ABPerson`-suffixed, not a real UID |

## Emitting identities — the connector's contract

This app is one of the connectors the [contacts-identity](https://agentos.to)
outcome is built around, so it owns the third guidance tier (after the static
validator at commit time and engine matching at runtime): **emit each identity
already canonical, and never dedup — the engine does that.**

- Return `identities: [{platform, id, handle?, account?}]`, one entry per
  identity known for the contact.
- `id` is the **stable match key** — emit it canonical: the vCard UID for
  `icloud`, lowercased address for `email`, E.164 for `phone`. We use the
  `agentos.identity` helpers (`normalize_email`, `normalize_phone`); a number
  that can't be parsed to E.164 (no country code, ambiguous) is **skipped**
  rather than emitted as a bad key. Pass `default_region` to disambiguate local
  numbers.
- Social profiles in the address book give only a **username** (a mutable
  handle), never the platform's stable id — so they ride as **handle-only**
  entries (`{platform, handle}`, no `id`). The engine never indexes a handle-only
  entry, which is exactly right: a bare @handle gets recycled and is not a
  reliable identity.
- We never dedup or merge — the agent upserts what we return via
  `data.create(person, …)` and the engine merges on any `(platform, id)`
  collision, returning a receipt that names the matched signal.

> **Gmail note:** unlike the Google Contacts app, this connector does **not**
> strip dots / `+tags` from `gmail.com` addresses — that rule needs Gmail-API
> knowledge this app doesn't have, so it emits the generic lowercased form. A
> Gmail address that appears in both books still collides on the rest of its
> identity set (phone, social, the same UID after one import).

## Tools

### `list_contacts(limit=200, account="icloud", default_region="US")`
Browse the book as `person[]`. Each record carries `id` (vCard UID), `name`,
name parts, and `identities`. `account` labels which book the `icloud` identity
belongs to (pass the real iCloud account email when known).

### `search_contacts(query, limit=50, account="icloud", default_region="US")`
Name search (given / family / nickname / organization) as `person[]`. This is
the on-demand path — "add Conor" → `search_contacts("Conor")` → `data.create`
the one you want; the engine dedups against anyone already on the graph.

## The import flow

Browse with `remember: false` (so the read leaves no trace), pick the person you
want, then upsert exactly that one:

```
apps.run(macos-contacts, search_contacts, {query:"Conor"}, remember:false)
  → person[] with already-canonical identities
data.create(person, {…, identities:[{platform:"icloud", id:<UID>}, …]})
  → engine merges on any identity collision, or creates fresh
```
