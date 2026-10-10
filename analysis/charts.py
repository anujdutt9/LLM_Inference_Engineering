"""Charts for the Exp-4 results, computed from the recorded runs.

    .venv/bin/python -m analysis.charts                 # all charts → docs/presentation/charts/*.svg + *.png
    .venv/bin/python -m analysis.charts memory kill     # only some

Sources: results/exp4/<run>/report.json, per-call requests.jsonl, 1 s state.jsonl, pd_probe.json. The memory chart
also reads the gateway's learned prefill rate from Prometheus (the laptop replay of the final snapshot,
`bash observability/local/replay.sh results/prometheus/20261004T061215Z-51ca83e3b6e9ef8e`).
"""

from __future__ import annotations

import json
import statistics as st
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "results" / "exp4"
OUT = ROOT / "docs" / "presentation" / "charts"
PROM = "http://127.0.0.1:9090/api/v1"

MAIN, MORE, H2H = "exp4-20261004-011154", "exp4more-20261004-022522", "exp4h2h-20261004-042503"
MEM, LIVE, DEMO = "exp4mem-20261004-055441", "exp4live3", "exp4-20261004-011154-demo"
PD_NIXL, PD_MOON = "exp4pd-20261004-051357", "exp4pd-mooncake-20261004-054254"
USERS = [4, 8, 16, 24, 32]
NAME = {"one": "1 engine × 8 slots", "round_robin": "2 engines, round robin", "prefix_p2c": "2 engines, cache-aware v1",
        "one16": "1 engine × 16 slots", "prefix_p2c_v2": "cache-aware v2", "least_loaded": "least loaded",
        "p2c": "power of two choices"}

# one palette for the whole deck
C = {"one": "#6b7280", "round_robin": "#2563eb", "prefix_p2c": "#16a34a", "one16": "#d97706",
     "least_loaded": "#7c3aed", "p2c": "#0891b2", "prefix_p2c_v2": "#dc2626",
     "a": "#2563eb", "b": "#ea580c", "route": "#6b7280", "queue": "#2563eb", "full": "#dc2626",
     "sql": "#2563eb", "10k": "#16a34a", "code": "#d97706"}

plt.rcParams.update({"font.family": ["Helvetica Neue", "Arial Unicode MS", "DejaVu Sans"], "font.size": 11, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.25, "figure.dpi": 100,
                     "savefig.bbox": "tight", "legend.frameon": False, "axes.titleweight": "bold",
                     "axes.titlesize": 12})


def load(p: Path) -> list[dict]:
    return [json.loads(line) for line in open(p)]


def report(run: str) -> dict:
    return json.load(open(EXP / run / "report.json"))


def steps(run: str, group: str, variant: str) -> list[dict]:
    j = report(run)
    return j["locust"][group]["steps"][variant]


def utc(t: float) -> datetime:
    return datetime.fromtimestamp(t, tz=timezone.utc)


def save(fig, name: str, source: str) -> None:
    bb = fig.get_tightbbox(fig.canvas.get_renderer())  # below everything, tick labels included
    fig.text(0.0, (bb.y0 - 0.08) / fig.get_figheight(), f"Source: {source}", fontsize=8, color="#6b7280", ha="left",
             va="top", transform=fig.transFigure)
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.svg")
    fig.savefig(OUT / f"{name}.png", dpi=200)
    fig.savefig(OUT / f"{name}.pdf")  # vector, for Keynote
    plt.close(fig)
    print(f"  {name}.svg / .png")


def time_axis(ax, fmt: str = "%H:%M") -> None:
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=timezone.utc))
    ax.set_xlabel("UTC, 2026-10-04")


def per_step(run: str, path: str, fn, by_end: bool = False) -> list:
    """fn(calls in that 45 s step) per ramp step, from requests.jsonl and meta.json's start; a call belongs to the
    step it started in, or (by_end) the step it finished in, so a step's tokens are the ones produced in it."""
    rs = load(EXP / run / path / "requests.jsonl")
    s = json.load(open(EXP / run / path / "meta.json"))["start"]
    at = (lambda r: r["ts"] + (r.get("e2e_s") or 0)) if by_end else (lambda r: r["ts"])
    return [fn([r for r in rs if s + 45 * k <= at(r) < s + 45 * (k + 1)], s + 45 * k) for k in range(len(USERS))]


def itl_ms(rs, _t0) -> float | None:
    x = [r["itl_p50_s"] for r in rs if r["status"] == 200 and r.get("itl_p50_s")]
    return st.median(x) * 1000 if x else None


# ---------------------------------------------------------------- charts

def knee() -> None:
    """Goodput and attainment against users: one, two replicas (both runs), and H1's one engine × 16 slots."""
    series = [("one", MAIN, "locust", "one", "-"), ("one", MORE, "replicate", "one", "--"),
              ("round_robin", MAIN, "locust", "round_robin", "-"), ("round_robin", MORE, "replicate", "round_robin", "--"),
              ("prefix_p2c", MAIN, "locust", "prefix_p2c", "-"), ("prefix_p2c", MORE, "replicate", "prefix_p2c", "--"),
              ("one16", H2H, "batch16", "one16", "-")]
    label = {"one": "1 replica × 8 slots", "round_robin": "2 replicas × 8, round_robin",
             "prefix_p2c": "2 replicas × 8, prefix_p2c", "one16": "1 replica × 16 slots (H1)"}
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.4))
    seen = set()
    for key, run, group, var, ls in series:
        ss = steps(run, group, var)
        lab = label[key] if key not in seen else None
        seen.add(key)
        a1.plot(USERS, [s["goodput_s"] for s in ss], ls, marker="o", color=C[key], label=lab, lw=2)
        a2.plot(USERS, [100 * s["attainment"] for s in ss], ls, marker="o", color=C[key], lw=2)
    a2.axhline(90, color="#111827", lw=1, ls=":")
    a2.text(4.3, 91.5, "sustained ≥ 90%", fontsize=9)
    for a in (a1, a2):
        a.set_xticks(USERS)
        a.set_xlabel("concurrent users")
    a1.set_ylabel("goodput (calls/s meeting targets)")
    a2.set_ylabel("attainment %")
    a1.set_title("Two replicas move the knee; one 16-slot engine raises throughput")
    a2.set_title("Knee (highest load with ≥ 90% on time): 8 → 16 → 24 users")
    a1.legend(loc="upper left", fontsize=9)
    fig.text(0.5, 1.0, "solid: first run · dashed: repeat (extra C)", ha="center", fontsize=9, color="#6b7280")
    save(fig, "01_knee", f"report.json of {MAIN}, {MORE} (C), {H2H} (H1)")


def itl() -> None:
    """Token gap per step: one engine 7 ms at every load; two engines 14–17 ms; one engine × 16 slots 8 ms."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.2))
    for key, run, path in [("one", MAIN, "locust/one"), ("round_robin", MAIN, "locust/round_robin"),
                           ("prefix_p2c", MAIN, "locust/prefix_p2c"), ("one16", H2H, "batch16/one16")]:
        a1.plot(USERS, per_step(run, path, itl_ms), marker="o", color=C[key], label=NAME[key], lw=2)
        tps = per_step(run, path, lambda rs, _t: sum(r.get("completion_tokens") or 0 for r in rs if r["status"] == 200) / 45,
                       by_end=True)
        a2.plot(USERS, tps, marker="o", color=C[key], lw=2)
    a1.set_ylabel("token gap per call, median ITL (ms)")
    a1.set_title("Once both engines are busy,\nmedian token gap rises from ~7 to 14–17 ms")
    a1.legend(fontsize=9)
    a2.set_ylabel("output tokens/s (whole GPU)")
    a2.set_title("One 16-slot engine outscales\ntwo 8-slot engines on one GPU")
    for a in (a1, a2):
        a.set_xticks(USERS)
        a.set_xlabel("concurrent users")
    a1.set_ylim(0, 20)
    save(fig, "02_itl_throughput", "requests.jsonl (itl_p50_s, completion_tokens per 45 s step)")


def _bars(rows: list[tuple[str, str, str, str]], name: str, title: str, source: str,
          colors: list[str] | None = None) -> None:
    """Goodput and cache share for a few (label, run, group, variant) rows, side by side."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2))
    for i, (label, run, g, v) in enumerate(rows):
        st_ = steps(run, g, v)[0]
        col = colors[i] if colors else C[v]
        a1.bar(i, st_["goodput_s"], color=col, width=0.65)
        a1.text(i, st_["goodput_s"] + 0.08, f"{st_['goodput_s']:.2f}", ha="center", fontsize=10)
        a2.bar(i, 100 * st_["cached_share"], color=col, width=0.65)
        a2.text(i, 100 * st_["cached_share"] + 1.5, f"{100 * st_['cached_share']:.0f}%", ha="center", fontsize=10)
    for a in (a1, a2):
        a.set_xticks(range(len(rows)), [x[0] for x in rows], fontsize=9)
        a.grid(axis="x", visible=False)
    a1.set_ylabel("goodput (calls/s on time)")
    a2.set_ylabel("share of prompt tokens from cache")
    a2.set_ylim(0, 100)
    fig.suptitle(title, fontweight="bold")
    fig.tight_layout()
    save(fig, name, source)


def placement() -> None:
    """Three placement comparisons, one chart each: the policy sweep, spare capacity, and the v1 → v2 fix."""
    _bars([("round robin\n(Part 2)", MAIN, "placement", "round_robin"), ("round robin\n(repeat A)", MORE, "routing", "round_robin"),
           ("least loaded", MORE, "routing", "least_loaded"), ("power of two\nchoices", MORE, "routing", "p2c"),
           ("cache-aware v1\n(Part 2)", MAIN, "placement", "prefix_p2c"), ("cache-aware v1\n(repeat A)", MORE, "routing", "prefix_p2c")],
          "03a_placement_sweep", "Policy sweep, 24 users (saturated): load-aware beats round robin by 11–18%",
          f"report.json of {MAIN} (Part 2) and {MORE} (A); 40 documents, mode route")
    _bars([("round robin", H2H, "spare", "round_robin"), ("cache-aware v2", H2H, "spare", "prefix_p2c_v2")],
          "03b_placement_spare", "Spare capacity, 12 users: affinity holds (+42% goodput, 49% → 91% from cache)",
          f"report.json of {H2H} (H2)", colors=["#9ca3af", "#16a34a"])
    _bars([("cache-aware v1", H2H, "saturated", "prefix_p2c"), ("cache-aware v2", H2H, "saturated", "prefix_p2c_v2")],
          "03c_placement_fix", "Saturated, 24 users: the v2 fix still improves reuse (+36% goodput, 47% → 66%)",
          f"report.json of {H2H} (H3)", colors=["#9ca3af", "#16a34a"])


def modes() -> None:
    """Part 3: where overload waits and what admission refuses (interactive class, 2 s TTFT target)."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.2))
    names, att_srv, att_all, p50, refused, served = ["route", "queue", "full"], [], [], [], [], []
    for m in names:
        rs = [r for r in load(EXP / MAIN / "modes" / m / "requests.jsonl") if r["sla"] == "interactive"]
        ok = [r for r in rs if r["status"] == 200]
        met = [r for r in ok if r["ttft_s"] is not None and r["ttft_s"] <= 2]
        att_srv.append(100 * len(met) / len(ok))
        att_all.append(100 * len(met) / len(rs))
        p50.append(st.median([r["ttft_s"] for r in ok if r["ttft_s"] is not None]))
        refused.append(sum(r["status"] in (429, 503) for r in rs))  # 499 (client hung up) shown separately
        served.append(len(ok))
    xs = range(3)
    a1.bar([x - 0.2 for x in xs], att_srv, 0.4, color="#16a34a", label="of served calls")
    a1.bar([x + 0.2 for x in xs], att_all, 0.4, color="#9ca3af", label="of all attempts")
    for x, v, w in zip(xs, att_srv, att_all):
        a1.text(x - 0.2, v + 1, f"{v:.0f}%", ha="center", fontsize=9)
        a1.text(x + 0.2, w + 1, f"{w:.0f}%", ha="center", fontsize=9)
    a1.set_xticks(list(xs), names)
    a1.set_ylabel("interactive calls with TTFT ≤ 2 s")
    a1.set_title("Attainment: full protects who it admits")
    a1.legend(fontsize=9)
    a2.bar([x - 0.2 for x in xs], served, 0.4, color="#2563eb", label="served")
    a2.bar([x + 0.2 for x in xs], refused, 0.4, color="#dc2626", label="refused (429/503)")
    for x, v in zip(xs, p50):
        a2.text(x, max(served) + 4, f"TTFT p50 {v:.2f} s", ha="center", fontsize=9)
    a2.set_xticks(list(xs), names)
    a2.set_ylabel("interactive calls (90 s, 24 users)")
    a2.set_title("…by refusing more than half the interactive callers")
    a2.legend(fontsize=9, loc="center right")
    a2.set_ylim(0, max(served) + 12)
    dis = [sum(r["status"] == 499 for r in load(EXP / MAIN / "modes" / m / "requests.jsonl") if r["sla"] == "interactive")
           for m in names]
    fig.text(0.5, -0.06, "Full: " + f"{served[2]} served, {refused[2]} gateway rejects (429 + 503), {dis[2]} client disconnect. "
             "Client disconnects (499) per mode: " + " / ".join(map(str, dis)), ha="center", fontsize=9, color="#374151")
    save(fig, "04_modes", f"{MAIN}/modes/*/requests.jsonl (interactive SLA class)")


def queue_depth() -> None:
    """Extra B as two aligned dot charts over the categorical bound: refusals, and TTFT p99 (no connecting lines)."""
    bounds = [0, 2, 8, 64]
    ss = [steps(MORE, "queue_depth", f"depth_{b}")[0] for b in bounds]
    fig, (a1, a2, a3) = plt.subplots(1, 3, figsize=(13, 3.8), sharex=True)
    xs = range(len(bounds))
    for a, vals, col, lab, fmt in [(a1, [sum(s_["refused"].values()) for s_ in ss], "#dc2626", "refused calls", "{:.0f}"),
                                   (a2, [s_["ttft_p99"] for s_ in ss], "#d97706", "TTFT p99 (s)", "{:.2f}"),
                                   (a3, [s_["calls_s"] for s_ in ss], "#2563eb", "served calls/s", "{:.2f}")]:
        a.scatter(list(xs), vals, s=110, color=col, zorder=3)
        for x, v in zip(xs, vals):
            a.annotate(fmt.format(v), (x, v), textcoords="offset points", xytext=(0, 10), ha="center", fontsize=10)
        a.set_title(lab, fontsize=11)
        a.set_xticks(list(xs), [f"bound {b}" for b in bounds])
        a.set_ylim(0, max(vals) * 1.3)
    fig.suptitle("A queue trades refusals for waiting; GPU throughput remains unchanged (bound = per model lane)",
                 fontweight="bold")
    fig.tight_layout()
    save(fig, "05_queue_depth", f"{MORE} report.json (mode queue, 24 users, one run per bound)")


def kill() -> None:
    """Extra D / E: failed calls per 5 s around the replica kill, before and after the liveness fix."""
    fig, axes = plt.subplots(2, 1, figsize=(11, 5.4), sharex=False)
    for a, v, kill_t, ready_t, title in [
            (axes[0], "kill_before", "03:06:02", "03:08:14", "D · before the fix: half the calls go to the dead replica"),
            (axes[1], "kill_after", "03:13:36", "03:15:47", "E · after the fix: only the calls in flight at the kill fail")]:
        rs = load(EXP / MORE / v / "round_robin" / "requests.jsonl")
        t0 = min(r["ts"] for r in rs)
        b = lambda t: int((t - t0) // 5)  # noqa: E731
        ok, bad = Counter(b(r["ts"]) for r in rs if r["status"] == 200), Counter(b(r["ts"]) for r in rs if r["status"] in (502, 503))
        n = max(max(ok), max(bad or {0: 0})) + 1
        xs = [utc(t0 + 5 * i) for i in range(n)]
        a.bar(xs, [ok[i] / 5 for i in range(n)], width=5 / 86400, color="#16a34a", label="served /s", align="edge")
        a.bar(xs, [bad[i] / 5 for i in range(n)], width=5 / 86400, bottom=[ok[i] / 5 for i in range(n)], color="#dc2626",
              label="failed /s (502/503)", align="edge")
        day = "2026-10-04 "
        for t, lab in [(kill_t, "pod deleted"), (ready_t, "Ready again")]:
            x = datetime.fromisoformat(day + t).replace(tzinfo=timezone.utc)
            a.axvline(x, color="#111827", ls="--", lw=1)
            a.text(x, a.get_ylim()[1] * 0.92, f" {lab} {t}", fontsize=8)
        nb = sum(r["status"] in (502, 503) for r in rs)
        a.set_title(f"{title} ({nb} failed)", fontsize=11)
        a.set_ylabel("calls/s")
        time_axis(a, "%H:%M:%S")
    axes[0].legend(fontsize=9, loc="lower left")
    fig.tight_layout()
    fig.text(0.0, -0.04, "Calls are grouped by arrival time, so calls already running at the kill can sit left of the line. "
             "Client hang-ups (499: 14 before, 13 after) are not counted as failures.", fontsize=9, color="#374151")
    save(fig, "06_kill", f"{MORE}/kill_*/round_robin/requests.jsonl (round_robin, 16 users)")


def prom_range(q: str, start: float, end: float, step: int = 5) -> list[tuple[float, float]]:
    url = f"{PROM}/query_range?" + urllib.parse.urlencode({"query": q, "start": start, "end": end, "step": step})
    try:
        res = json.load(urllib.request.urlopen(url, timeout=10))["data"]["result"]
    except Exception as e:  # Prometheus replay not running
        print(f"  (no Prometheus at {PROM}: {e}; start observability/local/replay.sh)", file=sys.stderr)
        return []
    pts = {}
    for r in res:  # one series per gateway process; the newest value wins
        for t, v in r["values"]:
            pts[float(t)] = float(v)
    return sorted(pts.items())


def _mem_data():
    parts = ["memory/route", "memory/full", "agents/prefix_p2c"]
    states, reqs = [], []
    for p_ in parts:
        states += load(EXP / MEM / p_ / "state.jsonl")
        reqs += load(EXP / MEM / p_ / "requests.jsonl")
    states.sort(key=lambda s_: s_["t"])
    return states, reqs, states[0]["t"], states[-1]["t"]


def _steps_marks(axes, top) -> None:
    for a in axes:
        for t, lab in [("05:59:15", "route"), ("06:01:37", "full"), ("06:04:12", "10-K team (route)")]:
            x = datetime.fromisoformat("2026-10-04 " + t).replace(tzinfo=timezone.utc)
            a.axvline(x, color="#111827", lw=0.8, ls="--")
            if a is top:
                a.text(x, a.get_ylim()[1] * 1.02, f" {lab}", fontsize=9, fontweight="bold")


def memory() -> None:
    """Memory demo, two charts: (a) what vLLM did, (b) what the gateway's admission did (c14)."""
    states, reqs, t0, t1 = _mem_data()
    xs = [utc(s_["t"]) for s_ in states]
    fig, (a_kv, a_run, a_pre) = plt.subplots(3, 1, figsize=(11, 7), sharex=True)
    for rid, key in [("qwen-9b-a", "a"), ("qwen-9b-b", "b")]:
        rep = [next((r for r in s_["replicas"] if r["id"] == rid), {}) for s_ in states]
        a_kv.plot(xs, [100 * (r.get("kv") or 0) for r in rep], color=C[key], lw=1.3, label=rid)
        a_run.plot(xs, [r.get("running") or 0 for r in rep], color=C[key], lw=1.3, label=f"{rid} running")
        a_run.plot(xs, [r.get("waiting") or 0 for r in rep], color=C[key], lw=1.3, ls=":", label=f"{rid} waiting")
        base = rep[0].get("preemptions") or 0
        a_pre.plot(xs, [(r.get("preemptions") or 0) - base for r in rep], color=C[key], lw=2, label=rid)
    a_kv.set_ylabel("KV cache used %")
    a_kv.set_ylim(0, 105)
    a_run.set_ylabel("calls inside vLLM")
    a_pre.set_ylabel("preemptions\n(cumulative)")
    for a in (a_kv, a_run, a_pre):
        a.legend(fontsize=8, loc="upper right", ncol=2)
    _steps_marks((a_kv, a_run, a_pre), a_kv)
    time_axis(a_pre, "%H:%M")
    a_kv.set_title("KV cache 1.25 GiB per replica: memory, not slots, becomes the limit", pad=20)
    fig.tight_layout()
    save(fig, "07a_memory_engines", f"{MEM} state.jsonl (gateway's 1 s view of each replica)")

    fig, (a_rate, a_st) = plt.subplots(2, 1, figsize=(11, 5.6), sharex=True)
    rate = prom_range("max by (pool) (orch_learned_prefill_tps)", t0 - 60, t1)
    if rate:
        a_rate.plot([utc(t) for t, _ in rate], [v / 1000 for _, v in rate], color="#7c3aed", lw=2)
    a_rate.axhline(10 / 3.0, color="#dc2626", ls=":", lw=1)
    a_rate.text(utc(t0), 10 / 3.0 + 0.6, " below ~3.3K tok/s, a 10K-token document is estimated > 3 s → refused",
                fontsize=8, color="#dc2626")
    a_rate.set_ylabel("learned prefill rate\n(K tokens/s)")
    ok = Counter(int(r["ts"]) // 5 for r in reqs if r["status"] == 200)
    ref = Counter(int(r["ts"]) // 5 for r in reqs if r["status"] == 429)
    ks = range(int(t0) // 5, int(t1) // 5 + 1)
    a_st.bar([utc(k * 5) for k in ks], [ok[k] / 5 for k in ks], width=5 / 86400, color="#16a34a", align="edge", label="served /s")
    a_st.bar([utc(k * 5) for k in ks], [ref[k] / 5 for k in ks], width=5 / 86400, color="#dc2626", align="edge",
             bottom=[ok[k] / 5 for k in ks], label="429 ttft_unmeetable /s")
    a_st.set_ylabel("calls/s")
    a_st.legend(fontsize=8, loc="upper right")
    _steps_marks((a_rate, a_st), a_rate)
    time_axis(a_st, "%H:%M")
    a_rate.set_title("The gateway counted preemption delays as prefill, then refused everything", pad=20)
    fig.tight_layout()
    save(fig, "07b_memory_lockout", f"{MEM} requests.jsonl; Prometheus orch_learned_prefill_tps (5 s)")


def pd() -> None:
    """P/D probes: decode-side TTFT vs recomputing the prompt, per document, NIXL and Mooncake."""
    n, m = json.load(open(EXP / PD_NIXL / "pd_probe.json")), json.load(open(EXP / PD_MOON / "pd_probe.json"))
    docs = [t["filing"].removesuffix(".md") for t in n["trials"]]
    fig, ax = plt.subplots(figsize=(9, 4.2))
    w = 0.27
    for i, (lab, vals, col) in enumerate([
            ("recompute (cold prefill on the decode replica)", [t["baseline_ttft_s"] for t in n["trials"]], "#6b7280"),
            ("P/D, NIXL (UCX over TCP, ~92 MB/s)", [t["pd_decode_ttft_s"] for t in n["trials"]], "#dc2626"),
            ("P/D, Mooncake (TCP, ~1,319 MB/s)", [t["pd_decode_ttft_s"] for t in m["trials"]], "#16a34a")]):
        xs = [x + (i - 1) * w for x in range(len(docs))]
        ax.bar(xs, vals, w, color=col, label=lab)
        for x, v in zip(xs, vals):
            ax.text(x, v + 0.04, f"{v:.2f}", ha="center", fontsize=8)
    ax.set_xticks(range(len(docs)), [f"{d}\n(~{t['pd_prompt_tokens'] / 1000:.1f}K tokens)" for d, t in zip(docs, n["trials"])])
    ax.set_ylabel("time to first token on the decode side (s)")
    ax.set_title("KV transfer succeeded with both; only Mooncake reduced decode-side TTFT")
    nm, mm = sum(t["answers_match"] for t in n["trials"]), sum(t["answers_match"] for t in m["trials"])
    ax.text(0.99, 0.9, f"identical answers: NIXL {nm}/4, Mooncake {mm}/4", transform=ax.transAxes, ha="right", va="top", fontsize=10)
    ax.set_ylim(0, 3.5)
    ax.legend(fontsize=9, loc="upper left", ncol=1)
    ax.grid(axis="x", visible=False)
    save(fig, "08_pd", f"{PD_NIXL}/pd_probe.json, {PD_MOON}/pd_probe.json")


def agents() -> None:
    """In-flight calls per team: Part 4 headless and the user's live run."""
    fig, axes = plt.subplots(2, 1, figsize=(11, 5.6))
    for a, run, pre, title in [(axes[0], MAIN, "exp4-prefix_p2c-", "Part 4 · headless (30-min cap)"),
                               (axes[1], LIVE, "exp4live3-", "exp4live3 · run live by the user, after the fixes")]:
        rs = [r for r in load(EXP / run / "agents" / "prefix_p2c" / "requests.jsonl") if r["status"] == 200]
        t0 = min(r["ts"] for r in rs)
        t1 = max(r["ts"] + r["e2e_s"] for r in rs)
        grid = [t0 + 5 * i for i in range(int((t1 - t0) / 5) + 1)]
        ys = []
        for team in ["sql", "10k", "code"]:
            mine = [r for r in rs if r["task"] == pre + team]
            ys.append([sum(r["ts"] <= t < r["ts"] + r["e2e_s"] for r in mine) for t in grid])
        a.stackplot([utc(t) for t in grid], ys, colors=[C["sql"], C["10k"], C["code"]], labels=["SQL", "10-K", "code"], alpha=0.85)
        a.axhline(16, color="#111827", ls=":", lw=1)
        a.text(utc(t0), 16.4, " 16 slots (2 × 8)", fontsize=8)
        a.set_ylabel("successful calls active")
        a.set_title(f"{title} · {(t1 - t0) / 60:.0f} min", fontsize=11)
        if run == LIVE:  # the brief accidental switch to full mode
            f0 = datetime.fromisoformat("2026-10-04 04:08:15").replace(tzinfo=timezone.utc)
            f1 = datetime.fromisoformat("2026-10-04 04:10:41").replace(tzinfo=timezone.utc)
            a.axvspan(f0, f1, color="#fee2e2", zorder=0)
            a.text(f0, 14.2, " mode full: 47 rejects", fontsize=8, color="#b91c1c")
        a.set_ylim(0, 18)
        time_axis(a)
    axes[0].legend(fontsize=9, loc="upper right", ncol=3, bbox_to_anchor=(1.0, 0.85))
    fig.tight_layout()
    save(fig, "09_agents", "agents/prefix_p2c/requests.jsonl (status-200 calls only, ts → ts + e2e)")


CHARTS = {"knee": knee, "itl": itl, "placement": placement, "modes": modes, "queue_depth": queue_depth,
          "kill": kill, "memory": memory, "pd": pd, "agents": agents}


def main(argv: list[str]) -> int:
    for name in argv or list(CHARTS):
        print(name)
        CHARTS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
