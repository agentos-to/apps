"""Vowen — local speech-to-text via the whisper.cpp stack bundled with Vowen.app.

Provides the `transcribe` service. No new binary, no model download: it drives
the `whisper-cli` (whisper.cpp, Metal) and the ggml models that ship inside
Vowen.app, converting non-PCM inputs (iMessage .caf/.m4a, etc.) to 16 kHz mono
wav with ffmpeg first.

A transcription reads nothing from the graph — it shells whisper and returns a
`transcript`. The transcript carries a nested `file` child (identified by the
audio's content hash), so the engine upserts `file(sha) —transcribed_from→
transcript` as a side effect of remembering the result. The audio's bytes are
the join key: the same voice note always resolves to one file node → one
transcript, regardless of path.
"""

import hashlib
import json
import os
import re
import tempfile

from agentos import connection, provides, returns, shell, test, timeout


WHISPER_CLI = "/Applications/Vowen.app/Contents/Resources/bin/whisper-cli"
MODELS_DIR = "~/Library/Application Support/vowen/models"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
HISTORY = "~/Library/Application Support/vowen/transcription-history.json"


connection(
    'cli',
    description='whisper.cpp CLI + ggml models bundled in Vowen.app, ffmpeg for format conversion',
    vars={
        'binary': '/Applications/Vowen.app/Contents/Resources/bin/whisper-cli',
        'models_dir': '~/Library/Application Support/vowen/models',
        'ffmpeg': '/opt/homebrew/bin/ffmpeg',
        'history': '~/Library/Application Support/vowen/transcription-history.json',
    })


# ── Connection helpers ────────────────────────────────────────────────────────

def _var(connection: dict | None, key: str, default: str) -> str:
    if isinstance(connection, dict):
        v = (connection.get("vars") or {}).get(key)
        if v:
            return str(v)
    return default


def _binary(connection: dict | None) -> str:
    return _var(connection, "binary", WHISPER_CLI)


def _models_dir(connection: dict | None) -> str:
    return os.path.expanduser(_var(connection, "models_dir", MODELS_DIR))


def _ffmpeg(connection: dict | None) -> str:
    return _var(connection, "ffmpeg", FFMPEG)


def _history_path(connection: dict | None) -> str:
    return os.path.expanduser(_var(connection, "history", HISTORY))


_EXT_MIME = {
    ".caf": "audio/x-caf", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg", ".opus": "audio/ogg; codecs=opus",
    ".wav": "audio/wav", ".aac": "audio/aac", ".flac": "audio/flac",
}

_WHISPER = {"shape": "product", "name": "Whisper", "url": "https://github.com/ggml-org/whisper.cpp"}


# ── List models ───────────────────────────────────────────────────────────────

@test
@returns("model[]")
@connection("cli")
async def list_models(connection: dict | None = None, **params) -> list[dict]:
    """List the ggml transcription models on disk — the models this provider serves.

    Model id is the name a caller passes to `transcribe` (e.g. "medium.en",
    "large-v3"), derived from the `ggml-<name>.bin` files in the models dir.
    """
    models_dir = _models_dir(connection)
    out = []
    if os.path.isdir(models_dir):
        for fn in sorted(os.listdir(models_dir)):
            if not (fn.startswith("ggml-") and fn.endswith(".bin")):
                continue
            name = fn[len("ggml-"):-len(".bin")]
            path = os.path.join(models_dir, fn)
            out.append({
                "id": name,
                "name": name,
                "at": _WHISPER,
                "format": "ggml",
                "modelType": "transcription",
                "size": f"{os.path.getsize(path) // (1024 * 1024)} MB",
            })
    return out


# ── Dictation history (Vowen's own voice log) ─────────────────────────────────

@test(params={"limit": 3})
@returns("transcript[]")
@connection("cli")
async def list_dictations(*, limit: int = 20, query: str = None,
                          connection: dict | None = None, **params) -> list[dict]:
    """List the user's recent Vowen dictations — their own spoken voice log.

    Reads Vowen's transcription history (newest first) — every snippet the user
    has dictated into any app. This is Vowen's own data, not a brokered service:
    it answers "what were the last few things I said?". The source audio is not
    retained by Vowen, so dictations carry text only — no audio link.

    Args:
        limit: Max dictations to return (default 20, newest first).
        query: Optional case-insensitive substring to filter the text.
    """
    path = _history_path(connection)
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        entries = json.load(f)

    out = []
    needle = (query or "").lower()
    for e in entries:
        text = e.get("text") or ""
        if needle and needle not in text.lower():
            continue
        out.append({
            "id": f"dictation:{e.get('id')}",
            "name": text[:60] + ("…" if len(text) > 60 else ""),
            "content": text,
            "published": e.get("timestamp"),
            "sourceType": "dictation",
        })
        if len(out) >= limit:
            break
    return out


# ── Transcribe ────────────────────────────────────────────────────────────────

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _segments(transcription: list) -> tuple[list, str]:
    """Whisper JSON `transcription[]` → WebVTT-style cues + joined text."""
    cues, parts = [], []
    for seg in transcription:
        text = (seg.get("text") or "").strip()
        off = seg.get("offsets") or {}
        cues.append({
            "start": (off.get("from") or 0) / 1000.0,
            "end": (off.get("to") or 0) / 1000.0,
            "text": text,
        })
        if text:
            parts.append(text)
    return cues, " ".join(parts)


@test.skip(reason="needs a real audio file path")
@provides("audio_transcription")
@returns("transcript")
@connection("cli")
@timeout(600)
async def transcribe(*, audio: str, model: str, language: str = "auto",
                     connection: dict | None = None, **params) -> dict:
    """Transcribe an audio file to a timestamped transcript via whisper.cpp.

    Converts the input to 16 kHz mono wav (ffmpeg) — whisper-cli only accepts
    wav/mp3/flac/ogg, so .caf/.m4a are converted first — then runs whisper and
    maps its JSON onto the `transcript` shape. The transcript carries a nested
    `file` child keyed by the audio's content hash, so remembering the result
    writes `file(sha) —transcribed_from→ transcript` to the graph.

    Args:
        audio: Path to the audio file to transcribe.
        model: Model id (e.g. "medium.en", "base", "large-v3") — required.
        language: Force a language code, or "auto" (default) to let the model
            decide. We never override what the model reports — English-only
            models (.en) always report "en"; multilingual models detect it.
    """
    audio = os.path.expanduser(audio)
    if not os.path.isfile(audio):
        raise FileNotFoundError(f"audio file not found: {audio}")

    model_path = os.path.join(_models_dir(connection), f"ggml-{model}.bin")
    if not os.path.isfile(model_path):
        raise FileNotFoundError(
            f"model {model!r} not found at {model_path} — call list_models for available models")

    sha = _sha256(audio)
    basename = os.path.basename(audio)
    ext = os.path.splitext(basename)[1].lower()

    work = os.path.join(tempfile.gettempdir(), f"vowen-{sha}")
    wav = f"{work}.wav"
    try:
        conv = await shell.run(_ffmpeg(connection), args=[
            "-y", "-i", audio, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav,
        ], timeout=120)
        if conv["exit_code"] != 0:
            raise RuntimeError(f"ffmpeg failed: {conv['stderr'].strip()[-500:]}")

        # No -np: whisper prints the language-detection line ("auto-detected
        # language: en (p = 0.99)") to stderr, and that confidence lives
        # nowhere in the JSON — stderr is the only place to read it.
        res = await shell.run(_binary(connection), args=[
            "-m", model_path, "-f", wav, "-oj", "-of", work, "-l", language,
        ], timeout=580)
        if res["exit_code"] != 0:
            raise RuntimeError(f"whisper-cli failed: {res['stderr'].strip()[-500:]}")

        with open(f"{work}.json") as f:
            data = json.load(f)
    finally:
        for p in (wav, f"{work}.json"):
            try:
                os.remove(p)
            except OSError:
                pass

    cues, full_text = _segments(data.get("transcription") or [])
    # Store exactly what the model reports — never the input flag.
    detected = (data.get("result") or {}).get("language")
    # Confidence is present only when the model actually auto-detected the
    # language (multilingual + "auto"); English-only models report none.
    conf = re.search(r"auto-detected language:\s*\S+\s*\(p\s*=\s*([0-9.]+)\)",
                     res.get("stderr") or "")
    language_confidence = float(conf.group(1)) if conf else None

    return {
        "id": f"transcript:{model}:{sha}",
        "name": f"Transcript of {basename}",
        "language": detected,
        "languageConfidence": language_confidence,
        "content": full_text,
        "segments": cues,
        "segmentCount": len(cues),
        "durationMs": int(cues[-1]["end"] * 1000) if cues else 0,
        "sourceType": "audio",
        "contentRole": "transcript",
        "transcribed_from": {
            "shape": "file",
            "sha": sha,
            "path": audio,
            "filename": basename,
            "mimeType": _EXT_MIME.get(ext),
        },
    }
