from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gateway.types import Call, Shed

# ---- the queue, the only box that can say "later" ----
#
# Without a gateway queue, requests under load wait inside vLLM, where nobody can see or reorder
# them. Here each model pool gets a lane. The
# gateway lets at most `slots` requests per replica into vLLM (its --max-num-seqs) and holds
# the rest; when a slot frees, the lane picks who goes next: deficit round-robin between
# tenants (so one tenant's six agents can't crowd out another's synthesis call), earliest
# effective deadline within a tenant, with aging so long jobs don't starve.


@dataclass
class Item:
    call: Call
    deadline: float  # absolute (monotonic) deadline for this call
    budget_s: float  # how long it may wait in the queue
    cost: float  # tokens, for deficit round-robin
    priority: int = 0  # SLA class priority (lower first), for the `priority` policy
    enqueued: float = field(default_factory=time.monotonic)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def effective_deadline(self, now: float, aging_gain: float) -> float:
        return self.deadline - aging_gain * (now - self.enqueued)


class Lane:
    def __init__(self, pool: Any, q: dict) -> None:
        self.pool = pool
        self.q = q
        self.by_tenant: dict[str, list[Item]] = {}
        self.order: deque[str] = deque()  # tenants in round-robin order
        self.deficit: dict[str, float] = {}
        self.arrivals: deque[float] = deque()  # for Little's law: L = λ·W
        self.waits: deque[float] = deque(maxlen=200)

    @property
    def depth(self) -> int:
        return sum(len(v) for v in self.by_tenant.values())

    @property
    def bound(self) -> int:
        return int(self.q.get("max_depth_per_lane", 64))

    def items(self) -> list[Item]:
        return [it for items in self.by_tenant.values() for it in items]

    def ordered(self) -> list[Item]:
        """The waiting calls in the order pop_next would release them now (for the UI),
        without changing the lane: pop_next run on a copy of its state."""
        import copy

        shadow = copy.copy(self)
        shadow.by_tenant = {t: list(items) for t, items in self.by_tenant.items()}
        shadow.order = copy.copy(self.order)
        shadow.deficit = copy.copy(self.deficit)
        out = []
        while (it := shadow.pop_next()) is not None:
            out.append(it)
        return out

    def depth_by_sla(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in self.items():
            out[it.call.req.sla] = out.get(it.call.req.sla, 0) + 1
        return out

    def est_wait(self) -> float:
        """Requests ahead × mean slot time ÷ slots: what a new request should expect."""
        if self.depth == 0 and self.pool.free(gated=True):
            return 0.0
        return (self.depth + 1) * self.pool.service_s / max(self.pool.slots, 1)

    def little_l(self, window_s: float = 60.0) -> float:
        now = time.monotonic()
        while self.arrivals and now - self.arrivals[0] > window_s:
            self.arrivals.popleft()
        mean_wait = sum(self.waits) / len(self.waits) if self.waits else 0.0
        return len(self.arrivals) / window_s * mean_wait

    def push(self, item: Item) -> None:
        t = item.call.req.tenant
        if t not in self.by_tenant:
            self.by_tenant[t] = []
            self.order.append(t)
            self.deficit.setdefault(t, 0.0)
        self.by_tenant[t].append(item)
        self.arrivals.append(time.monotonic())

    def remove(self, item: Item) -> None:
        items = self.by_tenant.get(item.call.req.tenant, [])
        if item in items:
            items.remove(item)

    def pop_next(self) -> Item | None:
        """The next call to let through, by queue.policy:
        fifo      oldest first; predictable, protects nobody
        priority  SLA class priority, then oldest; the bottom class can wait forever
        sjf       smallest call first (prompt + expected output); best mean, starves long calls
        edf       earliest effective deadline, aging so long waits improve their place
        edf_fair  edf within each tenant, deficit round-robin across tenants (the default)"""
        now = time.monotonic()
        gain = float(self.q.get("aging_gain", 0.5))
        policy = self.q.get("policy", "edf_fair")
        if policy != "edf_fair":
            items = self.items()
            if not items:
                return None
            if policy == "fifo":
                head = min(items, key=lambda it: it.enqueued)
            elif policy == "priority":
                head = min(items, key=lambda it: (it.priority, it.enqueued))
            elif policy == "sjf":
                head = min(items, key=lambda it: (it.cost, it.enqueued))
            else:  # edf
                head = min(items, key=lambda it: it.effective_deadline(now, gain))
            self.remove(head)
            return head
        quantum = float(self.q.get("drr_quantum_tokens", 8192))
        busy = [t for t in self.order if self.by_tenant.get(t)]
        if not busy:
            return None
        for _ in range(100_000):
            t = self.order[0]
            items = self.by_tenant.get(t)
            if not items:
                self.order.rotate(-1)
                self.deficit[t] = 0.0  # an idle tenant doesn't bank credit
                continue
            head = min(items, key=lambda it: it.effective_deadline(now, gain))
            if self.deficit[t] >= head.cost or len(busy) == 1:
                self.deficit[t] = max(0.0, self.deficit[t] - head.cost)
                items.remove(head)
                return head
            self.deficit[t] += quantum
            self.order.rotate(-1)
        return None


class LaneScheduler:
    """Hands each call a replica: at once without the gateway queue (modes baseline/route),
    else when a slot frees and its lane picks it (modes queue/full)."""

    def __init__(self, cfg: Any, pools: dict, router: Any) -> None:
        self.cfg = cfg
        self.pools = pools
        self.router = router
        self.lanes = {name: Lane(pool, cfg.queue) for name, pool in pools.items()}
        self.on_dispatch: list[Callable[[Call], None]] = []
        self.on_expired: list[Callable[[Item], None]] = []
        self._expiry: asyncio.Task | None = None

    async def acquire(self, call: Call, pool: Any) -> Any:
        gated = self.cfg.features["queue"]
        lane = self.lanes[pool.name]
        if not gated:
            replica, reason, scores = self.router.pick_replica(call.req, call.blocks, pool.live())
            self._take(replica, call, reason, scores)
            return replica
        if lane.depth >= lane.bound and (lane.depth or not pool.free(gated=True)):  # only a call that would wait
            raise Shed(503, "server_is_overloaded", "queue_full", 2, f"{pool.name} queue is full ({lane.depth})")
        sla = self.cfg.sla(call.req.sla)
        item = Item(
            call=call,
            deadline=time.monotonic() + sla.deadline_s,
            budget_s=sla.queue_budget_s,
            cost=call.req.prompt_tokens + min(call.max_tokens, 2000),
            priority=sla.priority,
        )
        lane.push(item)
        self.pump(pool)
        try:
            return await item.future
        except asyncio.CancelledError:  # the client went away while queued
            lane.remove(item)
            raise

    def release(self, replica: Any) -> None:
        replica.inflight = max(0, replica.inflight - 1)
        self.pump(self.pools[replica.pool])

    def pump(self, pool: Any) -> None:
        lane = self.lanes[pool.name]
        while lane.depth:
            free = pool.free(gated=True)
            if not free:
                return
            item = lane.pop_next()
            if item is None:
                return
            if item.future.done():
                continue
            replica, reason, scores = self.router.pick_replica(item.call.req, item.call.blocks, free)
            self._take(replica, item.call, reason, scores)
            lane.waits.append(time.monotonic() - item.enqueued)
            item.future.set_result(replica)

    def drain_all(self) -> None:
        """Let everything waiting through (switching to a mode without the queue)."""
        for pool in self.pools.values():
            lane = self.lanes[pool.name]
            while lane.depth:
                item = lane.pop_next()
                if item is None or item.future.done():
                    continue
                replica, reason, scores = self.router.pick_replica(item.call.req, item.call.blocks, pool.live())
                self._take(replica, item.call, reason, scores)
                item.future.set_result(replica)

    def _take(self, replica: Any, call: Call, reason: str, scores: dict[str, float]) -> None:
        replica.inflight += 1
        if self.cfg.features["prefix"]:
            # how many prompt-prefix blocks placement remembers per replica: sized to the replica's KV cache, so it
            # doesn't count as cached what vLLM has long evicted (default: the old 50,000)
            replica.max_blocks = int(self.cfg.routing.get("prefix_memory_blocks", 50_000))
            replica.remember(call.blocks)
        call.replica = replica.id
        call.route_reason = call.route_reason or reason
        call.route_scores = scores
        call.t_dispatched = time.monotonic()
        for cb in self.on_dispatch:
            cb(call)

    def start(self) -> None:
        self._expiry = asyncio.create_task(self._expire_forever())

    async def stop(self) -> None:
        if self._expiry:
            self._expiry.cancel()
            await asyncio.gather(self._expiry, return_exceptions=True)

    async def _expire_forever(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            for lane in self.lanes.values():
                for items in lane.by_tenant.values():
                    for item in list(items):
                        if now - item.enqueued > item.budget_s and not item.future.done():
                            items.remove(item)
                            item.future.set_exception(
                                Shed(503, "server_is_overloaded", "expired_in_queue", 5, f"waited {now - item.enqueued:.0f}s in the {lane.pool.name} queue")
                            )
                            for cb in self.on_expired:
                                cb(item)
