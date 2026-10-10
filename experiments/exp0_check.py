"""Exp-0: the gate before any experiment (run from the Mac with the tunnel open).

    python experiments/exp0_check.py [--orch http://127.0.0.1:8080] [--no-box]

Works for either fleet, read from the gateway's /v1/models:
  two-9b   (Setup B) the 9B listed, both replicas (qwen-9b-a, qwen-9b-b) healthy and each answering
  27b-9b   (Setup A) both models listed, each answering as itself
Then through the gateway: tool calls, reasoning, thinking off, prefix cache hits and resets, the exact tokenizer. On the box (over SSH,
LAMBDA / LAMBDA_SSH_KEY from .env): each engine sees its HAMi slice, not the whole GPU, and
its KV cache and max concurrency clear the thresholds below. On the Mac: tinyharness is the
demo build (sends the session headers). Each check prints PASS/FAIL, exit 1 on any FAIL;
everything measured goes to results/exp0/<stamp>/checks.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TOOL = [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                          "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                         "required": ["city"]}}}]
HEADERS = {"X-Tenant-ID": "exp0", "X-Session-ID": "exp0-check", "X-Agent-Role": "lead", "X-Task": "exp0"}
# Engine -> (HAMi gpumem MiB, min KV tokens, min concurrency at 65,536 tokens).
# Setup B, two identical replicas. KV floor: two full 64K contexts (a fixed KV_GIB on the box can go well
# below the ~1.0M a slice holds)
_9B = (38912, int(os.environ.get("MIN_KV_9B", 131_072)), float(os.environ.get("MIN_CONC_9B", 2)))
ENGINES = {"vllm-9b-a": _9B, "vllm-9b-b": _9B}
REPLICAS = {"qwen-9b-a": "http://vllm-9b-a:8001", "qwen-9b-b": "http://vllm-9b-b:8002"}  # gateway replica id -> URL
# Setup A, one engine per model (k8s-config/hami/hami-27b-9b.yaml)
ENGINES_27B_9B = {"vllm-27b": (47104, int(os.environ.get("MIN_KV_27B", 300_000)), float(os.environ.get("MIN_CONC_27B", 4))),
                  "vllm-9b": (31232, int(os.environ.get("MIN_KV_9B", 300_000)), float(os.environ.get("MIN_CONC_9B", 5)))}
TOKENIZERS = {"qwen-27b": "Qwen/Qwen3.8-27B-FP8", "qwen-9b": "Qwen/Qwen3.5-9B"}


def stream(client: httpx.Client, body: dict) -> dict:
    """One streamed call: what came back, and when."""
    t0 = time.perf_counter()
    out = {"reasoning": 0, "content": "", "tool_calls": [], "ttft_s": None, "usage": {}, "status": None, "error": "", "model": None}
    with client.stream("POST", "/v1/chat/completions", json={**body, "stream": True}, headers=HEADERS, timeout=600) as r:
        out["status"] = r.status_code
        if r.status_code != 200:
            out["error"] = r.read().decode()[:300]
            out["e2e_s"] = round(time.perf_counter() - t0, 3)
            return out
        for line in r.iter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            chunk = json.loads(line[5:])
            out["usage"] = chunk.get("usage") or out["usage"]
            out["model"] = chunk.get("model") or out["model"]
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                got = d.get("reasoning") or d.get("reasoning_content") or d.get("content") or d.get("tool_calls")
                if got and out["ttft_s"] is None:
                    out["ttft_s"] = round(time.perf_counter() - t0, 3)
                out["reasoning"] += 1 if (d.get("reasoning") or d.get("reasoning_content")) else 0
                out["content"] += d.get("content") or ""
                for tc in d.get("tool_calls") or []:
                    name = (tc.get("function") or {}).get("name")
                    if name:
                        out["tool_calls"].append(name)
    out["e2e_s"] = round(time.perf_counter() - t0, 3)
    return out


def box(cmd: str) -> str:
    """Run a command on the GPU box in ~/llm-inference (same as experiments/_common.sh box)."""
    host, key = os.environ.get("LAMBDA"), os.environ.get("LAMBDA_SSH_KEY")
    if not host or not key:
        raise RuntimeError("LAMBDA / LAMBDA_SSH_KEY not set (.env)")
    r = subprocess.run(["ssh", "-i", key, "-o", "StrictHostKeyChecking=accept-new", host, f"cd ~/llm-inference && {cmd}"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


def engine_facts(name: str) -> dict:
    """What the engine's container sees (nvidia-smi under HAMi) and what vLLM said at boot."""
    smi = box(f"kubectl exec deploy/{name} -- nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits")
    # HAMi-core logs its own lines ("[HAMI-core Msg(pid:tid:...)]") around nvidia-smi's; keep only the "total, used" row
    rows = [ln for ln in smi.splitlines() if re.fullmatch(r"\s*\d+\s*,\s*\d+\s*", ln)]
    nums = [int(x) for x in re.findall(r"\d+", rows[-1])] if rows else []
    log = box(f"kubectl logs deploy/{name} 2>&1 | grep -E 'KV cache size|Maximum concurrency' | tail -4")
    kv = re.search(r"KV cache size:\s*([\d,]+)\s*tokens", log)
    conc = re.search(r"Maximum concurrency for [\d,]+ tokens per request:\s*([\d.]+)x", log)
    return {"mem_total_mib": nums[0] if len(nums) >= 2 else None, "mem_used_mib": nums[1] if len(nums) >= 2 else None,
            "kv_tokens": int(kv.group(1).replace(",", "")) if kv else None,
            "max_concurrency": float(conc.group(1)) if conc else None, "raw": (smi.strip()[-200:], log.strip()[-300:])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orch", default="http://127.0.0.1:8080")
    ap.add_argument("--no-box", action="store_true", help="skip the checks that SSH to the GPU box")
    a = ap.parse_args()
    client = httpx.Client(base_url=a.orch.rstrip("/").removesuffix("/v1"))
    results: dict = {}
    failed = 0

    def check(name: str, ok: bool, detail: object) -> None:
        nonlocal failed
        failed += not ok
        results[name] = {"ok": ok, "detail": detail}
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {json.dumps(detail, default=str)[:220]}", flush=True)

    # which fleet: Setup A serves qwen-27b and qwen-9b, Setup B qwen-9b on two replicas
    served = {m["id"]: m.get("max_model_len") for m in client.get("/v1/models").json()["data"]}
    setup_a = "qwen-27b" in served
    models_used = ("qwen-27b", "qwen-9b") if setup_a else ("qwen-9b",)
    big = models_used[0]  # the lead's model: reasoning and cancellation are checked on it
    print(f"fleet: {'27b-9b (Setup A)' if setup_a else 'two-9b (Setup B)'}", flush=True)

    th = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), "-c",
                         "import tinyharness, tinyharness.llm.identity as i; print(tinyharness.__file__)"],
                        capture_output=True, text=True)
    check("tinyharness in .venv is the demo build (sends session headers)", th.returncode == 0,
          th.stdout.strip() or th.stderr.strip()[-200:])
    # compaction counts with each model's own tokenizer (tinyharness.toml `tokenizer`): needs the package
    # and the download; without them tinyharness falls back to calibrated estimates, silently
    tok = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), "-c",
                          "from tinyharness.llm.tokens import model_counter as m; "
                          f"print({{r: bool(m(r)) for r in {tuple(TOKENIZERS[m] for m in models_used)!r}}})"],
                         capture_output=True, text=True)
    check("tinyharness counts tokens with each model's own tokenizer", "False" not in tok.stdout and tok.returncode == 0,
          tok.stdout.strip() or tok.stderr.strip()[-200:])

    health = client.get("/healthz").json()
    check("gateway up", health.get("ok") is True, health)
    for _ in range(30):  # the tokenizer loads in the background at start
        if health.get("tokenizer_exact"):
            break
        time.sleep(2)
        health = client.get("/healthz").json()
    check("gateway counts tokens with the Qwen tokenizer (not the 3.5 chars/token estimate)",
          health.get("tokenizer_exact") is True, health)
    models = {m["id"]: m.get("max_model_len") for m in client.get("/v1/models").json()["data"]}
    if setup_a:
        check("both models listed", set(models) == {"qwen-27b", "qwen-9b"}, models)
    else:
        check("one model listed: qwen-9b", set(models) == {"qwen-9b"}, models)
    state = client.get("/admin/state").json()
    reps = {r["id"]: {"healthy": r["healthy"], "kv": r["kv"], "slots": r["slots"]} for r in state["replicas"]}
    if not setup_a:
        check("replicas qwen-9b-a and qwen-9b-b in the pool", set(reps) == set(REPLICAS), sorted(reps))
    check("engines healthy, KV scraped", all(r["healthy"] and r["kv"] is not None for r in reps.values()), reps)
    # each replica answers on its own: pin the pool to one replica at a time (a live variant), then put it back
    for rid, url in ({} if setup_a else REPLICAS).items():
        client.post("/admin/config", json={"variant": f"exp0/{rid}", "patch": {"mode": "route", "pools": {"qwen-9b": {"replicas": [url]}}}})
        r = stream(client, {"model": "qwen-9b", "max_tokens": 64, "chat_template_kwargs": {"enable_thinking": False},
                            "messages": [{"role": "user", "content": "Reply OK."}]})
        served = next((e.get("replica") for e in reversed(client.get("/events/recent?n=200").json())
                       if e.get("kind") == "done" and e.get("status") == 200), None)
        check(f"replica {rid} answers through the gateway", r["status"] == 200 and served == rid,
              {"status": r["status"], "served_by": served, "ttft_s": r["ttft_s"]})
    if not setup_a:
        client.post("/admin/config", json={"variant": "default", "patch": {}})

    ask = [{"role": "user", "content": "What's the weather in Paris? Use the tool."}]
    for model in models_used:
        r = stream(client, {"model": model, "messages": ask, "tools": TOOL, "max_tokens": 1024})
        check(f"{model}: streamed tool call parsed by vLLM", r["status"] == 200 and r["tool_calls"] == ["get_weather"], r)
        check(f"{model}: answered by the {model} engine", r["model"] == model, {"asked": model, "answered_by": r["model"]})
    r = stream(client, {"model": big, "messages": [{"role": "user", "content": "What is 17 * 23?"}], "max_tokens": 1024})
    check(f"{big}: streams reasoning (thinking on)", r["status"] == 200 and r["reasoning"] > 0, {k: r[k] for k in ("reasoning", "ttft_s", "e2e_s")})
    r = stream(client, {"model": "qwen-9b", "messages": [{"role": "user", "content": "What is 17 * 23?"}], "max_tokens": 256,
                        "chat_template_kwargs": {"enable_thinking": False}})
    check("qwen-9b: thinking off per request", r["status"] == 200 and r["reasoning"] == 0 and "391" in r["content"],
          {k: r[k] for k in ("reasoning", "content", "ttft_s")})

    long_doc = (ROOT / "workspace" / "data" / "company_context_8k.md").read_text()
    for model in models_used:
        body = {"model": model, "max_tokens": 16, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": long_doc}, {"role": "user", "content": "Reply OK."}]}
        first, second = stream(client, body), stream(client, body)
        cached = (second["usage"].get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        check(f"{model}: prefix cache hit on a repeated ~8K-token prompt", cached > 0,
              {"prompt_tokens": second["usage"].get("prompt_tokens"), "cached_tokens": cached,
               "ttft_first_s": first["ttft_s"], "ttft_repeat_s": second["ttft_s"]})

    # cancellation: a client that leaves mid-stream must free the engine (the gateway closes its
    # upstream connection and vLLM aborts the request), and the call is recorded as 499
    req_id, t_left = None, None
    with client.stream("POST", "/v1/chat/completions", headers=HEADERS, timeout=120,
                       json={"model": big, "stream": True, "max_tokens": 4000,
                             "messages": [{"role": "user", "content": "Write a very long, detailed essay about GPUs."}]}) as r:
        req_id = r.headers.get("x-orch-request-id")
        for line in r.iter_lines():
            if line.startswith("data:") and '"delta"' in line:
                break
        t_left = time.time()
    freed = None
    for _ in range(40):  # vLLM's metrics are scraped every second
        st = client.get("/admin/state").json()
        running = sum((rep.get("running") or 0) for rep in st["replicas"] if rep["pool"] == big)
        if running == 0:
            freed = round(time.time() - t_left, 1)
            break
        time.sleep(0.25)
    recent = client.get("/events/recent?n=400").json()
    status = next((e.get("status") for e in recent if e.get("kind") == "done" and e.get("req_id") == req_id), None)
    check("client leaving mid-stream frees the engine and is recorded as 499",
          freed is not None and freed <= 8 and status == 499,
          {"engine_running_back_to_0_after_s": freed, "gateway_status": status, "req_id": req_id})

    reset = client.post("/admin/reset_prefix_cache")
    check("prefix cache resets on every engine (cold starts for Exp-3)", reset.status_code == 200, reset.json())

    if a.no_box:
        print("SKIP  box checks (--no-box): HAMi slices, KV size, max concurrency")
    else:
        for name, (slice_mib, min_kv, min_conc) in (ENGINES_27B_9B if setup_a else ENGINES).items():
            try:
                f = engine_facts(name)
            except Exception as e:
                check(f"{name}: box reachable", False, str(e))
                continue
            total, used = f["mem_total_mib"], f["mem_used_mib"]
            check(f"{name}: HAMi shows the pod its {slice_mib} MiB slice, not the whole GPU",
                  total is not None and abs(total - slice_mib) <= 1024, {"nvidia-smi total": total, "slice": slice_mib, "raw": f["raw"][0]})
            check(f"{name}: memory in use fits the slice", total is not None and used is not None and used <= total,
                  {"used": used, "total": total})
            check(f"{name}: KV cache ≥ {min_kv:,} tokens", (f["kv_tokens"] or 0) >= min_kv,
                  {"kv_tokens": f["kv_tokens"], "log": f["raw"][1]})
            check(f"{name}: max concurrency at 65,536 tokens ≥ {min_conc:g}x", (f["max_concurrency"] or 0) >= min_conc,
                  {"max_concurrency": f["max_concurrency"]})

    out = ROOT / "results" / "exp0" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "checks.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'} → {out / 'checks.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
