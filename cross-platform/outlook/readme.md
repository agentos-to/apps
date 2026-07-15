---
id: outlook
name: Outlook
description: Live Outlook.com mail via a browser-driven session — read inbox and folders with full HTML bodies, mapped to the email shape
services:
  - browser_session
color: "#0A6ED1"
website: "https://outlook.live.com/"
product:
  name: Outlook
  website: https://outlook.com
  developer: Microsoft Corporation
---

# Outlook

Read Outlook.com mail through a live Outlook Web tab in the engine's HEADLESS
background profile. Ops run as JS payloads via the `browser_session` service —
the engine holds the CDP session; this app never sees the protocol. Like the
WhatsApp and Exa connectors, **the browser profile is the session**: nothing is
extracted, no cookie or token is stored. No window opens for a read — the mail
surfaces in the Mail app (CLAUDE.md rule 19), so headless is the right surface.

## Requirements

- A Chromium browser (Brave/Chrome/Edge) installed — the engine runs its own
  headless instance on the background profile (`~/.agentos/browsers-bg/`).
- Ops return `NeedsAuth` when signed out — run `login` to sign into the
  background profile (a one-time headed flip; see Login).

## How it works (the short version)

Outlook Web's `service.svc` transport authenticates on a **rotating
`MSAuth1.0` token header** (not a cookie), minted by OWA's own request layer —
a raw `fetch()` can't reproduce it and 401s. So, like WhatsApp calling its own
JS modules, this connector **calls OWA's own EWS operation functions**
(`findItem` / `getItem`), which go through OWA's authed pipeline. Auth is a
non-problem: OWA attaches the fresh token itself.

- **Identity + folder ids** come from OWA's Satchel store
  (`owaSessionStore`, `folderStore`) — read-only and stable.
- **Mail data** comes from `findItem` (ordered ids) + one batched `getItem`
  (full items with HTML bodies), mapped to the `email` shape key-for-key with
  Gmail so the Mail app renders both identically.

The full reverse-engineering method (grabbing `__webpack_require__` via a chunk
push, resolving operations by protocol-stable string markers, the request-shape
rules) is documented at the top of `outlook.py` and in the system doc
`reverse-engineering-runtime-internals`.

## Login

Outlook.com is a Microsoft-account sign-in (OAuth + MFA) that can't be driven
programmatically — the `login_window` kind of the login protocol. Canonical flow:

1. `outlook.login` opens a chromeless sign-in window on the engine's background
   profile (a headed flip of that profile) and returns an `auth_challenge`
   (`kind: "login_window"`, `continueWith: "check_session"`).
2. You sign in (Microsoft account + MFA) in that window.
3. Poll `outlook.check_session` until it returns `authenticated: true`.
4. `browser.login_window(close=true)` — flip the profile back to its headless
   daemon (the session already persisted).

The session then persists in the engine's background profile — the exact
profile every headless read uses — no re-auth across engine restarts.

## Common tasks

- **Inbox:** `list_emails` (default `mailbox: inbox`, most recent first)
- **Other folders:** `list_emails` with `mailbox`: `sent` · `drafts` ·
  `trash` · `spam` · `archive`
- **Paging:** `list_emails` with `offset` (0-based) + `limit`
- **One message:** `get_email` with `id` (an EWS ItemId from a list result),
  or a mail `url`

## Entity model

- **email** — `name` (subject), `content` (HTML body) + `content_mime`,
  `from` / `to` / `copied_to` / `bcc` (`account` shape, `platform: email`),
  `published`, `isUnread` / `isDraft` / `isInbox` / `isSent` / `isTrash` /
  `isSpam` / `isStarred`, `hasAttachments`, `messageId`, `conversationId`,
  `accountEmail` (the mailbox it arrived on). Same keys as gmail's mapping.

## Behavior notes

- First op after an engine restart is slower (~5-15s): browser attach + page
  load + OWA runtime init. Warm ops run in well under a second. This is
  **absorbed transparently** — the readiness gate polls OWA until the EWS
  operation functions actually resolve before running the op, so a cold or
  freshly-opened tab self-heals inside the one call. You never need to retry a
  cold op, and a slow first op is normal, not a failure.
- `BindingDrift` now means a **genuine** breaking OWA build (the operation
  functions never resolved within the readiness deadline), never a transient
  cold-tab race — so it is a real "re-derive the bindings" signal, not a retry-me.
- `list_emails` sorts by `ItemLastModifiedTime` server-side (the only sort
  field B2Service accepts) and then by received time client-side, so the
  result is newest-received first.
- `logout` performs the **real Microsoft-account sign-out** — it signs out the
  whole MSA session in your daily browser (Office, other MS tabs), not just
  Outlook, because the session is the shared browser profile.

## Internals (for maintainers)

If Outlook Web ships a breaking build and ops return `BindingDrift`, the module
ids shifted — re-derive by driving the live tab over CDP (Node ≥ 21 has a
global `WebSocket`; `Network.enable` + `Page.reload` + `Network.getResponseBody`
captures the exact working request shape to replay). The connector already
resolves OWA's operations by **protocol-stable string markers** (the EWS action
names), not module ids, so most redeploys need no change. See the
reverse-engineering header in `outlook.py`.
