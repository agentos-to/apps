---
id: claude
services:
- http
- secrets
- shell
name: Claude
description: "Claude \u2014 Anthropic's AI model family. Inference via API or local\
  \ CLI, plus claude.ai chat history."
color: '#D97757'
website: https://claude.ai
privacy_url: https://www.anthropic.com/privacy
terms_url: https://www.anthropic.com/terms-of-service
product:
  name: Claude
  website: https://claude.ai
  developer: Anthropic
---

# Claude

One app for everything Claude. Four access modalities, one product.

| Connection | File | What it does |
|---|---|---|
| `api` | `claude_api.py` | Inference via the Claude API (Messages endpoint) |
| `code` | `claude_code.py` | Inference via the local `claude` CLI, plus reads local Claude Code state |
| `web` | `claude_web.py` | Browse/search/import claude.ai chat history |

Models are **never hardcoded**. All operations accept a `model` parameter that is
resolved through the graph (`list_models` on the relevant connection populates it).
See `docs/specs/done/no-hardcoded-models.md` for rationale.

## Usage

### `api` connection — Claude API inference

| Tool | Description |
|---|---|
| `list_models` | Fetch the current model catalog from `api.anthropic.com/v1/models` |
| `chat` | Send a single Messages API request. Supports tools, system prompts, temperature. Returns raw tool_use blocks for the caller to process. |

### `code` connection — Claude Code

Uses the user's logged-in `claude` binary (no API key required) AND reads local
on-disk state under `~/.claude/projects/`.

| Tool | Description |
|---|---|
| `chat_cli` | `@provides("chat")` — one `claude -p` completion, no `--mcp-config`. The no-loop twin of `agent`. (`_cli` suffix: `claude_api.chat` owns the flat `chat` name.) |
| `agent` | `@provides("agent")` — run Claude as a full agent loop via `claude -p`. Tool use + structured output via `--mcp-config` / `--json-schema`. |
| `list_models_cli` | List Claude models via the keychain OAuth token (no API key). Same endpoint as `list_models`, different auth. |
| `list_projects` | List every project directory under `~/.claude/projects/` with conversation counts and last activity. |
| `list_conversations_cli` | List local Claude Code conversations (one per JSONL transcript) as shape-native `conversation[]`. Optional `project` scope, optional `limit`. |
| `read_conversation_cli` | Read a full conversation transcript — returns one `conversation` with a nested `message[]` relation (content, blocks, author, published, tool calls). |

> **Note:** `claude -p` only ever returns a *final message* — it can't emit the
> request-style `tool_calls` an SDK loop consumes — so the `code` connection splits
> by return shape: `chat` `@provides("chat")` (a completion) + `agent`
> `@provides("agent")` (it loops internally over tool calls). The `api` connection's
> `chat` also `@provides("chat")`, so service routing picks by model + credentials.
>
> The `_cli` suffix on `list_models_cli` / `list_conversations_cli` /
> `read_conversation_cli` is because app tool names share a flat namespace
> across all `.py` files in the app, and the same names are already taken by
> the `api` and `web` connections (for the Anthropic API model list and
> claude.ai web chats, respectively).

### `web` connection — claude.ai chat history (browser-driven)

Every op runs same-origin `fetch()` inside a claude.ai tab of the engine-owned
browser (the Exa pattern). The session is the profile's `sessionKey` cookie —
never extracted, never vaulted.

| Tool | Description |
|---|---|
| `list_conversations` | Browse conversations, most recent first |
| `get_conversation` | Full conversation with all messages |
| `search_conversations` | Search by title (client-side filter) |
| `import_conversation` | Import messages into graph for FTS |
| `list_orgs` | Discover orgs and capabilities |
| `check_session` | Ask /api/organizations in the tab, return identity |
| `login` | Drive the email form → magic-link `auth_challenge` |
| `verify_login` | Navigate the magic-link URL in the tab to finish login |
| `logout` | POST claude.ai signout in the tab |

## Setup

### `api` connection
1. Get an API key from https://console.anthropic.com/settings/keys
2. Add credential in AgentOS Settings → Providers → Claude (API Key)

### `code` connection
Install Claude Code and log in:
```bash
curl -fsSL https://claude.ai/install.sh | bash    # or: brew install claude-code
claude auth login                                   # opens browser for OAuth
```
Works with Pro/Max/Team/Enterprise subscriptions. Once logged in, `claude_code.py`
uses that auth state directly — no key exchange with agentOS.

### `web` connection
Browser-driven — sign in to claude.ai once in the AgentOS browser profile, or
run `claude.login` (drives the email form, returns a magic-link `auth_challenge`;
read the link from your inbox and call `verify_login`). No credential is stored;
the session is the browser profile itself.
