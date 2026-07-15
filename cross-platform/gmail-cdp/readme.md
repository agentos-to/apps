---
id: gmail-cdp
name: Gmail (Live)
description: Live Gmail via a browser-driven session — read any signed-in Google account's mail by intercepting Gmail Web's own requests, mapped to the email shape. No OAuth, no Google Cloud project.
services:
  - browser_session
  - blobs
  - http
color: "#EA4335"
website: "https://mail.google.com/"
product:
  name: Gmail
  website: https://mail.google.com
  developer: Google LLC
---

# Gmail (Live)

Read Gmail through the live `mail.google.com` tab in your daily browser —
**every Google account you're already signed into**, no OAuth consent, no
Google Cloud project, no API key. Like the Outlook and WhatsApp connectors,
**the browser profile is the session**: nothing is extracted, no cookie or
token is stored.

> **Before extending this app** (dev only), read:
> 1. [Browser-Driven Connectors](browser-driven on the system volume)
> 2. [dev/requirements.md](./dev/requirements.md) — sync protocol, mutations, send ladder
> 3. [dev/gmail_re.md](./dev/gmail_re.md) — sealed-JS footholds (`window.__gre`)
>
> **Layout**
> ```
> gmail-cdp/
>   readme.md           # runtime contract
>   gmail_cdp.py        # tools + inline page helper (_LIB → window.__agmail)
>   dev/                # authoring only — never injected at runtime
>     requirements.md   # durable RE / ops notes
>     gmail_re.js       # RE helpers (window.__gre) — not production inject
>     gmail_re.md
>     probes/           # one-shot RE scratch (ex-.helpers)
> ```

This is the counterpart to the `gmail` (OAuth) plugin. That one needs a
provider holding Google OAuth tokens (Mimestream); this one needs only that
you're logged into Gmail in your browser — so it works for *anyone who just
installed AgentOS*.

## Requirements

- A Chromium browser (Brave/Chrome/Edge) set as your daily browser, signed
  into one or more Google accounts at `mail.google.com`.
- Ops return `NeedsAuth` when signed out — run `login` to sign in.

## How it works (the short version)

Gmail Web's data transport is its internal `/sync/u/N/i/{bv,fd,s}` API — a
positional-array protocol guarded by a per-account `X-Framework-Xsrf-Token`.
It is **not reproducible by a hand-built fetch** (reconstruction 400s/500s),
and Gmail is Closure/Wiz-compiled so there is no webpack registry to call its
functions the way the Outlook connector calls OWA's. So this connector does
what InboxSDK / Streak / Gmail.js all do: it lets **Gmail issue its own
request**, and **intercepts the response**.

- **Account map** — the `/mail/u/N/feed/atom` title carries the account email,
  so enumerating `N = 0,1,2,…` (a redirect to `/u/0/` terminates the scan)
  yields the `email → /u/N/` map. `N` in the path selects the account; the
  session is multiplexed, so any account is reachable.
- **Mail data** — the connector navigates the tab to a Gmail **search view**
  for the target account (`#search/<gmail query>`), which forces Gmail to fire
  a fresh `bv` request; an injected XHR hook captures the response, and the
  thread stubs (`[subject, snippet, dateMs, "thread-f:id", [messages]]`) map to
  the `email` shape key-for-key with `gmail.py` so the Mail app renders both
  identically.
- **Full body** — `get_email` fetches Gmail's own message-source endpoint
  (`?ik=<GM_ID_KEY>&view=om&th=<hex>`, a same-origin cookie GET) and parses the
  raw RFC822 it returns → the original sender HTML + every header. Not DOM
  scraping, not a forged POST.
- **Mutations** — `archive_email` / `trash_email` / `star_email` / `mark_read`
  (and the general `modify_email`) forge Gmail's own `/sync/i/s` **action 13**
  (modify labels) — the same POST the UI fires. gmonkey has no mutation verb, so
  we reuse Gmail's live per-session action headers (captured off its own sync
  traffic) + the thread's ids straight off the bv stub. Same-origin, authed by
  the real session. See `dev/requirements.md` §2.
- **Sending** — `send_email` drives Gmail's own in-page API, **gmonkey**
  (`GmailMainWindow.createNewCompose` → `GmailDraftMessage.setTo/setBody/send`),
  so the request rides Gmail's authed pipeline. (gmonkey — discovered via the RE
  toolkit's `apis()`/`methods()` — is Gmail's official in-page API for reads,
  writes, and live watch; see the `reverse-engineering-toolkit` system doc.)

The reverse-engineering method — the sync protocol, the stub layout, the
mutation action, the send lifecycle, and the dead ends (the clients6 frontend
API, hand-built list requests) — is documented in **`dev/requirements.md`**.

**Continuing Gmail RE (agents):** Gmail is Closure/Wiz — `registry()` is sealed.
Do **not** forge opaque settings tokens. Production Settings-UI path is fine;
to call sealed writers use the CDP Debugger verbs (`browser.breakpoint` with
`waitPause`+`triggerJs` or `urlRegex`+`line`, `browser.script_source`,
`browser.call` / `inspect`). Recipe + footholds: **`dev/gmail_re.md`** (start at
"Cracking sealed Gmail JS"), helpers in `dev/probes/gbreak`, ladder in
`commons/re/toolkit.md`. Never touch Joe's sacred modernist filter — only
`AOS-FILTER-*` throwaways.

## Accounts

Multiple Google accounts signed into one browser live at `/mail/u/0/`,
`/mail/u/1/`, … Pass `account: "you@gmail.com"` to target one; `list_accounts`
enumerates them. Search-driven reads mean the folder vocabulary is Gmail
query syntax under the hood (`in:inbox`, `is:unread`, `in:sent`, `label:X`).

## Login

Google sign-in (OAuth + MFA + account chooser) can't be driven
programmatically. Canonical flow:

1. `gmail-cdp.login` returns `NeedsAuth` with a `login_url`.
2. `browser.login_window(url="https://mail.google.com/")` opens the sign-in
   as a foreground tab in your daily browser.
3. You sign in (and can **Add account** for more Google accounts — the
   multi-account switcher is Google's own).
4. Poll `gmail-cdp.check_session` until it returns `authenticated: true`.

The session persists in the daily browser profile across engine restarts.

## Behavior notes

- First op after an engine restart is slower (browser attach + Gmail boot);
  warm ops are fast. The readiness gate absorbs a cold tab inside one call.
- Reading an account other than the one on screen **navigates the tab** to
  that account + a search view — it is a browser-driven connector, so it
  drives the real, visible tab (you can watch it work).
- Mutations and sends drive the one shared background tab, so they run
  **serially** — two in parallel collide and one fails.
- Tools today (live-verified against the browser session): reads
  (`list_emails` / `search_emails` / `get_email` / `get_attachment` /
  `list_accounts` / `list_labels` / `list_filters`), the account trio, label
  mutations (`archive` / `trash` / `untrash` / `star` / `mark_read` /
  `modify` / `create_filter` / `delete_filter` / `create_label` /
  `delete_label` + batch variants), compose
  (`send_email` / `forward_email` / `create_draft` / `save_draft` /
  `send_draft` / `delete_draft` / `list_drafts`), and **threaded
  `reply_email` / `reply_all_email`**.
- **Filters** — `list_filters` / `create_filter` / `delete_filter` drive
  `#settings/filters` (`__agmail.listFilters` / `createFilter` /
  `deleteFilter`). See `dev/requirements.md` §9. **Sealed-path RE** (call
  `_m.V1k.prototype.E0b` instead of UI): read **`dev/gmail_re.md`** § "Cracking
  sealed Gmail JS" — use `browser.breakpoint` `waitPause`/`urlRegex` +
  `dev/probes/gbreak`, not hand-forged `/sync/i/s`.
- **Labels** — `create_label` / `delete_label` drive `#settings/labels`
  (`__agmail.createLabel` / `deleteLabel`); user `id` is the display name
  (same as `list_labels`). See `dev/requirements.md` §10.
- **Unsubscribe** — `unsubscribe_email` parses List-Unsubscribe from
  `get_email`; default dry-run, `confirm=True` POSTs RFC 8058 one-click.
  See `dev/requirements.md` §11.
- **Send-as / vacation** — `list_send_as` (`#settings/accounts`),
  `get_vacation` / `set_vacation` (`#settings/general`). Restore vacation
  after any test mutate. See `dev/requirements.md` §12.
- **`send_email` is reliable** (fixed 2026-07-09). Root cause was compose
  "moles" accumulating, not the undo-send window. Every step gates on
  Gmail's OWN `/sync/i/s` traffic — clean → autosave → send 200 — under a
  reactive retry. Full mechanism: `dev/requirements.md` §3.
- **Attachments** — outbound via `File`+`DataTransfer` into
  `input[name=Filedata]` (`__agmail.composeSend`). Inbound via `view=om` with
  the **message** legacy hex (`list.messageHex` / bv `msg[55]`) — thread hex
  fails on many self-sents; see `dev/requirements.md` §8.
- **`reply_email` / `reply_all_email` are done** (2026-07-09). Open the
  conversation via a Gmail-accepted page-JS list-row click (hex /
  `thread-a:` / `#inbox/<permId>`), capture the message-view controller by
  wrapping `_m.jLn`, call clean `_m.EQn(ctrl, mode)`, then fill+send the
  gmonkey mole with the same autosave/send gates. Durable page helper:
  `window.__agmail.reply(...)`. See `dev/requirements.md` §4.
- **`forward_email` is done** — a standalone "Fwd:" rides gmonkey compose
  (shared `_compose_and_deliver`). `thread_id` accepted for OAuth signature
  parity only (not honored — forward is a new thread).
