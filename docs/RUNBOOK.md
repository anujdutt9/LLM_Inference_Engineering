# Reproduction runbook

This is the shortest complete path from a fresh clone to a replay, a functional rehearsal and a measured H100
run. The commands assume macOS or Linux, Python 3.11+, Bash and Docker. GPU runs use the Lambda Ubuntu image and
a local SSH tunnel; the repository's tests and functional rehearsal need neither a GPU nor network access.

## What each level proves

| Level | Hardware | What it proves | What it does not prove |
|---|---|---|---|
| Tests | laptop | admission, queueing, placement, cancellation, configs, dashboards, UI contracts and analysis | GPU performance |
| Replay | laptop + Docker for Grafana | the recorded call path and metrics can be inspected end to end | a fresh measurement |
| Functional rehearsal | laptop | the real gateway, experiment variants, load generator and output layout work together | H100 latency or throughput |
| Short GPU smoke run | H100 | deployment, tunnel, models, cache reset, load and collection all work | statistically useful results |
| Full experiment | H100 | the published setup and measurement can be repeated | bit-for-bit identical sampling |

## 1. Prepare the laptop

```bash
git clone https://github.com/anujdutt9/multi-tenant-llm-serving.git
cd multi-tenant-llm-serving
make setup
make data
make test
```

Expected:

- `make data` verifies the compressed NovaAI database checksum and unpacks an 84 MiB SQLite file.
- `make test` reports 90 passing tests. It opens localhost sockets but uses no network or GPU.
- `.env`, `.venv/`, unpacked databases and `results/` remain Git-ignored.

## 2. Inspect a recorded H100 run

```bash
make replay
```

Expected: the browser opens the pipeline UI with 1,160 recorded calls. The playback control supports 1–30× speed,
scrubbing and a per-call drawer.

With Docker running:

```bash
make grafana
# inspect http://127.0.0.1:3000, then:
make grafana-stop
```

Expected: Prometheus serves the included snapshot and Grafana opens the matching ten-minute measurement window.

## 3. Rehearse the experiment machinery without a GPU

```bash
bash experiments/smoke_local.sh
```

The script starts the real gateway in front of two accelerated fake vLLM engines, then runs the `one`,
`round_robin` and `prefix_p2c` variants. It stops the local fleet on exit.

Expected:

- three variant summaries, each with zero failures;
- `results/exp4/smoke-local-<timestamp>/locust/<variant>/locust_stats.csv`;
- `locust.html`, `locust.log`, `config.json` and `meta.json` beside each CSV;
- fake-engine and gateway logs in the smoke-run folder.

Useful overrides:

```bash
RAMP_STEPS=2,4,8 RAMP_HOLD_S=15 SPEED=10 bash experiments/smoke_local.sh
```

This is a functional rehearsal, not a benchmark. Fake engines intentionally replace CUDA, vLLM scheduling and
real KV-cache behavior, so never compare these timings with `docs/RESULTS.md`.

## 4. Prepare the H100

Create a Lambda 1× H100 80 GB SXM5 instance using its Ubuntu image and an NVIDIA driver compatible with the
vLLM CUDA 13 container. On the laptop:

```bash
cp .env.example .env
# Edit .env: LAMBDA=ubuntu@<ip> and LAMBDA_SSH_KEY=<path-to-key>
make sync
make ssh
```

Keep `make ssh` open; it forwards the gateway, both vLLM endpoints, Prometheus and Grafana to localhost. In that
SSH shell on the GPU box:

```bash
bash setup/demo_cluster.sh
```

Expected after roughly 15 minutes: k3s, HAMi, observability, two Qwen3.5-9B vLLM pods (Setup B) and orch-serve
are ready.
The script prints the model boot numbers and Grafana password.

Back on the laptop, size the cache exactly as measured and run the preflight gate:

```bash
KV_GIB=4.5 bash setup/demo_apply.sh   # run inside the GPU-box SSH shell
make gate                            # run on the laptop
```

Do not measure until the gate's last line reads `ALL PASS`. A `FAIL` line names the mismatched model behavior,
tokenizer, cache, cancellation path, HAMi slice or concurrency threshold.

For Setup A (27B + 9B, Exp-0 to Exp-2), build with `FLEET=27b-9b bash setup/demo_cluster.sh` instead (no `KV_GIB`
step), or switch an existing box with `FLEET=27b-9b bash experiments/redeploy.sh fleet`. The gate detects the fleet.

## 5. Run a short GPU smoke test

Use one short load step before spending time on a full experiment:

```bash
export EXP4_RUN=smoke-h100
PART=locust GROUP=scale ONLY=one,round_robin,prefix_p2c \
  RAMP_STEPS=4 RAMP_HOLD_S=20 COOLDOWN_S=2 \
  bash experiments/exp4_locust.sh
bash experiments/collect.sh
.venv/bin/python experiments/exp4_export.py "results/exp4/$EXP4_RUN"
.venv/bin/python -m analysis.scale "results/exp4/$EXP4_RUN"
```

Expected: three load summaries with no transport failures; Locust artifacts under `results/exp4/smoke-h100/locust/`;
collected gateway rows, state and replay windows after export; and `report.md`. The short duration is only a
pipeline check and is not evidence for the published capacity claims.

## 6. Run the measured experiments

All drivers refuse to start while another request is active, reset prefix caches between variants, record the
live config with each run and restore temporary config changes on exit.

| Driver | Command | Approximate time | Expected result |
|---|---|---:|---|
| Main scale-out block, c01–c03 + c07 | `bash experiments/exp4_scale.sh` | 70 min | gate, ramps, placement, modes, agent teams, collection and `report.md` |
| Placement/queue/failure block, c02/c05/c06 | `bash experiments/exp4_more.sh` | 60 min | policy table, queue depths, repeat run and replica-kill before/after evidence |
| Batching and placement controls, c09–c11 | `bash experiments/exp4_h2h.sh` | 45 min | one×16 control, spare-capacity routing, v1/v2 saturation comparison |
| Full-mode recording, c04 | `bash experiments/exp4_demo.sh` | 6 min | one replay-ready full-mode document run |
| Memory pressure, c14 | `bash experiments/exp4_mem.sh` | 20 min | preemption, admission behavior, agent run and fleet restoration |
| NIXL P/D, c12 | `bash experiments/exp4_pd.sh` | 20 min | four cold-vs-transfer probes and normal-fleet restoration |
| Mooncake P/D, c13 | `CONNECTOR=mooncake bash experiments/exp4_pd.sh` | 20 min | the same four probes using Mooncake |
| Headless agent teams, c07 | `bash experiments/exp4_agents.sh` | ≤30 min | SQL, 10-K and coding session logs plus reports |
| Live agent teams, c08 | `bash experiments/exp4_live_agents.sh prepare <name>` then the printed commands and `... finish <name>` | 15–30 min | three visible sessions, snapshots, collection and grading |

Run one block at a time. `PARTS=...`, `PART1=0` through `PART4=0`, and the timing overrides documented at the top
of each driver select smaller subsets. Do not use shortened timings for comparisons with the published tables.

Setup A's agent experiments are deliberately driven in separate terminals because simultaneous teams are the
workload. Their exact commands, prediction, success rule and outputs are on the corresponding pages:

- [Exp-0 gate](experiments/exp0.md)
- [Exp-1A SQL team](experiments/exp1a.md)
- [Exp-1B 10-K team](experiments/exp1b.md)
- [Exp-1C coding team](experiments/exp1c.md)
- [Exp-2 concurrent tenants](experiments/exp2.md)
- [Exp-2c overloaded coding sessions](experiments/exp2c.md)
- [Exp-2M all workloads](experiments/exp2m.md)

## 7. Manual controls and helper scripts

| Script | Use it for | Expected effect/output |
|---|---|---|
| `experiments/mode.sh [baseline|route|queue|full]` | inspect or switch the gateway mode | immediate live switch; no restart |
| `experiments/variant.sh <group>/<name>` | apply one variant by hand | live config patch, recorded on later calls |
| `experiments/snapshot.sh <name>` | capture before/after state | pods, events, logs, metrics, GPU and gateway state under `results/runs/` |
| `experiments/collect.sh` | preserve the GPU box before teardown | gateway logs, Prometheus snapshot, annotations, transcripts and reports under `results/` |
| `experiments/finish.sh <label>` | package one agent experiment | window, filtered calls, replay, snapshots, Prometheus and agent output |
| `experiments/replay.sh <replay.jsonl>` | inspect any recorded run | local pipeline UI; no GPU needed |
| `experiments/redeploy.sh gateway` | apply gateway code/config | gateway restart, engines untouched |
| `experiments/redeploy.sh fleet` | change model fleet, KV or slots | model redeploy followed by gateway restart |
| `experiments/exp4_locust.sh` | run a chosen variant group directly | CSV, HTML, log, applied config and window metadata per variant |
| `experiments/exp4_export.py <run>` | join collected rows to experiment windows | `requests.jsonl`, `state.jsonl`, `replay.jsonl` and `window.md` per variant |

Every script's opening comment lists its accepted environment variables and generated paths. The orchestrators
above are the supported entry points; the helpers are useful for targeted reruns and debugging.

## 8. Verify and preserve a run

Before terminating the paid instance:

```bash
bash experiments/collect.sh
.venv/bin/python -m analysis.scale results/exp4/<run>
.venv/bin/python -m analysis.grade results
```

Check that the run contains:

- Locust CSV/HTML and `meta.json` for every intended variant;
- `requests.jsonl`, `state.jsonl`, `replay.jsonl` and `window.md` after export;
- before/after stack snapshots and the Prometheus snapshot;
- `report.md` and `report.json` from analysis;
- agent reports/transcripts when the workload used tinyharness.

Replay locally before deleting the instance:

```bash
bash experiments/replay.sh results/exp4/<run>/<part>/<variant>/replay.jsonl
bash observability/local/replay.sh results/exp4/<run>/prometheus
```

## 9. Interpret a reproduction honestly

The published Locust runs repeated within roughly ±5%. Agent runs are nondeterministic and may differ in call
count, tool choices and duration. Use the same model images, GPU, KV size, slot count, load steps and hold times
before comparing numeric results. Report deviations instead of silently retuning the success rule.
