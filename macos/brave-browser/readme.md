---
id: brave-browser
services:
  - crypto
  - http
  - secrets
  - shell
  - sql
name: Brave Browser
description: "Browsing history, bookmarks, and cookies from Brave Browser on macOS — including session key extraction for claude.ai"
color: "#F83B1D"
website: "https://brave.com"
accounts:
  list_via: list_accounts
  id_field: name
---

# Brave Browser

Access browsing history, bookmarks, cookies, and session credentials from Brave Browser's local databases.

Brave is Chromium-based, so it uses the same cookie encryption scheme as Chrome:
AES-128-CBC with a key derived via PBKDF2 from a master password stored in macOS Keychain.

## Requirements

- **macOS only** — reads local SQLite databases
- **Brave Browser installed** — databases must exist at the standard paths
- **Full Disk Access** — System Settings > Privacy & Security > Full Disk Access (for the process reading the databases)
- **Brave closed (for cookies)** — SQLite WAL lock; or use `cookie_get` which copies to `/tmp`

## Data Sources

```
History  ~/Library/Application Support/BraveSoftware/Brave-Browser/Default/History
Cookies  ~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies
```

## Cookie Decryption

Brave encrypts cookie values on macOS using:
1. A master password stored in macOS Keychain under `"Brave Safe Storage"` / account `"Brave"`
2. PBKDF2-HMAC-SHA1 (salt: `saltysalt`, 1003 iterations, 16-byte key)
3. AES-128-CBC (IV: 16 space bytes = `20` repeated 16 times in hex)
4. The encrypted value has a 3-byte `v10` prefix that must be stripped before decryption

The `get_cookie_key` operation handles steps 1-2. The `cookie_get` operation does the full pipeline.

## Cookie Extraction

Extract decrypted cookies for any domain. Consumed through cookie provider matchmaking at runtime.

```
run({ app: "brave-browser", tool: "cookie_get", params: { domain: ".claude.ai", names: "sessionKey" } })
→ { domain: ".claude.ai", cookies: [{name: "sessionKey", value: "sk-ant-...", httpOnly: true, ...}], count: 1 }

run({ app: "brave-browser", tool: "cookie_get", params: { domain: ".chase.com" } })
→ { domain: ".chase.com", cookies: [...], count: 5 }
```

## Usage

```
OPERATION          DESCRIPTION
--------------     -------------------------------------------------------
list_webpages      Recently visited pages from Brave history
search_webpages    Search browsing history by URL or title

OPERATION          DESCRIPTION
--------------     -------------------------------------------------------
list_accounts      List Brave profiles with display names
get_cookie_key     Derive AES-128 key from Keychain (PBKDF2)
list_cookies       List raw (encrypted) cookies for a domain
cookie_get         Full pipeline: extract + decrypt any cookies for a domain

OPERATION          DESCRIPTION
--------------     -------------------------------------------------------
cdp_connect        CDP WebSocket URL (the cdp_access provider)
login_window       Headed app-mode sign-in window on the AgentOS profile
```

## The engine-owned instance (`cdp.py`)

`cdp_connect(mode="launch")` runs a separate, engine-owned Brave on its
own profile at `~/.agentos/browsers/brave` — headless (`--headless=new`),
named **"AgentOS"**, never sharing fate with the user's daily Brave.
This is the session host every browser-driven connector rides.

When a login needs a human (SSO, MFA, CAPTCHA), `login_window(url,
label?)` swaps the same profile to a headed Chromium app-mode window
(`--app=<url>`); the human signs in, the agent polls the target app's
`check_session`, then `login_window(close=true)` swaps back to
headless. One profile, two modes — the session persists on disk across
every swap; no cookie copying. Agents reach it through the
`login_window` *service* (provided by the OS browser app), which
resolves `cdp_access` once so the window always opens on the session
host's exact profile.
