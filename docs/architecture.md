# Architecture

Phase 1 is a **modular monolith**: one installable Python package (`trend_agent`), one
PostgreSQL database, one CLI. No services, queues or orchestrators — the system must run on
a laptop and be easy to reason about. Module boundaries are enforced by convention and by
dependency direction (below), not by deployment.

## Module responsibilities

```
src/trend_agent/
├── config.py            Settings (pydantic-settings, TREND_AGENT_*), secrets as SecretStr
├── logging.py           JSON logging to stderr, recursive secret redaction, JSON-safe payloads
├── container.py         Composition root: builds engine, quota store, providers, services
├── cli.py               Typer CLI (presentation only; no business logic)
├── comparability.py     Can two observations' values be compared? Grouping by shared scale
├── schemas/             Provider-neutral contracts (no I/O)
│   ├── enums.py           MeasurementType, SearchProperty, ScaleType, SampleQuality, ...
│   ├── observation.py     NormalizationContext, ObservationCandidate, ValidatedObservation
│   ├── video.py           VideoSearchResult, VideoDetails, ChannelDetails, ...
│   └── provider.py        ProviderDescriptor, RetentionRule, status reports, requests
├── providers/           Everything that talks to an external source
│   ├── base.py            Provider / SearchInterestProvider / VideoDataProvider protocols
│   ├── errors.py          Typed error taxonomy (status + retryability)
│   ├── registry.py        Name -> provider lookup, capability matrix
│   ├── quota.py           Quota buckets, reset windows, ledger/budgets, QuotaStore protocol
│   ├── http.py            Exponential backoff with full jitter (retryable errors only)
│   ├── youtube.py         YouTube Data API v3
│   ├── google_trends.py   Google Trends adapter + TrendsTransport protocol
│   └── mock.py            Deterministic synthetic provider
├── ingestion/
│   ├── validation.py      Candidate -> ValidatedObservation or diagnosed rejection
│   ├── retention.py       Storage decisions, expiry, purge
│   └── service.py         Orchestration, run tracking, summaries
├── db/
│   ├── base.py, models.py ORM schema (mirrors migrations/)
│   ├── session.py         Engine (UTC-pinned), session factory, transaction()
│   └── repositories/      The only code that issues SQL
└── utils/               ISO codes, query canonicalisation, UTC/time parsing
```

**Dependency direction** (arrows = "may import"):
`cli → container → ingestion → {providers, db, schemas}`; `providers → schemas`;
`db → schemas`; `schemas → utils`. Providers never import ingestion or db. Quota lives in
`providers/` (not `ingestion/` as originally sketched) because it is enforced in the
transport layer; putting it in `ingestion` would invert that dependency. The quota *store*
implementation that uses PostgreSQL lives in `db/repositories/quota.py` and is injected.

## Data flow

```
             ┌──────────────────────── IngestionService ───────────────────────┐
request ──►  │ resolve provider ─► capability? ─► status() ready? (no quota)   │
             │        │ no / not ready ─► FAILED run recorded, nothing fetched │
             │        ▼                                                        │
             │ start run row (own txn) ─► fetch units concurrently ───────────────► provider
             │   (Semaphore, per-unit timeout, typed errors, quota ledger)     │     │ quota.reserve()
             │        ▼                                                        │     │ before EVERY
             │ validate_batch ─► accepted / rejected(+reasons) / in-batch dups │     │ HTTP attempt
             │        ▼                                                        │
             │ storage_decision(provider, record kind) ─► allowed? expires_at  │
             │        ▼                                                        │
             │ ONE transaction: upsert queries, INSERT .. ON CONFLICT DO       │
             │ NOTHING, detect conflicts, quarantine rejections, finish run    │
             │        ▼                                                        │
             │ IngestionSummary (balanced counts, quota used, failures)        │
             └─────────────────────────────────────────────────────────────────┘
```

Every received record ends in exactly one bucket — `stored`, `already_present`,
`duplicate_in_batch`, `rejected` or `withheld` (storage not permitted) — and
`IngestionSummary.is_balanced()` checks that the counts add up. There is no path where a
record silently disappears.

## Provider interfaces

Providers are plain classes that satisfy `typing.Protocol`s (structural typing, no base
class). Every provider exposes:

* `descriptor: ProviderDescriptor` — identifier, version (+ compatible versions),
  provider type, capabilities, measurement types, search properties, regions,
  granularities, authentication requirement, historical availability, data nature
  (actual / sampled / estimated / synthetic / unavailable), enabled flag, per-record-kind
  **retention rules** and usage restrictions.
* `status() -> ProviderStatusReport` — readiness per capability without network I/O or
  quota spend: `AVAILABLE`, `NOT_CONFIGURED`, `ACCESS_RESTRICTED`, `UNSUPPORTED`,
  `RATE_LIMITED`, `QUOTA_EXHAUSTED`, `TEMPORARILY_UNAVAILABLE`, `DISABLED`.
* Capability methods: `SearchInterestProvider.fetch_search_interest(...)` or
  `VideoDataProvider.search_videos / get_videos / get_channels / popular_videos /
  video_categories`.

Providers return provider-neutral models only. `ObservationCandidate` is deliberately
loosely typed so malformed provider output can be *quarantined with a reason* instead of
crashing the pipeline.

The registry is a typed dictionary (`ProviderRegistry`), not a plugin framework:
`registry.search_interest(name)` resolves a provider and raises
`CapabilityNotSupportedError` if it lacks the capability.

## Database design

| Table | Purpose | Key constraints |
|---|---|---|
| `providers` | Provider declarations (synced from descriptors) | unique `name` |
| `search_queries` | Canonical query + first-seen original text | unique `(canonical_query, language, region)` **NULLS NOT DISTINCT** |
| `search_interest_observations` | Measured/derived interest values | identity unique key (below); CHECKs for window order, finite values, value↔quality, synthetic label, YouTube-vs-external property, no `unavailable` |
| `videos`, `channels` | Third-party metadata with `metadata_collected_at` / `metadata_expires_at` | expiry > collection |
| `video_statistics` | Snapshots of public counters (**not written unless enabled**) | unique `(video_id, collected_at, source_provider_id)`, non-negative, cascade on video delete |
| `video_search_results` | Which videos a query returned, when, at what rank (supply snapshot for Phase 5) | snapshot unique key, cascade on video delete, `expires_at` |
| `ingestion_runs` | One row per operation: status, counts, quota, error type/message, summary JSON | non-negative counts, completed ≥ started |
| `rejected_records` | Quarantine with reason code, detail, redacted JSON-safe payload | cascade with run, optional `expires_at` |
| `provider_rate_limits` | Local quota estimate per provider/bucket/reset window | unique `(provider_name, bucket, window_start)` |

Observation identity (idempotency key): `(query_id, provider_id, measurement_type,
search_property, geography, language, granularity, observation_start, observation_end,
normalization_key)`. Re-ingesting the same observation is a no-op (`already_present`);
the same identity with a *different* value is never overwritten — it is quarantined as
`conflicts_with_stored`.

Time columns are all `timestamptz`; connections are pinned to `timezone=UTC`. Event time
(`observation_start/end`), collection time (`collected_at`), provider availability time
(`available_at`) and row creation (`created_at`) are separate columns.

Semantic labels are `VARCHAR` + `CHECK` constraints generated from the enums (not native
PG enums, which need `ALTER TYPE` migrations to extend).

The database re-checks the most important invariants independently of application
validation, so a bug in one layer cannot store a mislabelled record (tested by inserting
invalid rows directly).

## Error handling

`providers/errors.py` defines the taxonomy; each error carries `error_type`, the implied
`provider_status`, and `retryable`:

| Error | Retryable | Typical cause |
|---|---|---|
| `ProviderNotConfiguredError` | no | missing key / configuration (lists missing variables) |
| `ProviderDisabledError`, `ProviderNotFoundError` | no | configuration |
| `CapabilityNotSupportedError` | no | provider cannot do this |
| `InvalidCredentialsError` (⊂ `AccessRestrictedError`) | no | bad / expired key |
| `AccessRestrictedError` | no | API not enabled, key restrictions, alpha access missing |
| `QuotaExhaustedError` (`source=local_estimate|server`) | **no** | daily bucket used up; carries reset time |
| `RequestBudgetExceededError` | no | per-operation budget used up |
| `RateLimitedError` | yes | 429 / `rateLimitExceeded` (honours `Retry-After` up to the cap) |
| `TemporarilyUnavailableError`, `ProviderTimeoutError` | yes | 5xx, network errors, timeouts |
| `MalformedResponseError` | no | response not in the documented shape |
| `InvalidRequestError` | no | bad parameters |

Rules: quota is reserved *before every HTTP attempt* (retries are billable); quota
exhaustion is never retried and immediately stops further requests in the run; error
messages pass through secret redaction; transport exceptions are re-raised `from None` so
request URLs are never chained into tracebacks. Unexpected exceptions inside a provider
become an `internal_error` fetch outcome (run marked `partial`/`failed`), not a crash.
A database error during persistence rolls back the whole run's writes; the run is then
recorded as `failed` with `database_error` in a separate transaction. Cancellation
(Ctrl-C / SIGTERM) marks the run `cancelled`; nothing from it is persisted.

## Concurrency and I/O

HTTP is async (`httpx.AsyncClient`) with bounded concurrency (`asyncio.Semaphore`,
`TaskGroup` for batched lookups). Database access is synchronous (psycopg 3): writes happen
once per run in a short transaction, so an async driver would add complexity without a
measurable benefit. The SQL quota store uses a conditional `UPDATE ... WHERE used + cost
<= limit RETURNING` which is atomic across processes (verified with concurrent threads).

## Future integration points

* **Phase 2 (keyword discovery):** new providers declaring `SEARCH_SUGGESTIONS` /
  `TREND_DISCOVERY`, emitting `SEARCH_SUGGESTION` observations with `RANK`/`PRESENCE`
  scales into the same pipeline; `search_queries` is the keyword table.
* **Phase 3 (scheduling):** a scheduler calls `IngestionService` methods; quota state is
  already shared through `provider_rate_limits`; `youtube refresh` and
  `data retention --purge` become scheduled jobs.
* **Phase 4 (surge detection):** build series only within one
  `comparability.group_by_comparability` group; mock scenarios are the labelled
  evaluation datasets.
* **Phase 5 (content gaps):** `video_search_results` + `videos` give query → supply
  snapshots; subject to the 30-day retention and derived-metric policies.
* **Phases 6–8:** `ingestion_runs`, `collected_at` and `available_at` support
  point-in-time (leakage-free) backtests: only use data whose `available_at` precedes the
  simulated decision time.
