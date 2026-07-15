# Apps — orientation for Claude

You are inside `commons/plugins/` (installed as a source's `plugins/`
aisle). Every plugin adapts an external platform to shapes + brokered
services.

**Golden rule:** there is always an existing pattern. Copy it; don't invent.
Prefer **small accurate docs** on the system volume over lore in this file.

## Read first

| Task | Doc (`volume:"system"`) | Copy |
|---|---|---|
| Building any plugin | `apps-overview` | — |
| **Which login path?** | `apps-adding-login` (decision table at top) | — |
| SPA / member-portal session | `apps-browser-driven` | `outlook`, `gmail-cdp` |
| Portable token (OAuth / Cognito / API key) | `apps-adding-login` | `gmail`, `austin-boulder-project` |
| Commons app (Email-style UI) | `apps-commons` | `commons/apps/email/` |
| Connections / auth types | `apps-connections` | — |
| Shapes | `shapes-overview` | `platform/ontology/shapes/*.yaml` |
| Timezones | `shapes-overview` §10b | — |
| RE / capture notes | `reverse-engineering-overview` | `united/dev/requirements.md` |
| Plugin file layout | `reverse-engineering-transport` § App File Layout | `uber/` (`readme` + tools + `lib/` + `page/`; RE in `dev/`) |

On disk: `core/system-docs/apps/<slug>.md` ↔ id `apps-<slug>`.

## Decision: API vs CDP session

| Situation | Do |
|---|---|
| Official API with real auth | Use it (`gmail` OAuth, ABP Cognito, Exa API key) |
| Logged-in SPA / billing portal | **CDP session** — `login_window` + background profile (`outlook` / `gmail-cdp`) |
| Tempted to curl-replay cookies | **Don't** — `auth.type=cookies` is retired |

## Universal rules

- **Ship the account trio** — `@account.check` / `@account.login` /
  `@account.logout` together (`agent-sdk validate` errors if logout missing).
- **Credentials from matchmaking** — in portable-credential `login`,
  `credentials.retrieve(domain=..., required=[...])`; never invent Joe's
  email. Providers: `onepassword`, `macos-keychain`.
- **Browser sessions live in the profile** — no `__secrets__` cookie blob;
  `@connection("none")` + `browser_session.*`.
- **Never hardcode venue timezones** — `place.timezone` / provider venue tz;
  user "today" → OS `user_environment.timezone` (`shapes-overview` §10b).
  Emit absolute `…Z` datetimes: provider wall → `wall_to_utc(local, tz)`;
  already UTC → `canonicalize_datetime`; write local → `utc_to_wall`.
  Never return naive `startDate` + `timezone` (consumers treat naive as UTC).
- **Brand plugin, platform connection** — e.g. future `switchyards` plugin,
  `connection("skedda")` inside (like ABP ≠ Tilefive).
- **`@provides` for commons apps** — Email fans out `mailbox`; don't hardcode
  plugin ids in app UI (`apps-commons`).

## Working here

- Validate: `agent-sdk validate <app>` from the plugins root.
- Run: `agentos call apps '{"op":"run","params":{"app":"outlook","tool":"check_session"}}'`.
- Python hot-loads; Rust changes need `./dev.sh restart` in `core/`.
- Sideband keys are **camelCase** (`itemType`, `expiresAt`).

## Auth references (paths under `cross-platform/`)

| Shape | App |
|---|---|
| Browser session + `login_window` | `outlook/outlook.py`, `gmail-cdp/gmail_cdp.py` |
| OAuth API | `gmail/gmail.py` |
| Cognito → vault IdToken | `austin-boulder-project/abp.py` |
| In-tab OTP | `exa/exa.py` |
| QR + deep JS | `whatsapp/whatsapp.py` |
