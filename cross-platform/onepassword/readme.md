---
id: onepassword
name: 1Password
description: >
  Local 1Password vault connector (B5 sqlite decrypt via AgentOS crypto).
  Provides login_credentials / api_key for matchmaking, and imports Secure
  Notes, cards, identities, government IDs, memberships, servers, and
  software licenses onto the graph as typed shapes.
services:
  - sql
  - crypto
  - secrets
color: "#0572EC"
website: "https://1password.com/"
---

# 1Password

**Local-first.** Reads your on-disk `1password.sqlite` directly. No
`op` CLI, no desktop Integrate IPC, no Python SDK / native dylib.

Crypto primitives run in the AgentOS engine (`crypto.pbkdf2_sha256`,
`crypto.aes_gcm`, `crypto.rsa_oaep`, `crypto.hkdf`). Unlock material is
stored per-user in the AgentOS credential store (`op.local`).

## Setup (once per user)

Call without a password — AgentOS opens the **AgentOS Security** window
so you type the Master Password there. The Secret Key is loaded from the
1Password app on this Mac when possible; agents never see either secret.

```
setup_local_unlock()
```

That returns `{ status: "pending", challengeId, prompt: "secret_challenge" }`.
After you Unlock in the desktop window, secrets land in `op.local` and
later tools can unlock the vault.

App-developer reference for this pattern (any plugin, not just 1Password):
`read({id:"apps-secret-challenges", volume:"system"})` — authored at
`core/system-docs/apps/secret-challenges.md`.

Legacy / tests (agent must never be given production secrets this way):

```
setup_local_unlock(master_password="…", secret_key="A3-…")
```

macOS DB path (automatic):
`~/Library/Group Containers/2BUA8C4S2C.com.1password/Library/Application Support/1Password/Data/1password.sqlite`

## Credential providers

| Service | Tool | Returns |
|---|---|---|
| `login_credentials` | `get_credentials(domain=)` | `{email,password}` via `__secrets__` |
| `api_key` | `get_api_key(service=)` | `{key}` via `__secrets__` |

Used by `credentials.retrieve(...)`. After the first successful pull,
Vault is tried first on later calls.

## Graph import

| Tool | Shape | Deterministic links |
|---|---|---|
| `get_login(q=)` | `account` | `for_site → website`; `at → organization` |
| `get_secure_note(q=)` | `secure_note` | body only in `__secrets__` |
| `get_credit_card(q=)` | `payment_method` | PCI-safe last4/brand; PAN/CVV in `__secrets__` |
| `get_identity(q=)` | `person` | identities[] |
| `get_government_id(q=)` | `government_id` | `held_by → person` |
| `get_membership(q=)` | `membership` | `at → organization` |
| `get_server(q=)` | `server` | `about → website` when host looks like FQDN |
| `get_software_license(q=)` | `software_license` | `licenses → software` |
| `list_items(category?, q?)` | overview json | titles/urls only |

Passkeys are **not** supported yet.

## Security

- Never log master password, secret key, passwords, note bodies, PAN/CVV.
- Unlock secrets: domain `op.local`. Imported item secrets: `op.vault`.
- Derived vault keys stay in the Python worker for a **30-minute sliding
  TTL** (re-derived from `op.local` after expiry — no re-prompt while the
  unlock row exists). Biometric gate on reading `op.local` is planned later.

## Why not Integrate / official MCP / opcli

| Path | Why not |
|---|---|
| Desktop Integrate + `op` | Chronically flaky IPC |
| Labs MCP | Environments only — no vault Login plaintext |
| Vendored `opcli` | Third-party binary; we own the decrypt instead |
