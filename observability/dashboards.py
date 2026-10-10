from __future__ import annotations

import json
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}

def _target(expr: str, legend: str = "", ref: str = "A") -> dict:
    target: dict = {"datasource": PROM, "expr": expr, "refId": ref}
    if legend:
        target["legendFormat"] = legend
    return target

def _dash(uid: str, title: str, panels: list[dict], tags: list[str]) -> dict:
    return {
        "uid": uid,
        "title": title,
        "tags": tags,
        "schemaVersion": 39,
        "version": 2,
        "timezone": "browser",
        "refresh": "5s",
        "time": {"from": "now-1h", "to": "now"},
        "templating": {"list": []},
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "links": [],
        "liveNow": True,
        "panels": panels,
        "style": "dark",
    }

# ---------------------------------------------------------------------------------------
# Demo boards (one H100 behind the live gateway: Exp-1/2 ran qwen-27b + qwen-9b, Exp-4 two qwen-9b replicas,
# qwen-9b-a and qwen-9b-b). uid/title "Demo / ...",
# written to k8s-config/observability/dashboards/demo-*.json.
#
# Conventions: every rate() uses $__rate_interval (set the datasource's scrape interval to 5s
# so it resolves to 20s); vLLM series are split per engine pod by `app` (vllm-9b-a / vllm-9b-b; Exp-1/2's
# vllm-27b / vllm-9b), except the Exp-1/2 boards (by model_name, one engine per model then); gateway series
# carry the pool name in `model` (same values as model_name) and the replica id (qwen-9b-a / qwen-9b-b) in `replica`. Histogram bucket bounds may be exported as
# "2" or "2.0" (prometheus_client / Prometheus 3 normalisation), so le matchers use "2|2.0".
# ---------------------------------------------------------------------------------------

GRAFANA_DS = {"type": "grafana", "uid": "-- Grafana --"}
RI = "$__rate_interval"
M27, M9 = "qwen-27b", "qwen-9b"
ENG = "app"  # the vLLM pod's Deployment: one series per replica (two replicas share one model_name)


def _slo_classes() -> tuple[tuple[str, float], ...]:
    """SLA class -> TTFT target seconds, read from gateway/config.yaml so the panels use
    the same class names and targets as the gateway."""
    import yaml

    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "gateway" / "config.yaml").read_text())
    return tuple((name, float(c["ttft_s"])) for name, c in raw["sla_classes"].items())


SLO_CLASSES = _slo_classes()
_SLO_TEXT = ", ".join(f"{n} {t:g} s" for n, t in SLO_CLASSES)

DEMO_METRIC_NAMES = {
    "demo-pipeline": (
        "orch_chat_requests_total", "orch_admission_total", "orch_rejections_total", "orch_queue_depth", "orch_queue_bound",
        "orch_queue_wait_seconds_bucket", "orch_little_l", "orch_inflight", "orch_slots", "orch_route_total",
        "orch_spill_total", "orch_est_wait_seconds", "orch_mode",
    ),
    "demo-engines": (
        "vllm:num_requests_running", "vllm:num_requests_waiting", "vllm:kv_cache_usage_perc",
        "vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total", "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total", "vllm:time_to_first_token_seconds_bucket",
        "vllm:inter_token_latency_seconds_bucket", "vllm:e2e_request_latency_seconds_bucket",
        "vllm:request_queue_time_seconds_bucket", "vllm:request_prefill_time_seconds",
        "vllm:request_decode_time_seconds", "vllm:num_preemptions_total", "orch_tokens_total",
    ),
    "demo-tenants": (
        "orch_rejections_total",
        "orch_ttft_seconds_bucket", "orch_ttfat_seconds_bucket", "orch_reasoning_seconds_bucket",
        "orch_itl_seconds_bucket", "orch_e2e_seconds_bucket", "orch_chat_completed_total",
        "orch_tokens_total", "orch_tool_calls_total", "orch_tool_gap_seconds_bucket", "orch_sessions_active",
        "orch_agents_active",
    ),
    "demo-gpu": (
        "DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_PROF_SM_ACTIVE", "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "DCGM_FI_DEV_FB_USED",
        "DCGM_FI_DEV_FB_FREE", "DCGM_FI_DEV_POWER_USAGE", "vllm:kv_cache_usage_perc",
    ),
    "demo-exp1": (
        "vllm:prompt_tokens_total", "vllm:generation_tokens_total", "orch_ttft_seconds_bucket",
        "vllm:inter_token_latency_seconds_bucket", "orch_reasoning_seconds_sum", "orch_ttfat_seconds_sum",
        "orch_tool_calls_total", "vllm:prefix_cache_hits_total",
    ),
    "demo-exp2": (
        "orch_ttft_seconds_bucket", "orch_e2e_seconds_bucket", "orch_queue_wait_seconds_bucket",
        "orch_admission_total", "orch_spill_total", "vllm:inter_token_latency_seconds_bucket",
        "vllm:prompt_tokens_total", "vllm:time_to_first_token_seconds_bucket",
    ),
    "demo-exp3": (
        "orch_rejections_total",
        "orch_chat_requests_total", "orch_chat_completed_total", "orch_ttft_seconds_bucket", "orch_e2e_seconds_bucket",
        "orch_admission_total", "vllm:kv_cache_usage_perc", "vllm:prefix_cache_hits_total", "orch_queue_depth",
        "orch_little_l",
    ),
    "demo-exp4": (
        "orch_chat_completed_total", "orch_route_total", "orch_ttft_seconds_bucket", "vllm:num_requests_running",
        "vllm:num_requests_waiting", "vllm:prefix_cache_hits_total", "vllm:kv_cache_usage_perc",
        "vllm:generation_tokens_total", "vllm:inter_token_latency_seconds_bucket", "DCGM_FI_DEV_GPU_UTIL",
    ),
}


def _le(bound: float) -> str:
    """Matcher for one classic-histogram bucket, whichever way the bound was rendered."""
    whole = f"{bound:g}"
    return f'le=~"{whole}|{float(bound)!r}"'


def _sel(*parts: str) -> str:
    parts = tuple(p for p in parts if p)
    return "{" + ",".join(parts) + "}" if parts else ""


def _rate(metric: str, sel: str = "", by: str = "", window: str = RI) -> str:
    agg = f"sum by ({by})" if by else "sum"
    return f"{agg} (rate({metric}{_sel(sel)}[{window}]))"


def _inc(metric: str, sel: str = "", by: str = "") -> str:
    agg = f"sum by ({by})" if by else "sum"
    return f"{agg} (increase({metric}{_sel(sel)}[$__range]))"


def _q(q: float, metric: str, by: str = "", sel: str = "") -> str:
    by = f"le, {by}" if by else "le"
    return f"histogram_quantile({q}, sum by ({by}) (rate({metric}_bucket{_sel(sel)}[{RI}])))"


def _mean(metric: str, by: str = "", sel: str = "") -> str:
    return f"{_rate(metric + '_sum', sel, by)} / ({_rate(metric + '_count', sel, by)} > 0)"


def _ratio(num: str, den: str) -> str:
    return f"({num}) / (({den}) > 0)"


# Bucket bounds of orch_ttft_seconds (gateway/metrics.py TTFT_BUCKETS). A class target that is
# not a bound (10 s today) is linearly interpolated between its neighbours, the same estimate
# histogram_quantile makes; if the gateway ever adds the exact bound, that series wins.
GATEWAY_TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 45, 60, 90, 120, 180, 300)


def _under(target: float, agg: str, fn: str, sel: str, window: str) -> str:
    """Calls/s (or count) with TTFT <= target, from orch_ttft_seconds buckets."""

    def bucket(bound: float) -> str:
        inner = ",".join(x for x in (sel, _le(bound)) if x)
        return f"{agg} ({fn}(orch_ttft_seconds_bucket{{{inner}}}[{window}]))"

    exact = bucket(target)
    if target in GATEWAY_TTFT_BUCKETS:
        return exact
    lo = max(b for b in GATEWAY_TTFT_BUCKETS if b < target)
    hi = min(b for b in GATEWAY_TTFT_BUCKETS if b > target)
    frac = (target - lo) / (hi - lo)
    return f"({exact} or ({bucket(lo)} + {frac:g} * ({bucket(hi)} - {bucket(lo)})))"


def _slo_share(sla: str, target: float, by: str = "tenant", extra: str = "", window: str = RI) -> str:
    """Share of calls in an SLA class whose TTFT was under the class target."""
    sel = f'sla="{sla}"' + (f",{extra}" if extra else "")
    fn = "increase" if window == "$__range" else "rate"
    agg = f"sum by ({by})"
    count = f"{agg} ({fn}(orch_ttft_seconds_count{{{sel}}}[{window}]))"
    return _ratio(_under(target, agg, fn, sel, window), count)


def _slo_met_rate() -> str:
    """Calls/s whose TTFT met their class target (goodput by SLO), summed over classes."""
    terms = []
    for sla, t in SLO_CLASSES:
        sel = f'sla="{sla}"'
        terms.append(f'({_under(t, "sum", "rate", sel, RI)} or vector(0))')
    return " + ".join(terms)


def _annotations() -> dict:
    return {
        "list": [
            {
                "builtIn": 1,
                "datasource": GRAFANA_DS,
                "enable": True,
                "hide": True,
                "iconColor": "rgba(0, 211, 255, 1)",
                "name": "Annotations & Alerts",
                "type": "dashboard",
            },
            {
                # Experiment scripts POST /api/annotations with tags ["run", "<exp>"].
                "datasource": GRAFANA_DS,
                "enable": True,
                "hide": False,
                "iconColor": "orange",
                "name": "Runs",
                "target": {"type": "tags", "tags": ["run"], "matchAny": False, "limit": 200},
            },
            {
                # Works in the replay stack too: mode switches are in the Prometheus snapshot.
                "datasource": PROM,
                "enable": True,
                "hide": False,
                "iconColor": "purple",
                "name": "Gateway mode change",
                "expr": "(orch_mode == 1) unless (orch_mode offset 15s == 1)",
                "step": "15s",
                "titleFormat": "mode",
                "textFormat": "gateway mode -> {{mode}}",
                "tagKeys": "mode",
                "useValueForTime": False,
            },
        ]
    }


def _var(name: str, label: str, metric: str, metric_label: str) -> dict:
    query = f"label_values({metric}, {metric_label})"
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": PROM,
        "definition": query,
        "query": {"query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery", "qryType": 1},
        "refresh": 2,
        "includeAll": True,
        "multi": True,
        "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
        "options": [],
        "sort": 1,
        "hide": 0,
    }


TENANT_VAR = ("tenant", "Tenant", "orch_chat_requests_total", "tenant")
POOL_VAR = ("model", "Model", "orch_queue_bound", "pool")
ENGINE_VAR = ("model", "Model", "vllm:num_requests_running", "model_name")


class _Board:
    """Flows panels left to right in a 24-column grid, wrapping to a new row when full."""

    def __init__(self) -> None:
        self.panels: list[dict] = []
        self.x = self.y = self.row_h = 0

    def _place(self, panel: dict, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel["id"] = len(self.panels) + 1
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.x += w
        self.row_h = max(self.row_h, h)
        self.panels.append(panel)
        return panel

    def row(self, title: str) -> None:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        self._place({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1)
        self.x, self.y, self.row_h = 0, self.y + 1, 0

    def text(self, content: str, *, w: int = 24, h: int = 5, title: str = "") -> None:
        self._place(
            {"type": "text", "title": title, "options": {"mode": "markdown", "content": content.strip() + "\n"}},
            w,
            h,
        )

    def _targets(self, targets: list[tuple[str, str]], instant: bool = False) -> list[dict]:
        out = []
        for i, (expr, legend) in enumerate(targets):
            t = _target(expr, legend, chr(ord("A") + i))
            if instant:
                t["instant"], t["range"] = True, False
            out.append(t)
        return out

    def ts(
        self,
        title: str,
        targets: list[tuple[str, str]],
        *,
        w: int = 12,
        h: int = 8,
        unit: str = "",
        desc: str = "",
        stack: bool = False,
        right: str = "",
        right_unit: str = "",
        dashed: str = "",
        min0: bool = True,
        max_: float | None = None,
        bars: bool = False,
    ) -> None:
        custom: dict = {"drawStyle": "bars" if bars else "line", "fillOpacity": 60 if bars else 10, "lineWidth": 1,
                        "showPoints": "never", "spanNulls": False}
        if stack:
            custom["stacking"] = {"mode": "normal", "group": "A"}
        defaults: dict = {"custom": custom}
        if unit:
            defaults["unit"] = unit
        if min0:
            defaults["min"] = 0
        if max_ is not None:
            defaults["max"] = max_
        overrides = []
        if right:
            props = [{"id": "custom.axisPlacement", "value": "right"}]
            if right_unit:
                props.append({"id": "unit", "value": right_unit})
            overrides.append({"matcher": {"id": "byRegexp", "options": right}, "properties": props})
        if dashed:
            overrides.append(
                {
                    "matcher": {"id": "byRegexp", "options": dashed},
                    "properties": [
                        {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                        {"id": "custom.fillOpacity", "value": 0},
                        {"id": "custom.lineWidth", "value": 2},
                    ],
                }
            )
        self._place(
            {
                "type": "timeseries",
                "title": title,
                "description": desc,
                "datasource": PROM,
                "targets": self._targets(targets),
                "options": {
                    "legend": {"displayMode": "table", "placement": "bottom", "showLegend": True, "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"},
                },
                "fieldConfig": {"defaults": defaults, "overrides": overrides},
            },
            w,
            h,
        )

    def stat(
        self,
        title: str,
        targets: list[tuple[str, str]],
        *,
        w: int = 4,
        h: int = 4,
        unit: str = "",
        desc: str = "",
        instant: bool = False,
        text_mode: str = "auto",
        decimals: int | None = None,
        thresholds: list[tuple[float | None, str]] | None = None,
    ) -> None:
        defaults: dict = {}
        if unit:
            defaults["unit"] = unit
        if decimals is not None:
            defaults["decimals"] = decimals
        steps = thresholds or [(None, "blue")]
        defaults["thresholds"] = {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}
        defaults["color"] = {"mode": "thresholds"}
        self._place(
            {
                "type": "stat",
                "title": title,
                "description": desc,
                "datasource": PROM,
                "targets": self._targets(targets, instant),
                "options": {
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value",
                    "graphMode": "none" if instant else "area",
                    "justifyMode": "auto",
                    "textMode": text_mode,
                    "orientation": "auto",
                },
                "fieldConfig": {"defaults": defaults, "overrides": []},
            },
            w,
            h,
        )

    def bars(self, title: str, targets: list[tuple[str, str]], *, w: int = 12, h: int = 8, unit: str = "", desc: str = "") -> None:
        """Totals over the selected time range (instant queries) as horizontal bars."""
        defaults: dict = {"color": {"mode": "palette-classic"}}
        if unit:
            defaults["unit"] = unit
        self._place(
            {
                "type": "bargauge",
                "title": title,
                "description": desc,
                "datasource": PROM,
                "targets": self._targets(targets, instant=True),
                "options": {
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "orientation": "horizontal",
                    "displayMode": "basic",
                    "showUnfilled": True,
                    "valueMode": "color",
                    "namePlacement": "left",
                },
                "fieldConfig": {"defaults": defaults, "overrides": []},
            },
            w,
            h,
        )


def _demo(uid: str, title: str, board: _Board, tags: list[str], variables: tuple = (), desc: str = "") -> dict:
    dash = _dash(uid, title, board.panels, ["demo", *tags])
    dash.update(
        {
            "description": desc,
            "time": {"from": "now-30m", "to": "now"},
            "refresh": "5s",
            "liveNow": False,
            "annotations": _annotations(),
            "templating": {"list": [_var(*v) for v in variables]},
            "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
            "links": [{"type": "dashboards", "tags": ["demo"], "asDropdown": True, "title": "Demo boards", "includeVars": False, "keepTime": True}],
        }
    )
    return dash


# ---- shared expressions -------------------------------------------------------------------

def _prefill_decode_ratio(sel: str = 'model_name=~"$model"', window: str = RI, excl_cache: bool = False, by: str = "model_name") -> str:
    fn = "increase" if window == "$__range" else "rate"
    prompt = f"sum by ({by}) ({fn}(vllm:prompt_tokens_total{{{sel}}}[{window}]))"
    if excl_cache:
        prompt = f"({prompt} - sum by ({by}) ({fn}(vllm:prefix_cache_hits_total{{{sel}}}[{window}])))"
    gen = f"sum by ({by}) ({fn}(vllm:generation_tokens_total{{{sel}}}[{window}]))"
    return _ratio(prompt, gen)


def _prefix_hit(sel: str = 'model_name=~"$model"', window: str = RI, by: str = "model_name") -> str:
    fn = "increase" if window == "$__range" else "rate"
    return _ratio(
        f"sum by ({by}) ({fn}(vllm:prefix_cache_hits_total{{{sel}}}[{window}]))",
        f"sum by ({by}) ({fn}(vllm:prefix_cache_queries_total{{{sel}}}[{window}]))",
    )


def _cached_share(by: str = "model", sel: str = "") -> str:
    extra = f",{sel}" if sel else ""
    return _ratio(
        _rate("orch_tokens_total", f'kind="prompt_cached"{extra}', by),
        _rate("orch_tokens_total", f'kind="prompt"{extra}', by),
    )


def _p50_p99(metric: str, by: str, legend: str, sel: str = "") -> list[tuple[str, str]]:
    return [(_q(0.5, metric, by, sel), f"p50 {legend}"), (_q(0.99, metric, by, sel), f"p99 {legend}")]


def _slo_targets(by: str = "tenant", extra: str = "", window: str = RI) -> list[tuple[str, str]]:
    leg = " ".join("{{%s}}" % b.strip() for b in by.split(","))
    return [(_slo_share(sla, t, by, extra, window), f"{leg} {sla} (<{t}s)") for sla, t in SLO_CLASSES]


def _mode_stat(b: _Board, w: int = 4) -> None:
    b.stat("Gateway mode", [("orch_mode == 1", "{{mode}}")], w=w, text_mode="name",
           desc="baseline / route / queue / full (POST /admin/mode).")


# ---- boards -------------------------------------------------------------------------------

def demo_pipeline() -> dict:
    b = _Board()
    t, m = 'tenant=~"$tenant"', 'model=~"$model"'
    p = 'pool=~"$model"'
    _mode_stat(b)
    b.stat("Calls / s", [(_rate("orch_chat_requests_total", f"{t},{m}"), "calls/s")], unit="reqps", decimals=2)
    b.stat("Admitted / s", [(_rate("orch_admission_total", f'decision="admit",{t},{m}'), "admit/s")], unit="reqps", decimals=2)
    b.stat("Shed / s", [(_rate("orch_admission_total", f'decision="shed",{t},{m}'), "shed/s")], unit="reqps", decimals=2,
           thresholds=[(None, "green"), (0.01, "orange")])
    b.stat("Completed 200 / s", [(_rate("orch_chat_completed_total", f'status="200",{t},{m}'), "200/s")], unit="reqps", decimals=2)
    b.stat("Queued now", [(f"sum(orch_queue_depth{{{p}}})", "queued")], thresholds=[(None, "green"), (1, "orange")])

    b.row("Admit")
    b.ts("Calls / s by tenant & model", [(_rate("orch_chat_requests_total", f"{t},{m}", "tenant, model"), "{{tenant}} → {{model}}")],
         unit="reqps", stack=True)
    b.ts("Admit vs shed by reason + code", [(_rate("orch_admission_total", f"{t},{m}", "decision, reason, code"), "{{decision}} {{reason}} {{code}}")],
         unit="reqps", stack=True, desc="admit/ok/200 vs shed reasons (429 tenant_tokens, 503 kv_pressure, queue_full, deadline_unmeetable, ...).")
    b.ts("Non-200 by layer · code · reason", [(_rate("orch_rejections_total", f"{t},{m}", "layer, code, reason"), "{{layer}} {{code}} {{reason}}")],
         unit="reqps", stack=True, desc="Every call that didn't get a 200, by the layer that decided it (gateway/codes.py): edge 400 (guardrails), admission 429 (tenant budget, SLA estimate) / 503 (KV pressure, stale metrics, busy), queue 503 (full, expired), engine 5xx, client 499 (hung up).")

    b.row("Queue")
    b.ts("Queue depth by pool & SLA vs bound",
         [(f"sum by (pool, sla) (orch_queue_depth{{{p}}})", "{{pool}} {{sla}}"), (f"orch_queue_bound{{{p}}}", "bound {{pool}}")],
         stack=False, dashed="bound.*", w=8)
    b.ts("Queue wait p50 / p95 / p99 by pool",
         [(_q(0.5, "orch_queue_wait_seconds", "pool", f"{p},{t}"), "p50 {{pool}}"),
          (_q(0.95, "orch_queue_wait_seconds", "pool", f"{p},{t}"), "p95 {{pool}}"),
          (_q(0.99, "orch_queue_wait_seconds", "pool", f"{p},{t}"), "p99 {{pool}}")], unit="s", w=8)
    b.ts("Queue wait p95 by tenant & SLA", [(_q(0.95, "orch_queue_wait_seconds", "pool, tenant, sla", f"{p},{t}"), "{{pool}} {{tenant}} {{sla}}")],
         unit="s", w=8)
    b.ts("Little's L vs measured depth",
         [("orch_little_l{%s}" % p, "L gateway {{pool}}"),
          (_rate("orch_queue_wait_seconds_sum", p, "pool"), "L = λ·W (from wait histogram) {{pool}}"),
          (f"sum by (pool) (orch_queue_depth{{{p}}})", "depth {{pool}}")], dashed="L .*", w=8,
         desc="L = arrival rate × mean wait. rate(wait_sum) is λ·W directly; it should track the measured depth.")
    b.ts("Estimated wait for a new call", [(f"orch_est_wait_seconds{{{p}}}", "{{pool}}")], unit="s", w=8)
    b.ts("Learned prefill tok/s and ITL", [(f"orch_learned_prefill_tps{{{p}}}", "prefill tok/s {{pool}}"),
                                           (f"orch_learned_itl_seconds{{{p}}}", "ITL {{pool}}")],
         right="ITL.*", right_unit="s", w=8)

    b.row("Route")
    b.ts("Inflight vs slots per replica",
         [(f"orch_inflight{{{p}}}", "inflight {{pool}} {{replica}}"), (f"orch_slots{{{p}}}", "slots {{pool}} {{replica}}")],
         dashed="slots.*", w=8)
    b.ts("Route decisions by replica & reason", [(_rate("orch_route_total", p, "pool, replica, reason"), "{{replica}} {{reason}}")], unit="reqps", stack=True, w=8,
         desc="Which replica each call went to and why: prefix = it held the prompt's prefix (KV-aware), p2c = less loaded of two, round_robin, single.")
    b.ts("Spills to another model", [(_rate("orch_spill_total", "", "from_pool, to_pool, role"), "{{from_pool}}→{{to_pool}} {{role}}")],
         unit="reqps", w=8)
    b.ts("Engine KV as the gateway sees it", [(f"orch_engine_kv_usage{{{p}}}", "{{pool}} {{replica}}")], unit="percentunit", max_=1, w=12)
    b.ts("Engine signal age (stale > 5 s → 503 no_signal)", [(f"orch_engine_signal_age_seconds{{{p}}}", "{{pool}} {{replica}}")], unit="s", w=12)
    return _demo("demo-pipeline", "Demo / Pipeline (gateway)", b, ["gateway"], (TENANT_VAR, POOL_VAR),
                 "admit → queue → route at the gateway.")


def demo_engines() -> dict:
    b = _Board()
    v = 'model_name=~"$model"'
    b.stat("Prefill:decode tokens (range)", [(_prefill_decode_ratio(window="$__range", by=ENG), "{{app}}")], w=8, instant=True, decimals=1,
           desc="prompt tokens / generation tokens over the selected time range. SQL / coding teams are decode-heavy (low), the 10-K team prefill-heavy (high).")
    b.stat("Prefix-cache hit % (range)", [(_prefix_hit(window="$__range", by=ENG), "{{app}}")], w=8, instant=True, unit="percentunit")
    b.stat("Preemptions (range)", [(_inc("vllm:num_preemptions_total", v, ENG), "{{app}}")], w=8, instant=True, decimals=0,
           thresholds=[(None, "green"), (1, "red")])

    b.row("Load")
    b.ts("Running per engine", [("sum by (app) (vllm:num_requests_running{%s})" % v, "{{app}}")], w=8)
    b.ts("Waiting inside vLLM (≈ 0 when the gateway queue is on)", [("sum by (app) (vllm:num_requests_waiting{%s})" % v, "{{app}}")], w=8)
    b.ts("KV cache usage", [("sum by (app) (vllm:kv_cache_usage_perc{%s})" % v, "{{app}}")], unit="percentunit", max_=1, w=8)

    b.row("Tokens")
    b.ts("Prompt vs generation tokens / s",
         [(_rate("vllm:prompt_tokens_total", v, ENG), "prompt {{app}}"),
          (_rate("vllm:generation_tokens_total", v, ENG), "generation {{app}}")], w=8)
    b.ts("Prefill:decode token ratio (headline)",
         [(_prefill_decode_ratio(by=ENG), "prompt:gen {{app}}"), (_prefill_decode_ratio(excl_cache=True, by=ENG), "computed prefill:gen {{app}}")],
         dashed="computed.*", w=8, desc="computed prefill excludes prefix-cache hits (tokens vLLM did not recompute).")
    b.ts("Prefix-cache hit rate & cached prompt share",
         [(_prefix_hit(by=ENG), "vLLM hit rate {{app}}"), (_cached_share("model", 'model=~"$model"'), "gateway cached share {{model}}")],
         unit="percentunit", max_=1, dashed="gateway.*", w=8,
         desc="vLLM: rate(prefix_cache_hits)/rate(prefix_cache_queries). Gateway: usage.cached_tokens / prompt_tokens.")

    b.row("Latency (vLLM histograms)")
    b.ts("TTFT p50 / p99", _p50_p99("vllm:time_to_first_token_seconds", ENG, "{{app}}", v), unit="s", w=12)
    b.ts("ITL p50 / p99", _p50_p99("vllm:inter_token_latency_seconds", ENG, "{{app}}", v), unit="s", w=12)
    b.ts("e2e p50 / p99", _p50_p99("vllm:e2e_request_latency_seconds", ENG, "{{app}}", v), unit="s", w=12)
    b.ts("vLLM-internal queue time p50 / p99", _p50_p99("vllm:request_queue_time_seconds", ENG, "{{app}}", v), unit="s", w=12)
    b.ts("Prefill vs decode time per request (mean)",
         [(_mean("vllm:request_prefill_time_seconds", ENG, v), "prefill {{app}}"),
          (_mean("vllm:request_decode_time_seconds", ENG, v), "decode {{app}}")], unit="s", w=12)
    b.ts("Preemptions / s", [(_rate("vllm:num_preemptions_total", v, ENG), "{{app}}")], w=12)
    return _demo("demo-engines", "Demo / Engines (vLLM)", b, ["vllm"], (ENGINE_VAR,), "vLLM v0.30 engine metrics per engine pod (app).")


def demo_tenants() -> dict:
    b = _Board()
    t = 'tenant=~"$tenant"'
    b.stat("Active sessions", [("sum by (tenant) (orch_sessions_active{%s})" % t, "{{tenant}}")], w=6)
    b.stat("Active agents", [("sum by (tenant) (orch_agents_active{%s})" % t, "{{tenant}}")], w=6)
    b.stat("SLO attainment (range)", _slo_targets("tenant", t, "$__range"), w=12, instant=True, unit="percentunit",
           thresholds=[(None, "red"), (0.9, "orange"), (0.99, "green")],
           desc=f"Share of calls with TTFT under the class target: {_SLO_TEXT}.")

    b.row("Latency per tenant & role")
    b.ts("TTFT p50 / p99", _p50_p99("orch_ttft_seconds", "tenant, role", "{{tenant}} {{role}}", t), unit="s", w=8)
    b.ts("TTFAT p50 / p99 (first answer token)", _p50_p99("orch_ttfat_seconds", "tenant, role", "{{tenant}} {{role}}", t), unit="s", w=8)
    b.ts("Reasoning time p50 / p99", _p50_p99("orch_reasoning_seconds", "tenant, role", "{{tenant}} {{role}}", t), unit="s", w=8)
    b.ts("ITL p50 / p99", _p50_p99("orch_itl_seconds", "tenant, model", "{{tenant}} {{model}}", t), unit="s", w=12)
    b.ts("e2e p50 / p99 (200s)", _p50_p99("orch_e2e_seconds", "tenant, role", "{{tenant}} {{role}}", f'{t},status="200"'), unit="s", w=12)

    b.row("SLOs and goodput")
    b.ts("SLO attainment (TTFT under class target)", _slo_targets("tenant", t), unit="percentunit", max_=1, w=12,
         desc=f"rate(bucket le=target) / rate(count) per SLA class: {_SLO_TEXT}.")
    b.ts("Goodput (completed 200 / s) vs errors",
         [(_rate("orch_chat_completed_total", f'{t},status="200"', "tenant"), "200 {{tenant}}"),
          (_rate("orch_chat_completed_total", f'{t},status!="200"', "tenant, status"), "{{status}} {{tenant}}")], unit="reqps", w=12)
    b.ts("Non-200 per tenant by layer · code · reason", [(_rate("orch_rejections_total", t, "tenant, layer, code, reason"),
                                                          "{{tenant}} {{layer}} {{code}} {{reason}}")],
         unit="reqps", stack=True, w=12, desc="Every call that didn't get a 200, by the layer that decided it (gateway/codes.py): edge 400 (guardrails), admission 429 (tenant budget, SLA estimate) / 503 (KV pressure, stale metrics, busy), queue 503 (full, expired), engine 5xx, client 499 (hung up).")
    b.ts("Tokens / s per tenant by kind", [(_rate("orch_tokens_total", t, "tenant, kind"), "{{tenant}} {{kind}}")], w=12,
         desc="reasoning_est / answer_est count streamed chunks (≈ tokens).")

    b.row("Agents and tools")
    b.bars("Tokens per tenant by kind (range)", [(_inc("orch_tokens_total", t, "tenant, kind"), "{{tenant}} {{kind}}")], w=8)
    b.ts("Tool calls / s by tool", [(_rate("orch_tool_calls_total", t, "tenant, tool"), "{{tenant}} {{tool}}")], w=8)
    b.bars("Tool calls by tool (range)", [(_inc("orch_tool_calls_total", t, "tenant, tool"), "{{tenant}} {{tool}}")], w=8)
    b.ts("Local tool time between calls p50 / p95",
         [(_q(0.5, "orch_tool_gap_seconds", "tenant, role", t), "p50 {{tenant}} {{role}}"),
          (_q(0.95, "orch_tool_gap_seconds", "tenant, role", t), "p95 {{tenant}} {{role}}")], unit="s", w=12)
    b.ts("Active sessions and agents",
         [("sum by (tenant) (orch_sessions_active{%s})" % t, "sessions {{tenant}}"),
          ("sum by (tenant, role) (orch_agents_active{%s})" % t, "agents {{tenant}} {{role}}")], w=12)
    return _demo("demo-tenants", "Demo / Tenants & SLAs", b, ["tenants", "slo"], (TENANT_VAR,), "Per tenant, role and SLA class.")


def demo_gpu() -> dict:
    b = _Board()
    b.stat("GPU util", [("avg(DCGM_FI_DEV_GPU_UTIL)", "util")], unit="percent", w=6)
    b.stat("SM active", [("avg(DCGM_FI_PROF_SM_ACTIVE)", "SM active")], unit="percentunit", w=6)
    b.stat("Framebuffer used", [("sum(DCGM_FI_DEV_FB_USED)", "FB used")], unit="mbytes", w=6)
    b.stat("Power", [("sum(DCGM_FI_DEV_POWER_USAGE)", "power")], unit="watt", w=6)
    b.ts("GPU util (DCGM)", [("DCGM_FI_DEV_GPU_UTIL", "GPU {{gpu}} util")], unit="percent", max_=100, w=12)
    b.ts("SM active vs tensor pipe active", [("DCGM_FI_PROF_SM_ACTIVE", "SM active {{gpu}}"), ("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "tensor active {{gpu}}")],
         unit="percentunit", max_=1, w=12, desc="DCGM profiling fields (only if the exporter enables them). GPU_UTIL is 'any kernel running'; SM active is how busy the SMs really are.")
    b.ts("Framebuffer used / free", [("DCGM_FI_DEV_FB_USED", "used {{gpu}}"), ("DCGM_FI_DEV_FB_FREE", "free {{gpu}}")], unit="mbytes", stack=True, w=12)
    b.ts("Power", [("DCGM_FI_DEV_POWER_USAGE", "power {{gpu}}")], unit="watt", w=12)
    b.ts("KV usage per pool vs GPU util",
         [("sum by (app) (vllm:kv_cache_usage_perc)", "KV {{app}}"), ("avg(DCGM_FI_DEV_GPU_UTIL) / 100", "GPU util")],
         unit="percentunit", max_=1, dashed="GPU util", w=12)
    b.ts("Tokens / s on the GPU (every engine)",
         [(_rate("vllm:prompt_tokens_total", "", ENG), "prompt {{app}}"),
          (_rate("vllm:generation_tokens_total", "", ENG), "generation {{app}}")], stack=True, w=12)
    return _demo("demo-gpu", "Demo / GPU", b, ["gpu", "dcgm"], (), "DCGM on the one H100 shared by every vLLM engine.")


EXP1_TEXT = """
### Exp-1 · each tenant alone (config `full`, no contention)
**1A** Tenant A SQL team on **qwen-27b** (decode-heavy: short SQL prompts, long thinking) · **1B** Tenant B 10-K team on **qwen-9b** (prefill-heavy: ~30K-token filings, short answers).
Look at: the **prefill:decode ratio** (27B low, 9B high) · TTFT vs ITL per model · reasoning vs answer time · tool calls · prefix hits as agents reuse the same filing / schema. Queue wait should be ≈ 0. Orange marks = run annotations.
"""

EXP2_TEXT = """
### Exp-2 · both tenants in parallel (shared 27B) · `baseline` vs `full`
Tenant A (27B SQL team) + Tenant B (9B analysts, 27B lead/synthesis), started ~30 s apart. Compare the two runs (orange marks).
Look at: per-tenant latency side by side · **27B lane queue wait by tenant** (B's synthesis behind A's SQL turns) · sheds and spills · **27B ITL vs 9B prefill tokens/s** (shared SMs) · Exp-2b: 9B TTFT vs 27B ITL across `--max-num-batched-tokens` 2048 / 8192 / 16384.
"""

EXP3_TEXT = """
### Exp-3 · Locust ramp 1 → 64 users × configs baseline / route / queue / full
Users are not exported, so **offered load = calls/s**. Look at: goodput (200/s and SLO-met/s) bending away from offered load · p50/p99 TTFT and e2e · shed rate by code (should appear in `full`, not as timeouts) · KV % and prefix hit % · gateway queue depth and Little's L (λ·W should match the measured depth). Purple marks = gateway mode switches.
"""


def demo_exp1() -> dict:
    b = _Board()
    v = 'model_name=~"qwen-.*"'
    ok = 'status="200"'
    b.text(EXP1_TEXT, h=4)
    b.stat("Prefill:decode tokens (range)", [(_prefill_decode_ratio(v, "$__range"), "{{model_name}}")], w=6, instant=True, decimals=1)
    b.stat("Prefix-cache hit % (range)", [(_prefix_hit(v, "$__range"), "{{model_name}}")], w=6, instant=True, unit="percentunit")
    b.stat("Tool calls (range)", [(_inc("orch_tool_calls_total", "", "tenant"), "{{tenant}}")], w=6, instant=True, decimals=0)
    b.stat("LLM calls (range)", [(_inc("orch_chat_completed_total", "", "model"), "{{model}}")], w=6, instant=True, decimals=0)

    b.ts("Prompt vs generation tokens / s",
         [(_rate("vllm:prompt_tokens_total", v, "model_name"), "prompt {{model_name}}"),
          (_rate("vllm:generation_tokens_total", v, "model_name"), "generation {{model_name}}")], w=12)
    b.ts("Prefill:decode token ratio", [(_prefill_decode_ratio(v), "{{model_name}}"), (_prefill_decode_ratio(v, excl_cache=True), "computed (no cache hits) {{model_name}}")],
         dashed="computed.*", w=12)
    b.ts("TTFT p50 / p99 (gateway)", _p50_p99("orch_ttft_seconds", "model", "{{model}}"), unit="s", w=8)
    b.ts("ITL p50 / p99 (vLLM)", _p50_p99("vllm:inter_token_latency_seconds", "model_name", "{{model_name}}", v), unit="s", w=8)
    b.ts("Reasoning vs answer time per call (mean)",
         [(_mean("orch_reasoning_seconds", "model, role"), "reasoning {{model}} {{role}}"),
          (f'({_mean("orch_e2e_seconds", "model, role", ok)}) - ({_mean("orch_ttfat_seconds", "model, role")})', "answer {{model}} {{role}}")], unit="s", w=8,
         desc="reasoning = first token → last reasoning chunk; answer = e2e − TTFAT.")
    b.ts("Reasoning vs answer tokens / s (est.)", [(_rate("orch_tokens_total", 'kind=~"reasoning_est|answer_est"', "model, kind"), "{{model}} {{kind}}")],
         stack=True, w=8)
    b.ts("Tool calls / s by tool", [(_rate("orch_tool_calls_total", "", "tenant, tool"), "{{tenant}} {{tool}}")], w=8)
    b.ts("Prefix-cache hit rate", [(_prefix_hit(v), "{{model_name}}"), (_cached_share(), "gateway cached share {{model}}")],
         unit="percentunit", max_=1, dashed="gateway.*", w=8)
    b.ts("KV cache usage", [("sum by (model_name) (vllm:kv_cache_usage_perc{%s})" % v, "{{model_name}}")], unit="percentunit", max_=1, w=8)
    b.ts("e2e p50 / p99 by role", _p50_p99("orch_e2e_seconds", "model, role", "{{model}} {{role}}", 'status="200"'), unit="s", w=8)
    b.ts("Gateway queue wait p99 (≈ 0 here)", [(_q(0.99, "orch_queue_wait_seconds", "pool"), "{{pool}}")], unit="s", w=8)
    return _demo("demo-exp1", "Demo / Exp-1 Solo runs", b, ["experiment", "exp1"], (), "Each tenant alone.")


def demo_exp2() -> dict:
    b = _Board()
    b.text(EXP2_TEXT, h=4)
    _mode_stat(b, w=4)
    b.stat("Sheds (range)", [(_inc("orch_admission_total", 'decision="shed"', "tenant"), "{{tenant}}")], w=5, instant=True, decimals=0)
    b.stat("Spills (range)", [(_inc("orch_spill_total"), "spills")], w=5, instant=True, decimals=0)
    b.stat("SLO attainment (range)", _slo_targets("tenant", "", "$__range"), w=10, instant=True, unit="percentunit",
           thresholds=[(None, "red"), (0.9, "orange"), (0.99, "green")])

    b.row("Per tenant, side by side")
    b.ts("TTFT p99 by tenant & role", [(_q(0.99, "orch_ttft_seconds", "tenant, role"), "{{tenant}} {{role}}")], unit="s", w=8)
    b.ts("TTFAT p99 by tenant & role", [(_q(0.99, "orch_ttfat_seconds", "tenant, role"), "{{tenant}} {{role}}")], unit="s", w=8)
    b.ts("e2e p50 / p99 by tenant (200s)", _p50_p99("orch_e2e_seconds", "tenant", "{{tenant}}", 'status="200"'), unit="s", w=8)

    b.row("The shared 27B lane")
    s27 = f'pool="{M27}"'
    tok27 = f'model="{M27}",kind=~"prompt|completion"'
    b.ts("27B lane queue wait p50 / p95 by tenant",
         [(_q(0.5, "orch_queue_wait_seconds", "tenant, sla", s27), "p50 {{tenant}} {{sla}}"),
          (_q(0.95, "orch_queue_wait_seconds", "tenant, sla", s27), "p95 {{tenant}} {{sla}}")], unit="s", w=8)
    b.ts("27B lane depth by SLA vs bound",
         [(f"sum by (sla) (orch_queue_depth{{{s27}}})", "{{sla}}"), (f"orch_queue_bound{{{s27}}}", "bound")], stack=False, dashed="bound", w=8)
    b.ts("27B token share by tenant (DRR)",
         [(f'{_rate("orch_tokens_total", tok27, "tenant")} / ignoring(tenant) group_left () ({_rate("orch_tokens_total", tok27)} > 0)', "{{tenant}}")],
         unit="percentunit", max_=1, stack=True, w=8, desc="prompt+completion tokens served on the 27B per tenant (recorded when calls finish).")
    b.ts("Sheds by tenant, code, reason", [(_rate("orch_admission_total", 'decision="shed"', "tenant, code, reason"), "{{tenant}} {{code}} {{reason}}")],
         unit="reqps", stack=True, w=12)
    b.ts("Spills by role", [(_rate("orch_spill_total", "", "from_pool, to_pool, role"), "{{from_pool}}→{{to_pool}} {{role}}")], unit="reqps", w=12)

    b.row("GPU-sharing interference and chunked prefill (Exp-2b)")
    b.ts("27B ITL vs 9B prefill tokens / s",
         [(_q(0.5, "vllm:inter_token_latency_seconds", "", f'model_name="{M27}"'), "27B ITL p50"),
          (_q(0.99, "vllm:inter_token_latency_seconds", "", f'model_name="{M27}"'), "27B ITL p99"),
          (_rate("vllm:prompt_tokens_total", f'model_name="{M9}"'), "9B prompt tok/s")],
         unit="s", right="9B prompt.*", right_unit="short", w=12,
         desc="Both engines time-share the SMs: a long 9B prefill step delays the 27B's decode steps.")
    b.ts("9B TTFT vs 27B ITL (chunk-size trade-off)",
         [(_q(0.5, "vllm:time_to_first_token_seconds", "", f'model_name="{M9}"'), "9B TTFT p50"),
          (_q(0.99, "vllm:time_to_first_token_seconds", "", f'model_name="{M9}"'), "9B TTFT p99"),
          (_q(0.99, "vllm:inter_token_latency_seconds", "", f'model_name="{M27}"'), "27B ITL p99")],
         unit="s", right="27B ITL.*", right_unit="s", w=12,
         desc="Small --max-num-batched-tokens: worse 9B TTFT, smoother 27B ITL. Large: the reverse. Compare across runs (orange marks).")
    b.ts("Prefill:decode mix over time", [(_prefill_decode_ratio('model_name=~"qwen-.*"'), "{{model_name}}")], w=8)
    b.ts("9B ITL p50 / p99 (own later turns)", _p50_p99("vllm:inter_token_latency_seconds", "", "9B", f'model_name="{M9}"'), unit="s", w=8)
    b.ts("KV usage and preemptions",
         [("sum by (model_name) (vllm:kv_cache_usage_perc)", "KV {{model_name}}"),
          (_rate("vllm:num_preemptions_total", "", "model_name"), "preempt/s {{model_name}}")],
         unit="percentunit", right="preempt.*", right_unit="short", w=8)
    return _demo("demo-exp2", "Demo / Exp-2 Multi-tenant", b, ["experiment", "exp2"], (), "Both tenants on one GPU.")


def demo_exp3() -> dict:
    b = _Board()
    b.text(EXP3_TEXT, h=4)
    _mode_stat(b, w=4)
    b.stat("Offered calls / s", [(_rate("orch_chat_requests_total"), "offered")], unit="reqps", decimals=2, w=5)
    b.stat("Goodput 200 / s", [(_rate("orch_chat_completed_total", 'status="200"'), "200/s")], unit="reqps", decimals=2, w=5)
    b.stat("Shed share", [(_ratio(_rate("orch_admission_total", 'decision="shed"'), _rate("orch_chat_requests_total")), "shed")],
           unit="percentunit", w=5, thresholds=[(None, "green"), (0.05, "orange"), (0.2, "red")])
    b.stat("Queued now", [("sum(orch_queue_depth)", "queued")], w=5)

    b.ts("Goodput vs offered load",
         [(_rate("orch_chat_requests_total"), "offered (calls/s)"),
          (_rate("orch_chat_completed_total", 'status="200"'), "completed 200/s"),
          (_slo_met_rate(), "met TTFT SLO /s"),
          (_rate("orch_admission_total", 'decision="shed"'), "shed/s")],
         unit="reqps", dashed="offered.*", w=12)
    b.ts("Non-200 by layer · code · reason", [(_rate("orch_rejections_total", "", "layer, code, reason"), "{{layer}} {{code}} {{reason}}")],
         unit="reqps", stack=True, w=12, desc="Every call that didn't get a 200, by the layer that decided it (gateway/codes.py): edge 400 (guardrails), admission 429 (tenant budget, SLA estimate) / 503 (KV pressure, stale metrics, busy), queue 503 (full, expired), engine 5xx, client 499 (hung up).")
    b.ts("TTFT p50 / p99 by model & SLA", _p50_p99("orch_ttft_seconds", "model, sla", "{{model}} {{sla}}"), unit="s", w=12)
    b.ts("e2e p50 / p99 by model (200s)", _p50_p99("orch_e2e_seconds", "model", "{{model}}", 'status="200"'), unit="s", w=12)
    b.ts("KV % and prefix hit %",
         [("sum by (app) (vllm:kv_cache_usage_perc)", "KV {{app}}"), (_prefix_hit('model_name=~"qwen-.*"', by=ENG), "prefix hit {{app}}")],
         unit="percentunit", max_=1, dashed="prefix.*", w=8)
    b.ts("Queue depth per pool vs vLLM waiting",
         [("sum by (pool) (orch_queue_depth)", "gateway {{pool}}"), ("sum by (app) (vllm:num_requests_waiting)", "vLLM waiting {{app}}")],
         dashed="vLLM.*", w=8, desc="With the gateway queue on, waiting moves out of vLLM (invisible, FIFO) into the gateway (visible, EDF/DRR).")
    b.ts("Little's law check",
         [("orch_little_l", "L gateway {{pool}}"), (_rate("orch_queue_wait_seconds_sum", "", "pool"), "λ·W {{pool}}"),
          ("sum by (pool) (orch_queue_depth)", "depth {{pool}}")], dashed="(L|λ).*", w=8)
    b.ts("Queue wait p99 by pool", [(_q(0.99, "orch_queue_wait_seconds", "pool"), "{{pool}}")], unit="s", w=8)
    b.ts("Inflight vs slots", [("sum by (pool) (orch_inflight)", "inflight {{pool}}"), ("sum by (pool) (orch_slots)", "slots {{pool}}")],
         dashed="slots.*", w=8)
    b.ts("Preemptions / s", [(_rate("vllm:num_preemptions_total", "", ENG), "{{app}}")], w=8)
    return _demo("demo-exp3", "Demo / Exp-3 Load", b, ["experiment", "exp3"], (), "Locust ramp across gateway modes.")


EXP4_TEXT = """
### Exp-4 · serving-layer scale-out: two Qwen 9B replicas (qwen-9b-a, qwen-9b-b) on one H100
Variants (orange marks): **one** (qwen-9b-a only, qwen-9b-b idle) · **round_robin** · **prefix_p2c**, mode `route` (no admission, no gateway queue: overload waits inside vLLM).
Look at: completed and SLO-met calls/s per variant as users ramp 4 → 32 · TTFT p50/p99 · **calls per replica and why** (prefix = KV-aware placement) · **running / waiting per replica** · prefix-cache hit rate per replica · GPU util (both replicas share its compute).
"""


def demo_exp4() -> dict:
    b = _Board()
    b.text(EXP4_TEXT, h=4)
    b.stat("Completed 200 / s", [(_rate("orch_chat_completed_total", 'status="200"'), "200/s")], unit="reqps", decimals=2, w=6)
    b.stat("Met TTFT SLO / s", [(_slo_met_rate(), "met/s")], unit="reqps", decimals=2, w=6)
    b.stat("Waiting inside vLLM", [("sum(vllm:num_requests_waiting)", "waiting")], w=6, thresholds=[(None, "green"), (1, "orange"), (8, "red")])
    b.stat("GPU util", [("avg(DCGM_FI_DEV_GPU_UTIL)", "util")], unit="percent", w=6)

    b.row("Throughput and latency")
    b.ts("Calls / s: offered, completed, met SLO",
         [(_rate("orch_chat_requests_total"), "offered"), (_rate("orch_chat_completed_total", 'status="200"'), "completed 200"),
          (_slo_met_rate(), "met TTFT SLO")], unit="reqps", dashed="offered", w=12)
    b.ts("TTFT p50 / p99 by SLA class", _p50_p99("orch_ttft_seconds", "sla", "{{sla}}"), unit="s", w=12)

    b.row("Per replica")
    b.ts("Placement: calls / s by replica & reason", [(_rate("orch_route_total", 'pool="qwen-9b"', "replica, reason"), "{{replica}} {{reason}}")],
         unit="reqps", stack=True, w=8, desc="prefix = sent where the prompt's prefix is cached (KV-aware); p2c = the less loaded of two; round_robin; single = one replica in the pool.")
    b.ts("vLLM running / waiting per replica",
         [("sum by (app) (vllm:num_requests_running)", "running {{app}}"), ("sum by (app) (vllm:num_requests_waiting)", "waiting {{app}}")],
         dashed="waiting.*", w=8)
    b.ts("Prefix-cache hit rate per replica", [(_prefix_hit('model_name=~"qwen-.*"', by=ENG), "{{app}}")], unit="percentunit", max_=1, w=8)
    b.ts("KV cache used per replica", [("sum by (app) (vllm:kv_cache_usage_perc)", "{{app}}")], unit="percentunit", max_=1, w=8)
    b.ts("Generated tokens / s per replica", [(_rate("vllm:generation_tokens_total", "", ENG), "{{app}}")], stack=True, w=8,
         desc="Stacked: the total is what the one GPU produces; two replicas share its compute.")
    b.ts("Token gap (ITL) p50 per replica", [(_q(0.5, "vllm:inter_token_latency_seconds", ENG), "{{app}}")], unit="s", w=8,
         desc="Rises when both replicas are busy: they time-share one GPU.")
    return _demo("demo-exp4", "Demo / Exp-4 Scale-out", b, ["experiment", "exp4"], (), "One vs two 9B replicas and placement, one H100.")


DEMO_DASHBOARDS = {
    "demo-pipeline": demo_pipeline,
    "demo-engines": demo_engines,
    "demo-tenants": demo_tenants,
    "demo-gpu": demo_gpu,
    "demo-exp1": demo_exp1,
    "demo-exp2": demo_exp2,
    "demo-exp3": demo_exp3,
    "demo-exp4": demo_exp4,
}


def write_dashboards(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    # Boards are written compact: setup/observability.sh ships the whole folder as one
    # ConfigMap through `kubectl apply`, whose last-applied annotation is capped at 256 KiB.
    for name, builder in DEMO_DASHBOARDS.items():
        path = dest / f"{name}.json"
        path.write_text(json.dumps(builder(), separators=(",", ":")) + "\n")
        written.append(path)
    return written

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    write_dashboards(root / "k8s-config" / "observability" / "dashboards")
