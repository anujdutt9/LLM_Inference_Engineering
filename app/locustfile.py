from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

from locust import HttpUser, between, task

# ---- NovaAI concurrency benchmark (Locust): NovaAI questions → the gateway ----
#
#   locust -f app/locustfile.py NovaUser --host http://127.0.0.1:8080              # web UI on :8089
#   locust -f app/locustfile.py,app/locust_ramp.py NovaUser --headless ...         # the 1→64 ramp
#
# Each simulated user asks one of the 49 NovaAI benchmark questions per call, as one
# streamed call with the 2K-token company context as its system prompt (the same prefix for
# every call, so prefix caching shows). Every question goes to the 9B: LIGHT as `interactive`,
# MEDIUM/HEAVY as `batch` (Exp-3 sent those to the 27B, which is no longer deployed). The tenant is the question's own persona
# (exec, finance, sre, support, product), so the gateway's per-tenant fairness has five
# tenants to share between. Sheds (429/503) are recorded as failures named by their reason.

_DATA = Path(os.environ.get("LOAD_DATASET", Path(__file__).resolve().parents[1] / "datasets" / "exp3"))
_QUESTIONS = [json.loads(line) for line in (_DATA / "benchmark_questions.jsonl").read_text().splitlines() if line.strip()]
_CONTEXT = (_DATA / "company_context_2k.md").read_text()
_ROUTE = {  # workload class → (model, SLA class, max_tokens)
    "LIGHT": ("qwen-9b", "interactive", int(os.environ.get("LOAD_MAX_TOKENS_LIGHT", "400"))),
    "MEDIUM": ("qwen-9b", "batch", int(os.environ.get("LOAD_MAX_TOKENS_MEDIUM", "800"))),
    "HEAVY": ("qwen-9b", "batch", int(os.environ.get("LOAD_MAX_TOKENS_HEAVY", "1500"))),
}
_THINKING = os.environ.get("LOAD_THINKING", "0") == "1"  # off: output length stays bounded

# Exp-4's placement test (LOAD_DOCS=N > 0): instead of the shared 2K context, each call carries one of N
# distinct documents (a 10-K filing behind a line naming the copy, so no two documents share a cached
# prefix) and asks one short question about it. Sized so the N documents overflow one replica's KV cache
# but fit in two halves: round-robin makes both replicas hold every document (and evict), prefix-aware
# placement keeps each document on one replica.
_DOCS_N = int(os.environ.get("LOAD_DOCS", "0"))
_FILINGS = sorted((Path(__file__).resolve().parents[1] / "workspace" / "data" / "filings").glob("*.md")) if _DOCS_N else []
_FILING_TEXT = [f.read_text() for f in _FILINGS]
_DOC_QUESTIONS = [
    "What is the company's largest source of revenue? One sentence.",
    "Name the single biggest risk factor. One sentence.",
    "Did revenue grow or shrink in the latest year, and by roughly how much? One sentence.",
    "What does the MD&A say about gross margin? One sentence.",
    "How much cash did operations generate? One sentence.",
    "Name one supply-chain risk mentioned. One sentence.",
]


class NovaUser(HttpUser):
    """Exp-3 is a gateway load test: each question is one streamed call with the 2K context,
    no SQL is run and answers aren't graded. It measures latency, queueing, sheds and
    fairness under concurrency, not answer quality (that's Exp-1/2's reports)."""

    wait_time = between(float(os.environ.get("LOAD_WAIT_MIN", "0.5")), float(os.environ.get("LOAD_WAIT_MAX", "2.0")))
    host = os.environ.get("LOCUST_HOST", "http://127.0.0.1:8080")

    @task
    def ask(self) -> None:
        if _DOCS_N:
            doc = random.randrange(_DOCS_N)
            model, sla, max_tokens = "qwen-9b", "interactive", 128  # room for the one-sentence answer (64 cut 16 of them)
            body = {
                "model": model, "stream": True, "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": f"Document {doc}: an annual report excerpt.\n\n{_FILING_TEXT[doc % len(_FILING_TEXT)]}"},
                    {"role": "user", "content": random.choice(_DOC_QUESTIONS)},
                ],
                "chat_template_kwargs": {"enable_thinking": False},
            }
            headers = {"X-Tenant-ID": "locust-docs", "X-SLA-Class": sla, "X-Task": f"locust-doc-{doc}",
                       "X-Session-ID": f"locust-{id(self) % 100_000}", "X-Agent-Role": "default"}
            return self._send(model, body, headers)
        q = random.choice(_QUESTIONS)
        model, sla, max_tokens = _ROUTE[q["workload_class"]]
        body = {
            "model": model,
            "stream": True,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": _CONTEXT},
                {"role": "user", "content": q["question"] + "\n\nAnswer from the definitions and what you know; be concise."},
            ],
            "chat_template_kwargs": {"enable_thinking": _THINKING},
        }
        headers = {"X-Tenant-ID": f"locust-{q['tenant']}", "X-SLA-Class": sla, "X-Task": f"locust-{q['id']}",
                   "X-Session-ID": f"locust-{id(self) % 100_000}", "X-Agent-Role": "default"}
        self._send(model, body, headers)

    def _send(self, model: str, body: dict, headers: dict) -> None:
        name = f"/v1/chat/completions [{model}]"
        t0 = time.perf_counter()
        ttft = None
        with self.client.post("/v1/chat/completions", json=body, headers=headers, name=name, stream=True,
                              catch_response=True, timeout=600) as resp:
            if resp.status_code != 200:
                try:
                    reason = resp.json().get("error", {}).get("reason", "")
                except Exception:
                    reason = resp.text[:80]
                resp.failure(f"{resp.status_code} {reason}")
                return
            for line in resp.iter_lines():
                if ttft is None and line and line.startswith(b"data:") and b'"delta"' in line and b'"content": ""' not in line:
                    ttft = (time.perf_counter() - t0) * 1000
            resp.success()
        # Streamed, Locust's own row times only until the headers arrive; these two rows give
        # TTFT and the whole answer (client side, so they include the SSH tunnel).
        e2e = (time.perf_counter() - t0) * 1000
        fire = self.environment.events.request.fire
        if ttft is not None:
            fire(request_type="TTFT", name=f"ttft [{model}]", response_time=ttft, response_length=0, exception=None, context={})
        fire(request_type="E2E", name=f"e2e [{model}]", response_time=e2e, response_length=0, exception=None, context={})
