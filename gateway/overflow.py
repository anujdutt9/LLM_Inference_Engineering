"""The overflow gate: when the local engines have no capacity for a call, send it to the same
model at an external provider (Superlinked) instead of refusing it.

The rule: only "no capacity" refusals may leave, 503 (and 529). 429 is the tenant's own budget and stays (sending it
elsewhere would spend money to hide a tenant problem); 400 is a client bug; 500 is our bug;
a slice OOM is local. Here the gate also checks a reason allow-list, a per-pool target model,
a call budget and an in-flight cap, and can run as a dry run that only counts what would
leave.

    overflow:
      enabled: true
      dry_run: false
      base_url: https://api.superlinked.com/v1    # or $OVERFLOW_BASE_URL
      targets: {qwen-9b: Qwen/Qwen3.5-9B}   # pool -> remote model; pools not listed stay local
      max_calls: 200                              # per gateway start; 0 = no limit
      max_inflight: 8

The API key comes from $OVERFLOW_API_KEY (a Kubernetes secret on the box), never the config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

LEAVE_CODES = (503, 529)
LEAVE_REASONS = ("kv_pressure", "no_signal", "queue_depth", "queue_full", "expired_in_queue", "new_session_shed", "upstream_error")


@dataclass
class OverflowGate:
    enabled: bool = False
    dry_run: bool = True
    base_url: str = ""
    api_key: str = ""
    targets: dict[str, str] = field(default_factory=dict)
    leave_codes: tuple[int, ...] = LEAVE_CODES
    leave_reasons: tuple[str, ...] = LEAVE_REASONS
    max_calls: int = 0
    max_inflight: int = 8
    timeout_s: float = 120.0
    sent: int = 0
    inflight: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None, previous: OverflowGate | None = None) -> OverflowGate:
        raw = raw or {}
        gate = cls(
            enabled=bool(raw.get("enabled", False)),
            dry_run=bool(raw.get("dry_run", True)),
            base_url=(os.environ.get("OVERFLOW_BASE_URL") or raw.get("base_url") or "").rstrip("/"),
            api_key=os.environ.get("OVERFLOW_API_KEY", ""),
            targets=dict(raw.get("targets") or {}),
            leave_codes=tuple(int(c) for c in raw.get("leave_codes", LEAVE_CODES)),
            leave_reasons=tuple(raw.get("leave_reasons", LEAVE_REASONS)),
            max_calls=int(raw.get("max_calls", 0) or 0),
            max_inflight=int(raw.get("max_inflight", 8) or 8),
            timeout_s=float(raw.get("timeout_s", 120)),
        )
        if previous is not None:  # a config change keeps what was already spent
            gate.sent, gate.inflight, gate.counts = previous.sent, previous.inflight, previous.counts
        return gate

    def decide(self, pool: str, status: int, reason: str) -> str:
        """What the gate does with a refusal: 'send', 'dry_run', or why it stays local."""
        if not self.enabled:
            return "off"
        if status not in self.leave_codes:
            return "stays_local_code"  # 429 / 400 / 500 never leave
        if reason not in self.leave_reasons:
            return "stays_local_reason"
        if pool not in self.targets:
            return "no_target"
        if self.max_calls and self.sent >= self.max_calls:
            return "budget_spent"
        if self.inflight >= self.max_inflight:
            return "inflight_cap"
        if self.dry_run:
            return "dry_run"
        if not self.base_url or not self.api_key:
            return "no_credentials"
        return "send"

    def count(self, decision: str) -> None:
        self.counts[decision] = self.counts.get(decision, 0) + 1

    def state(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "dry_run": self.dry_run, "targets": self.targets, "sent": self.sent,
                "inflight": self.inflight, "max_calls": self.max_calls, "max_inflight": self.max_inflight,
                "budget_left": (self.max_calls - self.sent) if self.max_calls else None,
                "provider": self.base_url.split("//")[-1].split("/")[0] if self.base_url else "", "counts": dict(self.counts),
                "credentials": bool(self.api_key)}
