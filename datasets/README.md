# Datasets

Everything the agents and the load generator read. The agents themselves work in `workspace/`
(`workspace/data/` holds the same files, and `workspace/data/nova_ai.db` links to the database here).

| Folder | Used by | Contents |
|---|---|---|
| `exp1a/` | SQL team (tenant A), Locust, grading | `nova_ai.db.xz` (the NovaAI database, unpacked by `make data`), `company_context_8k.md` (the shared prefix every agent gets), `benchmark_questions.jsonl` (49 questions with expected facts and verification SQL, used for grading) |
| `exp1b/filings/` | 10-K team (tenant B), grading | The latest 10-K of NVIDIA, AMD, Intel and Broadcom, each trimmed to ~10K tokens with `[ticker-section-pNNN]` citation anchors (`*.md`), the anchor index the grader checks citations against (`index.json`), and where each filing came from (`manifest.json`) |
| `code/` | coding team (tenant C), grading | `ledger_fixed/`: the reference solution of the billing library the coders repair (`workspace/code/ledger/` is the broken copy they get); `hidden_tests/`: extra tests per planted bug, so a fix that only satisfies the visible tests is caught |
| `exp2/` | two tenants at once | links to `exp1a` and `exp1b` (the same inputs as the solo runs) |
| `exp3/` | Locust load | `benchmark_questions.jsonl`, `company_context_2k.md` (the system prompt of every Locust call), `company_context_8k.md` |

## The NovaAI database

A synthetic, seeded dataset for an "AI SaaS operations analyst" workload: NovaAI is a fictional LLM-API
company (March–August 2026). SQLite, 8 tables: `customers` (2,000), `usage_events` (~245K, a 1-in-1000
sample of requests), `invoices`, `support_tickets`, `incidents`, `customer_feedback`, `deployments` and
`gpu_cluster_metrics` (hourly, 4 regions), with planted stories the benchmark questions ask about.

```bash
make data                       # = python3 datasets/unpack.py: nova_ai.db.xz (11 MB) → nova_ai.db (84 MB), sha256 checked
sqlite3 -readonly -header -column datasets/exp1a/nova_ai.db "SELECT plan, COUNT(*) FROM customers GROUP BY plan"
```

`sha256(nova_ai.db) = c5c7132aceeff6fecf653b2a89414240bd5abf848b7ecbe32ce072683a90b1a7`

## The 10-K filings

`app/agents/prep_10k.py` made the trimmed filings from each company's published 10-K (the source URLs
are in `exp1b/filings/manifest.json`). The original PDFs (~80 MB) are not in the repo; to re-run the
trimming, download them from those URLs, extract their text to `exp1b/filings/raw/<ticker>_10k_<fy>.txt`,
and run `python -m app.agents.prep_10k`.
