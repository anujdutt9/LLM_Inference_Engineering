#!/usr/bin/env bash
# Mac, tunnel open. Exp-0, the gate before any experiment (experiments/exp0_check.py): fails
# unless the models answer as themselves (tool calls, reasoning, thinking off, prefix cache and
# its reset), the gateway has the exact tokenizer, each engine sees its HAMi slice and its KV
# cache / max concurrency clear the thresholds, and tinyharness is the demo build.
#   bash experiments/exp0_check.sh
#   MIN_KV_9B=250000 bash experiments/exp0_check.sh     # thresholds (each replica): MIN_KV_9B, MIN_CONC_9B
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
exec "$PY" experiments/exp0_check.py --orch "$ORCH" "$@"
