# Phase 8 report: end-to-end validation, accuracy, production operations

Date: 2026-11-14. Branch `claude/tender-thompson-620dmi`. Baseline `cafa1b8` (end of Phase 7).

## A. Executive summary and verdict

Phase 8 audited the system, then made it operable, measurable and honest about its limits:

* an evidence-derived **operating mode** gates what the system may claim;
* a **single-owner scheduler** (database lease with fencing) and **scalable workers** run
  the pipeline unattended;
* **health, metrics, budgets, alerts and dead letters** make it observable;
* an **evaluation framework** separates synthetic from real results;
* **deployment artifacts** were built and run;
* **40 end-to-end scenarios** cover failure modes and security boundaries.

**Verdict: `STAGING_READY`. Not production-ready, and the original objective is not
achieved.**

* **No search-interest source.** No legitimate provider of YouTube Search interest exists
  in this deployment. The best reachable mode is `PROXY_OPPORTUNITY_RESEARCH`, which makes no
  search-demand claims; `FULL_SEARCH_INTELLIGENCE` is **BLOCKED**.
* **Unknown accuracy.** Real-world accuracy is **UNKNOWN** for every component: there are
  zero labelled real observations.
* **What passes.** All build-level checks pass: tests, static checks, security scans,
  deployment artifacts, migrations and offline evaluation.
* **What does not.** Runtime items (scheduler, workers, real data flowing, external alert
  channel) fail because nothing is deployed. External items (direct and external search
  providers, compliance review, real accuracy, deployment) are BLOCKED. BLOCKED is never
  counted as PASS.

## B. Initial audit and resolution

`docs/phase8_initial_audit.md` recorded 3 CRITICAL, 10 HIGH, 8 MEDIUM and 3 LOW findings.

| Finding | Resolution |
|---|---|
| C1: no YouTube Search-interest source | Cannot be fixed by code. Operating modes now derive what may be claimed; direct claims are disabled; readiness marks it BLOCKED. |
| C2: real-world accuracy unknown | A ground-truth registry (real and synthetic kept apart) and a scorecard that reports UNKNOWN. |
| C3: no unattended pipeline | The pipeline scheduler with six gated stages, plus workers. |
| H1, H2, H3: no single owner, no heartbeats, health always "ok" | Lease plus fencing, `service_heartbeats`, five real health endpoints. |
| H4, H5: unsafe production defaults, unauthenticated dashboard | Production guard (exit 2); dashboard HTTP Basic authentication, mandatory in production. |
| H6: unbounded spend | Seven budgets and limits, measured from recorded spend, with a degraded mode. |
| H7, H8: bidi characters, vulnerable `postcss` | Fixed during the audit; production npm audit is 0. |
| H9: no REJECTED status or rollback | REJECTED status, rollback with checksum-verified restoration. |
| H10: silent permanent failures | Dead letters, plus ops alerts with dedup and cooldown. |
| M1–M8 | Metrics, correlation IDs, backups with a restore drill, experiment registry, opt-in outcomes, compliance audit, mode display, Docker and CI. |

## C. Architecture changes

See `phase8_architecture.md`.

* **New packages:** `trend_agent/ops` (signals, providers, modes, budgets, health, alerts,
  metrics, pipeline, readiness, production guard, backup, system) and
  `trend_agent/evaluation` (detection, forecasting, content gaps, recommendations,
  integrity, replay, labels, registry, scorecard, pipeline).
* **New CLI groups:** `system` and `evaluate`.
* **Migration 0009:** 10 tables, plus the `rejected` model status.
* **Changes to existing code (small, behaviour-preserving):**
  * budget hooks in the recommendation and content-analysis services (paid LLM path only);
  * `alerts_enabled` on recommendations;
  * `exclude_targets` on content analysis;
  * dead letters in the collection executor;
  * `MAX_RETRIES` in retry policies (default unchanged: 4 attempts);
  * correlation IDs in logs;
  * new API routes;
  * `forecast rollback --reject --restore-previous` and `forecast reject`.

## D. Live data readiness and operating modes

See `live_data_readiness.md`. Measured here:

* **YouTube:** not configured (no key).
* **Google Trends:** disabled (no alpha access).
* **Feeds:** live-verified (1 request).
* **GitHub and Hacker News:** blocked by the sandbox network.
* **Reddit:** not configured.

Mode: **DEGRADED (base PROXY_OPPORTUNITY_RESEARCH)**, with direct-search claims disabled.

## E. End-to-end pipeline validation

* **Scheduled stages:** E2E `s39` runs the synthetic pipeline through the real scheduler
  and worker: planning, collection by the worker, detection consuming the outbox, then
  forecasts, recommendations and retention. Every stage is recorded, no synthetic record
  is stored as real, and the integrity check passes.
* **Full chain into recommendations:** the Phase 7 database chain test (Phase 4 → 7) still
  passes.
* **Built containers:** the backend image ran `migrate`, `api`, `scheduler --once` and
  `worker --once` against a fresh database (§I).
* **Integrity invariants:** enforced partly by schema constraints (a synthetic observation
  can never be relabelled real) and partly by `evaluation/integrity.py`, which detected an
  injected contamination (`s34`).

## F. Accuracy evaluation (offline; synthetic and fixture data)

| Component | Held-out result | Real world |
|---|---|---|
| Detection | P 0.906, R 1.00, F1 0.951, 0.42 false alerts / 1,000 series-day, delay median 0.5 d / p90 6.5 d; proxy sources never confirm | UNKNOWN |
| Detection optimisation | 6-config grid on dev; the dev-best (`h = 7`) lost on test (F1 0.931, lower recall, more false alerts) → **rejected**; configuration unchanged | — |
| Forecasting | 5/18 scopes validated (trajectory), all beat the best baseline (MAE −14 % to −28 %); breakout falls back to baselines at scale 0.4; 6 h from daily data unsupported; the Phase 6 held-out breakout champion had negative Brier skill (reported) | UNKNOWN |
| Content gaps (four classes) | false gap rate 0.0, false VERIFIED_GAP 0, gap precision 1.0, recall 0.8 (12 cases) | UNKNOWN |
| Recommendations | status accuracy 1.0, false approvals 0, unsupported published 0, duplicate F1 0.889 | outcomes: none recorded |

These show behaviour on synthetic data designed alongside the detectors, and on small
author-labelled fixtures. They are not real-world accuracy. See `end_to_end_evaluation.md`.

## G. Security audit

See `security_audit.md`.

* **Python dependencies:** pip-audit 0.
* **Dashboard dependencies:** npm audit 0 for production. Dev-only vitest advisories
  remain; the fix is blocked by an npm bug and the risk is accepted.
* **Static analysis:** bandit has no medium or high findings.
* **Secrets:** no real secrets in the tree or history.
* **Access control:** dashboard authentication and the production guard, both verified in
  the built images.
* **Request handling:** request-ID validation, body limits, rate limits and CORS, all
  verified by E2E tests.

## H. API compliance audit

See `api_compliance_audit.md`.

* **Technical controls in place:** 30-day retention, statistics not stored, sources kept
  separate and labelled, no scraping, quota enforcement, opt-in outcomes, no publishing.
* **Unverified:** the official policy text could not be re-read here
  (`developers.google.com` blocked). Whether derived metrics, cross-source combination and
  scoring are permitted needs a human review. **BLOCKED.**

## I. Production infrastructure and deployment

See `production_deployment.md`.

**Built and run (no registry push, no deployment):**

* **Backend image** (non-root uid 10001):
  * `migrate` on an empty database: ok;
  * `/health/live` 200 and `/health/ready` 200;
  * `/health/workers` without a token: 401;
  * `scheduler --once` and `worker --once`: ok;
  * production with the dev database URL: **exit 2**.
* **Frontend image** (non-root uid 1000):
  * production without credentials: 503;
  * anonymous: 401; wrong password: 401; correct password: 200;
  * `/healthz`: 200.
* **Sandbox limitation:** the backend image was built here with `INSTALL_PG_CLIENT=0`,
  because the sandbox blocks `deb.debian.org`; CI builds the default image.

**Configuration:**

* compose: dev profile `app` plus a production variant with required secrets, read-only
  containers, no capabilities and loopback ports;
* CI: GitHub Actions with read-only permissions and no secrets;
* the CI workflow file is committed but has not run on GitHub at the time of writing.

**Deployment:** **nothing was deployed.** No target or credentials were available, so the
readiness item is BLOCKED.

## J. Operations

* **Health:** `/health/live`, `/health/ready`, `/health/database`, `/health/workers` and
  `/health/providers`.
* **Metrics:** `/metrics` in Prometheus text format: request counts and latency histograms,
  plus database gauges for queues, alerts, dead letters, heartbeats, stage runs, mode and
  budgets.
* **Logs:** structured JSON with redaction and correlation IDs.
* **Ops alerts:** fingerprint dedup and a cooldown; six recurring failures produced one
  notification (`s20`).
* **Dead letters:** for permanently failed jobs and stages; listed and resolved through the
  CLI (`s25`).
* **Budgets:** `DAILY_API_BUDGET`, `DAILY_LLM_BUDGET`, `MONTHLY_LLM_BUDGET`,
  `MAX_RECOMMENDATIONS_PER_RUN`, `MAX_ANALYSIS_REQUESTS_PER_TOPIC`, `MAX_RETRIES` and
  `MAX_CONCURRENT_WORKERS`; exhaustion produces DEGRADED (`s11`–`s15`).
* **Reliability:** full-jitter backoff; `Retry-After` honoured up to a cap; permanent errors
  never retried (`s09`, `s10`); stage isolation and timeouts; crash recovery; a real
  SIGTERM shutdown (`s23`).
* **Backups:** a real backup and restore drill on the development database. 83 tables and
  11,116 rows restored with 0 mismatches in 1.8 s, schema 0009, and a corrupted dump was
  rejected (in tests).
* **Model maintenance:** REJECTED, rollback with checksum-verified restoration, no
  automatic promotion.

## K. Testing

Recorded in `docs/benchmarks/phase8_verification.json`, parsed from the run logs by
`scripts/record_verification.py`; the fast checks were re-executed by that script.

| Suite | Result |
|---|---|
| Python 3.12, full suite with coverage | **964 passed**, 3 deselected (opt-in live tests), 0 failed; coverage **89 %** (23 min) |
| Python 3.13, full suite | **964 passed**, 3 deselected, 0 failed (15.6 min) |
| Tests added after the full runs started (outcome API, `system` CLI, evaluation pipeline): affected files re-run | 3.12: 194 passed (+ E2E 40 passed separately); 3.13: 234 passed (incl. E2E) |
| End-to-end scenarios (40) | 40 passed |
| Frontend: Vitest / Playwright / production build | 38 passed / 15 passed / ok |
| ruff, ruff format, mypy src, mypy tests --strict, tsc | all clean |
| bandit (medium+), detect-secrets (vs baseline), pip-audit, npm audit --omit=dev | clean / no new secrets / 0 / 0 |
| Migration validation (empty database: up, check, down, up) | ok, head 0009 |

**Bugs found by the new tests and fixed:**

* metrics read a non-existent table;
* resolving a dead letter was not flushed before the next read;
* label import counted skipped duplicates as accepted, because the driver's `rowcount` is
  unreliable for `ON CONFLICT DO NOTHING`.

## L. Performance

Single host, 4 CPUs, Python 3.12, one uvicorn process. Load tests use an in-process
async client, so the client shares the Python interpreter and the figures are a **lower
bound** on server capacity. Raw data is in `docs/benchmarks/phase8_*.json`.

**API load test** (`scripts/load_test.py`, 6-endpoint read mix):

| Backend | Concurrency | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| demo (in memory) | 1 / 8 / 32 | 231 / 209 / 200 req/s | 4 / 36 / 129 ms | 6 / 47 / 311 ms | 7 / 55 / 607 ms | 0 |
| database (development data, small) | 1 / 8 / 32 | 95 / 87 / 71 req/s | 8 / 71 / 353 ms | 27 / 194 / 665 ms | 30 / 282 / 1,207 ms | 0 |

* **Rate limit:** at the configured 240 requests per minute per client, 240 requests
  answered 200 and 60 answered 429 with `Retry-After: 58`.
* **Interpretation:** a dashboard page makes a handful of API calls, so a single creator is
  two orders of magnitude below capacity. Throughput stays flat with concurrency because
  one process is CPU-bound. If load ever required it, the first step would be more uvicorn
  workers, not new infrastructure.
* **Larger data volumes:** Phase 7 measured database list queries at 5–18 ms and the
  breakout sort at 105 ms (`phase7_benchmark.json`).

**Per-operation latency** (`trend-agent system benchmark -n 30`, sequential):

| Operation | p50 / p95 |
|---|---|
| health.database | 1.2 / 2.1 ms |
| health.workers | 1.8 / 4.2 ms |
| mode decision (uncached snapshot) | 18 / 27 ms |
| `/health/live` | 5.1 / 6.6 ms |
| `/health/ready` | 9.0 / 12 ms |
| `/api/system/mode` (15 s cache) | 6.5 / 8.0 ms |
| `/metrics` | 9.8 / 13 ms |
| `/api/overview` | 25 / 34 ms |
| `/api/opportunities` | 9.8 / 11 ms |
| `/api/recommendations` | 11 / 13 ms |

**Offline jobs:**

* evaluation pipeline (small): 2.2 min (detection 42 s, forecasting 89 s at scale 0.4);
* detection tuning: 49 s;
* backup of the development database (1.0 MB, 83 tables): 0.56 s;
* restore drill: 1.8 s;
* full test suites: 23 min (3.12 with coverage) and 15.6 min (3.13).

## M. Dashboard production review

* **Operating-mode banner:** on every page, with the mode, whether direct claims are
  enabled, the reasons and a signal-class legend (REAL / SYNTHETIC / DIRECT / EXTERNAL /
  PROXY / FORECAST / INSUFFICIENT EVIDENCE). It never hides an unknown mode.
* **Synthetic data:** synthetic data is still banner-labelled.
* **Authentication:** mandatory in production.
* **Checks:** axe WCAG 2.1 AA clean; mobile layout without horizontal scroll.
* **Screenshot:** `docs/screenshots/phase8_mode_banner.png`.

## N. Readiness scorecard and acceptance criteria

`trend-agent system readiness` (`docs/benchmarks/phase8_readiness.json`; rendered as
`docs/production_readiness.md`):

**Release state: STAGING_READY.** PASS 13, FAIL 5, BLOCKED 7, NOT_APPLICABLE 1.

| Scope | Items |
|---|---|
| build | **all 9 PASS**: backend tests, E2E scenarios, static checks, frontend tests, dependency audit, code security scan, deployment artifacts, migration validation, offline evaluation |
| runtime | PASS: database schema, backup restore drill, budgets. **FAIL** (nothing deployed): production configuration (development secrets in this environment), scheduler running, workers running, operational alert channel, real data flowing |
| external | PASS: proxy provider live (feeds). **BLOCKED**: direct search provider, external search provider, detection / content-gap / forecast real-world accuracy, YouTube API compliance review, production deployment. NOT_APPLICABLE: recommendation outcomes (post-launch) |

### Acceptance criteria

The Phase 8 requirements, summarised one per row:

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Repository audit first, findings classified CRITICAL / HIGH / MEDIUM / LOW | Met | `phase8_initial_audit.md` |
| 2 | Critical and high issues resolved first | Met where code can resolve them | C3, H1–H10 fixed; C1 and C2 need data and access (documented, enforced by modes) |
| 3 | Production readiness gate and live-data readiness with the six signal classes | Met | `live_data_readiness.md`; `system providers` |
| 4 | Proxy-intelligence mode; direct-search claims disabled without a direct source | Met | modes; E2E `s01`–`s08` |
| 5 | End-to-end pipeline validation | Met on synthetic data | E2E `s39`; Phase 7 chain test; container runs. Real pipeline not exercised (no real provider beyond feeds) |
| 6 | Optional live-provider smoke tests, credential-gated and quota-bounded | Met | `system smoke-test` (feeds verified live, 1 request) |
| 7 | Ground-truth dataset with real and synthetic separated, no manufactured data | Met (infrastructure) | `evaluation_labels`, label import (`s35`); **0 real labels exist** |
| 8 | Walk-forward backtesting without leakage | Partially met | validity classification and availability-aware replay exist; no real history to backtest |
| 9 | Detection accuracy metrics by source | Met (synthetic) | `detection_accuracy.md` |
| 10 | Detector optimisation with dev / calibration / test splits | Met | tuning loop; dev-best rejected on test |
| 11 | Forecast evaluation against baselines per horizon; no 6 h from daily data | Met (synthetic) | `forecast_validation.md` |
| 12 | Optimisation loop and experiment registry; no LLM changes thresholds | Met | `experiments`; proposals only |
| 13 | Content-gap accuracy in four classes | Met (fixtures) | `content_gap_accuracy.md`; verified-vs-candidate correctness UNKNOWN |
| 14 | Recommendation quality without self-judgement; no causal claims | Met | `recommendation_quality.md` |
| 15 | Quality scorecard, real-world accuracy UNKNOWN without data | Met | `phase8_evaluation.json` |
| 16 | Security audit with dependency and secret scanning | Met | `security_audit.md` (dev-only vitest advisories accepted) |
| 17 | API compliance audit; restricted features off by default | Met as an audit | compliance itself **BLOCKED** pending human review |
| 18 | Separate API / scheduler / worker / frontend / database services; single-owner scheduling; graceful shutdown | Met | `phase8_architecture.md`; `s16`, `s17`, `s23` |
| 19 | Dockerfiles, compose (dev and production), `.env.example`, migration step, health checks | Met | images built and run (§I) |
| 20 | CI: lint, types, unit, integration, security, frontend, backend, docker, migrations | Implemented | `.github/workflows/ci.yml`; **not yet executed on GitHub** |
| 21 | Backup and recovery with a restore drill | Met | drill passed (83 tables, 0 mismatches) |
| 22 | Health endpoints (live, ready, providers, workers, database) | Met | §J |
| 23 | Metrics, structured logs, correlation IDs | Met | §J |
| 24 | Retries with backoff and jitter, Retry-After, no retry of permanent errors, dead letters, malformed records never crash | Met | `s09`, `s10`, `s18`, `s24`, `s25` |
| 25 | Cost budgets and degraded mode | Met | `cost_optimization.md`; `s11`–`s15` |
| 26 | Model drift and maintenance: statuses, rollback, no automatic promotion | Met | `model_maintenance.md` |
| 27 | Operational alerting with dedup and cooldown; runbook | Met | `s20`; `operations_runbook.md` |
| 28 | Feedback and outcome tracking (opt-in, no auto-publishing) | Met | outcome API with consent; descriptive summary |
| 29 | 40-scenario E2E suite; load and performance testing with recorded conditions | Met | `tests/e2e`; §L |
| 30 | Dashboard production review with operating mode and data classes; readiness scorecard; CLI and docs | Met | §M; this section; README |

**Overall: STAGING_READY.** It is not PRODUCTION_READY, because the runtime items need a
deployment. It is not LIMITED_PRODUCTION_READY either, because the compliance review is
blocked and no deployment exists. FULL_SEARCH_INTELLIGENCE is unavailable, and the original
objective (YouTube Search surge intelligence) is **not** achieved with the data sources
available.

## O. Limitations, risks and next steps

1. **The objective needs data this deployment does not have.** A legitimate
   YouTube Search-interest source is a precondition for FULL mode; until then the
   system is a proxy-research tool and says so.
2. **Real-world accuracy is unknown.** Collect and label real outcomes (≥ 30 surges,
   ≥ 50 gaps, ≥ 100 forecast outcomes) before trusting any number in production.
3. **Compliance review** of derived metrics and scoring under the YouTube API Services
   policies, by a person.
4. **Deploy and observe.** Run the stack on a real host with a real database, configure an
   alert channel, run the smoke test and the restore drill there, watch it for at least
   two weeks, then re-run `trend-agent system readiness`.
5. **Small evaluation sets.** Fixture splits have 8–12 cases; their ratios have wide
   intervals.
6. **Dev-only vitest advisories** remain until npm's installer bug is fixed.
7. **Single host.** The design is deliberately single-host; scale-out beyond multiple
   workers is unproven and unnecessary at the measured load.
