#!/usr/bin/env bash
# Mac, tunnel open. Exp-4's extra block (predictions written before the run), one command,
# unattended, ~60 min, on the same fleet and the same Exp-0 gate:
#
#   bash experiments/exp4_more.sh
#
#   A routing      the placement workload (PLACEMENT_DOCS distinct ~10K-token documents, 24 users, 240 s each) for
#                  round_robin, least_loaded, p2c, prefix_p2c (mode route): a four-policy table; round_robin and
#                  prefix_p2c also repeat Part 2, so the difference between the two runs is the noise   (~18 min)
#   B queue_depth  Part 1's questions, 24 users, 120 s each, mode queue (admission off), gateway queue bound
#                  0 / 2 / 8 / 64: does a deeper queue add served calls or only waiting?               (~10 min)
#   C replicate    Part 1 again, the whole ramp, one / round_robin / prefix_p2c: run-to-run noise on the knee,
#                  S2 and E2                                                                          (~13 min)
#   D kill_before  round_robin, Part 1's questions, 16 users, 330 s; at 60 s the qwen-9b-b pod is deleted and
#                  Kubernetes restarts it: what callers see while one replica is gone and comes back   (~7 min)
#   (the gateway's fix: route only to replicas answering their metrics; synced and the gateway restarted)
#   E kill_after   the same, with the fix                                                              (~8 min)
#   then collect.sh and exp4_export.py (every variant's calls, state, window, Grafana links, UI replay), the
#   run's snapshots, and the analysis: <run>/report.md
#
#   PARTS="A B C D E" (default; any subset)   EXP4_RUN=<name> (default exp4more-<stamp>)   PLACEMENT_DOCS=40
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
need_idle
export EXP4_RUN="${EXP4_RUN:-exp4more-$STAMP}"
RUN="results/exp4/$EXP4_RUN"
mkdir -p "$RUN"
exec > >(tee -a "$RUN/run.log") 2>&1
PARTS=" ${PARTS:-A B C D E} "
DOCS="${PLACEMENT_DOCS:-40}"
K='export KUBECONFIG=/etc/rancher/k3s/k3s.yaml;'
echo "== $(date -u '+%Y-%m-%d %H:%M:%S') UTC  Exp-4 extra block $EXP4_RUN (parts:$PARTS) =="

echo "== Exp-0 gate =="
if ! bash experiments/exp0_check.sh > "$RUN/exp0.log" 2>&1; then
  grep -E "FAIL" "$RUN/exp0.log" | head -10
  echo "Exp-0 failed ($RUN/exp0.log): stopping." >&2
  exit 1
fi
tail -1 "$RUN/exp0.log"
bash experiments/snapshot.sh "$EXP4_RUN-before" | tail -1

if [[ "$PARTS" == *" A "* ]]; then
  env PART=routing GROUP=routing LOAD_DOCS="$DOCS" RAMP_STEPS=24 RAMP_HOLD_S=240 bash experiments/exp4_locust.sh
fi
if [[ "$PARTS" == *" B "* ]]; then
  env -u LOAD_DOCS PART=queue_depth GROUP=queue_depth RAMP_STEPS=24 RAMP_HOLD_S=120 bash experiments/exp4_locust.sh
fi
if [[ "$PARTS" == *" C "* ]]; then
  env -u LOAD_DOCS PART=replicate GROUP=scale RAMP_STEPS=4,8,16,24,32 RAMP_HOLD_S=45 bash experiments/exp4_locust.sh
fi

kill_test() {  # kill_test <part>: the Locust load in the background, qwen-9b-b's pod deleted 60 s in
  local part="$1" out="$RUN/$1/round_robin"
  env -u LOAD_DOCS PART="$part" GROUP=kill RAMP_STEPS=16 RAMP_HOLD_S=330 bash experiments/exp4_locust.sh &
  local pid=$!
  until [[ -f "$out/config.json" ]]; do sleep 1; done   # the variant is applied and the caches are cold
  sleep 60
  local old; old=$(box "$K kubectl get pod -l app=vllm-9b-b -o jsonpath='{.items[0].metadata.name}'")
  local killed; killed=$(date +%s.%N)
  echo "== $(date -u +%H:%M:%S) $part: deleting the qwen-9b-b pod ($old) =="
  box "$K kubectl delete pod $old --wait=false" | tail -1
  # wait for the REPLACEMENT to be Ready: a label wait also watches the old pod, which vanishes mid-wait
  # (kubectl: NotFound, exit 1), which stopped kill test E's first run
  until box "$K kubectl get pod -l app=vllm-9b-b -o jsonpath='{range .items[*]}{.metadata.name} {.status.conditions[?(@.type==\"Ready\")].status}{\"\\n\"}{end}'" \
      | grep -v "^$old " | grep -q " True$"; do
    sleep 2
  done
  local ready; ready=$(date +%s.%N)
  echo "== $(date -u +%H:%M:%S) $part: qwen-9b-b Ready again after $(printf '%.0f' "$(echo "$ready - $killed" | bc)") s =="
  wait "$pid"
  printf '{"killed": %s, "ready": %s, "replica": "qwen-9b-b"}\n' "$killed" "$ready" > "$out/kill.json"
}
if [[ "$PARTS" == *" D "* ]]; then
  kill_test kill_before
fi
if [[ "$PARTS" == *" E "* ]]; then
  echo "== $(date -u +%H:%M:%S) the fix: gateway routes only to replicas answering their metrics (sync, restart) =="
  bash experiments/redeploy.sh gateway | tail -1
  for _ in $(seq 1 60); do curl -sf "$ORCH/healthz" >/dev/null && break; sleep 2; done
  sleep 5
  need_idle
  kill_test kill_after
fi

bash experiments/snapshot.sh "$EXP4_RUN-after" | tail -1
echo "== $(date -u +%H:%M:%S) collect =="
bash experiments/collect.sh > "$RUN/collect.log" 2>&1 || { tail -5 "$RUN/collect.log"; echo "collect.sh failed; rerun it, then: $PY experiments/exp4_export.py $RUN" >&2; exit 1; }
echo "== $(date -u +%H:%M:%S) file every part's records into $RUN =="
"$PY" experiments/exp4_export.py "$RUN" | tail -n +3
echo "== $(date -u +%H:%M:%S) analysis =="
"$PY" -m analysis.scale "$RUN" | grep -E "^## |^- " || true
echo
echo "== done: $RUN/report.md   index: $RUN/index.md"
