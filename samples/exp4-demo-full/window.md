# demo/full

- **2026-10-04 02:13:34 UTC → 2026-10-04 02:17:36 UTC** (4.0 min), variant `demo/full`
- 1160 calls, by status {200: 1129, 503: 2, 429: 5, 499: 24}, served per replica {'qwen-9b-a': 558, 'qwen-9b-b': 571}
- 251 gateway state snapshots (1 s): running / waiting / KV / prefix hits per replica in `state.jsonl`
- UI replay (7973 events): `bash experiments/replay.sh samples/exp4-demo-full/replay.jsonl`

Grafana on the box (tunnel open):
- Exp-4 scale-out: http://127.0.0.1:31495/d/demo-exp4?from=1791080009000&to=1791080261000
- Engines (vLLM): http://127.0.0.1:31495/d/demo-engines?from=1791080009000&to=1791080261000
- Pipeline (gateway): http://127.0.0.1:31495/d/demo-pipeline?from=1791080009000&to=1791080261000
- GPU: http://127.0.0.1:31495/d/demo-gpu?from=1791080009000&to=1791080261000
- Tenants & SLAs: http://127.0.0.1:31495/d/demo-tenants?from=1791080009000&to=1791080261000

Grafana replay on the laptop (`bash observability/local/replay.sh samples/exp4-demo-full/prometheus`):
- Exp-4 scale-out: http://127.0.0.1:3000/d/demo-exp4?from=1791080009000&to=1791080261000
- Engines (vLLM): http://127.0.0.1:3000/d/demo-engines?from=1791080009000&to=1791080261000
- Pipeline (gateway): http://127.0.0.1:3000/d/demo-pipeline?from=1791080009000&to=1791080261000
- GPU: http://127.0.0.1:3000/d/demo-gpu?from=1791080009000&to=1791080261000
- Tenants & SLAs: http://127.0.0.1:3000/d/demo-tenants?from=1791080009000&to=1791080261000
