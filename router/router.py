from __future__ import annotations

import random
from collections.abc import Callable, Sequence

from gateway.metrics import METRICS
from gateway.types import STICKY_OVERLAP, Request, Shed, Snapshot

Scorer = Callable[[Snapshot, Request], float | None]

TOP_K = 4

def score_prefix_cache(snap: Snapshot, req: Request) -> float | None:
    if snap.prefix_tokens is None:
        return None
    if not req.prefix_hash or req.prompt_tokens <= 0:
        return 0.0
    hit = snap.prefix_tokens.get(req.prefix_hash, 0)
    return hit / req.prompt_tokens

def score_token_load(snap: Snapshot, req: Request) -> float | None:
    if snap.tokens_in_flight is None:
        return None
    return -float(snap.tokens_in_flight)

def score_active_request(snap: Snapshot, req: Request) -> float | None:
    if snap.active_requests is None:
        return None
    return -float(snap.active_requests)

def score_kv_utilisation(snap: Snapshot, req: Request) -> float | None:
    if snap.kv_free_ratio is None:
        return None
    return float(snap.kv_free_ratio)

def score_queue_depth(snap: Snapshot, req: Request) -> float | None:
    if snap.queue_depth is None:
        return None
    return -float(snap.queue_depth)

WEIGHTS: dict[Scorer, float] = {
    score_prefix_cache: 2.0,
    score_token_load: 1.0,
    score_active_request: 1.0,
    score_kv_utilisation: 1.0,
    score_queue_depth: 1.0,
}

def _score(snap: Snapshot, req: Request, scorers: Sequence[Scorer]) -> float:
    """Weighted sum of the scorers that have a value; -inf when none does."""
    total = 0.0
    weight = 0.0
    for fn in scorers:
        value = fn(snap, req)
        if value is None:
            continue
        w = WEIGHTS.get(fn, 1.0)
        total += w * value
        weight += w
    if weight == 0.0:
        return float("-inf")
    return total


# ---- route by model, then place inside the model's pool ----

LIVE_LOAD_SCORERS: list[Scorer] = [score_active_request, score_kv_utilisation, score_queue_depth]


class ModelRouter:
    """The request's model picks the pool, one pool per vLLM model; inside a pool, the policy
    in routing.policy places it (load scorers, sticky prefix, power of two choices).

    Spill: a subagent's call for the 9B may move up to the 27B when the 9B lane would break
    its deadline and the 27B has room. Calls never move down, which protects SQL and
    synthesis quality."""

    def __init__(self, cfg: object, pools: dict, *, rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self.pools = pools
        self.rng = rng or random.Random(7)

    def choose_pool(self, req: Request, lanes: dict) -> tuple[object, str]:
        pool_cfg = self.cfg.pool_for_model(req.model)
        if pool_cfg is None:
            raise Shed(
                404, "model_not_found", "unknown_model", None, f"no pool serves {req.model!r}; have {', '.join(self.cfg.models())}"
            )
        pool = self.pools[pool_cfg.name]
        spill = self.cfg.routing.get("spill") or {}
        if not self.cfg.features["spill"] or not spill.get("enabled", True):
            return pool, "model"
        target = self.pools.get(spill.get("to", ""))
        if target is None or pool.name != spill.get("from") or req.role not in spill.get("roles", []):
            return pool, "model"
        budget = self.cfg.sla(req.sla).queue_budget_s
        target_kv = target.kv()
        if (
            lanes[pool.name].est_wait() > 0.5 * budget
            and lanes[target.name].depth == 0
            and target.free(gated=self.cfg.features["queue"])
            and (target_kv is None or target_kv < float(spill.get("max_target_kv", 0.7)))
        ):
            return target, "spill"
        return pool, "model"

    def pick_replica(self, req: Request, blocks: list[str], candidates: list) -> tuple[object, str, dict[str, float]]:
        """Which replica, by routing.policy:
        round_robin   a counter; the baseline to beat
        random        any
        least_loaded  fewest in flight + KV in use, on ~1 s-old numbers (herds)
        p2c           power of two choices on load
        prefix_p2c    sticky to the replica holding ≥80% of the prompt's prefix if it has a
                      free slot, else power of two choices on prefix match + load
        prefix_p2c_v2 the least loaded of ALL replicas holding ≥80% of the prefix that has a free
                      slot (prefix_p2c looks only at the first best match, so a tie always went to the
                      first replica and a full holder fell back to p2c though another holder had room);
                      else power of two choices as prefix_p2c"""
        if len(candidates) == 1:
            return candidates[0], "single", {}
        policy = self.cfg.routing.get("policy", "prefix_p2c")
        if policy == "round_robin":
            self._rr = (getattr(self, "_rr", -1) + 1) % len(candidates)
            return candidates[self._rr], "round_robin", {}
        if policy == "random":
            return self.rng.choice(candidates), "random", {}
        if policy == "least_loaded":
            loads = {rep.id: rep.inflight / max(rep.slots, 1) + (rep.stats.kv or 0.0) for rep in candidates}
            return min(candidates, key=lambda rep: loads[rep.id]), "least_loaded", loads
        if policy == "prefix_p2c_v2" and self.cfg.features["prefix"]:
            holders = [rep for rep in candidates if rep.prefix_match(blocks) >= STICKY_OVERLAP and rep.inflight < rep.slots]
            if holders:
                METRICS.inc_sticky()
                pick = min(holders, key=lambda rep: (rep.inflight / max(rep.slots, 1), rep.stats.kv or 0.0))
                return pick, "prefix", {rep.id: rep.prefix_match(blocks) for rep in candidates}
        use_prefix = self.cfg.features["prefix"] and policy in ("prefix_p2c", "prefix_p2c_v2")
        w_prefix = float(self.cfg.routing.get("w_prefix", WEIGHTS[score_prefix_cache]))

        def score(rep: object) -> float:
            base = _score(rep.snapshot(), req, LIVE_LOAD_SCORERS)
            return base + (w_prefix * rep.prefix_match(blocks) if use_prefix else 0.0)

        scores = {rep.id: round(score(rep), 4) for rep in candidates}
        if use_prefix:
            best = max(candidates, key=lambda rep: rep.prefix_match(blocks))
            if best.prefix_match(blocks) >= STICKY_OVERLAP and best.inflight < best.slots:
                METRICS.inc_sticky()
                return best, "prefix", scores
        top = sorted(candidates, key=lambda rep: scores[rep.id], reverse=True)[: max(TOP_K, 2)]
        a, b = self.rng.sample(top, 2)
        METRICS.inc_pick()
        return (a if scores[a.id] >= scores[b.id] else b), "p2c", scores
