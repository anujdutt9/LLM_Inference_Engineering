"""A stand-in for one vLLM server, for the laptop: the OpenAI chat API with real SSE
streaming (reasoning, answer and tool-call deltas, then usage), vLLM-shaped /metrics, and
timing that behaves like an engine (prefill time grows with uncached prompt tokens, decode
emits one token per ITL, at most max_num_seqs run at once and the rest wait). The live
gateway, the agent harness and the UI can all be exercised against it without a GPU.

    python -m router.fake_vllm --model qwen-27b --port 8000 --itl 0.03 --prefill-tps 6000
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from typing import Any


class FakeEngine:
    def __init__(self, model: str, *, max_num_seqs: int = 8, itl_s: float = 0.02, prefill_tps: float = 8000.0,
                 kv_tokens: int = 400_000, think: bool = True, speed: float = 1.0) -> None:
        self.model = model
        self.max_num_seqs = max_num_seqs
        self.itl_s = itl_s / speed
        self.prefill_tps = prefill_tps * speed
        self.kv_tokens = kv_tokens
        self.think = think
        self.running = 0
        self.waiting = 0
        self.kv_used = 0
        self.prefix: set[str] = set()
        self.prefix_hits = 0
        self.prefix_queries = 0
        self.requests: list[dict] = []  # bodies received, for tests
        self._slots = asyncio.Semaphore(max_num_seqs)
        self.kv_override: float | None = None
        self.preemptions = 0

    def metrics(self) -> str:
        kv = self.kv_override if self.kv_override is not None else min(1.0, self.kv_used / self.kv_tokens)
        m = f'model_name="{self.model}",engine="0"'
        return "\n".join([
            "# TYPE vllm:kv_cache_usage_perc gauge",
            f"vllm:kv_cache_usage_perc{{{m}}} {kv}",
            f"vllm:num_requests_running{{{m}}} {self.running}",
            f"vllm:num_requests_waiting{{{m}}} {self.waiting}",
            f"vllm:prefix_cache_hits_total{{{m}}} {self.prefix_hits}",
            f"vllm:prefix_cache_queries_total{{{m}}} {self.prefix_queries}",
            f"vllm:num_preemptions_total{{{m}}} {self.preemptions}",
        ]) + "\n"

    def _cached(self, text: str, block: int = 1024) -> int:
        """Tokens (≈ chars/4) of the prompt's leading blocks seen before."""
        h = hashlib.sha1()
        hit = 0
        blocks = [text[i:i + block] for i in range(0, len(text) - block + 1, block)]
        for i, b in enumerate(blocks):
            h.update(b.encode())
            key = h.hexdigest()
            if key in self.prefix and hit == i * block:
                hit += block
            self.prefix.add(key)
        prompt = max(1, len(text) // 4)
        self.prefix_queries += prompt
        self.prefix_hits += hit // 4
        return hit // 4

    def plan(self, body: dict) -> tuple[list[tuple[str, Any]], dict]:
        """What to say: reasoning (if thinking), then a tool call when tools are offered and
        the last message is not a tool result, else a short answer."""
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        thinking = self.think and (body.get("chat_template_kwargs") or {}).get("enable_thinking", True)
        parts: list[tuple[str, Any]] = []
        if thinking:
            parts += [("reasoning", w + " ") for w in ("Let", "me", "work", "out", "what", "is", "being", "asked", "here.") * 3]
        tools = body.get("tools") or []
        if tools and last.get("role") != "tool":
            fn = tools[0].get("function", {})
            parts.append(("tool", {"name": fn.get("name", "tool"), "arguments": json.dumps({"query": "SELECT 1"})}))
            return parts, {"finish_reason": "tool_calls"}
        parts += [("content", w + " ") for w in ("Here", "is", "the", "answer:", "the", "numbers", "check", "out.") * 4]
        return parts, {"finish_reason": "stop"}

    async def complete(self, body: dict):
        """Yields SSE lines (stream) or returns a dict (not stream)."""
        self.requests.append(body)
        text = json.dumps(body.get("messages"))
        prompt_tokens = max(1, len(text) // 4)
        self.waiting += 1
        await self._slots.acquire()
        self.waiting -= 1
        self.running += 1
        self.max_running = max(getattr(self, "max_running", 0), self.running)
        self.kv_used += prompt_tokens
        try:
            cached = self._cached(text)
            if self.kv_used > self.kv_tokens:  # out of KV: like vLLM, preempt and recompute the prefill later
                self.preemptions += 1
                cached = 0
                await asyncio.sleep(prompt_tokens / self.prefill_tps)
            await asyncio.sleep(max(0.0, prompt_tokens - cached) / self.prefill_tps)
            parts, end = self.plan(body)
            rid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            created = int(time.time())

            def chunk(delta: dict, finish: str | None = None) -> str:
                return "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created, "model": self.model,
                                              "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

            yield chunk({"role": "assistant", "content": ""})
            for kind, value in parts:
                await asyncio.sleep(self.itl_s)
                if kind == "reasoning":
                    yield chunk({"reasoning": value})
                elif kind == "content":
                    yield chunk({"content": value})
                else:
                    yield chunk({"tool_calls": [{"index": 0, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                                 "function": {"name": value["name"], "arguments": value["arguments"]}}]})
            yield chunk({}, end["finish_reason"])
            usage = {"prompt_tokens": prompt_tokens, "completion_tokens": len(parts), "total_tokens": prompt_tokens + len(parts),
                     "prompt_tokens_details": {"cached_tokens": cached}}
            yield "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created, "model": self.model,
                                         "choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            self.running -= 1
            self.kv_used -= prompt_tokens
            self._slots.release()


def create_app(engine: FakeEngine) -> Any:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
    from starlette.routing import Route

    async def chat(request: Request) -> Any:
        body = await request.json()
        if body.get("model") != engine.model:
            return JSONResponse({"object": "error", "message": f"The model `{body.get('model')}` does not exist."}, status_code=404)
        if body.get("stream"):
            return StreamingResponse(engine.complete(body), media_type="text/event-stream")
        content, reasoning, tool_calls, usage, finish = "", "", [], {}, "stop"
        async for line in engine.complete(body):
            data = line[6:].strip()
            if data == "[DONE]":
                continue
            c = json.loads(data)
            usage = c.get("usage") or usage
            for ch in c.get("choices") or []:
                d = ch.get("delta") or {}
                content += d.get("content") or ""
                reasoning += d.get("reasoning") or ""
                tool_calls += d.get("tool_calls") or []
                finish = ch.get("finish_reason") or finish
        msg: dict = {"role": "assistant", "content": content or None, "reasoning": reasoning or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return JSONResponse({"id": "chatcmpl-fake", "object": "chat.completion", "model": engine.model,
                             "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": usage})

    async def models(request: Request) -> Any:
        return JSONResponse({"object": "list", "data": [{"id": engine.model, "object": "model", "max_model_len": 65536}]})

    async def metrics(request: Request) -> Any:
        return PlainTextResponse(engine.metrics())

    async def reset(request: Request) -> Any:
        engine.prefix.clear()
        return PlainTextResponse("OK")

    async def health(request: Request) -> Any:
        return PlainTextResponse("OK")

    async def tokenize(request: Request) -> Any:
        # vLLM's POST /tokenize with messages: the prompt after the chat template. Here a fixed
        # template (30 tokens, +5 per extra message, +80 with tools) around whitespace "tokens".
        from gateway.types import prompt_text

        body = await request.json()
        n = len(body.get("messages") or []) or 1
        count = len(prompt_text(body).split()) + 30 + 5 * (n - 1) + (80 if body.get("tools") else 0)
        return JSONResponse({"count": count, "max_model_len": 65536})

    return Starlette(routes=[
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/v1/models", models),
        Route("/metrics", metrics),
        Route("/reset_prefix_cache", reset, methods=["POST"]),
        Route("/health", health),
        Route("/tokenize", tokenize, methods=["POST"]),
    ])


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--max-num-seqs", type=int, default=8)
    ap.add_argument("--itl", type=float, default=0.02)
    ap.add_argument("--prefill-tps", type=float, default=8000.0)
    ap.add_argument("--no-think", action="store_true")
    ap.add_argument("--speed", type=float, default=1.0, help="multiply engine speed (e.g. 5 for quick rehearsals)")
    ap.add_argument("--kv-tokens", type=int, default=None, help="KV cache size in tokens (small values show KV pressure and preemption)")
    a = ap.parse_args()
    engine = FakeEngine(a.model, max_num_seqs=a.max_num_seqs, itl_s=a.itl, prefill_tps=a.prefill_tps, think=not a.no_think, speed=a.speed)
    if a.kv_tokens:
        engine.kv_tokens = a.kv_tokens
    uvicorn.run(create_app(engine), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
