#!/usr/bin/env bash
# Replay recorded runs on a laptop: Prometheus serves an exported TSDB snapshot, Grafana shows
# the same dashboards as the cluster (datasource uid "prometheus").
#
#   observability/local/replay.sh <snapshot-dir> [--annotations runs.json] [--in-place]
#   observability/local/replay.sh stop
#
# <snapshot-dir>: a Prometheus TSDB directory, e.g. what
#   curl -XPOST http://<prometheus>/api/v1/admin/tsdb/snapshot   (needs --web.enable-admin-api)
# leaves in <data>/snapshots/<name>/ (block folders with meta.json), copied to this machine.
#
# Prometheus writes into its data dir (lock file, WAL, compaction), so by default the snapshot is
# cloned into observability/local/.work/<name>/ first (APFS clone on macOS: instant, no extra
# space). --in-place mounts the snapshot directory itself.
#
# Run annotations live in the cluster Grafana, not in Prometheus. Export them with
#   curl -s 'http://<grafana>/api/annotations?tags=run&limit=1000' > runs.json
# and pass --annotations runs.json (or drop runs.json / annotations.json next to the
# snapshot) to re-create them locally.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.yml")
GRAFANA=http://127.0.0.1:3000
PROM=http://127.0.0.1:9090

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

if [[ "${1:-}" == "stop" ]]; then
  PROM_DATA=/tmp "${COMPOSE[@]}" down
  exit 0
fi
[[ $# -ge 1 && "$1" != "-h" && "$1" != "--help" ]] || usage 0

SNAP="" ANN="" IN_PLACE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --annotations) ANN="$2"; shift 2 ;;
    --in-place) IN_PLACE=1; shift ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) SNAP="$1"; shift ;;
  esac
done
[[ -d "$SNAP" ]] || { echo "not a directory: $SNAP" >&2; exit 1; }
SNAP="$(cd "$SNAP" && pwd)"

if ! ls "$SNAP"/*/meta.json >/dev/null 2>&1 && [[ ! -d "$SNAP/wal" ]]; then
  echo "warning: $SNAP has no TSDB blocks (*/meta.json) or wal/; Prometheus will start empty" >&2
fi
if [[ -z "$ANN" ]]; then
  for f in "$SNAP/runs.json" "$SNAP/annotations.json" "$SNAP/../runs.json" "$SNAP/../annotations.json"; do
    [[ -f "$f" ]] && { ANN="$f"; break; }
  done
fi

if [[ $IN_PLACE -eq 1 ]]; then
  DATA="$SNAP"
else
  DATA="$HERE/.work/$(basename "$SNAP")"
  if [[ ! -d "$DATA" ]]; then
    mkdir -p "$HERE/.work"
    echo "cloning snapshot -> $DATA"
    cp -cR "$SNAP" "$DATA" 2>/dev/null || cp -R --reflink=auto "$SNAP" "$DATA" 2>/dev/null || cp -R "$SNAP" "$DATA"
  else
    echo "reusing $DATA (delete it to re-copy the snapshot)"
  fi
fi

export PROM_DATA="$DATA"
export PROM_USER="$(id -u):$(id -g)"
"${COMPOSE[@]}" up -d --force-recreate

wait_for() {  # url, name
  for _ in $(seq 1 90); do
    curl -fsS -o /dev/null "$1" 2>/dev/null && return 0
    sleep 1
  done
  echo "$2 did not become ready; see: docker compose -f '$HERE/docker-compose.yml' logs $2" >&2
  return 1
}
wait_for "$PROM/-/ready" prometheus
wait_for "$GRAFANA/api/health" grafana

# Time range covered by the snapshot (block meta.json minTime/maxTime, ms since epoch).
RANGE="$(python3 - "$DATA" <<'PY'
import glob, json, os, sys
lo, hi = None, None
for meta in glob.glob(os.path.join(sys.argv[1], "*", "meta.json")):
    try:
        m = json.load(open(meta))
    except Exception:
        continue
    lo = m["minTime"] if lo is None else min(lo, m["minTime"])
    hi = m["maxTime"] if hi is None else max(hi, m["maxTime"])
if lo is not None:
    print(f"{lo} {hi}")
PY
)"

if [[ -n "$ANN" ]]; then
  echo "importing run annotations from $ANN"
  python3 - "$ANN" "$GRAFANA" <<'PY' || echo "warning: annotation import failed" >&2
import json, sys, urllib.request
items = json.load(open(sys.argv[1]))
ok = 0
for a in items if isinstance(items, list) else []:
    body = {k: a[k] for k in ("time", "timeEnd", "tags", "text") if k in a}
    req = urllib.request.Request(sys.argv[2] + "/api/annotations", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        ok += 1
    except Exception as e:
        print("  skip:", e, file=sys.stderr)
print(f"  {ok} annotations imported")
PY
fi

QS=""
if [[ -n "$RANGE" ]]; then
  read -r FROM TO <<<"$RANGE"
  QS="?from=$FROM&to=$TO&refresh="
  echo "snapshot covers $(python3 -c "import datetime as d,sys;f=lambda x:d.datetime.fromtimestamp(int(x)/1000).strftime('%Y-%m-%d %H:%M:%S');print(f(sys.argv[1]),'->',f(sys.argv[2]))" "$FROM" "$TO")"
fi

cat <<EOF

Replay stack is up.
  Grafana     $GRAFANA/dashboards   (anonymous access, no login)
  Pipeline    $GRAFANA/d/demo-pipeline$QS
  Exp-1       $GRAFANA/d/demo-exp1$QS
  Exp-2       $GRAFANA/d/demo-exp2$QS
  Exp-3       $GRAFANA/d/demo-exp3$QS
  Exp-4       $GRAFANA/d/demo-exp4$QS
  Prometheus  $PROM
Dashboards default to now-30m; the links above open the snapshot's time range with auto-refresh off.
Stop with: $0 stop
EOF
