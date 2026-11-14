# Hacker News API integration report

Date: 2026-11-14 (UTC). Branch `claude/tender-thompson-620dmi`, schema revision **0011**.

## Result

The agent now reads the official Hacker News API (`https://hacker-news.firebaseio.com/v0/`), with no credentials needed. It covers the five story lists, item records, `/updates.json` and `/maxitem.json`.

Collection and storage:
- Every story is classified (AI, developer technology, gaming, or not relevant) and stored with full provenance.
- A story is re-observed at a cadence set by its priority, so measured discussion momentum (points and comments per actual hour) builds up over time.
- Stories flow through the existing Phase 2 pipeline (items → entities → keyword hypotheses), so `discover run` and the scheduler include Hacker News.
- A dedicated 15-minute scheduler stage polls the lists that are due and refreshes tracked stories.

Interfaces:
- a `trend-agent hackernews …` command group;
- four read-only API endpoints;
- a `/hackernews` dashboard page with a list filter, plus a story detail page.

**Verified live in this sandbox:**
- every endpoint;
- a bounded discovery run over all five lists (152 requests, 66 relevant stories stored);
- incremental cursors;
- the `/updates.json`-driven refresh, which produced real measured momentum on five stories;
- entity linking;
- the dashboard against the live data.

**Not verified live:**
- The 15-minute cadence over many hours. The scheduler stage ran in tests with a simulated clock, but not unattended for a day.
- Behaviour under a real Hacker News outage. Only simulated failures were tested.

Hacker News data is labelled **community discussion activity** (`COMMUNITY_ACTIVITY`) everywhere. It is never search interest or search volume, it never feeds a YouTube Search surge claim, and the dashboard says so wherever activity numbers appear.

## 1. Audit before implementation

| Area | Finding | Decision |
|---|---|---|
| Existing HN support | Phase 2 `hackernews` provider uses the **HN Search API by Algolia** (keyword search, front page). The official Firebase API has no search. | Keep it unchanged for keyword search. Add a separate `hackernews_firebase` provider for the official lists and items, and never pretend it can search. |
| Provider registry / discovery | `DiscoveryProvider` protocol (`supports`, `can_afford`, `discover`, `status`). `DiscoveryRequest` carries the source's stored `etag`. Observer hook from the GitHub work. | Implement the protocol. Store the last-seen story ID in the source's `etag` field for incremental polling. Record structured rows through an observer. |
| Phase 3/8 scheduling | `PipelineScheduler` stages with per-task intervals and a gated `discovery` stage. Per-source `min_poll_interval_minutes` in the planner. | Per-list cadence via source intervals. A new `hackernews` stage (900 s). |
| Phase 4 evidence | `community_discussion` evidence → signal class COMMUNITY_ACTIVITY. Community evidence alone cannot verify an entity. | Reuse it unchanged: HN alone never verifies an entity or a release. |
| Database | GitHub tables pattern (stable external ID primary key, observation table unique on (id, time)). | Migration 0011 adds `hn_stories` and `hn_story_observations`. It is purely additive, with no enum changes. |
| Verification layer | `verification/untrusted.py`: `sanitize`, `injection_signals`. | Reused for titles and text. |
| Dashboard / CLI | GitHub page and API block as the template. | Same structure, HN-specific evidence classes. |
| Official API facts (probed live) | Lists: new 500 IDs, top 500, best 200, show ≈180, ask ≈28. Unknown IDs return HTTP 200 with `null`. No ETag. `Cache-Control: no-cache`. `/updates.json` = `{items, profiles}`. | Handle `null`, `deleted`, `dead` and non-story types explicitly. Use TTL caches instead of conditional requests. |

## 2. Provider architecture

```
config/sources/hackernews.yaml (5 list sources)
        │
        ▼
DiscoveryService ─► HackerNewsDiscoveryProvider ─► HackerNewsClient ─► official API (GET only)
        │                  │ classify_story (deterministic, optional constrained assist)
        │                  ▼
        │            DiscoveryItem (community_discussion, discussion URL = reference)
        ├─► Phase 2: items, entities, keyword hypotheses, evidence links
        └─► HackerNewsObservationRecorder (observer)
                 ├─ hn_stories (upsert by story ID) + hn_story_observations (one per poll)
                 └─ finish(): link entity → momentum → priority → refresh tier/next refresh
HackerNewsRefresher (scheduler stage / CLI): /updates.json hint + due stories → items
                 → observations → momentum → priority (bounded per run)
HackerNewsReadService → CLI + /api/hackernews/* → /hackernews dashboard
```

**Client** (`hackernews/client.py`)
- Read-only `GET`, with no authorization header.
- Path validation: only `/….json`, no `..`.
- Response size cap of 2 MB.
- Short TTL caches: lists 60 s, items 120 s, with in-flight deduplication.
- Bounded concurrency (semaphore, default 6) and a local daily request budget (default 20,000; the API documents no limit, and this client keeps one anyway).
- Retry with jittered backoff for transient errors.
- Errors are typed:
  - timeout → `ProviderTimeoutError`;
  - network → `TemporarilyUnavailableError`;
  - proxy denial → `AccessRestrictedError(reason=network_policy)`;
  - 429 → `RateLimitedError`;
  - 401/403 → `AccessRestrictedError`;
  - 5xx → `TemporarilyUnavailableError`;
  - malformed or oversized JSON → `MalformedResponseError`.
- `items()` records failures per item; it is never all-or-nothing.

**Records** (`hackernews/models.py`)
- `HNItem` ignores unknown fields.
- `parse_item` returns `missing` for `null`, `malformed:<field>` for schema problems, and skip reasons `deleted`, `dead`, `not_a_story:<type>`, `missing_title` and `missing_time`.

**Provider** (`discovery/providers/hackernews_firebase.py`)
- Capabilities:
  - `NEW_STORY_DISCOVERY`
  - `TOP_STORY_DISCOVERY`
  - `SHOW_HN_DISCOVERY`
  - `ASK_HN_DISCOVERY`
  - `STORY_METADATA`
  - `STORY_UPDATES`
  - `DISCUSSION_ACTIVITY`
  - `TREND_DISCOVERY`
- `new`, `show` and `ask` are read incrementally: only IDs above the stored cursor `hn:last=<id>`. If an item request fails, the cursor stays just below it, so it is retried next poll.
- `top` and `best` are re-read so their stories get repeated observations.
- Irrelevant, prompt-injection-flagged, older-than-lookback, dead, deleted and non-story records are dropped. Each reason is counted in the source's warnings, e.g. `not used: not_ai_technology_or_gaming x27`.

Each item carries:
- story ID, sanitised title and text;
- linked URL, as the item URL; its domain is the publisher, so reposts of one article share an origin;
- discussion URL, as the stable reference;
- author, published and collected times;
- score, comments, type, list and rank;
- the full classification;
- signal class, and `is_synthetic=false`.

**Provider status**
- Transient failures never block planning: each source backs off on its own consecutive failures.
- An access refusal pauses the provider for 30 minutes and a 429 for 15 minutes. Both expire, so a long-running scheduler recovers on its own (this was a bug in the first draft, caught by the outage integration test).

**Classifier** (`hackernews/classify.py`)
- Deterministic.
- Output: kind (Show/Ask/Launch/Tell HN or story), subject (product launch, release, announcement, question, discussion), primary and secondary category, relevance, ambiguity flag, a grounded project name (Show/Launch HN titles only), entity type, and potential search intents.
- Scoring:
  - title term 1.0, text term 0.4;
  - topical domain +1.5 (arxiv, huggingface, rust-lang.org, Steam, …);
  - code hosts (github.com, gitlab.com, …) only +0.5 technology, because a game or AI tool hosted on GitHub is still a game or AI tool;
  - relevance = top score / 3, kept when ≥ 0.3.
- Ambiguity: a second category is kept as secondary at ≥ 50 % of the top score, and the story is flagged ambiguous at ≥ 80 %. Nothing is forced into gaming (or any category) to fill a slot.
- Optional assist: a `StoryAssist` may choose only between the two ambiguous candidates. It cannot add categories or entities, and none is configured.
- Untrusted HTML:
  - stripped and sanitised;
  - instruction-like text is withheld;
  - an instruction-like **title** excludes the story;
  - the dashboard renders all text as text, never as HTML.

**Momentum** (`hackernews/momentum.py`)
- Needs two stored observations.
- Compares the latest observation with the most recent earlier one at least 10 minutes older. Deltas plus rates are computed over the **actual** elapsed time.
- `rising` means ≥ 20 points/h or ≥ 15 comments/h. It is a community-activity label only.
- One observation gives `insufficient_observations` with no growth reported.
- Too-close observations give deltas without rates.
- A falling score is noted.

**Priority** (`hackernews/priority.py`)
- 0–100 = 100·Σwᵢxᵢ/Σwᵢ, with every component stored as value, weight, contribution and basis.
- Default weights (configurable in `discovery.yaml`):

| Component | Weight |
|---|---|
| relevance | .25 |
| recency (half-life 12 h) | .15 |
| discussion activity (cumulative strength blended with measured rates) | .15 |
| launch significance | .15 |
| novelty | .10 |
| corroboration (independent publishers outside HN, /3) | .08 |
| entity verification | .07 |
| list reliability | .05 |

- **Reposts:** a story linking an article that an earlier story already linked (canonical URL, tracking parameters stripped) gets novelty 0.2, and its basis names the earlier story. Reposts are one origin, never independent confirmation.
- Corroboration counts publishers outside Hacker News only.

## 3. Files changed

New:
- `src/trend_agent/hackernews/`: `models.py`, `config.py`, `client.py`, `classify.py`, `momentum.py`, `priority.py`, `service.py` (recorder, assessment, refresher, list-discovery runner, connection test, read models)
- `src/trend_agent/discovery/providers/hackernews_firebase.py`
- `src/trend_agent/db/hackernews_models.py`, `src/trend_agent/db/repositories/hackernews.py`
- `migrations/versions/20261114_0011_hackernews_integration.py`
- `src/trend_agent/cli_hackernews.py`
- `config/sources/hackernews.yaml`
- `frontend/lib/hackernews.ts`, `frontend/components/HackerNewsViews.tsx`, `frontend/app/hackernews/page.tsx`, `frontend/app/hackernews/[id]/page.tsx`
- Tests:
  - `tests/hackernews_fixtures.py`
  - `tests/unit/hackernews/{test_client,test_classify_momentum_priority,test_provider}.py`
  - `tests/integration/test_hackernews.py`
  - `tests/live/test_hackernews_live.py`
  - `frontend/tests/hackernews.test.tsx`
- `docs/hackernews_api_integration_report.md`, `docs/screenshots/hackernews_dashboard.png`, `docs/screenshots/hackernews_story_detail.png`

Modified:
- `schemas/enums.py`: 7 capabilities.
- `config.py`: `hackernews_api_enabled`, shown in the settings summary.
- `discovery/providers/factory.py`: registration.
- `container.py`: recorder observer and `hackernews_refresher()`.
- `ops/pipeline.py`, `ops/config.py`, `config/operations.yaml`: the `hackernews` stage.
- `ops/providers.py`: credentials and approval text, plus a 3-request smoke probe.
- `ops/signals.py`: COMMUNITY_ACTIVITY.
- `ingestion/retention.py`: HN tables.
- `db/__init__.py`: expected revision 0011.
- `db/models.py`: model registration.
- `api/app.py`: four endpoints.
- `cli.py`: command group.
- `config/discovery.yaml`: `providers.hackernews_firebase`.
- Frontend: Nav link, Playwright spec.
- `.env.example`, `README.md`, `docs/discovery_configuration.md`.
- Tests pinned to the schema revision and the scheduler task list.
- `.secrets.baseline`: line numbers only.

The Algolia `hackernews` provider, `hn:front-page` and the `seed:hn:*` sources are unchanged.

## 4. Database migration 0011

Purely additive; Phase 1–8 tables are untouched. Upgrade, downgrade and upgrade were verified on the development database, and `alembic check` reports no drift.

**`hn_stories`**
- Primary key: `story_id`, the stable HN item ID.
- Story fields: sanitised title, `linked_url`, `canonical_link` (indexed; repost detection), `discussion_url`, domain, author.
- Classification: kind, subject, primary and secondary category, ambiguity flag, relevance, the full classification JSON.
- Activity and ranking: lists seen on, published time, latest score and comments, priority score and components, momentum JSON and state.
- Monitoring: monitoring state, refresh tier, `next_refresh_at`, `last_observed_at`.
- Links: `entity_id`, `item_id`, `provider_id`.
- Provenance: first source key, run ID, first and last seen, `is_synthetic`, `expires_at`.
- CHECK constraints on every enumerated column and range.

**`hn_story_observations`**
- One row per (story, `observed_at`), with a unique constraint `hn_observation_once`.
- Fields: score, comments, list, rank, origin (`list` or `refresh`), source key, run ID, item ID, `is_synthetic`, `expires_at`.
- Deleted with the story (`ON DELETE CASCADE`).

**Writes are idempotent.** Stories are upserted by ID. Titles and links follow the newest record, lists are merged, and the latest score is taken only from a newer observation. Observations use `ON CONFLICT DO NOTHING`. A story seen on a second list within the item-cache window of the same poll adds no duplicate observation.

**Retention** is 90 days (provider setting). Both tables are purged by the existing retention service.

## 5. Working endpoints (official API, GET only)

| Endpoint | Used for | Live status |
|---|---|---|
| `/newstories.json` | new stories (incremental) | ok: 500 IDs |
| `/topstories.json` | top stories (re-read) | ok: 500 IDs |
| `/beststories.json` | best stories (re-read) | ok: 200 IDs |
| `/showstories.json` | Show HN (incremental) | ok: 181 IDs |
| `/askstories.json` | Ask HN (incremental) | ok: 28 IDs |
| `/item/{id}.json` | story metadata, activity, deleted/dead | ok |
| `/updates.json` | changed-item hint for the refresher | ok: 43–53 changed items per read |
| `/maxitem.json` | connection test / smoke probe | ok: 50497300 |

No keyword search is claimed. The official API has none.

## 6. Live verification results (2026-11-14, 14:3x–14:47 UTC)

All numbers below are from real requests made in this session.

| Step | Result |
|---|---|
| `trend-agent hackernews test` | ok: 10 requests, all 8 endpoint types `ok`. Top-story sample: "Why get an expensive Pi when I have an old phone?" (237 points, `other`, correctly not relevant), "The Claude Delusion" (ai), "Generative AI uses more energy on non-English text" (ai). |
| `trend-agent hackernews discover` (all lists, 14:33:48–14:33:55) | 2 category runs, 5/5 sources succeeded, 152 requests. **new** 23 kept of 50 (27 not relevant); **best** 15 of 30; **show** 29 of 40 (9 already stored from *new*); **top** 8 of 30 (1 job, 21 not relevant); **ask** 6 of 20. **66 relevant stories, 0 failures.** |
| Cursors | new, show and ask stored `hn:last=<id>`; top and best none. |
| Entities | 37 new entity records (13 AI run, 24 technology run). After the title-grounded linking rule, 33 of 66 stories are linked to 28 distinct entities: 9 verified (Claude, ChatGPT, Cursor, Gemini 4, Linux, NVIDIA, OpenAI, Rust, macOS) and 19 `needs_verification` (17 Show HN project names plus Rust 1.100 and Pydantic v2). |
| Refresh 14:35 | 1 request (`/updates.json`, 48 changed items). Correctly selected nothing: no story due, and changed stories had been observed < 10 min earlier. |
| Refresh 14:47 | 6 requests: 5 tracked stories listed in `/updates.json` pulled forward and re-observed. |
| Measured momentum | "The Claude Delusion" 40 → 67 points, 14 → 44 comments in 0.22 h = **+123 points/h, +137 comments/h**, `rising`; "Nvidia allegedly engineered scarcity…" +27 points/h, `rising`; "Jonathan Blow – If AI solved coding…" +46 comments/h, `rising`; "Ggml v1.0.0" and "Terminal borders…" `steady`. The other 53 stories show "not measured" (one observation). |
| Live tests (`TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_hackernews_live.py`) | 2 passed: every endpoint; bounded new-stories discovery (≤ 11 requests). |
| Dashboard (API on :8011 against the dev DB, Next.js production build on :3011, basic auth) | `/hackernews` and `/hackernews/50496921` render with no browser errors. Every one of the 136 "search volume" mentions on the page is a negation, with no numeric search claims. Screenshots: `docs/screenshots/hackernews_dashboard.png`, `docs/screenshots/hackernews_story_detail.png`. |

**Corrected during live verification:**
- **Entity links.** The first live run linked some stories to weak generic matches from Phase 2 extraction ("One", "Still", "MS O365" at 0.45 confidence), and others to tools named only in the body (a Black Ops II recompilation linked to "Claude Opus 5.5"). Story → entity links now require a name **in the title** from a trusted extractor: Show HN project hint, gazetteer, or gazetteer version, with the versioned form preferred (`Rust 1.100` over `Rust`). Other extractors need confidence ≥ 0.6. Existing links were re-derived.
- **Code-host weighting.** It was reduced as described in section 2.
- **Planner allocation.** It gave an HN-only run too few slots (only 3 of 5 lists fetched in a dry run), so HN runs are now planned per category with one slot per list.
- **Duplicate observations.** Stories present on two lists in one poll got two identical observations seconds apart (visible in the dev data). New polls no longer do this.

## 7. AI and technology discoveries (live data, by priority)

These are rankings of what to look at, not demand. Points and comments are cumulative at the latest observation.

| Category | Story | Kind | Points / comments | Notes |
|---|---|---|---|---|
| ai | Show HN: Yet another agent manager (but we needed it) | Show HN | 2 / 0 | fresh launch: recency and launch significance dominate |
| ai | The Claude Delusion | story | 67 / 44 | measured rising discussion |
| ai | Nvidia allegedly engineered scarcity by funding AI startups to buy its GPUs | story | 815 / 488 | measured rising discussion |
| ai | Elevated errors on claude.ai, Claude Code, and platform.claude.com | story | 489 / 388 | entity Claude (verified) |
| ai | Gemini 4 Hydrogen / Gemini 4 Hydrogen and Orchestrator | story ×2 | 565 / 350, 501 / 161 | two stories about one entity; corroboration counts publishers outside HN only |
| ai | Show HN: Memro – Persistent Memory for Claude Code, Cursor and Codex | Show HN | 4 / 1 | secondary: technology |
| ai | Show HN: Browserr – An MCP-first browser for AI agents | Show HN | 3 / 0 | ambiguous ai/technology (kept) |
| technology | Rust 1.100 | story | 886 / 316 | release; entity `Rust 1.100` (needs verification: community evidence only) |
| technology | Show HN: Yogurt – Free, private, open-source alternative to Granola | Show HN | 301 / 115 | secondary: ai |
| technology | Show HN: Black Ops II statically recompiled to native ARM64 macOS | Show HN | 150 / 67 | ambiguous technology/gaming |
| technology | Show HN: Agweb, Fursoy, Ryph, Aris (new launches) | Show HN | 2 / 0 each | project names extracted from titles |
| gaming | Show HN: Hexalexagons, a daily word game on a hexagonal grid | Show HN | 19 / 10 | the only primary-gaming story found. None was forced. |

Distribution of the 66 stored stories:
- by category: ai 30, technology 35, gaming 1; 4 ambiguous, 10 with a secondary category;
- by kind: story 33, Show HN 29, Ask HN 4.

## 8. Keyword expansion results

Expansion runs in the existing Phase 2 pipeline from **verified** entities only. Hypotheses are stored as `generated_hypothesis` with their template and evidence links, never as observed queries. The integration test asserts that no HN-derived keyword has origin `observed_query`.

The live run produced **one** hypothesis: "elevated errors on claude.ai, claude code, and platform.claude.com" (title phrase, troubleshooting intent, prioritised). This is the honest result of the existing gates, not a failure:
- **Community evidence alone does not verify an entity.** Show HN projects (Agweb, Yogurt, …) stay `needs_verification` and are not expanded until an independent source (official feed, GitHub repository, gazetteer) corroborates them.
- **Evergreen gazetteer entities** (Claude, Rust, Linux, …) expand only on new releases or events, not on every mention.
- **Versioned names seen only in community text** ("Gemini 4", "Rust 1.100") have confidence 0.45, below the expansion threshold (`trend-agent discover expand --entity "Gemini 4"`: *confidence 0.45 below threshold*). A Hacker News post is not authoritative evidence of a release.

Each story's detail page lists the keyword hypotheses grounded in its item or entity, labelled `KEYWORD_HYPOTHESIS`.

## 9. Scheduling configuration

| What | Where | Default |
|---|---|---|
| Stage `hackernews` | `config/operations.yaml` | every 900 s, timeout 600 s, at most 5 list fetches per run. Gated by `TREND_AGENT_SCHEDULED_DISCOVERY=true` and `TREND_AGENT_HACKERNEWS_API_ENABLED`. Skipped in synthetic mode. |
| List cadence (minimum interval per source) | `config/sources/hackernews.yaml` | new 15 min (≤ 50 stories), show 15 min (≤ 40), top 30 min (≤ 30), ask 60 min (≤ 20), best 120 min (≤ 30) |
| Story refresh tiers | `providers.hackernews_firebase.params.refresh` | high 15 min (priority ≥ 60 or rising), normal 60 min (≥ 35), low 240 min. Stop after 72 h. ≤ 60 item requests per run. |
| Updates hint | refresher | 1 `/updates.json` request per run. Tracked stories in it are pulled forward, but never re-read within 10 min (momentum minimum). Due stories are refreshed regardless, because the feed covers only a short window. |
| Client limits | `params.client` | 20,000 requests/day local budget, concurrency 6, item cache 120 s, list cache 60 s |
| Failure backoff | discovery planner | per source: interval × 2^failures (capped). A failed item refresh is deferred by the normal interval. |

The general `discovery` stage also includes the HN sources (they are ordinary configured sources), so "general scheduled discovery includes HN when enabled". Per-source intervals and the shared cursor prevent double polling.

Worst case at defaults is about 700 requests per hour (each a small JSON GET): new ≈ 204, show ≈ 164, top ≈ 62, ask ≈ 21, best ≈ 15, refresh ≈ 244. Typical runs are far fewer, because incremental lists fetch only new IDs.

## 10. Error handling

| Situation | Behaviour |
|---|---|
| Unknown ID (`null`), deleted, dead, job/comment/poll | skipped with a counted reason. A tracked story that becomes deleted, dead or missing stops being monitored (reason stored). |
| Malformed JSON, wrong shape, missing `id`, > 2 MB | `MalformedResponseError` (schema change) or a per-item `malformed:<field>` skip |
| Timeout, network error, 5xx | retried with jittered backoff, then a typed error. One list failing never stops the others (outage test). The source health shows failures and the last error. |
| Some item requests fail | the other items are kept. The failure is counted per source. The cursor stays below the failed ID so it is retried. A failed refresh is deferred, not retried in a loop. |
| All item requests of a poll fail | `TemporarilyUnavailableError` for that source |
| 429 / access refused / proxy denial | `RateLimitedError` / `AccessRestrictedError` (reason `network_policy` for a proxy refusal). The provider pauses for 15 / 30 min, then retries. |
| Local daily budget reached | `QuotaExhaustedError` before the request is sent; `can_afford` stops planning. |
| HTML / prompt injection in titles or text | stripped, sanitised, withheld or excluded (section 2). The dashboard renders text only. |
| Provider disabled | no requests at all (`DISABLED` status, explicit detail) |

## 11. Tests performed

| Suite | Result |
|---|---|
| HN unit tests (`tests/unit/hackernews`): every list, parsing, null/malformed/unknown fields, skip reasons, empty and null lists, timeouts with backoff, transient recovery, HTTP error mapping, proxy denial, partial failures, oversize, local budget, cache and in-flight dedup, bounded concurrency; classification (categories, no forced gaming, ambiguity, code hosts, kind/subject, project names, entity types, HTML, injection in title and text, constrained assist); momentum (single observation, actual-interval rates, steady and falling, short intervals, frequent polling, duplicates, missing values); priority (all components, reposts, each signal, configurable weights, tiers, stop-after); provider (provenance, incremental cursor, re-read ranked lists, lookback, partial failure retry, outage, expiring access/rate-limit states, disabled, unsupported sources, budget, capabilities and signal class, smoke probe) | **78 passed** |
| HN integration tests (`tests/integration/test_hackernews.py`, PostgreSQL): pipeline and persistence, reposts not independent, keyword hypotheses never observed queries, injection/HTML neutralised, idempotent repeated polling and momentum, one observation per poll for multi-list stories, refresher (updates pull-forward, deleted/dead/missing, failure deferral, stop after 72 h), per-run bound, outage containment and source health, retention, dashboard API (labels, filters, 404/422, no search-volume fields), demo-mode API, scheduler stage gating and per-list cadence, scheduler config, CLI (all commands) | **15 passed** |
| Live (opt-in) `tests/live/test_hackernews_live.py` | **2 passed** |
| Frontend Vitest (incl. 11 new: evidence classes, accessible badges, safe links, `javascript:` URLs, titles never rendered as HTML, totals not growth, measured momentum only, priority components, observations, source health, axe) | **58 passed** |
| Playwright (incl. the new `/hackernews` page: evidence labels, demo state, list filter preserved in the URL, axe, phone width) | **17 passed** |
| Phase 8 ops unit tests (scheduler task list updated) | passed |
| Full default regression suite | **1,144 passed, 0 failed** (section 11a) |
| `ruff check`, `ruff format --check` (src + tests), `mypy src` (287 files), `tsc --noEmit`, `next build` | clean |
| detect-secrets on all changed files | no findings (baseline: line numbers only) |

### 11a. Full regression

Run on commit `47bb127` against a PostgreSQL test database, with no credentials in the environment:

**1,144 passed, 0 failed**, 7 deselected (the opt-in live tests), 15 min 10 s.

The suite was 1,051 tests after the GitHub integration. This work adds 93 Python tests: 78 HN unit tests, 15 HN integration tests, and the updated ops and schema checks. One earlier run was stopped because it had collected the code before two small final changes (the same-poll observation dedup and the smoke probe), so it could not certify the committed code.

## 12. Known limitations

- **No keyword search** in the official API. Search-based discovery stays with the Algolia `hackernews` provider.
- **Classification is lexical.** Titles naming only a product the term lists do not know are dropped as not relevant (e.g. a launch titled only with an unfamiliar brand name).
  - Priority favours fresh launches; a 2-point Show HN can outrank an 800-point story. Weights are configurable heuristics, not validated against outcomes.
  - Stories classified before the code-host change keep their stored classification until they are re-read on a list.
- **Entity links are conservative.** A story whose subject is not named in the title by a trusted extractor gets no entity (33 of 66 live). A title-grounded gazetteer name can still be incidental (e.g. "macOS" for the Black Ops II port).
- **Keyword expansion from HN alone is sparse by design** (section 8).
- **Momentum needs time.** Rates need two observations at least 10 minutes apart, and the 15-minute cadence over many hours was not observed live in this session.
- **The `/updates.json` window is short and undocumented in length.** It is used as a hint only.
- **Points are adjusted by Hacker News** (they can fall). The refresher notes this; it does not correct it.
- **Development data.** The dev database keeps a few duplicate same-poll observations recorded before that fix.

## 13. Exact startup commands

```bash
# database and schema (revision 0011)
docker compose up -d postgres            # or: docker start trend-agent-postgres
trend-agent db upgrade                   # applies 0011_hackernews_integration

# verify and discover
trend-agent hackernews status
trend-agent hackernews test
trend-agent hackernews discover --category ai --category technology
trend-agent hackernews stories --type show
trend-agent hackernews stories --type new --live --limit 10      # read now, store nothing
trend-agent hackernews refresh                                     # after ≥ 10 min
trend-agent hackernews trending
trend-agent hackernews stats

# unattended (scheduler + worker); HN runs every 15 min when enabled
TREND_AGENT_SCHEDULED_DISCOVERY=true trend-agent system scheduler

# dashboard
trend-agent dashboard serve --port 8000
cd frontend && npm run build && TREND_AGENT_API_URL=http://127.0.0.1:8000 \
  DASHBOARD_BASIC_AUTH_USER=<user> DASHBOARD_BASIC_AUTH_PASSWORD=<16+ chars> npx next start -p 3000
# open http://127.0.0.1:3000/hackernews

# tests
pytest tests/unit/hackernews
TREND_AGENT_TEST_DATABASE_URL=postgresql+psycopg://…/trend_agent_test pytest tests/integration/test_hackernews.py
TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_hackernews_live.py
cd frontend && npx vitest run tests/hackernews.test.tsx
```

## 14. Remaining configuration steps

1. **Nothing is required for access.** The API needs no key. `TREND_AGENT_HACKERNEWS_API_ENABLED=true` is the default.
2. Run `trend-agent db upgrade` on every environment, to schema 0011.
3. To poll unattended, set `TREND_AGENT_SCHEDULED_DISCOVERY=true` and run `trend-agent system scheduler`. It is off by default, so nothing polls until an operator enables it.
4. Allow outbound HTTPS to `hacker-news.firebaseio.com` in restricted networks. In this sandbox the first request was refused by the proxy until the environment's network policy allowed it. The client reports that case as `access_restricted` with reason `network_policy`.
5. Optional tuning in `config/discovery.yaml` → `providers.hackernews_firebase.params`:
   - classifier terms and domains per category;
   - priority weights;
   - refresh tiers;
   - momentum thresholds;
   - client budget and concurrency.

   List cadence and slice sizes are in `config/sources/hackernews.yaml`.
6. Optional: plug a `StoryAssist` (e.g. an LLM tie-breaker) into `HackerNewsDiscoveryProvider(assist=…)` for ambiguous stories. It can only choose between the two ambiguous candidates.
