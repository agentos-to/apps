# AgentOS Apps

Apps — Python adapters that connect AgentOS to third-party platforms
(GitHub, Google, iMessage, Brave, etc.) and expose agent-only tools
(LLM, web search, file system). Written against the Python **App
SDK** at [`platform/sdk/python/`](../platform/sdk/python).

[agentos.to](https://agentos.to) · [agentos.to/apps](https://agentos.to/apps/)

## What is AgentOS?

A local operating system for human-AI collaboration, built for agents
first. The engine speaks MCP so any MCP-capable agent (Claude Code,
Cursor, etc.) can use AgentOS as its tool surface. Your data stays on
your machine.

Apps are how services arrive. An app declares what it
**provides** — `@provides("chat")`, `@provides("web_search")`,
`@provides(file_system)` — and the engine matchmakes requests to the
best available provider. Callers ask for a service, not a specific
app.

## What's here

```
agents/         Agent-only tools (code-review, problem-solving, …)
ai/             LLM providers (anthropic, openai, …)
comms/          Messaging (imessage, whatsapp, slack, …)
finance/        Banking, budgeting, payments
hosting/        DNS, deployment (porkbun, vercel, …)
logistics/      Ride-share, delivery, travel
media/          Books, music, video (goodreads, spotify, …)
productivity/   Calendar, tasks, notes
web/            Browsers, search, scraping
```

Each top-level category is flat so the repo doubles as a browse-able
catalog — clone and you immediately see every app.

## Getting started

```bash
git clone https://github.com/agentos-to/apps
git clone https://github.com/agentos-to/site platform   # contract + SDKs + docs
cd apps
pip install -e ../platform/sdk/python         # ships the validator
git config core.hooksPath bin/git-hooks       # pre-commit + code review
```

Useful commands:

```bash
agent-sdk validate                  # lint every app in this repo
agent-sdk validate exa              # single app
agent-sdk validate --sandbox        # only the banned-import sandbox check
agent-sdk new-app my-app            # scaffold a new app
agent-sdk shapes                    # list available shapes
```

Full authoring guide at
[agentos.to/apps](https://agentos.to/apps/).

## Sibling repos

| Repo                                                       | Lang         | What |
| ---------------------------------------------------------- | ------------ | ---- |
| [`core`](https://github.com/agentos-to/core)               | Rust         | The engine, CLI, MCP server |
| [`platform`](https://github.com/agentos-to/site)           | Py·TS·Astro  | The contract — ontology, codegen, the Python + TypeScript SDKs, docs site → [agentos.to](https://agentos.to) |
| **`apps`** (this repo)                                     | Python       | Apps — adapters for third-party platforms |

## Contributing

Anyone can contribute. Found a bug? Want a new app?
[Open an issue](https://github.com/agentos-to/apps/issues) or a PR.

## License

MIT — see [LICENSE](LICENSE). By contributing you grant AgentOS the
right to use your contributions in official releases, including
commercial offerings. Your code stays open forever.
