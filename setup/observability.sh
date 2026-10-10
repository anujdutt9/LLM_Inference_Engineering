#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/setup/_lambda_only.sh"
cd "$ROOT"

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

echo "== Prometheus (helm) =="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo update prometheus-community >/dev/null
helm upgrade --install prometheus prometheus-community/prometheus \
  --namespace monitoring \
  --create-namespace \
  --values "$ROOT/k8s-config/observability/prometheus-values.yaml" \
  --wait --timeout 10m

echo "== Grafana (helm) =="
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update grafana >/dev/null
helm upgrade --install grafana grafana/grafana \
  --namespace monitoring \
  --values "$ROOT/k8s-config/observability/grafana-values.yaml" \
  --wait --timeout 10m

echo "== DCGM exporter (cluster GPU) =="
kubectl apply -f "$ROOT/k8s-config/observability/dcgm-exporter.yaml" || true

echo "== Grafana dashboards (sidecar ConfigMap) =="
python3 -m observability.dashboards
kubectl -n monitoring create configmap grafana-dashboards \
  --from-file="$ROOT/k8s-config/observability/dashboards" \
  --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
kubectl -n monitoring label configmap grafana-dashboards grafana_dashboard=1 --overwrite

echo
echo "Observability is up. On the Mac (tunnel from setup/ssh.sh open):"
echo "  Grafana:     http://127.0.0.1:31495   dashboards under Demo /; admin password below"
echo "  Prometheus:  http://127.0.0.1:31490"
echo
echo "Grafana admin password:"
kubectl -n monitoring get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d
echo
