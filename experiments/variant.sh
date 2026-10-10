#!/usr/bin/env bash
# Mac, tunnel open. Apply one variant from experiments/variants.yaml to the live gateway (no
# restart), for runs you drive by hand. Every call afterwards records the variant; the UI marks
# the switch on its timeline.
#   bash experiments/variant.sh scale/prefix_p2c
#   bash experiments/variant.sh scale/one
#   bash experiments/variant.sh default            # back to gateway/config.yaml
set -euo pipefail
source "$(dirname "$0")/_common.sh"
need_gateway
name="${1:?usage: variant.sh <group>/<variant> | default}"
if [[ "$name" == default ]]; then
  patch='{}'
else
  patch="$("$PY" - "$name" <<'PY'
import json, sys, yaml
group, _, var = sys.argv[1].partition("/")
v = yaml.safe_load(open("experiments/variants.yaml"))
if group not in v or var not in v[group]:
    sys.exit(f"no variant {sys.argv[1]!r}; groups: {', '.join(v)}")
print(json.dumps(v[group][var]))
PY
)"
fi
curl -sf -X POST "$ORCH/admin/config" -H 'content-type: application/json' -d "{\"variant\": \"$name\", \"patch\": $patch}"
echo
annotate "variant $name" "variant"
