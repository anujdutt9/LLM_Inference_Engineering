from __future__ import annotations

import json
from pathlib import Path

from observability.dashboards import DEMO_DASHBOARDS, DEMO_METRIC_NAMES, write_dashboards

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "k8s-config" / "observability" / "dashboards"

DEMO_TITLES = {
    "demo-pipeline": "Demo / Pipeline (gateway)",
    "demo-engines": "Demo / Engines (vLLM)",
    "demo-tenants": "Demo / Tenants & SLAs",
    "demo-gpu": "Demo / GPU",
    "demo-exp1": "Demo / Exp-1 Solo runs",
    "demo-exp2": "Demo / Exp-2 Multi-tenant",
    "demo-exp3": "Demo / Exp-3 Load",
    "demo-exp4": "Demo / Exp-4 Scale-out",
}

def _iter_panels(panels: list[dict]):
    for panel in panels:
        yield panel
        yield from _iter_panels(panel.get("panels", []))


def test_demo_dashboards_generate_valid_json() -> None:
    assert set(DEMO_DASHBOARDS) == set(DEMO_TITLES)
    written = {p.name for p in write_dashboards(DEST)}
    for name, builder in DEMO_DASHBOARDS.items():
        path = DEST / f"{name}.json"
        assert path.name in written
        payload = json.loads(path.read_text())
        assert payload == builder()
        assert payload["title"] == DEMO_TITLES[name]
        assert payload["uid"] == name and payload["title"].startswith("Demo / ")
        assert payload["time"] == {"from": "now-30m", "to": "now"}
        assert payload["refresh"] == "5s"
        names = {a["name"] for a in payload["annotations"]["list"]}
        assert {"Annotations & Alerts", "Runs"} <= names
        runs = next(a for a in payload["annotations"]["list"] if a["name"] == "Runs")
        assert runs["target"]["tags"] == ["run"]

        ids = [p["id"] for p in _iter_panels(payload["panels"])]
        assert len(ids) == len(set(ids)), f"{name}: duplicate panel ids"
        for panel in _iter_panels(payload["panels"]):
            grid = panel["gridPos"]
            assert 0 <= grid["x"] and grid["x"] + grid["w"] <= 24, f"{name}/{panel['title']}"
            if panel["type"] in ("text", "row"):
                continue
            assert panel["targets"], f"{name}/{panel['title']} has no targets"
            for target in panel["targets"]:
                expr = target["expr"]
                assert expr.strip()
                assert expr.count("(") == expr.count(")"), f"{name}/{panel['title']}: {expr}"
                assert expr.count("{") == expr.count("}"), f"{name}/{panel['title']}: {expr}"
                if "rate(" in expr:
                    assert "[5m]" not in expr and ("$__rate_interval" in expr or "$__range" in expr), expr

        blob = json.dumps(payload)
        for metric in DEMO_METRIC_NAMES[name]:
            assert metric in blob, f"{name} missing {metric}"


def test_experiment_boards_open_with_a_text_panel() -> None:
    for name in ("demo-exp1", "demo-exp2", "demo-exp3", "demo-exp4"):
        first = DEMO_DASHBOARDS[name]()["panels"][0]
        assert first["type"] == "text" and first["gridPos"]["y"] == 0
        assert first["options"]["content"].startswith("### Exp-")


def test_only_the_demo_boards_ship() -> None:
    write_dashboards(DEST)
    assert {p.stem for p in DEST.glob("*.json")} == set(DEMO_DASHBOARDS)
