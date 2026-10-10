#!/usr/bin/env bash
# Mac. Pull everything recorded so far into results/ and split it per session. Run it at the
# end of the day (and any time before tearing the box down); it's safe to run repeatedly.
#
#   results/box/            the gateway's logs from the GPU box (results/live/<start>/gateway/:
#                           events.jsonl, requests.jsonl, bodies.jsonl.gz) + cluster boot logs
#   results/prometheus/     a TSDB snapshot of everything Prometheus scraped (replay it with
#                           observability/local/replay.sh results/prometheus/<snapshot>)
#   results/grafana_annotations.json   mode switches and Exp-3 markers
#   results/harness/        tinyharness transcripts of your CLI sessions (lead + subagents)
#   results/reports/        what the agents wrote in workspace/reports/
#   results/analysis/       per-session and per-group summaries (analysis/sessions.py)
set -euo pipefail
source "$(dirname "$0")/_common.sh"
mkdir -p results/box results/prometheus results/harness results/reports

echo "== Prometheus snapshot (on the box) =="
if snap=$(box "curl -sf -XPOST http://127.0.0.1:31490/api/v1/admin/tsdb/snapshot" 2>/dev/null); then
  name=$(echo "$snap" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["data"]["name"])')
  box "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; pod=\$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus,app.kubernetes.io/component=server -o name | head -1); \
       mkdir -p results/_prometheus && kubectl -n monitoring cp -c prometheus-server \${pod#pod/}:/data/snapshots/$name results/_prometheus/$name >/dev/null"
  echo "snapshot $name"
else
  echo "(no snapshot: Prometheus admin API not reachable on the box)"
fi

echo "== rsync results from the box =="
rsync -az -e "ssh -i $LAMBDA_SSH_KEY -o StrictHostKeyChecking=accept-new" "$LAMBDA:~/llm-inference/results/" results/box/
if [[ -d results/box/_prometheus ]]; then
  rsync -a results/box/_prometheus/ results/prometheus/
fi

echo "== Grafana annotations =="
curl -sf -u "admin:${GRAFANA_PASSWORD:-admin}" "$GRAFANA/api/annotations?tags=run&limit=5000" > results/grafana_annotations.json \
  || echo "(Grafana not reachable; skipped)"

echo "== tinyharness transcripts =="
for d in "$HOME"/.tinyharness/projects/*Project-workspace*; do
  [[ -d "$d" ]] && rsync -a "$d/" results/harness/
done

echo "== reports =="
rsync -a workspace/reports/ results/reports/ 2>/dev/null || true

echo "== per-session analysis =="
"$PY" -m analysis.sessions results

echo "== report quality =="
"$PY" -m analysis.grade results

echo "== replay files (one per experiment) =="
"$PY" -m analysis.replays results
echo
echo "Summary: results/analysis/summary.md   Quality: results/analysis/quality.md   Replays: results/replays/index.md"
echo "Replay dashboards on the Mac: bash observability/local/replay.sh results/prometheus/<snapshot>"
