#!/usr/bin/env bash
# Laptop-only functional rehearsal. Starts the real gateway with accelerated fake vLLM engines, runs the
# three scale-out variants through the normal Locust driver, and stops everything on exit. This validates
# orchestration and artifact generation; it is not a performance benchmark.
#
#   bash experiments/smoke_local.sh
#   RAMP_STEPS=2,4,8 RAMP_HOLD_S=15 SPEED=10 bash experiments/smoke_local.sh
#
# Output: results/exp4/smoke-local-<timestamp>/locust/<variant>/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "Run 'make setup' first." >&2; exit 1; }

ORCH="${ORCH:-http://127.0.0.1:8080}"
if curl -sf "$ORCH/healthz" >/dev/null 2>&1; then
  echo "$ORCH is already serving. Stop the existing gateway before this self-contained smoke run." >&2
  exit 1
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"
RUN="${SMOKE_RUN:-smoke-local-$STAMP}"
OUT="$ROOT/results/exp4/$RUN"
mkdir -p "$OUT"
fleet_pid=""

cleanup() {
  if [[ -n "$fleet_pid" ]]; then
    kill -TERM "$fleet_pid" >/dev/null 2>&1 || true
    wait "$fleet_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "== starting local fake fleet (speed ${SPEED:-20}x) =="
SPEED="${SPEED:-20}" bash setup/local_fleet.sh > "$OUT/fleet.log" 2>&1 &
fleet_pid=$!
for _ in $(seq 60); do
  curl -sf "$ORCH/healthz" >/dev/null 2>&1 && break
  kill -0 "$fleet_pid" 2>/dev/null || {
    echo "Local fleet stopped during startup; see $OUT/fleet.log" >&2
    tail -20 "$OUT/fleet.log" >&2
    exit 1
  }
  sleep 0.5
done
curl -sf "$ORCH/healthz" >/dev/null || {
  echo "Local fleet did not become healthy; see $OUT/fleet.log" >&2
  tail -20 "$OUT/fleet.log" >&2
  exit 1
}

echo "== running one / round_robin / prefix_p2c =="
EXP4_RUN="$RUN" PART=locust GROUP=scale ONLY=one,round_robin,prefix_p2c \
  RAMP_STEPS="${RAMP_STEPS:-2,4}" RAMP_HOLD_S="${RAMP_HOLD_S:-5}" COOLDOWN_S="${COOLDOWN_S:-1}" \
  bash experiments/exp4_locust.sh
curl -sf "$ORCH/admin/state" > "$OUT/final-state.json"

echo
echo "PASS: local experiment machinery completed."
echo "Artifacts: $OUT/locust/"
echo "These fake-engine timings are not comparable with the H100 results."
