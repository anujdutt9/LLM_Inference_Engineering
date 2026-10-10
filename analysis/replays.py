"""Cut the gateway's event logs into one replay file per experiment, for the pipeline UI.

The gateway writes every event to results/.../gateway/events.jsonl, one folder per gateway
start (collect.sh copies them to results/box/). This merges them in time order and writes:

    results/replays/<task>.jsonl              one per tinyharness task label (TH_TASK: exp1a, exp1b, exp2-full-a, ...)
    results/replays/exp3-<group>-<variant>.jsonl   one per Exp-3 sweep variant
    results/replays/all.jsonl                 everything
    results/replays/index.md                  what each file holds

Each file starts with the gateway config in force (mode, thresholds, SLA classes, context
sizes), so a replay explains its refusals exactly like the live page did.

    python -m analysis.replays results            # (collect.sh runs it)
    bash experiments/replay.sh results/replays/exp1a.jsonl
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PAD_S = 15.0  # seconds kept before the first and after the last call of a chapter


def load(results: Path) -> list[dict]:
    seen, out = set(), []
    for path in sorted(results.rglob("gateway/events.jsonl")):
        if "replays" in path.parts:
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict) or "t" not in ev or "kind" not in ev:
                continue
            key = (ev["t"], ev["kind"], ev.get("req_id", ""))
            if key not in seen:
                seen.add(key)
                out.append(ev)
    out.sort(key=lambda e: e["t"])
    return out


GAP_S = 300.0  # a label idle this long starts a new chapter (e.g. a rerun of exp1a)


def _spans(times: list[float]) -> list[tuple[float, float]]:
    spans: list[list[float]] = []
    for t in times:
        if spans and t - spans[-1][1] <= GAP_S:
            spans[-1][1] = t
        else:
            spans.append([t, t])
    return [(a, b) for a, b in spans]


def chapters(events: list[dict]) -> list[dict]:
    """(name, start, end) for each task label and each non-default variant; a label that goes
    idle for GAP_S and comes back is a separate chapter (name-2, name-3, ...)."""
    tasks: dict[str, list[float]] = {}
    variants: dict[str, list[float]] = {}
    for ev in events:
        if ev["kind"] not in ("received", "done"):
            continue
        if ev.get("task"):
            tasks.setdefault(ev["task"], []).append(ev["t"])
        v = ev.get("variant")
        if v and v != "default":
            variants.setdefault(v, []).append(ev["t"])
    out = []
    for name, times in tasks.items():
        for i, (a, b) in enumerate(_spans(times)):
            out.append({"name": name if i == 0 else f"{name}-{i + 1}", "kind": "task", "start": a, "end": b})
    for v, times in variants.items():
        for i, (a, b) in enumerate(_spans(times)):
            base = "exp3-" + v.replace("/", "-")
            out.append({"name": base if i == 0 else f"{base}-{i + 1}", "kind": "variant", "variant": v, "start": a, "end": b})
    return sorted(out, key=lambda c: c["start"])


def cut(events: list[dict], start: float, end: float) -> list[dict]:
    """The events in [start, end], preceded by the config and mode in force at `start`."""
    lo, hi = start - PAD_S, end + PAD_S
    prior: dict[str, dict] = {}
    body = []
    for ev in events:
        if ev["t"] < lo:
            if ev["kind"] in ("config", "mode", "run_start"):
                prior[ev["kind"]] = ev
        elif ev["t"] <= hi:
            body.append(ev)
        else:
            break
    head = [{**ev, "t": lo - 0.001} for ev in (prior.get("run_start"), prior.get("config"), prior.get("mode")) if ev]
    return head + body


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "chapter"


def write(results: Path) -> list[dict]:
    events = load(results)
    out_dir = results / "replays"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    if not events:
        (out_dir / "index.md").write_text("# Replays\n\nNo gateway events found under results/ (run experiments/collect.sh).\n")
        return rows

    def dump(name: str, evs: list[dict], what: str) -> None:
        path = out_dir / f"{safe(name)}.jsonl"
        path.write_text("".join(json.dumps(e, default=str) + "\n" for e in evs))
        calls = sum(1 for e in evs if e["kind"] == "received")
        tenants = sorted({e.get("tenant", "") for e in evs if e["kind"] == "received" and e.get("tenant")})
        rows.append({"file": path.name, "what": what, "calls": calls, "tenants": tenants,
                     "minutes": round((evs[-1]["t"] - evs[0]["t"]) / 60, 1) if evs else 0, "mb": round(path.stat().st_size / 1e6, 1)})

    for ch in chapters(events):
        what = f"task {ch['name']}" if ch["kind"] == "task" else f"Exp-3 variant {ch['variant']}"
        dump(ch["name"], cut(events, ch["start"], ch["end"]), what)
    dump("all", events, "everything, every gateway start merged")
    lines = ["# Replays", "", "Open one in the pipeline UI: `bash experiments/replay.sh results/replays/<file>`, "
             "or drag the file onto the page (several files merge). In the replay bar, **Jump to…** lists each task and variant.", "",
             "| file | what | calls | tenants | minutes | MB |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{r['file']}` | {r['what']} | {r['calls']} | {', '.join(r['tenants'][:4])}{' …' if len(r['tenants']) > 4 else ''} | {r['minutes']} | {r['mb']} |")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return rows


if __name__ == "__main__":
    write(Path(sys.argv[1] if len(sys.argv) > 1 else "results"))
