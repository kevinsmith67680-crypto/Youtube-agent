# Phase 5 architecture — AI search intent & content-gap analysis

Phase 5 asks, for a query that Phase 4 flagged (or that an operator names): *what do
searchers appear to need, what do the inspected YouTube results appear to provide, and is
there a verifiable difference worth investigating?* It produces **opportunity reports**,
which are research priorities. It does not forecast, produce or publish videos, and it
does not generate titles, thumbnails or scripts.

```
Phase 4 surge event ──┐                          ┌── evidence registry (verification layer)
manual query ─────────┤                          │      signed IDs, bundles, integrity
                      ▼                          ▼
             ContentAnalysisService ── context assembly (read-only) ──> AnalysisInputs
                      │                                                     │
                      │            ContentAnalyzer (deterministic core)     ▼
                      │   policy ▸ entities ▸ intent ▸ requirements ▸ bounded retrieval
                      │   ▸ evidence registration ▸ relevance ▸ freshness ▸ coverage
                      │   ▸ supply ▸ gaps ▸ [optional LLM assist] ▸ claims ▸ verification
                      │   ▸ score ▸ report
                      ▼
      ContentAnalysisRepository (idempotent writes, retention) ── PostgreSQL (migration 0006)
```

## Modules (`src/trend_agent/content_analysis/`)

| module | responsibility |
|---|---|
| `config.py` | typed configuration (`config/content_analysis.yaml`); versioned (`version_tag` = name + file hash) |
| `demand.py` | Phase 4 → Phase 5 demand evidence: copies alert fields, labels them with a `DemandBasis`; nothing is recalculated or upgraded |
| `entity_verification.py` | query entities against the catalog and evidence views; versions, releases, breaking changes, season ends, removed/vaulted items |
| `intent_classifier.py` | hierarchical rule-based intent (10 primaries + AI/technology/gaming sub-intents), policy flags |
| `query_decomposition.py` | candidate information requirements per (sub-)intent; attribute focus; LLM suggestions validated as generic |
| `result_retrieval.py` | bounded retrieval: canonical query, then one variant only if thin; YouTube via the Phase 1 ingestion service (quota, cache, retention) or a fixture source |
| `result_normalization.py` | sanitising, injection flags, format detection, de-duplication |
| `evidence.py` | evidence drafts for videos (metadata) and deterministic observations (calculations) |
| `relevance.py` | lexical relevance index: entity/version match, intent cues, title, description, keywords; optional semantic scorer hook |
| `freshness.py` | CURRENT / POTENTIALLY_OUTDATED / OUTDATED_WITH_EVIDENCE / UNKNOWN |
| `coverage.py` | per requirement × video: COVERED / PARTIALLY_COVERED / NOT_EVIDENCED / CONTRADICTED / UNKNOWN |
| `supply.py` | scope-limited supply summary (OBSERVED / ESTIMATED / UNKNOWN); view counts disabled |
| `content_gaps.py` | ordered gap rules with abstention first |
| `opportunity_scoring.py` | 0–100 ranking index from six components with versioned weights and demand caps |
| `structured_outputs.py`, `llm_client.py` | controlled LLM assist: structured output, local re-validation, bounded attempts, reduced-capability mode |
| `verification.py` | application-side claim verification (statuses, publication) |
| `orchestrator.py` | `ContentAnalyzer`: one analysis end to end |
| `scheduling.py` | cost-aware queue: priorities, category allocation, budgets, cache invalidation |
| `service.py` | DB context, `SqlRegistrar`, persistence, queue execution, re-verification, status |
| `synthetic.py`, `evaluation.py` | scenarios A–J and the labelled evaluation |

CLI: `src/trend_agent/cli_analyze.py` (`trend-agent analyze …`). Container:
`AppContainer.content_config()` / `content_analysis()`.

## Trust boundaries

* **Application code** assembles context (read-only transactions, `permissions.read_only`),
  registers evidence and issues bundles (`SqlRegistrar`, `Principal.APPLICATION`), writes
  results and makes every decision (gap, status, score, publication of claims).
* **Source content** (titles, descriptions, news, the catalog's discovery items) is data.
  It is sanitised, flagged for instruction-like text, wrapped in a delimited data block when
  shown to an LLM, and a flagged description is excluded from relevance and coverage.
* **The LLM** (optional) only *suggests*: secondary intents, generic requirements, gap
  hypotheses and a summary. Its output is parsed into a closed schema without numeric
  fields; every statement is re-verified; it cannot write, fetch, cite unknown evidence or
  change the status or score (`tests/unit/content_analysis/test_llm_and_injection.py`).

## Data model (migration `0006`)

| table | key points |
|---|---|
| `content_analysis_runs` | one row per analysis (completed or failed); trigger, demand basis, versions, inputs fingerprint, analysis fingerprint (partial unique index on completed runs → idempotent), bundle FK, cost JSON, `expires_at`; kept as audit record |
| `search_intent_analyses` | one per run; primary/sub/secondary intents, entity FK, confidence label, specificity, method, ambiguity, payload |
| `information_requirements` | unique (run, key); priority, cues, source (`template`/`llm_suggested`), blocking |
| `video_result_analyses` | unique (run, video); API position, relevance score (0–100 check), match, format, freshness, evidence level, evidence ID, `expires_at` |
| `requirement_coverage` | unique (requirement, video); status, evidence level, matched cues, `expires_at` |
| `content_gaps` | unique (run, gap type); strength, verification status, limitations, evidence IDs, `expires_at` |
| `content_opportunities` | unique `opportunity_key` (UUIDv5 of query/region/language) → upsert keeps one current report per query; score check, `candidate_has_gap` check, report JSON, `expires_at` |

Foreign keys use `SET NULL` towards earlier phases and `CASCADE` within Phase 5.

### Idempotency and caching

* `inputs_fingerprint` = analysis version + configuration tag + query/region/language +
  demand (basis, event, statement) + content hashes of the context evidence. Identical
  inputs inside `reanalyse_after` (72 h) return the stored result without any API call.
* the analysis `fingerprint` adds the retrieved video IDs and collection times; saving an
  outcome whose fingerprint already exists writes nothing.
* retrieval reuses stored `video_search_results` within `cache_ttl` (24 h) — a forced
  re-analysis inside that window makes no API call.
* cache invalidation: a new analysis or configuration version, an updated surge event, new
  or changed context evidence, an expired previous analysis, or `--force`.

### Retention

YouTube metadata may be kept for at most 30 days (Phase 1 policy). Every Phase 5 row that
contains or is derived from third-party text (`video_result_analyses`,
`requirement_coverage`, `content_gaps`, `content_opportunities`) expires with the
*earliest* video metadata expiry it used and is deleted by
`trend-agent data retention --purge` (`RetentionService.purge_expired`). Run rows keep counts, versions and fingerprints only.
`trend-agent analyze verify` re-checks a stored report against the registry and reports
claims whose evidence has expired or been removed.

## Cost-aware queue

`trend-agent analyze pending|run` takes candidates only from open Phase 4 events (no
autonomous browsing), one per monitoring target:

* priority = demand-basis priority + urgency boost (new AI model, time-limited game event,
  technology announcement) + severity + recency;
* category allocation (AI 45 %, technology 25 %, gaming 30 %) orders selection; unused share
  flows to other categories;
* `daily_search_requests` (40) bounds search calls; `daily_llm_tokens` (200k) bounds the
  LLM — when short, the analysis runs deterministically instead of being skipped;
* `max_per_run` (10), `max_concurrency` (2, `asyncio.Semaphore`), `max_attempts` (2;
  transient errors only — quota exhaustion and access errors are not retried);
* Phase 4 `INSUFFICIENT_EVIDENCE` events and unchanged items are skipped with a reason.

## Capability modes and restrictions

| capability | default | why |
|---|---|---|
| YouTube retrieval | on with `TREND_AGENT_YOUTUBE_API_KEY`; otherwise **cache-only** (stored results; analyses abstain when none) | no key → no live calls, reported in `analyze status` and report limitations |
| captions / transcripts (evidence tier B) | **disabled** | the official API does not provide third-party captions without authorisation; tier B is used only for transcripts explicitly marked authorised |
| view counts / statistics as demand or supply | **disabled** | views are not demand; statistics persistence is off by default (Phase 1 compliance) |
| `totalResults` as supply | **disabled** | an API approximation, not a count of competing videos |
| LLM assist | **off** (`llm.enabled: false`) | reduced-capability deterministic mode; enabling requires the key and the `anthropic` package |
| semantic relevance | hook only (`SemanticScorer`), no model shipped | not evaluated; lexical relevance is used |
