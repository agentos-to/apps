---
id: reddit
name: Reddit
description: Read Reddit — posts, comments, and communities — through a browser-driven session, mapped to the post and community shapes
services:
  - browser_session
color: '#FF4500'
website: https://reddit.com
privacy_url: https://www.reddit.com/policies/privacy-policy
terms_url: https://www.redditinc.com/policies/user-agreement
product:
  name: Reddit
  website: https://reddit.com
  developer: Reddit, Inc.
sources:
  images:
  - styles.redditmedia.com
  - preview.redd.it
  - i.redd.it
  - external-preview.redd.it
  - a.thumbs.redditmedia.com
  - b.thumbs.redditmedia.com
  image_headers:
    Referer: https://www.reddit.com/
---

# Reddit

Read Reddit through a live reddit.com tab in the engine's HEADLESS background
profile. Every read is a **same-origin `fetch()`** of Reddit's own `.json`
endpoints, run via the `browser_session` service — the Exa pattern. The browser
profile IS the session; requests originate from the real browser, so Reddit's
JS-challenge / bot-detection clears invisibly and reads are never 403'd (the
failure of the old plain-HTTP connector). No window opens for a read.

## How it works

Reddit exposes a JSON API by appending `.json` to any path — fetched
same-origin from inside the tab:

| Path | Returns |
|------|---------|
| `/search.json?q=…` | sitewide post search |
| `/r/<sub>/search.json?restrict_sr=1&q=…` | in-subreddit post search |
| `/r/<sub>/<sort>.json` | subreddit listing (hot/new/top/rising) |
| `/comments/<id>.json` | a post + its full comment tree |
| `/subreddits/search.json?q=…` | subreddit search |
| `/r/<sub>/about.json` | subreddit metadata |
| `/api/me.json` | who am I (the honest auth signal) |

## Login

Logged-out reads work. Sign in to read *your* Reddit (subscriptions,
personalized ranking) — Reddit's sign-in has bot-detection on the POST, so it's
the `login_window` kind of the login protocol (the outlook.py pattern):

1. `reddit.login` opens a chromeless sign-in window on the engine's background
   profile (a headed flip) and returns an `auth_challenge`
   (`kind: "login_window"`, `continueWith: "check_session"`).
2. You sign in (username + password + any 2FA) in that window.
3. Poll `reddit.check_session` until it returns `authenticated: true`.
4. `browser.login_window(close=true)` — flip the profile back to its headless
   daemon (the session already persisted).

The session persists in the engine's background profile — the exact profile
every headless read uses — no re-auth across engine restarts.

## Usage

| Operation | Description |
|-----------|-------------|
| `search_posts` | Search posts sitewide or within a subreddit (`sort`, `time`) |
| `list_posts` | List a subreddit's posts (`sort`: hot/new/top/rising) |
| `get_post` | A post + its nested comment tree (also `web_fetch` for reddit URLs) |
| `comments_post` | A post + its comments flattened into `post[]` with `replies_to` edges |
| `search_communities` | Search for subreddits |
| `get_community` | Subreddit metadata |
| `check_session` / `login` / `logout` | The account trio (session = bg profile) |

## Entity model

- **post** — `name` (title), `content` (selftext/body), `url`, `author`,
  `published`, `score`, `commentCount`, `posted_by` (→ `account`),
  `published_in` (→ `community`). A comment is a `post` that `replies_to`
  another.
- **community** — `name` (subreddit), `content` (public description), `url`,
  `image`, `subscriberCount`.

## Behavior notes

- First read after an engine restart is slower (~2-5s): browser attach + page
  load + JS-challenge clear. Warm reads run in well under a second.
- A `403` from a read means bot-detection got through the browser profile
  (unexpected from a real browser) — a `login` usually clears it.
