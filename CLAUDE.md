# Apps — orientation for Claude

You are inside `~/dev/agentos/apps/`, the Python apps repo.
Every app is an adapter from an external platform to a small,
uniform set of shapes + services the engine routes on.

**Before you touch an app — build, extend, or debug — read the
canonical docs below. Do not guess.** The golden rule of this
repo is: *there is always an existing pattern. Copy it, don't
invent.*

The docs live on the **system volume** — read them with
`read({id:"<doc-id>", volume:"system"})` (MCP) or
`agentos call data '{"op":"read","params":{"id":"<doc-id>","volume":"system"}}'`
(CLI). No engine running? The same docs are markdown files in
[`core/system-docs/`](../core/system-docs/) — `<doc-id>` maps to a
file there (e.g. `apps-overview` → `apps/overview.md`).

## Read first (the cheat sheet)

| Task | Read (`id` on the system volume) | Canonical example |
|---|---|---|
| Building any app | `apps-overview` | — |
| Adding login / auth | `apps-adding-login` | ABP, Exa, Goodreads |
| Auth internals (cookies, tokens, providers) | `apps-auth-flows` | — |
| Credential matchmaking (how `login` gets `{email,password}`) | `apps-adding-login` §"The three credential-resolution paths" | `abp.py::login` |
| Multi-step flows (OTP / SMS / OAuth consent) | `apps-auth-flows` §"Multi-step flows" | `exa.py::send_login_code` / `verify_login_code` |
| Reverse engineering an API | `reverse-engineering-overview` (links the whole series) | `united/requirements.md` for endpoint inventory style |
| Connections, auth types, `@connection` | `apps-connections` | — |
| Writing the `logout` tool | `apps-adding-login` §"Do I need a logout tool" | `abp.py::logout` |
| How auth resolution picks one cookie | `architecture-auth-resolution` | — |
| App data/cache, `__secrets__`, expressions | `apps-data` | — |
| Shapes (ontology) | `shapes-overview`; the YAMLs: `../platform/ontology/shapes/*.yaml` | — |

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
  replay. See `reverse-engineering-overview` on the system volume. United's
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
