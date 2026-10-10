"""A replica that stops answering its metrics scrape (killed, restarting) drops out of routing at once, and comes
back when it answers again; with none answering, routing falls back to all (the caller sees the engine's error)."""

from __future__ import annotations

import time

from router.pools import EngineStats, LivePool


def _pool() -> LivePool:
    pool = LivePool("qwen-9b", "qwen-9b", ["http://a:8001", "http://b:8002"], slots=8, max_model_len=65536,
                    prefill_tps=17000, itl_s=0.009, names={"http://a:8001": "qwen-9b-a", "http://b:8002": "qwen-9b-b"})
    for r in pool.replicas:
        r.stats = EngineStats(kv=0.1, at=time.monotonic(), healthy=True)
    return pool


def test_a_dead_replica_is_not_routed_to():
    pool = _pool()
    a, b = pool.replicas
    b.stats.healthy = False  # its scrape failed, or a connection to it was refused
    assert pool.live() == [a] and pool.free(gated=True) == [a] and pool.free(gated=False) == [a]


def test_a_stale_replica_is_not_routed_to_and_comes_back_when_scraped():
    pool = _pool()
    a, b = pool.replicas
    b.stats.at = time.monotonic() - 10  # no answer for 10 s
    assert pool.live() == [a]
    b.stats = EngineStats(kv=0.0, at=time.monotonic(), healthy=True)  # answering again
    assert pool.live() == [a, b]


def test_with_none_live_routing_falls_back_to_all():
    pool = _pool()
    for r in pool.replicas:
        r.stats.healthy = False
    assert pool.live() == pool.replicas
