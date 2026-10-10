"""analysis/scale.py: Exp-4's numbers and verdicts from the gateway's own records (synthetic runs)."""

from __future__ import annotations

import json
from pathlib import Path

from analysis.scale import knee, main, placement_verdict, step_metrics, verdict

TARGETS = {"interactive": (2.0, 3.0), "batch": (30.0, 60.0)}
STEPS, HOLD = [4, 8, 16], 10.0


def _rows(t0: float, per_step: list[tuple[int, float]], variant: str, replicas: tuple[str, ...] = ("qwen-9b-a",)) -> list[dict]:
    """per step: (calls, ttft of each call); calls alternate over the given replicas."""
    rows = []
    for i, (n, ttft) in enumerate(per_step):
        for k in range(n):
            rows.append({"ts": t0 + i * HOLD + k * HOLD / max(n, 1), "status": 200, "sla": "interactive", "ttft_s": ttft,
                         "ttfat_s": ttft, "e2e_s": 1.0, "replica": replicas[k % len(replicas)], "variant": variant,
                         "task": f"locust-doc-{k % 3}", "route_reason": "prefix" if len(replicas) > 1 else "single",
                         "prompt_tokens": 1000, "cached_tokens": 900})
    return rows


def _states(t0: float, n: int, replicas: tuple[str, ...]) -> list[dict]:
    return [{"t": t0 + i, "replicas": [{"id": r, "running": 4.0, "waiting": 1.0, "prefix_hits": 10.0 * i, "prefix_queries": 20.0 * i}
                                        for r in replicas]} for i in range(n)]


def test_one_replica_saturates_two_replicas_move_the_knee():
    one = step_metrics(_rows(0, [(20, 0.5), (40, 0.8), (40, 9.0)], "locust/one"), _states(0, 30, ("qwen-9b-a",)), 0, STEPS, HOLD, TARGETS)
    rr = step_metrics(_rows(0, [(20, 0.4), (40, 0.5), (80, 0.9)], "locust/round_robin", ("qwen-9b-a", "qwen-9b-b")),
                      _states(0, 30, ("qwen-9b-a", "qwen-9b-b")), 0, STEPS, HOLD, TARGETS)
    assert [s["sustained"] for s in one] == [True, True, False]
    assert knee(one)["users"] == 8 and knee(rr)["users"] == 16
    assert one[0]["replicas"]["qwen-9b-a"]["prefix_hit_rate"] == 0.5
    assert rr[2]["split"] == {"qwen-9b-a": 40, "qwen-9b-b": 40}
    v = verdict({"one": one, "round_robin": rr})["scale_out"]
    assert v["S2"] == 2.0 and v["E2"] == 1.0 and v["confirmed"]
    assert v["same_load_users"] == 8 and v["same_load_ratio"] == 1.0  # at a load both sustain, goodput is the same


def test_no_scale_out_when_the_knee_stays_and_goodput_barely_moves():
    one = step_metrics(_rows(0, [(20, 0.5), (40, 0.8), (40, 9.0)], "v"), [], 0, STEPS, HOLD, TARGETS)
    rr = step_metrics(_rows(0, [(20, 0.5), (44, 0.8), (44, 9.0)], "v", ("qwen-9b-a", "qwen-9b-b")), [], 0, STEPS, HOLD, TARGETS)
    v = verdict({"one": one, "round_robin": rr})["scale_out"]
    assert round(v["S2"], 2) == 1.1 and not v["confirmed"]


def test_refusals_count_as_misses_and_by_code():
    rows = _rows(0, [(10, 0.5)], "modes/full")
    rows += [{"ts": 1.0, "status": 429, "shed_reason": "ttft_unmeetable", "sla": "interactive"},
             {"ts": 2.0, "status": 503, "shed_reason": "kv_pressure", "sla": "interactive"}]
    (s,) = step_metrics(rows, [], 0, [4], HOLD, TARGETS)
    assert s["refused"] == {"429 ttft_unmeetable": 1, "503 kv_pressure": 1}
    assert s["attainment"] == 10 / 12 and not s["sustained"]


def test_calls_the_load_generator_hung_up_on_at_the_end_are_not_failures():
    rows = _rows(0, [(10, 0.5)], "locust/one") + [{"ts": 9.0, "status": 499, "shed_reason": "client_disconnect", "sla": "interactive"}]
    (s,) = step_metrics(rows, [], 0, [4], HOLD, TARGETS)
    assert s["hung_up"] == 1 and s["errors"] == 0 and s["attainment"] == 1.0 and s["sustained"]


def test_placement_verdict_and_document_stickiness():
    rr = step_metrics(_rows(0, [(30, 1.0)], "p/rr", ("qwen-9b-a", "qwen-9b-b")), [], 0, [16], HOLD, TARGETS)
    p2c = step_metrics(_rows(0, [(30, 0.5)], "p/p2c", ("qwen-9b-a",)), [], 0, [16], HOLD, TARGETS)
    assert p2c[0]["doc_stickiness"] == 1.0 and rr[0]["doc_stickiness"] == 0.5
    v = placement_verdict(rr, p2c)
    assert v["ttft_p99_gain"] == 0.5 and v["adds_value"]


def test_report_from_a_run_folder(tmp_path: Path, monkeypatch):
    results = tmp_path / "results"
    gw = results / "box" / "live" / "20261004-000000" / "gateway"
    gw.mkdir(parents=True)
    run = results / "exp4" / "exp4-test"
    rows, states = [], []
    for k, (name, reps, steps) in enumerate([("one", ("qwen-9b-a",), [(20, 0.5), (40, 0.8), (40, 9.0)]),
                                             ("round_robin", ("qwen-9b-a", "qwen-9b-b"), [(20, 0.4), (40, 0.5), (80, 0.9)]),
                                             ("prefix_p2c", ("qwen-9b-a", "qwen-9b-b"), [(20, 0.4), (40, 0.4), (80, 0.6)])]):
        t0 = 1000.0 * (k + 1)
        rows += _rows(t0, steps, f"locust/{name}", reps)
        states += [{"kind": "state", **e} for e in _states(t0, 30, reps)]
        d = run / "locust" / name
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"variant": name, "variant_id": f"locust/{name}", "start": t0, "end": t0 + 30,
                                                 "ramp_steps": "4,8,16", "ramp_hold_s": HOLD}))
        (d / "config.json").write_text(json.dumps({"config": {"sla_classes": {"interactive": {"ttft_s": 2, "ttfat_s": 3}}}}))
    (gw / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (gw / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in states))
    monkeypatch.setattr("sys.argv", ["scale", str(run), "--results", str(results)])
    assert main() == 0
    report = (run / "report.md").read_text()
    assert "S₂ = 2.00" in report and "CONFIRMED" in report and "### prefix_p2c" in report


def test_kill_part_counts_failures_and_the_return(tmp_path: Path):
    from analysis.scale import kill_part
    results = tmp_path / "results"
    gw = results / "box" / "live" / "r" / "gateway"
    gw.mkdir(parents=True)
    d = results / "exp4" / "x" / "kill_before" / "round_robin"
    d.mkdir(parents=True)
    rows = []
    for i in range(120):  # one call a second; b is gone from 60 s to 90 s, its calls fail there
        t = 1000.0 + i
        rep = "qwen-9b-a" if i % 2 == 0 else "qwen-9b-b"
        dead = rep == "qwen-9b-b" and 60 <= i < 90
        rows.append({"ts": t, "variant": "kill_before/round_robin", "status": 503 if dead else 200,
                     "shed_reason": "upstream_error" if dead else "", "replica": rep, "ttft_s": 2.0 if (rep == "qwen-9b-b" and 90 <= i < 100) else 0.2})
    (gw / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "meta.json").write_text(json.dumps({"variant_id": "kill_before/round_robin", "start": 1000, "end": 1120}))
    (d / "kill.json").write_text(json.dumps({"killed": 1060.0, "ready": 1089.5, "replica": "qwen-9b-b"}))
    lines, out = kill_part(results / "exp4" / "x", results, "kill_before")
    assert out["failed"] == 15 and out["failed_by"] == {"503 upstream_error": 15}
    assert round(out["down_s"], 1) == 29.5 and out["first_served_after_kill_s"] == 31.0
    assert out["ttft_first_back"] == 2.0 and out["ttft_back_first10_p50"] == 2.0 and out["ttft_before_p50"] == 0.2
    assert any("← killed" in l for l in lines)
