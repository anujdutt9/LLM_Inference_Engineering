"""The live gateway (gateway/serve.py) against two fake vLLM engines: streaming with tool
calls passes through intact, admission refuses with the right codes, the queue holds what
the slots can't take, routing follows the model (and spills), and every call is recorded."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from gateway.config import TenantConfig, load_config
from gateway.queue import Item, Lane
from gateway.serve import create_app
from gateway.types import Call, prompt_text, request_from_openai
from router.fake_vllm import FakeEngine
from router.fake_vllm import create_app as fake_app
from router.pools import scrape_replica

TOOLS = [{"type": "function", "function": {"name": "sql_query", "description": "run SQL",
                                           "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}}}}]


def _host_router(apps: dict) -> object:
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            return
        host = dict(scope["headers"]).get(b"host", b"").decode().split(":")[0]
        await apps[host](scope, receive, send)

    return app


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "full", **pool_slots: int) -> None:
        monkeypatch.setenv("ORCH_RESULTS_DIR", str(tmp_path / "results"))
        monkeypatch.setenv("POOL_QWEN_27B_URLS", "http://e27")
        monkeypatch.setenv("POOL_QWEN_9B_URLS", "http://e9")
        monkeypatch.setenv("ORCH_MODE", mode)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / "gateway" / "config-27b-9b.yaml")
        for name, n in pool_slots.items():
            self.cfg.pools[name.replace("_", "-")].slots_per_replica = n
        self.e27 = FakeEngine("qwen-27b", itl_s=0.001, prefill_tps=1e7)
        self.e9 = FakeEngine("qwen-9b", itl_s=0.001, prefill_tps=1e7, think=False)
        self.eo = FakeEngine("Qwen/Qwen3.8-27B-FP8", itl_s=0.001, prefill_tps=1e7)  # the overflow provider
        self.app = create_app(self.cfg, tokenizer=False)
        self.gw = self.app.state.gateway
        self.engines_transport = httpx.ASGITransport(app=_host_router({"e27": fake_app(self.e27), "e9": fake_app(self.e9), "overflow": fake_app(self.eo)}))

    async def __aenter__(self) -> "Rig":
        await self.gw.start(transport=self.engines_transport)
        await self.scrape()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://gw", timeout=30)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()
        await self.gw.stop()

    async def scrape(self) -> None:
        for pool in self.gw.pools.values():
            for r in pool.replicas:
                await scrape_replica(self.gw.client, pool, r)

    async def chat(self, model: str = "qwen-27b", *, tenant: str = "tenant-a", role: str = "subagent", session: str = "s1",
                   stream: bool = True, tools: bool = True, text: str = "How many customers churned?") -> httpx.Response:
        body: dict = {"model": model, "stream": stream, "messages": [{"role": "user", "content": text}]}
        if tools:
            body["tools"] = TOOLS
        headers = {"X-Tenant-ID": tenant, "X-Agent-Role": role, "X-Session-ID": session, "X-Agent-ID": f"{session}-{role}"}
        return await self.client.post("/v1/chat/completions", json=body, headers=headers)


def run(coro):
    return asyncio.run(coro)


def _records(rig: Rig, exp: str = "t", run_id: str = "r1") -> list[dict]:
    path = rig.cfg.results_dir / exp / run_id / "gateway" / "requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_stream_passes_tools_through_and_is_measured(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            await rig.client.post("/admin/run", json={"exp": "t", "run": "r1"})
            resp = await rig.chat()
            assert resp.status_code == 200
            assert '"tool_calls"' in resp.text and "data: [DONE]" in resp.text
            assert '"reasoning"' in resp.text  # the 27B thinks; the reasoning stream reaches the client
            up = rig.e27.requests[-1]
            assert up["tools"] == TOOLS  # the body goes upstream as sent
            assert up["model"] == "qwen-27b" and up["max_tokens"] == 32768  # subagent default
            assert up["priority"] == 2 and up["stream_options"]["include_usage"]
            assert "tenant" not in up
            await rig.client.post("/admin/run/stop")
            rec = _records(rig)[-1]
            assert rec["status"] == 200 and rec["tenant"] == "tenant-a" and rec["role"] == "subagent" and rec["sla"] == "agent-sub"
            assert rec["tool_calls"] == ["sql_query"] and rec["finish_reason"] == "tool_calls"
            assert rec["ttft_s"] is not None and rec["ttfat_s"] >= rec["ttft_s"]
            assert rec["reasoning_tokens_est"] > 0 and rec["prompt_tokens"] > 0
            assert rec["pool"] == "qwen-27b" and rec["replica"] == "qwen-27b-0"
            events = (rig.cfg.results_dir / "t" / "r1" / "gateway" / "events.jsonl").read_text()
            for kind in ("received", "admitted", "dispatched", "first_token", "first_answer", "tool_call", "done"):
                assert f'"kind": "{kind}"' in events, kind

    run(go())


def test_non_stream_reply_and_tool_result_turn(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            resp = await rig.chat("qwen-9b", stream=False, tools=False)
            assert resp.status_code == 200
            assert resp.json()["choices"][0]["message"]["content"].startswith("Here")
            assert rig.e9.requests[-1]["model"] == "qwen-9b"

    run(go())


def test_tenant_over_budget_is_429_with_retry_after(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            rig.cfg.tenants["tiny"] = TenantConfig(tokens_per_min=600, burst=600)
            resp = await rig.chat(tenant="tiny")
            assert resp.status_code == 429
            assert resp.json()["error"]["reason"] == "tenant_tokens"
            assert int(resp.headers["Retry-After"]) >= 1
            assert not rig.e27.requests  # refused before any GPU work

    run(go())


def test_kv_pressure_and_stale_signal_are_503(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            rig.e27.kv_override = 0.95
            await rig.scrape()
            resp = await rig.chat()
            assert resp.status_code == 503 and resp.json()["error"]["reason"] == "kv_pressure"
            assert "Retry-After" in resp.headers
            rig.gw.pools["qwen-9b"].replicas[0].stats.at = time.monotonic() - 60  # scrape stalled
            resp = await rig.chat("qwen-9b")
            assert resp.json()["error"]["reason"] == "no_signal"

    run(go())


def test_baseline_mode_admits_everything(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch, mode="baseline") as rig:
            rig.e27.kv_override = 0.99
            await rig.scrape()
            assert (await rig.chat()).status_code == 200
            assert "priority" not in rig.e27.requests[-1]  # no gateway queue, no priorities

    run(go())


def test_queue_holds_calls_beyond_the_slots(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch, qwen_9b=1) as rig:
            rig.e9.itl_s = 0.01
            await rig.client.post("/admin/run", json={"exp": "t", "run": "r1"})
            resps = await asyncio.gather(*(rig.chat("qwen-9b", session=f"s{i}", tools=False) for i in range(4)))
            assert all(r.status_code == 200 for r in resps)
            assert rig.e9.max_running == 1  # the gateway let one in at a time
            await rig.client.post("/admin/run/stop")
            waits = [r["queue_wait_s"] for r in _records(rig)]
            assert max(waits) > 0.1

    run(go())


def test_deficit_round_robin_lets_the_quiet_tenant_in_early():
    async def go():
        class P:
            service_s, slots, name = 10.0, 1, "p"

            def free(self, gated: bool) -> list:
                return []

        lane = Lane(P(), {"drr_quantum_tokens": 8192, "aging_gain": 0.5})
        now = time.monotonic()
        for i in range(6):
            call = Call(req=request_from_openai({"messages": [{"role": "user", "content": "x"}]}, {"X-Tenant-ID": "busy"}), body={})
            lane.push(Item(call=call, deadline=now + 100 + i, budget_s=100, cost=6000))
        quiet = Call(req=request_from_openai({"messages": [{"role": "user", "content": "x"}]}, {"X-Tenant-ID": "quiet"}), body={})
        lane.push(Item(call=quiet, deadline=now + 200, budget_s=100, cost=6000))
        order = [lane.pop_next().call.req.tenant for _ in range(7)]
        assert order.index("quiet") <= 2  # not behind all six of the busy tenant's calls

    run(go())


def test_subagent_call_spills_up_when_the_9b_lane_is_backed_up(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch, qwen_9b=1) as rig:
            nine = rig.gw.pools["qwen-9b"]
            nine.replicas[0].inflight = 1
            lane = rig.gw.sched.lanes["qwen-9b"]
            for i in range(20):
                call = Call(req=request_from_openai({"messages": [{"role": "user", "content": "x"}]}, {"X-Tenant-ID": "b"}), body={})
                lane.push(Item(call=call, deadline=time.monotonic() + 100, budget_s=100, cost=1000))
            req = request_from_openai({"model": "qwen-9b", "messages": []}, {"X-Agent-Role": "subagent"})
            pool, reason = rig.gw.router.choose_pool(req, rig.gw.sched.lanes)
            assert (pool.name, reason) == ("qwen-27b", "spill")
            lead = request_from_openai({"model": "qwen-9b", "messages": []}, {"X-Agent-Role": "lead"})
            assert rig.gw.router.choose_pool(lead, rig.gw.sched.lanes)[1] == "model"  # leads never move

    run(go())


def test_unknown_model_refused_and_models_listed(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            resp = await rig.chat("gpt-6")
            assert resp.status_code == 400 and resp.json()["error"]["type"] == "model_not_allowed"
            data = (await rig.client.get("/v1/models")).json()["data"]
            assert {(m["id"], m["max_model_len"]) for m in data} == {("qwen-27b", 65536), ("qwen-9b", 65536)}

    run(go())


def test_metrics_and_mode_switch(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            await rig.chat()
            text = (await rig.client.get("/metrics")).text
            assert "orch_requests_total" in text  # the request/shed counter families are there too
            assert 'orch_ttft_seconds_count{model="qwen-27b",role="subagent",sla="agent-sub",tenant="tenant-a"} 1.0' in text
            assert 'orch_tool_calls_total{model="qwen-27b",tenant="tenant-a",tool="sql_query"} 1.0' in text
            r = await rig.client.post("/admin/mode", json={"mode": "queue"})
            assert r.json()["features"]["admission"] is False
            assert (await rig.client.post("/admin/mode", json={"mode": "nope"})).status_code == 400

    run(go())


def test_a_tinyharness_session_is_its_own_tenant(tmp_path, monkeypatch):
    """No X-Tenant-ID: the harness session is the tenant (one per terminal), the role picks
    the SLA class, and the agent name and task label are recorded."""
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            headers = {"X-Session-ID": "1790993315-ab12cd34", "X-Agent-ID": "1790993315-ab12cd34-lead",
                       "X-Agent-Role": "lead", "X-Agent-Name": "lead", "X-Task": "sql-review"}
            body = {"model": "qwen-27b", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
            assert (await rig.client.post("/v1/chat/completions", json=body, headers=headers)).status_code == 200
            assert rig.e27.requests[-1]["max_tokens"] == 32768 and rig.e27.requests[-1]["priority"] == 1
            run = rig.gw.log.run
            assert run is not None and run.exp == "live"  # always logging, no run opened
            rec = json.loads((run.dir / "requests.jsonl").read_text().splitlines()[-1])
            assert (rec["tenant"], rec["sla"], rec["task"]) == ("th-ab12cd34", "agent-lead", "sql-review")

    run(go())


def test_queue_policies_pick_differently():
    async def go():
        class P:
            service_s, slots, name = 10.0, 1, "p"

            def free(self, gated: bool) -> list:
                return []

        def item(tenant: str, cost: float, priority: int, deadline: float, enqueued: float) -> Item:
            call = Call(req=request_from_openai({"messages": [{"role": "user", "content": "x"}]}, {"X-Tenant-ID": tenant}), body={})
            it = Item(call=call, deadline=deadline, budget_s=100, cost=cost, priority=priority)
            it.enqueued = enqueued
            return it

        now = time.monotonic()
        picks = {}
        for policy in ("fifo", "priority", "sjf", "edf"):
            lane = Lane(P(), {"policy": policy, "aging_gain": 0.0})
            old_big = item("a", 9000, 3, now + 50, now - 10)  # oldest, biggest, lowest priority, middle deadline
            urgent = item("b", 5000, 2, now + 5, now - 5)  # earliest deadline
            small = item("c", 100, 1, now + 90, now - 1)  # smallest, highest priority, newest
            for it in (old_big, urgent, small):
                lane.push(it)
            picks[policy] = lane.pop_next().call.req.tenant
        assert picks == {"fifo": "a", "priority": "c", "sjf": "c", "edf": "b"}

    run(go())


def test_a_variant_applies_live_and_is_recorded(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            r = await rig.client.post("/admin/config", json={"variant": "fifo-noadmit",
                                                              "patch": {"mode": "queue", "queue": {"policy": "fifo"}}})
            assert r.json() == {"variant": "fifo-noadmit", "mode": "queue", "features": rig.gw.cfg.features}
            assert rig.gw.sched.lanes["qwen-27b"].q["policy"] == "fifo"
            assert (await rig.chat()).status_code == 200
            rec = json.loads((rig.gw.log.run.dir / "requests.jsonl").read_text().splitlines()[-1])
            assert rec["variant"] == "fifo-noadmit" and rec["mode"] == "queue"
            cfg = (await rig.client.get("/admin/config")).json()
            assert cfg["config"]["queue"]["policy"] == "fifo"
            # a new variant starts from the file again, not from the last variant
            await rig.client.post("/admin/config", json={"variant": "default", "patch": {}})
            assert rig.gw.cfg.queue["policy"] == "edf_fair" and rig.gw.cfg.mode == "full"

    run(go())


def test_a_replica_that_stays_keeps_its_signal_across_a_config_change(tmp_path, monkeypatch):
    """Changing a pool's replica list (Exp-4's one ↔ two replicas) must not drop what was scraped from the
    replicas that stay: a fresh object has no signal, and admission would refuse with 503 no_signal."""
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            before = rig.gw.pools["qwen-9b"].replicas[0]
            assert before.stats.kv is not None
            monkeypatch.delenv("POOL_QWEN_9B_URLS")  # let the patches set the replica list
            names = {"http://e9": "qwen-9b-a", "http://e9x": "qwen-9b-b"}
            await rig.client.post("/admin/config", json={"variant": "two", "patch": {
                "pools": {"qwen-9b": {"replicas": ["http://e9", "http://e9x"], "replica_names": names}}}})
            await rig.client.post("/admin/config", json={"variant": "only-b", "patch": {
                "pools": {"qwen-9b": {"replicas": ["http://e9x"], "replica_names": names}}}})
            await rig.client.post("/admin/config", json={"variant": "one", "patch": {
                "pools": {"qwen-9b": {"replicas": ["http://e9"], "replica_names": names}}}})
            (after,) = rig.gw.pools["qwen-9b"].replicas  # back after a variant without it: the same object
            assert after is before and after.id == "qwen-9b-a" and after.stats.kv is not None
            resp = await rig.chat("qwen-9b")  # mode full, no scrape in between
            assert resp.status_code == 200
            after.remember(["block-1"])  # what prefix-aware placement remembers sending there ...
            await rig.client.post("/admin/reset_prefix_cache")
            assert not after.blocks  # ... until the engine's prefix cache is emptied

    run(go())


def test_ttfat_check_refuses_when_reasoning_would_blow_the_answer_target(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            await rig.client.post("/admin/config", json={"variant": "tight", "patch": {
                "admission": {"checks": ["ttfat"], "estimate_slack": 1.0},
                "sla_classes": {"agent-sub": {"ttfat_s": 5}},
                "pools": {"qwen-27b": {"reasoning_tokens": 2000, "itl_s": 0.05}}}})
            resp = await rig.chat()  # 2000 reasoning tokens × 50 ms = 100 s before the answer
            assert resp.status_code == 429 and resp.json()["error"]["reason"] == "ttfat_unmeetable"
            body = {"model": "qwen-27b", "stream": True, "messages": [{"role": "user", "content": "hi"}],
                    "chat_template_kwargs": {"enable_thinking": False}}
            ok = await rig.client.post("/v1/chat/completions", json=body, headers={"X-Agent-Role": "subagent", "X-Tenant-ID": "t"})
            assert ok.status_code == 200  # no thinking: no reasoning to wait for

    run(go())


def test_spill_can_be_switched_off(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch, qwen_9b=1) as rig:
            await rig.client.post("/admin/config", json={"variant": "nospill", "patch": {"routing": {"spill": {"enabled": False}}}})
            rig.gw.pools["qwen-9b"].replicas[0].inflight = 1
            lane = rig.gw.sched.lanes["qwen-9b"]
            for _ in range(20):
                call = Call(req=request_from_openai({"messages": []}, {"X-Tenant-ID": "b"}), body={})
                lane.push(Item(call=call, deadline=time.monotonic() + 100, budget_s=100, cost=1000))
            req = request_from_openai({"model": "qwen-9b", "messages": []}, {"X-Agent-Role": "subagent"})
            assert rig.gw.router.choose_pool(req, rig.gw.sched.lanes)[1] == "model"

    run(go())


def test_prefix_cache_reset_reports_failure(tmp_path, monkeypatch):
    """The sweep relies on this: 200 only when every engine reset, else 502 naming the one
    that didn't, so a run meant to start cold can't silently start warm."""
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            r = await rig.client.post("/admin/reset_prefix_cache")
            assert r.status_code == 200 and r.json()["ok"] is True
            rig.gw.pools["qwen-9b"].replicas[0].url = "http://gone"
            r = await rig.client.post("/admin/reset_prefix_cache")
            assert r.status_code == 502 and r.json()["ok"] is False
            assert r.json()["replicas"][rig.gw.pools["qwen-27b"].replicas[0].id] == 200

    run(go())


def test_idle_tenants_leave_the_active_sessions_gauge(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            await rig.chat(tenant="tenant-a")
            rig.gw.state()
            assert 'orch_sessions_active{tenant="tenant-a"}' in (await rig.client.get("/metrics")).text
            rig.gw.admission.sessions.last_seen.clear()
            rig.gw.admission.sessions.agents.clear()
            rig.gw.state()
            assert 'orch_sessions_active{tenant="tenant-a"}' not in (await rig.client.get("/metrics")).text

    run(go())


def test_lane_order_preview_matches_pop_order_and_changes_nothing():
    """The UI draws each queue in release order (Lane.ordered); reading it must not move anything."""
    async def go():
        class P:
            service_s, slots, name = 10.0, 1, "p"

            def free(self, gated: bool) -> list:
                return []

        now = time.monotonic()
        for policy in ("fifo", "sjf", "edf", "edf_fair"):
            lane = Lane(P(), {"policy": policy, "aging_gain": 0.0, "drr_quantum_tokens": 4000})
            for i, (tenant, cost) in enumerate([("a", 9000), ("b", 300), ("a", 200), ("c", 5000), ("b", 7000)]):
                call = Call(req=request_from_openai({"messages": [{"role": "user", "content": "x"}]}, {"X-Tenant-ID": tenant}), body={})
                it = Item(call=call, deadline=now + 10 * (5 - i), budget_s=100, cost=cost, priority=i % 3)
                it.enqueued = now - 10 + i
                lane.push(it)
            preview = [it.call.req.id for it in lane.ordered()]
            assert lane.depth == 5
            popped = [lane.pop_next().call.req.id for _ in range(5)]
            assert preview == popped, policy

    run(go())


def test_state_lists_the_calls_in_each_engine_slot(tmp_path, monkeypatch):
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            rig.e27.itl_s = 0.05
            task = asyncio.create_task(rig.chat(tools=False))
            for _ in range(100):
                await asyncio.sleep(0.01)
                calls = [c for r in rig.gw.state()["replicas"] for c in r["calls"]]
                if calls:
                    break
            assert calls and calls[0]["tenant"] == "tenant-a" and calls[0]["phase"] in ("prefill", "thinking", "answering")
            await task
            assert not [c for r in rig.gw.state()["replicas"] for c in r["calls"]]

    run(go())


def test_every_non_200_has_a_layer_code_and_reason(tmp_path, monkeypatch):
    """Order of the no's: guardrails 400 → admission 429 → capacity 503; each recorded
    with the layer that decided it, in requests.jsonl, the event log and orch_rejections_total."""
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            r = await rig.client.post("/v1/chat/completions", content=b"not json", headers={"X-Tenant-ID": "tenant-a"})
            assert r.status_code == 400
            r = await rig.client.post("/v1/chat/completions", json={"model": "gpt-6", "messages": [{"role": "user", "content": "x"}]},
                                      headers={"X-Tenant-ID": "tenant-a"})
            assert r.status_code == 400
            rig.cfg.tenants["tiny"] = TenantConfig(tokens_per_min=600, burst=600)
            assert (await rig.chat(tenant="tiny")).status_code == 429
            rig.e27.kv_override = 0.95
            await rig.scrape()
            assert (await rig.chat()).status_code == 503
            recs = [json.loads(line) for line in (rig.gw.log.run.dir / "requests.jsonl").read_text().splitlines()]
            got = [(x["status"], x["layer"], x["shed_reason"]) for x in recs]
            assert got == [(400, "edge", "invalid_json"), (400, "edge", "model_not_allowed"),
                           (429, "admission", "tenant_tokens"), (503, "admission", "kv_pressure")]
            text = (await rig.client.get("/metrics")).text
            assert 'orch_rejections_total{code="429",layer="admission",model="qwen-27b",reason="tenant_tokens",tenant="tiny"} 1.0' in text
            events = [e for e in rig.gw.log.recent if e["kind"] == "shed"]
            assert [e["layer"] for e in events] == ["edge", "edge", "admission", "admission"]

    run(go())


def _overflow_rig(tmp_path, monkeypatch, **gate):
    monkeypatch.setenv("OVERFLOW_BASE_URL", "http://overflow/v1")
    monkeypatch.setenv("OVERFLOW_API_KEY", "test-key")
    rig = Rig(tmp_path, monkeypatch)
    from gateway.overflow import OverflowGate

    rig.gw.overflow = OverflowGate.from_config({"enabled": True, "dry_run": False,
                                                "targets": {"qwen-27b": "Qwen/Qwen3.8-27B-FP8"}, **gate})
    return rig


def test_capacity_refusal_overflows_to_the_same_model_elsewhere(tmp_path, monkeypatch):
    """503 kv_pressure on the 27B: the call is answered by the overflow provider (200), recorded
    with via=overflow and the refusal it came from; the local engine never saw it."""
    async def go():
        async with _overflow_rig(tmp_path, monkeypatch) as rig:
            rig.e27.kv_override = 0.95
            await rig.scrape()
            r = await rig.chat(tools=False)
            assert r.status_code == 200 and r.headers.get("x-orch-via") == "overflow"
            assert len(rig.eo.requests) == 1 and rig.eo.requests[0]["model"] == "Qwen/Qwen3.8-27B-FP8"
            assert "priority" not in rig.eo.requests[0] and not rig.e27.requests
            # engine-specific fields stay local (Superlinked 400s on chat_template_kwargs.reasoning_effort)
            assert "chat_template_kwargs" not in rig.eo.requests[0] and "thinking_token_budget" not in rig.eo.requests[0]
            rec = json.loads((rig.gw.log.run.dir / "requests.jsonl").read_text().splitlines()[-1])
            assert (rec["status"], rec["via"], rec["overflow_from"]) == (200, "overflow", "kv_pressure")
            assert rig.gw.overflow.sent == 1 and rig.gw.overflow.inflight == 0
            text = (await rig.client.get("/metrics")).text
            assert 'orch_overflow_total{decision="send",pool="qwen-27b",reason="kv_pressure"} 1.0' in text

    run(go())


def test_overflow_gate_keeps_429_local_and_respects_budget_and_dry_run(tmp_path, monkeypatch):
    async def go():
        async with _overflow_rig(tmp_path, monkeypatch, max_calls=1) as rig:
            rig.cfg.tenants["tiny"] = TenantConfig(tokens_per_min=600, burst=600)
            assert (await rig.chat(tenant="tiny", tools=False)).status_code == 429  # the tenant's own budget: stays
            rig.e27.kv_override = 0.95
            await rig.scrape()
            assert (await rig.chat(tools=False)).status_code == 200   # first capacity refusal leaves
            assert (await rig.chat(tools=False)).status_code == 503   # budget of 1 spent: refused locally
            decisions = [e["decision"] for e in rig.gw.log.recent if e["kind"] == "overflow"]
            assert decisions == ["stays_local_code", "send", "budget_spent"]
            rig.gw.overflow.dry_run, rig.gw.overflow.max_calls = True, 0
            assert (await rig.chat(tools=False)).status_code == 503   # dry run: counted, not sent
            assert [e["decision"] for e in rig.gw.log.recent if e["kind"] == "overflow"][-1] == "dry_run"
            assert len(rig.eo.requests) == 1

    run(go())


def test_queue_bound_zero_refuses_only_calls_that_would_wait(tmp_path, monkeypatch):
    """max_depth_per_lane 0 (the queue_depth sweep's depth_0): a free slot is used at once;
    only a call that would have to wait is refused."""
    async def go():
        async with Rig(tmp_path, monkeypatch, qwen_27b=1) as rig:
            rig.cfg.queue["max_depth_per_lane"] = 0
            rig.e27.itl_s = 0.05
            first = asyncio.create_task(rig.chat(tools=False, session="a"))
            await asyncio.sleep(0.2)
            second = await rig.chat(tools=False, session="b")  # the only slot is busy: refused, not queued
            assert (await first).status_code == 200
            assert second.status_code == 503 and second.json()["error"]["reason"] in ("queue_depth", "queue_full")

    run(go())


class _WordTokenizer:  # stands in for Qwen's tokenizer: one token per whitespace-separated word
    def encode(self, text, add_special_tokens=False):
        return type("Enc", (), {"ids": text.split()})()


def test_prompt_estimate_includes_the_measured_chat_template(tmp_path, monkeypatch):
    """The gateway counts message text and tool schemas; the engine also reads its chat template
    (role markers, Qwen3.8's reasoning-effort line, the tool instructions). Measured on the
    engine's /tokenize per pool and per chat_template_kwargs, then added to every estimate."""
    async def go():
        async with Rig(tmp_path, monkeypatch) as rig:
            rig.gw.tokens._tok = _WordTokenizer()
            assert await rig.gw._measure_template("qwen-27b")
            assert rig.gw.template.known[("qwen-27b", "{}")] == {"base": 30, "per_message": 5, "tools": 80}
            await rig.client.post("/admin/run", json={"exp": "t", "run": "r1"})
            await rig.chat(text="one two three", tools=True)
            body = {"messages": [{"role": "user", "content": "one two three"}], "tools": TOOLS}
            fake_count = len(prompt_text(body).split()) + 30 + 80  # what the fake engine's /tokenize says
            # a new chat_template_kwargs is measured on first sight (the first call uses the default's cost)
            kw = {"model": "qwen-27b", "stream": False, "messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                  {"role": "user", "content": "c"}], "chat_template_kwargs": {"enable_thinking": False}}
            await rig.client.post("/v1/chat/completions", json=kw, headers={"X-Tenant-ID": "t"})
            for _ in range(50):
                if ("qwen-27b", '{"enable_thinking": false, "reasoning_effort": "medium"}') in rig.gw.template.known:
                    break
                await asyncio.sleep(0.02)
            assert ("qwen-27b", '{"enable_thinking": false, "reasoning_effort": "medium"}') in rig.gw.template.known
            assert rig.gw.state()["template_cost"][0]["pool"] == "qwen-27b"
            await rig.client.post("/admin/run/stop")
            recs = _records(rig)
            assert recs[0]["prompt_tokens_est"] == fake_count  # matches the engine exactly
            assert recs[1]["prompt_tokens_est"] == 3 + 30 + 5 * 2  # 3 words, base, 2 extra messages

    run(go())


def test_max_tokens_is_lowered_to_fit_the_context_or_refused(tmp_path, monkeypatch):
    """vLLM 400s when prompt + max_tokens > max_model_len (Exp-1A's lead at 53.2K + 12,288 > 65,536).
    The gateway lowers max_tokens to the room left; with under min_output_tokens left it refuses.
    In every mode (baseline here, so admission doesn't refuse first)."""
    async def go():
        async with Rig(tmp_path, monkeypatch, mode="baseline") as rig:
            for p in rig.gw.pools.values():
                p.max_model_len = 3000
            await rig.client.post("/admin/run", json={"exp": "t", "run": "r1"})
            body = {"model": "qwen-27b", "stream": False, "max_tokens": 4000, "messages": [{"role": "user", "content": "x " * 1400}]}
            ok = await rig.client.post("/v1/chat/completions", json=body, headers={"X-Tenant-ID": "t"})
            assert ok.status_code == 200
            prompt = 1400 + 30  # near the limit the gateway asks the engine: the fake's /tokenize (words + 30)
            assert rig.e27.requests[-1]["max_tokens"] == 3000 - prompt - 32  # what was left
            full = await rig.client.post("/v1/chat/completions", json={**body, "messages": [{"role": "user", "content": "x " * 3200}]},
                                         headers={"X-Tenant-ID": "t"})
            assert full.status_code == 400 and full.json()["error"]["reason"] == "context_full"
            # what agent harnesses recognise as a context overflow (OpenAI's code, vLLM's wording)
            assert full.json()["error"]["code"] == "context_length_exceeded"
            assert "maximum context length is 3000 tokens" in full.json()["error"]["message"]
            assert len(rig.e27.requests) == 1  # never reached the engine
            await rig.client.post("/admin/run/stop")
            recs = _records(rig)
            assert (recs[0]["max_tokens_asked"], recs[0]["max_tokens"]) == (4000, 3000 - prompt - 32)
            assert (recs[1]["status"], recs[1]["shed_reason"], recs[1]["layer"]) == (400, "context_full", "edge")

    run(go())


def test_reasoning_policy_by_role(tmp_path, monkeypatch):
    """reasoning_by_role: the lead and subagents think at medium, both with a thinking budget
    that leaves their answer reserve, at most thinking_max; compaction summaries don't think.
    The client's values win."""
    async def go():
        async with Rig(tmp_path, monkeypatch, mode="baseline") as rig:
            async def send(role, **extra):
                body = {"model": "qwen-27b", "stream": False, "messages": [{"role": "user", "content": "hi"}], **extra}
                r = await rig.client.post("/v1/chat/completions", json=body, headers={"X-Tenant-ID": "t", "X-Agent-Role": role})
                assert r.status_code == 200
                return rig.e27.requests[-1]

            lead = await send("lead")
            assert lead["chat_template_kwargs"] == {"reasoning_effort": "medium"}
            assert (lead["max_tokens"], lead["thinking_token_budget"]) == (32768, 32768 - 16384)
            sub = await send("subagent")
            assert sub["chat_template_kwargs"] == {"reasoning_effort": "medium"}
            assert (sub["max_tokens"], sub["thinking_token_budget"]) == (32768, 12288)  # thinking_max, not 32768 − 4096
            comp = await send("compaction")
            assert comp["chat_template_kwargs"] == {"enable_thinking": False} and "thinking_token_budget" not in comp
            assert comp["max_tokens"] == 8192
            own = await send("subagent", chat_template_kwargs={"reasoning_effort": "low"}, thinking_token_budget=100)
            assert own["chat_template_kwargs"] == {"reasoning_effort": "low"} and own["thinking_token_budget"] == 100
            off = await send("subagent", chat_template_kwargs={"enable_thinking": False})
            assert "thinking_token_budget" not in off  # no thinking, no budget
            # after a context fit the budget follows the room left
            for p in rig.gw.pools.values():
                p.max_model_len = 6000
            fit = await send("subagent", messages=[{"role": "user", "content": "x " * 3000}])
            assert fit["thinking_token_budget"] == max(min(2048, fit["max_tokens"] // 2), fit["max_tokens"] - 4096)

    run(go())
