"""P/D probe (experiments/exp4_pd.sh): does a request's KV move from a prefill engine to a decode engine?

    .venv/bin/python experiments/pd_probe.py --out results/exp4/<run>/pd_probe.json

Straight against the two vLLM pods through the SSH tunnel (qwen-9b-a :8001 prefill, qwen-9b-b :8002 decode), with
vLLM's disaggregated-serving protocol (NixlConnector): the prefill engine gets the request with max_tokens 1 and
kv_transfer_params {do_remote_decode: true} and returns kv_transfer_params; the decode engine gets the same request
with those params and pulls the KV instead of computing the prompt.

Per document (a ~10K-token 10-K excerpt behind a unique first line, so no cache can help), temperature 0:
  baseline   the full request on the decode engine, cold: TTFT = the whole prefill there
  pd         prefill on qwen-9b-a, then decode on qwen-9b-b: TTFT on the decode side, prompt tokens it computed
             itself (prompt - cached, from usage), and whether the answer matches the baseline's for the same prompt
Go if the decode side computed (almost) none of the prompt and the answers match.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PREFILL, DECODE = "http://127.0.0.1:8001", "http://127.0.0.1:8002"
FILINGS = sorted((ROOT / "workspace" / "data" / "filings").glob("*.md"))
QUESTION = "What is the company's largest source of revenue? One sentence."


def body(doc: str, max_tokens: int, stream: bool, params: dict | None = None) -> dict:
    b = {"model": "qwen-9b", "max_tokens": max_tokens, "temperature": 0, "stream": stream,
         "chat_template_kwargs": {"enable_thinking": False},
         "messages": [{"role": "system", "content": doc}, {"role": "user", "content": QUESTION}]}
    if stream:
        b["stream_options"] = {"include_usage": True}
    if params is not None:
        b["kv_transfer_params"] = params
    return b


def stream(client: httpx.Client, url: str, b: dict) -> dict:
    t0 = time.perf_counter()
    out = {"ttft_s": None, "text": "", "usage": {}, "status": None, "error": ""}
    with client.stream("POST", f"{url}/v1/chat/completions", json=b, timeout=300) as r:
        out["status"] = r.status_code
        if r.status_code != 200:
            out["error"] = r.read().decode()[:400]
            return out
        for line in r.iter_lines():
            if not line.startswith("data:") or line.strip() == "data: [DONE]":
                continue
            c = json.loads(line[5:])
            out["usage"] = c.get("usage") or out["usage"]
            for ch in c.get("choices") or []:
                d = (ch.get("delta") or {}).get("content") or ""
                if d and out["ttft_s"] is None:
                    out["ttft_s"] = round(time.perf_counter() - t0, 3)
                out["text"] += d
    out["e2e_s"] = round(time.perf_counter() - t0, 3)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--docs", type=int, default=4)
    ap.add_argument("--connector", choices=["nixl", "mooncake"], default="nixl")
    ap.add_argument("--prefill-bootstrap", default="", help="mooncake: http://<prefill pod IP>:<bootstrap port> (the scheme is required)")
    a = ap.parse_args()
    client = httpx.Client()
    for url in (PREFILL, DECODE):
        client.post(f"{url}/reset_prefix_cache", timeout=10)
    trials = []
    for i in range(a.docs):
        text = FILINGS[i % len(FILINGS)].read_text()
        base_doc = f"Document base-{i}: an annual report excerpt.\n\n{text}"
        pd_doc = f"Document pd-{i}: an annual report excerpt.\n\n{text}"  # a different first line: no cache reuse
        base = stream(client, DECODE, body(base_doc, 64, True))
        same = stream(client, DECODE, body(pd_doc, 64, True))  # the pd prompt computed on decode alone, for the answer check
        client.post(f"{DECODE}/reset_prefix_cache", timeout=10)  # so the P/D decode can't reuse it
        t0 = time.perf_counter()
        if a.connector == "mooncake":
            # Mooncake: the caller names the transfer and tells the decode engine where the prefill engine is
            tid = uuid.uuid4().hex
            pre = client.post(f"{PREFILL}/v1/chat/completions", timeout=300,
                              json=body(pd_doc, 1, False, {"do_remote_decode": True, "transfer_id": tid}))
            params = {"do_remote_prefill": True, "transfer_id": tid, "remote_engine_id": "vllm-9b-a",
                      "remote_bootstrap_addr": a.prefill_bootstrap} if pre.status_code == 200 else None
        else:
            pre = client.post(f"{PREFILL}/v1/chat/completions", json=body(pd_doc, 1, False, {"do_remote_decode": True}), timeout=300)
        prefill_s = round(time.perf_counter() - t0, 3)
        pj = pre.json() if pre.status_code == 200 else {}
        if a.connector == "nixl":
            params = pj.get("kv_transfer_params")
        dec = stream(client, DECODE, body(pd_doc, 64, True, params)) if params else {"error": f"prefill gave no kv_transfer_params: {str(pj)[:300] or pre.text[:300]}"}
        u = dec.get("usage") or {}
        computed = (u.get("prompt_tokens") or 0) - ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        trials.append({"doc": i, "filing": FILINGS[i % len(FILINGS)].name, "baseline_ttft_s": base.get("ttft_s"),
                       "prefill_s": prefill_s, "prefill_status": pre.status_code, "kv_transfer_params": params,
                       "pd_decode_ttft_s": dec.get("ttft_s"), "pd_prompt_tokens": u.get("prompt_tokens"),
                       "pd_computed_on_decode": computed if u else None, "answers_match": bool(dec.get("text")) and dec.get("text") == same.get("text"),
                       "pd_answer": (dec.get("text") or "")[:200], "decode_alone_answer": (same.get("text") or "")[:200],
                       "error": dec.get("error") or base.get("error") or ""})
        t = trials[-1]
        print(f"doc {i} {t['filing']}: baseline TTFT {t['baseline_ttft_s']} s | prefill {t['prefill_s']} s ({t['prefill_status']}) → "
              f"decode TTFT {t['pd_decode_ttft_s']} s, computed on decode {t['pd_computed_on_decode']} of {t['pd_prompt_tokens']} | "
              f"answers match {t['answers_match']} {('ERROR ' + t['error'][:160]) if t['error'] else ''}", flush=True)
    ok = [t for t in trials if t["pd_computed_on_decode"] is not None and t["pd_prompt_tokens"]]
    go = bool(ok) and all(t["pd_computed_on_decode"] <= 0.05 * t["pd_prompt_tokens"] and t["answers_match"] for t in ok) and len(ok) == len(trials)
    verdict = {"go": go, "trials": trials,
               "rule": "go if, for every document, the decode engine computed <= 5% of the prompt itself and its answer matches the decode-alone answer"}
    a.out.write_text(json.dumps(verdict, indent=1))
    print(f"\nP/D: {'GO' if go else 'NO-GO'} → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
