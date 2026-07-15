---
id: vowen
services:
- shell
name: Vowen
description: Local speech-to-text — provides the transcribe service via whisper.cpp
color: '#5B21B6'
website: https://vowen.app
---

# Vowen

Local, on-device transcription. Provides the **`audio_transcription`** service by
driving the `whisper.cpp` stack (Metal-accelerated `whisper-cli` + ggml models)
bundled inside Vowen.app — no API keys, no cloud, no model download.

A caller never names this app: it asks the broker for `audio_transcription` and
the engine matchmakes here. The same interface is open to a future native macOS
Speech provider; callers wouldn't change.

## Requirements

Provided by an installed **Vowen.app** + Homebrew **ffmpeg**:

| Path | What |
|---|---|
| `/Applications/Vowen.app/Contents/Resources/bin/whisper-cli` | whisper.cpp CLI (Metal) |
| `~/Library/Application Support/vowen/models/ggml-*.bin` | ggml models (base, medium.en, large-v3) |
| `/opt/homebrew/bin/ffmpeg` | format conversion → 16 kHz mono wav |

Override any of these via the `cli` connection's `vars` (`binary`, `models_dir`, `ffmpeg`).

## Two faces, both decoupled

Vowen exposes a brokered **capability** and its own **data** — neither couples to
any other app:

| | What | How it's reached |
|---|---|---|
| `audio_transcription` (service) | transcribe any audio file | matchmaking — agents ask the broker, never name Vowen |
| dictation history (data) | the user's own voice log | direct tool call — Vowen-specific, no service |

## Tools

| Tool | Description |
|---|---|
| `list_models` | The ggml models on disk — the ids `transcribe` accepts (`base`, `medium.en`, `large-v3`) |
| `transcribe` | Transcribe an audio file → `transcript`. Provides `audio_transcription`. `model` required, no default |
| `list_dictations` | The user's recent Vowen dictations (`transcript[]`, newest first) — "what did I say?". Optional `query` substring filter |

## The voice log

`list_dictations` reads Vowen's own transcription history
(`transcription-history.json`) — every snippet the user has dictated into any
app, newest first. These are `transcript` nodes (`sourceType: dictation`). Vowen
does not retain the source audio, so dictations carry text only — no audio link.
This is Vowen's data, surfaced directly; it is not the brokered
`audio_transcription` service.

## How transcription writes to the graph

`transcribe` reads nothing. It shells whisper and returns a `transcript` whose
nested `file` child is identified by the audio's **content hash** (`sha`). When
the result is remembered, the engine upserts `file(sha) —transcribed_from→
transcript`. The audio's bytes are the join key, so the same voice note always
resolves to one file node → one transcript, regardless of path. The transcript
id is content-addressed (`transcript:<model>:<sha>`), so re-running on the same
audio with the same model upserts the same node — idempotent, no duplicates.

## Notes

- whisper-cli only accepts wav/mp3/flac/ogg; iMessage `.caf`/`.m4a` are converted
  to 16 kHz mono wav with ffmpeg first. Temp wav + json are written to the system
  temp dir and removed after each run.
- `language` defaults to `"auto"` — the model decides and we store exactly what it
  reports, never an override. English-only models (`*.en`) always report `en`;
  multilingual models (`base`, `large-v3`) detect the spoken language.
- Model choice is the speed/quality trade: `base` (~74M, fast), `medium.en`
  (~769M, the default Vowen quality for English), `large-v3` (~1.5G, best, slowest).
  A ~1 min note is ~10 s on `medium.en`.
