[← all experiments](../../README.md#experiments) · all numbers: [RESULTS.md](../RESULTS.md) · models and memory: [MODELS_AND_MEMORY.md](../MODELS_AND_MEMORY.md)

# exp2m · Three workloads at once, with larger teams: baseline vs full

## What we're doing

All three agent workloads at once, as three tenants, with larger teams: **SQL** (`sql8.md`: lead + 8 analysts,
27B), **10-K** (`10k8.md`: 27B lead + 8 readers on the 9B, two per filing) and **coding** (`code.md`: lead + 4
coders, 27B). That is 15 agents on the 27B's 8 slots and 8 readers on the 9B's 8 slots. Two passes, identical
except for the gateway mode: `baseline`, then `full`.

## Why

This was meant as the headline agent run: a mixed load where one 27B lane carries leads (priority 1) among analysts
and coders, so the gateway's priorities and fair turns between tenants have something to decide, and where the 9B
is busy at the same time.

**Status: the record of this experiment is incomplete.** The prediction and the baseline's main failure were
written up; the result section was not. The numbers below are the per-run call records (status, refusal reason,
duration) from the run folders, plus which reports were produced. Nothing was graded.

## At a glance

| | |
|---|---|
| **Changed** | gateway mode: `baseline` → `full` (same tasks, same harness build, caches reset) |
| **Baseline** | the `baseline` pass |
| **Success rule** (written before the runs) | baseline expected to **fail** (vLLM waiting > 0 on the 27B, ≥ 30% of calls past target). `full` **passes** if all three reports complete at their solo quality, vLLM waiting stays 0 on the 27B, every lead's first-answer p95 is within 80 s, refusals ≤ 5% with every agent recovering, and within-target beats baseline |
| **Result** | `full` **failed**: of 360 calls, 155 were refused (43%); **every one of the 10-K readers' 144 calls was refused** at admission (429 `deadline_unmeetable`), so the 10-K team produced no report; the SQL lead produced no final report in either pass. Only the coding team finished in both passes |

## Setup

| Setting | This experiment |
|---|---|
| Models | 27B: SQL lead + 8 analysts, 10-K lead, coding lead + 4 coders, summaries. 9B: 8 10-K readers |
| GPU | one H100 80 GB, compute time-shared by the two engines |
| HAMi slices | 47,104 MiB (27B), 31,232 MiB (9B) |
| Slots | 8 per engine |
| KV cache | 361,813 (27B) / 585,075 (9B) tokens |
| Context | 65,536 |
| Mode | `baseline`, then `full` |
| Workload | `workspace/tasks/sql8.md`, `10k8.md`, `code.md`, started together |
| Tenants | 3 |
| Targets | as set in [exp2](exp2.md): lead 4 / 80 / 120 s, analysts and coders 4 / 25 / 60 s, readers 3 / 3 / 35 s |

## How to run it

```bash
bash experiments/exp0_check.sh
bash experiments/mode.sh baseline
# three terminals, started together
cd workspace && ./th sql8 exp2m-baseline-sql
cd workspace && ./th 10k8 exp2m-baseline-10k
cd workspace && ./th code exp2m-baseline-code
# afterwards
bash experiments/finish.sh exp2m-baseline-sql
bash experiments/finish.sh exp2m-baseline-10k
bash experiments/finish.sh exp2m-baseline-code
.venv/bin/python -m analysis.grade results
.venv/bin/python -m analysis.grade_code exp2m-baseline-code

# the same with: bash experiments/mode.sh full  and labels exp2m-full-{sql,10k,code}
```

## Results

### Prediction (written before the runs)

| | baseline | full |
|---|---|---|
| 27B | vLLM waiting peaks 5–8; TTFT p95 15–40 s | excess held in the gateway queue, vLLM waiting 0; leads first; tenants take turns |
| 9B | 8 readers in 8 slots: no waiting, but decode ~2× slower than alone (shared GPU), so long answers miss the 35 s deadline | the same: compute sharing isn't queueing, and the gateway can't fix it |
| Refusals | 0 | a few (`ttft_unmeetable` on queued calls), retried |
| Within target | 50–70% | higher for leads; overall similar or a little higher |
| Time | coding 6–9 min, 10-K 10–14, SQL 15–20 | about the same |
| Quality | coding 15–16/16; 10-K citations all real; SQL fact recall 50–65% | the same |

### The data

From each run folder's call record:

| Run | Window (UTC) | Duration | Calls | Status | Refusals | Report |
|---|---|---|---|---|---|---|
| `exp2m-baseline-sql` | 22:52:04 → 23:18:58 | 26.9 min (stopped) | 324 | 323 × 200, 1 × 499 | 0 | area reports only; no final report |
| `exp2m-baseline-10k` | 22:52:07 → 23:18:23 | 26.3 min | 86 | 86 × 200 | 0 | `10k_report.md` + `.html` |
| `exp2m-baseline-code` | 22:52:10 → 22:58:50 | 6.7 min | 43 | 43 × 200 | 0 | `fix_report.md`: 106 tests pass (52 original, 54 added) |
| `exp2m-full-sql` | 23:46:05 → 00:00:46 | 14.7 min | 163 | 153 × 200, 9 × 429, 1 × 499 | 9 × `ttft_unmeetable` (analysts) | one area report; no final report |
| `exp2m-full-10k` | 23:46:09 → 23:54:49 | 8.7 min (interrupted) | 151 | **7 × 200, 144 × 429** | **144 × `deadline_unmeetable`: every reader call** | none |
| `exp2m-full-code` | 23:46:12 → 23:50:44 | 4.5 min | 46 | 44 × 200, 2 × 429 | 2 × `ttft_unmeetable` (coders) | `fix_report.md`: 98 tests pass |

No screenshots were taken of the `full` pass.

### What we observed

- **Baseline, SQL:** the lead lost its first team's work. An 8-report team result overflowed the 64K lead
  (summarize → re-read → summarize, 4 times), then the lead re-ran the team. It was stopped 27 minutes after the
  start without a final report. The rule for `full`, decided before it ran: stop the SQL session at the same point
  if unfinished, and expect the same failure (it is in the harness, not the gateway).
- **Full, 10-K:** the lead's first 7 calls were served; from the readers' first call (23:47:24) every reader call was
  refused at admission with `deadline_unmeetable`, 144 in all, and the session was interrupted at 23:55 with no
  report.
- **Coding** finished in both passes (6.7 and 4.5 min), within the predicted 6–9 min only in baseline.

### Why it came out this way

- The SQL failure is the harness's: eight analysts' results returned at once exceed what a 64K lead can hold, and
  compaction then loops. It is independent of the gateway mode.
- Why admission judged every reader call unable to meet its 35 s deadline (refusal past 1.5 × 35 = 52.5 s) was not
  analysed in the run record. Admission's deadline estimate is first answer + 2,000 output tokens × the learned
  token gap ([exp2](exp2.md), failure 3); the first things to check are the 9B's learned token gap under the shared
  GPU and how the estimate sizes the readers' output. What is clear is the effect: a class target set for
  short, thinking-off reader calls refused an entire tenant, and its agents had no other engine to go to, the same
  agent-refusal problem as [exp2c](exp2c.md) in its extreme form.

### Errors and surprises: what, why, how to fix

| What happened | Why | How to fix |
|---|---|---|
| The SQL lead lost its team's work in baseline (4 summarize/re-read cycles, then re-ran the team) | An 8-analyst result is too large for a 64K lead's context | In the harness: have members write results to files and return short summaries, or cap what a team result returns inline |
| `full` refused all 144 reader calls; the 10-K tenant produced nothing | Admission's deadline check for `agent-reader`; root cause not established in the run record | Session-aware admission (queue calls from running sessions instead of refusing them); check the reader class's deadline estimate against measured reader calls before a run, as was done for exp2 |
| The result write-up was never finished | — | This page reports only the run folders' call records; treat it as a partial result |

### What we learned

- Larger teams break the lead before they break the server: the first failure was context, not capacity.
- An SLA class mis-sized for its workload refuses a whole tenant at once; replay a workload's calls through
  admission before enabling it (as was done for exp2's targets).
- For agents, refusal is not load shedding: the 10-K team could only stop.

## What this result does and does not show

- It shows two failure modes at a larger scale: a harness failure (the SQL lead's context) and an admission failure
  that refused one tenant entirely.
- It does not give a clean baseline vs full comparison of the gateway: two of the three tenants did not finish in
  `full`, latency and within-target shares were not computed, and nothing was graded. One run per mode.

## Run data

- `results/experiments/exp2m-baseline-sql/`, `exp2m-baseline-10k/`, `exp2m-baseline-code/`
- `results/experiments/exp2m-full-sql/`, `exp2m-full-10k/`, `exp2m-full-code/`

Run folders, agent transcripts and reports are in the shared Drive folder (see the README, "Recordings and run data").
