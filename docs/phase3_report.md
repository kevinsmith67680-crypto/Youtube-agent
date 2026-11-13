# Phase 3 report — Historical Monitoring, Time-Series Data & Intelligent Scheduling

Date: 2026-11-13. Branch `claude/tender-thompson-620dmi`. Migration head `0003`.

## A. Implemented functionality

* **Monitoring targets** (`monitoring/target_manager.py`). Selection from Phase 2
  candidates (PRIORITIZED/MONITORING, priority ≥ 55) with provider capability, evidence
  quality and per-category capacity (45/25/30, unused share redistributed, 20 % exploration
  reserve). A separate monitoring lifecycle with validated, logged transitions (8 states),
  tiers with hysteresis, demotion with minimum active time and cooldown, manual overrides,
  and mirroring into the discovery lifecycle (keyword MONITORING) through the reserved
  `monitoring` actor. Keywords without a usable search-interest source stay
  `provider_unavailable`, with the reason stated. Optional proxy targets are explicitly
  labelled.
* **Observation model** (`ts_series`, `ts_observations`). Seven measurement categories,
  separate event / window / publication / first-available / collection / ingestion times,
  units, scale and normalisation context, value status, data-policy id, synthetic flag,
  quality status, provider version, and revision lineage. The database enforces that a
  missing value is never zero.
* **Freshness** (`freshness.py`). Provider profiles state granularity, cadence, delay,
  lookback, polling limit, quota, revision, sampling, normalisation and retention. A
  delay estimator learns from observed publication times. Six freshness statuses, exposed
  in the CLI and the internal API.
* **Scheduling** (`scheduler.py`, `planner.py`). Next useful check =
  `latest period + cadence × periods-per-request + delay`; re-checks per tier; minimum
  polling interval; per-category budgets with exploration and redistribution; aging and a
  starvation guard; quota-aware deferral with DST-aware resets; idempotent job creation;
  dry run.
* **Job queue** (`collection_jobs.py`, `executor.py`, `leases.py`, `job_state.py`).
  PostgreSQL `SKIP LOCKED` claiming, leases with renewal, recovery of expired leases,
  token-fenced state changes, bounded concurrency, retries with exponential backoff and
  full jitter (respecting Retry-After), deferral on quota, and immediate failure for
  permanent errors. Every attempt is recorded.
* **Storage** (`time_series.py`). Writes are classified as new, unchanged, revised or
  conflicting, and are idempotent under concurrency and re-execution. Range reads are
  keyset-paginated.
* **Comparability, readiness, quality**. Statuses with machine-readable reasons; readiness
  profiles per provider and resolution; quality findings at series, target, provider and
  category level.
* **Backfill** (coverage validation, clamping, window planning, budget estimate,
  duplicate avoidance, resume, provenance) and **replay** (availability-gated,
  revision-aware, deterministic digests, consumer interface for Phase 4).
* **Compliance**. Versioned data policies; policy-driven expiry (purged by
  `data retention --purge`); YouTube derived metrics disabled; Trends storage denied until
  a retention period is configured.
* **Metrics and CLI**. `trend-agent monitor targets|sync|activate|pause|resume|disable|
  override|plan|run|worker|scheduler|status|providers|freshness|readiness|quality|quota|
  history|backfill|replay|demo`.
* **Synthetic scenarios A–J** with an end-to-end demo that checks itself (24 checks).

Phase 1/2 changes are listed in
[phase3_architecture.md](phase3_architecture.md#phase-1--phase-2-code-changed).

## F. Example synthetic monitoring results

`trend-agent monitor demo`: fictional subjects, a simulated clock, 2026-03-02 → 2026-03-12
in hourly ticks, two workers, a 36 h publication delay, and a quota of 8 requests/day
resetting at midnight Los Angeles.

RESULTS_TABLE

All 24 scenario checks passed (verified on repeated runs):

CHECKS_TABLE

## G. Test results and performance

TEST_RESULTS

BENCH_RESULTS

## E. Working provider integrations

| Provider | Status here | Notes |
|---|---|---|
| `synthetic_trends`, `synthetic_trends_web` | working (demo/tests) | Phase 3 fixtures through the Phase 1 provider protocol and validation |
| `mock` (Phase 1) | working | per-request scaling, so each request is its own series; used for the backfill/replay CLI examples |
| `discovery_evidence` (proxy) | working | real data where discovery evidence exists (e.g. PyPI release feeds) |
| `google_trends` | **blocked** | API alpha access, credentials, endpoint and a transport are not available; `not_configured` |
| `youtube_data_api` | **not permitted** for monitoring | no search interest; statistics over time are derived metrics needing authorisation |

Remaining external dependencies: Google Trends API alpha access and a transport for its
contract; a retention period consistent with those terms; any licensed YouTube-specific
search-interest source; YouTube API derived-metrics authorisation if video-activity
monitoring is ever wanted.

## H. Known limitations

* There is **no real search-interest data** in this environment. Everything about real
  scheduling cadence is verified only against synthetic providers built from the
  publicly described Trends capabilities. The live Trends contract may differ.
* Delay and cadence estimates need publication timestamps. Providers that omit them give
  only collection-time upper bounds, and the declared delay is used instead.
* Proxy event signals count items found by discovery. Their coverage depends on which
  discovery sources are reachable, and days without items are unknown, not zero.
* The scheduler loop is a minimal asyncio loop, not APScheduler. Planner and workers are
  separate processes, but the planner should run as a single instance: job idempotency
  makes two planners safe, but wasteful.
* Not implemented: per-host politeness beyond quotas and poll intervals; table
  partitioning (not needed at the measured scale).
* Readiness and quality are computed on demand. `monitor status` computes readiness for
  every target, which is O(targets) queries.
* Benchmarks ran on a 4-vCPU container with PostgreSQL in Docker on the same host.
  Absolute numbers will differ elsewhere.

## J. Phase 4 readiness

Phase 4 can consume:

* **quality-controlled series**: `TimeSeriesRepository.points` / `series_summaries`, one
  scale per series, revision lineage, provenance, synthetic/proxy flags;
* **a gate**: `Insights.readiness()` with explicit reasons per series and profile;
* **comparability**: `assess_pair` / `comparable_groups` before any cross-series
  comparison;
* **back-testing without look-ahead**: `replay(..., consumers=[detector])` with
  deterministic digests;
* **operational context**: freshness, quality findings, target tiers and statuses.

Not done, by instruction: surge detection, forecasting, content-gap analysis,
recommendations. The blocking dependency for real (non-synthetic) Phase 4 work is a
licensed search-interest source.
