#!/usr/bin/env bash
# Mac, after an experiment (tunnel open). Pull everything about it into one folder:
#   results/experiments/<name>/
#     window.md            UTC start / end, calls, tenants; Grafana links for that range (box and local replay)
#     requests.jsonl       the gateway's row per call (task <label>; one tenant if given)
#     replay.jsonl         the pipeline UI replay of it (bash experiments/replay.sh <this file>)
#     snapshot-before/ snapshot-after/   experiments/snapshot.sh records (logs, events, nvidia-smi, /metrics)
#     prometheus/          the Prometheus TSDB snapshot (Grafana replay: observability/local/replay.sh <dir>)
#     grafana_annotations.json, analysis/summary.md
# and the agents' outputs into results/outputs/<name>/ (experiments/save_outputs.sh).
#   bash experiments/finish.sh exp1a                  # label = name
#   bash experiments/finish.sh exp1a exp1a th-1234abcd   # name, and only this tenant's calls
#   SKIP_COLLECT=1 SNAP=exp4-one bash experiments/finish.sh exp4-one-sql   # the caller already ran collect.sh
#     and took the snapshots results/runs/<SNAP>-before/-after (default SNAP: the name; taken here)
set -euo pipefail
source "$(dirname "$0")/_common.sh"
label="${1:?usage: finish.sh <label> [name] [tenant]}"
name="${2:-$label}"
tenant="${3:-}"
out="results/experiments/$name"
mkdir -p "$out"

snap="${SNAP:-$name}"
if [[ -z "${SKIP_COLLECT:-}" ]]; then
  bash experiments/snapshot.sh "$snap-after" | tail -1
  bash experiments/collect.sh > "$out/collect.log" 2>&1 || { tail -20 "$out/collect.log"; exit 1; }
fi

for side in before after; do
  src=$(ls -d results/runs/"$snap-$side"/*/ 2>/dev/null | tail -1 || true)
  [[ -n "$src" ]] && { rm -rf "$out/snapshot-$side"; cp -R "${src%/}" "$out/snapshot-$side"; }
done
snap=$(ls -d results/prometheus/*/ | tail -1)
rm -rf "$out/prometheus"; cp -cR "${snap%/}" "$out/prometheus" 2>/dev/null || cp -R "${snap%/}" "$out/prometheus"
cp results/grafana_annotations.json "$out/" 2>/dev/null || true
mkdir -p "$out/analysis"; cp results/analysis/summary.md "$out/analysis/" 2>/dev/null || true

"$PY" - "$label" "$tenant" "$out" <<'EOF'
import glob, json, sys, time, urllib.parse
from pathlib import Path
label, tenant, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
rows = []
for f in sorted(glob.glob("results/box/live/*/gateway/requests.jsonl")):
    rows += [json.loads(l) for l in open(f) if f'"{label}"' in l]
rows = [r for r in rows if r.get("task") == label and (not tenant or r["tenant"] == tenant)]
if not rows:
    sys.exit(f"no calls with task {label}" + (f" and tenant {tenant}" if tenant else ""))
rows.sort(key=lambda r: r["ts"])
(out / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
t0 = rows[0]["ts"]; t1 = max(r["ts"] + (r.get("e2e_s") or 0) for r in rows)
# the UI replay: analysis.replays' cut of the window (config and mode in force first, state snapshots),
# keeping only these calls' events (other tenants' calls, e.g. a test call, are dropped)
sys.path.insert(0, ".")
from analysis.replays import cut, load
ids, tenants = {r["req_id"] for r in rows}, {r["tenant"] for r in rows}
def keep(e: dict) -> bool:
    if "req_id" in e:
        return e["req_id"] in ids
    return e.get("tenant") in tenants if "tenant" in e else True
ev = [e for e in cut(load(Path("results")), t0, t1) if keep(e)]
(out / "replay.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev))
fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))
frm, to = int((t0 - 60) * 1000), int((t1 + 60) * 1000)
links = "\n".join(f"- {n}: http://127.0.0.1:{{port}}/d/{uid}?from={frm}&to={to}" for n, uid in
                  [("Exp-1 solo", "demo-exp1"), ("Exp-2 multi-tenant", "demo-exp2"), ("Exp-3 load", "demo-exp3"),
                   ("Engines (vLLM)", "demo-engines"), ("GPU", "demo-gpu"), ("Pipeline (gateway)", "demo-pipeline"),
                   ("Tenants & SLAs", "demo-tenants")])
by = {}
for r in rows:
    by[r["status"]] = by.get(r["status"], 0) + 1
(out / "window.md").write_text(f"""# {out.name}

- task `{label}`{f", tenant `{tenant}`" if tenant else ""}; tenants: {", ".join(sorted({r["tenant"] for r in rows}))}
- **{fmt(t0)} → {fmt(t1)}** ({(t1 - t0) / 60:.1f} min), {len(rows)} calls, statuses {by}

Grafana on the box (tunnel open, port 31495):
{links.replace("{port}", "31495")}

Grafana replay on the laptop (`bash observability/local/replay.sh {out}/prometheus`, port 3000):
{links.replace("{port}", "3000")}

UI replay: `bash experiments/replay.sh {out}/replay.jsonl`
""")
print(f"{len(rows)} calls, {fmt(t0)} → {fmt(t1)}; {len(ev)} replay events")
EOF
bash experiments/save_outputs.sh "$label" "$name" "$tenant" | tail -3 || echo "(no agent outputs for $label)"
echo "→ $out ($(du -sh "$out" | cut -f1)):"; ls "$out"
