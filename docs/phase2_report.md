# Phase 2 report — Autonomous Keyword Discovery Engine

Date: 2026-11-13. Branch `claude/tender-thompson-620dmi`. Version 0.2.0.

## A. Implementation summary

Phase 2 adds a discovery engine that continuously finds **candidate** YouTube search keywords
for AI (45 % of effort), technology (25 %) and gaming (30 %), explains where each came from,
and ranks them for monitoring in Phase 3. It does not measure demand: no search volume,
trend score or demand estimate is produced or stored, and the stored priority score carries
an explicit note saying so.

* **Multi-source discovery** behind one `DiscoveryProvider` interface: YouTube Data API
  (search by date, most-popular charts, channel uploads), RSS/Atom feeds (official
  announcements, PyPI/Steam/vendor feeds), GitHub releases, Hacker News (Algolia API),
  Reddit (OAuth Data API), Google Trends related queries (only with alpha access + a
  transport), curated seeds, and a synthetic fixture provider for tests/demo.
* **Grounded entity extraction** (no LLM required): gazetteer + known entities + version /
  release / announcement / game-structure patterns, family parents, sentence-scoped
  attributes, speculation handling, optional LLM extractor whose proposals are rejected
  unless the name occurs in the source text.
* **Keyword candidates** with explicit origin labels (`observed_query`,
  `generated_hypothesis`; `verified_interest` reserved), grounded intent templates
  (49 templates over 9 entity families), grounded comparisons, query-like title phrases,
  seed terms, and explosion limits.
* **Intent** (13 intents, multiple uncalibrated scores) and **category** classification.
* **Two-level clustering** (entity/topic → intent facet) with stored decision records and
  a pluggable similarity backend; versions, seasons and different products never merge.
* **Novelty** (timestamp-based, idempotent) and **priority-v1** (6 stored components;
  corroboration counts independent origins, so syndicated copies do not count).
* **Lifecycle** state machine with validated, logged transitions; `monitoring` reserved for
  the Phase 3 actor; dormancy sweep.
* **Budget allocation and scheduling**: category shares, 20 % exploration pool, poll
  intervals with failure backoff, readiness and quota checks, recorded skip reasons.
* **Storage**: migration `0002` (12 additive tables), retention-aware item storage.
* **CLI**: `trend-agent discover run|demo|expand|keywords|show|entities|sources|clusters|stats`.

## B. Repository changes

New:

| Path | Content |
|---|---|
| `src/trend_agent/discovery/` (12 modules) | models, config, text, extraction, expansion, intent, clustering, evidence, scoring, lifecycle, budget, service, demo |
| `src/trend_agent/discovery/providers/` (7 modules) | base, youtube, feeds, community (GitHub/HN/Reddit), local (Trends/seeds), synthetic, factory |
| `src/trend_agent/discovery/fixtures/synthetic_discovery.yaml` | two-stage synthetic scenario |
| `src/trend_agent/cli_discover.py` | `discover` CLI |
| `src/trend_agent/db/discovery_models.py`, `db/repositories/discovery.py` | ORM models, repository |
| `migrations/versions/20261113_0002_keyword_discovery.py` | additive migration |
| `config/discovery.yaml`, `config/seeds/*.yaml`, `config/sources/*.yaml` | configuration |
| `tests/unit/discovery/` (5 modules + helpers), `tests/integration/test_discovery.py`, `tests/live/test_discovery_feeds_live.py` | tests |
| `docs/phase2_architecture.md`, `docs/keyword_discovery.md`, `docs/discovery_configuration.md`, this report | docs |

Phase 1 files changed for integration (details in
[phase2_architecture.md](phase2_architecture.md#phase-1-code-changed-for-integration)):
`providers/youtube.py` (channel uploads), `schemas/enums.py`, `config.py`, `.env.example`,
`container.py`, `ingestion/retention.py`, `cli.py`, `db/models.py`, `pyproject.toml`,
`__init__.py`, and tests that asserted the old version/migration head
(`tests/unit/test_cli_basic.py`, `tests/integration/test_database.py`) plus Phase 2 tables
in the test truncation list (`tests/conftest.py`). New dependencies: `pyyaml`, `defusedxml`.

## C. Working discovery sources

Status as observed **in this build environment** (an egress-restricted cloud container
without API credentials). "Implemented" means code + offline tests with mocked HTTP.

| Source | Implemented | Live in this environment | Notes |
|---|---|---|---|
| RSS/Atom feeds — PyPI release feeds (20) | yes | **yes** — fetched live, conditional GET (`not_modified`) confirmed on repeat runs | `code_release`; live opt-in test passed |
| RSS/Atom — vendor blogs, Steam news (6) | yes | no — egress policy denied the hosts → `access_restricted` | URLs `verified: false` |
| YouTube Data API (queries, charts, channels) | yes | not exercised — no API key → `not_configured` (no request made) | offline tests with mocked API; channel sources disabled until handles confirmed |
| GitHub releases (10 repos) | yes | no — `api.github.com` refused for this session → `access_restricted` | optional token supported |
| Hacker News (Algolia) | yes | no — host blocked | |
| Reddit (OAuth) | yes | no — no credentials → `not_configured` | token flow tested with mocks only |
| Google Trends related queries | interface only | no — official API is limited-access alpha; no transport shipped → `not_configured` | never scraped |
| Curated seeds | yes | yes (local) | |
| Synthetic fixture | yes | yes (local) | labelled synthetic end to end |
| Unlicensed scraping | **no, by design** | — | a configured source with no provider is skipped as `unsupported` |

Consequence: in this environment real discovery only had PyPI release feeds, so **real
gaming discovery produced nothing** (all reachable gaming sources were blocked and YouTube
was not configured). Gaming depends on a YouTube API key and reachable official news feeds.

## D. Demonstration results

### D.1 Synthetic end-to-end demo (`trend-agent discover demo`)

Fictional entities only (Corvid Labs "Nimbus Coder", "Lattice" framework, "Skyforge Arena"
game). Two stages: *history* (90 days earlier) and *current*. Fresh database:

| Stage | Status | Notes |
|---|---|---|
| history | succeeded | 3 items, 7 entities, 16 candidates |
| current | **partial** | gaming: synthetic outage (`temporarily_unavailable`) and quota exhaustion (`quota_exhausted`) sources failed; unlicensed scraper source skipped as `unsupported`; 1 duplicate delivery; 1 outdated item ignored; 10 keywords moved to dormant (previous event, superseded Lattice 4.0) |

Totals: 62 candidates (3 observed queries, 59 generated hypotheses), 18 entities, 21 items.
A second run of the same stages created no new items/keywords/evidence and left every
status, priority, novelty kind and cluster unchanged (tested).

Top candidates per category (priority ranks monitoring, not demand):

| Category | Priority | Candidate | Intent | Origin | Novelty | Independent origins | Evidence (simulated types) |
|---|---|---|---|---|---|---|---|
| AI | 84.5 | how to run Nimbus Coder 2 locally | how_to | generated | new_version | 4 | announcement ×3, suggestion ×1, video ×2 |
| AI | 82.4 | how to install Nimbus Coder 2 | how_to | generated | new_version | 4 | same |
| AI | 82.2 | Nimbus Coder 2 benchmark | comparison | generated | new_version | 4 | same |
| AI | 81.9 | nimbus coder 2 install | tutorial | **observed** | new_version | 4 | + suggestion ×2 |
| Tech | 84.8 | how to upgrade to Lattice 5.0 | how_to | generated | new_version | 4 | release ×1, community ×1, video ×2 |
| Tech | 84.3 | Lattice 5.0 migration guide | tutorial | generated | new_version | 4 | same |
| Tech | 83.8 | how to fix Lattice 5.0 errors | troubleshooting | generated | new_version | 4 | same |
| Tech | 82.8 | Lattice 5.0 vs Lattice 4.0 | comparison | generated | new_version | 4 | same |
| Gaming | 82.9 | Frostfall Festival challenges | challenge_guide | generated | new_event | 3 | announcement ×1, video ×2 |
| Gaming | 81.9 | Frostfall Festival rewards | game_guide | generated | new_event | 3 | same |
| Gaming | 80.8 | Glacier Lance location | item_location | generated | new_event | 2 | announcement ×1, video ×1 |

What the fixture's data-quality cases produced:

* Nimbus Coder 2 has 6 mentioning items but **4 independent origins**: the TechWire and
  DailyByte reposts of the announcement were grouped with it. Their conflicting date
  (4 March) lost to the official 28 February date; the conflict is recorded.
* "Nimbus Coder 3" (rumour) is stored as `needs_verification` with no release date and no
  keywords. The rumoured "Void Crown" item never became an entity.
* "Nimbus 2" (a thermostat, technology) stayed separate from "Nimbus Coder 2" (AI model).
* Lattice 5.0.1 (patch) was recorded but not expanded; no keyword mentions "Lattice 2.0"
  (outdated item).
* Install variants share one intent cluster (`instructional:install`: "how to install
  Nimbus Coder 2", "nimbus coder 2 install", "How to Install Nimbus Coder 2 Locally"); benchmark,
  pricing and free stay separate.

Provenance of one candidate (`trend-agent discover show --query "glacier lance location"`):

```
origin generated_hypothesis, method entity_template, template item.location
grounding: entity:game_item:Glacier Lance       entity parent: Frostfall Festival -> Skyforge Arena
novelty new_event 92.88 {entity 100, query 100, intent 60, recency 94.39}
priority-v1 80.82 {novelty 92.88, freshness 96.35, corroboration 51.0 (2 origins),
                   relevance 80.0, intent_actionability 95.0, evidence_quality 66.3}
evidence: entity_mention synthetic://synthetic:gaming:skyforge-news/frostfall (q 86.4)
          entity_mention synthetic://synthetic:gaming:creator-videos/glacier-lance-location (q 46.2)
history:  None -> discovered (entity_template via generated_hypothesis)
          discovered -> validated (grounded in a verified entity)
          validated -> prioritized (priority 80.82 >= 45.0)
```

### D.2 Real run (`trend-agent discover run --all --max-fetches 24`, 2026-11-13 14:56 UTC)

Slots: AI 11, technology 6, gaming 7 (24 total, 4 exploration). 23 fetches: 17 PyPI feeds
succeeded, 5 feeds and 1 GitHub repository failed `access_restricted` (egress policy). 64
sources were skipped as `not_configured` (50 YouTube seed searches, 3 YouTube charts,
8 subreddits, 3 Trends sources), 50 for budget, 3 disabled. Local request quota charged: 23.

Items: AI 40, technology 11, gaming 0. Technology releases in the 14-day window were all
patch releases (Django 6.1.3, FastAPI 0.162.x, pandas 3.1.1, Pydantic 2.14.3), which are
recorded as evidence and not expanded, so technology produced **0 candidates**. AI produced
5, one per library because older minor releases are superseded:

| Priority | Candidate | Evidence |
|---|---|---|
| 71.9 | LangChain 1.5 new features | code_release https://pypi.org/project/langchain/1.5.0/ (2026-11-11) |
| 71.9 | Diffusers 0.43 new features | code_release (PyPI) |
| 70.6 | Sentence Transformers 6.3 new features | code_release (PyPI) |
| 70.2 | OpenAI Python SDK 3.50 new features | code_release (PyPI) |
| 65.9 | Anthropic Python SDK 2.8 new features | code_release (PyPI) |

These are weak, single-origin candidates (corroboration 30). They show that the pipeline
works on real data; they are not a useful keyword list. Useful real output needs the
YouTube and community sources.

## E. Verification

All commands below were run in this environment on 2026-11-13 and passed:

| Check | Result |
|---|---|
| `ruff check src tests migrations`, `ruff format --check` | clean |
| `mypy src tests` (strict) | no issues in 87 files |
| `TREND_AGENT_REQUIRE_DB_TESTS=1 pytest --cov` (Python 3.12) | **374 passed**, 3 live deselected; total coverage 91 % (discovery modules 88–100 %, `cli_discover.py` 66 %) |
| same suite on Python 3.13 | 374 passed |
| `alembic check` after upgrade | no drift |
| migration 0002 upgrade on a DB with Phase 1 data, downgrade/upgrade round trip | verified (earlier in this phase) |
| opt-in live test (`TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_discovery_feeds_live.py`) | 1 passed (real PyPI feed) |
| live YouTube / Reddit / HN / GitHub tests | **not run**: no credentials / hosts blocked |

Phase 2 tests: 111 unit tests (config, extraction, intent examples from the brief, expansion,
normalisation, clustering, evidence/syndication, novelty, priority, lifecycle, budget,
providers incl. XML bombs, proxy denial, HTTP error mapping, quotas, OAuth) and 8 database
integration tests (end-to-end demo, provenance/score/cluster/history invariants,
idempotent re-run, single-category and dry runs, no-access run fabricates nothing,
retention cascade, all `discover` CLI commands, missing configuration exit code). Explicit
checks: suggestions/video data never written as search-interest observations; generated
hypotheses never carry `query_observed` evidence; syndicated copies are one origin;
similar names are separate entities; denied access produces only curated-seed items.

Fixes made while testing (behaviour changes): GitHub repository references rejecting
`..`/`.` segments; release dates no longer derived from arbitrary mentions (curated/family
/rumoured entities have none, basis recorded); whole-name matching for claimed dates;
template grounding records the entity; novelty made timestamp-based (re-runs were changing
scores); superseded code releases not expanded; YouTube discovery's own daily cap enforced.

## F. Known limitations

* **Environment**: only PyPI was reachable; YouTube, Reddit, Hacker News, GitHub, vendor
  blogs and Steam were not exercised live. Their behaviour is verified only against mocked
  HTTP that follows the documented APIs.
* **Unverified URLs**: 6 feed URLs and 3 YouTube channel handles are unverified (marked in config).
* **Gaming** discovery has no working real source without a YouTube key and reachable
  official news feeds.
* **Priority is uncalibrated**: weights are heuristics, and with the default threshold (45)
  every valid candidate in the fresh demo was prioritised. The threshold does not yet
  discriminate. Intent scores are rule scores, not probabilities.
* **Deterministic extraction** is English-only and pattern-based: unusual product names
  can be missed, and the gazetteer needs maintenance. Title-phrase candidates come from
  creators' titles and can be clickbait.
* **Clustering** is lexical only; no embedding backend is bundled.
* **Syndication detection** is heuristic (shingle containment ≥ 0.6, Jaccard ≥ 0.8 for short
  texts, URL/publisher/author identity); heavily rewritten copies count as separate origins.
* Claimed release dates are parsed only in ISO and "Month D, YYYY" formats.
* Exploration queries use YouTube only, so they are inactive without a key.
* Keywords persist after their source items expire (derived text); their evidence links
  are removed by the retention purge.
* No per-host politeness delay beyond poll intervals, quotas and 4-way concurrency.

## G. Phase 3 readiness

Phase 3 (monitoring) can build directly on:

* `discovered_keywords` with `candidate_status = prioritized`, each linked to a Phase 1
  `search_queries` row (the same table Phase 1 ingestion uses), with category, intent,
  entity, clusters, priority and provenance.
* `lifecycle.transition(..., actor="monitoring")`: the only way into `MONITORING`;
  discovery refuses it. `KeywordOrigin.VERIFIED_INTEREST` is reserved for keywords with
  real interest observations.
* Phase 1 ingestion (`search_interest_observations`, comparability rules) for the actual
  measurements, which discovery has never written to.
* Dormancy and re-activation already work; Phase 3 will need a repository method to claim
  prioritised keywords for monitoring (not built — out of scope).

Not started, by instruction: monitoring, surge detection, forecasting, recommendations.

## H. Recommended improvements

1. Configure a YouTube Data API key (and Reddit app credentials) and run discovery from a
   network that allows the configured hosts. Then verify the 6 unverified feeds and the 3
   channel handles, and enable the channels.
2. Add verified official news feeds for major games (publisher blogs, patch notes) so gaming
   does not depend on YouTube alone.
3. Calibrate `prioritize_threshold` and the priority weights against Phase 3 outcomes:
   which prioritised keywords actually showed measurable interest.
4. Add an embedding `SimilarityBackend` and an optional LLM `EntityExtractor`. Both
   interfaces exist, and grounding checks already guard LLM output.
5. Apply for Google Trends API alpha access; the related-queries provider is ready for a
   transport.
6. Extend date parsing and add multilingual gazetteers when regions beyond US/GB are added.
