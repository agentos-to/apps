"""OpenRouter — unified AI gateway for models across providers."""

import json
from datetime import datetime, timezone

from agentos import connection, provides, returns, test, client


connection(
    'api',
    base_url='https://openrouter.ai/api/v1',
    auth={'type': 'api_key', 'header': {'Authorization': '"Bearer " + .auth.key'}},
    label='API Key',
    help_url='https://openrouter.ai/keys')


API_BASE = "https://openrouter.ai/api/v1"

MODEL_ALIASES = {
    "opus": "anthropic/claude-opus-4-6",
    "sonnet": "anthropic/claude-sonnet-4-5",
    "haiku": "anthropic/claude-haiku-4-5-20251001",
    "gpt-4o": "openai/gpt-4o",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "llama3.3": "meta-llama/llama-3.3-70b-instruct",
}


def _auth_header(params: dict) -> dict:
    key = params.get("auth", {}).get("key", "")
    return {"Authorization": f"Bearer {key}"}


def _ts_to_iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _per_mtok(per_token) -> float | None:
    """OpenRouter prices per token (USD, as a string); the graph stores USD per
    1,000,000 tokens (the unit the engine's computed-cost path multiplies).
    None when the field is absent; a malformed present value raises — a bad
    price is loud, never silently coerced to 0."""
    if per_token in (None, ""):
        return None
    return float(per_token) * 1_000_000


@test
@returns("model[]")
@connection("api")
async def list_models(**params) -> list[dict]:
    """List available AI models from all providers via OpenRouter."""
    resp = await client.get(f"{API_BASE}/models", headers=_auth_header(params))
    results = []
    for m in (resp["json"] or {}).get("data", []):
        provider_slug = m.get("id", "").split("/")[0] if m.get("id") else None
        at = {"shape": "organization", "name": provider_slug.title()} if provider_slug else None
        pricing = m.get("pricing") or {}
        results.append({
            "id": m.get("id"),
            "name": m.get("name"),
            "at": at,
            "content": m.get("description"),
            "published": _ts_to_iso(m.get("created")),
            "modelType": "chat",
            "contextWindow": int(m["context_length"]) if m.get("context_length") else None,
            # The graph IS the engine's price table — populate it so a call the
            # provider didn't itself price can be costed from here.
            "pricingInput": _per_mtok(pricing.get("prompt")),
            "pricingOutput": _per_mtok(pricing.get("completion")),
        })
    return results


def _to_openai_msg(msg: dict) -> dict:
    """Convert agentOS message format to OpenAI format."""
    if msg.get("role") == "assistant" and msg.get("tool_calls"):
        return {
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("input", {})),
                    },
                }
                for tc in msg["tool_calls"]
            ],
        }
    elif msg.get("role") == "tool":
        return {
            "role": "tool",
            "tool_call_id": msg.get("tool_call_id"),
            "content": msg.get("content"),
        }
    return {"role": msg.get("role"), "content": msg.get("content")}


@test.skip(reason='destructive or unsupported — migrated from yaml')
@provides("chat", serves=["*"])
@returns({"content": "{'type': 'string', 'description': 'Text response from the model (null if tool calls only)'}", "tool_calls": "{'type': 'array', 'description': 'Tool calls the model wants to make'}", "stop_reason": "{'type': 'string', 'enum': ['end_turn', 'tool_use', 'max_tokens']}", "usage": "{'type': 'object', 'description': 'Token usage (input_tokens, output_tokens)'}"})
@connection("api")
async def chat(*, model: str, messages: list, tools: list = None, max_tokens: int = 4096, temperature: float = 0, system: str = None, **params) -> dict:
    """Send a chat completion request through OpenRouter."""
    model = MODEL_ALIASES.get(model, model)
    openai_messages = []
    if system:
        openai_messages.append({"role": "system", "content": system})
    openai_messages.extend(_to_openai_msg(m) for m in messages)

    body: dict = {
        "model": model,
        "messages": openai_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Ask OpenRouter to embed the real generation cost in `usage.cost` —
        # ground-truth spend the broker records, not a price-table estimate.
        "usage": {"include": True},
        # Surface the model's thinking (content + reasoning_tokens) for reasoning
        # models; a non-reasoning model ignores this, so it's safe to always send.
        "reasoning": {"enabled": True},
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    resp = await client.post(f"{API_BASE}/chat/completions",
                     json=body, headers=_auth_header(params))
    data = resp["json"] or {}
    # Surface an API failure as a real error. OpenRouter answers a bad model id,
    # auth failure, or rate limit with a non-2xx (or a 200 carrying an
    # `{"error": {...}}` envelope); left unchecked, `data` has no `choices` and
    # this returns a benign `content:null, stop_reason:"end_turn"` — which the
    # agent loop misreads as a successful empty turn instead of the failed run it
    # is. Raise so the engine records the failure (ExecutionResult.error) and the
    # loop records the run with status "error".
    if not resp.get("ok", True) or data.get("error"):
        err = data.get("error")
        msg = err.get("message") if isinstance(err, dict) else (err or "request failed")
        raise RuntimeError(f"OpenRouter chat failed (HTTP {resp.get('status')}): {msg}")
    choices = data.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")

    tool_calls = [
        {
            "id": tc["id"],
            "name": tc["function"]["name"],
            "input": (
                json.loads(tc["function"].get("arguments", "{}"))
                if isinstance(tc["function"].get("arguments"), str)
                else tc["function"].get("arguments") or {}
            ),
        }
        for tc in (message.get("tool_calls") or [])
    ]

    stop_reason = (
        "tool_use" if finish_reason == "tool_calls"
        else "max_tokens" if finish_reason == "length"
        else "end_turn"
    )

    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        # The model OpenRouter actually routed to (e.g. "openai/gpt-4o-mini" for
        # the "gpt-4o-mini" alias) — the honest record and the graph pricing key.
        "model": data.get("model"),
        "content": message.get("content"),
        # The model's thinking, when it exposes it — recorded as a reasoning block.
        "reasoning": message.get("reasoning"),
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            # Thinking tokens (a subset of completion) — surfaced for the record.
            "reasoning_tokens": details.get("reasoning_tokens", 0),
            # Real USD cost of this generation (present with usage.include);
            # the broker reads it as `reported` spend.
            "cost": usage.get("cost"),
        },
    }
