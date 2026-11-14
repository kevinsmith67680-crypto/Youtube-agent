# YouTube Trend Agent — Phases 1–6: Data Acquisition, Keyword Discovery, Historical Monitoring, Surge Detection, Content-Gap Analysis & Trend Forecasting (+ verification layer)

An autonomous intelligence platform (in progress) for understanding **YouTube search
demand**: what people search for, which searches are surging, which are under-served by
existing videos, and what videos to make. This repository contains **Phases 1–6 of 8**:
the typed foundation, data-acquisition layer, PostgreSQL schema, validation and compliance
controls (Phase 1), an autonomous **keyword discovery engine** for AI, technology and
gaming (Phase 2), **historical monitoring** with time-series storage and scheduling
(Phase 3), a **statistical surge detection engine** (Phase 4), **search-intent and
content-gap analysis** (Phase 5), and **predictive trend forecasting with opportunity-window
estimation** (Phase 6).

> **Read this first — what the data can and cannot tell you.**
> YouTube does **not** publish search volume. The YouTube Data API has no endpoint that
> returns how often a keyword is searched, now or historically. This project therefore
> labels every record with *what it actually measures* (`MeasurementType`):
> `DIRECT_SEARCH_INTEREST` (YouTube-search-specific interest from a provider that really
> measures it), `SEARCH_SUGGESTION` (discovery only), `VIDEO_ACTIVITY` (video supply /
> attention, e.g. YouTube API metadata), `EXTERNAL_SEARCH_INTEREST` (e.g. Google *web*
> search), and `UNKNOWN`. Video views are never converted into search counts, and no
> search-volume data is ever fabricated.

## What Phase 1 provides

| Area | Status |
|---|---|
| YouTube Data API v3 provider (search, video details, channels, most-popular charts, categories) | Implemented; real authenticated requests when a key is configured |
| Quota management (per-bucket, Pacific-midnight reset, safety margin, budgets, typed exhaustion) | Implemented |
| Google Trends provider (official API is limited-access alpha) | Adapter + honest `NOT_CONFIGURED` / `ACCESS_RESTRICTED` status; no transport shipped (see [data sources](docs/data_sources.md)) |
| Deterministic synthetic provider (12 scenarios) | Implemented, labelled `SYNTHETIC` |
| Validation, quarantine, comparability checks | Implemented |
| PostgreSQL schema + Alembic migrations | Implemented |
| Retention / compliance controls (30-day expiry, statistics persistence off by default) | Implemented |
| CLI (`trend-agent`) | Implemented |

## What Phase 2 adds — keyword discovery

Discovery finds **candidate** keywords and explains where each came from; it never
estimates demand. Details: [docs/keyword_discovery.md](docs/keyword_discovery.md).

| Area | Status |
|---|---|
| Discovery providers: YouTube Data API (search/charts/channel uploads), RSS/Atom feeds, GitHub releases, Hacker News, Reddit (OAuth), Google Trends related queries, curated seeds, synthetic fixture | Implemented. In the build environment only PyPI feeds were reachable live; see [phase2 report §C](docs/phase2_report.md#c-working-discovery-sources) |
| Grounded entity extraction (no LLM needed; optional LLM interface with grounding checks) | Implemented |
| Expansion with origin labels (`observed_query` / `generated_hypothesis`), grounded templates, limits | Implemented |
| Intent (13) + category classification, two-level clustering with decision records | Implemented |
| Novelty + `priority-v1` scoring (components stored), lifecycle with logged transitions | Implemented |
| Budget allocation (AI 45 / tech 25 / gaming 30, 20 % exploration), scheduling, quotas | Implemented |
| YAML configuration (`config/`), migration `0002`, `trend-agent discover` CLI | Implemented |

## What Phase 3 adds — historical monitoring

Monitoring turns prioritised candidates into **monitoring targets** and collects their
history from sources that can actually measure them, with full provenance. Details:
[docs/phase3_architecture.md](docs/phase3_architecture.md).

| Area | Status |
|---|---|
| Targets with category/priority/region/provider, separate validated lifecycle (pending, active, paused, provider_unavailable, quota_blocked, stale, dormant, disabled) | Implemented |
| Provider freshness/capability profiles; delay estimator; freshness statuses | Implemented |
| Cadence-aware scheduler with tiers (critical…dormant), category budgets 45/25/30 + 20 % exploration, starvation guard, quota-aware planning (DST-aware resets) | Implemented |
| PostgreSQL job queue: `SKIP LOCKED` claiming, leases, fencing, crash recovery, retries with jitter | Implemented (real-thread concurrency tested) |
| Revision-aware, idempotent time-series storage (new / unchanged / revised / conflicting / missing) | Implemented |
| Comparability engine, baseline readiness, data quality, backfill, deterministic replay | Implemented |
| Real search-interest source | **None available here**: Google Trends API alpha not configured; YouTube Data API has no search interest. Proxy event signals and synthetic providers work |

## What Phase 4 adds — statistical surge detection

Detection decides, causally and explainably, whether what a source measures rose
abnormally or persistently, and keeps one persistent, deduplicated **surge episode** per
event. Details: [docs/phase4_architecture.md](docs/phase4_architecture.md),
[docs/statistical_detection.md](docs/statistical_detection.md),
[docs/surge_alerts.md](docs/surge_alerts.md).

| Area | Status |
|---|---|
| Causal preprocessing (knowledge at T, comparability, period grid, missing ≠ zero) | Implemented |
| Robust rolling (median/MAD, zero-MAD handling) and seasonal (weekday / hour-of-day, Kruskal–Wallis-gated) baselines | Implemented |
| Rapid anomaly detector, one-sided CUSUM, Bayesian online change points, cold-start states | Implemented (persisted state; incremental == recomputed) |
| Strict evidence classes: only direct YouTube search interest can be CONFIRMED; external → `EXTERNAL_SEARCH_SURGE`; video/suggestion/event/community → proxy attention | Implemented (policy, contract validator, DB constraint) |
| Calibrated p-values (empirical null + GPD tail), per-run Benjamini–Hochberg/Yekutieli, daily alert budget | Implemented; calibrations exist for **synthetic** data only |
| Severity components, episodes (dedupe, escalate, downgrade, resolve, reopen), deterministic explanations, `surge-alert/1.0` contract for Phase 5 | Implemented |
| Phase 3 integration: transactional outbox + `LISTEN/NOTIFY` worker, `monitor run --once` hook | Implemented |
| Causal replay, walk-forward calibration, independent synthetic evaluation | Implemented — results in [docs/phase4_report.md](docs/phase4_report.md); **synthetic only**, real-world accuracy unknown |

Not in scope yet (by design): search-intent and content-gap analysis (Phase 5),
forecasting, dashboard.

## Verification layer — anti-hallucination gate

Every recommendation (from a template, a person or an optional LLM) passes a
deterministic **verification gate** before it can be shown. Details:
[docs/verification_layer.md](docs/verification_layer.md),
[docs/verification_evaluation.md](docs/verification_evaluation.md),
[docs/verification_report.md](docs/verification_report.md).

| Area | Status |
|---|---|
| Persistent, versioned evidence registry: signed, application-issued IDs, per-request bundles, integrity re-checked on use, immutable content (DB trigger), retention copied from sources | Implemented |
| Claim verification (declared + extracted from title/rationale/notes): VERIFIED / CORROBORATED / UNVERIFIED / CONTRADICTED, support checked per claim (entities, numbers, dates, release wording, content), credible contradictions, freshness, attribution | Implemented (deterministic, English-only, lexical) |
| Statistical authority: growth, scores, severity, significance, freshness and readiness only from Phase 4; no search volumes; proxy activity never presented as search growth | Implemented |
| Entity verification: canonical resolution, ambiguity, unknown names, confirmed vs reported vs rumoured, release dates, denials, conflicts | Implemented |
| Prompt-injection protection: no generator tools, least-privilege permission policy, read-only assembly, data blocks with escaped markup, output URL/self-certification checks | Implemented |
| Abstention (VERIFIED_RESULT, PARTIALLY_VERIFIED, INSUFFICIENT_EVIDENCE, CONTRADICTED, SOURCE_UNAVAILABLE) and publication decided by code; bounded retries | Implemented |
| Optional Claude adapter (structured output, refusal handling, server-side fallbacks) | Implemented; **not run against the live API here** (fake-client tests only) |
| Adversarial evaluation (58 labelled fictional cases, dev + held-out) | 0 labelled unsupported claims published; held-out first run: precision 1.00, false rejection 0.10, contradiction recall 0.60 — **fictional data, real-world accuracy unknown** |

## What Phase 5 adds — search intent & content-gap analysis

For a query behind a Phase 4 surge event (or one an operator names), Phase 5 classifies
the search intent, lists what searchers appear to need, inspects the YouTube results it
may retrieve, and reports — with evidence and limitations — whether a **verifiable**
content gap exists. Reports are research priorities, not forecasts; no titles, thumbnails
or scripts are generated. Details: [docs/phase5_architecture.md](docs/phase5_architecture.md),
[docs/search_intent_analysis.md](docs/search_intent_analysis.md),
[docs/content_gap_detection.md](docs/content_gap_detection.md),
[docs/evidence_verification.md](docs/evidence_verification.md),
[docs/opportunity_scoring.md](docs/opportunity_scoring.md),
[docs/phase5_evaluation.md](docs/phase5_evaluation.md),
[docs/phase5_report.md](docs/phase5_report.md).

| Area | Status |
|---|---|
| Phase 4 demand evidence preserved and labelled (confirmed direct / candidate / external / potential-proxy / cold start / watch / insufficient / manual); never recalculated or upgraded | Implemented |
| Hierarchical intent classification (10 primaries; AI, technology, gaming sub-intents), entities and versions, ambiguity, policy blocking of cheats/exploits | Implemented (deterministic rules, English-only) |
| Query decomposition into candidate information requirements (no invented product details or item locations) | Implemented |
| Bounded YouTube retrieval through the Phase 1 ingestion service (quota, 24 h cache, 30-day retention); API order is not treated as ranking | Implemented; **not run against the live API here** (fake transport) |
| Relevance, freshness (age alone never "outdated"), requirement coverage, scope-limited supply, ten gap types with abstention first | Implemented (metadata tier; captions disabled) |
| Opportunity score (six components, versioned weights, missing ≠ zero, capped by demand basis; not a probability) | Implemented |
| Application-side claim verification; prompt-injection defences; optional Claude assist with structured output | Implemented; LLM **off by default**, adapter tested with a fake client only |
| Migration 0006 (7 tables), idempotent writes, retention, cost-aware queue, `trend-agent analyze …` | Implemented |
| Labelled evaluation (32 fictional cases, chronological dev / calibration / held-out) | Held-out first run: gap precision 0.57, false-gap rate 0.50 (fixed afterwards; re-run is not held-out) — **fictional data, real-world accuracy unknown** |

## What Phase 6 adds — predictive trend forecasting & opportunity windows

For each monitored series, Phase 6 forecasts, per horizon (6 / 24 / 72 h; daily data never
gets a 6-hour forecast), the probability that relative interest **breaks out** (or, if
already elevated, **continues**), the expected **trajectory** with an interval, a
censoring-aware **peak** range, and **opportunity windows**: whether interest is likely to
still be elevated after a hypothetical production time (2 h / 8 h / 24 h). Every forecast
can abstain with a typed status. Details: [docs/phase6_architecture.md](docs/phase6_architecture.md),
[docs/forecast_features.md](docs/forecast_features.md),
[docs/forecast_targets.md](docs/forecast_targets.md),
[docs/model_training.md](docs/model_training.md),
[docs/forecast_accuracy.md](docs/forecast_accuracy.md),
[docs/forecast_uncertainty.md](docs/forecast_uncertainty.md),
[docs/opportunity_windows.md](docs/opportunity_windows.md),
[docs/phase6_report.md](docs/phase6_report.md).

> Forecasts describe **relative interest of the measured source**: never search counts,
> never view predictions. A proxy (video/community activity) forecast is labelled
> `proxy_attention`, never search demand. All accuracy figures are on **synthetic** data;
> no real held-out data exists here, so **real-world accuracy is unknown**.

| Area | Status |
|---|---|
| Causal datasets: point-in-time joins on publication/availability times and revisions, independent labels with maturity rules, leakage audit in every manifest | Implemented (audit: 0 mismatches) |
| 52 versioned features (growth, acceleration, volatility, Phase 4 detector state, seasonality, novelty, attention, Phase 5 content gaps, quality), comparability-checked | Implemented |
| Baselines (climatology, persistence, seasonal naive, damped trend), logistic regression, LightGBM classifier/regressor; chronological splits with embargo and unseen topics; rolling origins | Implemented |
| Calibration (isotonic/Platt with statuses), split-conformal intervals, OOD detection, eight forecast statuses including abstention | Implemented |
| Peak ranges with right-censoring; opportunity windows from explicit production profiles | Implemented (**experimental**) |
| Model registry (checksummed JSON/LightGBM text, no pickle), explicit promotion, one champion per scope, champion–challenger, drift (never automatic) | Implemented |
| Migration 0007 (8 tables), idempotent hourly-issued forecasts, retention, typed `forecast/1.0` contract for Phase 7, `trend-agent forecast …` | Implemented |
| Synthetic evaluation (360 series, untouched test periods, independent held-out corpus) | LightGBM selected for hourly 24/72 h and daily 72 h; logistic where LightGBM was not significantly better; one champion (daily 24 h) failed to generalise to the held-out corpus — **synthetic only** |

## Architecture overview

A modular monolith (`src/trend_agent/`):

```
providers/   provider protocols, registry, YouTube, Google Trends, mock, quota, retry
schemas/     provider-neutral domain models and enums (the data contracts)
ingestion/   validation, storage/retention policy, ingestion orchestration
discovery/   Phase 2: discovery providers, extraction, expansion, intent, clustering,
             evidence, scoring, lifecycle, budget, DiscoveryService
monitoring/  Phase 3: targets, scheduler, planner, executor, freshness, comparability,
             readiness, quality, backfill, replay, metrics, MonitoringService
detection/   Phase 4: preprocessing, baselines, anomaly, CUSUM, change points, cold
             start, significance, severity, alert policy, episodes, replay, evaluation
db/          SQLAlchemy models, session management, repositories (all SQL lives here)
comparability.py   decides whether two observations' values may be compared
container.py composition root (dependency injection), cli*.py Typer CLI
verification/  evidence registry, claim extraction/verification, entities, statistical
             authority, untrusted-content handling, permissions, generators, gate
content_analysis/  Phase 5: intent, decomposition, retrieval, relevance, freshness,
             coverage, supply, gaps, scoring, LLM assist, verification, queue, service
forecasting/ Phase 6: point-in-time feature store, targets, features, eligibility,
             dataset builder, evaluation, baselines, training, trajectory, calibration,
             uncertainty, peaks, opportunity windows, registry, drift, explanations,
             replay adapter, ForecastingService
config/      discovery.yaml, seeds/, sources/ (Phase 2); monitoring.yaml (Phase 3);
             detection.yaml (Phase 4); verification.yaml + verification/ (fixtures);
             content_analysis.yaml + content_analysis/ (Phase 5 fixtures, eval splits);
             forecasting.yaml (Phase 6)
```

Data flows `provider -> candidates -> validation -> storage policy -> one DB transaction
-> ingestion run record -> summary`. See [docs/architecture.md](docs/architecture.md) and,
for discovery, [docs/phase2_architecture.md](docs/phase2_architecture.md).

## Prerequisites

* Python **3.12+** (tested on 3.12 and 3.13)
* Docker with Docker Compose (for the local PostgreSQL 16)
* Optional: a YouTube Data API v3 key (for live YouTube calls)
* Optional: [`uv`](https://docs.astral.sh/uv/) (faster installs; `pip` works too)

## Installation

```bash
git clone <this repository> && cd Youtube-agent
python3.12 -m venv .venv && source .venv/bin/activate      # or: uv venv --python 3.12
pip install -e ".[dev]"                                      # or: uv pip install -e ".[dev]"
cp .env.example .env                                         # then edit .env
trend-agent --help
```

## Environment configuration

All settings are environment variables prefixed `TREND_AGENT_` (or lines in a local
`.env`, which is git-ignored). `.env.example` documents every variable and its default.
The most important ones:

| Variable | Purpose |
|---|---|
| `TREND_AGENT_DATABASE_URL` | PostgreSQL URL (default matches `docker-compose.yml`) |
| `TREND_AGENT_YOUTUBE_API_KEY` | YouTube Data API v3 key. Without it, YouTube commands report `not_configured`. |
| `TREND_AGENT_YOUTUBE_DAILY_UNIT_LIMIT` / `..._SEARCH_DAILY_CALL_LIMIT` | Your project's real quota (defaults: 10,000 units; 100 `search.list` calls) |
| `TREND_AGENT_YOUTUBE_QUOTA_SAFETY_MARGIN_PCT` | Keep this % of each bucket unused (default 10) |
| `TREND_AGENT_YOUTUBE_PERSIST_STATISTICS` | Store view/like/comment counts. **Default `false`** — read [docs/compliance.md](docs/compliance.md) first. |
| `TREND_AGENT_GOOGLE_TRENDS_ACCESS_MODE` | `disabled` (default) or `official_alpha` |
| `TREND_AGENT_GOOGLE_TRENDS_RETENTION_DAYS` | Empty = Trends data is never stored |
| `TREND_AGENT_DISCOVERY_CONFIG_DIR` | Discovery configuration directory (default `./config`) |
| `TREND_AGENT_HTTP_USER_AGENT` | User-Agent for feeds and public APIs (put a real contact in it) |
| `TREND_AGENT_GITHUB_TOKEN` | Optional read-only GitHub token (higher rate limit) |
| `TREND_AGENT_REDDIT_CLIENT_ID` / `..._SECRET` | Reddit OAuth app; Reddit sources are `not_configured` without them |
| `TREND_AGENT_FORECAST_ARTIFACT_DIR` | Where checksummed forecast model artifacts are stored (default `var/forecast_models`) |

Getting a YouTube key: Google Cloud Console → create/select a project → *APIs & Services*
→ enable **YouTube Data API v3** → *Credentials* → *Create API key* → restrict the key to
that API. The key is sent only in the `X-Goog-Api-Key` header, never in URLs or logs.

## PostgreSQL setup

```bash
docker compose up -d            # PostgreSQL 16 on 127.0.0.1:5432 (UTC), plus a
                                # separate `trend_agent_test` database for the test suite
docker compose ps               # wait for "healthy"
```

Data persists in the `trend_agent_pgdata` volume (`docker compose down -v` deletes it).
To use another server, set `TREND_AGENT_DATABASE_URL` (PostgreSQL **15+** required: the
schema uses `UNIQUE NULLS NOT DISTINCT`).

## Database migrations

Run from the repository root (where `alembic.ini` lives):

```bash
trend-agent db upgrade          # == alembic upgrade head
trend-agent db current          # current vs. latest revision
alembic downgrade base          # drop everything (development only)
```

## CLI

Logs are JSON on **stderr** (WARNING+ by default, `-v` for INFO); results go to stdout.
Most commands accept `--json`.

```bash
trend-agent doctor [--live]                 # config (secrets redacted), DB, schema, providers
                                            # --live spends 1 quota unit to validate the key
trend-agent providers list                  # capabilities, measurement types, retention rules
trend-agent trends status                   # Google Trends access + supported granularity

trend-agent youtube search --query "artificial intelligence" --region GB --limit 20
trend-agent youtube popular --region GB --limit 20 [--category 28]
trend-agent youtube categories --region GB
#   add --persist to store results (subject to retention policy)
trend-agent youtube refresh                 # refresh metadata nearing 30-day expiry

trend-agent ingest --provider mock --query "AI agents" [--scenario sudden_surge] [--days 90]
trend-agent data inspect --query "AI agents"  # observations, provenance, comparability groups
trend-agent data runs --rejections          # ingestion runs and quarantined records
trend-agent data retention [--purge]        # expired third-party data

trend-agent quota status                    # local quota estimate per bucket

# Phase 2: keyword discovery (run from the repository root, or set TREND_AGENT_DISCOVERY_CONFIG_DIR)
trend-agent discover demo                   # full pipeline on the SYNTHETIC fixture (no network/quota)
trend-agent discover run --category ai      # or --category gaming, --all; --dry-run plans only
trend-agent discover keywords --category gaming --limit 50   # [--include-synthetic] [--status ...]
trend-agent discover show --query "glacier lance location"   # evidence, scores, clusters, history
trend-agent discover expand --entity "Nimbus Coder 2"        # grounded hypotheses + skip reasons
trend-agent discover entities --recent [--days 7]
trend-agent discover sources                # providers, readiness, source health
trend-agent discover clusters               # topic -> intent clusters with merge decisions
trend-agent discover stats [--days 7]
```

```bash
# Phase 3: historical monitoring (see docs/monitoring_operations.md)
trend-agent monitor demo                    # SYNTHETIC end-to-end demo, scenarios A-J (checked)
trend-agent monitor sync                    # select/activate targets from discovered keywords
trend-agent monitor targets [--include-synthetic]
trend-agent monitor plan --dry-run          # planned jobs + estimated quota, nothing executed
trend-agent monitor run --once              # sync + plan + execute eligible jobs once
trend-agent monitor scheduler / worker      # long-running processes (share only PostgreSQL)
trend-agent monitor status | providers | freshness | readiness | quality | quota
trend-agent monitor history --query "AI coding agents"
trend-agent monitor backfill --query "AI coding agents" --provider mock --days 30
trend-agent monitor replay --query "AI coding agents" --provider mock
trend-agent monitor activate|pause|resume|disable|override --query "..."

# Phase 4: surge detection (see docs/statistical_detection.md, docs/surge_alerts.md)
trend-agent detect demo                     # SYNTHETIC end-to-end demo, scenarios A-N (checked)
trend-agent detect run --all                # detect on new/revised observations (also --category ai|technology|gaming, --query)
trend-agent detect worker                   # event-driven: LISTEN for new observations (Phase 3 outbox)
trend-agent detect alerts --active          # open surge episodes (synthetic hidden unless --include-synthetic)
trend-agent detect explain --event-id 12    # evidence, reasoning, transitions, versioned payload
trend-agent detect query --query "AI coding agents"
trend-agent detect readiness | thresholds | stats | schema
trend-agent detect replay --query "AI coding agents" --provider mock
trend-agent detect calibrate --dataset synthetic
trend-agent detect evaluate --dataset synthetic

# Verification layer (see docs/verification_layer.md)
trend-agent verify demo                     # offline walk-through of labelled fictional cases
trend-agent verify evaluate --split all     # labelled adversarial evaluation (offline)
trend-agent verify recommend --target 42    # evidence -> draft (template by default) -> gate
trend-agent verify check --target 42 --draft draft.json   # verify an externally written draft
trend-agent verify runs | explain <run-id> | evidence [--id ev_...] | stats | schema

# Phase 5: search intent & content-gap analysis (see docs/phase5_architecture.md)
trend-agent analyze demo                    # SYNTHETIC scenarios A-J, offline (checked)
trend-agent analyze evaluate --split all    # labelled evaluation, offline (also --sweep)
trend-agent analyze query --query "AI coding agents" [--category ai] [--no-llm] [--dry-run]
trend-agent analyze surge --event-id 12     # analyse the query behind a Phase 4 surge event
trend-agent analyze pending [--category ai] # what the cost-aware queue would analyse, and why not the rest
trend-agent analyze run --category gaming   # run the queue once (budgets, priorities, cache)
trend-agent analyze results --query "AI coding agents"   # inspected results, relevance, freshness
trend-agent analyze intent --query "AI coding agents"    # intent + information requirements only
trend-agent analyze gaps --category gaming  # detected gaps (hypotheses) across analyses
trend-agent analyze opportunities --limit 20
trend-agent analyze explain --opportunity-id ID          # full report, score components, claims
trend-agent analyze verify --opportunity-id ID           # re-check claims against the registry now
trend-agent analyze status                  # usage vs budgets, capability mode, versions

# Phase 6: forecasting (see docs/phase6_architecture.md). Training and inference are separate.
trend-agent forecast demo                   # SYNTHETIC scenarios A-M, offline (trains in memory)
trend-agent forecast replay --dataset synthetic         # causal walk-forward replay, offline
trend-agent forecast schema [--which features]          # forecast/1.0 output contract, feature schema
trend-agent forecast eligibility [--category ai]        # which series/horizons can be forecast, and why not
trend-agent forecast features --query "AI coding agents" # point-in-time features with provenance
trend-agent forecast train [--horizon 24h]   # real monitored data only (refuses synthetic rows)
trend-agent forecast train --dataset synthetic [--scale 0.5] [--promote]  # isolated experiment
trend-agent forecast models                  # registry: scope, origin, status, metrics
trend-agent forecast promote MODEL_ID        # explicit; VALIDATED/CHALLENGER only (rollback MODEL_ID)
trend-agent forecast evaluate --horizon 24h  # re-score champions on a fresh untouched test period
trend-agent forecast compare                 # champion vs challengers on the same rows
trend-agent forecast predict [--query Q | --category C] [--dry-run]   # idempotent, stored
trend-agent forecast explain --prediction-id ID          # evidence, contributions, limitations
trend-agent forecast peaks | windows [--status URGENT]   # stored peak ranges / opportunity windows
trend-agent forecast drift [--days 7]        # data vs concept drift; recommendations only
trend-agent forecast status
```

Forecast listings hide synthetic series unless `--include-synthetic` is given. Models
trained with `--dataset synthetic` serve synthetic series only; a real series without a
real validated model gets a `BASELINE_ONLY` forecast.

Without `TREND_AGENT_YOUTUBE_API_KEY`, `analyze` runs **cache-only** (stored results;
analyses abstain when none exist). The LLM assist is off by default (`llm.enabled` in
`config/content_analysis.yaml`, plus `TREND_AGENT_ANTHROPIC_API_KEY` and the `anthropic`
package); without it, analyses are deterministic and say so.

`discover run` fetches within budgets, quotas and poll intervals (`--force` ignores
intervals, never quotas). Keyword listings hide synthetic data unless
`--include-synthetic` is given. Priority ranks candidates for monitoring; it is **not**
search volume.

Mock scenarios: `stable`, `gradual_growth`, `sudden_surge`, `declining`, `sparse`,
`missing_data`, `out_of_order`, `duplicates`, `mixed_normalization`, `invalid_records`,
`provider_failure`, `rate_limited`. All mock output is labelled synthetic.

Exit codes: `0` success, `1` error, `2` missing configuration, `3` database unavailable,
`130` interrupted (an in-flight ingestion run is marked `cancelled`).

Quota costs (defaults, configurable): each `search.list` page = 1 call from the 100/day
search bucket; each `videos.list`, `channels.list`, `videoCategories.list` request = 1 unit
from the 10,000/day pool (video/channel lookups are batched 50 IDs per request).

## Testing

```bash
docker compose up -d                       # integration tests need the test database
pytest                                     # unit + integration; live tests excluded
pytest --cov                               # with coverage
pytest tests/unit                          # no database, no network
ruff check . && ruff format --check . && mypy && mypy tests --strict
```

* Unit tests never touch the network: YouTube HTTP is served by `httpx.MockTransport`
  using fixtures in `tests/fixtures/`.
* Integration tests use `TREND_AGENT_TEST_DATABASE_URL` (default: the `trend_agent_test`
  database). They are **skipped** if it is unreachable; set
  `TREND_AGENT_REQUIRE_DB_TESTS=1` to make that a failure (recommended in CI).
* Live tests (`tests/live`) consume real quota and are opt-in only:
  `TREND_AGENT_RUN_LIVE_TESTS=1 TREND_AGENT_YOUTUBE_API_KEY=... pytest -m live tests/live`
  (the PyPI feed test needs no key: `TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_discovery_feeds_live.py`).
* Discovery tests use the synthetic fixture and mocked HTTP only.
* Forecasting tests train small models on the synthetic corpus in memory (fixed seeds,
  about 2 minutes for `tests/unit/forecasting`); the integration tests train into the
  isolated test database. Benchmarks: `scripts/benchmark_forecasting.py` (resets a
  `*test*`/`*bench*` database); synthetic evaluation: `scripts/evaluate_forecasting.py`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `database unavailable` / exit code 3 | `docker compose up -d`; check `TREND_AGENT_DATABASE_URL`; `trend-agent doctor` |
| `Schema is not at head` | `trend-agent db upgrade` (from the repository root) |
| `[not_configured] ... TREND_AGENT_YOUTUBE_API_KEY` | Add the key to `.env` |
| `[invalid_credentials]` | Key wrong, deleted, or restricted to other APIs/referrers |
| `[access_restricted] ... accessNotConfigured` | Enable *YouTube Data API v3* for the key's Cloud project |
| `[quota_exhausted]` | Wait for the reset (midnight Pacific) shown by `trend-agent quota status`; local estimates can differ from Google's — the Cloud Console is authoritative |
| `invalid region code 'UK'` | Use ISO 3166-1 codes (`GB`) |
| Google Trends `not_configured` | Expected: the official API is limited-access alpha. See [docs/data_sources.md](docs/data_sources.md) |
| `discovery configuration: ... not found` (exit 2) | Run from the repository root or set `TREND_AGENT_DISCOVERY_CONFIG_DIR` |
| Discovery source `access_restricted` | The network refused the host (proxy/firewall) or the API returned 403; nothing is retried or fabricated |
| `discover keywords` shows nothing after `discover demo` | Demo data is synthetic: add `--include-synthetic` |
| `forecast train` says "no eligible series" | Expected without real monitored history: synthetic data is never used for real models. Use `--dataset synthetic` for an isolated experiment |
| Every forecast is `BASELINE_ONLY` | No validated champion for that scope and data origin: train and `--promote` (or `forecast promote`) explicitly |
| `FORECAST_UNAVAILABLE` "artifact failed verification" | A model file changed after registration; it is refused. Retrain or restore the artifact directory |
| Integration tests all skipped | Test DB unreachable; `docker compose up -d` (the init script creates `trend_agent_test` on first start — if your volume predates it, run `docker compose exec postgres createdb -U trend_agent trend_agent_test`) |

## Documentation

* [docs/architecture.md](docs/architecture.md) — modules, data flow, interfaces, schema, errors
* [docs/data_sources.md](docs/data_sources.md) — what each provider can and cannot measure
* [docs/data_contracts.md](docs/data_contracts.md) — canonical models and comparison rules
* [docs/compliance.md](docs/compliance.md) — YouTube API policies, retention, permissions
* [docs/phase1_report.md](docs/phase1_report.md) — what was built and verified; Phase 2 readiness
* [docs/phase2_architecture.md](docs/phase2_architecture.md) — discovery modules, pipeline, schema, Phase 1 changes
* [docs/keyword_discovery.md](docs/keyword_discovery.md) — sources, entities, labels, clustering, scoring, lifecycle
* [docs/discovery_configuration.md](docs/discovery_configuration.md) — every configuration key and how to add sources
* [docs/phase2_report.md](docs/phase2_report.md) — what was built, demonstrated and verified; limitations; Phase 3 readiness
* [docs/phase3_architecture.md](docs/phase3_architecture.md) — monitoring modules, processes, schema, Phase 1/2 changes
* [docs/monitoring_scheduler.md](docs/monitoring_scheduler.md) — activation, tiers, cadence, budgets, quota, job queue and recovery
* [docs/time_series_data.md](docs/time_series_data.md) — observation schema, time semantics, revisions, comparability, freshness, readiness
* [docs/historical_replay.md](docs/historical_replay.md) — replay without look-ahead; interface for Phase 4
* [docs/monitoring_operations.md](docs/monitoring_operations.md) — commands, processes, troubleshooting, retention, benchmarks
* [docs/phase3_report.md](docs/phase3_report.md) — what was built and verified, benchmarks, limitations, Phase 4 readiness
* [docs/phase4_architecture.md](docs/phase4_architecture.md) — detection modules, Phase 3 integration, schema, design decisions
* [docs/statistical_detection.md](docs/statistical_detection.md) — baselines, anomaly, CUSUM, change points, cold start, multiple testing, failure modes
* [docs/surge_alerts.md](docs/surge_alerts.md) — evidence classes, stages, severity, episodes, output contract, explanations
* [docs/detection_evaluation.md](docs/detection_evaluation.md) — ground truth, metrics, causal replay, calibration, leakage control
* [docs/phase4_report.md](docs/phase4_report.md) — what was built and verified, synthetic accuracy, benchmarks, limitations, Phase 5 readiness
* [docs/verification_layer.md](docs/verification_layer.md) — evidence registry, claim verification, statistical authority, entities, injection protection, abstention, gate, failure modes
* [docs/verification_evaluation.md](docs/verification_evaluation.md) — labelled adversarial evaluation (fictional), metrics, held-out results
* [docs/verification_report.md](docs/verification_report.md) — verification-layer report: changes, schemas, tests, risks, Phase 5 readiness
* [docs/phase5_architecture.md](docs/phase5_architecture.md) — content-analysis modules, data flow, trust boundaries, schema, queue, retention, capability modes
* [docs/search_intent_analysis.md](docs/search_intent_analysis.md) — intent taxonomy, entities, classification and decomposition methods
* [docs/content_gap_detection.md](docs/content_gap_detection.md) — retrieval, evidence tiers, relevance, freshness, coverage, supply, gap rules
* [docs/evidence_verification.md](docs/evidence_verification.md) — report claims, source checking, prompt-injection defences, controlled LLM
* [docs/opportunity_scoring.md](docs/opportunity_scoring.md) — score components, weights, caps, statuses, limitations
* [docs/phase5_evaluation.md](docs/phase5_evaluation.md) — labelled datasets (fictional), metrics, held-out results, benchmarks
* [docs/phase5_report.md](docs/phase5_report.md) — Phase 5 report: implementation, validation, limitations, Phase 6 readiness
* [docs/phase6_architecture.md](docs/phase6_architecture.md) — forecasting modules, data flow, schema, measurement separation, Phase 7 contract
* [docs/forecast_features.md](docs/forecast_features.md) — feature definitions, units, availability rules, leakage safeguards
* [docs/forecast_targets.md](docs/forecast_targets.md) — breakout, continuation, trajectory, peak and censoring semantics
* [docs/model_training.md](docs/model_training.md) — datasets, splits, candidates, acceptance, registry, promotion, drift
* [docs/forecast_accuracy.md](docs/forecast_accuracy.md) — metrics, chronological testing, observed (synthetic) results
* [docs/forecast_uncertainty.md](docs/forecast_uncertainty.md) — calibration, prediction intervals, OOD, abstention
* [docs/opportunity_windows.md](docs/opportunity_windows.md) — production assumptions and how to read a window
* [docs/phase6_report.md](docs/phase6_report.md) — Phase 6 report: implementation, validation, limitations, Phase 7 readiness
