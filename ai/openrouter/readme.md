---
id: openrouter
services:
- http
name: OpenRouter
description: Unified AI gateway for models across providers via one API
color: '#111827'
website: https://openrouter.ai
privacy_url: https://openrouter.ai/privacy
terms_url: https://openrouter.ai/terms
---

# OpenRouter

OpenRouter lets AgentOS access models from multiple providers using one API key and one `chat` interface.

## Setup

1. Create an API key at https://openrouter.ai/keys
2. Add credential in AgentOS Settings -> Providers -> OpenRouter

## Usage

Use `chat` with a provider-qualified model ID:

```
run({ app: "openrouter", tool: "chat", params: {
  model: "openai/gpt-4o-mini",
  messages: [
    { role: "user", content: "Summarize this in one sentence: AgentOS is local-first." }
  ]
} })
```

The response is normalized to AgentOS' canonical shape:
`content`, `tool_calls`, `stop_reason`, and `usage`.
