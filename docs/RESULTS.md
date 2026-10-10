# Results

Every experiment, what was predicted before it ran, what was measured, and the verdict. Each row links to its
page in [experiments/](experiments/), which has the setup, how to run it, the data, what went wrong and what we
learned. How the models fit on the GPU (weights, KV cache, bandwidth): [MODELS_AND_MEMORY.md](MODELS_AND_MEMORY.md).

Hardware for everything: one NVIDIA H100 80 GB SXM5 (HBM3, 3.35 TB/s) on Lambda, k3s + HAMi memory slices,
vLLM v0.30.0, one gateway (orch-serve) in front.

## Setup A: Qwen3.8-27B-FP8 + Qwen3.5-9B (`FLEET=27b-9b`)

Two engines, one model each: the 27B (46 GiB slice, 8 slots, ~362K KV tokens) and the 9B (30.5 GiB slice,
8 slots, ~585K KV tokens). The gateway routes by intent: each agent role (lead, analyst, reader, compaction) has
its model, thinking budget and SLA class. The workloads are three agent teams run with tinyharness: SQL analysis
of the NovaAI database, a 10-K comparison of four chip makers, and a coding team fixing a billing library.

| Exp | What | Measured | Verdict | Page |
|---|---|---|---|---|
| Exp-0 | The gate: models, tool calls, reasoning, prefix cache, cancellation, HAMi slices, KV size | First run 18 PASS / 4 FAIL (the check misread HAMi's `nvidia-smi` output), then **ALL PASS 23 / 23**. Prefix cache cut TTFT on an 8.7K-token prompt **0.685 → 0.185 s** (27B) and **0.366 → 0.127 s** (9B), reused in whole blocks (5 × 1,568 / 8 × 1,056 tokens); a cancelled stream frees the engine in ~0.1 s; counting the chat template brought prompt estimates from −83% to within 0.5% | ✅ ALL PASS | [exp0](experiments/exp0.md) |
| Exp-1A | SQL team alone: 27B lead + 4 analysts, 64K window | Four attempts. The fixes (compaction trigger, per-role thinking budgets, the summarizer call, limits sized from the usable window) took fact recall from **40% → 64%** (16 / 25 facts, 6 / 8 stories): 17.7 min, 103 calls, 0 refused, 0 cut off | ✅ after the fixes | [exp1a](experiments/exp1a.md) |
| Exp-1B | 10-K team alone: 9B lead vs 27B lead, 4 readers on the 9B | 9B lead: **3.1 min**, 82 / 82 citations real, table 25 / 28 right (1 wrong number, 2 missing). 27B lead: **9.0 min**, 113 / 113 citations, no wrong number | ✅, quality tracks the lead's model | [exp1b](experiments/exp1b.md) |
| Exp-1C | Coding team alone: lead + 4 coders on the 27B | v1 8 / 8 in 1.7 min; v2 **16 / 16** (33 / 33 hidden checks) in **3.7 min**, 45 calls: right quality, too short to load the GPU on its own | ✅ | [exp1c](experiments/exp1c.md) |
| Exp-2 | SQL and 10-K teams at once (two tenants), baseline | 0 calls waited inside vLLM (peak 6 agents on 8 slots); A finished in 13.6 min, B in 9.9 min; A's lead whole-call p95 **74 → 172 s** and the 9B's token gap **8.6 → 17.8 ms** from sharing the GPU's compute. The SLA targets per role were derived from the solo runs, after four documented failures | ✅ (the full pass was not run) | [exp2](experiments/exp2.md) |
| Exp-2c | 4 coding sessions at once (20 agents for 8 slots), baseline vs full | Full: vLLM waiting **0**, TTFT p95 **17.3 → 7.5 s**, goodput **+19%**, within target 63% → 81% of served but **50% of attempts**: **140 / 365 calls refused**, 14 coder runs died, sessions **47% longer** (7.6 → 11.2 min); quality 16 / 16 × 4 because the work was redone | ❌ chat-style admission hurts agents: a refused agent step comes back. Fix: session-aware admission | [exp2c](experiments/exp2c.md) |
| Exp-2M | All three workloads at once with larger teams (SQL 8 analysts, 10-K 8 readers, code 4 coders) | Baseline: the SQL lead overflowed 64K and was stopped at 27 min with no report; coding finished in 6.7 min. Full: 155 / 360 calls refused, including every 10-K reader call (144 × `deadline_unmeetable`, no report); coding finished in 4.5 min. Not graded | Partial record; the same lesson as Exp-2c | [exp2m](experiments/exp2m.md) |

## Setup B: two Qwen3.5-9B replicas (`FLEET=two-9b`, Exp-4)

Two identical replicas (qwen-9b-a, qwen-9b-b) in 38 GiB HAMi slices, 8 slots and 4.5 GiB KV (264,993 tokens)
each. The exceptions are H1 (16 slots), the P/D probes (KV connector on) and the memory test (KV 1.25 GiB).
"Questions" is the Locust load (NovaAI questions, 2K-token shared prefix); "documents" is 40 distinct ~10K-token
annual-report documents with one short question each. A call is **on time** when it meets every target of its
SLA class; **goodput** is on-time calls per second; the **knee** is the highest load with ≥ 90% on time.
Observed numbers are recomputed from the recorded calls (`report.json`, per-call `requests.jsonl`, 1 s `state.jsonl`).

| # | Experiment | Prediction (before) | Observed | Verdict | Page |
|---|---|---|---|---|---|
| 1 | **One vs two replicas.** Questions, 4→8→16→24→32 users × 45 s, route; `one` / `round_robin` / `prefix_p2c`; run twice | knee ~8–10 users with one, ~14–18 with two; S₂ 1.3–1.7 | knee **8 → 16** in both runs; S₂ **1.18 / 0.91**; token gap **7 ms → 14–17 ms** at every load; tokens/s ~950–1,030 at 16–32 users either way | Scale-out ✅ (knee rule); S₂ ❌: the second replica added **slots, not speed** | [c01](experiments/c01.md) |
| 2 | **Placement on 40 documents.** 24 users, route; round_robin, least_loaded, p2c, prefix_p2c; run twice | prefix_p2c: >90% same replica, >75% from cache, p99 ≥30% lower | goodput round_robin **3.72 / 3.85**, least_loaded 4.39, p2c 4.27, prefix_p2c **4.56 / 4.32**; from cache 41% vs 44–48%; same replica 56% vs 60% | Borderline (+12–23% vs the 15% rule); cache prediction ❌: both replicas were full, so prefix_p2c fell back to p2c (912 vs 294 placements) | [c02](experiments/c02.md) |
| 3 | **Gateway modes.** 24 users on 16 slots: route / queue / full, prefix_p2c | queue moves the waiting to the gateway; full refuses some LIGHT calls (429) | interactive TTFT ≤ 2 s, of served: route **52%**, queue **59%**, full **76%**; of attempts: 50% / 55% / 32%. Full refused **48 × 429 + 3 × 503**. Interactive TTFT p50 1.90 / 1.58 / **0.70 s** | ✅ queue moves the waiting (vLLM waiting 2.4 → 0); full refused more than expected | [c03](experiments/c03.md) |
| 4 | **Full-mode demo.** Documents, prefix_p2c + full, 24 users | (demonstration) | 1,129 served, 5 × 429, 2 × 503; placement `single` **1,016 / 1,129** | With a gateway queue, the queue decides placement | [c04](experiments/c04.md) |
| 5 | **Queue depth** 0 / 2 / 8 / 64. Questions, 24 users, queue | the same served/s at every depth; deeper means fewer refusals and higher p99 | served **2.27 / 2.21 / 2.33 / 2.25 /s**; refused 481 / 343 / 2 / 0; TTFT p99 **0.28** / 3.18 / 7.70 / 6.58 s; goodput 2.27 / 2.17 / 1.98 / 1.85 | ✅ a queue trades refusals for waiting; capacity is the GPU's | [c05](experiments/c05.md) |
| 6 | **Replica kill.** round_robin, 16 users, qwen-9b-b's pod deleted at 60 s; before and after the liveness fix | before: ~half the calls fail until it's back; after: ≤5 failures | before: **188 of 782** failed (181 × 503 unreachable, 7 × 502), back after 132 s. After: **8 of 675** (all in flight at the kill), back after 131 s. Locust hang-ups (499: 14 / 13) not counted | ✅ fix: 8 / 188 = 4.3% ≤ 5% | [c06](experiments/c06.md) |
| 7 | **Agent teams, headless.** sql (lead+4), 10-K (lead+8), code (lead+4) at once, prefix_p2c, route | all finish in 30 min; even split; readers placed by prefix | 607 calls split **300 / 307**; **77%** from cache; KV peak ~62%, 0 preemptions; 10-K 9.4 min (ran its team 3×), SQL and code hit the 30-min cap | Serving ✅; the agent harness failed, then was fixed | [c07](experiments/c07.md) |
| 8 | **Agent teams, live** (three terminals, after the harness fixes) | no sandbox prompts, no team re-runs | SQL **9.5 min / 94 calls**, 10-K **8.5 / 76**, code **12.6 / 156**; 47 × 429 while full mode was briefly on; quality: SQL recall 12%, 10-K 0 citations, code 14 / 16 | Serving ✅; quality ❌ with the 9B as lead (a model-capacity finding) | [c08](experiments/c08.md) |
| 9 | **H1: one engine × 16 slots vs two × 8.** The same ramp | one16 sustains 16 users at ≥ the same goodput, ITL 7–10 ms | knee **24** (vs 16); at 24 users **4.02 vs 2.20 goodput/s (1.8×)**; token gap **8 ms**; ~1,690–1,730 tok/s (vs ~970) | ✅, stronger than predicted: **batch before replicas** | [c09](experiments/c09.md) |
| 10 | **H2: placement with spare capacity.** Documents, 12 users: round_robin vs prefix_p2c_v2 | v2: >85% same replica, cache far above round_robin, TTFT p50 >15% lower | goodput **3.70 → 5.24 (+42%)**; from cache **49% → 91%**; same replica 58% → 86%; TTFT p50 **0.62 → 0.10 s** | ✅ | [c10](experiments/c10.md) |
| 11 | **H3: placement at saturation, v1 vs v2.** Documents, 24 users | v2 gains a little (<15%) | goodput **4.50 → 6.12 (+36%)**; from cache 47% → 66%; prefix placements 298 → 674 | ❌ better than predicted: v1's flaws, not only saturation, held #2 back | [c11](experiments/c11.md) |
| 12 | **P/D with NIXL.** Prefill on qwen-9b-a → decode on qwen-9b-b, 4 × ~10K-token documents | decode computes ≤5% of the prompt; answers match | computed on decode **1 token** of 9.0–10.0K; answers match 4 / 4; decode TTFT **2.2–2.5 s** vs **0.35–0.40 s** cold recompute; 206 MB at ~92 MB/s (UCX over TCP) | GO, but 6× slower than recomputing | [c12](experiments/c12.md) |
| 13 | **P/D with Mooncake.** The same probe | as #12 | 1 token computed; decode TTFT **0.27–0.30 s** (−25% vs recompute); 214.5 MB in 163 ms (**1,319 MB/s, 14×** NIXL); 1 of 4 answers differs by three words | NO-GO by the strict rule (1 / 4 wording); the hand-off works and pays | [c13](experiments/c13.md) |
| 14 | **Memory pressure.** KV 1.25 GiB (73K tokens), documents, 24 users: route, then full; then the 10-K team | route: preemptions, <8 running, long TTFT; full: 503 kv_pressure; the team finishes | route: **194 preemptions**, KV 99%, ~5.3 running + 4.5 waiting per replica, from cache **6%**, goodput **4.56 → 0.09/s**, TTFT p50 4.40 s. Full: **all 2,158 refused, 429 ttft_unmeetable**. Team: 10 preemptions, KV 99–100%, finished in 2.5 min with citations | route ✅; full ❌: admission locked itself out (its learned prefill rate stuck at 2,670 tok/s); team ✅ | [c14](experiments/c14.md) |
| — | **What went wrong, and what we learned from it** | | every issue found during Exp-4, what failed, the fix and whether it was verified | | [c15](experiments/c15.md) |

### Notes on the numbers

- **#3, interactive on time.** Both bases are shown: of served calls and of all attempts (TTFT ≤ 2 s). Interactive
  calls in full: 90 attempts = 38 served + 48 × 429 (admission) + 3 × 503 (expired in queue) + 1 × 499 (client
  disconnect).
- **#14, the 10-K team's KV peak** is the gateway's 1 s state (99% / 100%); Prometheus' 5 s samples show 87% / 97%.
- **#1 and #9, tokens/s**, per 45 s step from completed calls: one ~950–1,030 at 16–32 users; round_robin 740–965;
  one16 1,230 at 16 users, 1,690–1,730 at 24–32. Grafana's peak rates read higher (~1,050 and ~1,765).
- **#14, the learned prefill rate** (`orch_learned_prefill_tps`): ~17–19K tok/s before the route step, as low as
  ~1.6K during it, flat at 2,670 through the whole full step, back to ~11K within seconds of the team's first
  served call. Chart: [07b_memory_lockout.png](presentation/charts/07b_memory_lockout.png).

## Charts

Drawn from the run folders by `python -m analysis.charts` and `python -m analysis.talk_charts` (`make charts`,
with the Drive folder's `results/` in place; the memory charts also read Prometheus, so start
`bash observability/local/replay.sh results/prometheus/20261004T061215Z-51ca83e3b6e9ef8e` first):
[presentation/charts/](presentation/charts/).
