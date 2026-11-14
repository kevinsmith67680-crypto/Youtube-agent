# Phase 8 — initial repository audit

Audit date: 2026-11-14. Baseline: commit `cafa1b8` (end of Phase 7), schema revision `0008`.
This audit was written **before** any Phase 8 change. Each finding states the evidence it
rests on. It is not a list of things that "exist as files": every item was checked by
reading the code path or running a command.

## 1. What was checked and how

| Area | Method |
|---|---|
| Test suite | Full `pytest` run on Python 3.12 against an isolated PostgreSQL database (`trend_agent_test2`). Phase 7 ended with 856 tests passing (851 in the last full run plus 5 added afterwards and run separately). |
| Python dependencies | `pip-audit` against the 60 pinned packages in the project virtualenv (`uv pip freeze`). |
| Python static analysis | `bandit -r src` (68,347 lines). |
| Secret scanning | `detect-secrets` over every tracked file except the npm lockfile and PNGs. `git log --all -p` was also searched for real key formats (`AIza…`, `sk-ant-…`, `ghp_…`). |
| Frontend dependencies | `npm audit`, both the full tree and `--omit=dev`. |
| Live providers | Unauthenticated HTTPS request with `curl -m 10` to each provider host from this environment (results in §3). |
| Policies | Tried to re-read the YouTube API Services Developer Policies. `developers.google.com` does not resolve in this environment, as in Phase 1, so the compliance review relies on `docs/compliance.md` and needs human confirmation (see `api_compliance_audit.md`). |
| Code paths | Read the scheduler and worker loops, the provider registry, the retry helper, the model registry, the API middleware, the dashboard proxy and the configuration validation. |

## 2. Findings

Severity scale:

* **CRITICAL** — blocks the system's stated objective, or blocks any honest production claim.
* **HIGH** — must be fixed before an internet-facing or unattended deployment.
* **MEDIUM** — an operational gap that an operator would hit within weeks.
* **LOW** — hygiene.

### CRITICAL

| ID | Finding | Evidence | Phase 8 response |
|---|---|---|---|
| C1 | **No legitimate source of YouTube Search interest.** No provider in the code base measures YouTube Search interest with real data. The Google Trends provider has no transport: the official alpha contract is supplied by Google on admission, and the provider reports `NOT_CONFIGURED` or `ACCESS_RESTRICTED`. The mock provider is synthetic. | `providers/google_trends.py`; `providers/mock.py`; `trends.google.com` is unreachable from here; no Trends credentials. | Cannot be fixed by code. Phase 8 adds operating modes that **derive** the mode from provider readiness. The highest mode reachable today is `PROXY_OPPORTUNITY_RESEARCH`, and only with a YouTube Data API key. Direct-search claims stay disabled. `FULL_SEARCH_INTELLIGENCE` is reported as BLOCKED. |
| C2 | **Real-world accuracy is unknown.** Every evaluation from Phases 4–7 is synthetic, or uses curated fixtures. The repository has no labelled real observations: no confirmed real surges, no real gap labels, no real forecast outcomes. | `docs/benchmarks/*.json` (all synthetic or fixture origin); no label store in the schema. | A ground-truth registry that separates REAL from SYNTHETIC labels. The quality scorecard reports real-world accuracy as `UNKNOWN` until enough real labels exist. No number is extrapolated from synthetic results. |
| C3 | **The pipeline does not run end to end without an operator.** `monitor scheduler` only syncs targets and plans collection jobs. Detection (`detect worker`), content analysis (`analyze run`), forecasting (`forecast predict`) and recommendations (`recommend generate`) are separate manual commands, and nothing schedules them. Phase 7's "chain test" calls each stage by hand. | `cli_monitor.py` (`monitor_scheduler` = `sync_targets()` + `plan()`); no other periodic loop exists. | A single orchestrating scheduler with a database lease (one owner per schedule). It runs each stage on its own cadence, records every run, and leaves the existing manual commands unchanged. |

### HIGH

| ID | Finding | Evidence | Phase 8 response |
|---|---|---|---|
| H1 | **No single-owner guarantee for schedules.** Two `monitor scheduler` processes would both plan. Job idempotency makes this safe but wasteful. Two copies of any future stage scheduler would run every stage twice: double LLM spend, double alerts. | Advisory locks exist only for detection state, surge episodes and evidence registration. There is no leader lease. | A `scheduler_leases` table with an expiring lease and a fencing token. Only the lease holder runs schedules. Handover after crash or expiry is tested. |
| H2 | **Worker liveness is invisible.** No heartbeat is recorded, so an operator cannot tell whether workers or the scheduler are running. | No heartbeat table; `monitor status` reports the queue only. | A `service_heartbeats` table, written by the API, scheduler and workers; `/health/workers` reads it. |
| H3 | **The health endpoint does not check anything.** `/api/health` always answers `ok`, even when the database is down. There are no liveness or readiness, provider, worker or database checks. | `api/app.py::health`. | `/health/live`, `/health/ready`, `/health/providers`, `/health/workers`, `/health/database`, backed by real checks. |
| H4 | **Unsafe production defaults are accepted.** With `ENVIRONMENT=production`, the settings still accept: (a) the hard-coded development database URL and password; (b) a missing API token; (c) localhost CORS origins; (d) demo mode. Only the verification signing key is enforced. | `config.py` (`DEFAULT_DATABASE_URL`); `detect-secrets` flags it. | A production configuration check that refuses to start in production with development secrets or an insecure setup. `system audit` reports the same checks. |
| H5 | **The dashboard has no authentication.** The Next.js server attaches the API bearer token to every same-origin request it proxies. It also uses the token to render private pages (saved ideas, profile, alerts). Anyone who can reach the dashboard port can read private data, save or dismiss ideas, and trigger LLM-backed generation, which costs money. | `frontend/app/api/proxy/[...path]/route.ts` (`authHeaders()` on every forward); `frontend/lib/api.ts`. | Dashboard authentication: HTTP Basic, enforced by Next.js middleware on every route. It is mandatory in production; the server refuses to serve production without credentials. |
| H6 | **Spending is not capped.** Budgets exist only for LLM generation per day (recommendations) and for the YouTube quota. There is no monthly LLM budget, no daily API budget across providers, no limit on recommendations per run or analysis requests per topic, and no degraded mode when budgets run out. | `recommendations/service.py` (daily LLM budget); `providers/quota.py`. | A cost ledger and budget policy for the seven limits named in the spec, a `DEGRADED` mode when a budget is exhausted, and `system costs`. |
| H7 | **Hidden Unicode direction-control characters in source** (a "Trojan Source" pattern). A regex in the untrusted-text sanitiser contained literal U+202A–U+202E and U+2066–U+2069 characters. The regex itself behaved correctly, but the line cannot be reviewed safely. | `bandit` B613 (HIGH) on `verification/untrusted.py:32`. | **Fixed during the audit**: rewritten with `\u` escapes. Equivalence was checked over all code points. |
| H8 | **Vulnerable frontend dependencies.** The production build chain used `postcss` ≤ 8.5.22, including the copy bundled inside Next.js (high: XSS in stringify, arbitrary `.map` read). `vitest` and `tinypool` (dev only) are rated critical. | `npm audit`: 2 critical, 1 high, 2 moderate. | **Fixed during the audit for production dependencies**: `postcss` 8.5.32, forced into Next.js with an npm override. `npm audit --omit=dev` now reports 0. The dev-only `vitest` advisories are partly mitigated (3.2.4 → 3.2.7) and recorded in `security_audit.md` (see the note after the tables). |
| H9 | **No REJECTED model status and no rollback.** The model registry has no REJECTED status, so a model that fails validation stays `experimental`. A bad promotion can only be undone by hand-editing the database. | `schemas/model_metadata.py` (`ModelStatus`); a `known_status` check constraint on `forecast_models`. | Add `REJECTED`, and an explicit `rollback` that restores the previous champion with an audit event. Retrained models are never promoted automatically. |
| H10 | **Permanent failures disappear.** A collection job that fails permanently ends as `FAILED` inside the job table. A source record that fails validation is counted, then dropped. Nothing groups these for an operator. There are also no system-level operational alerts (scheduler down, budget exhausted, provider unauthorised). | `monitoring/executor.py`; `schemas/monitoring.py` (`ErrorCategory`). | A `dead_letters` table, plus `ops_alerts` with fingerprint deduplication and a cooldown. |

**Note on H8 (vitest).** The fixed vitest (4.1.12) is a major version. Installing it fails with an npm arborist bug: `Cannot read properties of null (reading 'edgesOut')`. The remaining advisories cover the vitest UI server and its mock registration, which are not used here (the tests run headless in CI and locally).

### MEDIUM

| ID | Finding | Phase 8 response |
|---|---|---|
| M1 | No metrics endpoint. There are no request or stage counters, and no latency histograms. | `/metrics` in Prometheus text format, built in-process with no new dependency. |
| M2 | No correlation IDs. API log lines cannot be tied to the stage runs they trigger. | An `X-Request-ID` middleware; the ID goes into structured logs and is propagated into the scheduler's stage runs. |
| M3 | No backup or restore procedure, and nothing has ever been restored. | `scripts/backup.sh`, `scripts/restore.sh`, and a restore drill run against a scratch database with row-count verification. |
| M4 | No experiment registry. Evaluation results are scattered in `docs/benchmarks/*.json` with no record of the decision taken. | An `experiments` table, `evaluate` commands that register their runs, and adopt/reject decisions with a reason. |
| M5 | Production tracking stops at "published". The system records the video ID but no outcome. | Opt-in outcome records entered by the creator. Nothing is fetched from private analytics without authorisation, and nothing is published automatically. |
| M6 | The YouTube policy text could not be re-read here (`developers.google.com` blocked). | `api_compliance_audit.md` lists every assumption for human verification. Restricted features stay disabled by default. |
| M7 | The dashboard does not show the system's operating mode, or whether a signal is direct, external or proxy at system level. | An operating-mode banner and data-class legend in the dashboard. |
| M8 | Deployment artifacts are missing: no Dockerfile for the API, scheduler, worker or frontend, and no CI workflow. `docker-compose.yml` runs PostgreSQL only. | Dockerfiles, a compose file with separate services, health checks, a migration step and GitHub Actions CI. |

### LOW

| ID | Finding |
|---|---|
| L1 | `bandit` LOW findings: 74 × `assert` (internal invariants), 4 × `random` used for jitter and sampling (not cryptographic), 3 × false "hardcoded password" hits on environment variable *names*, 1 × `try/except/pass`, and 3 subprocess warnings for the dashboard UI launcher (fixed argument list, no shell). None is exploitable; recorded in `security_audit.md`. |
| L2 | `detect-secrets` hits are all development or test fixtures: the local compose password `trend_agent_dev`, fake keys in tests, variable names, and a SHA-256 data hash. No real credential appears in the working tree or in history. |
| L3 | An empty stray file `/smoke.py` exists outside the repository. It is not tracked and not part of the project. |

## 3. Provider reachability from this environment (2026-11-14)

| Provider | Signal class | Result |
|---|---|---|
| YouTube Data API v3 | VIDEO_ACTIVITY | Reachable, but `403` without a key. No key is configured. |
| YouTube Analytics API | (owner analytics) | `401` without OAuth. Not used. |
| Google Trends (official alpha) | EXTERNAL_SEARCH_INTEREST; YouTube property only via the alpha API | No access, endpoint unknown, `trends.google.com` blocked. |
| GitHub Releases API | OFFICIAL_EVENT_SIGNAL | `403` from this session's proxy, which allows only the repositories attached to the session. A normal deployment reaches it, at unauthenticated limits unless `TREND_AGENT_GITHUB_TOKEN` is set. |
| Hacker News, Reddit, Steam, YouTube search suggestions | COMMUNITY_ACTIVITY / OFFICIAL_EVENT_SIGNAL / SEARCH_SUGGESTION_PRESENCE | Blocked by this environment's network policy (connection refused before TLS). |
| Synthetic / mock | SYNTHETIC_TEST_DATA | Always available; isolated. |

## 4. What works today (verified, not assumed)

* Every stage runs from the CLI on synthetic data, and Phase 7's database chain test exercises the Phase 4 → 5 → 6 → 7 stages against PostgreSQL.
* The API protects private routes and mutations with a bearer token (tests: `tests/unit/recommendations/test_api.py`). It also applies rate limits, a body-size limit, CORS restricted to configured origins, and security headers.
* Provider HTTP calls retry only retryable errors, with full-jitter exponential backoff. They honour `Retry-After` up to a cap (`providers/http.py`).
* Collection jobs use `SKIP LOCKED` leases with expiry. Writes are idempotent, so a duplicate planner is safe.
* The data-class separation of DIRECT / EXTERNAL / PROXY / SYNTHETIC already runs end to end: from measurement categories, through detection, forecasts and recommendations, to dashboard labels.

## 5. Implementation plan

Ordered by severity. Critical integration issues come first. Every step lands with tests.

1. **Readiness and modes** (C1, C2, M7): provider capability readiness, operating modes with an allowed-capability matrix, `system providers`, `system readiness`, and `live_data_readiness.md`.
2. **Operations core** (C3, H1, H2, H3, H6, H10, M1, M2): migration `0009` with the tables `service_heartbeats`, `scheduler_leases`, `scheduled_runs`, `cost_ledger`, `ops_alerts`, `dead_letters`, `evaluation_labels`, `experiments`, `recommendation_outcomes`, plus the `rejected` model status. Then the orchestrating scheduler, worker heartbeats, health endpoints, metrics, budgets and degraded mode, and the production configuration check (H4).
3. **Security** (H5, H4): dashboard authentication and production configuration guards; `security_audit.md`.
4. **Evaluation** (C2, M4, H9): ground-truth registry; pipeline, detection, forecast, gap and recommendation evaluations with explicit dataset origin and splits; walk-forward backtest with availability timestamps; experiment registry; quality scorecard; model maintenance (REJECTED, rollback).
5. **Deployment** (M8, M3): Dockerfiles, compose services, CI, backup and restore with a drill.
6. **Verification**: 40 end-to-end scenarios, load test, regression on 3.12 and 3.13, frontend tests, readiness scorecard and final report.

Nothing in this plan can create a source of YouTube Search interest. The release state will be decided by the readiness scorecard, which separates FAIL from BLOCKED and never counts BLOCKED as PASS.
