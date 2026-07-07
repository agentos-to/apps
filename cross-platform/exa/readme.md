---
id: exa
services:
- http
name: Exa
description: Semantic web search and content extraction
color: '#1F40ED'
website: https://exa.ai
privacy_url: https://exa.ai/privacy
terms_url: https://exa.ai/terms
product:
  name: Exa
  website: https://exa.ai
  developer: Exa AI, Inc.
---

# Exa

Semantic web search and content extraction. Neural search finds content by meaning, not just keywords.

## Setup

1. Get your API key from https://dashboard.exa.ai/api-keys
2. Add credential in AgentOS Settings → Providers → Exa

Or use the automated bootstrap flow (see Auth below).

## Features

- Neural/semantic search
- Fast content extraction
- Find similar pages
- Relevance scoring

## Usage

### search

Create a web search. Returns search results (index records, not full page content).

```
run({ app: "exa", tool: "search", params: { query: "rust programming" } })
```

Results are `result` entities — snapshots of what the search engine knew about each URL.
To get full page content, follow up with `read_webpage` on a result's URL.

### read_webpage

Extract full content from a URL.

```
run({ app: "exa", tool: "read_webpage", params: { url: "https://example.com" } })
```

## Auth

Two halves, two substrates:

- **API (`search`, `read_webpage`)** — a portable Exa **API key** on the
  `api` connection, stored in the vault. `get_api_keys` / `create_api_key`
  populate it from the dashboard. This is the only thing the vault holds
  for Exa.
- **Dashboard (account trio + key management)** — **browser-driven**. The
  session is the engine-owned browser profile, not a vault row. Every
  dashboard op is a same-origin `fetch()` evaluated *inside* the Exa tab
  via the `browser_session` service; the `.exa.ai` session cookie rides
  it automatically, so requests originate from the real browser and clear
  Vercel's security checkpoint / Cloudflare by construction. The app
  never sees the cookie.

### Where the session lives

`next-auth.session-token` on `.exa.ai`, written by NextAuth's own
Set-Cookie into the profile at `~/.agentos/browsers/brave`. It survives
engine and browser restarts because the profile does — log in once, stay
logged in. There is no cookie credential row and no `__cookie_delta__`.

### Login flow (email verification code)

NextAuth.js, signin == signup. All three steps run inside the browser tab:

1. `login(email)` — returns the `account` if the profile already holds a
   live session; otherwise runs `fetch('/api/auth/csrf')` +
   `POST /api/auth/signin/email` in the **auth.exa.ai** tab, triggering a
   6-digit code email (subject "Sign in to Exa Dashboard"). Returns an
   `auth_challenge{kind: code_sent}`.
   Email resolves from `credentials.retrieve(domain=".exa.ai")` (any
   `login_credentials` provider — 1Password, Keychain) when not passed.
2. Agent reads the 6-digit code from any `email_lookup` provider (Gmail,
   Mimestream).
3. `verify_login_code(email, code)` — runs `POST /api/verify-otp` then
   `GET /api/auth/callback/email` same-origin in the auth tab. The
   callback's Set-Cookie lands the session in the profile directly;
   confirmed by reading the session back from the dashboard tab.

### Auth signal — the redirect IS the state

NextAuth bounces an unauthenticated dashboard visit to `auth.exa.ai`.
`_eval` waits for the tab to settle on an `.exa.ai` origin, then exposes
`__onDashboard` to the op body: on the dashboard → logged in; bounced to
auth → logged out. No timeout, no cookie inspection.

### Dashboard endpoints (called same-origin from the tab)

| Endpoint | Returns |
|----------|---------|
| `GET /api/auth/session` | user + team memberships (the session probe) |
| `GET /api/get-api-keys` | keys — the bearer secret is **`legacyBearerSecret`**; `id` is the row UUID, `publicId` the display handle (neither authenticates) |
| `GET /api/get-teams` | rate limits, credits, usage, billing |
| `POST /api/create-api-key` | mints a key (secret in `legacyBearerSecret`) |

The stored API key (a portable secret) is the only thing that leaves the
browser for the vault.


## Known Limitations

**`read_webpage`**: May fail for URLs the crawl API cannot fetch (e.g., pages behind auth, rate-limited sites). The API returns empty results with error info in `statuses`. Retry with another integration that implements `webpage.read` using a real browser if you need JS rendering.
