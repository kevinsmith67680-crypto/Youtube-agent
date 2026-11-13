# Phase 3 report — Historical Monitoring, Time-Series Data & Intelligent Scheduling

Date: 2026-11-13. Branch `claude/tender-thompson-620dmi`. Migration head `0003`.

## 1. Implemented functionality

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

## 2. Synthetic monitoring results (AI, technology, gaming)

`trend-agent monitor demo`: fictional subjects, a simulated clock, 2026-03-02 → 2026-03-12
in hourly ticks, two workers, a 36 h publication delay, a quota of 8 requests/day
resetting at midnight Los Angeles, and a 36 h provider outage on 7–8 March.

| Category | Query (fictional) | Scenario | Priority | Tier | Collection attempts (10 days) | Stored obs. | Freshness | Readiness |
|---|---|---|---|---|---|---|---|---|
| AI | how to run nimbus coder 2 locally | A: new AI model | 86 | critical | 10 (1/day: the source publishes daily) | 29 | source_current | ready |
| AI | ai coding agents | J: revised history + backfill | 70 | normal | 12 (11 succeeded, 1 deferred by quota) | 98 | source_current | ready |
| AI | ai wallpaper generator | below threshold | 48 | — | not monitored | — | — | — |
| Gaming | fortnite neon rift event | B: 48 h event | 88 | critical → high (2026-03-06, after the event + 24 h hold) | 10; 3 during the 48 h event | 22 | source_current | ready |
| Gaming | frostfall festival rewards | baseline | 66 | high | 10 | 38 | source_current | ready |
| Technology | python tutorial for beginners | C: evergreen | 58 | low | 3 scheduled (7-day batches) + 3 backfill windows (1 deferred) | 370 | source_current (planned batching) | ready (weekly_seasonal) |
| Technology | lattice 5 migration guide | F: duplicates | 74 | high | 10 | 38 | source_current | ready |

Revision lineage for "ai coding agents", 2026-03-03 (`monitor history`): rev 1 = 47.29,
published 2026-03-05 12:00, superseded by rev 2 = 54.79, published 2026-03-07 06:00.
Both are kept.

Backfills: Python tutorial 365 days, 3 windows; the plan reported "3 requests exceed the
remaining quota (2); the rest wait for the reset at 2026-03-02T08:00Z". Quota windows
observed: resets at 08:00 UTC until 2026-03-08, then 07:00 UTC (Los Angeles DST).

All 24 scenario checks passed (verified on repeated runs):

| Scenario | Check | Result |
|---|---|---|
| A | critical tier for a new, time-sensitive AI model | tier critical at activation and at the end |
| A | attention follows the source cadence | 10 attempts in 10 days (1.0/day) for a daily source |
| B | time-sensitive while running | critical → high after the event |
| B | no hourly polling of a daily source | 3 attempts during the 48 h event (hourly would be 48) |
| C | efficient cadence | low tier; 3 scheduled attempts in 10 days |
| C | long history | 370 observations from backfill |
| C | baseline-ready | `weekly_seasonal`: ready |
| D | outage detected | provider unhealthy from 2026-03-07 02:00; 6 targets provider_unavailable; status checks prevented failed requests |
| D | recovery | 6 targets re-activated after 2026-03-08 12:00 |
| D | nothing fabricated | 0 observations collected during the outage |
| D | gap filled | 2026-03-05 (published during the outage) collected 2026-03-08 12:00 |
| E | deferral | 2 deferred attempts |
| E | resumed after reset | 2 deferred jobs completed after the reset |
| E | DST-aware reset | resets at 08:00 and 07:00 UTC |
| F | duplicates stored once | 83 in-response duplicates, 45 unchanged re-fetches; 38 rows for 38 periods |
| G | no look-ahead | Monday's value invisible on simulated Tuesday 12:00, visible Wednesday 12:00 |
| G | deterministic replay | digest `d2a07f54dc62d031` (same on every run and database) |
| H | normalisation mismatch rejected | incompatible: `different_normalization_context` |
| H | consistent series comparable | comparable |
| I | crash recovery by another worker | worker-a claimed 5 jobs and vanished; worker-b recovered all 5 one hour later |
| I | idempotent re-execution | 595 current (series, period) keys, multiplicity 1 |
| J | revision lineage | rev1 47.29 → rev2 54.79, both kept |
| J | revision visible only from its publication | 47.29 on Mar 6, 54.79 on Mar 7 |
| — | selective activation | priority-48 keyword not monitored |

## 3. Test results

All commands were run in this environment on 2026-11-13 and passed:

| Check | Result |
|---|---|
| `ruff check`, `ruff format --check` (src, tests, scripts, migrations) | clean |
| `mypy src tests` (strict) | no issues in 122 files |
| full suite, Python 3.12, `TREND_AGENT_REQUIRE_DB_TESTS=1`, with coverage | **437 passed**, 3 live tests deselected; total coverage 91 % (monitoring modules 71–100 %, `cli_monitor.py` 70 %) |
| full suite, Python 3.13 | **437 passed** |
| `alembic check` (test and dev databases) | no model/migration drift |
| migration test: clean database → `0003` → round trip | passed |
| `trend-agent monitor demo` on a fresh database | 24/24 scenario checks passed |

Phase 3 added 46 unit tests and 17 database integration tests. Together they cover the 26
required areas: target selection and the discovery-lifecycle link, category allocation
and redistribution, cadence and minimum poll intervals ("five-minute polling of a daily
source yields ~1 useful check per day"), freshness estimation and statuses, fairness and
starvation, quota deferral and resume at the Los Angeles reset (incl. DST), job creation
and idempotency keys, **concurrent claiming with four real threads against PostgreSQL**
(every job exactly once), lease expiry, recovery and token fencing, crash after write
(re-execution stores nothing new), retry classification, duplicate/revised/conflicting
and missing-value writes (and the database refusing zero for missing), comparability and
normalisation contexts, backfill (clamping, budget, resume, provenance, refusals),
readiness, quality, replay and look-ahead prevention, retention purge, every `monitor`
command, the complete synthetic workflow, and regression of all Phase 1 and Phase 2 tests.

Not run: live provider tests (no Trends access; YouTube has no search interest).

## 4. Performance benchmarks

`scripts/benchmark_monitoring.py`, raw output in
[benchmarks/phase3_benchmark.json](benchmarks/phase3_benchmark.json).

* **Hardware:** container with 4 vCPU (Intel Xeon @ 2.10 GHz, 1 thread per core), 16 GB
  RAM; PostgreSQL 16.16 in Docker on the same host; Python 3.12.3.
* **Dataset:** 11,000 targets (1,000 active, 10,000 dormant) and 11,000 discovered
  keywords; 90 days of history for each active target, giving 96,200 observations at the
  end.

| Operation | Result |
|---|---|
| Target selection (`sync`: 1,000 candidates, 11,000 targets evaluated) | 5.04 s |
| Planning, 1,000 due targets, capacity 200 | 0.49 s (dry run 0.17 s); allocation AI 90 / tech 50 / gaming 60 (= 45/25/30) |
| Job claiming, 2,000 jobs, batches of 50 | 6,639 jobs/s (1 worker); 9,921 jobs/s (4 concurrent workers, split 450/650/450/450, **0 duplicates**) |
| Historical range query, 90-day window | p50 4.6 ms, p95 5.7 ms (100 targets, 88 points each) |
| Ingestion through the real write path (series + revision-aware upsert) | 381 observations/s (90,000 in 236 s); idempotent rewrite 3.8 ms/observation |
| Executor end to end (claim → Phase 1 mock request → validation → write → attempt record), 200 jobs | 8.6 jobs/s, 6,200 new observations, all succeeded |
| Replay, 30,000 events × 31 daily steps | 4.9 s (6,148 events/s) |

Interpretation: claiming, planning and range reads are far from being a bottleneck at
this scale. Writes are the slowest path: one round trip per observation, by design, so
revisions and conflicts are detected per value. At ~380 observations/s this machine can
store a day's worth of daily values for 1,000 targets in under 3 s, and a 90-day history
backfill for 1,000 targets takes about 4 minutes. A multi-row insert path for backfills
would be the first optimisation if needed. Partitioning or TimescaleDB is not warranted at
these volumes. These are measurements on one container, not a scalability guarantee.

## 5. Working provider integrations and external dependencies

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

## 6. Known limitations

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

## 7. Phase 4 readiness

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
