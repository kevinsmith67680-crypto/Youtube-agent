# Phase 2 architecture — keyword discovery

Phase 2 adds an autonomous **keyword discovery engine** on top of the Phase 1 foundation.
It finds *candidate* search keywords for AI, technology and gaming and ranks them for
future monitoring. It never measures, estimates or invents search demand; that is the job
of later phases, which will observe real search-interest data for the candidates found here.

## Modules

```
src/trend_agent/
  discovery/
    models.py        enums + pydantic models (sources, items, entities, candidates, results)
    config.py        YAML configuration loading/validation (config/discovery.yaml, seeds/, sources/)
    text.py          normalisation, version keys, URL canonicalisation, shingles, Jaccard
    extraction.py    deterministic, grounded entity extraction (+ optional LLM interface)
    expansion.py     entity -> grounded search hypotheses (templates, comparisons, limits)
    intent.py        rule-based intent classifier + category classifier
    clustering.py    two-level clustering (topic, intent) with traceable decisions
    evidence.py      evidence quality, independent-origin grouping, release-date claims
    scoring.py       novelty and priority-v1 scoring (components stored, recomputable)
    lifecycle.py     candidate state machine and validation rules
    budget.py        category budget allocation, exploration pool, source scheduling
    service.py       DiscoveryService: plan -> fetch -> store -> extract -> expand -> score
    demo.py          end-to-end run on the synthetic fixture
    providers/
      base.py        DiscoveryProvider protocol, HttpFetcher (quota, errors, size cap)
      youtube.py     YouTube Data API (search by date, charts, channel uploads)
      feeds.py       RSS 2.0 / RSS 1.0 (RDF) / Atom, conditional GET, defusedxml
      community.py   GitHub releases, Hacker News (Algolia API), Reddit (OAuth)
      local.py       Google Trends related queries (gated), curated seeds
      synthetic.py   deterministic fixture provider (test/demo data only)
      factory.py     builds and registers discovery providers from settings + config
    fixtures/synthetic_discovery.yaml
  db/discovery_models.py          SQLAlchemy models for the Phase 2 tables
  db/repositories/discovery.py    all discovery SQL
  cli_discover.py                 `trend-agent discover ...`
migrations/versions/20261113_0002_keyword_discovery.py
config/                            discovery.yaml, seeds/*.yaml, sources/*.yaml
```

## Pipeline

```
configured sources ──► budget planner ──► fetch (concurrent, quota-checked per fetch)
   (static YAML +        (category shares,     │  provider errors are recorded per source,
    seed-derived +        exploration pool,    │  never fatal for the run
    exploration)          intervals, quota)    ▼
                                         discovery_items  (deduplicated by provider+reference,
                                               │           quality flags, retention expiry)
                                               ▼
                         entity extraction (gazetteer + known entities + patterns; grounded)
                                               │  resolve against stored entities / aliases
                                               ▼
                   entity aggregation (independent origins, verification, release dates)
                                               │
              ┌────────────────────────┬───────┴────────────┬─────────────────────┐
     observed queries          title phrases         seed terms        entity expansion
     (suggestions/related)     (query-like video      (track_as_keyword)  (templates +
                                titles)                                    comparisons)
              └────────────────────────┴───────┬────────────┴─────────────────────┘
                                               ▼
           category + intent classification ─► validation ─► topic/intent clustering
                                               ▼
                    evidence links ─► novelty + priority-v1 ─► lifecycle transition (logged)
                                               ▼
                                 dormancy sweep (no new evidence for N days)
```

Each category is processed in **its own transaction** with its own `discovery_runs` row, so a
failure in one category does not lose another's results. Fetching happens before any
database work; a provider error marks only that source as failed.

## Interfaces

* **`DiscoveryProvider`** (extends the Phase 1 `Provider` protocol): `supports(source)`,
  `can_afford(source)` (local quota check, no I/O), `discover(request, ctx) -> DiscoveryResult`.
  Every provider has a `ProviderDescriptor` with a retention rule for `DISCOVERY_ITEM` records.
  Providers are registered in the same `ProviderRegistry` as the Phase 1 providers.
* **`EntityExtractor`**: `extract(item) -> list[ExtractedEntity]`. The deterministic
  extractor needs no network or API key. An LLM extractor can be added through
  `CompositeExtractor(extra=[...])`; every extra proposal is grounding-checked (the name must
  appear in the item text) and otherwise stored as `REJECTED` / `LLM_PROPOSED`.
* **`SimilarityBackend`** (`clustering.py`): `similarity(tokens_a, tokens_b, text_a, text_b)`.
  The bundled backend is lexical Jaccard (`lexical-jaccard-v1`); an embedding backend can be
  injected into `DiscoveryService(similarity=...)` without schema changes.
* **`IntentClassifier`**: rule-based by default; replaceable via `DiscoveryService(intent_classifier=...)`.

## Data model (migration `0002`, additive)

| Table | Purpose |
|---|---|
| `discovery_sources` | configured sources + health (last check/success, failures, EWMA yield, ETag/Last-Modified) |
| `discovery_runs`, `discovery_run_sources` | one run row per category per invocation; per-source outcome, quota used, errors |
| `discovery_items` | fetched source material (title, short excerpt, URL, publisher, timestamps, quality flags, expiry). Unique on (provider, source reference). `is_synthetic` is enforced by a CHECK to equal `evidence_type = 'synthetic_test_data'` |
| `discovered_entities`, `entity_aliases` | entities with type, category, family parent, version, release level, status, origin, release date (+ basis), attributes |
| `entity_mentions` | which item mentions which entity, with grounded attributes and related names |
| `topic_clusters`, `intent_clusters` | two-level clusters |
| `discovered_keywords` | one row per candidate (FK to Phase 1 `search_queries`): origin, method, template, category/intent scores, lifecycle status, novelty + priority components and version, cluster assignment decisions, grounding |
| `discovery_evidence` | keyword ↔ item links with relation (`query_observed`, `entity_mention`, `template_grounding`) and evidence quality |
| `keyword_status_history` | every lifecycle transition with reason, actor and run |

The migration only adds tables, enum types and one widened CHECK constraint
(`rejected_records.record_kind` accepts `discovery_item`). It was verified to upgrade a
database containing Phase 1 data without loss, to downgrade cleanly, and to show no
model/migration drift (`alembic check`).

## Phase 1 code changed for integration

Only what integration required (all Phase 1 tests still pass):

| File | Change |
|---|---|
| `providers/youtube.py` | `channel_uploads()` (channels.list + playlistItems.list, 2 default units) for monitored channels; `playlistItems.list` cost entry |
| `schemas/enums.py` | `RecordKind.DISCOVERY_ITEM` |
| `config.py`, `.env.example` | `discovery_config_dir`, `http_user_agent`, `github_token`, `reddit_client_id/secret` (secrets as `SecretStr`, redacted in `describe()`) |
| `container.py` | separate HTTP client for discovery, `discovery()` / `discovery_config()` factories |
| `ingestion/retention.py` | retention report/purge include `discovery_items` (evidence links and mentions cascade) |
| `cli.py` | registers the `discover` sub-app |
| `db/models.py` | imports the discovery models so Alembic sees one metadata |
| `pyproject.toml`, `__init__.py` | version 0.2.0; `pyyaml`, `defusedxml` dependencies |

## Error handling

Provider failures use the Phase 1 typed errors. Notable mappings in `HttpFetcher`:
egress-proxy refusals → `AccessRestrictedError` (not retried); timeouts → `ProviderTimeoutError`;
transport errors and 5xx → `TemporarilyUnavailableError` (retried with jittered backoff);
401 → `InvalidCredentialsError`; 429 or 403 with `x-ratelimit-remaining: 0` →
`RateLimitedError` (a `Retry-After` longer than the retry policy allows is not slept on);
other 403 → `AccessRestrictedError`; responses over 5 MB → `MalformedResponseError`.
Local daily quotas are reserved before each attempt, so a refused request is still counted.
A run whose sources partly failed ends `partial`; consecutive failures back a source off
exponentially (capped by `max_failure_backoff_multiplier`).
