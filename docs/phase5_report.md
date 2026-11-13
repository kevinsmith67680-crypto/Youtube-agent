# Phase 5 report — AI search intent & content-gap analysis

Version 0.5.0. Phase 6 (forecasting) was not started.

## A. Implementation summary

For a query behind a Phase 4 surge event, or one an operator names, Phase 5:

1. copies the Phase 4 demand evidence and labels it (`DemandBasis`) without recomputing
   or upgrading it;
2. resolves entities and versions against the catalog and credible evidence, classifies
   the intent (10 primaries + AI/technology/gaming sub-intents) and lists candidate
   information requirements;
3. retrieves YouTube results through the Phase 1 ingestion service (≤ 2 searches, cached,
   quota- and retention-aware) and registers each result as evidence;
4. assesses relevance, freshness and requirement coverage, summarises supply within its
   scope, and applies ordered gap rules (abstention first);
5. optionally asks an LLM for suggestions (off by default), then verifies every report
   claim in application code;
6. scores the opportunity (0–100 ranking index, capped by demand basis) and stores the
   report idempotently, with retention tied to the YouTube data it used.

Conservatism is the design choice: unknown, rumoured, ambiguous or removed subjects,
too few results and metadata-only absences produce abstentions or weak hypotheses, not
opportunities.

## B. Repository and architectural changes

New:

* `src/trend_agent/content_analysis/` — 22 modules (≈ 7,400 lines): config, demand,
  entity verification, intent classifier, query decomposition, retrieval, normalisation,
  evidence, relevance, freshness, coverage, supply, content gaps, scoring, structured
  outputs, LLM client, verification, orchestrator, scheduling, service, synthetic,
  evaluation;
* schemas `schemas/search_intent.py`, `search_result.py`, `content_gap.py`,
  `content_opportunity.py`; DB models `db/content_models.py`; repository
  `db/repositories/content_analyses.py`; CLI `cli_analyze.py`;
* configuration `config/content_analysis.yaml`; fixtures `config/content_analysis/`
  (`world.yaml`, `scenarios.yaml`, `eval_dev.yaml`, `eval_calibration.yaml`,
  `eval_heldout.yaml`);
* tests `tests/unit/content_analysis/` (8 test modules + helpers) and
  `tests/integration/test_content_analysis.py`; `scripts/benchmark_content_analysis.py`;
  seven documents and three result files in `docs/benchmarks/`.

Changed (earlier phases):

* `container.py` — `content_config()`, `content_analysis()`; `ingestion(clock=…)` so
  ingestion timestamps follow the caller's clock;
* `ingestion/retention.py` — counts and purges expiring Phase 5 rows;
* `db/models.py` (model registration), `cli.py` (`analyze` group);
* `verification/untrusted.py` — the `credential_access` injection signal now requires an
  action verb ("send/print/reveal … API key"); official text such as "create an API key
  to start" was flagged before. The verification-layer tests and evaluation still pass;
* tests: `conftest.py` table list, `test_database.py` (revision 0006).

Architecture: `docs/phase5_architecture.md`.

## C. Database migrations

`migrations/versions/20261113_0006_content_analysis.py` (revision `0006`, down `0005`)
adds `content_analysis_runs`, `search_intent_analyses`, `information_requirements`,
`video_result_analyses`, `requirement_coverage`, `content_gaps`,
`content_opportunities` with foreign keys (`SET NULL` to earlier phases, `CASCADE` within
Phase 5), indexes, uniqueness constraints (per run/key/video/gap type; partial unique
fingerprint for completed runs; unique opportunity key) and checks (score 0–100,
candidate requires a gap). Verified: upgrade → downgrade to 0005 → upgrade on the
development database, `alembic check` reports no drift, and the test suite migrates from
base to head.

## D. Intent classification implementation

Deterministic weighted rules with precedence, category-specific sub-intents, entity and
version resolution, ambiguity detection, policy blocking, attribute focus for
informational/review queries, and LLM suggestions limited to secondary intents (or a
choice among rule-plausible primaries when the rules are LOW confidence). Confidence is a
label (HIGH/MEDIUM/LOW), never a probability. Details: `docs/search_intent_analysis.md`.

## E. Search-result and content-gap analysis methodology

Bounded retrieval (canonical query; one variant only when thin), evidence tiers (metadata
only by default; captions disabled), lexical relevance index with entity/version matching
(threshold 60 from the calibration sweep), conservative freshness (age alone never
"outdated"; "outdated with evidence" needs a verified breaking change), per-requirement
coverage, scope-limited supply (view counts and `totalResults` disabled) and ten gap types
in a fixed order with abstention first. Details: `docs/content_gap_detection.md`;
scoring: `docs/opportunity_scoring.md`.

## F. Evidence verification and hallucination safeguards

Every report sentence is a claim citing registry evidence; the application resolves IDs
against the analysis bundle (fabricated or substituted IDs fail), reproduces numbers from
cited calculations, rejects "watched" claims and metadata overreach, runs the claim
verifier, and publishes only verified/corroborated claims. Instruction-like source text is
flagged, excluded from relevance and coverage, and shown to an LLM only inside a delimited
data block; the LLM's closed output schema has no numeric fields, and it has no tools.
Status, score and gap are decided before and independently of the LLM. Details:
`docs/evidence_verification.md`.

## G. Synthetic AI, technology and gaming demonstrations

`trend-agent analyze demo` runs scenarios A–J plus C2 and E2 on fictional entities
(CodeForge, Helios Agent, Orbit — AI; Pyxis, Quanta Notes, Nova Lens — technology;
Skyforge Arena — gaming). All 12 meet their checks; outcomes are tabulated in
`docs/phase5_evaluation.md`. The DB integration test runs the full path with a synthetic
Phase 4 surge and a fake YouTube transport.

## H. Accuracy evaluation results

32 labelled fictional cases in chronological splits (`docs/phase5_evaluation.md`). The
official held-out run (12 cases): intent 0.75, entity 0.92, nDCG@5 0.94, relevant
precision 0.84 / recall 0.96, **gap precision 0.57, false-gap rate 0.50, abstention
recall 0.0**, unsupported published claims 0, future-evidence leaks 0. The three false gaps
(an uncatalogued product version, a vaulted item, an attribute question) were then fixed;
the re-run (gap precision 1.0, false-gap rate 0.0) is **not** a held-out measurement. Labels
are the rules' author's, the data is fictional, and none of this is real-world accuracy.

## I. Tests, linting, typing and integration verification

| check | result |
|---|---|
| full suite, Python 3.12 (with coverage) | pending (run in progress at this commit) |
| full suite, Python 3.13 | pending (run in progress at this commit) |
| Phase 5 unit tests (`tests/unit/content_analysis`) | 72 passed |
| Phase 5 DB integration tests (real PostgreSQL 16, fake YouTube transport) | 7 passed |
| `ruff check .`, `ruff format --check` (src, tests, scripts, migrations) | clean |
| `mypy` (src, strict) and `mypy tests --strict` | clean |
| migration upgrade/downgrade/upgrade and `alembic check` | verified on the dev database |
| synthetic scenarios A–J (+C2, E2) | 12/12 pass |

Integration tests ran with `TREND_AGENT_REQUIRE_DB_TESTS=1` against dedicated test
databases (`trend_agent_test`, `trend_agent_test2`), never the development database.

## J. Working live providers versus mock providers

| component | status in this environment |
|---|---|
| YouTube Data API (`search.list`, `videos.list`) via Phase 1 | implemented; **not exercised live** (no API key) — tested through `httpx.MockTransport` fixtures and the fake transport in the integration test |
| cache-only mode (no key) | exercised in tests |
| Anthropic LLM assist | implemented; **not exercised live** (no key, SDK not installed) — tested with a fake client and scripted outputs |
| captions / transcripts | disabled by design (no authorised access) |
| semantic relevance | interface only; not implemented |
| Phase 4 surge input | real Phase 4 code on synthetic series in the integration test |

Not completed because credentials are unavailable: a live retrieval run, a live LLM
assist run, and any evaluation on real YouTube results.

## K. Known limitations and remaining risks

* **Validation**: 32 fictional cases labelled by the rules' author; the held-out run
  revealed a 50 % false-gap rate before fixes. Real-world precision is unknown and could be
  much lower on real queries and results.
* **Metadata only**: without captions, "missing" means "not in titles/descriptions";
  `MISSING_SPECIFIC_ANSWER` is always a weak hypothesis.
* **Lexical rules**: English-only; paraphrases, typos and new jargon are missed ("vs code"
  read as "versus"); relevance can be gamed by keyword-stuffed titles.
* **Entity coverage**: products absent from the catalog cannot be verified; analyses of
  them abstain (by design), so new products yield no opportunities until Phase 2 catalogs
  them.
* **Prompt injection**: regex signals miss paraphrased instructions; defences are layered
  (no tools, closed schemas, application-side decisions) but not proven complete.
* **Quota**: at 100 units per search, 1,000 keywords take ~25 days at the configured 40
  searches/day; freshness of analyses is bounded by that.
* **Retention**: reports expire with the YouTube metadata they cite (≤ 30 days); consumers
  must not keep copies beyond that.
* **Score weights** are an untested operating point, not fitted to outcomes.

## L. Commands to run Phase 5 locally

```bash
docker compose up -d && trend-agent db upgrade
trend-agent analyze demo                     # offline synthetic scenarios
trend-agent analyze evaluate --split all     # offline labelled evaluation
trend-agent analyze query --query "AI coding agents" --category ai
trend-agent analyze pending && trend-agent analyze run --category ai
trend-agent analyze opportunities --limit 20
trend-agent analyze explain --opportunity-id ID && trend-agent analyze verify --opportunity-id ID
trend-agent analyze status
trend-agent data retention --purge
python scripts/benchmark_content_analysis.py
TREND_AGENT_TEST_DATABASE_URL=postgresql+psycopg://…/trend_agent_test \
  TREND_AGENT_REQUIRE_DB_TESTS=1 pytest
```

Set `TREND_AGENT_YOUTUBE_API_KEY` for live retrieval; set `llm.enabled: true`,
`TREND_AGENT_ANTHROPIC_API_KEY` and install `anthropic` for the optional assist.

## M. Phase 6 readiness assessment

Phase 6 can consume:

* `content_opportunities.report` — the versioned `OpportunityReport`
  (`content-opportunity/1.0`), one current report per query/region/language, with demand
  evidence, gaps, score components, verification state and source references;
* `OpportunityRepository.ranked()` / `ContentAnalysisService.opportunities()` and
  `trend-agent analyze opportunities --json`;
* the run history (`content_analysis_runs`) for when and how each query was analysed.

Conditions for Phase 6: treat scores as ranking indices, not forecasts or probabilities;
respect `expires_at`; use only `published` claims; join to Phase 3/4 time series by
`query_id`/`surge_event_id` for anything temporal; and validate on real data before
relying on gap labels. Phase 5 does not produce forecasts, titles, thumbnails or scripts.

## Acceptance criteria

| # | criterion | status | evidence |
|---|---|---|---|
| 1 | Phase 1–4 functionality preserved | passed | full suites (section I) |
| 2 | Phase 4 surge events trigger content research | passed | `analyse_event`, queue; integration test with a real Phase 4 episode |
| 3 | manual query analysis | passed | `analyze query`; manual-label test |
| 4 | intents classified | passed | unit tests; evaluation |
| 5 | queries decomposed into requirements | passed | unit tests |
| 6 | YouTube results via authorised providers | passed with fakes; **live not run** | integration test through the Phase 1 ingestion service with a fake transport |
| 7 | relevance evaluated | passed | unit tests; nDCG/precision in evaluation |
| 8 | entity and version mismatches recognised | passed | unit tests; scenarios B, C, E |
| 9 | conservative freshness | passed | unit tests (age alone ≠ outdated; breaking change required) |
| 10 | metadata-only limitations enforced | passed | coverage/gap tests; scenario F |
| 11 | coverage and gaps classified correctly in tests | passed (fixtures) | scenarios; evaluation tables |
| 12 | strong content does not produce false gaps | passed on fixtures | scenario D; false-gap rate 0 after fixes (held-out run 1 was 0.5) |
| 13 | LLM outputs use validated schemas | passed | structured-output tests |
| 14 | unsupported numerical claims rejected | passed | injection/LLM tests; scenario H |
| 15 | fabricated references rejected | passed | scenario H; LLM tests |
| 16 | prompt-injection safeguards tested | passed | unit, integration, scenario H, H12 |
| 17 | missing information → abstention | passed | abstention tests; scenarios E, G, J |
| 18 | explainable scores | passed | score components, caps, explanation fields |
| 19 | reports preserve evidence and source references | passed | report schema; reverify test |
| 20 | idempotent DB writes | passed | integration test (cached / identical fingerprint writes nothing) |
| 21 | API and retention policies enforced | passed | quota via Phase 1; retention purge test; statistics/captions disabled |
| 22 | CLI commands function | passed | CLI integration test (DB commands) + offline CLI tests (`demo`, `evaluate`, `intent --offline`) |
| 23 | synthetic end-to-end tests pass | passed | scenarios + DB integration test |
| 24 | existing regression tests pass | passed | full suites |
| 25 | evaluation documented | passed | `docs/phase5_evaluation.md` |
| 26 | Phase 6 can consume reports | passed (interface) | section M; not exercised by a Phase 6 consumer |
