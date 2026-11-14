# Phase 7 architecture: recommendation engine and intelligence dashboard

Phase 7 turns verified Phase 4–6 outputs into evidence-grounded video **ideas** for a
creator to evaluate, and presents demand, gaps, forecasts and ideas in a dashboard. It does
not create, upload or publish videos, and it does not retrain anything from feedback.

```
 Phase 4 surge events ─┐
 Phase 5 opportunity   ├──► RecommendationService ──► video_recommendations (+ hooks,
   reports + evidence  │      (bounded, cached,          outlines, scores), runs, alerts
   bundles             │       budgeted)                         │
 Phase 6 forecasts ────┘          │                              ▼
                                  │                    DashboardBackend (protocol)
                     RecommendationEngine               ├─ DatabaseBackend (PostgreSQL)
                     (pure, per opportunity)            └─ DemoBackend (in memory, synthetic)
                                                                 │
                                                       FastAPI dashboard API (/api/*)
                                                       bearer token · rate limits · CORS
                                                                 │  server side only
                                                       Next.js dashboard (frontend/)
                                                       server rendering + same-origin proxy
                                                                 │
                                                              browser (no credentials)
```

## Components

| Layer | Module | Responsibility |
|---|---|---|
| Contract | `schemas/recommendation.py` | `recommendation/1.0`: concept, titles, hooks, labelled outline, evidence, feasibility, ranking, verification, demand/forecast summaries, creator profile |
| Contract | `schemas/dashboard.py` | `dashboard/1.0` read models (data-class labelled series, forecasts, opportunities, gaps, alerts, system) and request bodies |
| Config | `config/recommendations.yaml`, `recommendations/config.py` | versioned weights, generation bounds, production profiles, diversity, LLM budget and prices, alert thresholds, API limits |
| Inputs | `recommendations/context_builder.py` | validates Phase 4–6 inputs (eligibility, policy flags, report age, forecast query/age/synthetic match) and builds the constrained context (confirmed entities, requirements, gap, credible facts, evidence references, creator profile) |
| Formats | `recommendations/format_selector.py` | 30 specialised formats (10 AI, 10 technology, 10 gaming) selected by search intent and gap; comparisons only with a second verified entity |
| Generation | `recommendations/idea_generator.py` | deterministic template generator (default) and an optional LLM generator with a strict schema, bounded attempts and template fallback |
| Text rules | `title_generator.py`, `hook_generator.py`, `outline_generator.py` | search-oriented titles (7 strategies), hooks (6 types, 5–15 s), labelled outlines |
| Verification | `recommendations/verification.py` | Phase 7 rules + the existing verification gate; application code decides the status |
| Feasibility | `recommendations/feasibility.py` | hours, profile (2 h / 8 h / 24 h), complexity, requirements, capacity, urgency from the Phase 6 window |
| Ranking | `ranking.py`, `deduplication.py`, `diversity.py` | transparent versioned score, duplicate merging, diversity caps |
| Orchestration | `recommendations/orchestrator.py` | one opportunity end to end; deterministic recommendation IDs |
| Service | `recommendations/service.py` | candidates from the database, reuse of unchanged evidence, LLM budget, cross-opportunity ranking, idempotent persistence, alerts |
| Alerts | `recommendations/notifications.py` | event detection, deduplication, cooldown, in-app / email / Telegram channels with bounded retries |
| Read models | `views.py`, `backend.py`, `db_backend.py`, `demo.py` | the `DashboardBackend` protocol and its two implementations |
| API | `api/app.py`, `api/security.py` | REST endpoints, authentication, rate limiting, CORS, security headers, structured errors |
| Persistence | `db/recommendation_models.py`, `db/repositories/recommendations.py`, migration `0008` | 11 tables; retention |
| Evaluation | `recommendations/evaluation.py`, `config/recommendations_eval/` | labelled calibration / held-out drafts, duplicate threshold sweep, generated-idea metrics |
| CLI | `cli_recommend.py` | `recommend generate/list/explain/verify/stats/demo/evaluate`, `dashboard serve` |
| UI | `frontend/` | Next.js 15 + TypeScript + Tailwind + Radix + Recharts |

## Data flow for one generation run

1. **Candidates**: up to `generation.max_opportunities_per_run` stored opportunities with an
   eligible status (`candidate`; `insufficient_evidence` yields one labelled research
   candidate), highest Phase 5 score first. Synthetic records only with `--include-synthetic`.
2. **Inputs**: the report's own evidence bundle (read-only registry view), the entity
   catalogue and statistics of the target it was analysed for, and the latest Phase 6
   forecasts for the same query. Nothing is recomputed.
3. **Reuse**: the input fingerprint (report version, forecast ID, profile, generator,
   configuration, engine version) is compared with stored current recommendations; if it is
   unchanged, nothing is generated and no LLM call is made.
4. **Generation → verification → feasibility → ranking** per concept (see
   `recommendation_engine.md`).
5. **Global ordering**: deduplication and diversity run across every current
   recommendation of the profile, real and synthetic ranked separately.
6. **Persistence**: idempotent upsert by deterministic ID; superseded ideas are marked
   `is_current = false` (user state attached to them is kept).
7. **Alerts**: new strong ideas, new or upgraded opportunities, time-sensitive windows and
   verification changes, deduplicated and cooled down.

## Isolation and safety boundaries

* **Demo mode** (`dashboard serve --demo`, `recommend demo`) builds its snapshot from the
  fictional Phase 5/6 scenarios in memory and never opens the database. Every record is
  labelled synthetic; alerts stay in-app.
* **Production mode** excludes synthetic records from every query unless a test explicitly
  asks for them.
* **Credentials** (YouTube, Anthropic, SMTP, Telegram, API token) live in server settings
  only. The browser talks to the Next.js server, which calls the API with the token from its
  own environment; mutations go through an allowlisted, same-origin proxy.
* **The LLM** (optional, off by default) drafts text only. It sees untrusted source text
  inside a nonce-delimited data block, writes no numbers, and cannot approve its claims:
  application code verifies every statement and decides the status. Valid JSON is not
  treated as correct output.
* **Nothing executes recommendation text.** Instruction-like, command and credential text is
  removed from generated output; the system has no path that runs it.

## What Phase 8 receives

A complete, observable workflow: stored inputs → verified, ranked ideas with full
provenance → user decisions, feedback and production outcomes → evaluation fixtures and
benchmarks. Phase 8 (deployment, monitoring, retraining) is not started here.
