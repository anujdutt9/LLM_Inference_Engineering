"""Split everything the gateway recorded into sessions and experiments, with no labels needed.

    python -m analysis.sessions results

- A tinyharness terminal is one session (X-Session-ID). Its name: TH_TASK if you set it, else
  the start of your first prompt (from the tinyharness transcript).
- Sessions that overlapped in time form a parallel group (Exp-2); one alone is solo (Exp-1).
- Locust traffic is cut by each Exp-3 ramp's window (results/exp3/*/meta.json), per mode.

For each session and group: calls, agents, sheds by reason, tokens and the prefill:decode
ratio per model, TTFT / TTFAT / ITL / e2e / queue-wait percentiles per model and role, tool
calls and local tool time, the gateway modes seen, and a Grafana link for its time window.
Writes results/analysis/summary.md, summary.json, and per entry summary.json + calls.csv.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

GRAFANA = os.environ.get("GRAFANA", "http://127.0.0.1:31495")


def pct(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (the dataset's rule)."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return round(v[min(len(v) - 1, int(len(v) * p))], 4)


def load_calls(results: Path) -> list[dict]:
    seen: set[str] = set()
    calls: list[dict] = []
    for path in sorted(results.rglob("gateway/requests.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["req_id"] in seen:
                continue
            seen.add(row["req_id"])
            calls.append(row)
    calls.sort(key=lambda r: r["ts"])
    return calls


def first_prompts(results: Path) -> dict[str, str]:
    """session id → the first thing you typed in that tinyharness session."""
    out: dict[str, str] = {}
    for path in results.glob("harness/sessions/*.jsonl"):
        for line in path.open():
            ev = json.loads(line)
            if ev.get("event") == "user":
                out[path.stem] = " ".join(str(ev.get("text", "")).split())[:70]
                break
    return out


def summarize(calls: list[dict]) -> dict[str, Any]:
    start = min(c["ts"] for c in calls)
    end = max(c["ts"] + (c.get("e2e_s") or 0) for c in calls)
    ok = [c for c in calls if c["status"] == 200]
    by_model: dict[str, Any] = {}
    for model in sorted({c["pool"] or c["model_requested"] for c in calls}):
        mc = [c for c in ok if (c["pool"] or c["model_requested"]) == model]
        prompt = sum(c.get("prompt_tokens") or 0 for c in mc)
        cached = sum(c.get("cached_tokens") or 0 for c in mc)
        completion = sum(c.get("completion_tokens") or 0 for c in mc)
        by_model[model] = {
            "calls": len(mc),
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "completion_tokens": completion,
            "reasoning_tokens_est": sum(c.get("reasoning_tokens_est") or 0 for c in mc),
            "prefill_decode_ratio": round(prompt / completion, 2) if completion else None,
            "uncached_prefill_decode_ratio": round((prompt - cached) / completion, 2) if completion else None,
            "cache_hit_share": round(cached / prompt, 3) if prompt else None,
            **{f"{k}_p{int(p * 100)}": pct([c.get(k) for c in mc], p)
               for k in ("ttft_s", "ttfat_s", "reasoning_s", "e2e_s", "queue_wait_s", "itl_p50_s")
               for p in (0.5, 0.95, 0.99)},
        }
    by_role: dict[str, Any] = {}
    for role in sorted({c["role"] for c in calls}):
        rc = [c for c in ok if c["role"] == role]
        by_role[role] = {"calls": len(rc), "ttft_p50": pct([c.get("ttft_s") for c in rc], 0.5),
                         "ttft_p99": pct([c.get("ttft_s") for c in rc], 0.99),
                         "e2e_p50": pct([c.get("e2e_s") for c in rc], 0.5)}
    tools = Counter(t for c in calls for t in c.get("tool_calls") or [])
    agents = {c["agent"]: c.get("agent_name") or c["role"] for c in calls}
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "duration_s": round(end - start, 1),
        "calls": len(calls),
        "ok": len(ok),
        # every non-200 by the layer that decided it (gateway/codes.py), e.g. "admission 429 tenant_tokens"
        "shed": dict(Counter(_rejection(c) for c in calls if c["status"] not in (200, 499))),
        "cancelled": sum(1 for c in calls if c["status"] == 499),
        "spilled": sum(1 for c in calls if c.get("spilled")),
        "agents": len(agents),
        "agent_types": dict(Counter(agents.values())),
        "tenants": sorted({c["tenant"] for c in calls}),
        "modes": sorted({c.get("mode", "") for c in calls}),
        "models": by_model,
        "roles": by_role,
        "tool_calls": dict(tools),
        "tool_gap_p50_s": pct([c.get("tool_gap_s") for c in calls], 0.5),
        "grafana": f"{GRAFANA}/d/demo-exp2?from={int(start * 1000)}&to={int(end * 1000) + 60_000}",
    }


def sweep_metrics(calls: list[dict], cfg: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """What a sweep variant is judged on: SLO attainment against its own SLA classes,
    goodput, per-class latency, fairness across tenants, and where calls landed."""
    classes = cfg.get("sla_classes", {})
    calls = [c for c in calls if c["status"] != 499]  # cut off when Locust stopped: not a gateway decision
    n = len(calls) or 1
    duration = max(1.0, meta["end"] - meta["start"])

    def met(c: dict, key: str, target_key: str) -> bool:
        target = (classes.get(c["sla"]) or {}).get(target_key)
        return c["status"] == 200 and c.get(key) is not None and (target is None or c[key] <= target)

    ok_ttft = [c for c in calls if met(c, "ttft_s", "ttft_s")]
    ok_both = [c for c in ok_ttft if met(c, "ttfat_s", "ttfat_s")]
    by_tenant: dict[str, list[float]] = defaultdict(list)
    for c in calls:
        if c.get("ttft_s") is not None:
            by_tenant[c["tenant"]].append(c["ttft_s"])
    means = [sum(v) / len(v) for v in by_tenant.values() if v]
    jain = round(sum(means) ** 2 / (len(means) * sum(x * x for x in means)), 3) if means and any(means) else None
    replicas = Counter(c["replica"] for c in calls if c.get("replica"))
    split_pools = {c["pool"] for c in calls if sum(1 for r in replicas if r.startswith(f"{c['pool']}-")) > 1}
    out: dict[str, Any] = {
        "shed_share": sum(1 for c in calls if c["status"] != 200) / n,
        "slo_ttft": len(ok_ttft) / n,
        "slo_ttfat": sum(1 for c in calls if met(c, "ttfat_s", "ttfat_s")) / n,
        "goodput_per_s": round(len(ok_both) / duration, 2),
        "queue_wait_p99": pct([c.get("queue_wait_s") for c in calls], 0.99),
        "jain_tenants": jain,
        "replica_split": " / ".join(f"{r} {v}" for r, v in sorted(replicas.items()) if r.rsplit("-", 1)[0] in split_pools) or "—",
    }
    for sla in ("interactive", "batch"):
        sc = [c for c in calls if c["sla"] == sla and c["status"] == 200]
        out[f"ttft_p50_{sla}"] = pct([c.get("ttft_s") for c in sc], 0.5)
        out[f"ttft_p99_{sla}"] = pct([c.get("ttft_s") for c in sc], 0.99)
    return out


def _rejection(c: dict) -> str:
    from gateway.codes import outcome

    layer = c.get("layer") or outcome(c.get("shed_reason") or "", c["status"]).layer
    return f"{layer} {c['status']} {c.get('shed_reason') or ''}".strip()


def write_entry(dest: Path, calls: list[dict], summary: dict[str, Any]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "summary.json").write_text(json.dumps(summary, indent=2))
    keys = [k for k in calls[0] if not isinstance(calls[0][k], dict)]
    with (dest / "calls.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for c in calls:
            w.writerow({k: (";".join(c[k]) if isinstance(c.get(k), list) else c.get(k)) for k in keys})


def slug(text: str) -> str:
    s = "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")
    return "-".join(filter(None, s.split("-")))[:40] or "session"


def main(results: Path) -> None:
    calls = load_calls(results)
    if not calls:
        print(f"no gateway records under {results}/**/gateway/requests.jsonl")
        return
    out = results / "analysis"
    prompts = first_prompts(results)
    by_session: dict[str, list[dict]] = defaultdict(list)
    for c in calls:
        by_session[c["session"]].append(c)

    harness = {s: cs for s, cs in by_session.items() if cs[0]["tenant"].startswith("th-")}
    entries: list[dict[str, Any]] = []
    for sid, cs in harness.items():
        task = next((c["task"] for c in cs if c.get("task")), "") or prompts.get(sid, "")
        s = summarize(cs)
        s.update(session=sid, tenant=cs[0]["tenant"], task=task)
        entries.append({"session": sid, "calls": cs, "summary": s})

    # overlapping harness sessions → one parallel group (Exp-2); alone → solo (Exp-1)
    entries.sort(key=lambda e: e["summary"]["start"])
    groups: list[list[dict]] = []
    for e in entries:
        if groups and e["summary"]["start"] <= max(x["summary"]["end"] for x in groups[-1]):
            groups[-1].append(e)
        else:
            groups.append([e])

    lines = ["# Sessions and experiments", "", f"{len(calls)} calls recorded. Percentiles are nearest-rank.", ""]
    report: dict[str, Any] = {"groups": [], "locust": []}
    for i, group in enumerate(groups, 1):
        kind = "parallel" if len(group) > 1 else "solo"
        gcalls = sorted((c for e in group for c in e["calls"]), key=lambda c: c["ts"])
        gsum = summarize(gcalls)
        gname = f"{i:02d}-{kind}"
        gdir = out / "groups" / gname
        write_entry(gdir, gcalls, gsum)
        lines += [f"## {gname}: {len(group)} session(s), {gsum['duration_s'] / 60:.1f} min, mode {', '.join(gsum['modes'])}",
                  "", f"[Grafana for this window]({gsum['grafana']})", "",
                  "| session | task | calls | agents | sheds | 27B TTFT p50/p99 (s) | 9B TTFT p50/p99 (s) | prefill:decode 27B / 9B | duration |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for e in group:
            s = e["summary"]
            m27, m9 = s["models"].get("qwen-27b", {}), s["models"].get("qwen-9b", {})
            write_entry(gdir / slug(f"{s['tenant']}-{s['task']}"), e["calls"], s)
            lines.append(
                f"| {s['tenant']} | {s['task'] or '—'} | {s['calls']} | {s['agents']} | {sum(s['shed'].values())} | "
                f"{m27.get('ttft_s_p50')}/{m27.get('ttft_s_p99')} | {m9.get('ttft_s_p50')}/{m9.get('ttft_s_p99')} | "
                f"{m27.get('prefill_decode_ratio')} / {m9.get('prefill_decode_ratio')} | {s['duration_s'] / 60:.1f} min |")
        lines.append("")
        report["groups"].append({"name": gname, "kind": kind, "summary": gsum, "sessions": [e["summary"] for e in group]})

    # SLA targets measured, not guessed: p95 of the solo runs (Exp-1) per role and model
    solo = [c for g in groups if len(g) == 1 for e in g for c in e["calls"] if c["status"] == 200]
    if solo:
        lines += ["## Suggested SLA targets (from the solo runs)", "",
                  "p95 per role and model when each tenant ran alone; suggested target ≈ 1.5 × p95 "
                  "(set them in gateway/config.yaml `sla_classes` before Exp-2).", "",
                  "| role @ model | calls | TTFT p95 | TTFAT p95 | e2e p95 | suggested ttft_s / ttfat_s / deadline_s |",
                  "|---|---|---|---|---|---|"]
        for key in sorted({(c["role"], c["pool"]) for c in solo}):
            rc = [c for c in solo if (c["role"], c["pool"]) == key]
            p95 = [pct([c.get(k) for c in rc], 0.95) for k in ("ttft_s", "ttfat_s", "e2e_s")]
            sug = " / ".join("—" if v is None else f"{max(1, round(v * 1.5))}" for v in p95)
            lines.append(f"| {key[0]} @ {key[1]} | {len(rc)} | {p95[0]} | {p95[1]} | {p95[2]} | {sug} |")
            report.setdefault("sla_suggestions", {})[f"{key[0]}@{key[1]}"] = {"p95": p95, "suggested": sug}
        lines.append("")

    # Locust (Exp-3): one row per sweep variant, one table per group
    locust = [c for c in calls if c["tenant"].startswith("locust-")]
    tables: dict[str, list[str]] = {}
    for mp in sorted(results.glob("exp3/**/meta.json")):
        meta = json.loads(mp.read_text())
        name = f"{meta.get('group', 'modes')}/{meta.get('variant', meta.get('mode'))}"
        lc = [c for c in locust if meta["start"] <= c["ts"] <= meta["end"] and c.get("variant") in (name, None, "default", meta.get("variant"))]
        if not lc:
            continue
        cfg = json.loads((mp.parent / "config.json").read_text())["config"] if (mp.parent / "config.json").is_file() else {}
        s = summarize(lc)
        s.update(sweep_metrics(lc, cfg, meta))
        s.update(group=meta.get("group", "modes"), variant=meta.get("variant", meta.get("mode")), mode=meta.get("mode"),
                 run=str(mp.parent.relative_to(results)), grafana=meta.get("grafana", s["grafana"]))
        write_entry(out / "exp3" / mp.parent.relative_to(results / "exp3"), lc, s)
        report["locust"].append(s)
        rows = tables.setdefault(s["group"], [])
        rows.append(
            f"| {s['variant']} | {s['mode']} | {s['calls']} | {s['shed_share']:.0%} | {s['slo_ttft']:.0%} | {s['slo_ttfat']:.0%} | "
            f"{s['goodput_per_s']} | {s['ttft_p50_interactive']}/{s['ttft_p99_interactive']} | {s['ttft_p50_batch']}/{s['ttft_p99_batch']} | "
            f"{s['queue_wait_p99']} | {s['jain_tenants']} | {s['replica_split']} | {', '.join(f'{k} {v}' for k, v in s['shed'].items()) or '—'} |")
    for group, rows in tables.items():
        lines += [f"## Exp-3 sweep: {group}", "",
                  "SLO = share of all calls (sheds count as misses) whose TTFT / TTFAT met their class's target. "
                  "Goodput = calls/s that met both. Latency p50/p99 in seconds. Jain = fairness of mean TTFT across tenants (1 = equal).",
                  "", "| variant | mode | calls | shed | SLO TTFT | SLO TTFAT | goodput/s | TTFT interactive | TTFT batch | queue wait p99 | Jain | replica split | sheds |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|", *rows, ""]
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text("\n".join(lines))
    (out / "summary.json").write_text(json.dumps(report, indent=2))
    print("\n".join(lines))


def bodies(results: Path, req_id: str) -> dict | None:
    """The full request body and assembled output of one call (for digging into a run)."""
    for path in results.rglob("gateway/bodies.jsonl.gz"):
        with gzip.open(path, "rt") as f:
            for line in f:
                if req_id in line[:80]:
                    row = json.loads(line)
                    if row["req_id"] == req_id:
                        return row
    return None


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "results"))
