---
id: amazon
services:
- http
name: Amazon
description: Search products, get details, and access your Amazon account
color: '#FF9900'
website: https://www.amazon.com
product:
  name: Amazon
  website: https://amazon.com
  developer: Amazon.com, Inc.
---

# Amazon

Search products, get details, and access your Amazon account. No API keys.

Two halves, two substrates:

- **Public** (`search_suggestions`, `search_products`, `get_product`) —
  plain HTTP against Amazon's public autocomplete API + HTML pages. No
  auth, no login.
- **Account** (order history, lists, subscriptions, identity) —
  **browser-driven**. Every op runs as a same-origin `fetch()` *inside*
  a tab of the engine-owned browser (`www.amazon.com`) via the
  `browser_session` service. The session **is** the browser profile —
  Amazon's `.amazon.com` auth cookies, written by Amazon's own
  Set-Cookie, never extracted, never vaulted, never seen by this app.
  Requests originate from the real browser, so the live session, real
  TLS fingerprint, and every anti-bot cookie ride by construction.

The old cookie-vault transport is gone — and with it all the anti-bot
gymnastics it needed to fake a browser (custom client hints, the
`csd-key`/`csm-hit`/`aws-waf-token` strip, the homepage session-warming
hop). The tab is already a warm, real session, so none of that is
required. The same endpoint paths and the same lxml parsers are kept;
only the transport changed.

## Login — sign in once, headed

Amazon's sign-in is email + password + frequent OTP/CAPTCHA, fronted by
heavy anti-bot (Lightsaber), with no stable form to drive blind. So
`login` does **not** attempt to drive it — it returns a NeedsAuth error
telling you to **sign in once, headed, at amazon.com in the AgentOS
browser**. After that the session lives in the browser profile and every
account op rides it. `check_session` and `logout` are fully implemented
against the tab.

## Features

### Product Search
- **`search_suggestions`** — Keyword autocomplete via Amazon's public JSON API (completion.amazon.com). Up to 10 suggestions per query. Supports organic and personalized ranking, 21 marketplaces, 25+ department filters. Zero authentication, no bot detection.
- **`search_products`** — Full product search results with ASIN, title, price, rating, review count, images, and Prime badge. Parsed from search result HTML.
- **`get_product`** — Detailed product page data: title, price, brand, description, rating, images, categories, availability. Parsed from product detail HTML including Amazon's `a-state` embedded data.

### Order History (requires session cookies)
- **`list_orders`** — List orders with date, total, status, and items. Supports time filters: `last30`, `months-3`, `year-2024` through `year-2006`. Pagination via `page` parameter (10 per page).
- **`get_order`** — Full order details: per-item prices and quantities, order summary (subtotal, shipping, tax, grand total), shipping address, delivery status, and tracking URL.
- **`buy_again`** — Products Amazon recommends for repurchase. Returns ASIN, title, price, Prime eligibility.
- **`subscriptions`** — Active Subscribe & Save subscriptions with delivery frequency, next delivery date, upcoming scheduled deliveries, edit deadlines, and total savings.

### Account (browser-driven)
- **`check_session`** — Verify your Amazon session is active and identify the logged-in account. Returns display name, customer ID, marketplace, and Prime status. Reads the homepage + Login & Security pages inside the amazon.com tab.
- **`login`** — Reports the live session, or returns NeedsAuth telling you to sign in once headed (Amazon OTP/CAPTCHA is best cleared by a human).
- **`logout`** — Drives Amazon's own sign-out in the tab, clearing the session from the browser profile.

## Setup

### Public Operations (no setup needed)

`search_suggestions` works immediately — it hits Amazon's public autocomplete API with no authentication.

`search_products` and `get_product` parse public Amazon pages. They work without login but may occasionally encounter CAPTCHAs under heavy use.

### Account Operations (browser-driven)

1. Sign in to [amazon.com](https://www.amazon.com) **once, headed, in the AgentOS browser**. Amazon's login (email + password + OTP/CAPTCHA) is best cleared by a human.
2. The session lands in the browser profile. Every account op runs same-origin inside that profile's amazon.com tab — nothing is extracted or vaulted.
3. Sessions last weeks to months. When one expires, `check_session` reports `authenticated: false`; sign in headed again.

## Graph Model

| Entity | Represents | Key Fields |
|--------|------------|------------|
| **account** | Amazon account | customer_id, display, issuer, marketplace_id, is_prime |
| **order** | Purchase order | order_id, order_date, total, status, delivery_date, shipping_address, tracking_url, summary, items |
| **product** | Amazon product | asin, title, price, brand, rating, image, prime, availability |

### Relationships

```
Account --placed--> Order --contains--> Product
```

Orders contain nested product entities. Each product is identified by ASIN and linked to the order. When imported to the graph, orders are connected to the account and products are connected to the orders.

## Examples

```bash
# Autocomplete suggestions (public JSON API — most reliable)
run({ app: "amazon", tool: "search_suggestions",
  params: { query: "wireless head" } })

# Full product search
run({ app: "amazon", tool: "search_products",
  params: { query: "usb c cable", department: "electronics" } })

# Product details by ASIN
run({ app: "amazon", tool: "get_product",
  params: { asin: "B0BQPNMXQV" } })

# Check session / identify account
run({ app: "amazon", tool: "check_session" })

# List recent orders (default: last 30 days)
run({ app: "amazon", tool: "list_orders" })

# List orders from past 3 months
run({ app: "amazon", tool: "list_orders",
  params: { filter: "months-3" } })

# List orders from a specific year (supports 2006-2026)
run({ app: "amazon", tool: "list_orders",
  params: { filter: "year-2024" } })

# Page 2 of results (10 per page)
run({ app: "amazon", tool: "list_orders",
  params: { filter: "months-3", page: 2 } })

# Get full order details (items, prices, shipping, tracking)
run({ app: "amazon", tool: "get_order",
  params: { order_id: "114-4501818-4961814" } })

# Products recommended for repurchase
run({ app: "amazon", tool: "buy_again" })

# Subscribe & Save subscriptions
run({ app: "amazon", tool: "subscriptions" })
```

## Session Architecture (browser-driven)

Amazon uses tiered cookie-based authentication (`session-id`, `at-main`,
`sess-at-main`, `sst-main`, …). With the browser-driven transport, **the
app never touches these cookies** — they live in the engine-owned browser
profile and ride every `fetch()` automatically because the request is
same-origin inside the amazon.com tab. There is no cookie jar to pass, no
auth-tier tokens to select, and no `skip_cookies` filter: the real browser
serves plain, parseable HTML to its own same-origin requests, so the old
Siege client-side-encryption workaround (stripping `csd-key` / `csm-hit` /
`aws-waf-token`) is no longer needed.

## Technical Details

### Anti-Bot Considerations

- Account ops run inside a real browser tab, so Amazon's Lightsaber bot
  detection sees a genuine session — no client-hint spoofing or
  session-warming needed. The tab supplies the real fingerprint by
  construction.
- The public ops (`search_products`, `get_product`) still scrape HTML over
  plain HTTP; `search_suggestions` uses the lighter `completion.amazon.com`
  JSON API and remains the most reliable fallback if a scrape is blocked.

### Order History Page

The order history page is **pure server-rendered HTML** — no hidden JSON or GraphQL API exists for orders. The page uses `.order-card` containers with `li.order-header__header-list-item` elements for date, total, ship-to, and order ID, plus `.yohtmlc-item` containers for product line items. The amazon.com tab fetches this HTML same-origin and the existing lxml parsers handle it unchanged.

### ASIN Format

10 uppercase alphanumeric characters: `/^[A-Z0-9]{10}$/`
- Non-book products start with `B0` (e.g. `B0BQPNMXQV`)
- Books use ISBN-10 (starts with digits)

### Page Architecture

Amazon uses a server-rendered monolith (not Next.js/React SSR). Product data is embedded in:
- Standard HTML DOM (title, price, rating selectors)
- `<script type="a-state">` proprietary state blocks (~35 per product page)
- `ImageBlockATF` inline scripts (full image manifests)
- Hidden form inputs (ASIN, merchant ID, CSRF tokens)

No JSON-LD or GraphQL endpoints are exposed publicly.

## Backlog

### Done
- [x] `search_suggestions` — public completion API
- [x] `search_products` — HTML search result parsing
- [x] `get_product` — product detail page parsing
- [x] `list_orders` — order history with BeautifulSoup + anti-bot headers
- [x] `check_session` — account identity extraction
- [x] Siege bypass — strip `csd-key` cookie to force plain HTML
- [x] `get_order` — full detail parsing with per-item prices, quantities, summary, tracking
- [x] Pagination — `page` parameter, 10 per page, next-page detection
- [x] Time filters — `last30`, `months-3`, `year-YYYY` back to 2006
- [x] `buy_again` — repurchase recommendations from `/gp/buyagain`
- [x] `subscriptions` — Subscribe & Save management via AJAX endpoint

### In Progress
(none)

### Planned
- [ ] Digital orders — Kindle, apps, etc.
- [ ] Payment method extraction from order details

### Known Issues
- Cookie provider freshness — stale cookies from inactive browsers override fresh ones (backlog `wcya9y`)
- Siege encryption — Amazon's `csd-key` cookie triggers client-side encrypted HTML; must be stripped
- Chrome version drift — `Sec-Ch-Ua` headers must match a real browser version; currently pinned to Chrome 145

## Limitations

- Always returns exactly 10 autocomplete suggestions (server-side cap, `limit` param is ignored)
- HTML scraping operations may be blocked by Amazon's bot detection under heavy use
- Amazon does not provide a public JSON API for product search or details
- Order history parsing relies on HTML structure which Amazon may change
- `csd-key` cookie must be stripped or Amazon sends Siege-encrypted content that requires JS to decrypt
