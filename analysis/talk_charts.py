"""Talk versions of the Exp-4 charts, for the v2 deck (docs/presentation/deck_v2.yaml): one claim per chart, labels
sized for a projector, red for bad outcomes, words next to colours, sources in the speaker notes instead of on the chart.

    .venv/bin/python -m analysis.talk_charts           # all → docs/presentation/charts/talk/*.pdf + *.png
    .venv/bin/python -m analysis.talk_charts modes     # only some

The data and helpers are analysis/charts.py's; the memory chart needs the Prometheus replay running (see there).
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import Counter
from datetime import datetime, timezone

import matplotlib.pyplot as plt

from analysis import charts as c

OUT = c.OUT / "talk"
GREY, BLUE, GREEN, AMBER, RED, INK, MUTED = "#9ca3af", "#2563eb", "#16a34a", "#d97706", "#dc2626", "#111827", "#6b7280"

plt.rcParams.update({"font.size": 17, "axes.titlesize": 19, "axes.labelsize": 17, "xtick.labelsize": 16,
                     "ytick.labelsize": 16, "legend.fontsize": 16, "lines.linewidth": 3, "lines.markersize": 9})


def save(fig, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.png", dpi=150)
    fig.savefig(OUT / f"{name}.pdf")
    plt.close(fig)
    print(f"  talk/{name}")


def tokens_per_s(run: str, path: str) -> list[float]:
    return c.per_step(run, path, lambda rs, _t: sum(r.get("completion_tokens") or 0 for r in rs if r["status"] == 200) / 45,
                      by_end=True)


def replicas() -> None:
    """One vs two 8-slot replicas only: the on-time limit doubles, GPU tokens/s doesn't."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.6))
    for key, col, lab in [("one", GREY, "1 replica × 8 slots (baseline)"), ("round_robin", BLUE, "2 replicas × 8 slots")]:
        a1.plot(c.USERS, [100 * s["attainment"] for s in c.steps(c.MAIN, "locust", key)], marker="o", color=col, label=lab)
        a2.plot(c.USERS, tokens_per_s(c.MAIN, f"locust/{key}"), marker="o", color=col, label=lab)
    a1.axhline(90, color=INK, lw=1.5, ls=":")
    a1.text(4.2, 92, "90% on time", fontsize=15)
    for x, col, lab in [(8, GREY, "knee 8"), (16, BLUE, "knee 16")]:
        a1.annotate(lab, (x, 100), xytext=(x, 112), ha="center", color=col, fontweight="bold",
                    arrowprops={"arrowstyle": "->", "color": col})
    a1.set_ylim(50, 120)
    a1.set_yticks([60, 70, 80, 90, 100])
    a1.set_ylabel("calls on time (%)")
    a1.set_title("On-time limit doubled: 8 → 16 users")
    a2.set_ylabel("output tokens/s, whole GPU")
    a2.set_ylim(0, 1300)
    a2.set_title("Token throughput did not grow: ~1,000 at best")
    a2.legend(loc="lower right")
    for a in (a1, a2):
        a.set_xticks(c.USERS)
        a.set_xlabel("concurrent users")
    fig.tight_layout()
    save(fig, "t01_replicas")


def batching() -> None:
    """Two 8-slot replicas vs one 16-slot engine: token gap and goodput."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.6))
    rows = [("round_robin", c.MAIN, "locust", BLUE, "2 replicas × 8 slots"),
            ("one16", c.H2H, "batch16", GREEN, "1 engine × 16 slots")]
    for key, run, g, col, lab in rows:
        a1.plot(c.USERS, c.per_step(run, f"{g}/{key}", c.itl_ms), marker="o", color=col, label=lab)
        gp = [s["goodput_s"] for s in c.steps(run, g, key)]
        a2.plot(c.USERS, gp, marker="o", color=col, label=lab)
        a2.annotate(f"{gp[3]:.2f}", (24, gp[3]), xytext=(-10, 12 if key == "one16" else -30), textcoords="offset points",
                    color=col, fontweight="bold")
    a1.set_ylim(0, 20)
    a1.set_ylabel("ms between tokens (median)")
    a1.set_title("Per-call speed: 8 ms vs 14–17 ms")
    a1.legend(loc="lower right")
    a2.set_ylabel("calls/s on time (goodput)")
    a2.set_ylim(0, 4.6)
    a2.set_title("Goodput at 24 users: 4.02 vs 2.20 (1.8×)")
    for a in (a1, a2):
        a.set_xticks(c.USERS)
        a.set_xlabel("concurrent users")
    fig.tight_layout()
    save(fig, "t02_batching")


def placement() -> None:
    """Two separate comparisons, kept visibly separate: A at 12 users, B at 24 users."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.8), sharey=True)
    for a, head, rows in [
            (axes[0], "A · 12 users, spare slots", [("round robin\n(baseline)", "spare", "round_robin", GREY),
                                                     ("cache-aware v2", "spare", "prefix_p2c_v2", GREEN)]),
            (axes[1], "B · 24 users, both full", [("cache-aware v1\n(baseline)", "saturated", "prefix_p2c", GREY),
                                                   ("cache-aware v2", "saturated", "prefix_p2c_v2", GREEN)])]:
        vals = []
        for i, (lab, g, v, col) in enumerate(rows):
            s = c.steps(c.H2H, g, v)[0]
            vals.append(s["goodput_s"])
            a.bar(i, s["goodput_s"], 0.6, color=col)
            a.text(i, s["goodput_s"] + 0.12, f"{s['goodput_s']:.2f}", ha="center", fontweight="bold", fontsize=19)
            a.text(i, 0.25, f"{100 * s['cached_share']:.0f}%\nfrom cache", ha="center", color="white", fontsize=16,
                   fontweight="bold")
        a.set_xticks([0, 1], [r[0] for r in rows])
        a.set_title(f"{head}: +{100 * (vals[1] / vals[0] - 1):.0f}%")
        a.grid(axis="x", visible=False)
    axes[0].set_ylabel("calls/s on time (goodput)")
    axes[0].set_ylim(0, 7.5)
    fig.tight_layout()
    save(fig, "t03_placement")


def placement_each() -> None:
    """One chart per comparison, for the v3 sections: c10 (A, 12 users) and c11 (B, 24 users)."""
    for name, head, rows in [
            ("t03a_spare", "12 users, spare slots", [("round robin\n(baseline)", "spare", "round_robin", GREY),
                                                     ("cache-aware v2", "spare", "prefix_p2c_v2", GREEN)]),
            ("t03b_saturated", "24 users, both replicas full", [("cache-aware v1\n(baseline)", "saturated", "prefix_p2c", GREY),
                                                                ("cache-aware v2", "saturated", "prefix_p2c_v2", GREEN)])]:
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.8))
        ss = [c.steps(c.H2H, g, v)[0] for _, g, v, _ in rows]
        for a, key, scale, fmt, ylab in [(a1, "goodput_s", 1, "{:.2f}", "calls/s on time (goodput)"),
                                         (a2, "cached_share", 100, "{:.0f}%", "prompt tokens from cache (%)")]:
            for i, (s_, (lab, _, _, col)) in enumerate(zip(ss, rows)):
                v = s_[key] * scale
                a.bar(i, v, 0.6, color=col)
                a.text(i, v * 1.02, fmt.format(v), ha="center", va="bottom", fontweight="bold", fontsize=19)
            a.set_xticks([0, 1], [r[0] for r in rows])
            a.set_ylabel(ylab)
            a.grid(axis="x", visible=False)
        a1.set_ylim(0, max(s_["goodput_s"] for s_ in ss) * 1.25)
        a2.set_ylim(0, 105)
        g = 100 * (ss[1]["goodput_s"] / ss[0]["goodput_s"] - 1)
        a1.set_title(f"{head}: goodput +{g:.0f}%")
        a2.set_title(f"from cache {100 * ss[0]['cached_share']:.0f}% → {100 * ss[1]['cached_share']:.0f}%")
        fig.tight_layout()
        save(fig, name)


def modes() -> None:
    """Two questions: if admitted, how fast? Of all attempts, what happened?"""
    names = ["route", "queue", "full"]
    p50, out = [], []
    for m in names:
        rs = [r for r in c.load(c.EXP / c.MAIN / "modes" / m / "requests.jsonl") if r["sla"] == "interactive"]
        ok = [r for r in rs if r["status"] == 200]
        p50.append(st.median([r["ttft_s"] for r in ok if r["ttft_s"] is not None]))
        on = sum(r["ttft_s"] is not None and r["ttft_s"] <= 2 for r in ok)
        out.append({"on time": on, "served late": len(ok) - on, "refused (429/503)": sum(r["status"] in (429, 503) for r in rs),
                    "client gave up (499)": sum(r["status"] == 499 for r in rs)})
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.8), gridspec_kw={"width_ratios": [1, 1.5]})
    a1.bar(range(3), p50, 0.6, color=[GREY, BLUE, GREEN])
    for x, v in enumerate(p50):
        a1.text(x, v - 0.2, f"{v:.2f} s", ha="center", fontweight="bold", fontsize=19, color="white")
    a1.axhline(2, color=INK, ls=":", lw=1.5)
    a1.text(2.45, 2.04, "2 s target", fontsize=15, ha="right")
    a1.set_xticks(range(3), names)
    a1.set_ylim(0, 2.5)
    a1.set_ylabel("first token, median (s)")
    a1.set_title("If admitted: how fast?")
    a1.grid(axis="x", visible=False)
    cols = {"on time": GREEN, "served late": AMBER, "refused (429/503)": RED, "client gave up (499)": GREY}
    for i, o in enumerate(out):
        left = 0
        for k, v in o.items():
            a2.barh(i, v, left=left, color=cols[k], label=k if i == 0 else None)
            if v >= 6:
                a2.text(left + v / 2, i, str(v), ha="center", va="center", color="white", fontweight="bold")
            left += v
        a2.text(left + 1.5, i, f"{100 * o['on time'] / left:.0f}% on time", va="center", fontweight="bold")
    a2.set_yticks(range(3), names)
    a2.invert_yaxis()
    a2.set_xlim(0, 135)
    a2.set_xlabel("interactive calls attempted")
    a2.set_title("Of every call attempted: what happened?")
    a2.legend(loc="lower center", bbox_to_anchor=(0.5, -0.5), ncol=2, fontsize=15)
    a2.grid(axis="y", visible=False)
    fig.tight_layout()
    save(fig, "t04_modes")


def queue_depth() -> None:
    bounds = [0, 2, 8, 64]
    ss = [c.steps(c.MORE, "queue_depth", f"depth_{b}")[0] for b in bounds]
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for a, vals, col, lab, fmt in [(axes[0], [sum(s["refused"].values()) for s in ss], RED, "refused calls (bad)", "{:.0f}"),
                                   (axes[1], [s["ttft_p99"] for s in ss], AMBER, "slowest first token, p99 (s)", "{:.2f}"),
                                   (axes[2], [s["calls_s"] for s in ss], BLUE, "served calls/s (flat)", "{:.2f}")]:
        a.bar(range(4), vals, 0.6, color=col)
        for x, v in enumerate(vals):
            a.text(x, v + max(vals) * 0.03, fmt.format(v), ha="center", fontweight="bold")
        a.set_title(lab)
        a.set_xticks(range(4), [str(b) for b in bounds])
        a.set_xlabel("queue bound")
        a.set_ylim(0, max(vals) * 1.2)
        a.grid(axis="x", visible=False)
    fig.tight_layout()
    save(fig, "t05_queue_depth")


def kill() -> None:
    fig, axes = plt.subplots(2, 1, figsize=(14, 6.4), sharey=True)
    for a, v, kill_t, ready_t, title in [
            (axes[0], "kill_before", "03:06:02", "03:08:14", "Before: half the calls go to the dead replica"),
            (axes[1], "kill_after", "03:13:36", "03:15:47", "After: only the calls already streaming on it fail")]:
        rs = c.load(c.EXP / c.MORE / v / "round_robin" / "requests.jsonl")
        t0 = min(r["ts"] for r in rs)
        b = lambda t: int((t - t0) // 5)  # noqa: E731
        ok = Counter(b(r["ts"]) for r in rs if r["status"] == 200)
        bad = Counter(b(r["ts"]) for r in rs if r["status"] in (502, 503))
        n = max(max(ok), max(bad or {0: 0})) + 1
        xs = [(5 * i - (datetime.fromisoformat("2026-10-04 " + kill_t).replace(tzinfo=timezone.utc).timestamp() - t0))
              for i in range(n)]
        a.bar(xs, [ok[i] / 5 for i in range(n)], width=5, color=GREEN, label="served", align="edge")
        a.bar(xs, [bad[i] / 5 for i in range(n)], width=5, bottom=[ok[i] / 5 for i in range(n)], color=RED,
              label="failed (502/503)", align="edge")
        a.axvline(0, color=INK, ls="--", lw=1.5)
        a.text(2, 3.3, "pod deleted", fontsize=15)
        nb = sum(r["status"] in (502, 503) for r in rs)
        a.set_title(f"{title}:  {nb} failed", color=RED if nb > 20 else GREEN, loc="left")
        a.set_ylabel("calls/s")
        a.set_xlim(-60, 200)
    axes[1].set_xlabel("seconds since the pod was deleted (back ~130 s later)")
    axes[0].legend(loc="upper right", ncol=2)
    fig.tight_layout()
    save(fig, "t06_kill")


def agents_serving() -> None:
    """Part 4 headless: three teams on the 9B, 604 calls served, peak 16 running (every slot)."""
    rs = [r for r in c.load(c.EXP / c.MAIN / "agents" / "prefix_p2c" / "requests.jsonl") if r["status"] == 200]
    t0 = min(r["ts"] for r in rs)
    t1 = max(r["ts"] + r["e2e_s"] for r in rs)
    grid = [t0 + 2 * i for i in range(int((t1 - t0) / 2) + 1)]
    ys = [[sum(r["ts"] <= t < r["ts"] + r["e2e_s"] for r in rs if r["task"] == "exp4-prefix_p2c-" + team) for t in grid]
          for team in ["sql", "10k", "code"]]
    fig, a = plt.subplots(figsize=(14, 5.4))
    a.stackplot([(t - t0) / 60 for t in grid], ys, colors=[c.C["sql"], c.C["10k"], c.C["code"]],
                labels=["SQL team", "10-K team", "code team"], alpha=0.85)
    a.axhline(16, color=INK, ls=":", lw=1.5)
    a.text(0.2, 16.4, "16 slots (2 × 8)", fontsize=15)
    a.set_ylim(0, 18)
    a.set_xlabel("minutes")
    a.set_ylabel("agent calls running")
    a.set_title("Part 4 headless run, same three tasks: calls running per team", loc="left")
    a.legend(loc="upper right", ncol=3)
    fig.tight_layout()
    save(fig, "t07_agents_serving")


def agents_quality() -> None:
    """Report quality with a 27B lead (earlier runs) vs the 9B lead (exp4live3); same tasks and scoring."""
    rows = [("SQL\nfact recall", 64, "16/25", 12, "3/25"), ("10-K\nclaims cited", 100, "82/82", 0, "0 cited"),
            ("code\nbugs + features", 100, "16/16", 87.5, "14/16")]
    fig, a = plt.subplots(figsize=(13, 5.4))
    for i, (lab, v27, l27, v9, l9) in enumerate(rows):
        a.bar(i - 0.2, v27, 0.38, color=GREY, label="27B lead (earlier run, baseline)" if i == 0 else None)
        a.bar(i + 0.2, v9, 0.38, color=RED, label="9B lead (this run)" if i == 0 else None)
        a.text(i - 0.2, v27 + 2, l27, ha="center", fontweight="bold")
        a.text(i + 0.2, v9 + 2, l9, ha="center", fontweight="bold", color=RED)
    a.set_xticks(range(3), [r[0] for r in rows])
    a.set_ylim(0, 125)
    a.set_ylabel("score (%)")
    a.legend(loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.14))
    a.grid(axis="x", visible=False)
    fig.tight_layout()
    save(fig, "t08_agents_quality")


def pd() -> None:
    """Break-even: decode-side first token vs recomputing the prompt."""
    n, m = json.load(open(c.EXP / c.PD_NIXL / "pd_probe.json")), json.load(open(c.EXP / c.PD_MOON / "pd_probe.json"))
    rec = [t["baseline_ttft_s"] for t in n["trials"] + m["trials"]]
    rows = [("NIXL hand-off\n(~92 MB/s)", [t["pd_decode_ttft_s"] for t in n["trials"]], RED),
            ("recompute\n(break-even)", rec, GREY),
            ("Mooncake hand-off\n(~1,319 MB/s)", [t["pd_decode_ttft_s"] for t in m["trials"]], GREEN)]
    fig, a = plt.subplots(figsize=(14, 5))
    for i, (lab, vals, col) in enumerate(rows):
        a.barh(i, st.mean(vals), 0.6, color=col)
        a.scatter(vals, [i] * len(vals), color=INK, s=30, zorder=3)
        a.text(max(vals) + 0.06, i, f"{min(vals):.2f}–{max(vals):.2f} s", va="center", fontweight="bold")
    a.axvline(st.mean(rec), color=INK, ls="--", lw=1.5)
    a.text(st.mean(rec) + 0.04, -0.45, "left of this line: the hand-off beats recomputing", fontsize=15)
    a.set_yticks(range(3), [r[0] for r in rows])
    a.set_xlabel("decode side: time to first token (s), 4 documents of ~10K tokens")
    a.set_xlim(0, 3.1)
    a.set_ylim(2.5, -0.6)
    a.grid(axis="y", visible=False)
    fig.tight_layout()
    save(fig, "t09_pd")


def memory() -> None:
    states, reqs, t0, t1 = c._mem_data()
    fig, (a_rate, a_st) = plt.subplots(2, 1, figsize=(14, 6.6), sharex=True)
    rate = c.prom_range("max by (pool) (orch_learned_prefill_tps)", t0 - 60, t1)
    if rate:
        a_rate.plot([c.utc(t) for t, _ in rate], [v / 1000 for _, v in rate], color="#7c3aed")
    a_rate.axhline(10 / 3.0, color=RED, ls=":", lw=1.5)
    a_rate.text(c.utc(t0), 10 / 3.0 - 2.6, " admission threshold for a 10K document", fontsize=14, color=RED)
    a_rate.set_ylabel("learned prefill\n(K tok/s)")
    ok = Counter(int(r["ts"]) // 5 for r in reqs if r["status"] == 200)
    ref = Counter(int(r["ts"]) // 5 for r in reqs if r["status"] == 429)
    ks = range(int(t0) // 5, int(t1) // 5 + 1)
    a_st.bar([c.utc(k * 5) for k in ks], [ok[k] / 5 for k in ks], width=5 / 86400, color=GREEN, align="edge", label="served")
    a_st.bar([c.utc(k * 5) for k in ks], [ref[k] / 5 for k in ks], width=5 / 86400, color=RED, align="edge",
             bottom=[ok[k] / 5 for k in ks], label="refused (429)")
    a_st.set_ylabel("calls/s")
    a_st.legend(loc="upper left", bbox_to_anchor=(0.0, 1.0))
    for a in (a_rate, a_st):
        for t, lab in [("05:59:15", "route"), ("06:01:37", "full"), ("06:04:12", "10-K team")]:
            x = datetime.fromisoformat("2026-10-04 " + t).replace(tzinfo=timezone.utc)
            a.axvline(x, color=INK, lw=1, ls="--")
            if a is a_rate:
                a.text(x, a.get_ylim()[1] * 1.03, f" {lab}", fontsize=15, fontweight="bold")
    for t, y, s in [("05:59:40", 12, "1–2  preemption delays\ncounted as prefill"),
                    ("06:02:10", 9, "3–5  rate stuck,\nevery call refused"),
                    ("06:04:20", 15, "first served calls:\nrate recovers")]:
        a_rate.text(datetime.fromisoformat("2026-10-04 " + t).replace(tzinfo=timezone.utc), y, s, fontsize=14,
                    color=RED if t.startswith("06:02") else INK)
    c.time_axis(a_st, "%H:%M")
    fig.tight_layout()
    save(fig, "t10_memory")


def batch_wait() -> None:
    """Part 3, batch class (the long tasks, 30 s TTFT target): median and slowest first token per mode."""
    names, med, top = ["route", "queue", "full"], [], []
    for m in names:
        t = [r["ttft_s"] for r in c.load(c.EXP / c.MAIN / "modes" / m / "requests.jsonl")
             if r["sla"] == "batch" and r["status"] == 200 and r["ttft_s"] is not None]
        med.append(st.median(t))
        top.append(max(t))
    fig, a = plt.subplots(figsize=(12, 5.4))
    xs = range(3)
    a.bar([x - 0.2 for x in xs], med, 0.4, color=BLUE, label="median")
    a.bar([x + 0.2 for x in xs], top, 0.4, color=AMBER, label="slowest")
    for x, v, w in zip(xs, med, top):
        a.text(x - 0.2, v + 0.4, f"{v:.2f} s", ha="center", fontweight="bold")
        a.text(x + 0.2, w + 0.4, f"{w:.1f} s", ha="center", fontweight="bold")
    a.axhline(30, color=RED, ls=":", lw=1.5)
    a.text(2.6, 30.6, "30 s target", ha="right", color=RED)
    a.set_xticks(list(xs), names)
    a.set_ylim(0, 34)
    a.set_ylabel("batch calls: first token (s)")
    a.set_title("Batch calls stayed far inside their target: this load could not test aging")
    a.legend(loc="upper left")
    a.grid(axis="x", visible=False)
    fig.tight_layout()
    save(fig, "t11_batch_wait")


def placement_sweep() -> None:
    """Part 2 and its repeat A: goodput and cache share per policy at 24 users (both replicas full)."""
    rows = [("round robin", [(c.MAIN, "placement", "round_robin"), (c.MORE, "routing", "round_robin")], GREY),
            ("least loaded", [(c.MORE, "routing", "least_loaded")], BLUE),
            ("power of\ntwo choices", [(c.MORE, "routing", "p2c")], BLUE),
            ("cache-aware v1", [(c.MAIN, "placement", "prefix_p2c"), (c.MORE, "routing", "prefix_p2c")], GREEN)]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.6))
    for i, (lab, runs, col) in enumerate(rows):
        ss = [c.steps(r, g, v)[0] for r, g, v in runs]
        for a, key, scale, fmt in [(a1, "goodput_s", 1, "{:.2f}"), (a2, "cached_share", 100, "{:.0f}%")]:
            vals = [s_[key] * scale for s_ in ss]
            a.bar(i, sum(vals) / len(vals), 0.6, color=col)
            a.scatter([i] * len(vals), vals, color=INK, s=30, zorder=3)
            a.text(i, max(vals) * 1.03, " / ".join(fmt.format(v) for v in vals), ha="center", fontweight="bold", fontsize=15)
    for a in (a1, a2):
        a.set_xticks(range(len(rows)), [r[0] for r in rows])
        a.grid(axis="x", visible=False)
    a1.set_ylabel("calls/s on time (goodput)")
    a1.set_ylim(0, 5.4)
    a1.set_title("Load-aware placement: +11–18% goodput")
    a2.set_ylabel("prompt tokens from cache (%)")
    a2.set_ylim(0, 100)
    a2.set_title("…but the cache share barely moved")
    fig.tight_layout()
    save(fig, "t12_placement_sweep")


def memory_engines() -> None:
    """The route step under memory pressure: KV, running vs waiting, preemptions (both replicas)."""
    states, _reqs, _t0, _t1 = c._mem_data()
    xs = [c.utc(s_["t"]) for s_ in states]
    fig, (a_kv, a_run, a_pre) = plt.subplots(3, 1, figsize=(14, 8), sharex=True)
    for rid, col in [("qwen-9b-a", BLUE), ("qwen-9b-b", AMBER)]:
        rep = [next((r for r in s_["replicas"] if r["id"] == rid), {}) for s_ in states]
        a_kv.plot(xs, [100 * (r.get("kv") or 0) for r in rep], color=col, lw=2, label=rid)
        a_run.plot(xs, [r.get("running") or 0 for r in rep], color=col, lw=2, label=f"{rid} running")
        a_run.plot(xs, [r.get("waiting") or 0 for r in rep], color=col, lw=2, ls=":", label=f"{rid} waiting")
        base = rep[0].get("preemptions") or 0
        a_pre.plot(xs, [(r.get("preemptions") or 0) - base for r in rep], color=col, lw=3, label=rid)
    a_kv.set_ylabel("KV used (%)")
    a_kv.set_ylim(0, 105)
    a_run.set_ylabel("calls in vLLM")
    a_run.axhline(8, color=INK, ls=":", lw=1)
    a_pre.set_ylabel("preemptions")
    for a in (a_kv, a_run, a_pre):
        a.legend(loc="upper right", ncol=2, fontsize=13)
        for t, lab in [("05:59:15", "route"), ("06:01:37", "full"), ("06:04:12", "10-K team")]:
            x = datetime.fromisoformat("2026-10-04 " + t).replace(tzinfo=timezone.utc)
            a.axvline(x, color=INK, lw=1, ls="--")
            if a is a_kv:
                a.text(x, 108, f" {lab}", fontsize=15, fontweight="bold")
    c.time_axis(a_pre, "%H:%M")
    fig.tight_layout()
    save(fig, "t13_memory_engines")


def agents_live() -> None:
    """The user's live run (exp4live3): calls running per team, with the brief Full window."""
    rs = [r for r in c.load(c.EXP / c.LIVE / "agents" / "prefix_p2c" / "requests.jsonl") if r["status"] == 200]
    t0 = min(r["ts"] for r in rs)
    t1 = max(r["ts"] + r["e2e_s"] for r in rs)
    grid = [t0 + 2 * i for i in range(int((t1 - t0) / 2) + 1)]
    ys = [[sum(r["ts"] <= t < r["ts"] + r["e2e_s"] for r in rs if r["task"] == "exp4live3-" + team) for t in grid]
          for team in ["sql", "10k", "code"]]
    fig, a = plt.subplots(figsize=(14, 5.4))
    a.stackplot([c.utc(t) for t in grid], ys, colors=[c.C["sql"], c.C["10k"], c.C["code"]],
                labels=["SQL team", "10-K team", "code team"], alpha=0.85)
    f0 = datetime.fromisoformat("2026-10-04 04:08:15").replace(tzinfo=timezone.utc)
    f1 = datetime.fromisoformat("2026-10-04 04:10:41").replace(tzinfo=timezone.utc)
    a.axvspan(f0, f1, color="#fee2e2", zorder=0)
    a.text(f0, 18.4, " full mode on by mistake: 47 refused", fontsize=14, color=RED)
    a.axhline(16, color=INK, ls=":", lw=1.5)
    a.set_ylim(0, 20)
    a.set_ylabel("agent calls running")
    a.legend(loc="upper right", ncol=3, bbox_to_anchor=(1.0, 0.93))
    a.set_title("Live run by hand (exp4live3): SQL 9.5 min, 10-K 8.5 min, code 12.6 min", loc="left")
    c.time_axis(a, "%H:%M")
    fig.tight_layout()
    save(fig, "t14_agents_live")


CHARTS = {"replicas": replicas, "batching": batching, "placement": placement, "modes": modes,
          "queue_depth": queue_depth, "kill": kill, "agents_serving": agents_serving, "agents_quality": agents_quality,
          "pd": pd, "memory": memory, "batch_wait": batch_wait, "placement_sweep": placement_sweep,
          "memory_engines": memory_engines, "agents_live": agents_live, "placement_each": placement_each}


def main(argv: list[str]) -> int:
    for name in argv or list(CHARTS):
        CHARTS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
