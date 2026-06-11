# Apps — orientation for Claude

You are inside `~/dev/agentos/apps/`, the Python apps repo.
Every app is an adapter from an external platform to a small,
uniform set of shapes + services the engine routes on.

**Before you touch an app — build, extend, or debug — read the
canonical docs below. Do not guess.** The golden rule of this
repo is: *there is always an existing pattern. Copy it, don't
invent.*

## Read first (the cheat sheet)

| Task | Read | Canonical example |
|---|---|---|
| Building any app | [`apps/overview.md`](../platform/docs/src/content/docs/apps/overview.md) | — |
| Adding login / auth | [`apps/adding-login.md`](../platform/docs/src/content/docs/apps/adding-login.md) | ABP, Exa, Goodreads |
| Auth internals (cookies, tokens, providers) | [`apps/auth-flows.md`](../platform/docs/src/content/docs/apps/auth-flows.md) | — |
| Credential matchmaking (how `login` gets `{email,password}`) | [`apps/adding-login.md#the-three-credential-resolution-paths`](../platform/docs/src/content/docs/apps/adding-login.md) | `abp.py::login` |
| Multi-step flows (OTP / SMS / OAuth consent) | [`apps/auth-flows.md#multi-step-flows`](../platform/docs/src/content/docs/apps/auth-flows.md) | `exa.py::send_login_code` / `verify_login_code` |
| Reverse engineering an API | [`apps/reverse-engineering/`](../platform/docs/src/content/docs/apps/reverse-engineering/) | `united/requirements.md` for endpoint inventory style |
| Connections, auth types, `@connection` | [`apps/connections.md`](../platform/docs/src/content/docs/apps/connections.md) | — |
| Writing the `logout` tool | [`apps/adding-login.md#do-i-need-a-logout-tool`](../platform/docs/src/content/docs/apps/adding-login.md) | `abp.py::logout` |
| How auth resolution picks one cookie | [`architecture/auth-resolution.md`](../platform/docs/src/content/docs/architecture/auth-resolution.md) | — |
| Shapes (ontology) | `../platform/ontology/shapes/*.yaml` | — |

## Universal rules for this repo

- **Credentials come from matchmaking, never from guesses.** In
  a `login` tool, call `credentials.retrieve(domain=".service.com",
  required=["email","password"])`. Never default a user email.
  Never reuse an email from one platform for another — Joe uses a
  different address per provider.
- **`@connection("public")` for `login`; everything else uses
  the authed connection.** The `login` tool produces credentials,
  so it can't run on a connection that requires them.
- **Ship `check_session`, `login`, and `logout` together.** They
  are the three legs of the account protocol. The validator
  (`agent-sdk validate`) warns if `logout` is missing.
- **Cookies self-persist via `__cookie_delta__`.** The ambient
  Jar captures `Set-Cookie` during the handshake; the engine
  writes back on tool exit. Don't hand-roll cookie storage.
- **`@provides(login_credentials)` is the contract for 1Password,
  Keychain, and any future provider.** Callers say
  `credentials.retrieve(...)`, the engine dispatches every
  provider that declares the service, freshest wins.
- **Three-path credential resolution in every `login` tool:**
  (1) explicit args, (2) `credentials.retrieve`,
  (3) `app_error(code="NeedsCredentials", required=[...])`.
  Copy the template from `adding-login.md` §3.
- **For multi-step auth (OTP via SMS/email): two separate
  tools** — `send_login_code(email)` returns a `hint` the agent
  reads; `verify_login_code(email, code)` finishes. Agent reads
  the code via any `@provides(email_lookup)` app (Gmail,
  Mimestream) or via iMessage SQL for SMS.
- **Never hardcode URLs to a password manager — use the
  provider service.** `@provides(login_credentials)` apps
  today: `secrets/onepassword`, `macos/macos-keychain`.
- **Reverse engineering starts with CDP capture**, not body-only
  replay. See `docs/.../reverse-engineering/`. United's
  `requirements.md` is a good reference for what a thorough
  capture log looks like.

## Working here

- **Validate an app:** `agent-sdk validate <app>` from the
  `apps/` root.
- **Call an app tool from CLI:** `agentos call apps '{"op":"run",
  "params":{"app":"united","tool":"check_session"}}'`.
- **Restart the engine after Python changes:** not needed —
  Python workers hot-load. Only rebuild Rust (`./dev.sh restart`
  in `core/`) for engine changes.
- **Credential store:** the `credentials` table inside the user
  vault `~/.agentos/users/<u>.db` (encrypted, key in macOS
  Keychain). Writes happen through `__secrets__` + `__cookie_delta__`
  sidebands — never open the DB directly from an app. Sideband keys
  are **camelCase** (`itemType`, `expiresAt`) — the engine rejects
  snake_case.
- **api-key connections are directly settable** — no login flow
  needed: `agentos call apps '{"op":"connect","params":{"app":
  "<id>","key":"<secret>"}}'`. `apps.load` shows per-connection
  auth state (`## Connections`), and an unauthed call returns
  NEEDS_CREDENTIALS with the obtain-URL + the literal connect call.

## If a doc says "it depends"

Follow the reference implementation. The three canonical auth
shapes are:

| Shape | App | Why pick it |
|---|---|---|
| Cognito / Amplify (password + IdToken) | `fitness/austin-boulder-project/abp.py` | Provider handshake + portal follow-up |
| NextAuth + email OTP | `web/exa/exa.py` | Two-step flow, pure HTTP |
| Plain form POST + cookies | `media/goodreads/goodreads_web.py` | Simplest case |

If none of the three fit — your platform does something weirder
— that's a reverse-engineering project. Start with CDP capture
and document the shape in the app's own `requirements.md`
(see `logistics/united/requirements.md` for the style).
