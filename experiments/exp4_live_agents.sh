#!/usr/bin/env bash
# Mac, tunnel open. The three agent teams run by a person in three tinyharness terminals (to watch and record), with
# the gateway set up and the run filed like the scripted ones. Two steps:
#
#   bash experiments/exp4_live_agents.sh prepare exp4live3     # gateway: placement prefix_p2c, mode route, both
#                                                               # replicas, cold caches; snapshot <prefix>-before;
#                                                               # prints the three commands to run
#   ... run the three teams (auto mode; stop SQL at ~30 min if it runs on) ...
#   bash experiments/exp4_live_agents.sh finish exp4live3      # snapshot <prefix>-after, collect, file each session
#                                                               # (finish.sh), results/exp4/<prefix>/, grade the code
#
# Keep the mode at route (under admission the 9B's thinking subagents are refused by a class meant for readers).
# As run on 2026-10-04: prefix exp4live3 (exp4live / exp4live2 were stopped attempts).
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
step="${1:?usage: exp4_live_agents.sh prepare|finish <prefix>}"
prefix="${2:?usage: exp4_live_agents.sh prepare|finish <prefix>}"
labels=("$prefix-sql" "$prefix-10k" "$prefix-code")

case "$step" in
  prepare)
    need_idle
    for l in "${labels[@]}"; do
      [[ -z "$(ls -A "workspace/reports/$l" 2>/dev/null)" ]] || { echo "workspace/reports/$l already has a run: pick another prefix" >&2; exit 1; }
    done
    cold_start
    apply_variant "agents-live/$prefix" scale/prefix_p2c
    bash experiments/snapshot.sh "$prefix-before" | tail -1
    cat <<MSG

Gateway ready (variant agents-live/$prefix: prefix_p2c, route, both replicas, cold caches). In three terminals:
  cd "$ROOT/workspace"
  ./th sql  ${labels[0]}  --model gw:qwen-9b
  ./th 10k8 ${labels[1]}  --model gw:qwen-9b
  ./th code ${labels[2]} --model gw:qwen-9b
Auto mode (Shift+Tab), paste each printed task, Enter in all three. Then: bash experiments/exp4_live_agents.sh finish $prefix
MSG
    ;;
  finish)
    bash experiments/snapshot.sh "$prefix-after" | tail -1
    bash experiments/collect.sh > /dev/null 2>&1 || { echo "collect.sh failed" >&2; exit 1; }
    for l in "${labels[@]}"; do
      SKIP_COLLECT=1 SNAP="$prefix" bash experiments/finish.sh "$l" 2>&1 | grep -E "calls,|no calls" || true
    done
    "$PY" - "$prefix" <<'PY'
import glob, json, sys
from pathlib import Path
prefix = sys.argv[1]
labels = {"sql": f"{prefix}-sql", "10k": f"{prefix}-10k", "code": f"{prefix}-code"}
ts = []
for f in glob.glob("results/box/live/*/gateway/requests.jsonl"):
    for line in open(f):
        if prefix in line:
            r = json.loads(line)
            if r.get("task") in labels.values():
                ts += [r["ts"], r["ts"] + (r.get("e2e_s") or 0)]
if not ts:
    sys.exit(f"no calls labelled {prefix}-*")
d = Path(f"results/exp4/{prefix}/agents/prefix_p2c")
d.mkdir(parents=True, exist_ok=True)
json.dump({"exp": "exp4", "part": "agents", "config": "prefix_p2c", "variant_id": f"agents-live/{prefix}",
           "start": int(min(ts)), "end": int(max(ts)) + 1, "labels": labels,
           "ended": {k: "run by a person (interactive)" for k in labels}}, open(d / "meta.json", "w"), indent=1)
PY
    "$PY" experiments/exp4_export.py "results/exp4/$prefix" | tail -n +3
    "$PY" -m analysis.grade_code "${labels[2]}" | sed -n 3p
    echo "== done: results/exp4/$prefix/index.md   report quality: results/analysis/quality.md"
    ;;
  *)
    sed -n '2,14p' "$0"; exit 1 ;;
esac
