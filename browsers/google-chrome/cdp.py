"""Google Chrome — CDP access provider.

Implements the `cdp_access` service: *"give me a CDP WebSocket URL
to a debug-attachable Chromium browser."* Tiny surface — one tool,
`cdp_connect`. The caller (the engine's `browser_session` system app)
opens its own WebSocket from the returned URL and drives CDP from
there. This module knows Chrome-specific things (profile paths,
DevToolsActivePort, launch flags, debug port); it knows nothing
about what callers do with the session.

Matchmaking picks the provider: any Chromium-family browser app can
provide `cdp_access` (Brave ships the same shape), the user's default
app for the service wins, and consumers never name a browser. Zero
cross-app imports; the engine brokers every boundary.

Two modes. **attach**: Chrome must already be running with
`--remote-debugging-port=<port>`; if it isn't, we return a
structured `NeedsDebugBrowser` error with the exact relaunch
command. **launch**: spawn (or reuse) an engine-owned Chrome
instance with its own profile at `~/.agentos/browsers/chrome` —
a dedicated, always-on session host that doesn't share fate with
the user's daily browser. `open -na` detaches via LaunchServices,
so the instance survives the app call.
"""

import asyncio
import json
import os
from typing import Any

from agentos import (
    cdp_access,
    client,
    connection,
    provides,
    returns,
    shell,
    app_error,
    timeout,
)


# ──────────────────────────────────────────────────────────────────────
# Chrome-specific constants
# ──────────────────────────────────────────────────────────────────────

_CHROME_BASE = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome"
)

# DevToolsActivePort is a two-line file Chromium writes into the user
# data directory when launched with --remote-debugging-port. Line 1 is
# the port (auto-assigned when the flag is given `0`); line 2 is an
# opaque token that's part of the WebSocket path for initial handshake
# (we don't use it — /json/version gives us the canonical URL).
_DEVTOOLS_ACTIVE_PORT_FILE = os.path.join(_CHROME_BASE, "DevToolsActivePort")

# Launch mode runs an engine-owned instance against its own profile so
# the session host never shares fate with the user's daily browser.
# Chromium treats user-data-dir as the instance key: same dir = same
# instance, different dir = a genuinely separate process.
_AGENTOS_PROFILE = os.path.expanduser("~/.agentos/browsers/chrome")
_AGENTOS_PORT_FILE = os.path.join(_AGENTOS_PROFILE, "DevToolsActivePort")


# ──────────────────────────────────────────────────────────────────────
# Connection — public, no auth. We only hit the local debug endpoint.
# ──────────────────────────────────────────────────────────────────────

connection(
    "cdp",
    description="Chrome's local CDP HTTP endpoint — /json/version etc. "
                "No auth (loopback-only by Chromium's design).",
    client="fetch",
)


# ──────────────────────────────────────────────────────────────────────
# Port discovery
# ──────────────────────────────────────────────────────────────────────

def _read_devtools_active_port(
    port_file: str = _DEVTOOLS_ACTIVE_PORT_FILE,
) -> int | None:
    """Read the auto-assigned port Chrome wrote to DevToolsActivePort.

    File exists iff Chrome launched with `--remote-debugging-port`.
    Missing file = Chrome not in debug mode (the common case — users
    don't launch with debug flags by default). Chromium leaves the
    file behind after a crash, so a readable port is only a *candidate*
    — callers must confirm with /json/version.
    """
    if not os.path.exists(port_file):
        return None
    try:
        with open(port_file, "r") as f:
            first_line = f.readline().strip()
        return int(first_line) if first_line else None
    except (OSError, ValueError):
        return None


async def _fetch_version(port: int) -> dict[str, Any] | None:
    """GET /json/version on the debug port. Returns Chromium's version
    info plus — critically — `webSocketDebuggerUrl`, the canonical WS
    endpoint for the browser-level target (not a page target).

    Returns None on any failure — the caller turns that into a
    structured error with the right code (NeedsDebugBrowser vs
    CDPConnectFailed).
    """
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/json/version",
                                 timeout=3.0)
    except Exception:
        return None
    if resp.get("status") != 200:
        return None
    body = resp.get("json") or {}
    return body if isinstance(body, dict) else None


async def _fetch_targets(port: int) -> list[dict[str, Any]]:
    """GET /json to list all CDP targets (pages, workers, iframes).

    Each target has {id, type, title, url, webSocketDebuggerUrl}.
    Callers that want a specific tab (by URL or title) pick from this
    list. Browser-level control uses the /json/version endpoint
    instead, which is a different, durable target.
    """
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/json",
                                 timeout=3.0)
    except Exception:
        return []
    if resp.get("status") != 200:
        return []
    body = resp.get("json")
    return body if isinstance(body, list) else []


# ──────────────────────────────────────────────────────────────────────
# Launch mode — the engine-owned instance
# ──────────────────────────────────────────────────────────────────────

async def _ensure_agentos_instance() -> int | dict[str, Any]:
    """Return the debug port of the engine-owned Chrome, launching it
    if needed. Returns the port on success, a `app_error` dict on
    failure.

    Reuse before launch: if the profile's DevToolsActivePort names a
    port that answers /json/version, that's our instance — `open -na`
    with the same user-data-dir would only flash a new window at it.
    """
    candidate = _read_devtools_active_port(_AGENTOS_PORT_FILE)
    if candidate is not None and await _fetch_version(candidate) is not None:
        return candidate

    # No (responsive) instance. A frozen or half-dead one would shadow
    # `open -na`: until its SingletonLock is released, a new process
    # with the same user-data-dir defers to it and exits without ever
    # rewriting DevToolsActivePort. Kill and *wait for actual exit*
    # before launching — SIGTERM first (clean profile shutdown),
    # SIGKILL if it lingers. pkill/pgrep exit 1 on no match.
    pattern = f"user-data-dir={_AGENTOS_PROFILE}"
    await shell.run("pkill", args=["-f", pattern])
    for attempt in range(10):
        alive = await shell.run("pgrep", args=["-f", pattern])
        if alive.get("exit_code") != 0:
            break
        if attempt == 5:
            await shell.run("pkill", args=["-9", "-f", pattern])
        await asyncio.sleep(0.5)
    # Remove the stale port file so the poll below only ever reads the
    # port the new process writes.
    try:
        os.remove(_AGENTOS_PORT_FILE)
    except OSError:
        pass
    os.makedirs(_AGENTOS_PROFILE, exist_ok=True)

    # Headless session host: `--headless=new` is the real browser without
    # a window (≥112 reports a plain `Chrome/` UA, so a windowed-linked
    # profile survives the flip). `open -na` detaches via LaunchServices
    # and honors `--headless=new`.
    launched = await shell.run("open", args=[
        "-na", "Google Chrome", "--args",
        f"--user-data-dir={_AGENTOS_PROFILE}",
        "--remote-debugging-port=0",
        "--headless=new",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ])
    if launched.get("exit_code", 1) != 0:
        return app_error(
            "Failed to launch Chrome via `open -na`. Is Google Chrome "
            "installed?",
            code="LaunchFailed",
            result=launched,
        )

    # Chrome writes DevToolsActivePort once the debug listener is up —
    # typically 1-3s cold.
    for _ in range(40):
        await asyncio.sleep(0.5)
        port = _read_devtools_active_port(_AGENTOS_PORT_FILE)
        if port is not None and await _fetch_version(port) is not None:
            return port

    return app_error(
        "Launched Chrome but its debug port never came up within 20s. "
        f"Check whether a window appeared and whether "
        f"{_AGENTOS_PORT_FILE} exists.",
        code="LaunchFailed",
        profile=_AGENTOS_PROFILE,
    )


# ──────────────────────────────────────────────────────────────────────
# The tool
# ──────────────────────────────────────────────────────────────────────

@returns({
    "ws_url": "string",
    "target_id": "string",
    "browser_version": "string",
    "attached_to": "string",
    "tabs": "array",
})
@provides(
    cdp_access,
    description="Chrome DevTools Protocol access to a running Google "
                "Chrome browser. Requires Chrome launched with "
                "--remote-debugging-port. Returns a WebSocket URL for "
                "the browser-level CDP target plus the current tabs "
                "list so callers can pick a page target if needed.",
)
@connection("cdp")
@timeout(30)
async def cdp_connect(
    *,
    mode: str = "attach",
    port: int | None = None,
    **params,
) -> dict[str, Any]:
    """Return a CDP WebSocket URL for a debug-attachable Chrome.

    Args:
        mode: `"attach"` finds a running Chrome with a debug port (the
            user's daily browser, relaunched with the flag). `"launch"`
            spawns — or reuses — the engine-owned instance with its own
            profile at `~/.agentos/browsers/chrome`; use this for
            always-on session hosts that must not share fate with the
            user's browsing.
        port: Optional specific port to try (attach mode only). When
            `None`, we read `DevToolsActivePort` — the file Chrome
            writes inside its user-data dir when debug is enabled.

    Returns a shape the `browser_session` provider (or any other
    caller) can use to open its own WebSocket:
        {
          ws_url:          browser-level CDP endpoint
          target_id:       browser target id (opaque)
          browser_version: "HeadlessChrome/..." or "Chrome/..."
          attached_to:     informative string ("Chrome on port 9222")
          tabs:            [{id, type, title, url, webSocketDebuggerUrl}]
        }

    Structured errors (surfaced via `app_error`):
        - NeedsDebugBrowser: attach mode, no debug port found. Message
          includes the exact relaunch command.
        - CDPConnectFailed: port found but /json/version failed
          (browser frozen, protocol mismatch).
        - LaunchFailed: launch mode couldn't start the instance or its
          debug port never came up.
        - UnsupportedMode: mode not in {attach, launch}.
    """
    if mode not in ("attach", "launch"):
        return app_error(
            f"Mode {mode!r} not supported.",
            code="UnsupportedMode",
            mode=mode,
            supported=["attach", "launch"],
        )

    if mode == "launch":
        resolved = await _ensure_agentos_instance()
        if isinstance(resolved, dict):  # app_error envelope
            return resolved
        resolved_port = resolved
    else:
        # Prefer an explicit port; otherwise read DevToolsActivePort.
        resolved_port = port if port is not None else _read_devtools_active_port()
    if resolved_port is None:
        return app_error(
            "Chrome is not running with --remote-debugging-port. "
            "Quit Chrome and relaunch it with the debug flag:\n\n"
            "    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
            "--remote-debugging-port=9222\n\n"
            "Then retry this call. Alternatively, pass `port=<N>` if "
            "you have a debug instance running on a known port.",
            code="NeedsDebugBrowser",
            help_command=(
                "/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
                "--remote-debugging-port=9222"
            ),
            devtools_file_checked=_DEVTOOLS_ACTIVE_PORT_FILE,
        )

    version = await _fetch_version(resolved_port)
    if version is None:
        return app_error(
            f"Found Chrome debug port {resolved_port} but /json/version "
            f"failed. The browser may be frozen, or the protocol may "
            f"have drifted. Try restarting Chrome.",
            code="CDPConnectFailed",
            port=resolved_port,
        )

    ws_url = version.get("webSocketDebuggerUrl") or ""
    if not ws_url:
        return app_error(
            f"Chrome responded on port {resolved_port} but omitted "
            f"webSocketDebuggerUrl. Unexpected — likely a Chromium "
            f"version incompatibility.",
            code="CDPConnectFailed",
            port=resolved_port,
            version=version,
        )

    # Browser-level target ID is embedded in the WS path:
    # ws://127.0.0.1:PORT/devtools/browser/<uuid>
    target_id = ws_url.rsplit("/", 1)[-1]
    browser_version = version.get("Browser") or "unknown"

    tabs = await _fetch_targets(resolved_port)

    owner = "engine-owned Chrome" if mode == "launch" else "Chrome"
    return {
        "ws_url": ws_url,
        "target_id": target_id,
        "browser_version": browser_version,
        "attached_to": f"{owner} on port {resolved_port}",
        "tabs": tabs,
    }
