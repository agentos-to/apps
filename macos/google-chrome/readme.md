---
id: google-chrome
name: Google Chrome
description: "CDP access to Google Chrome — debug-attachable sessions for the engine's browser_session host, attach or launch"
services:
  - http
  - shell
color: "#4285F4"
website: "https://www.google.com/chrome/"
product:
  name: Google Chrome
  website: https://www.google.com/chrome/
  developer: Google LLC
---

# Google Chrome

Chrome DevTools Protocol access to Google Chrome. Provides the
`cdp_access` service — the layer the engine's `browser_session` system
app consumes to host live, debug-attachable browser sessions. Any
Chromium-family browser app can provide the same service (Brave ships
the identical shape); the user's default app for the service decides
which browser hosts sessions.

## Requirements

- **macOS only** — launches via LaunchServices (`open -na`)
- **Google Chrome installed**

## Modes

- **attach** — find the user's daily Chrome already running with
  `--remote-debugging-port`. If it isn't, returns a structured
  `NeedsDebugBrowser` error carrying the exact relaunch command.
- **launch** — spawn (or reuse) an engine-owned Chrome instance with
  its own profile at `~/.agentos/browsers/chrome`. A dedicated,
  always-on session host that never shares fate with the user's
  daily browsing. Reuse is keyed on the profile's `DevToolsActivePort`
  answering `/json/version`; a frozen instance is killed and waited
  out before relaunch.

## Usage

```
OPERATION       DESCRIPTION
-----------     -------------------------------------------------------
cdp_connect     Return {ws_url, target_id, browser_version, tabs} for a
                debug-attachable Chrome (mode: attach | launch)
login_window    Headed app-mode sign-in window on the AgentOS profile
                ({url, label?} opens, {close: true} → back to headless)
```

Consumers should almost never call this directly — ask for the
`browser_session` service instead and let the engine hold the socket.

## Headed login (`login_window`)

When a login needs a human (SSO, MFA, CAPTCHA), `login_window(url,
label?)` swaps the engine-owned profile — named **"AgentOS"** — from
headless to a headed Chromium app-mode window (`--app=<url>`). The
human signs in, the agent polls the target app's `check_session`, then
`login_window(close=true)` swaps back to headless. One profile, two
modes; the session persists on disk across every swap — no cookie
copying. Agents reach it through the `login_window` *service*
(provided by the OS browser app), which resolves `cdp_access` once so
the window always opens on the session host's exact profile.
