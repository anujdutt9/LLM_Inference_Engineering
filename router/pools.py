from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from gateway.types import KV_SATURATION, Snapshot

_COUNTERS = {
    "vllm:prefix_cache_hits_total": "prefix_hits",
    "vllm:prefix_cache_queries_total": "prefix_queries",
    "vllm:num_preemptions_total": "preemptions",
}


def parse_vllm_metrics(text: str, model: str | None = None) -> dict[str, float]:
    """KV usage (max over engines), running/waiting and prefix/preemption counters (summed).
    With `model` set, only series labelled model_name="<model>" count (two models can share a
    scrape)."""
    usage: float | None = None
    waiting: float | None = None
    running: float | None = None
    counters: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if model and "{" in line and "model_name=" in line and f'model_name="{model}"' not in line:
            continue
        try:
            value = float(line.rsplit(None, 1)[-1])
        except ValueError:
            continue

        if name in ("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc"):
            usage = value if usage is None else max(usage, value)
        elif name == "vllm:num_requests_waiting":
            waiting = (waiting or 0.0) + value
        elif name == "vllm:num_requests_running":
            running = (running or 0.0) + value
        elif name in _COUNTERS:
            counters[_COUNTERS[name]] = counters.get(_COUNTERS[name], 0.0) + value
    if usage is not None and usage > 1.0:
        usage = usage / 100.0
    out: dict[str, float] = dict(counters)
    if usage is not None:
        out["gpu_cache_usage"] = usage
    if waiting is not None:
        out["waiting"] = waiting
    if running is not None:
        out["running"] = running
    return out

# ---- live gateway (gateway/serve.py): one pool per model, async, scraped every second ----


@dataclass
class EngineStats:
    """An engine's own numbers as last scraped from /metrics (up to a second stale)."""

    kv: float | None = None
    running: float = 0.0
    waiting: float = 0.0
    prefix_hits: float = 0.0
    prefix_queries: float = 0.0
    preemptions: float = 0.0
    at: float = 0.0  # monotonic time of the scrape; 0 = never
    healthy: bool = False

    def age(self, now: float | None = None) -> float:
        return float("inf") if not self.at else (now or time.monotonic()) - self.at


@dataclass
class LiveReplica:
    """One vLLM server. `inflight` is our own count of requests sent there (exact);
    `slots` is its --max-num-seqs, the most the gateway queue lets through at once."""

    id: str
    url: str
    pool: str
    slots: int
    inflight: int = 0
    stats: EngineStats = field(default_factory=EngineStats)
    blocks: OrderedDict = field(default_factory=OrderedDict)  # prefix blocks sent here (LRU)
    max_blocks: int = 50_000

    def remember(self, blocks: list[str]) -> None:
        for b in blocks:
            self.blocks[b] = None
            self.blocks.move_to_end(b)
        while len(self.blocks) > self.max_blocks:
            self.blocks.popitem(last=False)

    def prefix_match(self, blocks: list[str]) -> float:
        """Share of the prompt's leading blocks this replica was sent before."""
        if not blocks:
            return 0.0
        n = 0
        for b in blocks:
            if b not in self.blocks:
                break
            n += 1
        return n / len(blocks)

    def snapshot(self) -> Snapshot:
        """This replica as a Snapshot, the input of the load scorers (router/router.py)."""
        kv = self.stats.kv
        return Snapshot(
            pod_id=self.id,
            age_s=self.stats.age(),
            healthy=self.stats.healthy,
            saturating=bool(kv is not None and kv > KV_SATURATION),
            kv_free_ratio=None if kv is None else 1.0 - kv,
            tokens_in_flight=self.inflight,
            active_requests=self.inflight,
            queue_depth=int(self.stats.waiting),
            waiting=int(self.stats.waiting),
            running=int(self.stats.running),
        )


class LivePool:
    def __init__(self, name: str, model: str, urls: list[str], *, slots: int, max_model_len: int, prefill_tps: float, itl_s: float,
                 reasoning_tokens: float = 0.0, names: dict[str, str] | None = None) -> None:
        self.name = name
        self.model = model
        self.max_model_len = max_model_len
        names = {u.rstrip("/"): n for u, n in (names or {}).items()}
        self.replicas = [LiveReplica(id=names.get(u.rstrip("/"), f"{name}-{i}"), url=u.rstrip("/"), pool=name, slots=slots)
                         for i, u in enumerate(urls)]
        # learned rates for the deadline estimate (EWMA), seeded from config
        self.prefill_tps = prefill_tps
        self.itl_s = itl_s
        self.reasoning_tokens = reasoning_tokens  # per call, when thinking is on
        self.service_s = 20.0  # mean time a request holds a slot

    def live(self, fresh_s: float = 3.0) -> list[LiveReplica]:
        """The replicas to route to: answering their metrics scrape within the last few seconds. A replica that
        died (or is restarting) drops out at once instead of failing every call sent to it until it returns.
        If none is live, all of them: the caller then sees the engine's own error, as before."""
        now = time.monotonic()
        up = [r for r in self.replicas if r.stats.healthy and r.stats.age(now) <= fresh_s]
        return up or list(self.replicas)

    def free(self, gated: bool) -> list[LiveReplica]:
        return [r for r in self.live() if not gated or r.inflight < r.slots]

    @property
    def slots(self) -> int:
        return sum(r.slots for r in self.replicas)

    @property
    def inflight(self) -> int:
        return sum(r.inflight for r in self.replicas)

    def kv(self) -> float | None:
        values = [r.stats.kv for r in self.replicas if r.stats.kv is not None]
        return max(values) if values else None

    def signal_age(self) -> float:
        return min((r.stats.age() for r in self.replicas), default=float("inf"))

    def learn(self, *, prefill_tps: float | None = None, itl_s: float | None = None, service_s: float | None = None,
              reasoning_tokens: float | None = None) -> None:
        a = 0.2
        if reasoning_tokens is not None and reasoning_tokens > 0:
            self.reasoning_tokens = (1 - a) * self.reasoning_tokens + a * reasoning_tokens
        if prefill_tps and prefill_tps > 0:
            self.prefill_tps = (1 - a) * self.prefill_tps + a * prefill_tps
        if itl_s and itl_s > 0:
            self.itl_s = (1 - a) * self.itl_s + a * itl_s
        if service_s and service_s > 0:
            self.service_s = (1 - a) * self.service_s + a * service_s


def build_model_pools(cfg: Any) -> dict[str, LivePool]:
    """One LivePool per model in the gateway config."""
    return {
        name: LivePool(
            name,
            p.model,
            p.replicas,
            slots=p.slots_per_replica,
            max_model_len=p.max_model_len,
            prefill_tps=p.prefill_tps,
            itl_s=p.itl_s,
            reasoning_tokens=p.reasoning_tokens,
            names=p.replica_names,
        )
        for name, p in cfg.pools.items()
    }


async def scrape_replica(client: Any, pool: LivePool, r: LiveReplica) -> None:
    try:
        resp = await client.get(f"{r.url}/metrics", timeout=3.0)
        resp.raise_for_status()
    except Exception:
        r.stats.healthy = False
        return
    v = parse_vllm_metrics(resp.text, pool.model)
    r.stats = EngineStats(
        kv=v.get("gpu_cache_usage"),
        running=v.get("running", 0.0),
        waiting=v.get("waiting", 0.0),
        prefix_hits=v.get("prefix_hits", 0.0),
        prefix_queries=v.get("prefix_queries", 0.0),
        preemptions=v.get("preemptions", 0.0),
        at=time.monotonic(),
        healthy=True,
    )


async def scrape_forever(client: Any, pools: dict[str, LivePool], interval_s: float = 1.0) -> None:
    """Engine load, ~1 s stale. One background loop, instead of a blocking GET /metrics
    per replica on every pick."""
    while True:
        await asyncio.gather(*(scrape_replica(client, p, r) for p in pools.values() for r in p.replicas), return_exceptions=True)
        await asyncio.sleep(interval_s)
