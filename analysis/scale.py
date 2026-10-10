"""Exp-4: serving-layer scale-out. Does a second 9B replica move the knee, and does placement matter?

    .venv/bin/python -m analysis.scale results/exp4/<run>          # after experiments/collect.sh

Reads the run folder experiments/exp4_*.sh wrote (locust/<variant>/meta.json, agents/<config>/meta.json)
and the gateway's own records pulled by collect.sh (results/box/live/*/gateway/requests.jsonl: one row per
call; events.jsonl: the 1 s state of every replica). Writes <run>/report.md and <run>/report.json.

Locust part, per variant and ramp step (a call belongs to the step it started in):
  calls/s        successful calls per second
  goodput/s      successful calls that met their SLA class's TTFT and first-answer targets, per second
  attainment     those good calls ÷ all attempts (an error counts as a miss)
  TTFT p50/p99, first answer p99, errors, calls per replica, running/waiting per replica (mean, peak),
  prefix-cache hit rate per replica (vLLM's counters over the step)

Decision rules (fixed before the run):
  a step is SUSTAINED when attainment >= 90% and no call failed; a variant's knee is the highest step
  sustained with every step below it sustained.
  S2 = max goodput/s over one's sustained steps of round_robin ÷ the same for one; E2 = S2 / 2.
  Also shown: the goodput ratio at the highest step both sustain (the same offered load).
  Scale-out confirmed: S2 >= 1.30, or round_robin's knee is at least one ramp step right of one's with
  attainment at least as high there. Placement adds value: prefix_p2c beats round_robin by > 15% on
  goodput or TTFT p99 at the highest step both sustain.

Agent part, per config: each tenant session's time, calls, errors, TTFT p50/p99; makespan; calls,
running/waiting and prefix-cache hit rate per replica.
"""

from __future__ import annotations

import argparse
from collections import Counter
import glob
import json
import statistics
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SUSTAIN_ATTAINMENT = 0.90
SCALE_OUT_S2 = 1.30
PLACEMENT_GAIN = 0.15


def q(xs: list[float | None], p: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def targets_from(config: dict) -> dict[str, tuple[float, float]]:
    """SLA class -> (TTFT target, first-answer target) from a gateway config (raw or /admin/config)."""
    raw = config.get("config", config)  # GET /admin/config wraps the settings in "config"
    return {n: (c["ttft_s"], c.get("ttfat_s") or c["ttft_s"]) for n, c in raw["sla_classes"].items()}


def good(r: dict, targets: dict[str, tuple[float, float]]) -> bool:
    if r.get("status") != 200:
        return False
    ttft_t, ttfat_t = targets.get(r.get("sla") or "", (float("inf"), float("inf")))
    ttft, ttfat = r.get("ttft_s"), r.get("ttfat_s") if r.get("ttfat_s") is not None else r.get("ttft_s")
    return ttft is not None and ttft <= ttft_t and (ttfat is None or ttfat <= ttfat_t)


def replica_series(states: list[dict], t0: float, t1: float) -> dict[str, dict]:
    """Per replica over [t0, t1): running / waiting mean and peak, prefix-cache hit rate (counter deltas)."""
    out: dict[str, dict] = {}
    for e in states:
        if not t0 <= e["t"] < t1:
            continue
        for r in e.get("replicas") or []:
            s = out.setdefault(r["id"], {"running": [], "waiting": [], "hits": [], "queries": []})
            s["running"].append(r.get("running") or 0.0)
            s["waiting"].append(r.get("waiting") or 0.0)
            if r.get("prefix_queries") is not None:
                s["hits"].append(r.get("prefix_hits") or 0.0)
                s["queries"].append(r["prefix_queries"])
    res = {}
    for rid, s in out.items():
        dq = (s["queries"][-1] - s["queries"][0]) if len(s["queries"]) > 1 else 0
        dh = (s["hits"][-1] - s["hits"][0]) if len(s["hits"]) > 1 else 0
        res[rid] = {"running_mean": statistics.mean(s["running"]), "running_peak": max(s["running"]),
                    "waiting_mean": statistics.mean(s["waiting"]), "waiting_peak": max(s["waiting"]),
                    "prefix_hit_rate": dh / dq if dq > 0 else None}
    return res


def step_metrics(rows: list[dict], states: list[dict], t0: float, steps: list[int], hold_s: float,
                 targets: dict[str, tuple[float, float]]) -> list[dict]:
    out = []
    for i, users in enumerate(steps):
        a, b = t0 + i * hold_s, t0 + (i + 1) * hold_s
        # 499 = the client hung up: Locust stopping at the end of a ramp cancels the calls still running. Not the
        # system failing, so not an attempt; counted on its own.
        hung_up = sum(1 for r in rows if a <= r["ts"] < b and r.get("status") == 499)
        rs = [r for r in rows if a <= r["ts"] < b and r.get("status") != 499]
        ok = [r for r in rs if r.get("status") == 200]
        g = [r for r in rs if good(r, targets)]
        split: dict[str, int] = {}
        why: dict[str, int] = {}
        refused: dict[str, int] = {}
        docs: dict[str, dict[str, int]] = {}
        for r in ok:
            split[r.get("replica") or "?"] = split.get(r.get("replica") or "?", 0) + 1
            why[r.get("route_reason") or "?"] = why.get(r.get("route_reason") or "?", 0) + 1
            if str(r.get("task", "")).startswith("locust-doc-"):
                d = docs.setdefault(r["task"], {})
                d[r.get("replica") or "?"] = d.get(r.get("replica") or "?", 0) + 1
        for r in rs:
            if r.get("status") != 200:
                k = f"{r.get('status')} {r.get('shed_reason') or r.get('error') or ''}".strip()
                refused[k] = refused.get(k, 0) + 1
        out.append({
            "users": users, "attempts": len(rs), "ok": len(ok), "errors": len(rs) - len(ok), "refused": refused, "hung_up": hung_up,
            "route_reasons": why, "gw_queue_p95": q([r.get("queue_wait_s") or 0 for r in ok], 0.95),
            # placement: share of each document's calls that landed on the replica that served it most
            "doc_stickiness": (sum(max(d.values()) for d in docs.values()) / sum(sum(d.values()) for d in docs.values())) if docs else None,
            "calls_s": len(ok) / hold_s, "goodput_s": len(g) / hold_s,
            "attainment": len(g) / len(rs) if rs else None,
            "ttft_p50": q([r.get("ttft_s") for r in ok], 0.50), "ttft_p99": q([r.get("ttft_s") for r in ok], 0.99),
            "ttfat_p99": q([r.get("ttfat_s") for r in ok], 0.99),
            "cached_share": (sum(r.get("cached_tokens") or 0 for r in ok) / max(1, sum(r.get("prompt_tokens") or 0 for r in ok))),
            "split": split, "replicas": replica_series(states, a, b),
        })
        out[-1]["sustained"] = bool(rs) and out[-1]["errors"] == 0 and out[-1]["attainment"] >= SUSTAIN_ATTAINMENT
    return out


def knee(steps: list[dict]) -> dict | None:
    """The highest step sustained with every step below it sustained."""
    last = None
    for s in steps:
        if not s["sustained"]:
            break
        last = s
    return last


def placement_verdict(rr: list[dict], p2c: list[dict]) -> dict:
    """prefix_p2c vs round_robin at the highest step both sustain (the first step if neither does)."""
    common = [(a, b) for a, b in zip(rr, p2c) if a["sustained"] and b["sustained"]]
    a, b = common[-1] if common else (rr[0], p2c[0])
    gain_good = (b["goodput_s"] / a["goodput_s"] - 1) if a["goodput_s"] else None
    gain_p99 = (1 - b["ttft_p99"] / a["ttft_p99"]) if a["ttft_p99"] and b["ttft_p99"] is not None else None
    return {"users": a["users"], "goodput_gain": gain_good, "ttft_p99_gain": gain_p99,
            "cached_share": (a["cached_share"], b["cached_share"]), "doc_stickiness": (a["doc_stickiness"], b["doc_stickiness"]),
            "adds_value": any(g is not None and g > PLACEMENT_GAIN for g in (gain_good, gain_p99))}


def verdict(by_variant: dict[str, list[dict]]) -> dict:
    v: dict = {}
    one, rr, p2c = (by_variant.get(k) for k in ("one", "round_robin", "prefix_p2c"))
    if one and rr:
        k1, k2 = knee(one), knee(rr)
        g1 = max((s["goodput_s"] for s in one if s["sustained"]), default=0.0)
        g2 = max((s["goodput_s"] for s in rr if s["sustained"]), default=0.0)
        s2 = g2 / g1 if g1 else None
        common = [(a, b) for a, b in zip(one, rr) if a["sustained"] and b["sustained"]]
        same_load = (common[-1][1]["goodput_s"] / common[-1][0]["goodput_s"]) if common and common[-1][0]["goodput_s"] else None
        idx = {s["users"]: i for i, s in enumerate(one)}
        knee_moved = bool(k2 and (k1 is None or idx[k2["users"]] > idx[k1["users"]]))
        no_worse = bool(k2 and (k1 is None or rr[idx[k1["users"]]]["attainment"] >= k1["attainment"]))
        v["scale_out"] = {
            "knee_one": k1 and k1["users"], "knee_round_robin": k2 and k2["users"],
            "S2": s2, "E2": s2 / 2 if s2 else None,
            "same_load_users": common[-1][0]["users"] if common else None, "same_load_ratio": same_load,
            "confirmed": bool((s2 is not None and s2 >= SCALE_OUT_S2) or (knee_moved and no_worse)),
        }
    if rr and p2c:
        v["placement"] = placement_verdict(rr, p2c)
    return v


def load_gateway(results: Path, t0: float, t1: float) -> tuple[list[dict], list[dict]]:
    """The gateway's call rows and state events in [t0, t1] from every live run collect.sh pulled."""
    rows, states = [], []
    for d in sorted(glob.glob(str(results / "box" / "live" / "*" / "gateway"))):
        req, ev = Path(d) / "requests.jsonl", Path(d) / "events.jsonl"
        if req.exists():
            for line in open(req):
                r = json.loads(line)
                if t0 <= r["ts"] <= t1:
                    rows.append(r)
        if ev.exists():
            for line in open(ev):
                if '"kind": "state"' not in line:
                    continue
                e = json.loads(line)
                if t0 <= e["t"] <= t1:
                    e["replicas"] = [{k: r.get(k) for k in ("id", "running", "waiting", "prefix_hits", "prefix_queries")}
                                     for r in e.get("replicas") or []]
                    states.append({"t": e["t"], "replicas": e["replicas"]})
    rows.sort(key=lambda r: r["ts"])
    states.sort(key=lambda e: e["t"])
    return rows, states


def fmt(x: float | None, spec: str = ".2f", pct: bool = False) -> str:
    if x is None:
        return "–"
    return f"{100 * x:.0f}%" if pct else format(x, spec)


PARTS = {  # folder -> heading
    "locust": "Part 1 · scale-out: the Locust ramp, one vs round_robin vs prefix_p2c (mode route)",
    "placement": "Part 2 · placement: N distinct documents, round_robin vs prefix_p2c (mode route)",
    "modes": "Part 3 · the gateway's control plane under the same overload: route / queue / full",
    "demo": "Demo · placement workload, prefix_p2c, mode full (recorded live; not part of the measurement)",
    "routing": "Extra A · four routing policies on the placement workload (mode route)",
    "queue_depth": "Extra B · gateway queue depth 0 / 2 / 8 / 64 (mode queue, admission off)",
    "replicate": "Extra C · Part 1 repeated: one vs round_robin vs prefix_p2c",
    "spare": "Head-to-head H2 · placement with spare capacity (documents, 12 users): round_robin vs prefix_p2c_v2",
    "saturated": "Head-to-head H3 · placement at saturation (documents, 24 users): prefix_p2c vs prefix_p2c_v2",
    "memory": "Memory-pressure demo · KV cache shrunk until memory is the limit (documents, 24 users): route vs full",
    "batch16": "Head-to-head H1 · ONE replica with 16 slots (compare with two replicas × 8 slots: Part 1 / extra C round_robin)",
}


def locust_part(run: Path, results: Path, part: str = "locust") -> tuple[list[str], dict]:
    metas = sorted(glob.glob(str(run / part / "*" / "meta.json")))
    if not metas:
        return [], {}
    by_variant: dict[str, list[dict]] = {}
    lines = [f"## {PARTS[part]}", ""]
    order = {"one": 0, "one16": 0, "round_robin": 1, "least_loaded": 2, "p2c": 3, "prefix_p2c": 4, "prefix_p2c_v2": 5, "route": 6, "queue": 7, "full": 8,
             "depth_0": 8, "depth_2": 9, "depth_8": 10, "depth_64": 11}
    for m in sorted(metas, key=lambda p: order.get(Path(p).parent.name, 9)):
        meta = json.load(open(m))
        cfg_path = Path(m).parent / "config.json"
        config = json.load(open(cfg_path)) if cfg_path.exists() else yaml.safe_load(open(ROOT / "gateway" / "config.yaml"))
        rows, states = load_gateway(results, meta["start"] - 5, meta["end"] + 5)
        rows = [r for r in rows if r.get("variant") == meta["variant_id"] and str(r.get("task", "")).startswith("locust-")]
        if not rows:
            lines += [f"**{meta['variant']}**: no gateway rows found (run experiments/collect.sh first)", ""]
            continue
        steps = [int(x) for x in str(meta["ramp_steps"]).split(",")]
        t0 = rows[0]["ts"]  # the ramp's clock starts with Locust's first call
        sm = step_metrics(rows, states, t0, steps, float(meta["ramp_hold_s"]), targets_from(config))
        by_variant[meta["variant"]] = sm
        docs = f", {meta['load_docs']} distinct documents" if meta.get("load_docs") else ""
        lines += [f"### {meta['variant']}{docs}", "",
                  "| users | calls/s | goodput/s | attainment | TTFT p50 / p99 (s) | first answer p99 (s) | refused / failed | gateway queue p95 (s) | calls per replica | placement reasons | running / waiting mean (peak) per replica | prefix hit rate per replica | prompt from cache | same document, same replica | sustained |",
                  "|---:|---:|---:|---:|---|---:|---|---:|---|---|---|---|---:|---:|:-:|"]
        for s in sm:
            reps = sorted(set(s["split"]) | set(s["replicas"]))
            split = ", ".join(f"{r}: {s['split'].get(r, 0)}" for r in reps)
            rw = ", ".join(f"{r}: {fmt(v['running_mean'], '.1f')} / {fmt(v['waiting_mean'], '.1f')} ({v['waiting_peak']:.0f})"
                           for r, v in sorted(s["replicas"].items()))
            hit = ", ".join(f"{r}: {fmt(v['prefix_hit_rate'], pct=True)}" for r, v in sorted(s["replicas"].items()))
            refused = ", ".join(f"{k}: {n}" for k, n in sorted(s["refused"].items())) or "0"
            if s["hung_up"]:
                refused += f" (+{s['hung_up']} cancelled by the client at the end, not counted)"
            why = ", ".join(f"{k} {n}" for k, n in sorted(s["route_reasons"].items(), key=lambda x: -x[1]))
            lines.append(f"| {s['users']} | {s['calls_s']:.2f} | {s['goodput_s']:.2f} | {fmt(s['attainment'], pct=True)} | "
                         f"{fmt(s['ttft_p50'])} / {fmt(s['ttft_p99'])} | {fmt(s['ttfat_p99'])} | {refused} | {fmt(s['gw_queue_p95'])} | {split} | {why} | {rw} | {hit} | "
                         f"{fmt(s['cached_share'], pct=True)} | {fmt(s['doc_stickiness'], pct=True)} | {'yes' if s['sustained'] else 'no'} |")
        lines.append("")
    if part in ("routing", "queue_depth", "spare", "saturated"):
        lines += compare_table(part, by_variant)
        return lines, {"steps": by_variant}
    if part == "batch16":
        lines += ["Compare step by step with two replicas × 8 slots (Part 1 and extra C, variant round_robin): the knee, "
                  "goodput/s and TTFT, and the token gap per call (one engine batches all its calls in one weight read "
                  "per step; two engines on one GPU each read the weights, taking turns).", ""]
        return lines, {"steps": by_variant}
    if part in ("modes", "demo", "memory"):
        lines += ["Read across: in route the overload waits inside vLLM (running/waiting); in queue it waits in the "
                  "gateway queue (queue p95) and vLLM waiting stays ~0; in full admission refuses with 429 (SLA "
                  "estimate / tenant budget) or 503 (no safe capacity). Refusals count as misses in attainment.", ""]
        return lines, {"steps": by_variant}
    v = verdict(by_variant) if part in ("locust", "replicate") else {"placement": placement_verdict(by_variant["round_robin"], by_variant["prefix_p2c"])} \
        if {"round_robin", "prefix_p2c"} <= set(by_variant) else {}
    lines += ["### Verdict (rules fixed before the run)", ""]
    if "scale_out" in v:
        so = v["scale_out"]
        lines += [f"- Knee: one at **{so['knee_one']}** users, round_robin at **{so['knee_round_robin']}** users "
                  f"(sustained = attainment ≥ {SUSTAIN_ATTAINMENT:.0%} and no errors).",
                  f"- **S₂ = {fmt(so['S2'])}, E₂ = {fmt(so['E2'])}** (max sustained goodput, round_robin ÷ one). "
                  f"At the same offered load ({so['same_load_users']} users, the highest both sustain): "
                  f"round_robin ÷ one goodput = {fmt(so['same_load_ratio'])}.",
                  f"- Serving-layer scale-out: **{'CONFIRMED' if so['confirmed'] else 'NOT shown'}** "
                  f"(S₂ ≥ {SCALE_OUT_S2} or the knee moves ≥ 1 step right without worse attainment)."
                  + ("" if so["confirmed"] else " Shared H100 compute, not replica count, is the bottleneck.")]
    if "placement" in v:
        pl = v["placement"]
        lines += [f"- Placement at {pl['users']} users: prefix_p2c vs round_robin goodput {fmt(pl['goodput_gain'], pct=True)}, "
                  f"TTFT p99 {fmt(pl['ttft_p99_gain'], pct=True)} better; prompt from cache {fmt(pl['cached_share'][0], pct=True)} → "
                  f"{fmt(pl['cached_share'][1], pct=True)} → **{'adds value' if pl['adds_value'] else 'no meaningful difference'}** "
                  f"(threshold > {PLACEMENT_GAIN:.0%} on goodput or TTFT p99)."]
    lines.append("")
    return lines, {"steps": by_variant, "verdict": v}


def agents_part(run: Path, results: Path) -> tuple[list[str], dict]:
    metas = sorted(glob.glob(str(run / "agents" / "*" / "meta.json")))
    if not metas:
        return [], {}
    out: dict = {}
    lines = ["## Part 4 · the three tenants at once (sql, 10k8, code), every call on the 9B", "",
             "| config | tenant | session (min) | calls | errors | TTFT p50 / p99 (s) | ended |",
             "|---|---|---:|---:|---:|---|---|"]
    fleet = ["", "| config | makespan (min) | calls | calls per replica | running / waiting mean (peak) per replica | prefix hit rate per replica | prompt tokens from cache |",
             "|---|---:|---:|---|---|---|---:|"]
    for m in metas:
        meta = json.load(open(m))
        cfg = meta["config"]
        rows, states = load_gateway(results, meta["start"] - 5, meta["end"] + 60)
        rows = [r for r in rows if r.get("task") in meta["labels"].values()]
        out[cfg] = {}
        for tenant, label in meta["labels"].items():
            rs = [r for r in rows if r.get("task") == label]
            ok = [r for r in rs if r.get("status") == 200]
            span = (max(r["ts"] + (r.get("e2e_s") or 0) for r in rs) - rs[0]["ts"]) / 60 if rs else None
            ended = meta.get("ended", {}).get(tenant, "?")
            out[cfg][tenant] = {"session_min": span, "calls": len(rs), "errors": len(rs) - len(ok),
                                "ttft_p50": q([r.get("ttft_s") for r in ok], 0.5), "ttft_p99": q([r.get("ttft_s") for r in ok], 0.99),
                                "ended": ended}
            lines.append(f"| {cfg} | {tenant} | {fmt(span, '.1f')} | {len(rs)} | {len(rs) - len(ok)} | "
                         f"{fmt(out[cfg][tenant]['ttft_p50'])} / {fmt(out[cfg][tenant]['ttft_p99'])} | {ended} |")
        if rows:
            t0, t1 = rows[0]["ts"], max(r["ts"] + (r.get("e2e_s") or 0) for r in rows)
            reps = replica_series(states, t0, t1)
            split: dict[str, int] = {}
            for r in rows:
                split[r.get("replica") or "?"] = split.get(r.get("replica") or "?", 0) + 1
            ok = [r for r in rows if r.get("status") == 200]
            cached = sum(r.get("cached_tokens") or 0 for r in ok) / max(1, sum(r.get("prompt_tokens") or 0 for r in ok))
            out[cfg]["_fleet"] = {"makespan_min": (t1 - t0) / 60, "calls": len(rows), "split": split, "replicas": reps, "cached_share": cached}
            fleet.append(f"| {cfg} | {(t1 - t0) / 60:.1f} | {len(rows)} | {', '.join(f'{k}: {n}' for k, n in sorted(split.items()))} | "
                         + ", ".join(f"{r}: {v['running_mean']:.1f} / {v['waiting_mean']:.1f} ({v['waiting_peak']:.0f})" for r, v in sorted(reps.items()))
                         + " | " + ", ".join(f"{r}: {fmt(v['prefix_hit_rate'], pct=True)}" for r, v in sorted(reps.items()))
                         + f" | {cached:.0%} |")
    lines += fleet + ["", "Mode `route` has no admission, so agent calls are never refused; the SLA labels of the 9B's "
                      "thinking subagents were set for 10-K readers, so judge agents by session time and TTFT, not goodput. "
                      "Report quality: results/analysis/quality.md (bash experiments/collect.sh).", ""]
    return lines, out


def compare_table(part: str, by_variant: dict[str, list[dict]]) -> list[str]:
    """One row per variant (each ran one fixed step), side by side."""
    out = ["### Side by side", ""]
    if part in ("routing", "spare", "saturated"):
        out += ["| policy | calls/s | goodput/s | TTFT p50 / p99 (s) | prompt from cache | same document, same replica | split (a / b) | placement reasons |",
                "|---|---:|---:|---|---:|---:|---|---|"]
    else:
        out += ["| queue bound | calls/s | goodput/s | attainment | refused | gateway queue p95 (s) | TTFT p50 / p99 (s) | vLLM waiting mean (peak) |",
                "|---|---:|---:|---:|---|---:|---|---|"]
    for name, steps in by_variant.items():
        s = steps[-1]
        split = " / ".join(str(n) for _, n in sorted(s["split"].items()))
        if part in ("routing", "spare", "saturated"):
            why = ", ".join(f"{k} {n}" for k, n in sorted(s["route_reasons"].items(), key=lambda x: -x[1]))
            out.append(f"| {name} | {s['calls_s']:.2f} | {s['goodput_s']:.2f} | {fmt(s['ttft_p50'])} / {fmt(s['ttft_p99'])} | "
                       f"{fmt(s['cached_share'], pct=True)} | {fmt(s['doc_stickiness'], pct=True)} | {split} | {why} |")
        else:
            refused = ", ".join(f"{k}: {n}" for k, n in sorted(s["refused"].items())) or "0"
            wait = ", ".join(f"{fmt(v['waiting_mean'], '.1f')} ({v['waiting_peak']:.0f})" for _, v in sorted(s["replicas"].items()))
            out.append(f"| {name} | {s['calls_s']:.2f} | {s['goodput_s']:.2f} | {fmt(s['attainment'], pct=True)} | {refused} | "
                       f"{fmt(s['gw_queue_p95'])} | {fmt(s['ttft_p50'])} / {fmt(s['ttft_p99'])} | {wait} |")
    return out + [""]


def kill_part(run: Path, results: Path, part: str) -> tuple[list[str], dict]:
    """A replica deleted under load and restarted: per 10 s, calls served per replica and calls that failed;
    how long until the first failure stopped and until the returning replica served again; its TTFT once back."""
    d = run / part / "round_robin"
    if not (d / "meta.json").exists() or not (d / "kill.json").exists():
        return [], {}
    meta, k = json.load(open(d / "meta.json")), json.load(open(d / "kill.json"))
    rows, _ = load_gateway(results, meta["start"] - 5, meta["end"] + 5)
    rows = [r for r in rows if r.get("variant") == meta["variant_id"] and r.get("status") != 499]
    if not rows:
        return [f"## {part}: no gateway rows (run experiments/collect.sh first)", ""], {}
    t0, killed, ready = rows[0]["ts"], k["killed"], k["ready"]
    fails = [r for r in rows if r.get("status") != 200]
    back = [r for r in rows if r.get("status") == 200 and r.get("replica") == k["replica"] and r["ts"] > killed]
    first_back = back[0]["ts"] if back else None
    later = [r for r in rows if r.get("status") == 200 and r.get("replica") == k["replica"] and r["ts"] < killed]
    out = {"calls": len(rows), "failed": len(fails), "failed_by": dict(Counter(f"{r['status']} {r.get('shed_reason') or ''}".strip() for r in fails)),
           "down_s": ready - killed, "first_served_after_kill_s": (first_back - killed) if first_back else None,
           "ttft_first_back": back[0].get("ttft_s") if back else None, "ttft_back_first10_p50": q([r.get("ttft_s") for r in back[:10]], 0.5),
           "ttft_before_p50": q([r.get("ttft_s") for r in later], 0.5)}
    title = {"kill_before": "Extra D · a replica killed under load (before the fix)", "kill_after": "Extra E · the same, after the fix (route only to live replicas)"}[part]
    lines = [f"## {title}", "",
             f"qwen-9b-b deleted {killed - t0:.0f} s into the load, Ready again after **{ready - killed:.0f} s**; "
             f"**{len(fails)} of {len(rows)} calls failed** ({', '.join(f'{n} × {c}' for c, n in out['failed_by'].items()) or 'none'}); "
             f"qwen-9b-b served again {fmt(out['first_served_after_kill_s'], '.0f')} s after the kill; TTFT of its first call back "
             f"{fmt(out['ttft_first_back'])} s (cold prefix cache), p50 of its first 10 {fmt(out['ttft_back_first10_p50'])} s, "
             f"vs {fmt(out['ttft_before_p50'])} s before the kill.", "",
             "| t (s) | served by qwen-9b-a | served by qwen-9b-b | failed |", "|---:|---:|---:|---:|"]
    end = max(r["ts"] for r in rows)
    b = t0
    while b < end:
        rs = [r for r in rows if b <= r["ts"] < b + 10]
        mark = " ← killed" if b <= killed < b + 10 else " ← Ready" if b <= ready < b + 10 else ""
        lines.append(f"| {b - t0:.0f}{mark} | {sum(r.get('status') == 200 and r.get('replica') == 'qwen-9b-a' for r in rs)} | "
                     f"{sum(r.get('status') == 200 and r.get('replica') == 'qwen-9b-b' for r in rs)} | {sum(r.get('status') != 200 for r in rs)} |")
        b += 10
    return lines + [""], out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", type=Path, help="results/exp4/<run>")
    ap.add_argument("--results", type=Path, default=ROOT / "results")
    a = ap.parse_args()
    loc_lines, loc = [], {}
    for part in PARTS:
        part_lines, loc[part] = locust_part(a.run, a.results, part)
        loc_lines += part_lines
    for part in ("kill_before", "kill_after"):
        part_lines, loc[part] = kill_part(a.run, a.results, part)
        loc_lines += part_lines
    ag_lines, ag = agents_part(a.run, a.results)
    if not loc_lines and not ag_lines:
        print(f"nothing to analyse in {a.run}", file=sys.stderr)
        return 1
    text = "\n".join([f"# Exp-4 scale-out: {a.run.name}", ""] + loc_lines + ag_lines)
    (a.run / "report.md").write_text(text)
    (a.run / "report.json").write_text(json.dumps({"locust": loc, "agents": ag}, indent=2, default=str))
    print(text)
    print(f"\n→ {a.run / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
