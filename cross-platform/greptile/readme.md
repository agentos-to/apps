---
id: greptile
services:
- http
name: Greptile
description: "AI code review and codebase search \u2014 organization and member management\
  \ via dashboard session"
color: '#16B364'
website: https://greptile.com
privacy_url: https://www.greptile.com/privacy
terms_url: https://www.greptile.com/terms
product:
  name: Greptile
  website: https://greptile.com
  developer: Greptile, Inc.
---

# Greptile

AI code review and codebase search. This app manages **organization membership** via the dashboard session — listing members, sending invites, generating invite links, updating roles, and removing members.

## Setup

This app is **browser-driven**: every op runs same-origin `fetch()` inside a
tab of the engine-owned browser (the Exa pattern). The session is the browser
profile — nothing is extracted or vaulted.

1. Log into https://app.greptile.com once in the AgentOS browser profile.
2. `run({app:"greptile", tool:"check_session"})` returns `{authenticated: true, ...}`.

`login` can also drive the sign-in autonomously: it fills the email+password
form at `auth.greptile.com` with credentials from a `login_credentials`
provider (1Password / Keychain / vault). OAuth-only accounts (GitHub / GitLab
/ Google) have no password — sign those in once headed instead.

## Auth architecture

Greptile's dashboard (`app.greptile.com`) uses **Auth.js v5** (the rebrand of
NextAuth), fronted by an OAuth login service at `auth.greptile.com`: an
unauthenticated visit redirects to
`auth.greptile.com/login?login_challenge=…` — an email+password form plus
GitHub/GitLab/Google buttons. The session lands in
`__Secure-authjs.session-token` on `app.greptile.com`; both hosts share one
`greptile.com` browser tab, and the op prelude branches on which host the tab
settled on (`__onApp`) as the live auth signal.

The session response also carries a `greptileToken` (short-lived Bearer JWT)
for the backend at `api.greptile.com` — `backend_probe` calls it cross-origin
from the app tab, exactly as the real frontend does.

## People / Org API (reverse-engineered)

The organization settings page is a Next.js SPA at `/settings/organization/people`. It uses **tRPC** under the hood — every mutation/query goes through `/api/trpc/<procedure>` on `app.greptile.com`, run same-origin inside the tab.

- GET (queries): `/api/trpc/<procedure>?input=<urlencoded {"json":<args>}>`
- POST (mutations): `/api/trpc/<procedure>` with body `{"json":<args>}`
- Envelope (responses): `{"result":{"data":{"json":<payload>}}}` on success, `{"error":{"json":{"message":...,"code":...}}}` on failure

All procedures take `tenantExternalId` from the session's `currentTenantExternalId`. Arg shapes captured from call sites in the minified page bundle (chunk `164-*.js`).

| Operation | tRPC procedure | Method | Args |
|---|---|---|---|
| Session / identity | `/api/auth/session` (Auth.js) | GET | — |
| List members + invites | `organization.searchPeople` | GET | `{tenantExternalId, query, roles?, page, pageSize}` |
| Send email invite | `invitation.create` | POST | `{tenantExternalId, email, role}` |
| Get invite link | `invitation.getOrganizationInviteLink` | GET | `{tenantExternalId}` |
| Create/rotate invite link | `invitation.createOrganizationInviteLink` | POST | `{tenantExternalId, defaultRole}` |
| Revoke invite link | `invitation.revokeOrganizationInviteLink` | POST | `{tenantExternalId}` |
| Revoke pending invite | `invitation.revoke` | POST | `{email, tenantExternalId}` |
| Update member role | `organization.setMemberRole` | POST | `{tenantExternalId, email, role}` |
| Remove member | `organization.removeMember` | POST | `{email, tenantExternalId}` |

**Invite link format:** `https://app.greptile.com/invitation?token=<token>` — assembled in the "Copy Invite Link" button's onClick: `` `${appUrl}/invitation?token=${token}` ``.

**Roles:** `ADMIN` or `MEMBER` (enum `n.X` in the bundle). Accepted case-insensitively by the app.

**Namespace vs. organization:** the bundle also uses `namespace.*` procedures (`namespace.removeMember`, `namespace.updateMemberRole`, `namespace.addMember`) for **per-repo** access control. This app intentionally only wires the org-level procedures — remove from the org, not from a namespace.

There is also a separate backend at `https://api.greptile.com` (Express, Bearer JWT from `greptileToken` in the session). Not used by this app — member management lives entirely on the dashboard's tRPC route.

## Tools

- `check_session` — validate dashboard cookies, return identity.
- `list_members` — list all org members + pending invites (returns `account[]`).
- `send_invite` — send an email invite (`invitation.create`).
- `get_invite_link` — fetch the shareable org invite link (URL + token).
- `create_invite_link` — create or rotate the shareable link; `defaultRole` arg.
- `revoke_invite_link` — revoke the shareable org invite link entirely.
- `revoke_invite` — revoke a single pending email invite.
- `update_role` — change a member's role (ADMIN / MEMBER).
- `remove_member` — remove a member from the org.
- `login` / `logout` — drive the auth.greptile.com form / Auth.js signout in the tab.
- `probe`, `backend_probe` — reverse-engineering helpers (same-origin app fetch / cross-origin backend with the greptileToken). Leave in place while the API is still being mapped; don't ship them in a "stable" app.
