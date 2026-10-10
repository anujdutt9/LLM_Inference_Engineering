"""The same limits are set in several places; they must agree, or the stack misbehaves quietly:
gateway slots ≠ vLLM --max-num-seqs moves queueing into vLLM (where nothing can reorder or
refuse it); a client context larger than the engine's gets vLLM 400s; a role's max_tokens
above the guardrail is refused at the edge."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = yaml.safe_load((ROOT / "gateway" / "config.yaml").read_text())
DEMO = [d for d in yaml.safe_load_all((ROOT / "k8s-config" / "hami" / "hami-demo.yaml").read_text()) if d and d.get("kind") == "Deployment"]
TH = tomllib.loads((ROOT / "workspace" / "tinyharness.toml").read_text())


def _vllm_args(name: str) -> dict[str, str]:
    dep = next(d for d in DEMO if d["metadata"]["name"] == name)
    args = dep["spec"]["template"]["spec"]["containers"][0].get("args") or []
    flat = [str(a) for a in args]
    out = {}
    for i, a in enumerate(flat):
        if a.startswith("--") and i + 1 < len(flat) and not flat[i + 1].startswith("--"):
            out[a] = flat[i + 1]
    return out


def test_gateway_slots_and_context_match_each_engine():
    for deploy in ("vllm-9b-a", "vllm-9b-b"):
        spec, args = GATEWAY["pools"]["qwen-9b"], _vllm_args(deploy)
        assert int(args["--max-num-seqs"]) == spec["slots_per_replica"], deploy
        assert int(args["--max-model-len"]) == spec["max_model_len"], deploy


def test_the_two_replicas_are_identical_and_each_named():
    """Exp-4 compares 1 vs 2 replicas: only the name and host port may differ between them."""
    a, b = (next(d for d in DEMO if d["metadata"]["name"] == n) for n in ("vllm-9b-a", "vllm-9b-b"))
    ca, cb = (d["spec"]["template"]["spec"]["containers"][0] for d in (a, b))
    assert ca["args"] == cb["args"] and ca["resources"] == cb["resources"] and ca["env"] == cb["env"]
    assert {ca["ports"][0]["hostPort"], cb["ports"][0]["hostPort"]} == {8001, 8002}
    pool = GATEWAY["pools"]["qwen-9b"]
    assert sorted(pool["replica_names"].values()) == ["qwen-9b-a", "qwen-9b-b"]
    assert set(pool["replica_names"]) == set(pool["replicas"])


def test_tinyharness_context_matches_the_gateway():
    for provider in TH["providers"].values():
        for m in provider.get("models", []):
            assert m["context"] == GATEWAY["pools"][m["id"]]["max_model_len"], m


def test_output_caps_fit_the_guardrail_and_the_context():
    cap = GATEWAY["guardrails"]["max_new_tokens"]
    for role, n in GATEWAY["max_tokens_by_role"].items():
        assert n <= cap, role
        assert n < min(p["max_model_len"] for p in GATEWAY["pools"].values()), role


# Setup A (FLEET=27b-9b): the same limits must agree across its own three files.
GATEWAY_A = yaml.safe_load((ROOT / "gateway" / "config-27b-9b.yaml").read_text())
FLEET_A = [d for d in yaml.safe_load_all((ROOT / "k8s-config" / "hami" / "hami-27b-9b.yaml").read_text())
           if d and d.get("kind") == "Deployment"]
TH_A = tomllib.loads((ROOT / "workspace" / "tinyharness-27b-9b.toml").read_text())


def test_setup_a_gateway_matches_each_engine():
    for deploy, pool in (("vllm-27b", "qwen-27b"), ("vllm-9b", "qwen-9b")):
        dep = next(d for d in FLEET_A if d["metadata"]["name"] == deploy)
        c = dep["spec"]["template"]["spec"]["containers"][0]
        flat = [str(a) for a in c["args"]]
        args = {a: flat[i + 1] for i, a in enumerate(flat) if a.startswith("--") and i + 1 < len(flat)}
        spec = GATEWAY_A["pools"][pool]
        assert args["--served-model-name"] == spec["model"] == pool
        assert int(args["--max-num-seqs"]) == spec["slots_per_replica"], deploy
        assert int(args["--max-model-len"]) == spec["max_model_len"], deploy
        port = c["ports"][0]["hostPort"]
        assert spec["replicas"] == [f"http://{deploy}:{port}"], deploy


def test_setup_a_tinyharness_matches_the_gateway():
    assert TH_A["model"] == "gw:qwen-27b"
    for provider in TH_A["providers"].values():
        for m in provider.get("models", []):
            assert m["context"] == GATEWAY_A["pools"][m["id"]]["max_model_len"], m
