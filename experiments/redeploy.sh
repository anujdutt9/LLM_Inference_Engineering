#!/usr/bin/env bash
# Mac. Redeploy part of the fleet on the GPU box (runs setup/demo_apply.sh there over SSH), after
# syncing the code and gateway/config.yaml to the box.
#   bash experiments/redeploy.sh gateway   # restart only the gateway (~1 min; engines untouched)
#   bash experiments/redeploy.sh fleet     # the fleet deployed last, then the gateway (~10 min)
#   KV_GIB=4.5 bash experiments/redeploy.sh fleet   # Setup B: the same fixed KV cache on both (default: what fits)
#   KV_GIB=4.5 MAX_SEQS=16 bash experiments/redeploy.sh fleet   # Setup B: 16 slots per replica (default 8)
#   FLEET=27b-9b bash experiments/redeploy.sh fleet # switch fleets: 27b-9b (Setup A) or two-9b (Setup B)
set -euo pipefail
source "$(dirname "$0")/_common.sh"

case "${1:-}" in
  gateway)
    bash "$ROOT/setup/sync_to_lambda.sh" >/dev/null
    annotate "gateway restarted (new code/config)" "redeploy"
    box "ONLY=gateway bash setup/demo_apply.sh"
    ;;
  fleet)
    bash "$ROOT/setup/sync_to_lambda.sh" >/dev/null
    annotate "fleet redeployed (${FLEET:-same fleet})" "redeploy"
    box "FLEET=${FLEET:-} KV_GIB=${KV_GIB:-} MAX_SEQS=${MAX_SEQS:-8} bash setup/demo_apply.sh"
    ;;
  *)
    sed -n '2,7p' "$0"
    exit 1
    ;;
esac
