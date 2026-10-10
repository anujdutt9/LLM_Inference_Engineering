"""analysis/sessions.py: sessions split themselves, overlapping ones form a parallel group,
lone ones are solo, and Locust traffic is cut by its ramp windows."""

from __future__ import annotations

import json
from pathlib import Path

from analysis.sessions import main


def _call(i: int, ts: float, session: str, tenant: str, model: str, **kw) -> dict:
    row = {"req_id": f"r{i}", "ts": ts, "tenant": tenant, "session": session, "agent": f"{session}-lead",
           "agent_name": "lead", "task": "", "role": "lead", "sla": "agent-lead", "model_requested": model, "pool": model,
           "status": 200, "shed_reason": "", "spilled": False, "mode": "full", "prompt_tokens": 1000, "cached_tokens": 400,
           "completion_tokens": 100, "reasoning_tokens_est": 20, "ttft_s": 0.5, "ttfat_s": 1.0, "reasoning_s": 0.5,
           "e2e_s": 3.0, "queue_wait_s": 0.0, "itl_p50_s": 0.03, "tool_calls": ["Bash"], "tool_gap_s": 1.2}
    row.update(kw)
    return row


def test_sessions_groups_and_locust(tmp_path: Path) -> None:
    gw = tmp_path / "box" / "live" / "20261002-000000" / "gateway"
    gw.mkdir(parents=True)
    rows = [
        _call(1, 1000, "s-a", "th-aaaa", "qwen-27b", task="sql-review"),
        _call(2, 1010, "s-a", "th-aaaa", "qwen-27b"),
        _call(3, 1005, "s-b", "th-bbbb", "qwen-9b", role="subagent", status=503, shed_reason="kv_pressure"),  # overlaps s-a
        _call(4, 5000, "s-c", "th-cccc", "qwen-9b"),  # alone later
        _call(5, 9000, "locust-1", "locust-exec", "qwen-9b"),
    ]
    (gw / "requests.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    harness = tmp_path / "harness" / "sessions"
    harness.mkdir(parents=True)
    (harness / "s-c.jsonl").write_text(json.dumps({"event": "user", "text": "Compare the four 10-Ks"}) + "\n")
    exp3 = tmp_path / "exp3" / "x-full"
    exp3.mkdir(parents=True)
    (exp3 / "meta.json").write_text(json.dumps({"mode": "full", "start": 8990, "end": 9100}))

    main(tmp_path)
    report = json.loads((tmp_path / "analysis" / "summary.json").read_text())
    kinds = [(g["name"], len(g["sessions"])) for g in report["groups"]]
    assert kinds == [("01-parallel", 2), ("02-solo", 1)]
    first = report["groups"][0]["sessions"]
    assert {s["task"] for s in first} == {"sql-review", ""}
    assert report["groups"][0]["summary"]["shed"] == {"admission 503 kv_pressure": 1}
    assert report["groups"][1]["sessions"][0]["task"] == "Compare the four 10-Ks"  # from the transcript
    m = report["groups"][0]["summary"]["models"]["qwen-27b"]
    assert m["prefill_decode_ratio"] == 10.0 and m["cache_hit_share"] == 0.4
    assert report["locust"][0]["mode"] == "full" and report["locust"][0]["calls"] == 1
    assert (tmp_path / "analysis" / "groups" / "01-parallel" / "calls.csv").is_file()
