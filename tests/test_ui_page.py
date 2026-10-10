"""The pipeline UI (ui/): static, no build step, no network dependencies, and in step with the
event contract the live gateway publishes (router/trace.py EventLog, gateway/serve.py)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
HTML = (UI / "index.html").read_text()
JS = (UI / "app.js").read_text()
CSS = (UI / "style.css").read_text()


def test_page_loads_only_local_assets():
    assert '<script src="app.js">' in HTML
    assert 'href="style.css"' in HTML
    # nothing fetched from another origin: the page must work offline on the demo laptop
    for text in (HTML, JS, CSS):
        assert not re.search(r"(src|href)=[\"']https?://", text)
        assert "@import" not in text


def test_every_id_the_script_uses_exists():
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    used = set(re.findall(r"\$\('([A-Za-z][\w-]*)'\)", JS))
    missing = used - ids - {"ttftChart"}  # ttftChart is created by the latency panel
    assert not missing, missing


@pytest.mark.parametrize("kind", ["received", "admitted", "queued", "dispatched", "first_token", "first_answer",
                                  "tool_call", "shed", "done", "state", "mode", "run_start", "run_end",
                                  "prefix_cache_reset"])
def test_handles_every_gateway_event_kind(kind):
    assert f"case '{kind}'" in JS


def test_handles_harness_events_and_endpoints():
    assert "harness_" in JS
    for path in ("events?backlog=", "events/recent?n=", "admin/state"):
        assert path in JS
    assert "EventSource" in JS


def test_replay_controls():
    for el in ('id="file"', 'id="scrub"', 'id="btnPlay"', 'id="drop"'):
        assert el in HTML
    for speed in (1, 2, 5, 10, 30):
        assert f'data-speed="{speed}"' in HTML
    assert "params.get('replay')" in JS


def test_dark_and_light_themes_define_the_colour_language():
    for var in ("--queue", "--prefill", "--reason", "--answer", "--tool", "--spill", "--c429", "--c503"):
        assert CSS.count(var + ":") >= 2, var  # dark default + light override


def test_served_by_the_gateway_mount():
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles
    from starlette.testclient import TestClient

    app = Starlette(routes=[Mount("/ui", app=StaticFiles(directory=str(UI), html=True), name="ui")])
    with TestClient(app) as client:
        r = client.get("/ui/")
        assert r.status_code == 200 and "Inference Pipeline" in r.text
        assert client.get("/ui/app.js").status_code == 200
        assert client.get("/ui/style.css").status_code == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_script_parses():
    out = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))", str(UI / "app.js")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
