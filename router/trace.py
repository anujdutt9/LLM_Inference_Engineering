from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections import deque
from pathlib import Path
from typing import Any, TextIO

# ---- every state change of every call, per run ----
#
# The gateway writes three files per experiment run, under results/<exp>/<run>/gateway/:
#   events.jsonl      every state change (received, shed, queued, dispatched, first_token, ...)
#   requests.jsonl    one row per call (Call.record(): timings, tokens, placement, outcome)
#   bodies.jsonl.gz   each request body and the assembled output (reasoning, answer, tool calls)
# and pushes events live to GET /events (the pipeline UI). The experiment scripts open and
# close runs with POST /admin/run and /admin/run/stop.


class Run:
    def __init__(self, root: Path, exp: str, run: str, meta: dict[str, Any], record_bodies: bool) -> None:
        self.exp, self.run = exp, run
        self.dir = root / exp / run / "gateway"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events: TextIO = (self.dir / "events.jsonl").open("a", buffering=1)
        self.requests: TextIO = (self.dir / "requests.jsonl").open("a", buffering=1)
        self.bodies = gzip.open(self.dir / "bodies.jsonl.gz", "at") if record_bodies else None
        self.info = {"exp": exp, "run": run, "started": time.time(), **meta}
        (self.dir / "run.json").write_text(json.dumps(self.info, indent=2))

    def close(self) -> None:
        for f in (self.events, self.requests, self.bodies):
            if f is not None:
                f.close()
        self.info["ended"] = time.time()
        (self.dir / "run.json").write_text(json.dumps(self.info, indent=2))


class EventLog:
    def __init__(self, results_dir: Path, record_bodies: bool = True, recent: int = 4000) -> None:
        self.results_dir = results_dir
        self.record_bodies = record_bodies
        self.run: Run | None = None
        self.recent: deque[dict[str, Any]] = deque(maxlen=recent)
        self.subscribers: set[asyncio.Queue] = set()
        self.mode = ""
        self.variant = "default"

    def open_run(self, exp: str, run: str, meta: dict[str, Any] | None = None) -> Run:
        self.close_run()
        self.run = Run(self.results_dir, exp, run, {"mode": self.mode, **(meta or {})}, self.record_bodies)
        self.emit("run_start", exp=exp, run=run, meta=meta or {})
        return self.run

    def close_run(self) -> dict[str, Any] | None:
        if self.run is None:
            return None
        self.emit("run_end", exp=self.run.exp, run=self.run.run)
        info = {"exp": self.run.exp, "run": self.run.run, "dir": str(self.run.dir.parent)}
        self.run.close()
        self.run = None
        return info

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {"t": round(time.time(), 4), "kind": kind, "mode": self.mode, "variant": self.variant, **fields}
        if self.run is not None:
            event.setdefault("exp", self.run.exp)
            event.setdefault("run", self.run.run)
            self.run.events.write(json.dumps(event, default=str) + "\n")
        self.recent.append(event)
        for q in list(self.subscribers):
            if q.qsize() < 5000:  # a slow viewer misses events rather than slowing the gateway
                q.put_nowait(event)
        return event

    def request_done(self, record: dict[str, Any]) -> None:
        if self.run is not None:
            row = {"exp": self.run.exp, "run": self.run.run, "mode": self.mode, "variant": self.variant, **record}
            self.run.requests.write(json.dumps(row, default=str) + "\n")

    def body(self, req_id: str, body: dict[str, Any], output: dict[str, str]) -> None:
        if self.run is not None and self.run.bodies is not None:
            self.run.bodies.write(json.dumps({"req_id": req_id, "request": body, "output": output}, default=str) + "\n")

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
