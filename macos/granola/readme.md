---
id: granola
services:
  - http
name: Granola
description: "Meeting transcripts, AI summaries, and Q&A conversations from Granola"
color: "#2D6A4F"
website: "https://granola.ai"
privacy_url: "https://granola.ai/privacy"
product:
  name: Granola
  website: https://granola.ai
  developer: Granola
---

# Granola

Meeting transcripts and AI summaries from [Granola](https://granola.ai) — automatically captured as you meet.

## Setup

Granola must be installed and have run at least once. No API key needed — auth is read directly from Granola's local token file.

If you see auth errors, open the Granola app to refresh the token.

## Connections (API + cache)

The app declares two **connections** — `api` (live REST) and `cache` (local `cache-v6.json`). For operations that support both, pick one with `params.connection`: `"api"` or `"cache"`. The default is `api` (first in the list).

| Connection | When | Use case |
|------------|------|----------|
| **api** | Live calls with token | Freshest data, full transcripts |
| **cache** | Reads local cache file | Fast, works offline |

`get_meeting` uses the **api** connection only (the cache does not store full transcript text).

## What gets created in the graph

For each `get_meeting` call:

| Entity | Type | Details |
|--------|------|---------|
| The meeting | `meeting` | Title, times, location, AI summary as description |
| Transcript | `transcript` | Full text body (FTS5-indexed), segment count, duration |

Relationships:
- `transcript --transcribe--> meeting`

Attendees are stored in `data.attendees` on the meeting entity. Full person entity creation from attendees is a future enhancement.

## Usage

### `list_meetings` — Browse recent meetings

```
run({ app: "granola", tool: "list_meetings", params: { limit: 10 } })
```

Returns meetings with title, times, location, attendees. No transcript data — use `get_meeting` for that.

### `get_meeting` — Full meeting with transcript

```
run({ app: "granola", tool: "get_meeting", params: { id: "2fbc5ac2-..." } })
```

Returns the full meeting including:
- Transcript as a linked `transcript` entity (FTS5-indexed)
- AI summary as the meeting `description`
- All attendees with enrichment (name, avatar, job title)

## Transcript format

Transcripts are stored as plain text in the `transcript` entity body:

```
[00:02:34] You: What's the status on the deployment?
[00:02:41] Other: We hit a snag with the Docker config, but it's fixed now.
[00:02:58] You: Great. What's the timeline?
```

Speaker labels: `You` (microphone) and `Other` (system audio).

## Q&A conversations

Granola lets you chat with AI about meeting transcripts. Each meeting can have one or more Q&A threads.

### What this is on the graph

Each thread is a **`conversation` entity** — the same entity kind used elsewhere on the graph for chat threads and ordered message histories (email threads, assistant chats, SMS-style threads, etc.). AgentOS cares *what* it is (a named thread with messages and a URL), not which product produced it. `list_conversations` returns thin rows (title, ids, `notes_url`); `get_conversation` fills `text` / `content` by joining message bodies. The `document_id` you pass is the **meeting** document id in Granola; treat it as “which meeting this Q&A is about” when reasoning, even though the remembered entity type for the thread itself is still `conversation`.

### Workflow: Find AI chats about a meeting

1. **Get the meeting id** — `list_meetings` (or use the id from a meeting summary)
2. **List threads** — `list_conversations(document_id: meeting_id)` → returns thread(s) with titles and ids
3. **Read a thread** — `get_conversation(thread_id: thread_id)` → full user/assistant message history

Via AgentOS MCP:

```
run({ app: "granola", tool: "list_meetings", params: { limit: 10 } })
run({ app: "granola", tool: "list_conversations", params: { document_id: "<id from step 1>" } })
run({ app: "granola", tool: "get_conversation", params: { thread_id: "<id from step 2>" } })
```

### `list_conversations` — Q&A threads for a meeting

```
run({ app: "granola", tool: "list_conversations", params: { document_id: "6dc09094-1c7e-421d-86c1-2b23f924b34e" } })
```

Returns threads linked to that meeting (e.g. "Validator Agent and Auto Rewrite Loop"). Use `get_conversation` with a thread ID to read the full exchange.

### `get_conversation` — Full Q&A thread

```
run({ app: "granola", tool: "get_conversation", params: { thread_id: "5c4516ae-1224-4e39-a642-a1b9b7e0e279" } })
```

Returns the thread with all user/assistant messages in order.
