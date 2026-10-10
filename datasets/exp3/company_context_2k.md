# NovaAI Company Context
Shared operating context for analysts investigating NovaAI's business and platform. It is authoritative for
definitions, policies and schema; if the data disagrees with it, report the discrepancy.

**Data coverage:** 2026-03-01 to 2026-08-31 (six full months, March-August 2026).
**As-of date:** 2026-09-01. "Last month" means August 2026. "This quarter" means Q3 2026 (July-August so far).
All timestamps are UTC and stored as text `YYYY-MM-DD HH:MM:SS`; dates are `YYYY-MM-DD`.

## 1. Company overview
NovaAI sells an enterprise AI platform: customers call NovaAI-hosted LLMs through an HTTPS API and pay a monthly
subscription plus metered usage. GPU inference clusters run in four regions (US-West, US-East, Europe, APAC). ~2,000
customers use Free, Pro, Business or Enterprise plans; Enterprise generates most revenue.

Models: `nova-small` (fast, cheap, general purpose), `nova-reasoning` (large reasoning model that emits hidden thinking
tokens; highest quality, slowest, most expensive), `nova-vision` (multimodal: documents and images).
Endpoints: `chat` (interactive chat), `agents` (multi-step tool-calling agent runs), `embeddings` (vectors, no output
tokens), `document_analysis` (long-document and image understanding).

## 2. Database tables (SQLite)
Eight tables, one row per: `customers` (customer organization), `usage_events` (**sampled** API request, 1-in-1000),
`invoices` (paid customer per billing month), `support_tickets` (ticket), `incidents` (declared incident),
`customer_feedback` (in-product rating), `deployments` (production change), `gpu_cluster_metrics` (region per hour).

Customer-level tables join to `customers` on `customer_id`. Operational tables relate by time window and
region/model/endpoint (e.g. `usage_events.gpu_region = incidents.region`, timestamp between start and end).
Section 4 lists columns, enum values, units and NULL rules; the exact DDL follows it.

## 3. Core metric definitions
- **Revenue** = `SUM(invoices.total_amount)` for the billing month.
- **Requests / volume**: `COUNT(*)` of `usage_events` is a *sampled* count; estimated true volume = sampled count x 1000.
- **Tokens** = `input_tokens + output_tokens` (reasoning output includes thinking tokens).
- **TTFT** (`ttft_ms`): time to first token (queueing + prefill). **TPOT** (`tpot_ms`): mean time per output token after the first.
- **Error rate** = share of requests with `status_code <> 200`. **Availability** = 1 - share of 500/503/504/529 among non-429 requests (429s count as errors, not against availability).
- **Cache hit rate** = `AVG(cache_hit)`. **Churn**: `status = 'churned'`; `churn_date` = effective termination date.
- **Percentiles (P50/P95/P99)** use the nearest-rank rule: sort the n non-NULL values ascending; P(p) is the value at
  0-based position `CAST(n * p AS INT)` (p = 0.5, 0.95, 0.99). SQLite has no percentile function:
```sql
WITH x AS (SELECT ttft_ms AS v FROM usage_events WHERE ttft_ms IS NOT NULL
           AND timestamp >= '2026-07-01' AND timestamp < '2026-08-01')
SELECT v AS p95 FROM x ORDER BY v LIMIT 1 OFFSET (SELECT CAST(COUNT(*) * 0.95 AS INT) FROM x);
```
  Per-group version (window functions): section 10.

## 4. Data dictionary
### customers
- `customer_id` INTEGER PK; `company_name`; `industry`: Finance, Healthcare, Retail, SaaS, Manufacturing, Media, Education, Logistics, Insurance, Government, Legal, Gaming.
- `region` home region: US-West, US-East, Europe, APAC. `plan`: Free, Pro, Business, Enterprise.
- `status`: active, churned, trial (paid plan in its first 21 days; not invoiced). `signup_date` DATE.
- `churn_date` DATE, NULL unless churned. `monthly_contract_value` REAL, USD per month (0 for Free). `seats` INTEGER.

### usage_events (1-in-1000 sample)
- `request_id` `req_YYYYMMDD_<seq>`; `customer_id`; `timestamp` arrival at the gateway.
- `model`: nova-small, nova-reasoning, nova-vision. `endpoint`: chat, agents, embeddings (nova-small only), document_analysis.
- `input_tokens`, `output_tokens` INTEGER; output is 0 for embeddings and failed requests (max 8,192).
- `ttft_ms`, `tpot_ms`, `total_latency_ms` REAL ms. `ttft_ms` is NULL unless `status_code` = 200; `tpot_ms` is also NULL for
  embeddings and `output_tokens` <= 1. `total_latency_ms` is never NULL (429 ~5-40 ms, 500/503/529 ~0.1-2.5 s, 504 = 300000).
- `status_code`: 200 success, 429 rate limited, 500 internal error, 503 unavailable, 504 timeout (300 s), 529 overloaded.
- `gpu_region`: region that served the request (~3% fail over). `cache_hit` 0/1: prompt prefix served from the prefix cache.
- `estimated_cost_usd`: list-price charge in USD (0 for failed requests). `workload_type`: interactive or batch (client flag).

### invoices (no Free/trial invoices; none after the churn month)
- `invoice_id` `INV-YYYYMM-<customer_id, 5 digits>`; `billing_month` DATE, first day of the month ('2026-07-01').
- `base_subscription`: monthly_contract_value prorated by active days. `usage_charge`: 1000 x SUM(estimated_cost_usd) of the customer-month.
- `credits` (SLA and goodwill) and `refunds` are positive USD amounts that are subtracted; credits, then refunds, are capped so an
  invoice never goes negative. `total_amount` = base_subscription + usage_charge - credits - refunds (always holds; >= 0).
- `payment_status`: paid, pending (not yet due), overdue, failed.

### support_tickets
- `ticket_id` `TCK-<6 digits>`; `created_at`; `category`: latency, errors, rate_limiting, billing, account_access, feature_request,
  documentation, integration, model_quality, data_privacy (customer-chosen, often imprecise: read `subject` and `description`).
- `severity`: low, medium, high, critical. `status`: open, pending, resolved, closed. `resolution_time_hours` REAL hours, NULL for open/pending.
- `sentiment`: positive, neutral, negative, very_negative (automated classifier).

### incidents
- `incident_id` `INC-2026-<4 digits>`; `start_time` is the **declaration** time (impact can begin earlier); `end_time`.
- `region` may be `global`; `affected_model` / `affected_endpoint` may be `all`. `severity`: SEV1-SEV4.
  `root_cause` is a short label; `description` and `resolution` are post-incident free text.

### customer_feedback
- `feedback_id` `FB-<6 digits>`; `timestamp`; `rating` 1-5; `category`: quality, latency, reliability, pricing, documentation,
  features, support. `model`, `endpoint`: of the rated interaction. `feedback_text` free text.

### deployments
- `deployment_id` `DEP-<4 digits>`; `deployed_at`; `component`: gateway, router, inference-engine, rate-limiter, kv-cache, billing, api, autoscaler.
- `region` (or `global`); `version`; `change_summary`; `rolled_back` 1 if the change was later rolled back.

### gpu_cluster_metrics (`hour` = 'YYYY-MM-DD HH:00:00')
- `gpu_count`; `avg_gpu_utilization_pct` (mean over workers) and `max_worker_utilization_pct` (busiest worker, always >= the mean),
  both 0-100; a large gap means load imbalance. `kv_cache_utilization_pct`; `avg_queue_depth` (requests waiting for a slot).
- `sampled_requests`: usage_events rows in that hour/region. `prefix_cache_hit_rate` 0-1: share of them with cache_hit=1
  (NULL when `sampled_requests` = 0).
- `prompt_tokens_per_sec`, `generation_tokens_per_sec`: full-volume estimates (sampled tokens x 1000 / 3600).
