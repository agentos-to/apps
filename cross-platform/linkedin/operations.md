# LinkedIn Feed — reverse-engineering reference

Durable notes from live CDP on Joe's daily Brave (`mode:attach`,
`linkedin.com/feed/`, 2026-07-18). The connector (`linkedin.py`) is the
*what*; this file is the *why* so the next agent does not re-derive it.

## Deciding fact

The LinkedIn **home feed is flagship SDUI / RSC**, not classic Voyager
`UpdateV2` Ember cards and not a page-side Relay store.

| Signal | Value |
|---|---|
| Screen | `com.linkedin.sdui.flagshipnav.feed.MainFeed` |
| Page key | `d_flagship3_feed` |
| List DOM | `[data-testid="mainFeed"]` (`LazyColumn`) |
| Cookie | `sdui_ver=sdui-flagship:…` |

Classic `div.feed-shared-update-v2` is **gone**. Fiber collections carry
React-lazy UI trees; structured post fields for list mapping come from
the painted card (a11y text + `media.licdn.com` imgs + update links).

## Auth

| Cookie | Role |
|---|---|
| `li_at` (httpOnly) | true session — reads/writes work iff present |
| `JSESSIONID` | CSRF — send value as `csrf-token` header (`ajax:…`) |
| `liap=true` | logged-in chrome hint (not sufficient alone) |

### login auto-close

`linkedin.login` opens `login_window(strategy=profile)`, then polls
(`mode=login`) until either:

1. `li_at` is present on the login profile, or
2. the page left `/login|checkpoint` and shows MainFeed / global-nav /
   `/feed` / `liap=true`

Then `merge_login_session` → `close_login_window`. Do **not** leave the
headed window open after success.

`document.cookie` cannot see `li_at` — `check_session` must use
`browser_session.read_cookies` / `browser.cookies`.

Voyager identity still works:

```
GET /voyager/api/me
Accept: application/vnd.linkedin.normalized+json+2.1
csrf-token: <JSESSIONID>
x-restli-protocol-version: 2.0.0
```

→ `data.plainId`, `*miniProfile` → `included[]` MiniProfile
(`firstName`, `lastName`, `publicIdentifier`, `occupation`).

Wrong Accept → **406**.

## Feed list algorithm (connector)

1. Ensure `https://www.linkedin.com/feed/` is loaded.
2. Walk React fiber for `collectionId === "mainFeed"` with `items[]`.
3. Optionally call `paginationResult.fetchMoreItems()` to warm more cards.
4. Parse each `[data-lazy-mount-id]` under `[data-testid="mainFeed"]`
   whose innerText starts with `Feed post`.
5. Drop cards containing `Promoted`.
6. Map → `post`:
   - `id` from `a[href*="urn:li:"]` (`share` / `activity` / `ugcPost`)
   - else fiber `dedupeId` when it is a URN
   - else stable DOM fallback `li-dom:<author>:<published>`
   - `author` / `content` / relative `published` from card text
   - `image` = author face only (`profile-displayphoto*` / `company-logo*`,
     prefer `<a href="/in/…">` / `/company/` avatar) — **never** post media
   - `mediaUrl` / `thumb` = largest `feedshare*` / `image-shrink*` /
     `videocover*` URL found on the card (img + srcset + HTML). CDN size
     tokens are signed — do not rewrite `shrink_N`; pick the biggest URL
     already present.
   - `externalUrl` = `/feed/update/<urlencoded urn>/`
7. Ads and chrome (sharebox, sort control) never get a `dedupeId` URN —
   they fall out when we require author+content after Promoted filter.

Live proof (attach, 2026-07-18): **13 organic posts** after one
`fetchMoreItems`, including image + video-cover cards; viewer
`plainId=11138205` / handle `jcontini`.

## Pagination (SDUI)

Fiber `nextPageRequest`:

```
$type: proto.sdui.actions.requests.PaginationRequest
pagerId: com.linkedin.sdui.pagers.feed.mainFeed
requestedArguments.payload: {
  startIndex, count, token,
  feedSortOrder: "FeedSortOrder_RELEVANCE" | …,
  requestType: "Pagination"
}
```

Calling `paginationResult.fetchMoreItems()` fires:

```
POST /flagship-web/rsc-action/actions/pagination
  ?sduiid=com.linkedin.sdui.pagers.feed.mainFeed
```

Required headers (page-bound — scrape from the live tab, don't invent):

| Header | Example role |
|---|---|
| `csrf-token` | JSESSIONID |
| `x-li-page-instance` | `urn:li:page:d_flagship3_feed;…` |
| `x-li-page-instance-tracking-id` | tracking id |
| `x-li-application-instance` | app instance |
| `x-li-application-version` | e.g. `0.2.6389` |
| `x-li-anchor-page-key` | `d_flagship3_feed` |
| `x-li-rsc-stream` | `true` |
| `x-li-track` | JSON client/device blob |

Response is an **RSC flight stream** (~MB), not normalized Voyager JSON.
Prefer in-page `fetchMoreItems` + re-scrape DOM over parsing the stream.

Cursor for the Feeds pager: JSON-stringify the payload
`{startIndex,count,token,feedSortOrder,requestType}`.

## What we tried and rejected

| Path | Result |
|---|---|
| `__re.detect()` → `vjsForDebug` | **Video.js** — red herring, not LinkedIn's model |
| `__webpack_require__` registry | present but **empty cache** from page JS |
| Voyager `GET /voyager/api/feed/updates` | **400** |
| Voyager GQL `voyagerFeedDashMainFeed` (no hash) | **400** |
| Bundle grep for `voyagerFeedDashMainFeed.<hash>` on feed aero chunks | **no hits** (feed page no longer ships that queryId) |
| `__re.net.on()` then scroll | **0 hits** until `fetchMoreItems` (initial feed is SSR/RSC) |

Voyager GraphQL may still serve **profile updates / other surfaces**
(`voyagerFeedDashProfileUpdates`, etc.) — do not assume home feed.

## get_post / media

List rows carry `mediaUrl` pointing at `media.licdn.com`. In-page `fetch`
of CDN may be CORS-blocked; use `browser_session.response_body` (same
pattern as Facebook/IG). Prefer `feedshare*` / high-res over
`profile-displayphoto` for the post image.

## People / orgs on posts & comments

Feeds header avatars read `post.image` (FB/WA convention). LinkedIn must put
the **profile face** (or company logo) on `image`, and the attachment on
`mediaUrl` / `thumb`.

| Field | Value |
|---|---|
| `author` | display name string |
| `authorId` | member id when known, else `/in/{handle}` publicIdentifier (or company slug) |
| `from` | `{platform:"linkedin", id, handle?, display_name}` — FB-shaped JSON |
| `posted_by` | **shaped** child: `{shape:"person", identities:[{platform:"linkedin", id, handle}], image, url, givenName, familyName, jobTitle}` or `{shape:"organization", name, url, image}` |

Extraction only materializes nested nodes that self-declare `shape:` /
`_tag:` — a bare `from` dict stays a JSON val. Always stamp `posted_by`.

Author handle comes from the first `a[href*="/in/"]` (or `/company/`) on the
card; member id from `urn:li:member:N` in the card HTML when present.
Comments get the same treatment from Voyager `MiniProfile` + `commenter.urn`.

## Social counts (list)

Painted card footers expose trailing integers — typically
`reactions [, comments [, reposts]]`. Also `See N more comments`.
`list_posts` maps these to `score` / `commentCount` / `shareCount` and a
best-effort `reactions: [{type:LIKE,count}]`.

`viewerReaction` comes from
`aria-label="Reaction button state: …"` (`no reaction` → omit).

## Activity URN (critical)

Share URNs from update links **do not** work for social APIs
(`voyagerSocialDashReactions` returns `total: 0`). The **activity** URN
is on the reaction-button fiber / SDUI props:

```
threadUrn.threadUrnActivityThreadUrn.activityUrn.activityId
```

`list_posts` stores it as `activityUrn` + `feedbackId` (Feeds-shaped key).

Read path that works:

```
GET /voyager/api/voyagerSocialDashReactions
  ?q=reactionType&threadUrn=urn%3Ali%3Aactivity%3A…&count=1
→ data.paging.total
```

(`ugcPost` URNs also work; bare `share` usually does not.)

## Comments (read) — Voyager, not SDUI parse

UI loads comments via SDUI
`com.linkedin.sdui.feed.update.comments.fetchComment` (huge RSC stream).
For connector reads, prefer classic Voyager — proven live:

```
GET /voyager/api/feed/comments
  ?q=comments
  &updateId=urn%3Ali%3Aactivity%3A…   # or urn:li:ugcPost:…
  &count=15
  &start=0
Accept: application/vnd.linkedin.normalized+json+2.1
csrf-token: <JSESSIONID>
x-restli-protocol-version: 2.0.0
```

| Thread URN | Result |
|---|---|
| `urn:li:activity:…` | ✅ comments + MiniProfiles |
| `urn:li:ugcPost:…` | ✅ |
| `urn:li:share:…` | ❌ empty / useless |

`included[]` carries `com.linkedin.voyager.feed.Comment` + `MiniProfile`.
Map: `commentary.text`, `createdTime` (ms), `commenter.*miniProfile`,
`parentCommentUrn` (replies), `urn` / `entityUrn` as id.
Paging: `data.paging.{start,count,total}`; cursor = next `start` string.
`metadata.updatedCommentCount` is a richer total when present.

SDUI still useful for writes / open-panel:
`fetchFeedUpdateActionPrompt`, `fetchComment` (pageToken pagination).

## Writes

| Action | Layer | Status |
|---|---|---|
| React / clear | SDUI `com.linkedin.sdui.reactions.create` / `.delete` | ✅ wired (`post_react`) |
| Comment list | Voyager `feed/comments` | ✅ wired (`post_comments`) |
| Comment create | SDUI `com.linkedin.sdui.comments.createComment` (likely) | 🚧 not wired |

React POST (in-page `fetch`, same tab):

```
POST /flagship-web/rsc-action/actions/server-request
  ?sduiid=com.linkedin.sdui.reactions.create
body.payload = {
  threadUrn: { threadUrnActivityThreadUrn: { activityUrn: { activityId } } },
  reactionType: "ReactionType_LIKE" | PRAISE | APPRECIATION | EMPATHY | INTEREST | ENTERTAINMENT,
  reactionSource: "Update"
}
```

**Do not** live-test react/comment writes against friends' posts.

## Toolkit notes

- Ladder: `detect` → React + webpack leak; coach ranks in-page API then net.
- Highest live hook for feed **reads**: fiber `mainFeed` collection + DOM.
- Highest live hook for feed **pages**: `fetchMoreItems` /
  SDUI pagination POST above.
- Do **not** inject `toolkit.js` in production ops — only during RE.

---

## People search (2026-07-18)

Facebook parity: `@provides("search_people")` → brokered `services.search_people`.

```
GET /voyager/api/search/dash/clusters
  ?decorationId=com.linkedin.voyager.dash.deco.search.SearchClusterCollection-1
  &origin=SWITCH_SEARCH_VERTICAL&q=all
  &query=(keywords:{q},flagshipSearchIntent:SEARCH_SRP,
          queryParameters:(resultType:List(PEOPLE)),includeFiltersInResponse:false)
  &start=0&count={limit}
```

Included: `EntityResultViewModel` (+ thin `Profile` stubs). Map → light
`person`: `id` = fsd_profile `ACo…`, `name`, `jobTitle` (headline),
`about` (location line), `image`, `url` `/in/{handle}/`,
`identities:[{platform:"linkedin", id, handle}]`. Exact handle/id matches
sorted first.

Proven: `ryan-humphries7` (Austin / Floatie Kings) lands in results.

## Address book — `list_persons` (2026-07-18)

Messaging New Chat reads `chats` verb `list_persons` (no `@provides`).

| Source | Path |
|---|---|
| Connections | `GET /voyager/api/relationships/dash/connections?decorationId=…ConnectionList-1&q=search&sortType=RECENTLY_ADDED&count=&start=` |
| Warm inbox | `messengerConversations` participants (fills gaps) |

Included `Connection` → `Profile` (`publicIdentifier`, names, headline, picture).
Mapped `person.id` = fsd_profile `ACo…` — same token `send_message` accepts for
compose-to-profile.

## Messaging (2026-07-18)

Classic Ember messaging UI is still live at `/messaging/` (not SDUI MainFeed).
Reads go through **Voyager Messaging GraphQL** (same-origin `fetch` +
`csrf-token: JSESSIONID`).

| Surface | Value |
|---|---|
| Inbox URL | `https://www.linkedin.com/messaging/` |
| Thread URL | `/messaging/thread/{threadSlug}/` |
| Mailbox | `urn:li:fsd_profile:{ACo…}` from `/voyager/api/me` (`fs_miniProfile` → `fsd_profile`) |
| Conversations | `GET /voyager/api/voyagerMessagingGraphQL/graphql?queryId=messengerConversations.0d5e6781bbee71c3e51c8843c6519f48&variables=(mailboxUrn:…)` |
| Messages | `…queryId=messengerMessages.5846eeb71c981f11e0134cb6626cc314&variables=(conversationUrn:…)` |
| Conversation URN | `urn:li:msg_conversation:({mailbox},{threadSlug})` |
| Encoding | Percent-encode `: ( ) ,` in urns (`encodeURIComponent` leaves `()` → **400**) |
| Included types | `com.linkedin.messenger.Conversation` / `.Message` / `.MessagingParticipant` |
| Composer | `.msg-form__contenteditable` + `button.msg-form__send-button` |
| Realtime | `/realtime/realtimeFrontendSubscriptions` (presence proven; messages also land via GraphQL sync responses) |

### Wire → Messaging shapes

| Field | Source |
|---|---|
| `conversation.id` | thread slug from `conversationUrl` / URN |
| `conversation.name` | other participant(s) when `title` null |
| `conversation.image` | `VectorImage` face on `MessagingParticipant` |
| `conversation.unreadCount` | `unreadCount` / `read` |
| `message.content` | `body.text` (AttributedText) |
| `message.isOutgoing` | sender `hostIdentityUrn` === mailbox |
| `message.conversationId` | thread slug |

### Live watch

`browser_session.subscribe` installs a durable `window.fetch` wrap (flag
`__agentos_li_watch__`). New `com.linkedin.messenger.Message` rows in
messaging GraphQL / realtime JSON → `console.log("__agentos_entity__" + …)`
with `__shape__: "message"`. Seen-set + arm-time filter skip warm history
(Instagram / WhatsApp discipline).

### Send

Composer UI drive + GraphQL receipt (Instagram spirit).

| `to` | Navigate |
|---|---|
| Thread slug `2-…` / `/messaging/thread/…` / `msg_conversation` URN | `/messaging/thread/{slug}/` |
| Profile `ACo…` / `urn:li:fsd_profile:…` / `/in/{handle}` / bare handle | `/messaging/compose/?recipient=urn:li:fsd_profile:…` (handle resolved via identity dash profiles) |

After compose send, wait for `/messaging/thread/{slug}/` land, then poll
`messengerMessages` for matching outgoing text.

Do **not** live-send to a brand-new person unless asked. Existing-thread tests
(e.g. Ryan) are fine.

### Conversation actions (⋯ menu, 2026-07-18)

Base: `POST|DELETE /voyager/api/voyagerMessagingDashMessengerConversations`

| UI | HTTP | Body / query |
|---|---|---|
| Mark as read | `POST ?ids=List({encConvUrn})` | `{"entities":{"{convUrn}":{"patch":{"$set":{"read":true}}}}}` |
| Mark as unread | same | `$set: { read: false }` |
| Archive | `POST ?action=addCategory` | `{"conversationUrns":["{convUrn}"],"category":"ARCHIVE"}` → **204** |
| Unarchive | `POST ?action=removeCategory` | same body → **204** |
| Delete conversation | `DELETE ?ids=List({encConvUrn})` | (no body) — **do not live-test** |

`encConvUrn` = percent-encode `: ( ) ,` in the full
`urn:li:msg_conversation:({mailbox},{threadSlug})` (same rule as GraphQL).

Ops: `mark_read` / `mark_unread` / `set_archived` / `delete_conversation`
(`@provides` `message_mark_read` / `message_mark_unread` / `message_archive` /
`message_delete`). `list_conversations(archived=true)` is the ARCHIVE shelf.

### Dead ends (tried)

| Path | Result |
|---|---|
| Legacy `/voyager/api/messaging/conversations?q=search` | 500 |
| `voyagerMessagingDashMessengerConversations?q=…` | 400 without full deco |
