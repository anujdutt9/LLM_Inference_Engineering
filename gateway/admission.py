from __future__ import annotations

import time
from typing import TYPE_CHECKING

from gateway.types import Call, Request, Shed

if TYPE_CHECKING:
    from gateway.config import Config
    from router.pools import LivePool


class TokenBucket:
    """Per-tenant token budget on the gateway's own clock (a token bucket that refills
    continuously, not a fixed window)."""

    def __init__(self, per_min: float, burst: float | None = None) -> None:
        self.capacity = float(burst if burst is not None else per_min)
        self.refill = per_min / 60.0
        self.tokens = self.capacity
        self.ts = time.monotonic()

    def _tick(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.refill)
        self.ts = now

    def take(self, n: float, *, overdraft: float = 0.0) -> bool:
        """Take n tokens; with an overdraft the bucket may go that far below zero."""
        self._tick()
        if self.tokens - n < -overdraft:
            return False
        self.tokens -= n
        return True

    def give_back(self, n: float) -> None:
        self.tokens = min(self.capacity, self.tokens + n)

    def seconds_until(self, n: float) -> float:
        self._tick()
        return max(0.0, (n - self.tokens) / self.refill) if self.refill else 60.0


class Sessions:
    """Agent sessions and agents seen recently. A session already admitted is one we have
    invested in: refusing its 37th call wastes the first 36, and the harness then retries
    (4 attempts, and a failed teammate is restarted once)."""

    def __init__(self, idle_s: float = 900.0) -> None:
        self.idle_s = idle_s
        self.last_seen: dict[str, float] = {}
        self.tenant_of: dict[str, str] = {}
        self.agents: dict[str, tuple[str, str, float]] = {}  # agent -> (tenant, role, last seen)
        self.last_done: dict[str, float] = {}  # agent -> when its last call ended

    def known(self, session: str) -> bool:
        t = self.last_seen.get(session)
        return t is not None and time.monotonic() - t < self.idle_s

    def touch(self, req: Request) -> None:
        now = time.monotonic()
        self.last_seen[req.session] = now
        self.tenant_of[req.session] = req.tenant
        self.agents[req.agent] = (req.tenant, req.role, now)

    def active(self, window_s: float = 120.0) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
        now = time.monotonic()
        sessions: dict[str, int] = {}
        for s, t in self.last_seen.items():
            if now - t < window_s:
                tenant = self.tenant_of.get(s, "")
                sessions[tenant] = sessions.get(tenant, 0) + 1
        agents: dict[tuple[str, str], int] = {}
        for tenant, role, t in self.agents.values():
            if now - t < window_s:
                agents[(tenant, role)] = agents.get((tenant, role), 0) + 1
        return sessions, agents

    def reset(self) -> None:
        self.__init__(self.idle_s)


class Admission:
    """The only box that can say no, in this order: stale signal
    → fleet pressure → new sessions under pressure → tenant bucket → deadline. Every refusal
    has a status, a named reason and a Retry-After the client honours."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.a = cfg.admission
        self.buckets: dict[str, TokenBucket] = {}
        self.sessions = Sessions(float(self.a.get("session_idle_s", 900)))

    def bucket(self, tenant: str) -> TokenBucket:
        if tenant not in self.buckets:
            t = self.cfg.tenant(tenant)
            self.buckets[tenant] = TokenBucket(t.tokens_per_min, t.burst)
        return self.buckets[tenant]

    def reset(self) -> None:
        self.buckets.clear()
        self.sessions.reset()

    def reservation(self, call: Call) -> int:
        """Prompt plus the output reservation, capped: a declared max_tokens is booked, so
        clients asking for 12K by habit would be refused for capacity they won't use."""
        return call.req.prompt_tokens + min(call.max_tokens, int(self.a.get("reserve_cap", 4096)))

    def check(self, call: Call, pool: LivePool, *, lane_depth: int, lane_bound: int, est_wait_s: float) -> None:
        """Raise Shed if this call should not be admitted (gateway mode `full`). Runs the checks
        listed in admission.checks, in order."""
        a, req = self.a, call.req
        checks = set(a.get("checks", DEFAULT_CHECKS))
        kv = pool.kv()
        if "stale" in checks and pool.signal_age() > float(a.get("stale_s", 5)):
            raise Shed(503, "server_is_overloaded", "no_signal", 1, "engine metrics are stale; failing closed")
        if "kv" in checks and kv is not None and kv > float(a.get("kv_shed", 0.9)):
            raise Shed(503, "server_is_overloaded", "kv_pressure", 5, f"{pool.name} KV cache at {kv:.0%}")
        if "queue_bound" in checks and lane_depth >= lane_bound and (lane_depth or not pool.free(gated=True)):
            raise Shed(503, "server_is_overloaded", "queue_depth", 2, f"{pool.name} queue is full ({lane_depth})")
        known = self.sessions.known(req.session)
        if "new_session" in checks and not known and (
            (kv is not None and kv > float(a.get("new_session_kv", 0.8)))
            or lane_depth >= int(a.get("new_session_queue", 16))
        ):
            raise Shed(503, "server_is_overloaded", "new_session_shed", 10,
                       f"busy ({pool.name} KV {kv:.0%}, {lane_depth} queued): not starting new sessions now" if kv is not None
                       else f"busy ({pool.name} {lane_depth} queued): not starting new sessions now")
        cost = self.reservation(call)
        bucket = self.bucket(req.tenant)
        took = False
        if "bucket" in checks:
            overdraft = float(a.get("session_overdraft", 0.5)) * bucket.capacity if known else 0.0
            if not bucket.take(cost, overdraft=overdraft):
                raise Shed(
                    429, "rate_limit_error", "tenant_tokens", bucket.seconds_until(cost), f"tenant {req.tenant} is over its token budget"
                )
            took = True
        sla = self.cfg.sla(req.sla)
        slack = float(a.get("estimate_slack", 1.5))
        ttft, ttfat, total = self.estimate(call, pool, est_wait_s)
        call.estimates = {"ttft_s": round(ttft, 2), "ttfat_s": round(ttfat, 2), "total_s": round(total, 2), "slack": slack}
        for name, est, target, reason in (
            ("ttft", ttft, sla.ttft_s, "ttft_unmeetable"),
            ("ttfat", ttfat, sla.ttfat_s, "ttfat_unmeetable"),
            ("deadline", total, sla.deadline_s, "deadline_unmeetable"),
        ):
            if name in checks and target and est > slack * target:
                if took:
                    bucket.give_back(cost)  # refused: give the tokens back
                raise Shed(
                    429,
                    "rate_limit_error",
                    reason,
                    min(30.0, max(1.0, est - target)),
                    f"estimated {name} {est:.1f}s > {slack:g} × {target:g}s target for {sla.name}",
                    estimate_s=est,
                )

    def estimate(self, call: Call, pool: LivePool, est_wait_s: float) -> tuple[float, float, float]:
        """(TTFT, TTFAT, total) for this call now (plus reasoning): queue wait +
        prefill; + the reasoning this model usually does when thinking is on; + the answer.
        Decode uses the expected output, not the whole reservation."""
        ttft = est_wait_s + call.req.prompt_tokens / max(pool.prefill_tps, 1.0)
        thinking = (call.body.get("chat_template_kwargs") or {}).get("enable_thinking", True)
        ttfat = ttft + (pool.reasoning_tokens * pool.itl_s if thinking else 0.0)
        total = ttfat + min(call.max_tokens, 2000) * pool.itl_s
        return ttft, ttfat, total


DEFAULT_CHECKS = ("stale", "kv", "queue_bound", "new_session", "bucket", "ttft", "ttfat", "deadline")
