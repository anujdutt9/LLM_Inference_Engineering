"""Exp-4: file everything about a run into its own folder, after experiments/collect.sh.

    .venv/bin/python experiments/exp4_export.py results/exp4/<run>

Per variant of every part (<run>/<part>/<variant>/, next to Locust's CSVs and meta.json):
  requests.jsonl   the gateway's row per call of that variant (its own window, its variant id)
  state.jsonl      the gateway's 1 s state of every replica in that window (running, waiting, KV, prefix hits)
  window.md        UTC window, calls by status and replica, Grafana links for that window (box and local replay)
  replay.jsonl     the pipeline UI's replay of that window (bash experiments/replay.sh <this file>)
For the whole run (<run>/):
  snapshot-before/ snapshot-after/   experiments/snapshot.sh records (pod logs, events, nvidia-smi, /metrics)
  prometheus/      the Prometheus TSDB snapshot (Grafana replay: bash observability/local/replay.sh <run>/prometheus)
  replay.jsonl     the UI replay of the whole run, every part in order
  grafana_annotations.json, exp0 checks, and index.md listing every folder
"""

from __future__ import annotations

import glob
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # analysis.replays
BOARDS = [("Exp-4 scale-out", "demo-exp4"), ("Engines (vLLM)", "demo-engines"), ("Pipeline (gateway)", "demo-pipeline"),
          ("GPU", "demo-gpu"), ("Tenants & SLAs", "demo-tenants")]


def rel(path: Path) -> str:
    """A path as typed from the project folder, when it is inside it."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def fmt(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def gateway_records(t0: float, t1: float) -> tuple[list[dict], list[dict]]:
    rows, states = [], []
    for d in sorted(glob.glob(str(ROOT / "results" / "box" / "live" / "*" / "gateway"))):
        req, ev = Path(d) / "requests.jsonl", Path(d) / "events.jsonl"
        if req.exists():
            rows += [r for r in map(json.loads, open(req)) if t0 <= r["ts"] <= t1]
        if ev.exists():
            for line in open(ev):
                if '"kind": "state"' in line:
                    e = json.loads(line)
                    if t0 <= e["t"] <= t1:
                        states.append({"t": e["t"], "mode": e.get("mode"), "variant": e.get("variant"),
                                       "replicas": e.get("replicas"), "lanes": {n: {"depth": l.get("depth")} for n, l in (e.get("lanes") or {}).items()}})
    return sorted(rows, key=lambda r: r["ts"]), sorted(states, key=lambda e: e["t"])


_EVENTS: list[dict] | None = None


def ui_events() -> list[dict]:
    """Every gateway event collect.sh pulled (loaded once)."""
    global _EVENTS
    if _EVENTS is None:
        from analysis.replays import load
        _EVENTS = load(ROOT / "results")
    return _EVENTS


def replay(path: Path, t0: float, t1: float) -> int:
    from analysis.replays import cut
    ev = cut(ui_events(), t0, t1)
    path.write_text("".join(json.dumps(e) + "\n" for e in ev))
    return len(ev)


def export_variant(d: Path) -> str:
    meta = json.load(open(d / "meta.json"))
    t0, t1 = meta["start"] - 5, meta["end"] + (60 if meta.get("part") == "agents" else 5)
    rows, states = gateway_records(t0, t1)
    if meta.get("part") == "agents":
        labels = set(meta["labels"].values())
        rows = [r for r in rows if r.get("task") in labels]
    else:
        rows = [r for r in rows if r.get("variant") == meta["variant_id"]]
    (d / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "state.jsonl").write_text("".join(json.dumps(e) + "\n" for e in states))
    n_ev = replay(d / "replay.jsonl", t0, t1)
    frm, to = int(t0 * 1000), int(t1 * 1000)
    status = Counter(r.get("status") for r in rows)
    split = Counter(r.get("replica") for r in rows if r.get("status") == 200)
    links = "\n".join(f"- {n}: http://127.0.0.1:{{port}}/d/{uid}?from={frm}&to={to}" for n, uid in BOARDS)
    name = f"{meta.get('part')}/{meta.get('variant') or meta.get('config')}"
    (d / "window.md").write_text(f"""# {name}

- **{fmt(meta['start'])} → {fmt(meta['end'])}** ({(meta['end'] - meta['start']) / 60:.1f} min), variant `{meta['variant_id']}`
- {len(rows)} calls, by status {dict(status)}, served per replica {dict(split)}
- {len(states)} gateway state snapshots (1 s): running / waiting / KV / prefix hits per replica in `state.jsonl`
- UI replay ({n_ev} events): `bash experiments/replay.sh {rel(d)}/replay.jsonl`

Grafana on the box (tunnel open):
{links.replace("{port}", "31495")}

Grafana replay on the laptop (`bash observability/local/replay.sh {rel(d.parents[1])}/prometheus`):
{links.replace("{port}", "3000")}
""")
    return f"| {name} | {fmt(meta['start'])[11:19]}–{fmt(meta['end'])[11:19]} | {len(rows)} | {dict(split)} | [{d.relative_to(d.parents[1])}/window.md]({d.relative_to(d.parents[1])}/window.md) |"


def main() -> int:
    run = Path(sys.argv[1]).resolve()
    lines = [f"# {run.name}: everything about this run", "",
             "| part / variant | window (UTC) | calls | per replica | details |", "|---|---|---:|---|---|"]
    # only the parts' folders (the copied Prometheus TSDB has meta.json files of its own), in run order
    metas = [m for part in ("locust", "placement", "modes", "agents", "demo", "routing", "queue_depth", "replicate", "kill_before", "kill_after", "spare", "saturated", "batch16", "memory") for m in glob.glob(str(run / part / "*" / "meta.json"))]
    metas = sorted(metas, key=lambda m: json.load(open(m))["start"])
    for meta in metas:
        lines.append(export_variant(Path(meta).parent))
    if metas:
        first, last = json.load(open(metas[0])), json.load(open(metas[-1]))
        n = replay(run / "replay.jsonl", first["start"] - 5, last["end"] + 60)
        lines += ["", f"UI replay of the whole run ({n} events): `bash experiments/replay.sh {rel(run)}/replay.jsonl`"]
    for side in ("before", "after"):
        src = sorted(glob.glob(str(ROOT / "results" / "runs" / f"{run.name}-{side}" / "*")))
        if src:
            shutil.rmtree(run / f"snapshot-{side}", ignore_errors=True)
            shutil.copytree(src[-1], run / f"snapshot-{side}")
    snaps = sorted(glob.glob(str(ROOT / "results" / "prometheus" / "*")))
    if snaps:
        shutil.rmtree(run / "prometheus", ignore_errors=True)
        shutil.copytree(snaps[-1], run / "prometheus")
    for f in ("grafana_annotations.json",):
        if (ROOT / "results" / f).exists():
            shutil.copy(ROOT / "results" / f, run / f)
    exp0 = sorted(glob.glob(str(ROOT / "results" / "exp0" / "*" / "checks.json")))
    if exp0:
        shutil.copy(exp0[-1], run / "exp0-checks.json")
    lines += ["", "Also here: `report.md` / `report.json` (python -m analysis.scale), `run.log`, `exp0.log`, `collect.log`, "
              "`snapshot-before/`, `snapshot-after/`, `prometheus/` (Grafana replay), `grafana_annotations.json`, `exp0-checks.json`. "
              "Agent sessions: `results/experiments/<label>/` and `results/outputs/<label>/` (experiments/finish.sh)."]
    (run / "index.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:-2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
