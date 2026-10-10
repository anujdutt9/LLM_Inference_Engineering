#!/usr/bin/env bash
# Mac, tunnel open. Exp-4 part 4: the three real tenants at once on the two-replica fleet, every call on
# the 9B: realistic multi-agent load (long prompts, tool loops, the 10-K readers in pairs sharing a filing's
# prefix), per-tenant progress and whether the reports are still right. A demonstration, not the capacity
# measurement (that is the Locust parts: agent runs differ in length and calls from run to run).
#
#   bash experiments/exp4_agents.sh                                  # config prefix_p2c (the default)
#   AGENT_CONFIGS="one prefix_p2c" bash experiments/exp4_agents.sh   # + the one-replica control (~2× the time)
#   AGENT_TENANTS="10k" AGENT_CONFIGS="round_robin prefix_p2c" ...   # only the 10-K team: placement on agents
#   AGENT_TIMEOUT_MIN=30 (default): a session still running then is stopped and marked "timeout"
#
# Per config: cold prefix caches, the config applied, then three headless tinyharness sessions started
# together (workspace/th, TH_HEADLESS=1: auto mode, a tool call that needs approval is declined):
#   sql   tasks/sql.md   lead + 4 SQL analysts      label <PREFIX>-<config>-sql
#   10k   tasks/10k8.md  lead + 8 10-K readers      label <PREFIX>-<config>-10k
#   code  tasks/code.md  lead + 4 code fixers       label <PREFIX>-<config>-code
# Watch them live in the UI (http://127.0.0.1:8080/ui/) or: tail -f <run>/agents/<config>/<tenant>.log
# Results: results/exp4/<EXP4_RUN>/agents/<config>/ (session logs, meta.json); reports in workspace/reports/<label>/.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
RUN="results/exp4/${EXP4_RUN:-exp4-$STAMP}"
CONFIGS="${AGENT_CONFIGS:-prefix_p2c}"
PREFIX="${AGENT_PREFIX:-exp4}"
TIMEOUT_S=$(( ${AGENT_TIMEOUT_MIN:-30} * 60 ))
COOLDOWN_S="${COOLDOWN_S:-30}"
ALL_TENANTS=(sql:sql 10k:10k8 code:code)  # tenant:task file
TENANTS=()
for tt in "${ALL_TENANTS[@]}"; do
  [[ " ${AGENT_TENANTS:-sql 10k code} " == *" ${tt%%:*} "* ]] && TENANTS+=("$tt")
done
(( ${#TENANTS[@]} > 0 )) || { echo "AGENT_TENANTS: choose from sql 10k code" >&2; exit 1; }

for cfg in $CONFIGS; do
  for tt in "${TENANTS[@]}"; do
    if [[ -n "$(ls -A "workspace/reports/$PREFIX-$cfg-${tt%%:*}" 2>/dev/null)" ]]; then
      echo "workspace/reports/$PREFIX-$cfg-${tt%%:*} already has a run: use another AGENT_PREFIX (e.g. AGENT_PREFIX=${PREFIX}r2)" >&2
      exit 1
    fi
  done
done

PIDS=()
stop_sessions() {
  for p in "${PIDS[@]:-}"; do [[ -n "$p" ]] && kill -INT "$p" 2>/dev/null || true; done
  sleep 5
  for p in "${PIDS[@]:-}"; do [[ -n "$p" ]] && kill -TERM "$p" 2>/dev/null || true; done
}
trap 'stop_sessions; restore_default' EXIT

for cfg in $CONFIGS; do
  need_idle
  out="$RUN/agents/$cfg"
  mkdir -p "$out"
  cold_start
  apply_variant "agents/$cfg" "scale/$cfg"
  curl -sf "$ORCH/admin/config" > "$out/config.json"
  bash experiments/snapshot.sh "$PREFIX-$cfg-before" | tail -1
  annotate "exp4 agents / $cfg" "exp4"
  echo "== $(date -u +%H:%M:%S) agents / $cfg: ${TENANTS[*]} together (cap ${TIMEOUT_S}s) =="
  start=$(date +%s)
  PIDS=()
  labels=()
  for tt in "${TENANTS[@]}"; do
    tenant="${tt%%:*}" task="${tt##*:}" label="$PREFIX-$cfg-${tt%%:*}"
    (cd workspace && TH_HEADLESS=1 exec ./th "$task" "$label" --model gw:qwen-9b) > "$out/$tenant.log" 2>&1 < /dev/null &
    PIDS+=("$!")
    labels+=("\"$tenant\": \"$label\"")
    echo "  $tenant → $label (pid $!)"
  done
  ended=()
  for i in "${!PIDS[@]}"; do ended[$i]=""; done
  while :; do
    running=0
    for i in "${!PIDS[@]}"; do
      [[ -n "${ended[$i]}" ]] && continue
      if kill -0 "${PIDS[$i]}" 2>/dev/null; then
        running=$((running + 1))
      else
        wait "${PIDS[$i]}" && ended[$i]="done after $(( $(date +%s) - start ))s" || ended[$i]="exit $? after $(( $(date +%s) - start ))s"
        echo "  $(date -u +%H:%M:%S) ${TENANTS[$i]%%:*}: ${ended[$i]}"
      fi
    done
    (( running == 0 )) && break
    if (( $(date +%s) - start > TIMEOUT_S )); then
      for i in "${!PIDS[@]}"; do
        if [[ -z "${ended[$i]}" ]]; then
          kill -INT "${PIDS[$i]}" 2>/dev/null || true
          ended[$i]="timeout after ${TIMEOUT_S}s"
          echo "  ${TENANTS[$i]%%:*}: stopped at the ${TIMEOUT_S}s cap"
        fi
      done
      sleep 10
      for p in "${PIDS[@]}"; do kill -TERM "$p" 2>/dev/null || true; done
      break
    fi
    sleep 10
  done
  PIDS=()
  end=$(date +%s)
  ends=()
  for i in "${!TENANTS[@]}"; do ends+=("${TENANTS[$i]%%:*}=${ended[$i]}"); done
  "$PY" - "$out/meta.json" "$cfg" "$start" "$end" "{$(IFS=,; echo "${labels[*]}")}" "${ends[@]}" <<'PY'
import json, sys
path, cfg, start, end, labels, *ended = sys.argv[1:]
json.dump({"exp": "exp4", "part": "agents", "config": cfg, "variant_id": f"agents/{cfg}", "start": int(start), "end": int(end),
           "labels": json.loads(labels), "ended": dict(e.split("=", 1) for e in ended)}, open(path, "w"), indent=1)
PY
  bash experiments/snapshot.sh "$PREFIX-$cfg-after" | tail -1
  echo "== $(date -u +%H:%M:%S) agents / $cfg done in $(( (end - start) / 60 )) min =="
  sleep "$COOLDOWN_S"
done
echo "== agents done: $RUN/agents/"
