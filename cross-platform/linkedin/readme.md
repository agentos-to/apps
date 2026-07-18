---
id: linkedin
name: LinkedIn
description: LinkedIn home feed (SDUI) + Messaging DMs via Voyager Messaging GraphQL — Feeds + Messaging apps
services:
  - blobs
  - http
color: "#0A66C2"
website: "https://www.linkedin.com/"
product:
  name: LinkedIn
  website: https://www.linkedin.com/
  developer: LinkedIn Corporation
---

# LinkedIn

Two surfaces through one logged-in engine browser session:

1. **Home feed** — flagship SDUI MainFeed (`data-testid="mainFeed"`) → Feeds app
2. **Messaging** — Voyager `voyagerMessagingGraphQL` + composer UI → Messaging app

See [`operations.md`](./operations.md) for the reverse-engineering map.

## Requirements

- Engine browser with a logged-in LinkedIn session (`li_at` + `JSESSIONID`)
- Feed: `/feed/` warm for SDUI MainFeed
- Messaging: `/messaging/` warm for GraphQL + composer

## Linking

1. `linkedin.login` → headed `login_window` on the **login profile**
2. Sign in (password / 2FA / challenge)
3. Plugin **detects** signed-in state (`li_at`, or feed/nav / left `/login`),
   **merges** cookies into the headless bg profile, and **closes** the
   window automatically — no leftover LinkedIn chrome
4. If the ~4 minute wait times out, finish sign-in and call `linkedin.login`
   again (or `check_session` after a manual merge/close)

## Common tasks

- **Home feed:** `list_posts` — durable posts (`surface: feed|all`); ads
  (`Promoted`) dropped in-plugin
- **One item + media:** `get_post` with share/activity/ugc URN
- **Comments (read):** `list_comments` — Voyager thread on the post’s **activity**
  (or `ugcPost`) URN; paged via `cursor` = next `start`
- **Inbox:** `list_conversations` — Messaging threads (`@provides("chats")`);
  `archived: true` for the Archive shelf
- **Thread:** `list_messages` with `conversation_id` = thread slug
- **Send:** `send_message` — composer UI (`to` = thread slug **or** profile
  `ACo…` / handle / `/in/…` for compose-to-profile)
- **New Chat contacts:** `list_persons` — connections + recent DM participants
  (`id` = `ACo…` for compose send; Messaging `chats` verb)
- **Live:** `watch` — CDP `browser_session.subscribe` fetch-wrap on messaging GraphQL
- **Mark read / unread:** `mark_read` / `mark_unread`
- **Archive:** `set_archived` (`archived: true|false`)
- **Delete:** `delete_conversation` — wired, **do not live-test** without an ask
- **People search:** `search_people` with `query` (brokered `search_people`)

## Status

| Op | Status |
|---|---|
| `check_session` / `login` | ✅ `li_at` + Voyager `/me` identity |
| `list_posts` | ✅ SDUI MainFeed fiber + DOM cards; pagination via `fetchMoreItems`; reaction/comment/repost counts |
| `get_post` | ✅ re-find card / CDP `response_body` → blobs |
| `post_react` | ✅ SDUI `reactions.create` / `reactions.delete` via activity URN on the card |
| `post_comments` | ✅ Voyager `feed/comments` on activity/ugcPost thread (share URN alone fails) |
| `post_comment` | 🚧 create not wired (read path only) |
| `list_conversations` | ✅ Voyager Messaging GraphQL; `archived` shelf filter |
| `list_messages` | ✅ `messengerMessages` (sync window for the open thread) |
| `send_message` | ✅ composer UI + GraphQL receipt; `to` = thread **or** profile |
| `list_persons` | ✅ connections (`relationships/dash/connections`) + inbox participants |
| `watch` | ✅ `browser_session.subscribe` — wrap `fetch` for messaging GraphQL / realtime |
| `mark_read` / `mark_unread` | ✅ dash conversation patch `read` true/false |
| `set_archived` | ✅ `addCategory` / `removeCategory` `ARCHIVE` |
| `delete_conversation` | ✅ HTTP DELETE wired — **untested live** |
| `search_people` | ✅ Voyager dash PEOPLE SRP (`@provides search_people`) |
| `logout` | 🚧 stub |

## IDs

- **post** — `urn:li:share:…` / `urn:li:activity:…` / `urn:li:ugcPost:…` (preferred); DOM fallback `li-dom:…` when the card has no update link yet
- **conversation** — `/messaging/thread/{id}` slug (e.g. `2-ZTBhYWY0…`)
- **message** — `urn:li:msg_message:(urn:li:fsd_profile:…,2-…)`
- **person** — fsd_profile `ACo…` (connections / search / compose `to`)
- **mailbox** — `urn:li:fsd_profile:{ACo…}` from Voyager `/me` mini-profile
- **account** — member `plainId` from Voyager `/me` (also `publicIdentifier` handle)
- **author / person** — `authorId` = member id or `/in/{handle}`; `posted_by`
  is a shaped `person` (or `organization` for company pages) with
  `identities:[{platform:"linkedin", id, handle}]` so the graph dedups people
  and Feeds can show faces via `image`
- **csrf** — `JSESSIONID` value (`ajax:…`) as `csrf-token` header
