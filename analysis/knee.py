"""The knee: how the 27B behaves as the same agent workload is run N times at once.

    .venv/bin/python -m analysis.knee                       # every point found, table to stdout
    .venv/bin/python -m analysis.knee --chart results/analysis/knee.png

A point is a set of runs collected by experiments/finish.sh (results/experiments/<name>/): N = 1 is the
solo run (exp1c-v2), N > 1 the sweep's sessions (exp2c-n<N>-<mode>-<i>). Per point, all its sessions together:

  makespan        first call to last call's end
  session time    each session's own span (mean, max): how much slower each tenant got
  throughput      27B output tokens per second over the makespan; tasks per hour (N / makespan)
  goodput         calls within their SLA class's targets, per minute
  within target   share that met TTFT, first answer and total targets: among answers served (what the UI shows),
                  and among all attempts, where a refusal counts as a miss (the fair comparison across modes)
  refused         non-200 answers (admission's 429/503); overflow: calls sent to the external API
  latency         TTFT p50 / p95 (in baseline it includes waiting inside vLLM), first answer p95
  decode          median per-call token gap (ITL): the cost of sharing a batch
  vLLM            running and waiting on the 27B, mean and peak, from the gateway's 1 s state events
  gateway queue   time held in the gateway queue, p95 (0 in baseline: the queue is off)

The knee is where throughput stops rising with N while latency and the share past target jump.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "results" / "experiments"
TARGETS = {  # gateway/config.yaml sla_classes (c6d7d63): ttft, first answer, deadline
    "agent-lead": (4, 80, 120),
    "agent-sub": (4, 25, 60),
    "agent-reader": (3, 3, 35),
    "agent-compaction": (4, 4, 150),
    "interactive": (2, 3, 30),
    "batch": (30, 60, 600),
}


def q(xs: list[float], p: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def within(r: dict) -> bool:
    t = TARGETS.get(r.get("sla") or "")
    if not t:
        return True
    return all(v is None or v <= g for v, g in zip((r.get("ttft_s"), r.get("ttfat_s"), r.get("e2e_s")), t))


def points() -> dict[tuple[int, str], list[str]]:
    """(N, mode) -> the run folders that make up that point."""
    out: dict[tuple[int, str], list[str]] = {}
    if (EXP / "exp1c-v2").is_dir():
        out[(1, "solo")] = ["exp1c-v2"]
    for d in sorted(glob.glob(str(EXP / "exp2c-n*-*-*"))):
        m = re.match(r"exp2c-n(\d+)-(\w+)-\d+$", Path(d).name)
        if m:
            out.setdefault((int(m.group(1)), m.group(2)), []).append(Path(d).name)
    return out


def vllm_counts(names: list[str], t0: float, t1: float) -> tuple[list[float], list[float]]:
    """27B running and waiting per state event in [t0, t1] (one series: sessions share the events)."""
    seen: dict[float, tuple[float, float]] = {}
    for name in names:
        for line in open(EXP / name / "replay.jsonl"):
            if '"kind": "state"' not in line:
                continue
            e = json.loads(line)
            if not t0 <= e["t"] <= t1:
                continue
            for r in e.get("replicas") or []:
                if r.get("pool") == "qwen-27b":
                    seen[e["t"]] = (r.get("running") or 0.0, r.get("waiting") or 0.0)
    return [v[0] for v in seen.values()], [v[1] for v in seen.values()]


def measure(names: list[str]) -> dict:
    rows = [json.loads(line) for n in names for line in open(EXP / n / "requests.jsonl")]
    rows = [r for r in rows if r.get("model_served") == "qwen-27b"]
    t0 = min(r["ts"] for r in rows)
    t1 = max(r["ts"] + (r.get("e2e_s") or 0) for r in rows)
    span = t1 - t0
    sessions = []
    for n in names:
        rs = [json.loads(line) for line in open(EXP / n / "requests.jsonl")]
        sessions.append((max(r["ts"] + (r.get("e2e_s") or 0) for r in rs) - min(r["ts"] for r in rs)) / 60)
    ok = [r for r in rows if r["status"] == 200]
    good = [r for r in ok if within(r)]
    running, waiting = vllm_counts(names, t0, t1)
    return {
        "sessions": len(names),
        "calls": len(rows),
        "makespan_min": span / 60,
        "session_mean_min": statistics.mean(sessions),
        "session_max_min": max(sessions),
        "tok_s": sum(r.get("completion_tokens") or 0 for r in ok) / span,
        "tasks_h": len(names) / (span / 3600),
        "goodput_min": len(good) / (span / 60),
        "within": len(good) / max(1, len(ok)),  # among answers served (refusals excluded: flatters admission)
        "within_all": len(good) / max(1, len(rows)),  # among all attempts: a refusal counts as a miss
        "refused": sum(r["status"] != 200 for r in rows),
        "refused_pct": sum(r["status"] != 200 for r in rows) / max(1, len(rows)),
        "overflow": sum(1 for r in rows if r.get("via") == "overflow"),
        "ttft_p50": q([r.get("ttft_s") for r in ok], 0.5),
        "ttft_p95": q([r.get("ttft_s") for r in ok], 0.95),
        "ttfat_p95": q([r.get("ttfat_s") for r in ok], 0.95),
        "itl_ms": 1000 * (q([r.get("itl_p50_s") for r in ok], 0.5) or 0),
        "run_mean": statistics.mean(running) if running else None,
        "run_peak": max(running) if running else None,
        "wait_mean": statistics.mean(waiting) if waiting else None,
        "wait_peak": max(waiting) if waiting else None,
        "gw_queue_p95": q([r.get("queue_wait_s") or 0 for r in ok], 0.95),
    }


def table(results: dict[tuple[int, str], dict]) -> str:
    cols = [
        ("N", lambda k, m: k[0]), ("mode", lambda k, m: k[1]), ("calls", lambda k, m: m["calls"]),
        ("makespan", lambda k, m: f"{m['makespan_min']:.1f} min"),
        ("session mean / max", lambda k, m: f"{m['session_mean_min']:.1f} / {m['session_max_min']:.1f} min"),
        ("27B tok/s", lambda k, m: f"{m['tok_s']:.0f}"), ("tasks/h", lambda k, m: f"{m['tasks_h']:.0f}"),
        ("goodput/min", lambda k, m: f"{m['goodput_min']:.1f}"),
        ("within target: served / all attempts", lambda k, m: f"{m['within']:.0%} / {m['within_all']:.0%}"),
        ("refused", lambda k, m: f"{m['refused']} ({m['refused_pct']:.0%})"), ("overflow", lambda k, m: m["overflow"]),
        ("TTFT p50 / p95", lambda k, m: f"{m['ttft_p50']:.1f} / {m['ttft_p95']:.1f} s"),
        ("1st answer p95", lambda k, m: f"{m['ttfat_p95']:.1f} s"), ("token gap", lambda k, m: f"{m['itl_ms']:.0f} ms"),
        ("vLLM running mean / peak", lambda k, m: f"{m['run_mean']:.1f} / {m['run_peak']:.0f}"),
        ("vLLM waiting mean / peak", lambda k, m: f"{m['wait_mean']:.1f} / {m['wait_peak']:.0f}"),
        ("gateway queue p95", lambda k, m: f"{m['gw_queue_p95']:.1f} s"),
    ]
    lines = ["| " + " | ".join(c for c, _ in cols) + " |", "|" + "---|" * len(cols)]
    for k in sorted(results):
        lines.append("| " + " | ".join(str(f(k, results[k])) for _, f in cols) + " |")
    return "\n".join(lines)


def chart(results: dict[tuple[int, str], dict], path: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    modes = sorted({k[1] for k in results if k[1] != "solo"}) or ["baseline"]
    colors = {"baseline": "#c2410c", "full": "#1d4ed8"}
    panels = [
        ("27B output tokens / s", "tok_s", None), ("calls within target (all attempts)", "within_all", "pct"),
        ("TTFT p95 (s)", "ttft_p95", 4.0), ("calls waiting inside vLLM (mean)", "wait_mean", None),
    ]
    fig, axes = plt.subplots(1, 4, figsize=(16, 3.6))
    for ax, (title, key, ref) in zip(axes, panels):
        for mode in modes:
            ks = sorted(k for k in results if k[1] in (mode, "solo"))
            xs, ys = [k[0] for k in ks], [results[k][key] for k in ks]
            ax.plot(xs, ys, marker="o", color=colors.get(mode, "#555"), label=mode)
        if ref == "pct":
            ax.set_ylim(0, 1.05)
            ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        elif ref is not None:
            ax.axhline(ref, color="#888", linestyle="--", linewidth=1)
            ax.text(ax.get_xlim()[0], ref, " target", va="bottom", fontsize=8, color="#666")
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("coding sessions at once (5 agents each; 8 slots)")
        ax.set_xticks(sorted({k[0] for k in results}))
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print(f"chart → {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chart")
    args = ap.parse_args()
    results = {k: measure(v) for k, v in points().items()}
    print(table(results))
    if args.chart:
        chart(results, args.chart)


if __name__ == "__main__":
    main()
