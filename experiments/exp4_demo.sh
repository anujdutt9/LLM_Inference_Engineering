#!/usr/bin/env bash
# Mac, tunnel open. Exp-4 demo, recorded live (not part of the measurement): the placement workload (40 distinct
# ~10K-token documents, 24 users, 240 s) with placement prefix_p2c in mode full (admission + gateway queue, both
# replicas, overflow off), then collected and filed like every other run. Run after a main run as its companion:
#
#   EXP4_RUN=exp4-20261004-011154-demo bash experiments/exp4_demo.sh      (as run on 2026-10-04)
#   bash experiments/exp4_demo.sh                                          (a new folder exp4demo-<stamp>)
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
need_idle
export EXP4_RUN="${EXP4_RUN:-exp4demo-$STAMP}"
RUN="results/exp4/$EXP4_RUN"
mkdir -p "$RUN"
bash experiments/snapshot.sh "$EXP4_RUN-before" | tail -1
PART=demo GROUP=modes ONLY=full LOAD_DOCS="${PLACEMENT_DOCS:-40}" RAMP_STEPS=24 RAMP_HOLD_S=240 bash experiments/exp4_locust.sh
bash experiments/snapshot.sh "$EXP4_RUN-after" | tail -1
bash experiments/collect.sh > "$RUN/collect.log" 2>&1
"$PY" experiments/exp4_export.py "$RUN" | tail -n +3
"$PY" -m analysis.scale "$RUN" | grep -E "^## |^- " || true
echo "== done: $RUN/index.md"
