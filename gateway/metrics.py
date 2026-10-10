from __future__ import annotations

from collections import defaultdict

from gateway.types import Snapshot

SHED_REASONS = frozenset(
    {
        "tenant_tokens", "timeout_queue", "kv_free", "p99_spread", "no_eligible_pod",
        # live gateway (gateway/admission.py, gateway/queue.py)
        "no_signal", "kv_pressure", "queue_depth", "queue_full", "new_session_shed",
        "deadline_unmeetable", "ttft_unmeetable", "ttfat_unmeetable", "expired_in_queue", "unknown_model", "upstream_error",
    }
)

LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

STAGES = ("gateway", "pick", "local", "overflow", "e2e")

class Histogram:
    def __init__(self, buckets: tuple[float, ...] = LATENCY_BUCKETS) -> None:
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.inf = 0
        self.sum = 0.0
        self.count = 0

    def observe(self, seconds: float) -> None:
        value = max(0.0, float(seconds))
        self.sum += value
        self.count += 1
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1
                return
        self.inf += 1

    def render(self, name: str, labels: str = "", *, include_type: bool = True) -> list[str]:
        prefix = f"{labels}," if labels else ""
        lines = [f"# TYPE {name} histogram"] if include_type else []
        cumulative = 0
        for bound, n in zip(self.buckets, self.counts):
            cumulative += n
            lines.append(f'{name}_bucket{{{prefix}le="{bound}"}} {cumulative}')
        cumulative += self.inf
        lines.append(f'{name}_bucket{{{prefix}le="+Inf"}} {cumulative}')
        if labels:
            lines.append(f"{name}_sum{{{labels}}} {self.sum}")
            lines.append(f"{name}_count{{{labels}}} {self.count}")
        else:
            lines.append(f"{name}_sum {self.sum}")
            lines.append(f"{name}_count {self.count}")
        return lines

class Metrics:
    def __init__(self) -> None:
        self.requests_total = 0
        self.shed_total: dict[tuple[str, int], int] = defaultdict(int)
        self.pick_total = 0
        self.pick_unknown_snapshot_total = 0
        self.sticky_total = 0
        self.tokens_in_flight = {"prefill": 0, "decode": 0}
        self.kv_free_ratio = 0.0
        self.planner_desired = {"prefill": 0, "decode": 0}
        self.overflow_total = 0
        self.completed_total = 0
        self.kv_transfer_total = 0
        self.kv_transfer_tokens = 0
        self.kv_evict_total = 0
        self.place_total: dict[str, int] = defaultdict(int)
        self.latency: dict[str, Histogram] = {stage: Histogram() for stage in STAGES}
        self.replicas: dict[str, list[Snapshot]] = {"prefill": [], "decode": []}

    def inc_requests(self) -> None:
        self.requests_total += 1

    def inc_shed(self, reason: str, code: int) -> None:
        if reason not in SHED_REASONS:
            reason = "no_eligible_pod"
        self.shed_total[(reason, int(code))] += 1

    def inc_pick(self) -> None:
        self.pick_total += 1

    def inc_sticky(self) -> None:
        self.sticky_total += 1

    def inc_place(self, capability: str) -> None:
        self.place_total[capability or "text"] += 1

    def inc_completed(self) -> None:
        self.completed_total += 1

    def observe_duration(self, stage: str, seconds: float) -> None:
        if stage not in self.latency:
            self.latency[stage] = Histogram()
        self.latency[stage].observe(seconds)

    def reset(self) -> None:
        self.__init__()

    def render(self) -> str:
        lines = [
            "# TYPE orch_requests_total counter",
            f"orch_requests_total {self.requests_total}",
            "# TYPE orch_shed_total counter",
        ]
        if not self.shed_total:
            lines.append('orch_shed_total{reason="no_eligible_pod",code="503"} 0')
        for (reason, code), n in sorted(self.shed_total.items()):
            lines.append(f'orch_shed_total{{reason="{reason}",code="{code}"}} {n}')
        lines += [
            "# TYPE orch_pick_total counter",
            f"orch_pick_total {self.pick_total}",
            "# TYPE orch_pick_unknown_snapshot_total counter",
            f"orch_pick_unknown_snapshot_total {self.pick_unknown_snapshot_total}",
            "# TYPE orch_sticky_total counter",
            f"orch_sticky_total {self.sticky_total}",
            "# TYPE orch_tokens_in_flight gauge",
            f'orch_tokens_in_flight{{phase="prefill"}} {self.tokens_in_flight["prefill"]}',
            f'orch_tokens_in_flight{{phase="decode"}} {self.tokens_in_flight["decode"]}',
            "# TYPE orch_kv_free_ratio gauge",
            f"orch_kv_free_ratio {self.kv_free_ratio}",
            "# TYPE orch_planner_desired_replicas gauge",
            f'orch_planner_desired_replicas{{pool="prefill"}} {self.planner_desired["prefill"]}',
            f'orch_planner_desired_replicas{{pool="decode"}} {self.planner_desired["decode"]}',
            "# TYPE orch_overflow_total counter",
            f"orch_overflow_total {self.overflow_total}",
            "# TYPE orch_completed_total counter",
            f"orch_completed_total {self.completed_total}",
            "# TYPE orch_kv_transfer_total counter",
            f"orch_kv_transfer_total {self.kv_transfer_total}",
            "# TYPE orch_kv_transfer_tokens counter",
            f"orch_kv_transfer_tokens {self.kv_transfer_tokens}",
            "# TYPE orch_kv_evict_total counter",
            f"orch_kv_evict_total {self.kv_evict_total}",
            "# TYPE orch_place_total counter",
        ]
        if not self.place_total:
            lines.append('orch_place_total{capability="text"} 0')
        for cap, n in sorted(self.place_total.items()):
            lines.append(f'orch_place_total{{capability="{cap}"}} {n}')
        first = True
        for stage in STAGES:
            hist = self.latency.get(stage) or Histogram()
            lines += hist.render(
                "orch_request_duration_seconds",
                f'stage="{stage}"',
                include_type=first,
            )
            first = False
        lines += [
            "# TYPE orch_replica_healthy gauge",
            "# TYPE orch_replica_saturating gauge",
            "# TYPE orch_replica_kv_free_ratio gauge",
            "# TYPE orch_replica_tokens_in_flight gauge",
            "# TYPE orch_replica_waiting gauge",
            "# TYPE orch_replica_running gauge",
            "# TYPE orch_replica_queue_depth gauge",
            "# TYPE orch_replica_active_requests gauge",
        ]
        any_replica = False
        for pool, snaps in (("prefill", self.replicas["prefill"]), ("decode", self.replicas["decode"])):
            for snap in snaps:
                any_replica = True
                labels = f'pool="{pool}",pod="{snap.pod_id}"'
                lines.append(f'orch_replica_healthy{{{labels}}} {1 if snap.healthy else 0}')
                lines.append(f'orch_replica_saturating{{{labels}}} {1 if snap.saturating else 0}')
                if snap.kv_free_ratio is not None:
                    lines.append(f'orch_replica_kv_free_ratio{{{labels}}} {snap.kv_free_ratio}')
                if snap.tokens_in_flight is not None:
                    lines.append(f'orch_replica_tokens_in_flight{{{labels}}} {snap.tokens_in_flight}')
                if snap.waiting is not None:
                    lines.append(f'orch_replica_waiting{{{labels}}} {snap.waiting}')
                if snap.running is not None:
                    lines.append(f'orch_replica_running{{{labels}}} {snap.running}')
                if snap.queue_depth is not None:
                    lines.append(f'orch_replica_queue_depth{{{labels}}} {snap.queue_depth}')
                if snap.active_requests is not None:
                    lines.append(f'orch_replica_active_requests{{{labels}}} {snap.active_requests}')
        if not any_replica:
            lines.append('orch_replica_healthy{pool="prefill",pod="none"} 0')
        return "\n".join(lines) + "\n"

METRICS = Metrics()


# ---- live gateway: per tenant / model / role / SLA (prometheus_client) ----
#
# The orch_* families above stay as they were (class dashboards read them). These add what
# multi-tenant agent traffic needs: who (tenant, role, SLA class), which model, and the
# timings only the gateway can see: queue wait, TTFT, time to first *answer* token (after
# reasoning), ITL, local tool time between an agent's calls. Sessions and agents are not
# labels (unbounded); they are in the per-run event log.

TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 10, 12, 20, 30, 45, 60, 90, 120, 180, 300)
E2E_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180, 240, 300, 450, 600, 900)
ITL_BUCKETS = (0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.08, 0.1, 0.15, 0.2, 0.3, 0.5, 1, 2)
WAIT_BUCKETS = (0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 90, 120, 180, 240)


class LiveMetrics:
    def __init__(self) -> None:
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram as PHistogram

        r = self.registry = CollectorRegistry()
        req = ("tenant", "model", "role", "sla")
        self.requests = Counter("orch_chat_requests", "Chat calls received", req, registry=r)
        self.admission = Counter(
            "orch_admission", "Admission decisions", ("decision", "reason", "code", "tenant", "model", "sla"), registry=r
        )
        self.completed = Counter("orch_chat_completed", "Chat calls finished, by HTTP status", (*req, "status"), registry=r)
        self.queue_depth = Gauge("orch_queue_depth", "Calls waiting in the gateway queue", ("pool", "sla"), registry=r)
        self.queue_bound = Gauge("orch_queue_bound", "Queue bound per lane", ("pool",), registry=r)
        self.little_l = Gauge("orch_little_l", "Little's law L = arrival rate x mean wait, per lane", ("pool",), registry=r)
        self.queue_wait = PHistogram(
            "orch_queue_wait_seconds", "Time in the gateway queue", ("pool", "sla", "tenant"), buckets=WAIT_BUCKETS, registry=r
        )
        self.inflight = Gauge("orch_inflight", "Calls running on a replica", ("pool", "replica"), registry=r)
        self.slots = Gauge("orch_slots", "Slots per replica (vLLM max-num-seqs)", ("pool", "replica"), registry=r)
        self.route = Counter("orch_route", "Placement decisions", ("pool", "replica", "reason"), registry=r)
        self.spill = Counter("orch_spill", "Calls moved to another model's pool", ("from_pool", "to_pool", "role"), registry=r)
        self.ttft = PHistogram("orch_ttft_seconds", "Time to first token (reasoning or answer)", req, buckets=TTFT_BUCKETS, registry=r)
        self.ttfat = PHistogram("orch_ttfat_seconds", "Time to first answer token", req, buckets=TTFT_BUCKETS, registry=r)
        self.reasoning = PHistogram("orch_reasoning_seconds", "Time spent streaming reasoning", req, buckets=E2E_BUCKETS, registry=r)
        self.itl = PHistogram("orch_itl_seconds", "Gap between streamed chunks", ("model", "tenant"), buckets=ITL_BUCKETS, registry=r)
        self.e2e = PHistogram("orch_e2e_seconds", "Call time, received to last byte", (*req, "status"), buckets=E2E_BUCKETS, registry=r)
        self.tokens = Counter("orch_tokens", "Tokens by kind", ("kind", "tenant", "model", "role"), registry=r)
        self.tool_calls = Counter("orch_tool_calls", "Tool calls the model emitted", ("tool", "tenant", "model"), registry=r)
        self.tool_gap = PHistogram(
            "orch_tool_gap_seconds", "Local tool time between an agent's calls", ("tenant", "role"), buckets=E2E_BUCKETS, registry=r
        )
        self.sessions = Gauge("orch_sessions_active", "Sessions seen in the last 2 minutes", ("tenant",), registry=r)
        self.agents = Gauge("orch_agents_active", "Agents seen in the last 2 minutes", ("tenant", "role"), registry=r)
        self.kv = Gauge("orch_engine_kv_usage", "Engine KV usage as last scraped (0-1)", ("pool", "replica"), registry=r)
        self.signal_age = Gauge("orch_engine_signal_age_seconds", "Age of the last engine scrape", ("pool", "replica"), registry=r)
        self.mode = Gauge("orch_mode", "1 for the active gateway mode", ("mode",), registry=r)
        self.est_wait = Gauge("orch_est_wait_seconds", "Estimated queue wait for a new call", ("pool",), registry=r)
        self.prefill_tps = Gauge("orch_learned_prefill_tps", "Learned prefill rate (EWMA)", ("pool",), registry=r)
        self.learned_itl = Gauge("orch_learned_itl_seconds", "Learned inter-token latency (EWMA)", ("pool",), registry=r)
        self.harness = Counter("orch_harness_events", "Events reported by the agent harness", ("kind", "tenant"), registry=r)
        self.overflow = Counter(
            "orch_overflow", "Overflow gate decisions on capacity refusals: send, dry_run, or why it stayed local",
            ("decision", "reason", "pool"), registry=r,
        )
        self.rejections = Counter(
            "orch_rejections", "Calls that ended without a 200: which layer decided, the HTTP code and the reason (gateway/codes.py)",
            ("layer", "code", "reason", "tenant", "model"), registry=r,
        )

    def render(self) -> str:
        from prometheus_client import generate_latest

        return generate_latest(self.registry).decode("utf-8")
