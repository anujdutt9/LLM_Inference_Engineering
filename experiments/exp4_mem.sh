#!/usr/bin/env bash
# Mac, tunnel open. Exp-4 memory-pressure demo (a demonstration with a CHANGED fleet config, not a measurement):
# shrink both replicas' KV cache until memory, not slots, is the limit, then put the document load on it.
#
#   bash experiments/exp4_mem.sh                  (~20 min, two fleet redeploys)
#
#   1. fleet → KV_GIB=${MEM_KV_GIB:-1.25} per replica (~73K tokens; vLLM needs ≥ one full 64K context to start);
#      8 document calls running at once need ~82K: more than fits
#   2. documents (40), 24 users, 120 s, mode route: vLLM preempts (evicts running calls' KV, recomputes later)
#   3. the same in mode full: admission's KV check refuses with 503 kv_pressure above 90%
#   4. the 10-K team (lead + 8 readers, ~10K-token filings: ~80K tokens of KV), headless, mode route, cap 15 min:
#      preemption under real agent work, and whether the team still finishes correctly
#   5. fleet back to KV_GIB=4.5, Exp-0 gate; collect, export, file the agent session, analysis
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
need_idle
export EXP4_RUN="${EXP4_RUN:-exp4mem-$STAMP}"
RUN="results/exp4/$EXP4_RUN"
mkdir -p "$RUN"
exec > >(tee -a "$RUN/run.log") 2>&1
KV="${MEM_KV_GIB:-1.25}"
echo "== $(date -u '+%Y-%m-%d %H:%M:%S') UTC  memory-pressure demo $EXP4_RUN (KV $KV GiB per replica) =="
bash experiments/snapshot.sh "$EXP4_RUN-before" | tail -1

echo "== $(date -u +%H:%M:%S) fleet → KV_GIB=$KV =="
KV_GIB="$KV" MAX_SEQS=8 bash experiments/redeploy.sh fleet | grep -E "KV per replica|GPU KV cache size|Fleet up" || true
for _ in $(seq 1 60); do curl -sf "$ORCH/healthz" >/dev/null && break; sleep 2; done
MIN_KV_9B=60000 MIN_CONC_9B=1 bash experiments/exp0_check.sh > "$RUN/exp0-small-kv.log" 2>&1 || { grep FAIL "$RUN/exp0-small-kv.log" | head; echo "Exp-0 failed on the small cache" >&2; }
tail -1 "$RUN/exp0-small-kv.log"

env PART=memory GROUP=modes ONLY=route,full LOAD_DOCS=40 RAMP_STEPS=24 RAMP_HOLD_S=120 bash experiments/exp4_locust.sh
AGENT_TENANTS=10k AGENT_CONFIGS=prefix_p2c AGENT_PREFIX=exp4mem AGENT_TIMEOUT_MIN="${MEM_AGENT_MIN:-15}" \
  bash experiments/exp4_agents.sh

echo "== $(date -u +%H:%M:%S) fleet → back to KV_GIB=4.5 =="
KV_GIB=4.5 MAX_SEQS=8 bash experiments/redeploy.sh fleet | grep -E "KV per replica|GPU KV cache size|Fleet up" || true
for _ in $(seq 1 60); do curl -sf "$ORCH/healthz" >/dev/null && break; sleep 2; done
if ! bash experiments/exp0_check.sh > "$RUN/exp0-restored.log" 2>&1; then grep FAIL "$RUN/exp0-restored.log" | head; echo "Exp-0 failed after restoring" >&2; exit 1; fi
tail -1 "$RUN/exp0-restored.log"

bash experiments/snapshot.sh "$EXP4_RUN-after" | tail -1
bash experiments/collect.sh > "$RUN/collect.log" 2>&1 || { tail -5 "$RUN/collect.log"; exit 1; }
"$PY" experiments/exp4_export.py "$RUN" | tail -n +3
SKIP_COLLECT=1 SNAP=exp4mem-prefix_p2c bash experiments/finish.sh exp4mem-prefix_p2c-10k 2>&1 | grep -E "calls,|no calls" || true
"$PY" -m analysis.scale "$RUN" | grep -E "^## |^- " || true
echo "== done: $RUN/report.md"
