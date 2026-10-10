"""analysis/replays.py: one replay file per experiment, each opening with the config in force."""

from __future__ import annotations

import json
from pathlib import Path

from analysis.replays import write


def _events(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_cuts_per_task_and_variant_and_merges_restarts(tmp_path):
    cfg = {"kind": "config", "variant": "default", "mode": "full", "admission": {"kv_shed": 0.9}, "pools": {"qwen-9b": {"max_model_len": 65536}}}
    first = [{**cfg, "t": 0}]
    for i, t in enumerate((100, 110, 120)):
        first.append({"t": t, "kind": "received", "req_id": f"a{i}", "task": "exp1a", "variant": "default", "tenant": "th-a"})
        first.append({"t": t + 5, "kind": "done", "req_id": f"a{i}", "task": "exp1a", "variant": "default", "tenant": "th-a", "status": 200})
    first.append({"t": 1000, "kind": "received", "req_id": "a9", "task": "exp1a", "variant": "default", "tenant": "th-a"})  # a rerun later
    second = [{**cfg, "t": 2000, "variant": "queue/fifo"},  # gateway restarted: a second folder
              {"t": 2010, "kind": "received", "req_id": "l1", "variant": "queue/fifo", "tenant": "locust-exec"},
              {"t": 2011, "kind": "done", "req_id": "l1", "variant": "queue/fifo", "tenant": "locust-exec", "status": 503}]
    _events(tmp_path / "box" / "live" / "s1" / "gateway" / "events.jsonl", first)
    _events(tmp_path / "box" / "live" / "s2" / "gateway" / "events.jsonl", second)
    rows = {r["file"]: r for r in write(tmp_path)}
    assert set(rows) == {"exp1a.jsonl", "exp1a-2.jsonl", "exp3-queue-fifo.jsonl", "all.jsonl"}
    cut = [json.loads(line) for line in (tmp_path / "replays" / "exp1a.jsonl").read_text().splitlines()]
    assert cut[0]["kind"] == "config" and cut[0]["pools"]["qwen-9b"]["max_model_len"] == 65536  # explains itself
    assert rows["exp1a.jsonl"]["calls"] == 3 and rows["exp3-queue-fifo.jsonl"]["calls"] == 1
    assert rows["all.jsonl"]["calls"] == 5
    assert (tmp_path / "replays" / "index.md").exists()
