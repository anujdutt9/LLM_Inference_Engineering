"""Client cancellation, end to end over real sockets: the gateway and a fake engine each run in
uvicorn, a real HTTP client walks away, and the engine has to stop working on the call.

  - mid-stream: the client closes the stream; the gateway closes its upstream connection, the
    engine aborts the request (its running count drops long before the answer would end), and
    the call is recorded as 499 client_disconnect;
  - while queued: the client leaves before a slot frees; the call leaves the queue, the engine
    never receives it, and it is recorded as 499.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from gateway.config import load_config
from gateway.serve import create_app
from router.fake_vllm import FakeEngine
from router.fake_vllm import create_app as fake_app


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, app, port: int) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "Server":
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.server.force_exit = True
        self.thread.join(timeout=5)


def _wait(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = FakeEngine("qwen-27b", max_num_seqs=1, itl_s=0.05, prefill_tps=1e7)  # ~3 s per answer
    nine = FakeEngine("qwen-9b", itl_s=0.01, prefill_tps=1e7, think=False)
    pe, p9, pg = _port(), _port(), _port()
    monkeypatch.setenv("ORCH_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("POOL_QWEN_27B_URLS", f"http://127.0.0.1:{pe}")
    monkeypatch.setenv("POOL_QWEN_9B_URLS", f"http://127.0.0.1:{p9}")
    monkeypatch.setenv("ORCH_MODE", "queue")  # gateway queue on, admission off: only the queue decides
    cfg = load_config(Path(__file__).resolve().parents[1] / "gateway" / "config-27b-9b.yaml")
    cfg.pools["qwen-27b"].slots_per_replica = 1
    app = create_app(cfg, tokenizer=False)
    with Server(fake_app(engine), pe), Server(fake_app(nine), p9), Server(app, pg):
        gw = app.state.gateway
        assert _wait(lambda: all(r.stats.healthy for p in gw.pools.values() for r in p.replicas))
        yield engine, gw, f"http://127.0.0.1:{pg}"


def _body(text: str) -> dict:
    return {"model": "qwen-27b", "stream": True, "messages": [{"role": "user", "content": text}]}


def _records(gw) -> list[dict]:
    path = gw.log.run.dir / "requests.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_client_leaving_mid_stream_aborts_the_call_on_the_engine(stack):
    engine, gw, base = stack
    t0 = time.monotonic()
    with httpx.Client(timeout=30) as client:
        with client.stream("POST", f"{base}/v1/chat/completions", json=_body("long answer please"),
                           headers={"X-Tenant-ID": "t1", "X-Session-ID": "s1"}) as r:
            assert r.status_code == 200
            for line in r.iter_lines():  # read a few tokens, then walk away
                if line.startswith("data:") and '"reasoning"' in line:
                    break
            assert engine.running == 1
    # the engine stops: its slot frees well before the ~3 s answer would have ended
    assert _wait(lambda: engine.running == 0, timeout=2.0)
    assert time.monotonic() - t0 < 2.5
    assert _wait(lambda: any(r["status"] == 499 for r in _records(gw)))
    rec = next(r for r in _records(gw) if r["status"] == 499)
    assert (rec["shed_reason"], rec["layer"]) == ("client_disconnect", "client")
    assert _wait(lambda: not gw.active)  # the gateway's slot is free again


def test_client_leaving_while_queued_never_reaches_the_engine(stack):
    engine, gw, base = stack

    async def go():
        async with httpx.AsyncClient(timeout=30) as client:
            first = asyncio.create_task(client.post(f"{base}/v1/chat/completions", json=_body("first"),
                                                    headers={"X-Tenant-ID": "t1", "X-Session-ID": "a"}))
            await asyncio.sleep(0.3)  # the only slot is now busy
            second = asyncio.create_task(client.post(f"{base}/v1/chat/completions", json=_body("second, queued"),
                                                     headers={"X-Tenant-ID": "t2", "X-Session-ID": "b"}))
            await asyncio.sleep(0.5)
            assert gw.sched.lanes["qwen-27b"].depth == 1  # it is waiting in the gateway queue
            second.cancel()  # the client gives up while queued
            with pytest.raises(asyncio.CancelledError):
                await second
            await first

    asyncio.run(go())
    assert _wait(lambda: gw.sched.lanes["qwen-27b"].depth == 0)
    assert [b["messages"][0]["content"] for b in engine.requests] == ["first"]  # the second never reached it
    assert _wait(lambda: any(r["status"] == 499 for r in _records(gw)), timeout=3)
    rec = next(r for r in _records(gw) if r["status"] == 499)
    assert (rec["shed_reason"], rec["layer"], rec["replica"]) == ("client_disconnect", "client", "")  # never placed
