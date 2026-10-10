#!/usr/bin/env bash
# Mac, tunnel open. Exp-4 head-to-head (predictions written before it runs), ~45 min:
#
#   bash experiments/exp4_h2h.sh
#
#   H2 spare      documents (PLACEMENT_DOCS), 12 users, 240 s each, route: round_robin vs prefix_p2c_v2
#   H3 saturated  documents, 24 users, 240 s each, route: prefix_p2c vs prefix_p2c_v2
#   H1 batch16    the fleet redeployed with 16 slots per replica (MAX_SEQS=16, KV_GIB=4.5), Exp-0 gate, Part 1's
#                 ramp (4 → 32 users, 45 s each) on ONE replica with 16 slots (variant one16); then the fleet back to
#                 8 slots and the Exp-0 gate again. Compared with Part 1 / extra C's round_robin (two replicas × 8).
#   then collect.sh, exp4_export.py, analysis: <run>/report.md
#
#   PARTS="H2 H3 H1" (default; any subset)   EXP4_RUN=<name> (default exp4h2h-<stamp>)   PLACEMENT_DOCS=40
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
need_idle
export EXP4_RUN="${EXP4_RUN:-exp4h2h-$STAMP}"
RUN="results/exp4/$EXP4_RUN"
mkdir -p "$RUN"
exec > >(tee -a "$RUN/run.log") 2>&1
PARTS=" ${PARTS:-H2 H3 H1} "
DOCS="${PLACEMENT_DOCS:-40}"
echo "== $(date -u '+%Y-%m-%d %H:%M:%S') UTC  Exp-4 head-to-head $EXP4_RUN (parts:$PARTS) =="

gate() {
  if ! bash experiments/exp0_check.sh > "$RUN/exp0-$1.log" 2>&1; then
    grep -E "FAIL" "$RUN/exp0-$1.log" | head -10
    echo "Exp-0 failed ($RUN/exp0-$1.log): stopping." >&2
    exit 1
  fi
  echo "== Exp-0 ($1): $(tail -1 "$RUN/exp0-$1.log" | cut -c1-40)"
}
gate start
bash experiments/snapshot.sh "$EXP4_RUN-before" | tail -1

if [[ "$PARTS" == *" H2 "* ]]; then
  env PART=spare GROUP=h2h ONLY=round_robin,prefix_p2c_v2 LOAD_DOCS="$DOCS" RAMP_STEPS=12 RAMP_HOLD_S=240 bash experiments/exp4_locust.sh
fi
if [[ "$PARTS" == *" H3 "* ]]; then
  env PART=saturated GROUP=h2h ONLY=prefix_p2c,prefix_p2c_v2 LOAD_DOCS="$DOCS" RAMP_STEPS=24 RAMP_HOLD_S=240 bash experiments/exp4_locust.sh
fi
if [[ "$PARTS" == *" H1 "* ]]; then
  echo "== $(date -u +%H:%M:%S) fleet → 16 slots per replica (MAX_SEQS=16, KV_GIB=4.5) =="
  KV_GIB=4.5 MAX_SEQS=16 bash experiments/redeploy.sh fleet | grep -E "slots per replica|GPU KV cache size|Fleet up" || true
  for _ in $(seq 1 60); do curl -sf "$ORCH/healthz" >/dev/null && break; sleep 2; done
  MIN_CONC_9B=2 gate slots16
  env -u LOAD_DOCS PART=batch16 GROUP=h2h ONLY=one16 RAMP_STEPS=4,8,16,24,32 RAMP_HOLD_S=45 bash experiments/exp4_locust.sh
  echo "== $(date -u +%H:%M:%S) fleet → back to 8 slots per replica =="
  KV_GIB=4.5 MAX_SEQS=8 bash experiments/redeploy.sh fleet | grep -E "slots per replica|GPU KV cache size|Fleet up" || true
  for _ in $(seq 1 60); do curl -sf "$ORCH/healthz" >/dev/null && break; sleep 2; done
  gate end
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
