"""orch-serve: the HTTP front door. Every chat call goes edge (guardrails) → admit → queue →
route → vLLM, and streams straight back.

Agents need tool calls and real streaming, and TTFT/ITL can only be measured on a real
stream, so the server is async: the client's body goes upstream untouched except for the
fields the gateway owns (model, max_tokens, priority), and every SSE line is relayed the
moment it arrives while we time it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.guardrails import Limits, inspect
from gateway.admission import Admission
from gateway.codes import outcome
from gateway.overflow import OverflowGate
from gateway.config import MODES, Config, from_raw, load_config, merge
from gateway.metrics import METRICS, LiveMetrics
from gateway.queue import LaneScheduler
from gateway.types import Call, Shed, TemplateCost, TokenCounter, prefix_blocks, prompt_text, request_from_openai
from router.pools import build_model_pools, scrape_forever, scrape_replica
from router.router import ModelRouter
from router.trace import EventLog

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("orch")

# Fields only the gateway reads; never sent upstream.
GATEWAY_FIELDS = ("tenant", "capability", "kv_hop", "kv_evict", "prefix_hash", "arrival_t", "timeout_s",
                  "prompt_tokens", "max_new_tokens", "id", "max_completion_tokens")


def prepare_chat(payload: dict, limits: Limits | None = None) -> tuple[int, dict]:
    edge = inspect(payload, limits)
    if not edge.ok:
        return edge.status, {"error": {"type": edge.reason, "message": edge.message}}
    return 200, edge.payload

def listed_models(cfg: Config) -> dict:
    """GET /v1/models: one entry per pool, with max_model_len (the agent harness reads it to
    know each model's context window)."""
    return {
        "object": "list",
        "data": [
            {"id": p.model, "object": "model", "owned_by": "orch", "max_model_len": p.max_model_len}
            for p in cfg.pools.values()
        ],
    }

# ---------------------------------------------------------------------------
# The live gateway
# ---------------------------------------------------------------------------


class ClientGone(Exception):
    pass


class LiveGateway:
    def __init__(self, cfg: Config, *, tokenizer: bool = True) -> None:
        self.cfg = cfg
        self.pools = build_model_pools(cfg)
        # every replica the gateway has used, by URL: a variant that drops one and a later one that brings it back
        # get the same object (its name, its count of calls still running)
        self.replicas_seen = {r.url: r for p in self.pools.values() for r in p.replicas}
        self.router = ModelRouter(cfg, self.pools)
        self.sched = LaneScheduler(cfg, self.pools, self.router)
        self.admission = Admission(cfg)
        self.active: dict[str, Call] = {}  # dispatched and still streaming, by req_id (for the UI's slots)
        self.overflow = OverflowGate.from_config(cfg.raw.get("overflow"))
        self.log = EventLog(cfg.results_dir, cfg.record_bodies)
        self.log.mode = cfg.mode
        self.live = LiveMetrics()
        self.tokens = TokenCounter(load=tokenizer)
        self.template = TemplateCost(self.tokens)  # chat-template tokens, measured on each engine
        g = cfg.guardrails
        self.limits = Limits(
            max_new_tokens=int(g.get("max_new_tokens", 16384)),
            max_prompt_chars=int(g.get("max_prompt_chars", 1_500_000)),
            models=frozenset(cfg.models()),
            default_max_tokens=None,  # set below, by agent role
        )
        self.client: Any = None
        self._tasks: list[asyncio.Task] = []
        self.sched.on_dispatch.append(self._on_dispatch)
        self._set_mode_gauge()

    # ---- lifecycle ----

    async def start(self, transport: Any = None) -> None:
        """`transport` lets tests put a fake vLLM behind the gateway."""
        import httpx

        self.client = httpx.AsyncClient(transport=transport, limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200))
        self._tasks = [
            asyncio.create_task(scrape_forever(self.client, self.pools, self.cfg.scrape_interval_s)),
            asyncio.create_task(self._state_forever()),
            asyncio.create_task(self._measure_templates()),
        ]
        self.sched.start()
        # Always logging: everything lands in results/live/<start time>/ unless an experiment
        # script opens a named run. The analysis splits it by session afterwards.
        if self.log.run is None:
            self.log.open_run("live", time.strftime("%Y%m%d-%H%M%S", time.gmtime()), {"auto": True})
            self.log.emit("config", variant=self.cfg.variant, mode=self.cfg.mode, features=self.cfg.features,
                          changed=[], **self._policy())

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.sched.stop()
        self.log.close_run()
        if self.client is not None:
            await self.client.aclose()

    # ---- chat ----

    async def chat(self, request: Any) -> Any:
        from starlette.responses import JSONResponse, Response, StreamingResponse

        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            self._edge_reject(request, {}, "invalid_json", "body is not a JSON object")
            return JSONResponse({"error": {"type": "invalid_request_error", "message": "body is not JSON"}}, status_code=400)
        status, body = prepare_chat(payload, self.limits)
        if status != 200:
            err = (body.get("error") or {}) if isinstance(body, dict) else {}
            self._edge_reject(request, payload, str(err.get("type") or "bad_request"), str(err.get("message") or ""), status)
            return JSONResponse(body, status_code=status)
        req = request_from_openai(body, dict(request.headers))
        asked = self.cfg.pool_for_model(req.model)
        req.sla = req.sla or self.cfg.sla_for(req.role, asked.name if asked else "")
        self._apply_reasoning(req.role, body)  # before counting: it changes the chat template
        text = prompt_text(body)
        req.prompt_tokens = await asyncio.to_thread(self.tokens.count, text) if len(text) > 20_000 else self.tokens.count(text)
        if asked is not None:  # + what the chat template wraps around the text (as the engine counts it)
            req.prompt_tokens += self.template.tokens(asked.name, body)
            if self.template.missing(asked.name, body) and self.tokens.exact:
                self._spawn(self._measure_template(asked.name, body.get("chat_template_kwargs") or None))
        call = Call(req=req, body=body)
        call.max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or self.cfg.max_tokens_for(req.role))
        if self.cfg.features["prefix"]:
            call.blocks = prefix_blocks(text, int(self.cfg.routing.get("prefix_block_chars", 4096)))
        self._tool_context(call)
        self.live.requests.labels(*self._labels(call)).inc()
        METRICS.inc_requests()
        sla = self.cfg.sla(req.sla)
        self.log.emit("received", **self._who(call), prompt_tokens=req.prompt_tokens, max_tokens=call.max_tokens,
                      stream=req.stream, tool_results=call.tool_results, tool_gap_s=call.tool_gap_s,
                      targets={"ttft_s": sla.ttft_s, "ttfat_s": sla.ttfat_s, "deadline_s": sla.deadline_s,
                               "queue_budget_s": sla.queue_budget_s})
        try:
            pool, reason = self.router.choose_pool(req, self.sched.lanes)
            call.pool, call.model_served = pool.name, pool.model
            await self._fit_context(call, pool)
            if reason == "spill":
                call.spilled, call.route_reason = True, "spill"
                self.live.spill.labels(self.cfg.pool_for_model(req.model).name, pool.name, req.role).inc()
            lane = self.sched.lanes[pool.name]
            if self.cfg.features["admission"]:
                self.admission.check(call, pool, lane_depth=lane.depth, lane_bound=lane.bound, est_wait_s=lane.est_wait())
            self.admission.sessions.touch(req)
            call.t_admitted = time.monotonic()
            self.live.admission.labels("admit", "ok", "200", req.tenant, pool.name, req.sla).inc()
            self.log.emit("admitted", **self._who(call), pool=pool.name, spilled=call.spilled, estimates=call.estimates)
            if self.cfg.features["queue"] and (lane.depth or not pool.free(gated=True)):
                self.log.emit("queued", **self._who(call), pool=pool.name, position=lane.depth + 1, est_wait_s=round(lane.est_wait(), 2))
            replica = await self._acquire(request, call, pool)
        except Shed as shed:
            return await self._overflow_or_shed(call, body, shed)
        except ClientGone:
            call.disconnected = True
            call.shed_reason = "client_disconnect"
            self._finish(call, 499)
            return Response(status_code=499)

        up = {k: v for k, v in body.items() if k not in GATEWAY_FIELDS}
        up["model"] = pool.model
        up["max_tokens"] = call.max_tokens
        call.thinking_budget = self._thinking_budget(call)
        if call.thinking_budget is not None:
            up["thinking_token_budget"] = call.thinking_budget
        if self.cfg.features["priority"]:
            call.priority = up["priority"] = self.cfg.sla(req.sla).priority
        if req.stream:
            up["stream_options"] = {**(up.get("stream_options") or {}), "include_usage": True}
        timeout = self._timeout()
        try:
            upstream = await self.client.send(
                self.client.build_request("POST", f"{replica.url}/v1/chat/completions", json=up, timeout=timeout), stream=True
            )
        except Exception as e:
            replica.stats.healthy = False  # no new calls go there until a scrape answers again
            self.sched.release(replica)
            call.error = f"{type(e).__name__}: {e}"
            return await self._overflow_or_shed(call, body, Shed(503, "server_is_overloaded", "upstream_error", 2, f"engine unreachable: {e}"), dispatched=True)
        if upstream.status_code != 200:
            data = await upstream.aread()
            await upstream.aclose()
            self.sched.release(replica)
            call.error = data[:500].decode("utf-8", "replace")
            call.shed_reason = "engine_error"
            self.log.emit("shed", **self._who(call), pool=call.pool, code=upstream.status_code, reason="engine_error", layer="engine",
                          message=call.error[:200], dispatched=True, estimates=call.estimates)
            self._finish(call, upstream.status_code)
            return Response(data, status_code=upstream.status_code, media_type="application/json")
        if not req.stream:
            try:
                data = await upstream.aread()
            finally:
                await upstream.aclose()
                self.sched.release(replica)
            self._observe_completion(call, data)
            self._finish(call, 200)
            return Response(data, status_code=200, media_type="application/json")
        return StreamingResponse(
            self._relay(call, replica, upstream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Orch-Request-Id": req.id},
        )

    async def _acquire(self, request: Any, call: Call, pool: Any) -> Any:
        """A replica for this call; gives up if the client disconnects while it waits."""
        task = asyncio.create_task(self.sched.acquire(call, pool))
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                return task.result()
            if await request.is_disconnected():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise ClientGone()

    async def _relay(self, call: Call, replica: Any, upstream: Any):
        status = 200
        try:
            async for line in upstream.aiter_lines():
                yield line + "\n"
                if line.startswith("data:"):
                    self._observe_chunk(call, line[5:].strip())
        except asyncio.CancelledError:  # the client went away: closing upstream aborts it in vLLM
            call.disconnected = True
            call.shed_reason = "client_disconnect"
            status = 499
            raise
        except Exception as e:  # the engine dropped mid-stream, or went silent: tell the client in-band
            import httpx

            call.error = f"{type(e).__name__}: {e}"
            timed_out = isinstance(e, httpx.TimeoutException)
            status, call.shed_reason = (504, "upstream_timeout") if timed_out else (502, "stream_dropped")
            self.log.emit("shed", **self._who(call), pool=call.pool, code=status, reason=call.shed_reason, layer="engine",
                          message=call.error[:200], dispatched=True, estimates=call.estimates)
            yield f'data: {json.dumps({"object": "error", "message": call.error})}\n\n'
        finally:
            with contextlib.suppress(Exception):
                await upstream.aclose()
            if replica is not None:
                self.sched.release(replica)
            else:  # an overflow call
                self.overflow.inflight = max(0, self.overflow.inflight - 1)
            self._finish(call, status)

    # ---- measuring the stream ----

    def _observe_chunk(self, call: Call, data: str) -> None:
        if not data or data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return
        if chunk.get("usage"):
            call.usage = chunk["usage"]
        if chunk.get("object") == "error" or chunk.get("error"):
            call.error = str(chunk.get("message") or chunk.get("error"))[:500]
        now = time.monotonic()
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            content = delta.get("content")
            tool_calls = delta.get("tool_calls")
            if reasoning or content or tool_calls:
                if call.t_first_token is None:
                    call.t_first_token = now
                    self.log.emit("first_token", **self._who(call), ttft_s=call.since(now), reasoning=bool(reasoning))
                elif call.t_last_token is not None:
                    gap = now - call.t_last_token
                    call.gaps.append(gap)
                    self.live.itl.labels(call.pool, call.req.tenant).observe(gap)
                call.t_last_token = now
            if reasoning:
                call.reasoning_chunks += 1
                call.t_last_reasoning = now
                call.output["reasoning"] += reasoning
            if content or tool_calls:
                if call.t_first_answer is None:
                    call.t_first_answer = now
                    self.log.emit("first_answer", **self._who(call), ttfat_s=call.since(now))
                if content:
                    call.answer_chunks += 1
                    call.output["content"] += content
            for tc in tool_calls or []:
                call.tool_chunks += 1
                fn = tc.get("function") or {}
                if fn.get("name"):
                    call.tool_calls.append(fn["name"])
                    call.output["tool_calls"] += f"\n{fn['name']}("
                    self.log.emit("tool_call", **self._who(call), tool=fn["name"])
                call.output["tool_calls"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                call.finish_reason = choice["finish_reason"]

    def _observe_completion(self, call: Call, data: bytes) -> None:
        """A non-streamed reply: no TTFT, but tokens, tool calls and the output."""
        try:
            body = json.loads(data)
        except json.JSONDecodeError:
            return
        call.usage = body.get("usage") or {}
        for choice in body.get("choices") or []:
            msg = choice.get("message") or {}
            call.output["content"] += msg.get("content") or ""
            call.output["reasoning"] += msg.get("reasoning") or msg.get("reasoning_content") or ""
            for tc in msg.get("tool_calls") or []:
                name = (tc.get("function") or {}).get("name")
                if name:
                    call.tool_calls.append(name)
            call.finish_reason = choice.get("finish_reason") or call.finish_reason

    # ---- endings ----

    async def _overflow_or_shed(self, call: Call, body: dict, shed: Shed, *, dispatched: bool = False) -> Any:
        """A capacity refusal goes to the overflow provider when the gate allows it; anything
        else (or a failed overflow) is refused as before."""
        from starlette.responses import Response, StreamingResponse

        gate = self.overflow
        pool = call.pool or (self.cfg.pool_for_model(call.req.model).name if self.cfg.pool_for_model(call.req.model) else "")
        decision = gate.decide(pool, shed.status, shed.reason)
        gate.count(decision)
        self.live.overflow.labels(decision, shed.reason, pool or "-").inc()
        if decision != "off":
            self.log.emit("overflow", **self._who(call), pool=pool, decision=decision, from_code=shed.status, from_reason=shed.reason,
                          target=gate.targets.get(pool), provider=gate.state()["provider"])
        if decision != "send":
            return self._shed(call, shed, dispatched=dispatched)
        # only standard fields: the provider rejects our engine-specific ones (Superlinked: 400 on
        # chat_template_kwargs.reasoning_effort, checked 2026-10-03)
        local_only = {"priority", "chat_template_kwargs", "thinking_token_budget"}
        up = {k: v for k, v in body.items() if k not in GATEWAY_FIELDS and k not in local_only}
        up["model"] = gate.targets[pool]
        up["max_tokens"] = call.max_tokens
        if call.req.stream:
            up["stream_options"] = {**(up.get("stream_options") or {}), "include_usage": True}
        gate.inflight += 1
        gate.sent += 1
        call.via, call.overflow_from = "overflow", shed.reason
        call.pool, call.replica, call.route_reason = pool, "overflow", "overflow"
        call.t_dispatched = time.monotonic()
        self.log.emit("dispatched", **self._who(call), pool=pool, replica="overflow", reason="overflow", scores=None,
                      queue_wait_s=call.queue_wait_s, via="overflow", target=up["model"])
        try:
            upstream = await self.client.send(self.client.build_request(
                "POST", f"{gate.base_url}/chat/completions", json=up, timeout=self._timeout(read=gate.timeout_s),
                headers={"Authorization": f"Bearer {gate.api_key}"}), stream=True)
        except Exception as e:
            gate.inflight = max(0, gate.inflight - 1)
            call.error = f"overflow failed: {type(e).__name__}: {e}"
            self.log.emit("overflow", **self._who(call), pool=pool, decision="failed", from_code=shed.status, from_reason=shed.reason,
                          target=up["model"], error=call.error[:200])
            call.via = "local"
            return self._shed(call, shed, dispatched=True)
        if upstream.status_code != 200:
            data = await upstream.aread()
            await upstream.aclose()
            gate.inflight = max(0, gate.inflight - 1)
            call.error = f"overflow {upstream.status_code}: {data[:300].decode('utf-8', 'replace')}"
            self.log.emit("overflow", **self._who(call), pool=pool, decision="failed", from_code=shed.status, from_reason=shed.reason,
                          target=up["model"], error=call.error[:200])
            call.via = "local"
            return self._shed(call, shed, dispatched=True)
        if not call.req.stream:
            try:
                data = await upstream.aread()
            finally:
                await upstream.aclose()
                gate.inflight = max(0, gate.inflight - 1)
            self._observe_completion(call, data)
            self._finish(call, 200)
            return Response(data, status_code=200, media_type="application/json")
        return StreamingResponse(self._relay(call, None, upstream), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Orch-Request-Id": call.req.id,
                                          "X-Orch-Via": "overflow"})

    def _edge_reject(self, request: Any, payload: dict, reason: str, message: str, status: int = 400) -> None:
        """A request the guardrails refused before it became a call: still logged and counted,
        so every non-200 the client saw has a layer and a reason."""
        try:
            req = request_from_openai(payload, dict(request.headers))
        except Exception:
            req = request_from_openai({}, dict(request.headers))
        call = Call(req=req, body={})
        call.shed_reason, call.layer = reason, "edge"
        self.log.emit("received", **self._who(call), prompt_tokens=0, max_tokens=0, stream=bool(payload.get("stream")),
                      tool_results=0, tool_gap_s=None)
        self.log.emit("shed", **self._who(call), pool=None, code=status, reason=reason, layer="edge", message=message,
                      retry_after_s=None, dispatched=False, estimates=None)
        self._finish(call, status)

    def _shed(self, call: Call, shed: Shed, *, dispatched: bool = False) -> Any:
        from starlette.responses import JSONResponse

        call.shed_reason = shed.reason
        call.layer = outcome(shed.reason, shed.status).layer
        METRICS.inc_shed(shed.reason, shed.status)
        self.live.admission.labels("shed", shed.reason, str(shed.status), call.req.tenant, call.pool or "-", call.req.sla).inc()
        self.log.emit("shed", **self._who(call), pool=call.pool, code=shed.status, reason=shed.reason,
                      retry_after_s=shed.retry_after_s, message=shed.message, dispatched=dispatched, estimates=call.estimates,
                      layer=call.layer)
        self._finish(call, shed.status)
        return JSONResponse(shed.json_body(), status_code=shed.status, headers=shed.headers())

    def _finish(self, call: Call, status: int) -> None:
        self.active.pop(call.req.id, None)
        now = time.monotonic()
        call.t_done = now
        call.status = status
        req = call.req
        labels = self._labels(call)
        if status != 200:
            o = outcome(call.shed_reason, status)
            call.layer = call.layer or o.layer
            call.shed_reason = call.shed_reason or f"http_{status}"
            self.live.rejections.labels(call.layer, str(status), call.shed_reason, req.tenant, call.pool or req.model or "-").inc()
        self.live.completed.labels(*labels, str(status)).inc()
        self.live.e2e.labels(*labels, str(status)).observe(now - call.t0)
        METRICS.observe_duration("e2e", now - call.t0)
        if status == 200:
            METRICS.inc_completed()
            METRICS.inc_place(call.pool or "text")
        if call.t_first_token is not None:
            self.live.ttft.labels(*labels).observe(call.t_first_token - call.t0)
        if call.t_first_answer is not None:
            self.live.ttfat.labels(*labels).observe(call.t_first_answer - call.t0)
        if call.t_last_reasoning is not None and call.t_first_token is not None:
            self.live.reasoning.labels(*labels).observe(call.t_last_reasoning - call.t_first_token)
        if call.t_admitted is not None and call.t_dispatched is not None:
            self.live.queue_wait.labels(call.pool, req.sla or self.cfg.default_sla, req.tenant).observe(call.t_dispatched - call.t_admitted)
        usage = call.usage or {}
        tok = self.live.tokens
        model = call.pool or "-"
        prompt = usage.get("prompt_tokens") or 0
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        tok.labels("prompt", req.tenant, model, req.role).inc(prompt)
        tok.labels("prompt_cached", req.tenant, model, req.role).inc(cached)
        tok.labels("completion", req.tenant, model, req.role).inc(usage.get("completion_tokens") or 0)
        tok.labels("reasoning_est", req.tenant, model, req.role).inc(call.reasoning_chunks)
        tok.labels("answer_est", req.tenant, model, req.role).inc(call.answer_chunks + call.tool_chunks)
        for name in call.tool_calls:
            self.live.tool_calls.labels(name, req.tenant, model).inc()
        if call.tool_gap_s is not None:
            self.live.tool_gap.labels(req.tenant, req.role).observe(call.tool_gap_s)
        if status == 200 and call.pool and call.t_dispatched is not None:
            pool = self.pools[call.pool]
            prefill_s = (call.t_first_token - call.t_dispatched) if call.t_first_token else None
            uncached = prompt - cached
            pool.learn(
                prefill_tps=uncached / prefill_s if prefill_s and prefill_s > 0.05 and uncached > 512 else None,
                itl_s=sum(call.gaps) / len(call.gaps) if call.gaps else None,
                service_s=now - call.t_dispatched,
                reasoning_tokens=call.reasoning_chunks or None,
            )
        self.admission.sessions.last_done[req.agent] = now
        record = call.record()
        self.log.request_done(record)
        self.log.body(req.id, call.body, call.output)
        self.log.emit("done", **self._who(call), **{k: record[k] for k in (
            "status", "shed_reason", "layer", "via", "overflow_from", "pool", "replica", "queue_wait_s", "ttft_s", "ttfat_s", "reasoning_s", "e2e_s",
            "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens_est", "itl_p50_s", "itl_p99_s", "itl_max_s",
            "tool_calls", "finish_reason", "disconnected", "error")},
            why=outcome(call.shed_reason, status).meaning if status != 200 else None)

    def _on_dispatch(self, call: Call) -> None:
        self.active[call.req.id] = call
        self.live.route.labels(call.pool, call.replica, call.route_reason).inc()
        self.log.emit("dispatched", **self._who(call), pool=call.pool, replica=call.replica, reason=call.route_reason,
                      scores=call.route_scores, queue_wait_s=call.queue_wait_s)

    # ---- helpers ----

    def _tool_context(self, call: Call) -> None:
        """Tool results at the end of the messages mean the agent just ran tools locally:
        the time since its previous call ended is its tool time."""
        n = 0
        for m in reversed(call.body.get("messages") or []):
            if isinstance(m, dict) and m.get("role") == "tool":
                n += 1
            else:
                break
        call.tool_results = n
        last = self.admission.sessions.last_done.get(call.req.agent)
        if n and last is not None:
            call.tool_gap_s = call.t0 - last

    def _labels(self, call: Call) -> tuple[str, str, str, str]:
        req = call.req
        return (req.tenant, call.pool or req.model or "-", req.role, req.sla or self.cfg.default_sla)

    @staticmethod
    def _who(call: Call) -> dict[str, Any]:
        r = call.req
        return {"req_id": r.id, "tenant": r.tenant, "session": r.session, "agent": r.agent, "parent": r.parent,
                "agent_name": r.agent_name, "task": r.task, "role": r.role, "sla": r.sla, "model": r.model}

    def _timeout(self, read: float | None = None) -> Any:
        import httpx

        u = self.cfg.upstream
        return httpx.Timeout(connect=float(u.get("connect_s", 10)), read=float(read or u.get("read_idle_s", 240)), write=30.0, pool=None)

    def _reasoning_policy(self, role: str) -> dict:
        pol = self.cfg.raw.get("reasoning_by_role") or {}
        return pol.get(role) or pol.get("default") or {}

    def _apply_reasoning(self, role: str, body: dict) -> None:
        """The role's reasoning effort / thinking switch, unless the client set its own."""
        p = self._reasoning_policy(role)
        kw = dict(body.get("chat_template_kwargs") or {})
        if "effort" in p:
            kw.setdefault("reasoning_effort", p["effort"])
        if p.get("thinking") is False:
            kw.setdefault("enable_thinking", False)
        if kw:
            body["chat_template_kwargs"] = kw

    def _thinking_budget(self, call: Call) -> int | None:
        """max_tokens − the role's answer reserve, so thinking always leaves room for the answer;
        at most the role's thinking_max."""
        if call.body.get("thinking_token_budget") is not None:
            return int(call.body["thinking_token_budget"])  # the client's own
        p = self._reasoning_policy(call.req.role)
        reserve = p.get("answer_reserve")
        if reserve is None or (call.body.get("chat_template_kwargs") or {}).get("enable_thinking") is False:
            return None
        budget = max(min(2048, call.max_tokens // 2), call.max_tokens - int(reserve))  # the answer keeps its reserve
        return min(budget, int(p["thinking_max"])) if p.get("thinking_max") else budget

    async def _fit_context(self, call: Call, pool: Any) -> None:
        """vLLM refuses prompt + max_tokens > max_model_len with a 400. Agent harnesses compact on the
        prompt alone (tinyharness at 90% of the window), so a lead holding its team's reports can sit
        between "too big for its max_tokens" and "not big enough to compact". The gateway lowers
        max_tokens to the room left, or refuses when little is left. Its own estimate runs ~3% under on
        agent histories (tool calls and results cost more than the template probe measures), so near
        the limit it asks the engine's /tokenize for the exact count first."""
        g = self.cfg.guardrails
        if call.req.prompt_tokens * 1.05 + call.max_tokens > pool.max_model_len:
            exact = await self._engine_count(pool, call.body)
            if exact is not None:
                call.req.prompt_tokens = exact
        room = pool.max_model_len - call.req.prompt_tokens - int(g.get("context_margin_tokens", 32))
        if call.max_tokens <= room:
            return
        if room < int(g.get("min_output_tokens", 1024)):
            # OpenAI's code and vLLM's wording, so a harness's overflow check (OpenCode's patterns) compacts and retries
            raise Shed(400, "invalid_request_error", reason="context_full", code="context_length_exceeded",
                       message=f"This model's maximum context length is {pool.max_model_len} tokens. The prompt (~{call.req.prompt_tokens} tokens) "
                               f"leaves {max(room, 0)} for the answer; compact the conversation or start a new session.")
        call.max_tokens_asked, call.max_tokens = call.max_tokens, room
        self.log.emit("max_tokens_fit", **self._who(call), pool=pool.name, asked=call.max_tokens_asked, sent=room,
                      prompt_tokens=call.req.prompt_tokens, max_model_len=pool.max_model_len)

    async def _engine_count(self, pool: Any, body: dict) -> int | None:
        """The prompt's token count after the chat template, from the engine itself (vLLM POST /tokenize)."""
        r = next((r for r in pool.replicas if r.stats.healthy), None)
        if r is None or self.client is None:
            return None
        q = {k: body[k] for k in ("messages", "tools", "chat_template_kwargs") if body.get(k)}
        try:
            resp = await self.client.post(f"{r.url}/tokenize", json={"model": pool.model, **q}, timeout=5.0)
            resp.raise_for_status()
            return int(resp.json()["count"])
        except Exception as e:
            log.warning("exact count from %s failed: %s", r.url, e)
            return None

    def _spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._tasks.append(t)
        t.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    async def _measure_template(self, pool_name: str, kwargs: dict | None = None) -> bool:
        pool = self.pools.get(pool_name)
        r = next((r for r in pool.replicas if r.stats.healthy), None) if pool else None
        if r is None:
            return False
        cost = await self.template.measure(self.client, pool_name, r.url, pool.model, kwargs)
        if cost is not None:
            self.log.emit("template_cost", pool=pool_name, chat_template_kwargs=kwargs or {}, **cost)
        return cost is not None

    async def _measure_templates(self) -> None:
        """Each pool's default template cost, once its engine is up and the exact tokenizer loaded."""
        todo = set(self.pools)
        while todo:
            await asyncio.sleep(2.0)
            if not self.tokens.exact:
                continue
            for name in list(todo):
                if await self._measure_template(name):
                    todo.discard(name)

    def _set_mode_gauge(self) -> None:
        for m in MODES:
            self.live.mode.labels(m).set(1 if m == self.cfg.mode else 0)

    async def _state_forever(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            with contextlib.suppress(Exception):
                self.log.emit("state", **self.state())

    def state(self) -> dict[str, Any]:
        """Gauges for Prometheus, and the same picture as one `state` event for the UI."""
        now = time.monotonic()
        lanes: dict[str, Any] = {}
        for name, lane in self.sched.lanes.items():
            by_sla = lane.depth_by_sla()
            for sla in self.cfg.sla_classes:
                self.live.queue_depth.labels(name, sla).set(by_sla.get(sla, 0))
            self.live.queue_bound.labels(name).set(lane.bound)
            self.live.little_l.labels(name).set(lane.little_l())
            self.live.est_wait.labels(name).set(lane.est_wait())
            pool = self.pools[name]
            self.live.prefill_tps.labels(name).set(pool.prefill_tps)
            self.live.learned_itl.labels(name).set(pool.itl_s)
            lanes[name] = {
                "depth": lane.depth,
                "bound": lane.bound,
                "by_sla": by_sla,
                "est_wait_s": round(lane.est_wait(), 2),
                "little_l": round(lane.little_l(), 3),
                "policy": self.cfg.queue.get("policy", "edf_fair") if self.cfg.features["queue"] else None,
                "aging_gain": float(lane.q.get("aging_gain", 0.5)),
                "quantum_tokens": float(lane.q.get("drr_quantum_tokens", 8192)),
                "credit": {t: round(c) for t, c in lane.deficit.items() if lane.by_tenant.get(t)},  # DRR credit per waiting tenant
                "items": [  # in the order the lane will release them, with the numbers that ordered them
                    {"req_id": it.call.req.id, "tenant": it.call.req.tenant, "agent": it.call.req.agent,
                     "agent_name": it.call.req.agent_name, "role": it.call.req.role, "sla": it.call.req.sla,
                     "prompt_tokens": it.call.req.prompt_tokens, "waited_s": round(now - it.enqueued, 2),
                     "cost_tokens": round(it.cost), "budget_s": it.budget_s,
                     "deadline_in_s": round(it.deadline - now, 1),
                     "aged_s": round(float(lane.q.get("aging_gain", 0.5)) * (now - it.enqueued), 1)}
                    for it in lane.ordered()
                ],
            }
        replicas = []
        for pool in self.pools.values():
            for r in pool.replicas:
                self.live.inflight.labels(pool.name, r.id).set(r.inflight)
                self.live.slots.labels(pool.name, r.id).set(r.slots)
                if r.stats.kv is not None:
                    self.live.kv.labels(pool.name, r.id).set(r.stats.kv)
                age = r.stats.age(now)
                self.live.signal_age.labels(pool.name, r.id).set(age if age != float("inf") else -1)
                calls = [
                    {"req_id": c.req.id, "tenant": c.req.tenant, "agent": c.req.agent, "agent_name": c.req.agent_name,
                     "role": c.req.role, "sla": c.req.sla, "prompt_tokens": c.req.prompt_tokens,
                     "phase": "prefill" if c.t_first_token is None else "answering" if c.t_first_answer is not None
                     or c.t_last_reasoning is None else "thinking",
                     "running_s": round(now - (c.t_dispatched or now), 2)}
                    for c in self.active.values() if c.replica == r.id
                ]
                replicas.append({"id": r.id, "pool": pool.name, "model": pool.model, "inflight": r.inflight, "slots": r.slots,
                                 "calls": calls,
                                 "kv": r.stats.kv, "running": r.stats.running, "waiting": r.stats.waiting,
                                 "prefix_hits": r.stats.prefix_hits, "prefix_queries": r.stats.prefix_queries,
                                 "preemptions": r.stats.preemptions, "healthy": r.stats.healthy,
                                 "signal_age_s": None if age == float("inf") else round(age, 2)})
        sessions, agents = self.admission.sessions.active()
        self.live.sessions.clear()  # tenants idle past the window drop out instead of lingering
        self.live.agents.clear()
        for tenant, n in sessions.items():
            self.live.sessions.labels(tenant).set(n)
        for (tenant, role), n in agents.items():
            self.live.agents.labels(tenant, role).set(n)
        return {"lanes": lanes, "replicas": replicas, "sessions": sessions, "overflow": self.overflow.state(),
                "template_cost": [{"pool": p, "chat_template_kwargs": json.loads(k), **c} for (p, k), c in self.template.known.items()],
                "agents": {f"{t}/{r}": n for (t, r), n in agents.items()}, "mode_features": self.cfg.features}

    # ---- admin (the experiment scripts call these) ----

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose one of {', '.join(MODES)}")
        self.cfg.mode = mode
        self.cfg.raw["mode"] = mode
        self.log.mode = mode
        self._set_mode_gauge()
        if not self.cfg.features["queue"]:
            self.sched.drain_all()
        self.log.emit("mode", mode=mode, features=self.cfg.features)

    def apply_config(self, cfg: Config) -> None:
        """Switch every box to new settings at once (POST /admin/config). Calls already running
        finish as they are; queued ones are re-ordered by the new policy. Pools are rebuilt
        only if their replicas changed (the routing sweep's 2×9B fleet)."""
        old = self.cfg
        self.cfg = cfg
        self.overflow = OverflowGate.from_config(cfg.raw.get("overflow"), previous=self.overflow)
        for name, pc in cfg.pools.items():
            pool = self.pools.get(name)
            if pool is None or [r.url for r in pool.replicas] != pc.replicas:
                from router.pools import LivePool

                new = LivePool(name, pc.model, pc.replicas, slots=pc.slots_per_replica, max_model_len=pc.max_model_len,
                               prefill_tps=pc.prefill_tps, itl_s=pc.itl_s, reasoning_tokens=pc.reasoning_tokens,
                               names=pc.replica_names)
                # a replica seen before keeps its object (POST /admin/config then scrapes them all before it answers)
                for i, r in enumerate(new.replicas):
                    same = self.replicas_seen.setdefault(r.url, r)
                    same.id, same.slots, same.pool = r.id, r.slots, name
                    new.replicas[i] = same
                self.pools[name] = new
            else:
                for r in pool.replicas:
                    r.slots = pc.slots_per_replica
        self.router.cfg = self.sched.cfg = cfg
        self.router.pools = self.sched.pools = self.pools
        for name, pool in self.pools.items():
            lane = self.sched.lanes.get(name)
            if lane is None or lane.pool is not pool:
                from gateway.queue import Lane

                self.sched.lanes[name] = Lane(pool, cfg.queue)
            else:
                lane.q = cfg.queue
        self.admission.cfg, self.admission.a = cfg, cfg.admission
        self.log.mode, self.log.variant = cfg.mode, cfg.variant
        self._set_mode_gauge()
        if not cfg.features["queue"]:
            self.sched.drain_all()
        for pool in self.pools.values():
            self.sched.pump(pool)
        self.log.emit("config", variant=cfg.variant, mode=cfg.mode, features=cfg.features,
                      changed=sorted(k for k in cfg.raw if cfg.raw.get(k) != old.raw.get(k)), **self._policy())

    def _policy(self) -> dict[str, Any]:
        """The thresholds behind admission and queue decisions, so the UI (and a replay) can
        say which rule refused a call."""
        return {"admission": dict(self.cfg.admission), "queue": dict(self.cfg.queue),
                "sla_classes": {n: vars(c) for n, c in self.cfg.sla_classes.items()},
                "routing": {"policy": self.cfg.routing.get("policy"), "spill": bool((self.cfg.routing.get("spill") or {}).get("enabled", True))},
                "pools": {n: {"model": p.model, "max_model_len": p.max_model_len, "slots_per_replica": p.slots_per_replica,
                              "replicas": len(p.replicas)} for n, p in self.cfg.pools.items()}}

    def reset(self) -> None:
        """Fresh buckets, sessions and routing memory: each run starts from the same state."""
        self.admission.reset()
        for pool in self.pools.values():
            for r in pool.replicas:
                r.blocks.clear()
        for lane in self.sched.lanes.values():
            lane.deficit = {t: 0.0 for t in lane.deficit}
            lane.waits.clear()
            lane.arrivals.clear()


def create_app(cfg: Config | None = None, *, tokenizer: bool = True) -> Any:
    from starlette.applications import Starlette
    from starlette.requests import Request as HTTPRequest
    from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    gw = LiveGateway(cfg or load_config(), tokenizer=tokenizer)
    base_raw = dict(gw.cfg.raw)  # what POST /admin/config patches over by default

    async def chat(request: HTTPRequest) -> Any:
        return await gw.chat(request)

    async def models(request: HTTPRequest) -> Any:
        return JSONResponse(listed_models(gw.cfg))

    async def metrics(request: HTTPRequest) -> Any:
        return PlainTextResponse(METRICS.render() + gw.live.render(), media_type="text/plain; version=0.0.4")

    async def healthz(request: HTTPRequest) -> Any:
        return JSONResponse({"ok": True, "mode": gw.cfg.mode, "tokenizer_exact": gw.tokens.exact})

    async def events(request: HTTPRequest) -> Any:
        q = gw.log.subscribe()
        backlog = int(request.query_params.get("backlog", "300"))

        async def stream():
            try:
                for ev in list(gw.log.recent)[-backlog:] if backlog else []:
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                gw.log.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def events_recent(request: HTTPRequest) -> Any:
        n = int(request.query_params.get("n", "1000"))
        return JSONResponse(list(gw.log.recent)[-n:])

    async def ingest(request: HTTPRequest) -> Any:
        """Events from the agent harness (agent spawned/finished, tool runs): same log, same UI."""
        data = await request.json()
        for ev in data if isinstance(data, list) else [data]:
            kind = str(ev.pop("kind", "event"))
            gw.live.harness.labels(kind, str(ev.get("tenant", ""))).inc()
            gw.log.emit(f"harness_{kind}", **ev)
        return JSONResponse({"ok": True})

    async def admin_state(request: HTTPRequest) -> Any:
        run = gw.log.run
        return JSONResponse({"mode": gw.cfg.mode, "features": gw.cfg.features,
                             "run": None if run is None else {"exp": run.exp, "run": run.run}, **gw.state()})

    async def admin_mode(request: HTTPRequest) -> Any:
        try:
            gw.set_mode(str((await request.json()).get("mode", "")))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"mode": gw.cfg.mode, "features": gw.cfg.features})

    async def admin_run(request: HTTPRequest) -> Any:
        data = await request.json()
        exp, run = str(data.get("exp", "")), str(data.get("run", ""))
        if not exp or not run:
            return JSONResponse({"error": "exp and run are required"}, status_code=400)
        if data.get("reset", True):
            gw.reset()
        r = gw.log.open_run(exp, run, data.get("meta") or {})
        return JSONResponse({"exp": exp, "run": run, "dir": str(r.dir.parent), "mode": gw.cfg.mode})

    async def admin_run_stop(request: HTTPRequest) -> Any:
        return JSONResponse(gw.log.close_run() or {"error": "no run open"})

    async def admin_config(request: HTTPRequest) -> Any:
        """GET: the settings in force. POST {"variant": name, "patch": {...}, "base": true}:
        apply `patch` over the base config file (base=false: over the current settings) as
        the named variant; the mode can be in the patch. Every later call and event records
        the variant name."""
        if request.method == "GET":
            return JSONResponse({"variant": gw.cfg.variant, "mode": gw.cfg.mode, "config": gw.cfg.raw})
        data = await request.json()
        start = base_raw if data.get("base", True) else gw.cfg.raw
        try:
            raw = merge(start, data.get("patch") or {})
            cfg = from_raw(raw, variant=str(data.get("variant") or "custom"))
            cfg.results_dir = gw.cfg.results_dir
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
        gw.apply_config(cfg)
        # fresh engine signals before the first call under the new settings (a replica back in a pool would otherwise
        # look stale to admission until the next background scrape: 503 no_signal)
        await asyncio.gather(*(scrape_replica(gw.client, p, r) for p in gw.pools.values() for r in p.replicas), return_exceptions=True)
        if data.get("reset", True):
            gw.reset()
        return JSONResponse({"variant": cfg.variant, "mode": cfg.mode, "features": cfg.features})

    async def admin_reset_prefix_cache(request: HTTPRequest) -> Any:
        """vLLM's own POST /reset_prefix_cache on every replica the gateway knows (the server needs
        VLLM_SERVER_DEV_MODE=1), so each run starts with a cold prefix cache. 502 unless
        every replica answered 200, so a run that was meant to start cold can't start warm."""
        out = {}
        # every replica the gateway knows, also one the current variant doesn't use, so the next variant starts cold too
        for r in list(gw.replicas_seen.values()):
            r.blocks.clear()  # what prefix-aware placement remembers sending there is gone with the cache
            try:
                resp = await gw.client.post(f"{r.url}/reset_prefix_cache", timeout=10)
                out[r.id] = resp.status_code
            except Exception as e:
                out[r.id] = str(e)
        ok = bool(out) and all(v == 200 for v in out.values())
        gw.log.emit("prefix_cache_reset", result=out, ok=ok)
        return JSONResponse({"ok": ok, "replicas": out}, status_code=200 if ok else 502)

    routes = [
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/v1/models", models),
        Route("/models", models),
        Route("/metrics", metrics),
        Route("/healthz", healthz),
        Route("/events", events),
        Route("/events/recent", events_recent),
        Route("/events/ingest", ingest, methods=["POST"]),
        Route("/admin/state", admin_state),
        Route("/admin/mode", admin_mode, methods=["POST"]),
        Route("/admin/run", admin_run, methods=["POST"]),
        Route("/admin/run/stop", admin_run_stop, methods=["POST"]),
        Route("/admin/reset_prefix_cache", admin_reset_prefix_cache, methods=["POST"]),
        Route("/admin/config", admin_config, methods=["GET", "POST"]),
    ]
    ui = ROOT / "ui"
    if ui.is_dir():
        class FreshStatic(StaticFiles):
            """The UI is revalidated on every load, so an updated page is never served stale."""

            async def get_response(self, path: str, scope: Any) -> Any:
                resp = await super().get_response(path, scope)
                resp.headers["Cache-Control"] = "no-cache"
                return resp

        routes.append(Mount("/ui", app=FreshStatic(directory=str(ui), html=True), name="ui"))

    @contextlib.asynccontextmanager
    async def lifespan(app: Any):
        await gw.start()
        yield
        await gw.stop()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.gateway = gw
    return app


def main(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per scrape otherwise
    host = host or os.environ.get("SERVE_HOST", "0.0.0.0")
    port = int(os.environ.get("SERVE_PORT", port or 8080))
    cfg = load_config()
    print(f"orch-serve on http://{host}:{port} mode={cfg.mode} pools={', '.join(f'{n}→{p.replicas}' for n, p in cfg.pools.items())}", flush=True)
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning", timeout_keep_alive=75)

if __name__ == "__main__":
    main()
