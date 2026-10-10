from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_box_scripts_refuse_the_laptop() -> None:
    guard = (ROOT / "setup" / "_lambda_only.sh").read_text()
    assert "Darwin" in guard
    assert "nvidia-smi" in guard


def test_cluster_then_observability() -> None:
    k3s = (ROOT / "setup" / "k3s_hami.sh").read_text()
    assert "hami-charts/hami" in k3s
    assert "helm upgrade --install grafana" not in k3s
    obs = (ROOT / "setup" / "observability.sh").read_text()
    assert "helm upgrade --install prometheus" in obs
    assert "helm upgrade --install grafana" in obs
    assert "python3 -m observability.dashboards" in obs
    assert "dcgm-exporter" in obs
    prom = (ROOT / "k8s-config" / "observability" / "prometheus-values.yaml").read_text()
    assert "kube-state-metrics" in prom
    assert "prometheus-node-exporter:\n  enabled: true" in prom
    assert "job_name: hami" in prom
    assert "web.enable-admin-api" in prom  # experiments/ snapshot the TSDB after each run
    grafana = (ROOT / "k8s-config" / "observability" / "grafana-values.yaml").read_text()
    assert "sidecar" in grafana
    assert "grafana_dashboard" in grafana
    cluster = (ROOT / "setup" / "demo_cluster.sh").read_text()
    for step in ("demo_models.sh", "k3s_hami.sh", "observability.sh", "demo_apply.sh"):
        assert step in cluster
