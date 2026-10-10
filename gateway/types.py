from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("orch")

KV_SATURATION = 0.80

STICKY_OVERLAP = 0.8

@dataclass
class Request:
    id: str
    arrival_t: float
    priority: int
    prompt_tokens: int
    max_new_tokens: int
    prefix_hash: str | None
    timeout_s: float
    tenant: str
    aborted: bool = False

    capability: str = "text"
    prompt: str = ""
    messages: list | None = None
    force_phase: bool = False
    evict_after: bool = False

    # Who is asking, for the live gateway (X-* headers set by the agent harness)
    model: str = ""
    session: str = ""
    agent: str = ""
    parent: str = ""
    role: str = "default"
    sla: str = ""
    stream: bool = False
    agent_name: str = ""
    task: str = ""

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

def estimate_prompt_tokens(payload: dict) -> int:
    if payload.get("prompt_tokens") is not None:
        return max(1, int(payload["prompt_tokens"]))
    text = _message_text(payload)
    return max(16, max(1, len(text) // 4))

def request_from_openai(payload: dict, headers: dict[str, str] | None = None) -> Request:
    """An OpenAI chat payload as a Request. Every call gets its own id and its arrival on
    the gateway's clock (a shared id and arrival_t 0 would freeze the tenant window)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    text = _message_text(payload)
    prefix = payload.get("prefix_hash") or (
        hashlib.sha256(text[:256].encode("utf-8")).hexdigest()[:16] if text else None
    )
    # Tenant: an explicit X-Tenant-ID (Locust), else the agent harness's session (each
    # tinyharness terminal is one), else the body's tenant / user fields.
    session = h.get("x-session-id", "")
    tenant = (
        h.get("x-tenant-id")
        or (f"th-{session[-8:]}" if session else "")
        or payload.get("tenant")
        or payload.get("user")
        or "lab"
    )
    return Request(
        id=str(payload.get("id") or h.get("x-request-id") or uuid.uuid4().hex[:16]),
        arrival_t=float(payload.get("arrival_t") or time.monotonic()),
        priority=int(payload.get("priority", 1)),
        prompt_tokens=estimate_prompt_tokens(payload),
        max_new_tokens=int(payload.get("max_tokens", payload.get("max_new_tokens", 512))),
        prefix_hash=prefix,
        timeout_s=float(payload.get("timeout_s", 30)),
        tenant=str(tenant),
        capability=str(payload.get("capability") or ""),
        prompt=text,
        messages=payload.get("messages") if isinstance(payload.get("messages"), list) else None,
        force_phase=bool(payload.get("kv_hop")),
        evict_after=bool(payload.get("kv_hop") and payload.get("kv_evict")),
        model=str(payload.get("model") or ""),
        session=session or str(tenant),
        agent=h.get("x-agent-id") or session or str(tenant),
        parent=h.get("x-parent-agent-id", ""),
        role=h.get("x-agent-role", "default"),
        sla=h.get("x-sla-class", ""),
        stream=bool(payload.get("stream")),
        agent_name=h.get("x-agent-name", ""),
        task=h.get("x-task", ""),
    )

@dataclass
class Snapshot:
    pod_id: str
    age_s: float
    healthy: bool = True
    saturating: bool = False
    kv_free_ratio: float | None = None
    tokens_in_flight: int | None = None
    uncached_prefill_tokens: int | None = None
    active_requests: int | None = None
    queue_depth: int | None = None
    waiting: int | None = None
    running: int | None = None
    prefix_tokens: dict[str, int] | None = None

    soak_weight: float = 1.0

@dataclass
class Shed(Exception):

    status: int
    error: str
    reason: str = "no_eligible_pod"
    retry_after_s: float | None = None
    message: str = ""
    estimate_s: float | None = None
    code: str | None = None  # OpenAI's error code when one fits (context_length_exceeded); else the status

    def json_body(self) -> dict:
        err: dict[str, Any] = {"type": self.error, "code": self.code or self.status, "reason": self.reason, "message": self.message}
        if self.estimate_s is not None:
            err["estimate_s"] = round(self.estimate_s, 2)
        return {"error": err}

    def headers(self) -> dict[str, str]:
        """Retry-After on every 503, and on a 429 that knows when to come back."""
        if self.status in (503, 529) or (self.status == 429 and self.retry_after_s):
            return {"Retry-After": str(max(1, round(self.retry_after_s or 2)))}
        return {}

# ---- live serving: token counts and the per-call record ----

TOKENIZER_REPO = "Qwen/Qwen3.5-9B"  # the 27B and the 9B share one vocabulary


class TokenCounter:
    """Qwen's tokenizer when it can be loaded (in the background), else ~3.5 characters
    per token. Admission counts tokens, not requests."""

    def __init__(self, repo: str = TOKENIZER_REPO, *, load: bool = True) -> None:
        self._tok: Any = None
        if load:
            threading.Thread(target=self._load, args=(repo,), daemon=True).start()

    @property
    def exact(self) -> bool:
        return self._tok is not None

    def _load(self, repo: str) -> None:
        try:
            from tokenizers import Tokenizer

            self._tok = Tokenizer.from_pretrained(repo)
            log.info("tokenizer %s loaded", repo)
        except Exception as e:  # offline or no tokenizers package: estimate instead
            log.warning("tokenizer %s unavailable (%s); estimating 3.5 chars/token", repo, e)

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._tok is not None:
            return len(self._tok.encode(text, add_special_tokens=False).ids)
        return max(1, int(len(text) / 3.5))


class TemplateCost:
    """Tokens the engine's chat template adds around the text the gateway counts: role markers
    on every message, a fixed preamble (Qwen3.8's injects a reasoning-effort system line unless
    told otherwise) and the instructions it wraps tool schemas in. Measured from each engine's
    own /tokenize, per pool and per chat_template_kwargs (they change the template), relative to
    the gateway's own count, so estimate = count(prompt_text) + base + per_message × (n − 1) +
    tools. Until a pool is measured it adds 0 (the old behaviour)."""

    PROBE_TOOL = [{"type": "function", "function": {"name": "probe", "description": "probe",
                   "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}]

    def __init__(self, counter: "TokenCounter") -> None:
        self.counter = counter
        self.known: dict[tuple[str, str], dict[str, int]] = {}
        self._pending: set[tuple[str, str]] = set()

    @staticmethod
    def key(pool: str, body: dict) -> tuple[str, str]:
        return pool, json.dumps(body.get("chat_template_kwargs") or {}, sort_keys=True)

    def tokens(self, pool: str, body: dict) -> int:
        c = self.known.get(self.key(pool, body)) or self.known.get((pool, "{}"))
        if not c:
            return 0
        n = len(body.get("messages") or []) or 1
        return max(0, c["base"] + c["per_message"] * (n - 1) + (c["tools"] if body.get("tools") else 0))

    def missing(self, pool: str, body: dict) -> bool:
        k = self.key(pool, body)
        return k not in self.known and k not in self._pending

    async def measure(self, client: Any, pool: str, url: str, model: str, kwargs: dict | None = None) -> dict[str, int] | None:
        """Three /tokenize calls: one message, three messages, one message plus a tool."""
        k = (pool, json.dumps(kwargs or {}, sort_keys=True))
        self._pending.add(k)
        try:
            async def engine(messages: list, tools: list | None = None) -> tuple[int, int]:
                body: dict[str, Any] = {"model": model, "messages": messages}
                if tools:
                    body["tools"] = tools
                if kwargs:
                    body["chat_template_kwargs"] = kwargs
                r = await client.post(f"{url}/tokenize", json=body, timeout=5.0)
                r.raise_for_status()
                return int(r.json()["count"]), self.counter.count(prompt_text(body))

            one = [{"role": "user", "content": "Hello"}]
            three = one + [{"role": "assistant", "content": "Hello"}, {"role": "user", "content": "Hello"}]
            e1, g1 = await engine(one)
            e3, g3 = await engine(three)
            et, gt = await engine(one, self.PROBE_TOOL)
            base = e1 - g1
            cost = {"base": base, "per_message": round(((e3 - g3) - base) / 2), "tools": (et - gt) - base}
            self.known[k] = cost
            return cost
        except Exception as e:
            log.warning("template cost for %s %s not measured: %s", pool, k[1], e)
            return None
        finally:
            self._pending.discard(k)


def prompt_text(payload: dict) -> str:
    """Everything the model reads: message text, tool calls and their results, reasoning
    sent back, and the tool schemas. (_message_text keeps only the text parts.)"""
    parts: list[str] = []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(p.get("text") or "") for p in content if isinstance(p, dict))
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            parts.append(f"{fn.get('name', '')} {fn.get('arguments', '')}")
        for key in ("reasoning", "reasoning_content"):
            if isinstance(m.get(key), str):
                parts.append(m[key])
    if payload.get("tools"):
        parts.append(json.dumps(payload["tools"], separators=(",", ":")))
    return "\n".join(parts)


def prefix_blocks(text: str, block_chars: int) -> list[str]:
    """Hashes of the prompt's cumulative prefix, one per whole block: what a replica that
    served the same prefix would still hold in its prefix cache. (A hash of the first 256
    characters alone would be the same for every agent call.)"""
    out: list[str] = []
    h = hashlib.sha1()
    for i in range(0, len(text) - block_chars + 1, block_chars):
        h.update(text[i : i + block_chars].encode())
        out.append(h.hexdigest()[:16])
    return out


@dataclass
class Call:
    """One chat call through the live gateway: the Request plus everything measured on the
    way (admission, queue, placement, streaming). `record()` is its row in requests.jsonl."""

    req: Request
    body: dict
    t0: float = field(default_factory=time.monotonic)
    ts: float = field(default_factory=time.time)
    max_tokens: int = 0
    max_tokens_asked: int | None = None  # set when the gateway lowered max_tokens to fit the context
    thinking_budget: int | None = None  # thinking_token_budget sent to vLLM (reasoning_by_role)
    priority: int | None = None
    estimates: dict | None = None  # admission's predicted ttft/ttfat/total (s), when it ran
    tool_results: int = 0  # role=tool messages at the end: this call follows local tool work
    tool_gap_s: float | None = None  # since this agent's previous call ended
    blocks: list[str] = field(default_factory=list)
    pool: str = ""
    model_served: str = ""
    replica: str = ""
    route_reason: str = ""
    spilled: bool = False
    route_scores: dict[str, float] = field(default_factory=dict)
    status: int = 0
    shed_reason: str = ""
    layer: str = ""  # who decided a non-200 outcome (gateway/codes.py)
    via: str = "local"  # "overflow" when the external provider answered
    overflow_from: str = ""  # the local refusal it overflowed from
    error: str = ""
    t_admitted: float | None = None
    t_dispatched: float | None = None
    t_first_token: float | None = None
    t_first_answer: float | None = None
    t_last_reasoning: float | None = None
    t_last_token: float | None = None
    t_done: float | None = None
    reasoning_chunks: int = 0
    answer_chunks: int = 0
    tool_chunks: int = 0
    gaps: list[float] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    disconnected: bool = False
    output: dict[str, str] = field(default_factory=lambda: {"reasoning": "", "content": "", "tool_calls": ""})

    def since(self, t: float | None) -> float | None:
        return None if t is None else round(t - self.t0, 4)

    @property
    def queue_wait_s(self) -> float | None:
        if self.t_dispatched is None or self.t_admitted is None:
            return None
        return round(self.t_dispatched - self.t_admitted, 4)

    def record(self) -> dict:
        r = self.req
        gaps = sorted(self.gaps)
        usage = self.usage or {}
        details = usage.get("prompt_tokens_details") or {}
        return {
            "req_id": r.id,
            "ts": round(self.ts, 3),
            "tenant": r.tenant,
            "session": r.session,
            "agent": r.agent,
            "parent": r.parent,
            "agent_name": r.agent_name,
            "task": r.task,
            "role": r.role,
            "sla": r.sla,
            "model_requested": r.model,
            "pool": self.pool,
            "model_served": self.model_served,
            "replica": self.replica,
            "route_reason": self.route_reason,
            "spilled": self.spilled,
            "route_scores": self.route_scores,
            "stream": r.stream,
            "status": self.status,
            "shed_reason": self.shed_reason,
            "layer": self.layer,
            "via": self.via,
            "overflow_from": self.overflow_from,
            "error": self.error,
            "disconnected": self.disconnected,
            "priority": self.priority,
            "max_tokens": self.max_tokens,
            "max_tokens_asked": self.max_tokens_asked,
            "thinking_budget": self.thinking_budget,
            "chat_template_kwargs": self.body.get("chat_template_kwargs") or {},
            "prompt_tokens_est": r.prompt_tokens,
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": details.get("cached_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens_est": self.reasoning_chunks,
            "answer_tokens_est": self.answer_chunks + self.tool_chunks,
            "tool_results_in": self.tool_results,
            "tool_gap_s": None if self.tool_gap_s is None else round(self.tool_gap_s, 3),
            "tool_calls": self.tool_calls,
            "finish_reason": self.finish_reason,
            "admission_s": self.since(self.t_admitted),
            "queue_wait_s": self.queue_wait_s,
            "ttft_s": self.since(self.t_first_token),
            "ttfat_s": self.since(self.t_first_answer),
            "reasoning_s": (
                None
                if self.t_last_reasoning is None or self.t_first_token is None
                else round(self.t_last_reasoning - self.t_first_token, 4)
            ),
            "e2e_s": self.since(self.t_done),
            "itl_mean_s": round(sum(gaps) / len(gaps), 5) if gaps else None,
            "itl_p50_s": _pct(gaps, 0.50),
            "itl_p95_s": _pct(gaps, 0.95),
            "itl_p99_s": _pct(gaps, 0.99),
            "itl_max_s": round(gaps[-1], 5) if gaps else None,
            "chunks": len(gaps) + (1 if self.t_first_token else 0),
        }


def _pct(sorted_values: list[float], p: float) -> float | None:
    """Nearest-rank percentile, the rule the dataset uses everywhere."""
    if not sorted_values:
        return None
    return round(sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * p))], 5)
