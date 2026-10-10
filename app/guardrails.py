from __future__ import annotations

from dataclasses import dataclass, field

MAX_NEW_TOKENS = 512
MAX_PROMPT_CHARS = 8_000

@dataclass(frozen=True)
class Limits:
    """The edge's limits. The gateway passes its own (gateway/config.yaml `guardrails`): one
    agent call can carry a 30K-token filing, and the gateway sets max_tokens itself, by agent
    role. The defaults suit a small chat model."""

    max_new_tokens: int = MAX_NEW_TOKENS
    max_prompt_chars: int = MAX_PROMPT_CHARS
    models: frozenset[str] | None = None  # allowlist: the models the gateway serves; None = any
    default_max_tokens: int | None = MAX_NEW_TOKENS  # set when a client sends none; None = leave it

@dataclass(frozen=True)
class Guard:
    ok: bool
    status: int = 200
    reason: str = "ok"
    message: str = ""
    payload: dict = field(default_factory=dict)

def inspect(payload: dict, limits: Limits | None = None) -> Guard:
    limits = limits or Limits()
    body = dict(payload)
    model = str(body.get("model") or "")
    if limits.models is not None and model not in limits.models:
        return Guard(False, 400, "model_not_allowed", f"model {model!r} is not served here", body)

    tokens = body.get("max_tokens", body.get("max_new_tokens"))
    if tokens is None and limits.default_max_tokens is not None:
        body["max_tokens"] = limits.default_max_tokens
        tokens = limits.default_max_tokens
    if tokens is not None:
        try:
            n = int(tokens)
        except (TypeError, ValueError):
            return Guard(False, 400, "bad_max_tokens", "max_tokens must be an int", body)
        if n <= 0:
            return Guard(False, 400, "bad_max_tokens", "max_tokens must be > 0", body)
        if n > limits.max_new_tokens:
            body["max_tokens"] = limits.max_new_tokens
            body.pop("max_new_tokens", None)

    text = _message_text(body)
    if len(text) > limits.max_prompt_chars:
        return Guard(False, 400, "prompt_too_long", f"prompt > {limits.max_prompt_chars} chars", body)
    if not _is_vision(model, body) and not text.strip():
        return Guard(False, 400, "empty_prompt", "text model needs a prompt", body)
    if _is_vision(model, body) and not _has_image(body):
        return Guard(False, 400, "vision_needs_image", "vision model needs an image part", body)

    body.setdefault("tenant", "lab")
    return Guard(True, payload=body)

def _is_vision(model: str, payload: dict) -> bool:
    raw = model.lower()
    if raw == "vision" or "-vl" in raw or "vision" in raw:
        return True
    return _has_image(payload)

def _message_text(payload: dict) -> str:
    parts: list[str] = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    if payload.get("prompt"):
        parts.append(str(payload["prompt"]))
    return "\n".join(parts)

def _has_image(payload: dict) -> bool:
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("image_url", "image"):
                return True
    return False
