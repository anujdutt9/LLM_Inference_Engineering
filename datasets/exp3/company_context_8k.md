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

## Appendix to section 4: exact DDL
Exact DDL of the SQLite database:
```sql
CREATE TABLE customers (
    customer_id            INTEGER PRIMARY KEY,
    company_name           TEXT NOT NULL,
    industry               TEXT NOT NULL,
    region                 TEXT NOT NULL CHECK (region IN ('US-West','US-East','Europe','APAC')),
    plan                   TEXT NOT NULL CHECK (plan IN ('Free','Pro','Business','Enterprise')),
    signup_date            DATE NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('active','churned','trial')),
    monthly_contract_value REAL NOT NULL,
    seats                  INTEGER NOT NULL,
    churn_date             DATE
);
CREATE TABLE usage_events (
    request_id         TEXT PRIMARY KEY,
    customer_id        INTEGER NOT NULL REFERENCES customers(customer_id),
    timestamp          DATETIME NOT NULL,
    model              TEXT NOT NULL,
    endpoint           TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    ttft_ms            REAL,
    tpot_ms            REAL,
    total_latency_ms   REAL NOT NULL,
    status_code        INTEGER NOT NULL,
    gpu_region         TEXT NOT NULL,
    cache_hit          INTEGER NOT NULL CHECK (cache_hit IN (0,1)),
    estimated_cost_usd REAL NOT NULL,
    workload_type      TEXT NOT NULL CHECK (workload_type IN ('interactive','batch'))
);
CREATE TABLE invoices (
    invoice_id        TEXT PRIMARY KEY,
    customer_id       INTEGER NOT NULL REFERENCES customers(customer_id),
    billing_month     DATE NOT NULL,
    base_subscription REAL NOT NULL,
    usage_charge      REAL NOT NULL,
    credits           REAL NOT NULL,
    refunds           REAL NOT NULL,
    total_amount      REAL NOT NULL,
    payment_status    TEXT NOT NULL CHECK (payment_status IN ('paid','pending','overdue','failed'))
);
CREATE TABLE support_tickets (
    ticket_id             TEXT PRIMARY KEY,
    customer_id           INTEGER NOT NULL REFERENCES customers(customer_id),
    created_at            DATETIME NOT NULL,
    category              TEXT NOT NULL,
    severity              TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    subject               TEXT NOT NULL,
    description           TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('open','pending','resolved','closed')),
    resolution_time_hours REAL,
    sentiment             TEXT NOT NULL
);
CREATE TABLE incidents (
    incident_id       TEXT PRIMARY KEY,
    start_time        DATETIME NOT NULL,
    end_time          DATETIME NOT NULL,
    region            TEXT NOT NULL,
    affected_model    TEXT NOT NULL,
    affected_endpoint TEXT NOT NULL,
    severity          TEXT NOT NULL CHECK (severity IN ('SEV1','SEV2','SEV3','SEV4')),
    root_cause        TEXT NOT NULL,
    description       TEXT NOT NULL,
    resolution        TEXT NOT NULL
);
CREATE TABLE customer_feedback (
    feedback_id   TEXT PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    timestamp     DATETIME NOT NULL,
    rating        INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    category      TEXT NOT NULL,
    model         TEXT,
    endpoint      TEXT,
    feedback_text TEXT NOT NULL
);
CREATE TABLE deployments (
    deployment_id  TEXT PRIMARY KEY,
    deployed_at    DATETIME NOT NULL,
    component      TEXT NOT NULL,
    region         TEXT NOT NULL,
    version        TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    rolled_back    INTEGER NOT NULL CHECK (rolled_back IN (0,1))
);
CREATE TABLE gpu_cluster_metrics (
    hour                       DATETIME NOT NULL,
    region                     TEXT NOT NULL,
    gpu_count                  INTEGER NOT NULL,
    avg_gpu_utilization_pct    REAL NOT NULL,
    max_worker_utilization_pct REAL NOT NULL,
    kv_cache_utilization_pct   REAL NOT NULL,
    avg_queue_depth            REAL NOT NULL,
    prefix_cache_hit_rate      REAL,
    sampled_requests           INTEGER NOT NULL,
    prompt_tokens_per_sec      REAL NOT NULL,
    generation_tokens_per_sec  REAL NOT NULL,
    PRIMARY KEY (hour, region)
);
```

## 5. Model catalog
| Model | Typical use | Context window | Relative quality | Typical TTFT (short prompt) | Typical TPOT |
|---|---|---|---|---|---|
| nova-small | chat, classification, extraction, embeddings | 32K | good on simple tasks, weaker on multi-step reasoning | ~100-200 ms | ~9 ms |
| nova-reasoning | complex analysis, agents, planning | 128K | best | ~300-600 ms (grows with prompt length) | ~26 ms |
| nova-vision | documents, images, charts | 128K | good | ~250-500 ms | ~18 ms |

- nova-reasoning produces hidden "thinking" tokens that are billed as output tokens; typical outputs are 4-6x longer than nova-small.
- Prefill cost grows roughly linearly with prompt length; long prompts (>16K tokens) dominate TTFT.
- Prefix caching skips prefill for the cached part of the prompt, typically cutting prefill time by ~70%.

## 6. Plans and pricing
| Plan | Monthly fee | Seats | Rate limit (RPM) | Latency SLA | Availability SLA | Support |
|---|---|---|---|---|---|---|
| Free | $0 | 1-2 | 20 | none | none | community |
| Pro | $49-$499 | 1-10 | 120 | none | none | email, 2 business days |
| Business | ~$1.2K-$9K | 10-150 | 600 | none | 99.5% | email+chat, 8 business hours |
| Enterprise | ~$10K-$165K+ (negotiated) | 100-5000 | custom (>= 3000) | yes (see SLA) | 99.9% | 24/7, named TAM |

**Usage list prices (USD per 1M tokens)**

| Model | Input | Cached input | Output |
|---|---|---|---|
| nova-small | 0.15 | 0.075 | 0.60 |
| nova-reasoning | 3.00 | 1.50 | 15.00 |
| nova-vision | 1.25 | 0.625 | 5.00 |
| embeddings (nova-small) | 0.02 | 0.01 | - |

For a cache hit, all input tokens of the request are billed at the cached rate (simplification used by billing).
Failed requests (non-200) are not billed. Usage is invoiced monthly in arrears on top of the subscription.

## 7. Enterprise SLA
Enterprise customers are entitled to, per customer per calendar month:
- **Availability >= 99.9%**: 1 - (requests with status 500/503/504/529) / (all non-429 requests), over interactive requests.
- **P95 TTFT < 2,000 ms** for successful interactive `chat` and `agents` requests with prompts <= 32K tokens.
- **P95 TTFT < 8,000 ms** for successful interactive `document_analysis` requests.
- Batch workloads (`workload_type = 'batch'`) are excluded from all SLAs. P95 uses the nearest-rank rule of section 3.
- On the 1:1000 sampled log, an SLA is evaluated for a customer-month only when that SLA's population has >= 20 sampled requests.

**SLA credits**: 10% of the month's base subscription for each month in which any SLA is breached (one credit per breached
month, however many SLAs were breached). Credits are applied to the invoice of the month in which the breach occurred.
Availability is monitored automatically; latency SLA credits are applied by the SLA team when monitoring or a customer claim detects the breach.
Business plan has an availability target (99.5%) but no contractual credits; support may grant goodwill credits (typically 5%).

## 8. Incident severity definitions
| Severity | Definition | Example | Response target |
|---|---|---|---|
| SEV1 | Major outage or severe degradation of a core model/endpoint in a region, SLA breach likely for many Enterprise customers | error rate > 5% or TTFT > 3x baseline for > 1 hour | page on-call immediately, exec comms |
| SEV2 | Significant degradation affecting a subset of customers or one endpoint | elevated 429/5xx on one endpoint | page on-call |
| SEV3 | Minor or partial degradation, workaround exists, or slow-burn performance regressions | p99 latency regression | next business day |
| SEV4 | Negligible customer impact; internal only | brief blip, auto-recovered | backlog |

## 9. Interpretation guidelines
1. Distinguish **correlation** from **established cause**. An incident, deployment or ticket overlapping in time is evidence, not proof.
   Strong causal claims need a mechanism (e.g. deployment -> metric change -> customer impact) and a matching scope (same region, model, endpoint, window).
2. Always segment before concluding: by plan, region, model, endpoint, workload_type, prompt length bucket.
   Aggregate metrics can hide effects concentrated in a segment (e.g. only long prompts, only one region, only the tail).
3. Compare against a baseline period of similar length and weekday mix. Weekends have ~45% lower traffic.
4. Report tail latency (P95/P99) alongside P50. Queueing problems show up in the tail first.
5. Volume in `usage_events` is sampled 1:1000. Report sampled counts and, when relevant, estimated true volume.
6. Revenue questions should use `invoices`; usage questions should use `usage_events`. Explain the difference if both are used.
7. Ticket `category` is customer-chosen; classify from text when precision matters.
8. Incident `start_time` is the declaration time; true impact may start earlier. Check usage_events and gpu_cluster_metrics.
9. Quantify: include numbers, dates, customer names/IDs and regions in every finding.

## 10. SQL conventions and tips
- Month bucket: `substr(timestamp,1,7)` -> '2026-07'. Day: `date(timestamp)`. Hour: `strftime('%H', timestamp)`.
- Invoices: `billing_month` is '2026-07-01' for July.
- Boolean: `cache_hit` is 0/1, so `AVG(cache_hit)` is the hit rate.
- Error rate: `AVG(status_code <> 200)`.
- Percentiles use the nearest-rank rule of section 3 (0-based position `CAST(n * p AS INT)` in ascending order).
  Single value: `ORDER BY v LIMIT 1 OFFSET (SELECT CAST(COUNT(*) * 0.95 AS INT) FROM x)` (section 3).
  Per group, with window functions (identical results):
```sql
WITH s AS (SELECT gpu_region AS g, ttft_ms AS v FROM usage_events
           WHERE model='nova-reasoning' AND ttft_ms IS NOT NULL
             AND timestamp >= '2026-07-01' AND timestamp < '2026-08-01'),
r AS (SELECT g, v, ROW_NUMBER() OVER (PARTITION BY g ORDER BY v) - 1 AS rn,
             COUNT(*) OVER (PARTITION BY g) AS n FROM s)
SELECT g, MAX(n) AS n,
       MAX(CASE WHEN rn = CAST(n * 0.5 AS INT) THEN v END) AS p50,
       MAX(CASE WHEN rn = CAST(n * 0.95 AS INT) THEN v END) AS p95,
       MAX(CASE WHEN rn = CAST(n * 0.99 AS INT) THEN v END) AS p99
FROM r GROUP BY g ORDER BY g;
```
- `usage_events` has ~245K rows; always filter by time and aggregate in SQL instead of fetching raw rows.
- Useful indexes exist on timestamp, (customer_id, timestamp), (model, timestamp), (gpu_region, timestamp), status_code.

## 11. Serving architecture
NovaAI's request path is: client SDK -> global API gateway (auth, rate limiting, request logging) -> regional
router -> model worker pool (GPU nodes running a continuous-batching inference engine) -> streaming response.

**Gateway.** Terminates TLS, authenticates API keys, enforces per-organization rate limits via the rate-limiter
service, attaches request metadata and forwards to the customer's home region. Rejected requests return 429.
The gateway also enforces the 300-second request timeout (504) and returns 529 when the region reports that
all worker queues are above their admission threshold.

**Router.** Each region has a router that assigns requests to workers in the model's pool. Policies include
round-robin, least-outstanding-requests and weighted policies. A healthy router keeps the max-worker utilization
within ~5-10 percentage points of the pool average. Routing changes are deployed as `router` deployments.

**Inference engine.** Workers run a continuous-batching engine (vLLM-style) with paged KV-cache. Scheduling
interleaves prefill (processing the prompt) and decode (generating tokens). Long prompts are processed with
*chunked prefill*: each scheduler step processes at most a fixed budget of prompt tokens so that ongoing decodes
are not starved. The size of this budget trades long-prompt TTFT against decode smoothness.

**KV-cache and prefix caching.** Each running sequence holds KV-cache blocks proportional to its length. When
KV-cache fills, new requests wait in the queue and long sequences may be preempted. Prefix caching reuses KV blocks
for identical prompt prefixes (system prompts, tool schemas, shared documents), skipping their prefill. Hit rates
depend on how much prompt structure customers share across requests and on cache capacity and eviction policy.

**Batch traffic.** Customers can flag requests as batch (`workload_type='batch'`). Historically batch and
interactive requests share the same worker pools and queues.

**Regions and capacity.**
| Region | GPUs (H100-class) | Local time offset used for business hours |
|---|---|---|
| US-West | 256 | UTC-7 |
| US-East | 256 | UTC-4 |
| Europe | 192 | UTC+1 |
| APAC | 128 | UTC+8 |
Traffic peaks during local business hours (09:00-17:00) and drops ~45% on weekends.

## 12. Error code reference
| Code | Meaning | Typical cause | Billed | Counts against availability |
|---|---|---|---|---|
| 200 | Success | - | yes | - |
| 429 | Too Many Requests | per-org rate limit exceeded, or rate-limiter misconfiguration | no | no |
| 500 | Internal Server Error | worker crash, bug, dependency failure | no | yes |
| 503 | Service Unavailable | regional dependency down, network, maintenance | no | yes |
| 504 | Gateway Timeout | request exceeded 300 s (very long generations or extreme queueing) | no | yes |
| 529 | Overloaded | admission control: region queues over threshold (capacity) | no | yes |

Clients are expected to retry 429/529/503 with exponential backoff. Sustained 529s indicate capacity or routing problems;
sustained 429s with unchanged client traffic indicate rate-limiter problems.

## 13. Support policies
- **Channels**: Free - community forum; Pro - email; Business - email and chat; Enterprise - 24/7 phone, chat, email and a named Technical Account Manager (TAM).
- **First-response targets**: critical 1 h (Enterprise) / 4 h (Business); high 4 h / 8 business h; medium 1 business day; low 2 business days.
- **Resolution** is measured from ticket creation until status becomes resolved/closed.
- **Escalation**: Enterprise tickets marked critical, or three or more latency/error tickets from the same Enterprise customer in 30 days,
  must be escalated to the TAM and engineering on-call. Repeated escalations trigger an account-health review.
- **Account-health review** signals: declining usage over 4+ weeks, 3+ high/critical tickets in 30 days, average feedback rating <= 2,
  overdue invoices, or explicit mentions of alternatives/cancellation in tickets.
- **Customer communications** during SEV1/SEV2 incidents are posted on the status page and sent to Enterprise contacts.

## 14. Credit and refund policy
- **SLA credits** (Enterprise): 10% of base subscription per breached month (one credit per month). Customers need not request credits;
  the SLA team applies them when monitoring detects a breach, or after review of a customer claim.
- **Goodwill credits**: support may grant 2-8% of base subscription for poor experiences not covered by an SLA (e.g. Business plan incidents).
  Incidents affecting many Business customers may trigger a blanket 5% goodwill credit.
- **Refunds**: issued for billing errors (duplicate charges), and for disputed usage charges on failed or severely degraded requests.
  Refunds are recorded on the invoice of the month in which they are approved.
- **Proration**: base subscription is prorated by active days in the month of signup or churn.
- **Floor**: credits, then refunds, are capped so that an invoice total is never negative.
- **Termination**: Enterprise contracts can be terminated with notice; the churn_date is the effective end of service.

## 15. Analysis playbooks
**Revenue change investigation**
1. Decompose revenue change by component (base, usage, credits, refunds) and by plan, region and customer.
2. Identify the customers with the largest absolute change; check churn, contract changes and credits.
3. For affected customers, look back 4-8 weeks in usage, tickets and feedback for leading indicators.
4. Check incidents and deployments that overlap in time and scope.

**Latency investigation**
1. Compute P50/P95/P99 TTFT and TPOT by day, region, model, endpoint and prompt-length bucket.
2. Separate queueing (tail-heavy, affects all prompt lengths) from prefill regressions (scales with prompt length) and decode regressions (TPOT).
3. Correlate with gpu_cluster_metrics (utilization, max-worker gap, KV-cache utilization, queue depth) and with deployments.
4. Check whether a single tenant or workload_type dominates traffic in the affected window.

**Customer satisfaction investigation**
1. Trend average rating by model and category; check whether the category mix shifted (e.g. more latency/reliability feedback).
2. Separate quality ratings from latency/reliability/pricing ratings before concluding that model quality changed.
3. Read ticket and feedback text for recurring themes; quote representative examples.

**Churn investigation**
1. List churned customers with contract value, churn_date, region and plan.
2. Build a timeline per customer: usage trend, latency experienced, tickets, feedback, credits, invoice status.

## 16. Prompt-length buckets and workload shapes
Standard buckets for analysis: `<1K`, `1K-4K`, `4K-16K`, `16K-32K`, `>32K` input tokens.
Typical shapes (medians): chat ~900 input / 180-900 output; agents ~3.5K input / 250-1,200 output; embeddings ~350 input;
document_analysis ~7K input / 300-1,100 output. Output length depends strongly on model: nova-reasoning outputs include thinking tokens.
Token length distributions are heavy-tailed; use medians and percentiles rather than means.

## 17. Metric calculation cookbook
**Availability for a customer-month (SLA definition)**
```sql
SELECT customer_id, COUNT(*) AS n,
       1.0 - AVG(status_code IN (500,503,504,529)) AS availability
FROM usage_events
WHERE status_code <> 429 AND workload_type = 'interactive'
  AND timestamp >= '2026-06-01' AND timestamp < '2026-07-01'
GROUP BY customer_id HAVING COUNT(*) >= 20;
```

**Interactive SLA population for P95 TTFT**
```sql
SELECT customer_id, ttft_ms FROM usage_events
WHERE status_code = 200 AND workload_type = 'interactive'
  AND endpoint IN ('chat','agents') AND input_tokens <= 32000;
```

**Net revenue retention (month over month)**
```sql
WITH a AS (SELECT customer_id, total_amount FROM invoices WHERE billing_month='2026-06-01'),
     b AS (SELECT customer_id, total_amount FROM invoices WHERE billing_month='2026-07-01')
SELECT SUM(COALESCE(b.total_amount,0)) / SUM(a.total_amount) AS nrr
FROM a LEFT JOIN b USING(customer_id);
```

**Logo churn rate for a month**
```sql
SELECT CAST(SUM(churn_date >= '2026-07-01' AND churn_date < '2026-08-01') AS REAL)
       / SUM(signup_date < '2026-07-01' AND (churn_date IS NULL OR churn_date >= '2026-07-01'))
FROM customers;
```

**Average revenue per paying account (ARPA)**
```sql
SELECT billing_month, SUM(total_amount)/COUNT(DISTINCT customer_id) FROM invoices GROUP BY 1;
```

**Cost per 1K tokens by model**
```sql
SELECT model, 1000.0*SUM(estimated_cost_usd)/SUM(input_tokens+output_tokens)
FROM usage_events WHERE status_code=200 GROUP BY model;
```

**Prefix-cache effect on TTFT at similar prompt lengths**
```sql
SELECT model, cache_hit, ROUND(AVG(ttft_ms),1) FROM usage_events
WHERE status_code=200 AND input_tokens BETWEEN 2000 AND 6000 GROUP BY 1,2;
```

**Load-imbalance indicator**
```sql
SELECT hour, region, max_worker_utilization_pct - avg_gpu_utilization_pct AS imbalance_gap
FROM gpu_cluster_metrics ORDER BY imbalance_gap DESC LIMIT 20;
```

**Tenant share of regional traffic by hour**
```sql
SELECT strftime('%Y-%m-%d %H', timestamp) h, customer_id, COUNT(*) n
FROM usage_events WHERE gpu_region='US-East' GROUP BY 1,2 ORDER BY n DESC LIMIT 20;
```

**Ticket volume by category and week**
```sql
SELECT strftime('%Y-%W', created_at) wk, category, COUNT(*) FROM support_tickets GROUP BY 1,2;
```

**Customers with repeated high-severity tickets (health review trigger)**
```sql
SELECT customer_id, COUNT(*) FROM support_tickets
WHERE severity IN ('high','critical') AND created_at >= date('2026-08-31','-30 days')
GROUP BY 1 HAVING COUNT(*) >= 3;
```

**Month-over-month usage change per customer**
```sql
WITH m AS (SELECT customer_id, substr(timestamp,1,7) mo, COUNT(*) n FROM usage_events GROUP BY 1,2)
SELECT a.customer_id, a.n AS may, b.n AS jun, 1.0*b.n/a.n - 1 AS chg
FROM m a JOIN m b ON a.customer_id=b.customer_id AND a.mo='2026-05' AND b.mo='2026-06'
ORDER BY chg LIMIT 20;
```

## 18. Analyst report format
Analyst outputs should follow this structure so that the synthesis step can merge them:

1. **Answer** - two to four sentences answering the question directly.
2. **Key findings** - bullet list; every bullet has at least one number, a time window and a scope
   (region / plan / model / endpoint / customer).
3. **Evidence** - the SQL queries used (or summaries of them) and the resulting numbers. Keep tables small.
4. **Causality assessment** - for each candidate cause: supporting evidence, contradicting evidence, and a confidence
   label (established / likely / possible / ruled out).
5. **Financial impact** - revenue, credits, refunds or at-risk contract value, where relevant.
6. **Recommendations** - concrete, prioritized actions with owners (Infra, Support, Account Management, Product, Finance).
7. **Caveats** - sampling, missing data, ambiguous ticket text, confounders.

Style: concise, numeric, no speculation presented as fact. Refer to customers by name and customer_id.
Dates as YYYY-MM-DD, amounts in USD with thousands separators, latencies in ms or s with one decimal place.

## 19. API reference (abridged)
### POST /v1/chat
Request body fields: `model` (string, required), `messages` (array of {role, content}), `max_tokens` (int, default 1024, maximum 8192),
`temperature` (0-2, default 0.7), `stream` (bool), `tools` (array, optional), `batch` (bool, default false - marks workload_type=batch),
`cache_control` (object, optional - hints for prefix caching; ignored by prefix cache v1).
Response: streamed or complete message with `usage.input_tokens`, `usage.output_tokens`, `usage.cached` (bool).
Errors: 400 invalid request (not logged in usage_events), 401 auth (not logged), 429, 500, 503, 504, 529.

### POST /v1/agents/runs
Runs a multi-step agent. Fields: `model`, `instructions`, `tools` (JSON schema list), `input`, `max_steps` (default 8),
`batch`. Each model call inside a run is logged as a separate `agents` request in usage_events. Tool schemas and
instructions are sent on every step, so agents workloads have long, highly repetitive prompt prefixes.

### POST /v1/embeddings
Fields: `input` (string or array), `model` (must be nova-small). Returns vectors; output_tokens is always 0.

### POST /v1/documents/analyze
Fields: `model` (nova-vision recommended, nova-reasoning for complex reasoning over documents), `document` (PDF/image/URL),
`instructions`, `schema` (optional JSON schema for extraction), `batch`. Documents are converted to tokens (text + image patches),
so prompts are long (median ~7K tokens).

### Rate limit headers
`x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`, `x-ratelimit-reset-requests`. Limits are per organization and per region.

### Client retry guidance
Retry 429/503/529 with exponential backoff and jitter starting at 500 ms, max 5 attempts. Do not retry 400/401.
For 504, reduce max_tokens or use batch mode.

## 20. Industry usage notes
- **Finance**: Uses nova-reasoning for research summarization and compliance review; sensitive to latency during market hours.
- **Healthcare**: Document analysis of clinical and billing records; strict data-privacy requirements (BAA required).
- **Retail**: Customer-support chat and product catalog enrichment; seasonal traffic peaks.
- **SaaS**: Embeds NovaAI into their own products; mixed chat and agents traffic.
- **Manufacturing**: Document analysis of manuals and quality reports.
- **Media**: Content generation and tagging; bursty traffic around publishing schedules.
- **Education**: Tutoring chat; traffic follows academic calendars.
- **Logistics**: Agents for route and exception handling; long tool schemas.
- **Insurance**: Claims document analysis and reasoning over policies.
- **Government**: Strict regional data residency; conservative adoption.
- **Legal**: Long-context reasoning over contracts; long prompts are common.
- **Gaming**: NPC dialogue and moderation; latency sensitive.
