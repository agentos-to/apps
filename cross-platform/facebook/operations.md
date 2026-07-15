# Facebook Stories — reverse-engineering reference

Durable notes from live CDP on Joe's daily Brave (`mode:attach`,
`facebook.com`, 2026-07-13). The connector (`facebook.py`) is the *what*;
this file is the *why* so the next agent does not re-derive it.

## Deciding fact

Facebook **Stories** (24h rings on the home tray) are **not** in the MSYS
shared worker. They live in the **page Relay store** on `facebook.com` —
same lane as Instagram's DMs (fiber → `RelayModernEnvironment` →
normalized records), opposite of Messenger DMs (worker EAR).

Messenger stays the DM plugin. This plugin owns the social `feeds` /
`get_post` surface for FB Stories.

## Auth

| Cookie | Role |
|---|---|
| `xs` (httpOnly) | true session — writes work iff present |
| `c_user` | viewer FBID (plain) |

Same Facebook login as `messenger` (shared engine bg profile). Prefer
`login_window` for sign-in; do not cookie-copy from the daily browser.

## Store map (proven)

Relay source API: `env.getStore().getSource()` with `.get` / `.getRecordIDs`
(no enumerable `_records` map).

| Record id pattern | Typename | Role |
|---|---|---|
| `client:<viewer>:unified_stories_buckets(first:N)` (+ `after:` pages) | `UserToUnifiedStoryBucketsConnection` | tray pages |
| `client:<viewer>:__StoriesTrayRectangular_unified_stories_buckets_connection` | same | tray fragment connection |
| `<bucketId>` | `UserStoryBucket` / `DirectMessageThreadBucket` / … | one author's ring |
| `client:<bucketId>:unified_stories(include_fb_note:true)` | `UnifiedStoryBucketToUnifiedStoriesConnection` | cards in the ring |
| `<cardId>` (`UzpfSVND:…` base64 → `S:_ISC:<id>`) | `Story` | one story card |
| `client:<cardId>:story_card_info` | `StoryCardStoryInfo` | type, thumb |
| `client:<cardId>:story_card_seen_state` | `StoryCardSeenState` | `is_seen_by_viewer` |

Bucket fields that matter:

- `story_bucket_owner` → User/Page (`name`, `profile_picture(…)`)
- `first_story_to_show` → Story (often the only hydrated card until open)
- `is_bucket_seen_by_viewer`, `should_show_close_friend_badge`
- `expiration_time` / card `creation_time` (unix seconds)

Card media:

- `attachments[].media` → `Photo` with `image(height:…,width:…)` `.uri`
- or `Video` with playable URL fields
- tray thumb: `story_card_info.story_thumbnail(height:160,…).uri`

DOM cross-check: `[aria-label="stories tray"]` links
`/stories/<bucketId>/<cardId>/?source=story_tray`.

## List algorithm (connector)

1. Ensure `facebook.com` feed is loaded (tray query lands on cold start).
2. Find Relay env (fiber walk — toolkit `__re.relayEnv()` or the IG-style finder).
3. Walk bucket connections in preference order:
   `__StoriesTrayRectangular_unified_stories_buckets_connection` first
   (this edge order **is** Facebook's tray ranking — self, then friends),
   then `unified_stories_buckets(first:N)`, then `after:` pages. Dedup by
   bucket id; **do not** re-sort by `published`.
4. Per bucket: owner + stories from `unified_stories` / `first_story_to_show`;
   cards sorted oldest→newest within the ring.
5. Map each Story → `post` with `postType: "story"`, `image` (face),
   `thumb` (tray `story_thumbnail` / video thumb), `mediaUrl` (full media).

Live proof (attach, 2026-07-13): **23 buckets / 53 posts** including own
story + friends. Tray order matches the rectangular connection edges.

## get_post / media

List rows carry metadata + author `image` (face). Full media CDN URIs live
on the Story / Photo records. In-page `fetch` of `fbcdn` is CORS-blocked
(pixels ride Chromium's network stack via `<img>`/`<video>`, not page JS).

`get_post` hydrates with **`browser_session.response_body`** → CDP
`Network.getResponseBody` (forces an Image/video load, or falls back to
`Network.loadNetworkResource` + `IO.read`) → `blobs.put` → `attaches[0]`.
Engine `http.request` remains a last-resort fallback (plugin `http` grant).
No E2EE.

**Video stories:** the tray query often leaves `Video` records without
`playable_url` (only a tiny `story_thumbnail` JPEG). `get_post` navigates to
`/stories/<bucketId>/<cardId>/?source=story_tray` to land the viewer query,
re-reads `playable_url`, then fetches the mp4. Attach `mimeType` is trusted
over tray `mediaType` so a JPEG poster is never shaped as `video`.

## Pagination (next)

Tray connections use `after` cursors (`page_info.has_next_page`). Cold home
load only hydrates `first:6` then scrolls more into the store. To force a
full tray without depending on prior UI scroll: replay the tray's own
Relay query via `createOperationDescriptor` + `env.execute` (same pattern
as IG `loadThreadHistory`) — **not yet wired**; list currently reads
whatever the store already holds (enough once the feed has been visited).

## Non-goals / do not confuse

- News Feed units also typename `Story` — those are feed posts, not 24h
  Stories. Only walk `unified_stories_buckets` / `unified_stories`.
- `messenger.com` is a separate logged-out session — never use it.
- MSYS worker EAR tables are DMs only — ignore for Stories.

## Toolkit notes

`__re.detect()` on facebook.com reports metro/msys because Messenger is
embedded; for Stories ignore the worker coach and stay on **Relay**.
