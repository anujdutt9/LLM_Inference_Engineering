#!/usr/bin/env bash
# Mac, tunnel open. The same Locust load against each variant of a group in experiments/variants.yaml,
# applied live on the gateway, prefix caches emptied before each. Every call goes to the 9B.
#
#   bash experiments/exp4_locust.sh                 # part 1, the scale-out ramp: one / round_robin / prefix_p2c,
#                                                   # users 4 → 8 → 16 → 24 → 32, 45 s each (~4 min per variant)
#   PART=placement GROUP=scale ONLY=round_robin,prefix_p2c LOAD_DOCS=150 RAMP_STEPS=16 RAMP_HOLD_S=180 \
#     bash experiments/exp4_locust.sh               # part 2, placement on N distinct ~10K-token documents
#   PART=modes GROUP=modes RAMP_STEPS=24 RAMP_HOLD_S=90 bash experiments/exp4_locust.sh
#                                                   # part 3, route / queue / full at a fixed overload
#   (experiments/exp4_scale.sh runs all of them with these settings)
#
# Results: results/exp4/<EXP4_RUN>/<PART>/<variant>/ (Locust CSVs and HTML, the gateway config in force,
# meta.json with the window). Analysis after experiments/collect.sh: python -m analysis.scale <run dir>
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
need_idle
export RAMP_STEPS="${RAMP_STEPS:-4,8,16,24,32}"
export RAMP_HOLD_S="${RAMP_HOLD_S:-45}"
COOLDOWN_S="${COOLDOWN_S:-20}"
RUN="results/exp4/${EXP4_RUN:-exp4-$STAMP}"
GROUP="${GROUP:-scale}"
PART="${PART:-locust}"
VARIANTS="${ONLY:-$("$PY" -c 'import sys,yaml; print(",".join(yaml.safe_load(open("experiments/variants.yaml"))[sys.argv[1]]))' "$GROUP")}"
trap restore_default EXIT

for name in ${VARIANTS//,/ }; do
  out="$RUN/$PART/$name"
  mkdir -p "$out"
  echo "== $(date -u +%H:%M:%S) $PART / $name (users ${RAMP_STEPS}, ${RAMP_HOLD_S}s each${LOAD_DOCS:+, $LOAD_DOCS distinct documents}) =="
  cold_start
  apply_variant "$PART/$name" "$GROUP/$name"
  curl -sf "$ORCH/admin/config" > "$out/config.json"
  annotate "exp4 $PART / $name" "exp4"
  start=$(date +%s)
  "$ROOT/.venv/bin/locust" -f app/locustfile.py,app/locust_ramp.py NovaUser --headless --host "$ORCH" \
    --csv "$out/locust" --csv-full-history --html "$out/locust.html" --only-summary > "$out/locust.log" 2>&1 || true
  end=$(date +%s)
  grep -E "^(POST|TTFT|E2E) " "$out/locust.log" | head -6 || true
  cat > "$out/meta.json" <<JSON
{"exp": "exp4", "part": "$PART", "group": "$GROUP", "variant": "$name", "variant_id": "$PART/$name", "start": $start, "end": $end,
 "ramp_steps": "$RAMP_STEPS", "ramp_hold_s": $RAMP_HOLD_S, "load_docs": ${LOAD_DOCS:-0},
 "grafana": "$GRAFANA/d/demo-exp4?from=$((start * 1000))&to=$((end * 1000))"}
JSON
  sleep "$COOLDOWN_S"
done
echo "== $(date -u +%H:%M:%S) $PART done: $RUN/$PART/"
