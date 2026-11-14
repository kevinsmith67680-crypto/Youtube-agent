# Phase 7 report — AI video recommendation engine & intelligence dashboard

Status: implemented and verified as described below. Phase 8 has not been started.
Everything shown as an example comes from **synthetic fixtures** (fictional products and
games); nothing here is a real recommendation or real search demand.

## A. Implementation summary

* **Recommendation engine** (`src/trend_agent/recommendations/`): validates Phase 4–6
  inputs, builds a constrained context, selects formats from 30 specialised AI / technology /
  gaming formats by search intent and gap, generates concepts (deterministic templates by
  default; an optional, budgeted LLM generator with a strict schema and template fallback),
  verifies every statement (Phase 7 rules + the existing verification gate; application code
  decides the status), evaluates feasibility for 2 h / 8 h / 24 h production profiles,
  ranks with a transparent versioned score, deduplicates and diversifies.
* **Each recommendation** (`recommendation/1.0`) has a primary title and alternatives with
  strategies, three hooks, the format and why, alternative approaches, audience, viewer
  problem, premise, key questions and information requirements, a labelled outline
  (`VERIFIED_FACT`, `PLANNED_DEMONSTRATION`, `HYPOTHESIS_TO_TEST`, `UNKNOWN`) with what to
  verify before publishing and likely failure points, evidence references, feasibility and
  urgency, the score with every component, the verification record, demand and forecast
  summaries, and versions.
* **Persistence** (migration 0008, 11 tables), deterministic IDs and idempotent writes,
  reuse of unchanged evidence, retention (saved ideas kept), creator profiles, feedback,
  production tracking, alerts with deduplication, cooldowns and bounded retries.
* **API**: FastAPI with bearer-token auth, a read-only public mode, rate limits, restrictive
  CORS, security headers and structured errors; `DatabaseBackend` and an isolated
  `DemoBackend`.
* **Dashboard** (`frontend/`): Next.js 15 + TypeScript + Tailwind + Radix + Recharts; overview,
  opportunity explorer and detail, content gap explorer, trends and trend detail,
  recommendations and recommendation detail, saved ideas, settings. Dark by default,
  accessible (axe WCAG 2.1 A/AA clean on every page in both themes), credentials never in
  the browser.
* **CLI**: `recommend generate | list | explain | verify | stats | demo | evaluate`,
  `dashboard serve [--demo] [--ui]`.
* **Evaluation** with labelled calibration and held-out sets; **benchmarks** recorded.

Fixes found while testing (before evaluation): facts could keep the `VERIFIED_FACT` label
when no title passed; a fact citing evidence outside the analysis left the idea `APPROVED`;
planned demonstrations containing shell commands were kept; one-word hooks were accepted;
the time breakdown double-counted research; production-status milestones were compared
against an already-updated row; a Phase 5 manual-analysis lookup failed when a query text
existed for several regions; sorting opportunities by breakout probability validated every
report (1.5 s → 0.1 s at 500 opportunities).

## B. Repository architecture and file changes

See `phase7_architecture.md` for the diagram and module responsibilities.

| Area | Files |
|---|---|
| Contracts | `schemas/recommendation.py`, `schemas/dashboard.py` |
| Engine | `recommendations/{config,context_builder,format_selector,title_generator,hook_generator,outline_generator,idea_generator,verification,feasibility,ranking,deduplication,diversity,orchestrator}.py` |
| Service, alerts, read models | `recommendations/{service,notifications,views,backend,db_backend,demo,evaluation}.py` |
| Persistence | `db/recommendation_models.py`, `db/repositories/recommendations.py`, `migrations/versions/20261114_0008_recommendations.py` |
| API | `api/app.py`, `api/security.py` |
| CLI / wiring | `cli_recommend.py`, `cli.py`, `container.py`, `config.py`, `ingestion/retention.py` |
| Configuration | `config/recommendations.yaml`, `config/recommendations_eval/eval_{calibration,heldout}.yaml` |
| Frontend | `frontend/` (app routes, components, charts, lib, Vitest and Playwright tests) |
| Scripts | `scripts/benchmark_recommendations.py`, `scripts/benchmark_dashboard.cjs` |
| Tests | `tests/unit/recommendations/{test_api,test_engine,test_evaluation}.py`, `tests/integration/test_recommendations.py`; `tests/conftest.py`, `tests/integration/test_database.py` updated |
| Docs | the nine Phase 7 documents, `README.md`, `.env.example`, `docs/benchmarks/phase7_{benchmark,evaluation}.json`, `docs/screenshots/phase7_*.png` |

Changes to earlier phases: Phase 5 `inputs_for_query` now prefers the requested
language/region when a query text exists more than once (it raised before); retention
also purges expired recommendations. Version 0.7.0; `fastapi` and `uvicorn` added.

## C. Database migrations

`0008` (additive; nothing earlier is altered): `creator_profiles`,
`recommendation_generation_runs`, `video_recommendations` (projection columns + the full
document; `is_current`, `is_synthetic`, `expires_at`, check constraints on status, score
range and hours), `recommendation_hooks`, `recommendation_outlines`,
`recommendation_scores`, `saved_recommendations`, `recommendation_feedback`,
`production_tracking` (PUBLISHED requires a video ID), `notifications` (unique dedup key),
`notification_deliveries`. Verified: upgrade, downgrade and re-upgrade on the development
database; `alembic check` reports no drift; the schema test asserts revision `0008`.

## D. Recommendation generation methodology

Documented in `recommendation_engine.md` and `video_generation_prompts.md`. In short:
validated inputs → context of confirmed entities, requirements, gap and credible facts →
formats that serve the intent → concepts → verification → feasibility → score → global
deduplication and diversity → idempotent storage → alerts. The LLM path never sees scores or
volumes, treats source text as untrusted data, writes no numbers, and its schema has no
numeric fields; schema-valid output is verified like any other.

## E. Ranking and verification logic

Ranking (`recommendation_scoring.md`): seven components weighted 25 / 20 / 15 / 15 / 10 / 10
/ 5 (version `ranking-1.0`); missing components are excluded and listed, never zero; at least
60% of the weight must be available; an evidence-quality factor (status × Phase 5 evidence
quality) scales the score; research candidates and rejected ideas are never ranked. The
score is a research-priority index, not a probability of success.

Verification (`recommendation_engine.md` §5): deterministic rules for titles, hooks, labels,
evidence references, forecast/view claims and instruction/command text, then the
verification gate on declared facts and on the factual elements of free text. Status by
code: `APPROVED`, `APPROVED_WITH_UNCERTAINTY`, `RESEARCH_REQUIRED`, `REJECTED`.

## F. Dashboard features and screenshots

Pages, filters and visualisation rules: `dashboard.md`. Screenshots (synthetic demo):
`docs/screenshots/phase7_overview.png`, `phase7_opportunities.png`,
`phase7_trend_detail.png`, `phase7_recommendation.png`, `phase7_recommendation_light.png`,
`phase7_gaps.png`.

## G. Example recommendations (synthetic fixtures, template generator)

From `trend-agent recommend demo` (17 ideas: 6 approved, 9 approved with uncertainty, 2
research candidates; 9 ranked after diversity).

**Gaming — rank 1, score 78.7, APPROVED** — query "where to find the ember lantern
skyforge arena season 8" (direct YouTube Search surge candidate; version-mismatch gap).
* Title: *Where to Find the Ember Lantern in Skyforge Arena Season 8* (alternative:
  *Ember Lantern Location Guide (Skyforge Arena Season 8)*). Format: item-location
  walkthrough.
* Hooks: "Here's exactly where to find the Ember Lantern in Skyforge Arena Season 8, and
  what you need to check before you go." / "This is the Ember Lantern. Let's head straight to
  where it is in Skyforge Arena Season 8 and how to pick it up." / "Can't find the Ember
  Lantern? We'll go straight to its location in Skyforge Arena Season 8, no long intro."
* Outline (excerpt): VERIFIED_FACT "According to Ember Studios: The new season adds the Ember
  Lantern, a reward hidden in the volcano region, …"; PLANNED_DEMONSTRATION "Show the route
  to the Ember Lantern on the map and in-game"; UNKNOWN "Research before filming: when the
  item is available (not established by the inspected evidence)".
* Feasibility 3.5–7 h, standard tutorial, publish soon (Phase 6 window URGENT for the 8 h
  profile); equipment unknown (not in the profile). Score: demand 18.8, gap 18.0, intent
  15.0, forecast 13.5, creator 10.0, feasibility 9.3, originality 1.0; evidence factor 0.92.

**Technology — score 63.7, APPROVED_WITH_UNCERTAINTY** — "how to migrate to pyxis 5"
(external web-search surge: labelled "demand is not measured YouTube search growth").
* Title: *How to Migrate to Pyxis 5: Step-by-Step Guide*; the alternative *Pyxis 5
  Migration: Fixing the Breaking Changes* was removed by the gate (not supported by the
  records).
* Hook: "Upgrading to Pyxis 5 and something broke? Let's migrate a real project step by
  step." Outline: VERIFIED_FACT "According to Pyxis Foundation: Pyxis 5 is released.";
  UNKNOWN "Migration steps … not established by the inspected evidence"; HYPOTHESIS_TO_TEST
  "Check whether the steps work on a clean machine".
* 7.1–14.2 h; urgency unknown; the forecast component is excluded (no calibrated
  probability) and the explanation says so.

**AI — APPROVED** — "how to install codeforge 2" (direct YouTube Search surge candidate).
* Title: *How to Install CodeForge 2: Complete Setup Guide*; alternatives *CodeForge 2 Setup
  Tutorial: From Download to First Project*, *CodeForge 2 Setup Problems and How to Fix
  Them*. Facts cited from Lumen AI ("CodeForge 2 is available today for Windows, macOS and
  Linux."). Forecast: calibrated breakout probability 0.26 (synthetic model), urgency
  unknown (no estimable window). 5.9–11.8 h.

**Research candidates (not ranked)**: *where to find the void scythe skyforge arena season
8* (the item is not confirmed by any record) and *glimmer shards farming* (no demand
episode, no confirmed entity).

## H. Backend API documentation

`dashboard_api.md` — 24 routes, authentication modes, limits, error codes and
production-status transitions.

## I. Tests, linting, typing and integration results

All results below were produced by commands run in this environment.

| Check | Result |
|---|---|
| Full backend suite, Python 3.12, with coverage (unit + integration, PostgreSQL 16) | TEST_RESULT_312 |
| Full backend suite, Python 3.13 | TEST_RESULT_313 |
| Phase 7 backend tests | 39 API, 66 engine, 6 evaluation (unit); 11 integration (incl. Phase 4 → 5 → 6 → 7 on the database) |
| Frontend unit/component tests (Vitest, Testing Library, axe-core) | 29 passed |
| Frontend end-to-end (Playwright, Chromium, against the demo API; axe WCAG 2.1 A/AA on every page) | 13 passed |
| `ruff check` / `ruff format --check` (src, tests, scripts) | clean |
| `mypy src` (strict settings of the project) | clean, 236 files |
| `tsc --noEmit` (frontend, strict) and `next build` | clean |
| Migrations | upgrade / downgrade / upgrade and `alembic check` clean |

The 30 required test areas map to: input validation, context, generation, structured
output, titles, hooks, outlines, intent alignment, entity / evidence-reference / numerical /
forecast-claim verification, feasibility, ranking, missing data, deduplication, diversity,
hallucination rejection, prompt injection, alert deduplication and demo isolation
(`test_engine.py`); database idempotency, save/dismiss, production status and the pipeline
(`tests/integration/test_recommendations.py`); API validation and authentication
(`test_api.py`); frontend rendering, filtering/sorting, accessibility and provider-unavailable
states (`frontend/tests`, `frontend/e2e`); regression of Phases 1–6 (the full suite).

## J. LLM cost and performance measurements

`docs/benchmarks/phase7_benchmark.json` (4 CPUs, Python 3.12; measured):

| Measurement | Result |
|---|---|
| generation per opportunity, template generator (incl. verification and ranking) | mean 58 ms, p50 72 ms, p95 105 ms (17 ms per concept); 0 LLM calls, $0 |
| LLM path engine overhead (scripted client; excludes model latency) | mean 62 ms, p95 117 ms |
| LLM tokens and cost — **estimate, no live call** (characters/4 of the real prompt and of a schema-valid answer; configured list price $4 / $20 per million tokens) | ≈2,070 input + ≈3,650 output tokens per call; ≈$0.081 per opportunity, ≈$0.024 per recommendation; at most 2 calls per opportunity; daily budget 400,000 tokens |
| API latency, demo backend (p50) | 4.0–5.5 ms for every endpoint measured |
| Database, 500 opportunities / 2,500 recommendations (p50) | ranked list 13 ms (page 40: 15 ms), category + score 4 ms, text search 16 ms, combined filters 12 ms, opportunities page 61 ms, sort by breakout 97 ms, recommendation detail 10 ms, overview 24 ms |
| Persistence | 4.8 ms per new recommendation (with hooks, outline, scores); idempotent re-save of 2,500: 3.3 s, 0 rows created |
| Dashboard load, production build against the demo API (Chromium) | cold first visit: 81–166 ms to load event; warm medians 45–79 ms, trend detail (charts) 166 ms |

Real LLM latency, token use and cost were not measured (no live calls in this environment;
the LLM generator is off by default).

## K. Source and forecast limitations

* All demonstrated recommendations come from synthetic fixtures; no real opportunity was
  processed. The engine depends entirely on the quality of Phase 4–6 outputs: without stored
  candidate opportunities it generates nothing (by design).
* Demand is only called YouTube demand when Phase 4 measured YouTube Search directly;
  external web-search and proxy signals are labelled as such and lower the score.
* Forecasts are relative-interest estimates of the measured source from synthetic-trained
  models in the demo; real-world forecast accuracy remains unknown (Phase 6 report).
* Evidence is lexical: the gate and the evaluation's grounding check can miss paraphrased
  unsupported claims that reuse the record's words.
* Competing videos are referenced by ID; findings rest on metadata unless authorised full
  content exists.

## L. Exact local startup commands

```bash
docker compose up -d
pip install -e ".[dev]"
trend-agent db upgrade
# synthetic demonstration (no database needed)
trend-agent recommend demo
export TREND_AGENT_API_TOKEN=$(openssl rand -hex 32)
trend-agent dashboard serve --demo                      # API on http://127.0.0.1:8000/api
# dashboard (second terminal)
cd frontend && npm ci && cp .env.example .env.local     # set the same TREND_AGENT_API_TOKEN
npm run build && npm start                              # http://localhost:3000
# real data (after Phases 3-6 have stored opportunities and forecasts)
trend-agent recommend generate --all
trend-agent recommend list --limit 20
trend-agent dashboard serve                             # database mode
```

## M. Known issues

* The duplicate merge is lexical (title word overlap ≥ 0.6): paraphrased duplicates can
  survive (one held-out pair: overlap 0.50).
* Title query coverage is lexical with a small synonym table.
* Several creator-profile fields are stored but not used by the engine yet
  (`creator_profiles.md`); the profile's `llm_daily_token_budget` does not override the
  server budget.
* The in-process rate limiter is per API process; multiple workers would each count
  separately.
* Opportunity lists validate the report documents of the requested page (≈60 ms per page of
  25 at 500 opportunities); sorting by breakout reads at most 1,000 opportunities.
* The dashboard has no user accounts: anyone who can reach the Next.js server acts as the
  creator. Run it locally or behind your own access control.
* Human usefulness has not been measured.
* Next.js / React versions were pinned to known releases; `npm` reports deprecation notices
  for transitive dependencies of the test tooling.

## N. Phase 8 readiness assessment

Ready for Phase 8 to evaluate the complete workflow:

* Every stage is persisted with provenance (inputs, fingerprint, generator, versions,
  verification record, score components) and every user action is recorded (saved,
  dismissed, feedback, production milestones, published video ID), which is what an outcome
  evaluation needs.
* Evaluation fixtures, the evaluation command and benchmarks exist and are reproducible.
* Not done here (Phase 8 scope): deployment and process supervision, multi-user
  authentication, shared rate limiting, monitoring and alerting on the service itself,
  outcome collection with authorised channel analytics, real-data evaluation and any
  retraining from feedback.

## Acceptance criteria

"Verified" means checked by an executed test, command or measurement in this environment.
"Demo" means verified on synthetic fixtures only.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Phases 1–6 remain operational | Verified | full suite on 3.12 and 3.13 (section I) |
| 2 | Verified Phase 4–6 outputs feed the engine | Verified (synthetic data) | integration test Phase 4 surge → Phase 5 stored opportunity → Phase 6 stored forecasts → Phase 7 ideas citing the stored forecast |
| 3 | Relevant video concepts | Demo | 100% of generated ideas serve the intent; gap relevance 0.67 (`recommendation_evaluation.md`) |
| 4 | Titles match search intent | Demo | format alignment 0.88, title rules, evaluation title rejection F1 1.0 |
| 5 | Hooks relevant and grounded | Demo | hook rules and gate checks; evaluation hook rejection F1 1.0 |
| 6 | Outlines contain no fabricated facts | Demo | 0 unsupported statements published as fact (both splits); 24/24 generated facts grounded |
| 7 | Creator preferences supported | Verified | category/exclusion skips, format preference, capacity, requirements (unit + integration tests) |
| 8 | Production feasibility evaluated | Verified | feasibility tests; every idea carries hours, profile, breakdown, requirements, urgency |
| 9 | Ranking transparent | Verified | components, weights, raw inputs, explanation and version stored and shown |
| 10 | Missing data handled correctly | Verified | missing components excluded, never zero; unavailable forecasts shown as unavailable |
| 11 | Similar ideas deduplicated | Verified (lexical) | dedup/diversity tests; held-out merge F1 0.89 |
| 12 | Verification gates enforced | Verified | engine tests; status decided by code |
| 13 | Unsupported claims rejected or labelled | Verified (synthetic) | adversarial drafts: fabricated numbers, versions, dates, unlock methods, fake citations, results as facts, injection |
| 14 | Records persist correctly | Verified | integration tests: idempotent writes, reuse, supersession, retention |
| 15 | REST API endpoints work | Verified | 39 API tests; integration tests through the database backend |
| 16 | Dashboard loads | Verified | production build; Playwright; load timings |
| 17 | Opportunities filtered and sorted | Verified | API, database backend and Playwright tests |
| 18 | Charts use correctly labelled data | Verified | data-class badges and per-measurement charts (component + e2e tests) |
| 19 | Recommendation details show evidence | Verified | detail page and API document include evidence, labels and verification |
| 20 | Save and dismiss | Verified | API, database and e2e tests |
| 21 | Production tracking | Verified | transitions, PUBLISHED requires a video ID, history (API, database, e2e) |
| 22 | Feedback recorded | Verified | API, database and e2e tests |
| 23 | Alerts deduplicated | Verified | unit tests (dedup, cooldown, retries, synthetic suppression) and database integration test |
| 24 | Providers fail gracefully | Verified | unconfigured channels/providers reported; API-unavailable UI state; LLM failure → template fallback |
| 25 | Synthetic demo isolated | Verified | demo never opens the database (test); production mode excludes synthetic records (test) |
| 26 | Credentials never exposed to the frontend | Verified | no token or key in any API response or page/script/JSON response (API + e2e tests) |
| 27 | Automated backend tests pass | Verified | section I |
| 28 | Frontend tests pass | Verified | 29 Vitest + 13 Playwright |
| 29 | Migrations apply | Verified | up/down/up, `alembic check`, schema test |
| 30 | Type checking and linting pass | Verified | ruff, mypy, tsc |
| 31 | Performance benchmarks reported | Verified | section J (LLM figures are labelled estimates) |
| 32 | Phase 8 can evaluate the complete workflow | Verified as far as testable here | provenance and user-action data stored; evaluation and benchmark tooling (section N) |
