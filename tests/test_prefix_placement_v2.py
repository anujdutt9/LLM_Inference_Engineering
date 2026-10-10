"""prefix_p2c_v2: among ALL replicas holding the prompt's prefix, the least loaded one with a free slot gets the
call (prefix_p2c only looked at the first best match: a tie always went to the first replica, and a full holder fell
back to p2c though the other holder had room). The router's memory of what it sent is capped
(routing.prefix_memory_blocks), so it can be sized to the replica's KV cache."""

from __future__ import annotations

import time
from pathlib import Path

from gateway.config import load_config, merge, from_raw
from router.pools import EngineStats, LivePool
from router.router import ModelRouter

CFG = Path(__file__).resolve().parents[1] / "gateway" / "config.yaml"


def _setup(policy: str):
    base = load_config(CFG)
    cfg = from_raw(merge(base.raw, {"mode": "route", "routing": {"policy": policy}}))
    pool = LivePool("qwen-9b", "qwen-9b", ["http://a:8001", "http://b:8002"], slots=2, max_model_len=65536,
                    prefill_tps=17000, itl_s=0.009, names={"http://a:8001": "qwen-9b-a", "http://b:8002": "qwen-9b-b"})
    for r in pool.replicas:
        r.stats = EngineStats(kv=0.1, at=time.monotonic(), healthy=True)
    return ModelRouter(cfg, {"qwen-9b": pool}), pool


class _Req:
    prompt_tokens = 1000


def test_v2_sends_to_the_other_holder_when_the_first_is_full():
    router, pool = _setup("prefix_p2c_v2")
    a, b = pool.replicas
    doc = ["blk-1", "blk-2", "blk-3"]
    a.remember(doc); b.remember(doc)  # both hold the document
    a.inflight = 2                    # a is full
    rep, why, _ = router.pick_replica(_Req(), doc, pool.replicas)
    assert rep is b and why == "prefix"


def test_v2_breaks_a_tie_by_load_not_by_order():
    router, pool = _setup("prefix_p2c_v2")
    a, b = pool.replicas
    doc = ["blk-1"]
    a.remember(doc); b.remember(doc)
    a.inflight = 1                    # a busier, both have room
    rep, why, _ = router.pick_replica(_Req(), doc, pool.replicas)
    assert rep is b and why == "prefix"


def test_v1_keeps_its_measured_behaviour():
    router, pool = _setup("prefix_p2c")
    a, b = pool.replicas
    doc = ["blk-1"]
    a.remember(doc); b.remember(doc)
    a.inflight = 2                    # first holder full: v1 falls back to p2c, never 'prefix' on b
    _, why, _ = router.pick_replica(_Req(), doc, pool.replicas)
    assert why == "p2c"


def test_prefix_memory_is_capped():
    _, pool = _setup("prefix_p2c_v2")
    a = pool.replicas[0]
    a.max_blocks = 3
    a.remember(["x1", "x2", "x3", "x4"])
    assert list(a.blocks) == ["x2", "x3", "x4"]
