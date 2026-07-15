---
id: facebook
name: Facebook
description: Facebook Stories (24h rings) via the logged-in facebook.com Relay store — list and hydrate for the Social app
services:
  - blobs
  - http
color: "#1877F2"
website: "https://www.facebook.com/"
product:
  name: Facebook
  website: https://www.facebook.com/
  developer: Meta Platforms, Inc.
---

# Facebook

Read **Facebook Stories** (the 24h tray on `facebook.com`) through a live
session in the engine-held browser. Ops run as JS in the page via
`browser_session` — they walk the **Relay store** (not the Messenger
worker). DMs stay in the `messenger` plugin; this app owns the Social
`feeds` / `get_post` surface.

See [`operations.md`](./operations.md) for the Relay map and why Stories
are page-Relay instead of MSYS.

## Requirements

- Engine browser with a logged-in Facebook session (`xs` + `c_user`)
- Home feed loaded at least once so the stories tray query lands in Relay

## Linking

Same Facebook login as Messenger (shared background profile):

1. `facebook.login` → headed `login_window` on the bg profile
2. Sign in (password re-confirm / 2FA)
3. Poll `facebook.check_session` until `authenticated: true`
4. `browser.login_window(close=true)`

## Common tasks

- **Stories tray:** `list_posts` — `post` rows with `postType: story`
  (brokered as `feeds` for Social)
- **One story + media:** `get_post` with the story card id — hydrates
  photo/video into the blob store

## Status

| Op | Status |
|---|---|
| `check_session` / `login` | ✅ same cookie contract as messenger (`xs` / `c_user`) |
| `list_posts` | ✅ proven live — Relay `unified_stories_buckets` (23 rings / 53 cards on 2026-07-13) |
| `get_post` | ✅ `response_body` (CDP) → blobs; http fallback |
| tray pagination force-refresh | 🚧 reads store as hydrated by the feed; Relay `execute` replay not wired |
| `logout` | 🚧 stub |

## IDs

- **story / post** — base64 card id (`UzpfSVND:…` → `S:_ISC:<n>`)
- **author / ring** — `story_bucket_owner` FBID (User/Page)
- **account** — viewer FBID (`c_user`)
