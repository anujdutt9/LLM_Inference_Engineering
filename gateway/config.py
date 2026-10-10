from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# What each gateway mode turns on: each mode adds one box.
MODES: dict[str, dict[str, bool]] = {
    "baseline": {"queue": False, "admission": False, "spill": False, "prefix": False, "priority": False},
    "route": {"queue": False, "admission": False, "spill": True, "prefix": True, "priority": False},
    "queue": {"queue": True, "admission": False, "spill": True, "prefix": True, "priority": True},
    "full": {"queue": True, "admission": True, "spill": True, "prefix": True, "priority": True},
}

DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")


@dataclass
class PoolConfig:
    name: str
    model: str
    replicas: list[str]
    slots_per_replica: int = 8
    max_model_len: int = 65536
    prefill_tps: float = 8000.0
    itl_s: float = 0.03
    reasoning_tokens: float = 0.0  # expected reasoning per call when thinking is on (learned live)
    # replica URL -> its name in logs, metrics and the UI (default <pool>-<index>); keyed by URL so a
    # replica keeps its name when a variant changes which replicas the pool uses
    replica_names: dict[str, str] = field(default_factory=dict)


@dataclass
class SlaClass:
    """What a class of call is promised. TTFT: first token of any kind; TTFAT: first token of
    the answer, after any reasoning (what a user, or the agent waiting on it, actually waits
    for); deadline: the whole call. Admission can check each (admission.checks)."""

    name: str
    ttft_s: float
    deadline_s: float
    queue_budget_s: float
    priority: int
    ttfat_s: float | None = None


@dataclass
class TenantConfig:
    tokens_per_min: float
    burst: float


@dataclass
class Config:
    mode: str
    results_dir: Path
    record_bodies: bool
    pools: dict[str, PoolConfig]
    sla_classes: dict[str, SlaClass]
    default_sla: str
    max_tokens_by_role: dict[str, int]
    tenants: dict[str, TenantConfig]
    admission: dict[str, Any]
    queue: dict[str, Any]
    routing: dict[str, Any]
    guardrails: dict[str, int] = field(default_factory=dict)
    sla_by_role: dict[str, str] = field(default_factory=dict)
    variant: str = "default"  # the experiment variant these settings are (POST /admin/config)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)  # the settings as loaded
    scrape_interval_s: float = 1.0
    upstream: dict[str, float] = field(default_factory=dict)

    @property
    def features(self) -> dict[str, bool]:
        return MODES[self.mode]

    def sla(self, name: str | None) -> SlaClass:
        return self.sla_classes.get(name or "", self.sla_classes[self.default_sla])

    def tenant(self, name: str) -> TenantConfig:
        return self.tenants.get(name, self.tenants["default"])

    def max_tokens_for(self, role: str) -> int:
        return int(self.max_tokens_by_role.get(role, self.max_tokens_by_role["default"]))

    def sla_for(self, role: str, pool: str) -> str:
        """The SLA class of a call that names none: by role and model ("subagent@qwen-9b"),
        then by role, then the default."""
        return self.sla_by_role.get(f"{role}@{pool}") or self.sla_by_role.get(role) or self.default_sla

    def models(self) -> list[str]:
        return [p.model for p in self.pools.values()]

    def pool_for_model(self, model: str) -> PoolConfig | None:
        """A pool by its name or by the served model name it answers to."""
        if model in self.pools:
            return self.pools[model]
        return next((p for p in self.pools.values() if p.model == model), None)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("ORCH_CONFIG") or DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text())
    return from_raw(raw, mode=os.environ.get("ORCH_MODE"))


def merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """`base` with `patch` laid over it, nested dicts merged (lists and values replaced)."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def from_raw(raw: dict[str, Any], *, mode: str | None = None, variant: str = "default") -> Config:
    mode = mode or raw.get("mode", "full")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose one of {', '.join(MODES)}")
    pools = {
        name: PoolConfig(name=name, **{**spec, "replicas": _replicas(name, spec["replicas"])})
        for name, spec in raw["pools"].items()
    }
    return Config(
        mode=mode,
        results_dir=Path(os.environ.get("ORCH_RESULTS_DIR", raw.get("results_dir", "results"))),
        record_bodies=bool(raw.get("record_bodies", True)),
        pools=pools,
        sla_classes={n: SlaClass(name=n, **s) for n, s in raw["sla_classes"].items()},
        default_sla=raw["default_sla"],
        max_tokens_by_role={k: int(v) for k, v in raw["max_tokens_by_role"].items()},
        tenants={n: TenantConfig(**t) for n, t in raw["tenants"].items()},
        admission=dict(raw.get("admission", {})),
        queue=dict(raw.get("queue", {})),
        routing=dict(raw.get("routing", {})),
        guardrails={k: int(v) for k, v in (raw.get("guardrails") or {}).items()},
        sla_by_role=dict(raw.get("sla_by_role") or {}),
        scrape_interval_s=float(raw.get("scrape_interval_s", 1.0)),
        upstream=dict(raw.get("upstream", {})),
        variant=variant,
        raw=copy.deepcopy(raw),
    )


def _replicas(pool: str, default: list[str]) -> list[str]:
    """POOL_<NAME>_URLS (comma-separated) overrides a pool's replicas, e.g. for the
    laptop: POOL_QWEN_27B_URLS=http://127.0.0.1:8000."""
    env = os.environ.get(f"POOL_{pool.upper().replace('-', '_')}_URLS", "").strip()
    return [u.strip().rstrip("/") for u in env.split(",") if u.strip()] if env else [u.rstrip("/") for u in default]
