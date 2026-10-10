#!/usr/bin/env bash
# Mac. Switch the gateway mode (the four modes). No restart; takes effect at once.
#   baseline  route by model only; no admission, no gateway queue (vLLM queues internally)
#   route     + prefix-aware placement across the replicas
#   queue     + the gateway queue (EDF with aging, fair across tenants), vLLM priorities
#   full      + admission: tenant token budget, KV / queue / stale-signal shedding,
#             refusing new sessions under pressure, deadline check
#
#   bash experiments/mode.sh full
#   bash experiments/mode.sh          # show the current mode
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway

if [[ $# -eq 0 ]]; then
  curl -sf "$ORCH/admin/state" | "$PY" -c 'import json,sys; s=json.load(sys.stdin); print(s["mode"], s["features"])'
  exit 0
fi
set_mode "$1"
annotate "gateway mode → $1" "mode"
