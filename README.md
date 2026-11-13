# YouTube Trend Agent — Phases 1–3: Data Acquisition, Keyword Discovery & Historical Monitoring

An autonomous intelligence platform (in progress) for understanding **YouTube search
demand**: what people search for, which searches are surging, which are under-served by
existing videos, and what videos to make. This repository contains **Phases 1 and 2 of 8**:
the typed foundation, data-acquisition layer, PostgreSQL schema, validation and compliance
controls (Phase 1), and an autonomous **keyword discovery engine** for AI, technology and
gaming (Phase 2).

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

Not in scope yet (by design): surge detection (Phase 4), forecasting, LLM analysis,
recommendations, dashboard.

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
db/          SQLAlchemy models, session management, repositories (all SQL lives here)
comparability.py   decides whether two observations' values may be compared
container.py composition root (dependency injection), cli*.py Typer CLI
config/      discovery.yaml, seeds/, sources/ (Phase 2); monitoring.yaml (Phase 3)
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
```

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
