# Phase 1 report — Foundation and Data Acquisition

Date: 2026-11-13. Scope: Phase 1 only (no keyword discovery, scheduling, surge detection,
forecasting, LLM analysis, recommendations or dashboard).

## Initial assessment

The repository was empty (no commits, no files), so the project was initialised from
scratch; there was no existing functionality to preserve. Environment: Python 3.12/3.13,
Docker (daemon started manually), PostgreSQL 16 via Compose. Outbound access to
`www.googleapis.com` was available; `developers.google.com` was **blocked** by the
environment's network policy, so official documentation was checked through search-engine
excerpts only (see "Unresolved issues").

## What was implemented

* **Project foundation:** `pyproject.toml` (hatchling; runtime deps: httpx, pydantic v2,
  pydantic-settings, SQLAlchemy 2, Alembic, psycopg 3, Typer), Ruff, strict Mypy, pytest
  (+asyncio, cov), `.env.example`, `.gitignore`, Docker Compose (PostgreSQL 16, UTC,
  localhost-only, separate test database).
* **Configuration & logging:** typed `Settings` with validation and conservative compliance
  defaults; secrets as `SecretStr`; JSON logs to stderr with recursive redaction of keys,
  tokens, passwords, Google API-key patterns and DB URL passwords.
* **Domain contracts:** explicit enums for measurement type, search property, scale type,
  sample quality, data nature, granularity, capability, provider status, rejection reason;
  `NormalizationContext` with stable hash key; candidate vs validated observation models;
  provider-neutral video/channel models with `None` for hidden statistics.
* **Provider abstraction:** protocols (`Provider`, `SearchInterestProvider`,
  `VideoDataProvider`), `ProviderDescriptor` (all declarations requested in the brief,
  including per-record-kind retention rules), typed error taxonomy distinguishing
  unsupported / not configured / access restricted / rate limited / quota exhausted /
  temporarily unavailable / malformed / invalid request, and a small registry with a
  capability matrix.
* **YouTube Data API v3 provider:** search (query, region, language, date range, order,
  limit, bounded pagination), batched video details (≤ 50 IDs), batched channel details,
  most-popular charts with category filter, video categories; HTML-entity decoding of
  search snippets; ISO-8601 duration parsing; per-item malformed-data handling; key sent
  via `X-Goog-Api-Key` header only.
* **Quota management:** separate `search` (100 calls/day) and `default` (10,000 units/day)
  buckets, per-endpoint costs, configurable limits and safety margin, Pacific-midnight
  reset (DST-aware), per-operation request budgets, reservation before every HTTP attempt,
  server-reported exhaustion marks the bucket exhausted, typed `QuotaExhaustedError` that
  is never retried. In-memory store (tests) and an atomic PostgreSQL store shared across
  processes.
* **Resilience:** exponential backoff with full jitter for retryable errors only, honouring
  `Retry-After` up to a cap; bounded concurrency; per-unit timeouts.
* **Google Trends provider:** transport-agnostic adapter implementing the canonical mapping
  (actual granularity, scale/normalisation context, property → measurement type, 0 →
  `low_volume`, suppressed/missing as NULL); truthful `NOT_CONFIGURED` /
  `ACCESS_RESTRICTED` status; never returns data without a verified transport.
* **Synthetic provider:** 12 deterministic, labelled scenarios for later evaluation.
* **Database:** 10 tables + Alembic migration `0001` (hand-reviewed): idempotency keys with
  `NULLS NOT DISTINCT`, CHECK constraints that independently enforce labelling and value
  invariants, expiry columns, indexes.
* **Validation & quarantine:** 20 rejection reasons, all issues reported per record,
  in-batch duplicate/conflict resolution, conflict-with-stored detection (never overwrite),
  JSON-safe redacted quarantine payloads.
* **Comparability:** `check_comparability` with reasons, `comparability_key`,
  `group_by_comparability`.
* **Ingestion service:** search-interest, video-search, popular-video and refresh
  operations; run tracking; storage policy; single-transaction persistence; balanced
  accounting; graceful cancellation; partial-failure semantics.
* **Retention:** storage decisions per provider and record kind; expiry from collection
  time; refresh of expiring videos with deletion of unavailable ones; purge command.
* **CLI:** `doctor`, `providers list`, `trends status`, `youtube search|popular|categories|refresh`,
  `ingest`, `data inspect|runs|retention`, `quota status`, `db upgrade|current`, `version`.
* **Documentation:** README and the five documents in `docs/`.

## What was tested

| Suite | Tests | Covers |
|---|---:|---|
| `unit/test_youtube_provider.py` | 32 | success + pagination, page bounds, header-only key, missing/invalid credentials (real captured error body), API not enabled, 429/403 rate limits with retry & `Retry-After`, server & local quota exhaustion, request budgets, timeouts, network errors, 5xx retry, permanent 400s, non-JSON / wrong-shape / malformed items, hidden statistics → `None`, 50-ID batching, invalid IDs, channels, charts, descriptor |
| `unit/test_validation_comparability.py` | 61 | valid records, every rejection reason, low/missing/suppressed acceptance, partial periods, YouTube-vs-external labelling, synthetic labelling, version compatibility, batch dedup/conflicts, comparability rules & grouping |
| `unit/test_providers_mock_trends_registry.py` | 28 | mock determinism & scenarios, Trends status logic, no fabrication without access, Trends mapping, registry, status→error mapping |
| `unit/test_quota_retry.py` | 13 | buckets/margins, reset at Pacific midnight, server exhaustion, budgets, jittered backoff, retry rules |
| `unit/test_utils_config_logging.py` | 51 | ISO codes, canonicalisation, durations, DST windows, settings validation, secret redaction |
| `unit/test_cli_basic.py` | 19 | help text, missing configuration, validation messages, DB-down fallback, provider discovery, no secret echo |
| `integration/test_database.py` | 23 | migrations clean apply / downgrade / re-apply / drift check, timestamptz + UTC, unique constraints, idempotent inserts, conflicts, DB-level CHECK enforcement (11 cases), rollback, timestamp round-trip, retention & purge, cascades, concurrent atomic quota store |
| `integration/test_ingestion.py` | 23 | complete run, idempotent re-run, partial failure, all-failed, unsupported capability, not configured, unknown provider, quarantine, conflict-with-stored, storage-policy withholding, Trends with expiry, DB failure rollback, cancellation, timeout, quota exhaustion stops further requests, YouTube quota accounting, statistics withheld/allowed, no needless channel lookups, popular partial failure, refresh + deletion, malformed items, unexpected provider exceptions |
| `integration/test_cli.py` | 5 | mock ingestion + inspection, idempotent CLI re-run, incomparable-group warning, runs/retention/quota/doctor, YouTube commands incl. `--persist` |
| `live/test_youtube_live.py` | 2 | **opt-in only** (real quota); not run |

## Outcomes (all commands actually executed)

| Check | Result |
|---|---|
| `pytest` (Python 3.12) | **255 passed**, 2 deselected (live) |
| `pytest` (Python 3.13, fresh venv) | **255 passed**, 2 deselected |
| `pytest --cov` | **91 %** line+branch coverage of `src/trend_agent` (CLI rendering is the least covered module, ~70 %) |
| `ruff check .` / `ruff format --check .` | clean |
| `mypy` (strict, `src`) / `mypy tests --strict` | no issues |
| `docker compose up -d` | PostgreSQL 16.16 healthy, timezone UTC, test DB created |
| `alembic upgrade head` → `downgrade base` → `upgrade head` → `alembic check` | succeeded, no model/migration drift |
| Wheel build + install into a clean Python 3.13 venv | succeeded; `trend-agent` entry point works |
| CLI smoke test against the real database | `doctor`, `providers list`, `trends status`, `ingest` (incl. idempotent re-run and quarantine), `data inspect`, `data runs`, `data retention`, `quota status`, error paths |
| Live YouTube API | **No authenticated request was made** (no API key available). Two unauthenticated, zero-quota probes confirmed header-based key transport and the real error bodies for invalid / missing keys. |

Defects found and fixed during verification: pagination cap that assumed full pages;
JSONB rejection of `NaN` in quarantine payloads; CLI default window ending at "now" (broke
idempotency across runs); summary not reflecting post-persistence results (refresh
deletions); Rich markup swallowing `[error_type]` text.

## Unsupported features (by data availability, not by omission)

* Real YouTube search volume / YouTube-specific search interest: no public source exists;
  none is simulated outside the labelled mock.
* Google Trends data: requires alpha access; adapter ready, transport not implementable
  without the (non-public) contract.
* YouTube search suggestions: undocumented endpoint; not implemented pending a ToS decision.
* OAuth / authorised channel data, YouTube Analytics/Reporting APIs: not in Phase 1 scope.

## Unresolved issues

1. **Official documentation not re-read in full** (`developers.google.com` blocked here).
   Quota buckets, endpoint costs, reset time zone and the 30-day policy were corroborated
   by search excerpts of Google's pages; exact current wording and section numbers must be
   confirmed by the operator. All quota numbers are configurable.
2. **YouTube provider not exercised against the live API with a valid key.** Response
   parsing is tested against fixtures built to the documented formats plus real error
   bodies. Run `trend-agent doctor --live` and the `live` tests once a key is available.
3. Quota is a local estimate; Google's console is authoritative (documented).
4. A search interrupted by quota exhaustion mid-pagination raises and discards pages
   already fetched in that call (their quota is still accounted). Acceptable for Phase 1;
   revisit if partial results become valuable.
5. Persistence is row-by-row inside one transaction — fine for thousands of rows per run;
   batch `INSERT ... ON CONFLICT` may be needed at larger scale (Phase 3).

## Remaining external dependencies

* YouTube Data API v3 key (and, if needed, a quota extension via Google's audit form).
* Google Trends API alpha access, or a licensed search-interest provider.
* A decision on storing YouTube statistics, made against the current Developer Policies.
* PostgreSQL ≥ 15 for non-Docker deployments.

## Readiness for Phase 2 (Keyword Discovery Engine)

Ready:
* `search_queries` with canonicalisation and uniqueness is the keyword store.
* The provider protocol, registry, descriptor and status model accept new discovery
  providers (`SEARCH_SUGGESTIONS`, `TREND_DISCOVERY`) without changes to ingestion or the
  schema; `SEARCH_SUGGESTION` + `RANK`/`PRESENCE` scales already exist.
* YouTube search, most-popular charts, categories and video metadata are available as
  discovery seeds (within quota), with run tracking and retention.
* Validation, quarantine, idempotency and comparability apply to any new provider.

Must be addressed before or early in Phase 2:
* Choose legitimate keyword sources: YouTube search suggestions have no documented API —
  decide (with a ToS review) whether to use them, or use YouTube metadata (titles, tags,
  categories), the Trends alpha (related queries, if available), or a licensed provider.
* Obtain a YouTube API key and run the live checks; set the real quota limits.
* Budget the 100 `search.list` calls/day: keyword expansion must be quota-planned.
