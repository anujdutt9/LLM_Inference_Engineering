[← README](../README.md) · experiments: [docs/experiments/](experiments/) · all numbers: [RESULTS.md](RESULTS.md)

# Models and memory

Which models run, on which GPU, and how the 80 GB of GPU memory is divided between weights and KV cache. Every
number on this page was measured on the box (vLLM's boot log, `nvidia-smi`, the gateway's records) unless it is
marked as a spec or as arithmetic.

## The hardware

| | |
|---|---|
| Instance | Lambda, 1× NVIDIA H100 80 GB **SXM5** (HBM3, 81,559 MiB as `nvidia-smi` reports it), 26 vCPU, 221 GiB RAM, 2.7 TB disk, $4.29/hr |
| Driver / CUDA | 580.105.08 / 13.0, MIG disabled |
| Kubernetes | k3s v1.36.5+k3s1, Helm 3.22 |
| GPU sharing | HAMi 2.9.0 (memory slices per pod, compute shared) |
| Engine | vLLM 0.30.0 (`vllm/vllm-openai:v0.30.0`) |
| Memory bandwidth | 3.35 TB/s (H100 80 GB HBM3 SXM5 spec) |

**Why one H100.** The project is about the serving layer (admission, queueing, placement, isolation between
tenants), so one GPU is enough to create contention, and it keeps every claim honest: nothing here is hardware
scale-out. One 80 GB card holds a 27B FP8 model and a 9B BF16 model side by side with KV cache to spare (Setup A),
or two 9B replicas (Setup B). vLLM 0.30.0 is the version used because it is the first with support for these
Qwen3.5 / Qwen3.8 models, current metric names and the `reasoning` field.

## The models

| | Setup A (`FLEET=27b-9b`) | | Setup B (`FLEET=two-9b`) |
|---|---|---|---|
| Deployment | `vllm-27b` | `vllm-9b` | `qwen-9b-a`, `qwen-9b-b` |
| Model | `Qwen/Qwen3.8-27B-FP8` | `Qwen/Qwen3.5-9B` (BF16) | `Qwen/Qwen3.5-9B` (BF16), twice |
| Architecture | hybrid: Gated DeltaNet (linear attention) + full attention (`Qwen3_5ForConditionalGeneration`) | hybrid, same family | hybrid, same family |
| Role | leads, SQL analysts, coders: the agents that think | 10-K readers (thinking off), the permission classifier | everything (capacity and placement experiments) |
| Download | 30.0 GB | 16.6 GB | 16.6 GB |

**Why these two in Setup A.** The fleet is meant to carry two different inference shapes at once: decode-heavy
agents that think and write (the SQL and coding teams) and prefill-heavy readers that read ~10K-token filings and
answer briefly. The 27B is the stronger model for the first kind: as a lead it checked its analysts' numbers
against the filings itself and made no wrong number (113/113 citations real), where a 9B lead let one wrong and two
missing numbers through ([exp1b](experiments/exp1b.md)); as a coder at `medium` thinking it fixed 16/16 items,
including all four bugs no test catches ([exp1c](experiments/exp1c.md)). The 9B with thinking off is fast and
grounded for reading (3.1 min, every citation real). Both are the same family, with the same tokenizer family and
the same tool-call and reasoning parsers, so one gateway and one harness config serve both.

**Why two 9Bs in Setup B.** The scale-out experiments need two *identical* replicas. Two 27B FP8 engines (~28 GiB
of weights each) would leave almost no room for KV cache on 80 GB. The 9B was already in the fleet, and its hybrid
attention makes KV cheap.

**Context window.** Both models list `max_position_embeddings` 262,144. We serve `--max-model-len 65536`: the 64K
limit is a KV-memory decision, not the model's (below).

## How memory is split: HAMi slices

HAMi gives each vLLM pod a **memory slice** (`nvidia.com/gpumem`):

- **The slice is a hard wall.** Inside the pod, CUDA (and `nvidia-smi`) sees the slice as the GPU's total memory.
  Exp-0 checks this: the 27B pod reports **47,104 MiB** total, the 9B pod **31,232 MiB**, not 80 GB.
- **`--gpu-memory-utilization` is a fraction of the slice**, not of the card. Both engines run at 0.95, so each
  vLLM budgets 95% of its slice.
- **Compute is not partitioned.** No `gpucores` cap is set: an engine running alone gets the whole GPU; two engines
  running at once time-share it. This is visible in the results: in Exp-2 the 9B's token gap went from 8.6 to
  17.8 ms while the 27B was busy, and no gateway control can see or fix that.
- **Why per-model slices and not an even split.** Each model needs its weights plus enough KV for its workload,
  and weights take most of a slice (27.6 of the 27B's ~43.7 GiB budget). The slice sizes are a capacity decision
  per model.
- **Why HAMi and not MIG.** HAMi caps memory per pod at any size and leaves compute shared. MIG on an H100 would
  give fixed halves with separate SMs and bandwidth, with no way to give one engine the whole GPU when the other is
  idle.

| | Setup A | Setup B |
|---|---|---|
| Slices | 47,104 MiB (46 GiB, 27B) + 31,232 MiB (30.5 GiB, 9B) = 78,336 MiB | 2 × 38,912 MiB (38 GiB) = 77,824 MiB |
| Card total | 81,559 MiB | 81,559 MiB |
| Left in no slice | 81,559 − 78,336 = 3,223 MiB ≈ 3.1 GiB | 81,559 − 77,824 = 3,735 MiB ≈ 3.6 GiB (driver, DCGM, margin) |

With both Setup A engines serving, `nvidia-smi` showed **70,909 MiB of 81,559** in use: the 27B process held
42,612 MiB (90% of its slice), the 9B 28,282 MiB (91%). About 10 GB of the card stayed free: the margin against
either engine overrunning.

## Inside a slice: weights, then KV cache

vLLM loads the weights, profiles a forward pass, sets aside its CUDA-graph pool, and turns what is left of its
budget into KV cache: the memory that holds every running conversation's past tokens.

### The measured boot numbers (Setup A)

| | **27B FP8** | **9B BF16** |
|---|---|---|
| HAMi slice | 47,104 MiB (46 GiB) | 31,232 MiB (30.5 GiB) |
| vLLM budget (0.95 of the slice) | ~43.7 GiB | ~29.0 GiB |
| Weights | **27.64 GiB**, loaded in 14 s | **16.8 GiB**, loaded in 7 s |
| KV cache memory | **12.72 GiB** | **9.93 GiB** |
| KV cache size | **361,813 tokens** | **585,075 tokens** |
| KV memory per token | ~37.7 KB | ~18.2 KB |
| Max concurrency at 65,536 tokens | **5.52×** | **8.93×** |
| CUDA graph pool (actual / reserved) | 0.33 / 0.58 GiB | 0.05 / 0.08 GiB |
| KV block size (tokens) | 1,568 | 1,056 |
| Engine init | 235 s (compile 37 s) | 41 s (compile 16 s) |

### The arithmetic, step by step

**1. The budget.** Slice × 0.95:

- 27B: 46 GiB × 0.95 = **43.7 GiB**
- 9B: 30.5 GiB × 0.95 = **29.0 GiB**

**2. What is left after the weights.** Budget − weights:

- 27B: 43.7 − 27.64 = 16.06 GiB, of which vLLM made **12.72 GiB** KV cache. The other ~3.3 GiB is vLLM's own
  overhead (the profiled forward pass and the CUDA-graph pool, 0.58 GiB reserved); the boot log does not itemize
  the rest.
- 9B: 29.0 − 16.8 = 12.2 GiB, of which **9.93 GiB** KV cache (~2.3 GiB overhead).

**3. KV memory per token.** KV bytes ÷ KV tokens (1 GiB = 1,073,741,824 bytes):

- 27B: 12.72 GiB = 13.66 × 10⁹ bytes; ÷ 361,813 tokens ≈ **37.7 KB per token**
- 9B: 9.93 GiB = 10.66 × 10⁹ bytes; ÷ 585,075 tokens ≈ **18.2 KB per token**
- Setup B (KV fixed at 4.5 GiB per replica): 4.5 GiB = 4.83 × 10⁹ bytes; ÷ 264,993 tokens ≈ **18.2 KB per token**,
  the same figure as the 9B in Setup A, as it should be.

So the smaller 9B holds **1.6× the tokens** of the 27B (585,075 vs 361,813) in less memory, because each token
costs about half as much (18.2 vs 37.7 KB).

**4. Max concurrency at the full context.** KV tokens ÷ 65,536:

- 27B: 361,813 ÷ 65,536 = **5.52** requests at a full 64K context
- 9B: 585,075 ÷ 65,536 = **8.93**
- Setup B replica: 264,993 ÷ 65,536 = **4.04**
- Setup B memory demo (KV 1.25 GiB = 73,134 tokens): 73,134 ÷ 65,536 = **1.12**, the smallest KV vLLM accepts with
  a 64K context (it needs room for at least one full-length request)

This is the worst case. Real agent turns are shorter: 361,813 tokens is, for example, 12 agents at 30K tokens each.

**5. The minimum time per decode step.** Each decode step reads every weight once, whatever the batch size, so
weight bytes ÷ memory bandwidth is a floor on the token gap (ITL):

| | Weights | ÷ 3.35 TB/s | Measured token gap | Share of peak bandwidth |
|---|---|---|---|---|
| 27B FP8 | 27.64 GiB = 29.7 GB | **≈ 8.9 ms** | ~13 ms (alone, Exp-1) | ~68% |
| 9B BF16 | 16.8 GiB = 18.0 GB | **≈ 5.4 ms** | ~7–8.6 ms (7 ms in Setup B at batch 1–8; 8.6 ms with the 10-K team alone) | ~63–77% |

The measured gaps sit above the floor because each step also reads the running requests' KV and runs the
non-matmul work. Two practical consequences: each 27B sequence decodes about 1.5–1.9× slower than a 9B one (13 ms vs 7–8.6 ms),
and on one GPU a second engine adds **slots, not speed** (Setup B: the token gap went from 7 ms to 14–17 ms with
two replicas taking turns).

### Why the KV per token is small on these models

Qwen3.5 / Qwen3.8 are **hybrid** models. Most layers are **Gated DeltaNet** (a linear-attention, "Mamba-style"
layer) that keeps a fixed-size state per conversation, however long it gets. Only a minority of layers are normal
full attention, whose KV grows with every token. And the KV cache is stored in **FP8** (`--kv-cache-dtype fp8`),
half the bytes of BF16.

For the 9B the project's notes give the split: 8 of its 32 layers are full attention, the other 24 Gated DeltaNet
with a fixed state of ~49 MiB per sequence; in FP8 the attention KV is 16 KiB per token, and vLLM's pool works out
to the measured ~18 KB per token (the difference is taken to be vLLM's per-block accounting for the hybrid layers;
not verified). For the 27B the sources do not record the layer split, so its **~37.7 KB per token is the measured
figure from vLLM's boot log** (12.72 GiB ÷ 361,813 tokens), not derived from the architecture.

Two side effects of the hybrid design, both measured:

- **Large KV blocks.** vLLM stores both layer types in one pool of equal-sized pages, padding one to fit the other
  (`Padding mamba page size by 0.13%` on the 27B, 0.76% on the 9B). The resulting block is **1,568 tokens (27B)
  and 1,056 tokens (9B)**, far larger than the usual 16.
- **Block-granular prefix caching.** Prefix-cache reuse comes in whole blocks. Exp-0 measured it on a repeated
  8,678-token prompt: the 27B reused 7,840 tokens = 5 × 1,568, the 9B 8,448 = 8 × 1,056. A shared prefix shorter
  than one block (for example a 1,000-token system prompt on the 27B) gets no reuse at all. The gateway's
  cache-aware placement hashes prompts in blocks of ~1K tokens for the same reason.

### Small costs and leftovers

- **CUDA graphs.** vLLM reserved 0.58 GiB on the 27B for them and used 0.33 GiB, so ~0.25 GiB (≈ 7K tokens of KV,
  2%) sat idle; `--gpu-memory-utilization 0.9627` would reclaim it.
- **Uncalibrated FP8 KV.** `Using KV cache scaling factor 1.0 for fp8_e4m3`: the checkpoint has no calibrated KV
  scales. A small expected quality cost; the graders measure quality on every run.
- **Boot time.** The 27B is ready 235 s after its weights load (profiling, compile, CUDA graphs); the 9B in 41 s. A
  redeploy of the 27B (for example to change a flag) costs about 5 minutes.

## How the KV size sets the limits

| Limit | Value | Why |
|---|---|---|
| Context per request (`--max-model-len`) | **65,536** | The 27B's 361,813 KV tokens hold 5.5 full-length requests at 64K. A 128K window would halve that; the agent workloads never needed more than 64K. |
| Running requests per engine (`--max-num-seqs`) | **8** | Above the 5.52× worst case, because real turns are shorter than 64K; the gateway mirrors the 8 slots and queues the rest itself. In Setup A the limit that bit under load was always the slots, never KV (Exp-2c: KV ≤ 54%, 0 preemptions). |
| Chunked prefill budget (`--max-num-batched-tokens`) | 8,192 | So a 10K-token prompt does not stall the other calls' decode. |
| `max_tokens` per call, lead and subagents | **32,768** | One ceiling for every agent; the roles differ in how it is split. Lead: thinking at most 16,384, and at least 16,384 kept for the answer (a whole md + html report in one reply). Subagents: thinking at most 12,288, 4,096 kept for the answer. 10-K readers: thinking off. |
| `max_tokens`, compaction summaries | 8,192 | Thinking off. |
| `max_tokens`, permission classifier | 1,024 | Thinking off. |
| Compaction trigger | **65,536 − 32,000 = 33,536 tokens** (33.5K) | The harness compacts once the history leaves less room than the model's output limit (`usable = window − min(output limit, 32,000)`), so the lead's report always fits. |
| Kept word for word after a summary | a quarter of usable, within 2,000–15,000: **~8.4K** | Whole turns from the end, never starting at a tool result. |
| Largest single message (one Read) | half of usable: **16K** | Leaves room for a whole ~10K-token filing; larger outputs keep their start and end, with the whole saved to a file. |
| Context fit at the gateway | when prompt + `max_tokens` > 65,536, `max_tokens` is lowered to the room left; under 1,024 tokens of room the call is refused with 400 `context_full` | Turns a doomed request into a working one instead of relaying the engine's 400. |

In Setup B the KV cache is **fixed at 4.5 GiB per replica** (`--kv-cache-memory-bytes`, 264,993 tokens), not the
~1M tokens a 38 GiB slice could hold, so that memory pressure appears at realistic loads; 8 slots per replica;
and a memory demo shrinks KV to **1.25 GiB ≈ 73K tokens** to force preemptions.

## Where these numbers come from

- vLLM's boot log on each engine (weights, KV memory and tokens, max concurrency, block size, CUDA graphs, init
  time), recorded during bring-up on 2026-10-03.
- `nvidia-smi` on the host and inside each pod (the HAMi view), collected by `experiments/snapshot.sh`.
- The memory bandwidth (3.35 TB/s) is the H100 80 GB HBM3 SXM5 specification.
- Manifests: [`k8s-config/hami/hami-27b-9b.yaml`](../k8s-config/hami/hami-27b-9b.yaml) (Setup A),
  [`k8s-config/hami/hami-demo.yaml`](../k8s-config/hami/hami-demo.yaml) (Setup B); per-role token limits in
  [`gateway/config.yaml`](../gateway/config.yaml) (`reasoning_by_role`).
