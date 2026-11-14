# Phase 8 architecture: validation, operations and production deployment

Phase 8 adds no new intelligence. It adds the parts that make the existing pipeline
honest and operable:

* an evidence-derived operating mode;
* a single-owner scheduler and scalable workers;
* health, metrics, budgets and alerts;
* evaluation with explicit data origin;
* deployment artifacts and a readiness scorecard.

## 1. Process model

```
                 ┌──────────────┐  HTTP Basic   ┌──────────────┐  bearer token (server-side only)
 browser ───────▶│  frontend    │──────────────▶│     api      │◀──── /health/*, /metrics
                 │  (Next.js)   │               │  (FastAPI)   │
                 └──────────────┘               └──────┬───────┘
                                                       │
 ┌──────────────┐ lease "pipeline" (one owner)  ┌──────┴───────┐        ┌──────────────┐
 │  scheduler   │──────────────────────────────▶│  PostgreSQL  │◀───────│ worker × N   │
 │ (1 active,   │  ops checks, planning,        │  (state,     │ SKIP   │ collection + │
 │  N standby)  │  analysis, forecasts, recs,   │   queues,    │ LOCKED │ detection    │
 └──────────────┘  retention                    │   leases)    │        └──────────────┘
                                                └──────────────┘
                                      migrate (one-shot, forward-only, before the others)
```

All processes share one image (`api | scheduler | worker | migrate`) and one database. There
is no message broker, no Kubernetes and no distributed cache: PostgreSQL already provides
leases (`SKIP LOCKED`), advisory locks, `LISTEN/NOTIFY` and durable queues, and the load test
shows the API is not the bottleneck at this scale (`docs/benchmarks/phase8_load_*.json`).

### Single-owner scheduling

`scheduler_leases` holds an expiring lease per schedule:

* **Fencing token:** monotonically increasing.
* **Database clock:** the lease uses `clock_timestamp()`, so host clock skew cannot create two
  owners.
* **Before each stage:** the leader re-checks that it still holds the lease with its token.
  A stage never starts after the lease is lost (test `test_lost_lease_fences_further_stages`).
* **Standby:** a second scheduler waits; it takes over when the lease expires, with a new
  token (E2E `s16`).
* **Crash recovery:** runs a crashed leader left `running` are closed as `abandoned` by the
  next leader (E2E `s17`).

### Stages and gates

| Stage | Default cadence | Needs (any of) | Notes |
|---|---|---|---|
| ops_checks | 60 s | — | records the mode (alert on change), budget alerts, worker-missing and dead-letter alerts |
| monitor_plan | 5 min | collect_real_data | Phase 3 target sync + job planning (workers execute) |
| content_analysis | 30 min | content_gap_analysis | Phase 5 queue; skips topics at `MAX_ANALYSIS_REQUESTS_PER_TOPIC` |
| forecasting | 1 h | proxy_forecasts, direct_search_forecasts | Phase 6 predictions |
| recommendations | 6 h | recommendations | Phase 7; `MAX_RECOMMENDATIONS_PER_RUN`; creator alerts only if the mode allows |
| retention | 24 h | — | 30-day API data policy, expired recommendations, stale heartbeats |

Each run is recorded in `scheduled_runs`: owner, token, correlation ID, mode, status,
duration, summary and error (redacted). The failure handling is:

* **Isolation:** a failing or hanging stage (timeout) does not stop the others (`s18`, `s19`).
* **Retry:** a failed stage is retried with doubling backoff from 60 s, capped at its
  interval.
* **Escalation:** three consecutive failures raise a critical alert and a dead letter (`s20`).

### Workers

A worker drains collection jobs (Phase 3 leases) and the detection queue (Phase 4 outbox,
advisory lock per series) each tick, and writes a heartbeat. Start-up refuses to exceed
`MAX_CONCURRENT_WORKERS` live workers (`s22`). Permanently failed jobs become dead letters.

### Graceful shutdown

SIGTERM/SIGINT finish the current tick, then:

* the heartbeat is marked `stopped`;
* the lease is released;
* the process exits.

E2E `s23` sends a real SIGTERM. Containers run under an init process
(`init: true`).

## 2. Operating modes

See `live_data_readiness.md`. `ops/modes.py` derives the mode from:

* provider readiness: configuration, live verification and signal classes;
* validation evidence: real labels plus adopted real-data experiments;
* degradations: database, budgets, scheduler and workers, failing providers, stale data.

The mode gates scheduler stages, the generation endpoint and creator alerts. The dashboard
shows it on every page.

## 3. Observability

* **Health:**
  * `/health/live`: no I/O;
  * `/health/ready`: database reachable and schema = `EXPECTED_SCHEMA_REVISION`;
  * `/health/database`: latency and revision;
  * `/health/workers`: heartbeats and lease;
  * `/health/providers`: readiness and mode.
* **Metrics:** `/metrics` in Prometheus text format, built in-process with no client library:
  * request counts and latency histograms by route template;
  * gauges computed at scrape time from the database: job queue by status, detection
    queue, open alerts, dead letters, heartbeat ages, last stage runs, operating mode,
    budget use and limits.
* **Logs:** structured JSON with secret redaction and a correlation ID on every record
  (`X-Request-ID` for API requests, `sched-<task>-…` for stage runs, `worker-…` for worker
  ticks).
* **Alerts:** `ops_alerts`, deduplicated by fingerprint, with a notification cooldown
  (default 60 min) and an open/resolved state. A notification is sent on the first
  occurrence, on reopening, or once per cooldown (`s20`). Alerts go to the logs, and to
  email or Telegram when those channels are configured.

## 4. Budgets

`ops/budgets.py` measures spend from the records each stage writes; there is no second
ledger to drift. See `cost_optimization.md`.

## 5. Evaluation

`trend_agent/evaluation/` (see `end_to_end_evaluation.md`) wraps the per-phase evaluations
with consistent splits, an experiment registry (`experiments`), ground-truth labels kept
REAL/SYNTHETIC apart (`evaluation_labels`), pipeline integrity checks, backtest validity
and a quality scorecard.

## 6. Data model (migration 0009)

| Table | Purpose |
|---|---|
| `service_heartbeats` | one row per running process |
| `scheduler_leases` | leader leases with fencing tokens |
| `scheduled_runs` | each stage execution |
| `ops_alerts` | deduplicated operational alerts |
| `dead_letters` | permanently failed work, redacted payloads |
| `provider_checks` | live smoke-test results |
| `system_state` | current mode and history, restore drill, compliance review, deployment record |
| `evaluation_labels` | ground truth; `known_at` prevents look-ahead |
| `experiments` | evaluation runs and adopt/reject decisions |
| `recommendation_outcomes` | opt-in, creator-entered; `consent` enforced |
| `forecast_models.status` | + `rejected` |

The migration is additive except for the widened status check. Its downgrade refuses while a
model is `rejected`.

## 7. Deployment

See `production_deployment.md`. In summary:

* **Images:** the backend image (non-root) and the frontend image (non-root, production
  build).
* **Compose:**
  * the dev compose keeps PostgreSQL-only by default, with an `app` profile for the full stack;
  * the production compose has required secrets, read-only containers, no capabilities and
    loopback ports.
* **CI:** GitHub Actions with read-only permissions and no secrets.
