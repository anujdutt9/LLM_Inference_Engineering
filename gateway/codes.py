"""Every way a call can end other than 200: its HTTP code, the layer that decided it, and
why. One table, so the gateway, the event log, Prometheus, the UI and the analysis all use
the same names.

The order of "no"s: guardrails 400, then admission 429, then placement 503.
  400  edge guardrails: malformed or disallowed requests; fix the client
  429  admission, the tenant's own doing: over its token budget (rate_limit_error),
       or a deadline it can't meet (estimate in the body). The cluster may be
       completely idle. Stays local, never overflows.
  503  no capacity, not the tenant's fault: KV pressure (Retry-After 5), stale metrics, fail
       closed (Retry-After 1), queue full or expired, worker unreachable
       (server_is_overloaded, Retry-After). 529 means the same and is not emitted here.
  500  a bug in our own process; 502 the upstream failed
  499  the client went away ("cancelled / client_disconnect"), recorded with nginx's 499 and
       never sent on the wire
  504  the engine sent nothing within the idle timeout

Layers, in request order: client, edge, router, admission, queue, engine.
"""

from __future__ import annotations

from dataclasses import dataclass

LAYERS = ("client", "edge", "router", "admission", "queue", "engine")


@dataclass(frozen=True)
class Outcome:
    code: int
    layer: str
    meaning: str


OUTCOMES: dict[str, Outcome] = {
    # client
    "client_disconnect": Outcome(499, "client", "the client closed the connection before the answer finished"),
    # edge (app/guardrails.py)
    "invalid_json": Outcome(400, "edge", "the body is not JSON"),
    "model_not_allowed": Outcome(400, "edge", "model not in the allowlist"),
    "bad_max_tokens": Outcome(400, "edge", "max_tokens is not a positive integer"),
    "prompt_too_long": Outcome(400, "edge", "prompt over the guardrail's character limit"),
    "empty_prompt": Outcome(400, "edge", "no prompt"),
    "vision_needs_image": Outcome(400, "edge", "vision model called without an image"),
    "bad_request": Outcome(400, "edge", "request rejected by guardrails"),
    "context_full": Outcome(400, "edge", "prompt + the minimum output do not fit the model's context: compact or start a new session"),
    # router
    "unknown_model": Outcome(404, "router", "no pool serves this model (normally caught at the edge allowlist first)"),
    # admission (gateway/admission.py)
    "tenant_tokens": Outcome(429, "admission", "the tenant spent its token budget (token bucket)"),
    "ttft_unmeetable": Outcome(429, "admission", "estimated TTFT exceeds the class target × slack"),
    "ttfat_unmeetable": Outcome(429, "admission", "estimated first-answer time exceeds the class target × slack"),
    "deadline_unmeetable": Outcome(429, "admission", "estimated total time exceeds the class deadline × slack"),
    "no_signal": Outcome(503, "admission", "engine metrics are stale: fail closed"),
    "kv_pressure": Outcome(503, "admission", "engine KV cache above the shed threshold"),
    "new_session_shed": Outcome(503, "admission", "engine busy: new sessions refused, running ones continue"),
    "queue_depth": Outcome(503, "admission", "the model's queue is at its bound"),
    # queue (gateway/queue.py)
    "queue_full": Outcome(503, "queue", "the model's queue is at its bound"),
    "expired_in_queue": Outcome(503, "queue", "waited past its class's queue budget"),
    # engine
    "upstream_error": Outcome(503, "engine", "engine unreachable"),
    "engine_error": Outcome(500, "engine", "the engine returned an error status, passed through as is (e.g. vLLM 400: prompt longer than the context)"),
    "stream_dropped": Outcome(502, "engine", "the engine stream broke mid-answer"),
    "upstream_timeout": Outcome(504, "engine", "no token from the engine within the idle timeout"),
}


def outcome(reason: str, status: int) -> Outcome:
    """The table entry, or a best guess from the status for anything not listed."""
    if reason in OUTCOMES:
        o = OUTCOMES[reason]
        return o if o.code == status or reason == "engine_error" else Outcome(status, o.layer, o.meaning)
    layer = {499: "client", 400: "edge", 404: "router", 429: "admission"}.get(status, "engine" if status >= 500 else "edge")
    return Outcome(status, layer, reason or "")
