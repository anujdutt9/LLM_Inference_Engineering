from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.metrics import METRICS

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s-config"

@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    saved = {key: os.environ.pop(key, None) for key in ("OVERFLOW_BASE_URL", "OVERFLOW_API_KEY")}
    METRICS.reset()
    yield
    METRICS.reset()
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value
