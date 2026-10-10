#!/usr/bin/env bash
# Mac, tunnel open. Exp-4, the scale-out experiment, one command, unattended,
# on the two-replica 9B fleet (qwen-9b-a, qwen-9b-b). The measurements first, the demonstrations after:
#
#   bash experiments/exp4_scale.sh                                                      (~70 min)
#
#   0. Exp-0 gate (both replicas healthy and answering) and nothing else running on the gateway
#   1. scale      Locust ramp 4 → 8 → 16 → 24 → 32 users, 45 s each, for one / round_robin / prefix_p2c,
#                 mode route: does a second replica move the knee? (~13 min)               PART1=0 skips
#   2. placement  24 users for 240 s, each call one of N distinct ~10K-token documents (N sized from the
#                 replicas' KV so the documents overflow one replica but fit in two halves), round_robin vs
#                 prefix_p2c: does KV-aware placement keep useful cache? (~9 min)          PART2=0 skips
#   3. modes      24 users for 90 s in route / queue / full on both replicas: where overload waits (vLLM vs
#                 the gateway queue) and admission's 429 / 503 (~5 min)                     PART3=0 skips
#   4. agents     the three tenants (sql, 10k8, code) at once under prefix_p2c (≤ 30 min)  PART4=0 skips
#   5. collect.sh, every variant's records filed in its folder (exp4_export.py: requests, state, window and
#      Grafana links; the run's snapshots and Prometheus TSDB), each agent session (finish.sh), <run>/report.md
#
#   AGENT_CONFIGS="one prefix_p2c"   add the one-replica control to part 4      AGENT_TIMEOUT_MIN=30
#   PLACEMENT_DOCS=<n>               part 2's document count (default: 1.5 × one replica's KV / 10K tokens)
#   EXP4_RUN=<name> (default exp4-<stamp>)       SKIP_CHECK=1: skip the Exp-0 gate (only if it just passed)
#   PLACEMENT_USERS=24 PLACEMENT_HOLD_S=240 MODES_USERS=24 MODES_HOLD_S=90     (the defaults; smaller for a smoke run)
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
need_idle
export EXP4_RUN="${EXP4_RUN:-exp4-$STAMP}"
RUN="results/exp4/$EXP4_RUN"
mkdir -p "$RUN"
exec > >(tee -a "$RUN/run.log") 2>&1
echo "== $(date -u '+%Y-%m-%d %H:%M:%S') UTC  Exp-4 run $EXP4_RUN =="

if [[ -z "${SKIP_CHECK:-}" ]]; then
  echo "== Exp-0 gate =="
  if ! bash experiments/exp0_check.sh > "$RUN/exp0.log" 2>&1; then
    grep -E "FAIL" "$RUN/exp0.log" | head -10
    echo "Exp-0 failed ($RUN/exp0.log): fix the fleet first (bash experiments/redeploy.sh fleet)." >&2
    exit 1
  fi
  tail -1 "$RUN/exp0.log"
fi

bash experiments/snapshot.sh "$EXP4_RUN-before" | tail -1

if [[ "${PART1:-1}" != 0 ]]; then
  env -u LOAD_DOCS PART=locust GROUP=scale RAMP_STEPS="${RAMP_STEPS:-4,8,16,24,32}" RAMP_HOLD_S="${RAMP_HOLD_S:-45}" \
    bash experiments/exp4_locust.sh
fi
if [[ "${PART2:-1}" != 0 ]]; then
  DOCS="${PLACEMENT_DOCS:-}"
  if [[ -z "$DOCS" ]]; then  # 1.5 × the smaller replica's KV cache, in ~10K-token documents
    DOCS=$("$PY" - <<'PY'
import glob, json, math
kv = []
for f in sorted(glob.glob("results/exp0/*/checks.json"))[-1:]:
    for name, c in json.load(open(f)).items():
        if "KV cache" in name and isinstance(c["detail"], dict) and c["detail"].get("kv_tokens"):
            kv.append(c["detail"]["kv_tokens"])
print(math.ceil(1.5 * min(kv) / 10_000) if kv else 150)
PY
)
  fi
  echo "== placement: $DOCS distinct documents =="
  PART=placement GROUP=scale ONLY=round_robin,prefix_p2c LOAD_DOCS="$DOCS" RAMP_STEPS="${PLACEMENT_USERS:-24}" RAMP_HOLD_S="${PLACEMENT_HOLD_S:-240}" \
    bash experiments/exp4_locust.sh
fi
if [[ "${PART3:-1}" != 0 ]]; then
  env -u LOAD_DOCS PART=modes GROUP=modes RAMP_STEPS="${MODES_USERS:-24}" RAMP_HOLD_S="${MODES_HOLD_S:-90}" bash experiments/exp4_locust.sh
fi
if [[ "${PART4:-1}" != 0 ]]; then
  bash experiments/exp4_agents.sh
fi
bash experiments/snapshot.sh "$EXP4_RUN-after" | tail -1

echo "== $(date -u +%H:%M:%S) collect =="
bash experiments/collect.sh > "$RUN/collect.log" 2>&1 || { tail -5 "$RUN/collect.log"; echo "collect.sh failed; rerun it, then: $PY -m analysis.scale $RUN" >&2; exit 1; }
echo "== $(date -u +%H:%M:%S) file every part's records into $RUN =="
"$PY" experiments/exp4_export.py "$RUN" | tail -n +3
for meta in "$RUN"/agents/*/meta.json; do
  [[ -f "$meta" ]] || continue
  cfg=$(basename "$(dirname "$meta")")
  for label in $("$PY" -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["labels"].values()))' "$meta"); do
    SKIP_COLLECT=1 SNAP="${AGENT_PREFIX:-exp4}-$cfg" bash experiments/finish.sh "$label" 2>&1 | grep -E "calls,|no calls" || true
  done
done

echo "== $(date -u +%H:%M:%S) analysis =="
"$PY" -m analysis.scale "$RUN" | tail -60
echo
echo "== done: $RUN/report.md   (Grafana: Demo / Exp-4 Scale-out; windows in $RUN/*/*/meta.json)"
echo "When this run is valid, shut the GPU instance down."
