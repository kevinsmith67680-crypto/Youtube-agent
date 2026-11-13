# Phase 3 architecture — historical monitoring

Phase 3 turns prioritised discovery candidates (Phase 2) into **monitoring targets** and
collects their history from sources that can actually measure them. It produces
quality-controlled, provenance-complete time series for Phase 4. It does not detect
surges, forecast or recommend anything.

## Modules

```
src/trend_agent/
  schemas/monitoring.py    vocabulary: measurement categories, target status, tiers, freshness,
                           readiness, comparability (+ reason codes), job status/type, errors
  schemas/time_series.py   ObservationDraft (write model), SeriesIdentity, StoredPoint (read model)
  schemas/scheduling.py    ProviderProfile (capabilities/freshness), DelayEstimate, NextCheck,
                           PlanReport
  monitoring/
    config.py          config/monitoring.yaml (budgets, activation, tiers, jobs, providers,
                       readiness, quality, data policies, compliance)
    policies.py        data policies: storage allowed?, expiry, versioned policy id
    target_manager.py  target selection/activation/demotion, tiers, provider choice,
                       manual overrides, discovery-lifecycle mirroring
    lifecycle.py       monitoring state machine (validated, logged transitions)
    collectors.py      SearchInterestCollector (Phase 1 providers + Phase 1 validation),
                       EventSignalCollector (proxy signals from Phase 2 evidence), registry
    synthetic.py       deterministic synthetic provider + scenario fixture (tests/demo only)
    scheduler.py       cadence rules (next useful check), SchedulerLoop process
    freshness.py       publication-delay estimator, freshness status
    planner.py         planning cycle: due targets -> budgeted, quota-aware durable jobs
    job_state.py       error classification, retry/backoff/deferral decisions
    leases.py          lease renewal for long jobs
    executor.py        worker: claim -> collect -> idempotent write -> record attempt
    comparability.py   comparability engine (status + machine-readable reasons)
    readiness.py       baseline-readiness evaluator (Phase 4 input gate)
    quality.py         data-quality findings and health summaries
    backfill.py        resumable, quota-aware historical backfill
    replay.py          deterministic replay without look-ahead (+ consumer interface)
    metrics.py         read models: status, freshness, readiness, quality, history
    service.py         MonitoringService composition
    demo.py            end-to-end synthetic demonstration (scenarios A-J)
  db/monitoring_models.py               Phase 3 tables (migration 0003)
  db/repositories/monitoring.py         targets, events, keyword candidates, health, plans
  db/repositories/time_series.py        series + revision-aware observation writes, range reads
  db/repositories/collection_jobs.py    job queue: enqueue, SKIP LOCKED claim, fencing, attempts
  cli_monitor.py                        `trend-agent monitor ...`
config/monitoring.yaml
scripts/benchmark_monitoring.py
```

## Processes and data flow

```
 Phase 2 discovered_keywords (PRIORITIZED / MONITORING)
            │  target_manager.sync()  (scheduler process, every cycle)
            ▼
 monitoring_targets ──(validated transitions, events)──► monitoring_target_events
            │  planner.plan()        due targets → cadence check → category budget →
            │                        quota check → idempotent jobs
            ▼
 monitoring_jobs (PostgreSQL queue)
            │  executor (1..n worker processes): recover expired leases, claim with
            │  FOR UPDATE SKIP LOCKED, collect, validate, write, fenced completion
            ▼
 ts_series ──► ts_observations (append-only revisions)      monitoring_job_attempts
            │
            ├─► readiness / quality / freshness / history (read models, CLI, Phase 4)
            └─► replay (availability-gated snapshots for back-testing)
```

The scheduler (`monitor scheduler`) and workers (`monitor worker`) are separate processes
that share only PostgreSQL. More workers can be started at any time; correctness does not
depend on how many run. `monitor run --once` does sync + plan + drain in one process.

## Key design decisions

* **Database-backed scheduling, no broker.** Jobs live in `monitoring_jobs`; workers claim
  with `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED LIMIT n)`. No Redis/Celery.
  A small asyncio loop (`SchedulerLoop`) replaces APScheduler (one periodic job; no extra
  dependency; injectable sleep for tests).
* **At-least-once execution, exactly-once data.** Leases expire and are recovered; state
  changes are fenced on a per-claim lease token; observation writes are idempotent through
  database constraints (one row per series/window/revision, one current revision).
* **A new table for monitoring observations.** Phase 1's `search_interest_observations`
  rejects changed values as conflicts and has no target linkage, revision lineage or
  publication-time semantics. Phase 3 adds `ts_series`/`ts_observations` and keeps the
  Phase 1 table unchanged for `trend-agent ingest`. Phase 1 validation is reused for every
  search-interest value before it reaches the new table.
* **Two lifecycles.** The discovery lifecycle (keyword worth watching?) and the monitoring
  lifecycle (what is the system doing about it?) are separate. They meet at two logged
  points (keyword → MONITORING when a non-proxy target becomes active; back when none is).
* **Proxy monitoring is explicit.** Event/community/suggestion signals derived from stored
  discovery evidence form separate, labelled proxy targets and series; they are never
  presented as search interest.

## Data model (migration `0003`, additive)

| Table | Purpose |
|---|---|
| `data_policies` | versioned retention/permission policies (`<key>@<version>`), referenced by every observation |
| `monitoring_targets` | one per (query, region, language, measurement category): keyword, clusters, category, priority, tier, status, preferred/fallback/in-use provider, proxy flag, timestamps (last attempt / success / new observation / next collection / latest period), manual overrides, config version |
| `monitoring_target_events` | status, tier, provider and override changes with reason, actor, time |
| `ts_series` | one per scale: query, provider, measurement (and simulated) category, property, region, language, granularity, scale type, unit, normalisation context |
| `ts_observations` | revisions: window, event time, publication / first-available / collection / ingestion times, value, quality, policy, provenance, revision lineage, job id, expiry |
| `monitoring_jobs` | durable queue: type, priority, status, attempts, lease owner/token/expiry, retry-after, error category/details, idempotency key, estimated cost, backfill link |
| `monitoring_job_attempts` | one row per execution: worker, window, counts (new/unchanged/revised/conflicting/missing/rejected/duplicates/withheld), quota used, latency, schedule delay |
| `monitoring_plan_runs` | each planning cycle: capacity, allocation and usage by category, deferrals, skips |
| `monitoring_provider_health` | healthy/degraded/unhealthy with consecutive failures |
| `monitoring_backfills` | backfill runs (target, provider, range, windows, estimate, signature for resume) |

Indexes follow the query patterns: due targets `(status, next_collection_at)`, claimable
jobs `(status, scheduled_at, priority)`, expired leases (partial), observations by
`(series_id, period_start)`, `(query_id, period_start)`, `(provider_name, collected_at)`,
`(measurement_category, region)`, `normalization_key`, `target_id`, and a partial index on
`expires_at`. No partitioning or TimescaleDB: the benchmark shows no need at the target
scale (see [phase3_report.md](phase3_report.md#4-performance-benchmarks)).

## Phase 1 / Phase 2 code changed

| File | Change | Why |
|---|---|---|
| `discovery/service.py` | keywords in MONITORING keep being re-scored (status untouched) | monitoring reads current discovery priority |
| `ingestion/retention.py` | report/purge `ts_observations` | policy-aware expiry |
| `container.py` | `monitoring_config()`, `monitoring()` factories | composition |
| `cli.py` | registers `monitor` sub-app; help text | CLI |
| `db/models.py` | imports Phase 3 models | Alembic metadata |
| tests | migration head `0003`, Phase 3 tables in the truncation list | regression suite |
