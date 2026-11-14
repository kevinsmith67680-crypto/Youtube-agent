# YouTube Data API v3: connection report

Date: 2026-11-14 (UTC). Environment: the local development stack (Python 3.12 virtualenv,
PostgreSQL 16 in Docker, schema revision 0009) inside a cloud sandbox whose network policy
blocks some non-YouTube hosts (see section 7).

**Result: connected and live-verified.** These endpoints answered real authenticated
requests from the existing provider:

* `videoCategories.list`;
* `search.list`;
* `videos.list` (by ID and `chart=mostPopular`);
* `channels.list`.

Both registered YouTube providers (`youtube_data_api` and `youtube_discovery`) are at
readiness level **READY**. No provider code was rebuilt, and no search-volume capability
was added. One cosmetic CLI fix was made (section 7.3).

## 1. Configuration status

| Item | State |
|---|---|
| Expected variable | `TREND_AGENT_YOUTUBE_API_KEY` (`Settings.youtube_api_key`, a pydantic `SecretStr`; `API_KEY_ENV` in `src/trend_agent/providers/youtube.py`) |
| Template | `.env.example` line 25: `TREND_AGENT_YOUTUBE_API_KEY=` (empty placeholder), plus the quota settings on lines 26–36 |
| Where the key lives | `.env` at the repository root (read by pydantic-settings; also by `docker compose` for `${TREND_AGENT_YOUTUBE_API_KEY:-}` substitution) |
| `TREND_AGENT_YOUTUBE_ENABLED` | `true` |
| Daily unit limit / search-call limit | 10,000 / 100 (`TREND_AGENT_YOUTUBE_DAILY_UNIT_LIMIT`, `TREND_AGENT_YOUTUBE_SEARCH_DAILY_CALL_LIMIT`) |
| Safety margin / reset time zone | 10 % / `America/Los_Angeles` |
| Statistics persistence | `false` (default; statistics are fetched with `videos.list` but withheld) |
| Metadata retention | 30 days (validator caps it at 30) |
| Dependency injection | `build_container` (`src/trend_agent/container.py:359`) registers `YouTubeDataApiProvider.from_settings(...)` in the provider registry. `register_discovery_providers` (`src/trend_agent/discovery/providers/factory.py:60`) wraps that same instance as `youtube_discovery`, sharing its quota manager. |
| Docker Compose | `docker-compose.yml` and `docker-compose.prod.yml` pass the key to the `api`, `scheduler` and `worker` services only. The `frontend` service never receives it. |
| CI | `.github/workflows/ci.yml` does not reference the key. Default tests run without credentials: `tests/conftest.py` strips `TREND_AGENT_*` variables and runs each test outside the repository, so `.env` is never read. |

## 2. Credential detection status (no secret shown)

| Check | Result |
|---|---|
| `Settings()` loads the key | yes: `SecretStr('**********')`; shape check "Google API key format" true |
| `trend-agent doctor` | `youtube_api_key: set` |
| `trend-agent system providers` | `credentials: set` for `youtube_data_api` and `youtube_discovery` |
| `.env` ignored by Git | yes: `.gitignore:2:.env`; `git ls-files .env` is empty |
| `.env` permissions | `600` |
| Key in Git history (`git log --all -p`) | 0 occurrences |
| Key in tracked files (`git grep`) | 0 files |
| Key in `docs/` or logs | 0 files |
| Key in any command output captured during this work | 0 occurrences (every captured output was searched for the key prefix) |
| Key in request URLs | none: sent only in the `X-Goog-Api-Key` header. Stored `source_reference` URLs carry no key, e.g. `https://www.googleapis.com/youtube/v3/videos?chart=mostPopular&part=snippet%2CcontentDetails%2Cstatistics&regionCode=US&videoCategoryId=28` |

## 3. API connectivity results

All times are 2026-11-14 UTC. "Units" means units from the 10,000-unit default pool;
"search" means calls from the separate 100-calls/day search bucket.

| # | Request | Command | Outcome | Cost |
|---|---|---|---|---|
| 1 | `videoCategories.list` regionCode=US (minimal authenticated request) | `trend-agent system smoke-test -p youtube_data_api` | ok, HTTP 200, 391 ms, 11:55 | 1 unit |
| 2 | `search.list` q="AI agents", maxResults=5 | `trend-agent youtube search -q "AI agents" --limit 5 --persist --json` | succeeded, 5 results, 12:04 | 1 search |
| 3 | `videos.list` id=… (5 IDs, part=snippet,contentDetails,statistics) | the ingestion step of #2 (`video_details`) | succeeded, 5 records | 1 unit |
| 4 | `channels.list` (5 channels) | the ingestion step of #2 | succeeded, 5 records | 1 unit |
| 5 | `videos.list` chart=mostPopular, US, category 28 | `trend-agent youtube popular --region US --category 28 --limit 5 --json` | succeeded, 5 videos | 1 unit |
| 6 | discovery probe (`youtube_discovery`, first enabled source: a chart) | `trend-agent system smoke-test -p youtube_discovery` | ok, 398 ms, 12:07 | 1 unit |
| 7 | 2 × `search.list` (discovery) | `trend-agent discover run --all --max-fetches 24` (section 5) | succeeded, 25 items each | 2 search |
| 8 | opt-in live tests: `videoCategories.list`, `videos.list` chart, `search.list` | `TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_youtube_live.py` | 2 passed | 2 units + 1 search (in-memory ledger, see section 6) |
| 9 | invalid key (deliberate) | `TREND_AGENT_YOUTUBE_API_KEY=<fake> trend-agent youtube categories` | rejected as expected: see section 7.1 | none billed |

**`search.list` "AI agents" results** (request #2):

| Rank | Video ID | Published | Duration | Category | Channel | Title (truncated) |
|---|---|---|---|---|---|---|
| 1 | `1BTy5GmdIQA` | 2026-11-13 | PT8M20S | 28 | 零度解说 | 一键部署 24/7 AI Agent！让它自己写代码、开发游戏、自动任务… |
| 2 | `FwOTs4UxQS4` | 2025-04-08 | PT10M9S | 27 | Jeff Su | AI Agents, Clearly Explained |
| 3 | `2hzb21O2seg` | 2026-11-10 | PT26M2S | 24 | Jake One Page | How Complete Beginners Master 95% of AI Agents in 26 minutes |
| 4 | `hvnPWgfmvyI` | 2026-11-12 | PT9M4S | 27 | IBM Technology | Why AI Agents Need RAG + Observability |
| 5 | `nU2a2lm6vDE` | 2026-11-13 | PT1H40M44S | 27 | Stanford Online | Stanford CME295 Transformers & LLMs, Autumn 2026, Lecture 6 … |

* Duration and category come from `videos.list` (`contentDetails`, `snippet`), which
  proves request #3 returned full public metadata.
* `search.list` reported `pageInfo.totalResults` ≈ 1,000,000. The CLI deliberately labels
  this `approx_matching_videos_not_search_volume`: it is a count of matching videos, not
  how often anyone searched.

**`videos.list` chart results** (request #5) contained the full public metadata set:

* `title`, `description`, `channel_id`, `channel_title`, `published_at`;
* `duration_iso`, `duration_seconds`, `category_id`, `tags`;
* `default_language`, `default_audio_language`, `live_broadcast_content`;
* `thumbnails`, `statistics`;
* `measurement_type: video_activity`.

Observation: the chart was requested with `videoCategoryId=28`, but the returned videos
carry `snippet.categoryId` 22, 20, 23 and 27. The provider reports these as returned; the
category of a chart request should not be assumed for every item.

## 4. Provider registration status

| Check | Result |
|---|---|
| Phase 1 registry (`trend-agent providers list`) | `youtube_data_api`, `official_api`, `available`. Capabilities: `channel_metadata`, `trend_discovery`, `video_search`, `video_statistics`; measurement `video_activity` |
| Discovery registry (`trend-agent discover sources`) | `youtube_discovery`, `available`; 79 YouTube sources: 50 seed queries, 23 exploration queries and 3 charts enabled, plus 3 channel sources disabled in `config/sources/monitored_channels.yaml` |
| Ingestion pipeline | `youtube search --persist` runs `IngestionService.ingest_video_search`: search.list → videos.list → channels.list → PostgreSQL (section 3, #2–#4) |
| Discovery pipeline | `discover run` fetched 2 YouTube sources through `youtube_discovery` (section 5) |
| Readiness (`trend-agent system providers`) | `youtube_data_api` **READY**, live-verified 11:55. `youtube_discovery` **READY**, live-verified 12:07. Both `usable_for_real_data: true` |
| Health (`trend-agent system health`) | `live ok`, `database ok` (schema current), `providers ok` (usable real providers: curated_seeds, feeds, youtube_data_api, youtube_discovery). `workers fail` is expected: no scheduler or worker process was started |
| Readiness scorecard (`trend-agent system readiness`) | still **STAGING_READY** (13 PASS, 5 FAIL, 7 BLOCKED, 1 N/A); see below |
| Dashboard API, database mode | `/api/health` → `mode: database`, `synthetic: false`. `/api/system/mode` → `DEGRADED` (base `PROXY_OPPORTUNITY_RESEARCH`), `demo: false`, `direct_search_claims_enabled: false`. `/api/overview` → `synthetic: false` |
| Dashboard API, demo mode | `/api/health` → `mode: demo`, `synthetic: true`. `/api/system/mode` → `DEMONSTRATION`. Every opportunity carries `synthetic: true` |
| Dashboard UI | `ModeBanner` and component tests: 23 passed. The banner shows "Demonstration: Synthetic data only. Nothing shown is live intelligence." only in demo mode |

Readiness scorecard details:

* The `proxy_provider_live` evidence now reads "live-verified: feeds, youtube_data_api,
  youtube_discovery"; before, it listed feeds only.
* No status changed, because the remaining failures are runtime items: no scheduler or
  workers running, dev credentials, no alert channel, no real time-series data yet.
* The blocked items are external: search-interest access, labelled ground truth, the
  compliance review and the deployment.

The live dashboard currently shows 0 opportunities and 0 monitored queries. Discovery
candidates reach the dashboard only after the downstream stages run: proxy monitoring,
content analysis, forecasting and recommendations, all run by `system scheduler` and
`system worker`. No synthetic data is mixed in to fill it.

### Video activity is kept separate from search interest

* The stored descriptor of `youtube_data_api` has `measurement_types: ["video_activity"]`
  and `search_properties: ["not_applicable"]`.
* Its usage restrictions state: "Does not provide search volume or search interest for any
  keyword"; "search.list results are a ranked sample of matching videos, not demand".
* `search_interest_observations` holds 30 rows, all from the `mock` provider and all
  `is_synthetic = true`. No YouTube data was written as search interest.
  `ts_observations` likewise holds only synthetic rows.
* Discovery items from YouTube are stored as `evidence_type = video_title`
  (supply/attention evidence).
* Discovered keywords are labelled `origin: generated_hypothesis`, not measured demand.
* Operating-mode capabilities:
  * `direct_search_surge_claims`, `direct_search_forecasts` and `external_search_signals`
    are **not allowed**;
  * the verified signal classes are `VIDEO_ACTIVITY` and `OFFICIAL_EVENT_SIGNAL` only.

## 5. Real discovery results

The run was bounded: `trend-agent discover run --all --max-fetches 24 --json`, from
12:06:37 to 12:06:41 UTC. Status was **partial**: every YouTube fetch succeeded, and some
non-YouTube feeds were blocked by the sandbox network (section 7.2).

| Category | Run ID | Sources checked / failed | Items received / new | New entities | New candidates | YouTube fetches |
|---|---|---|---|---|---|---|
| AI | `1d69273a-…` | 11 / 1 | 32 / 4 | 5 | 2 | 0 |
| Technology | `4c4438dd-…` | 6 / 1 | 7 / 1 | 1 | 0 | 0 |
| Gaming | `cb6ef416-…` | 7 / 4 | 50 / 50 | 33 | 7 | 2 (`seed:youtube:gaming:best-settings-fps`, `seed:youtube:gaming:fortnite-event`; 25 items each) |

The planner put YouTube sources in the gaming plan only:

* `allocate_slots` / `build_plan` split the 24 slots as AI 11, technology 6, gaming 7.
* Candidates are ranked by reliability × expected yield × staleness.
* In this run, the AI and technology YouTube seeds and the `youtube:chart:US:28` chart
  ranked below package-release feeds, so they were skipped with "category budget
  exhausted". This is normal budget behaviour, not an error.
* Earlier, `trend-agent youtube search -q "AI agents"` exercised YouTube directly for AI
  (section 3).

New gaming candidates (evidence: YouTube video titles; all `synthetic: false`, status
`prioritized`):

| Priority | Query (hypothesis) | Method | Entity | Independent origins |
|---|---|---|---|---|
| 77.65 | Fortnite Chapter 7 what's new | entity_template | Fortnite Chapter 7 | 3 |
| 76.79 | Fortnite Zero Build Live Event Tactical Gameplay #fortnitezerobuild #fnclip #shorts | title_phrase | Fortnite | 28 |
| 76.15 | Fortnite Chapter 7 patch notes | entity_template | Fortnite Chapter 7 | 3 |
| 73.51 | Marvel Rivals Season 11 what's new | entity_template | Marvel Rivals Season 11 | 2 |
| 72.01 | Marvel Rivals Season 11 patch notes | entity_template | Marvel Rivals Season 11 | 2 |
| 71.95 | Here's How To Boost your FPS in Marvel Rivals INSTANTLY | title_phrase | Marvel Rivals | 3 |
| 62.34 | Boost FPS, Fix Stutters & Optimize Counter-Strike 2 | title_phrase | Counter-Strike 2 | 1 |

These are research hypotheses grounded in what creators are publishing. They are not
measurements of what people search for.

### Retention compliance of what was stored

| Table | Rows from YouTube | Retention (expiry − collection) |
|---|---|---|
| `videos` | 5 | 30 days (`metadata_expires_at`) |
| `video_search_results` | 5 | 30 days |
| `channels` | 5 | 30 days |
| `video_statistics` | **0**: 5 received and withheld (`youtube_persist_statistics=false`) | n/a |
| `discovery_items` (`youtube_discovery`) | 50 (`is_synthetic=false`, `evidence_type=video_title`) | 30 days (feeds: 90 days, not YouTube data) |

`trend-agent data retention` reports 0 expired records in every table. `trend-agent
youtube refresh` re-fetches or deletes metadata before expiry, and the scheduler's
`retention` stage purges expired rows daily. No raw API response bodies are stored.

## 6. Quota usage

### Official quotas compared with the configuration

Checked against Google's documentation on 2026-11-14:

| Endpoint | Official | Configured (`youtube_quota_policy`) |
|---|---|---|
| `search.list` | own "Search Queries" bucket: 100 calls/day by default, 1 per call (per page) | `search` bucket, limit 100, cost 1 per page |
| `videos.list`, `channels.list`, `videoCategories.list`, `playlistItems.list` | 1 unit each from the default 10,000 units/day | `default` bucket, limit 10,000, cost 1 each |
| Reset | midnight Pacific Time | `America/Los_Angeles` |

The configuration matches, so no change was needed. Sources:

* [search.list](https://developers.google.com/youtube/v3/docs/search/list)
* [videos.list](https://developers.google.com/youtube/v3/docs/videos/list)
* [Quota calculator](https://developers.google.com/youtube/v3/determine_quota_cost)
* [Getting started](https://developers.google.com/youtube/data/getting-started)
* [Revision history](https://developers.google.com/youtube/v3/revision_history)

### Used today

`trend-agent quota status` (local estimate; resets 2026-11-15 08:00 UTC):

| Bucket | Used | Usable (limit − 10 % margin) | Remaining |
|---|---|---|---|
| search | 3 | 90 | 87 |
| default | 6 | 9,000 | 8,994 |

Breakdown:

* **search 3:** the "AI agents" search (1) and 2 discovery searches.
* **default 6:** `videoCategories.list` smoke test (1), `videos.list` (1), `channels.list`
  (1), the chart (1), the discovery smoke-test chart (1), and 1 unit reserved locally for
  the deliberate invalid-key request. The quota manager reserves before sending and does
  not refund; Google cannot bill a request with an invalid key to this project.

The opt-in live tests use an in-memory ledger, so they add about **1 search call and
2 units** that the database ledger does not show. Google's Cloud Console quota page is
authoritative.

### Daily search-bucket shares by configuration

* discovery: at most 40 requests/day (`config/discovery.yaml`, `youtube_discovery.daily_request_limit`);
* content analysis: at most 40 search requests/day (`config/content_analysis.yaml`, `daily_search_requests`);
* about 10 calls remain for ad-hoc commands within the 90 usable.

## 7. Error details

### 7.1 Error handling (verified)

With a deliberately invalid key supplied through the environment, Google answered
HTTP 400 "API key not valid. Please pass a valid API key."

The provider handled it correctly:

* It raised `InvalidCredentialsError`, and the CLI printed `[invalid_credentials]` and
  exited with code 1.
* It did not retry: invalid credentials are non-retryable. The unit test
  `test_invalid_credentials_are_not_retried` covers this.
* Neither the fake key nor the real key appeared in any output.
* It did not record a failing provider check, so it does not change the READY state.

Other paths are covered by the unit suite and passed today:

* rate limits are retried with backoff;
* server-side quota exhaustion is final and blocks further requests until reset;
* local quota exhaustion stops mid-pagination;
* invalid parameters are permanent;
* the key goes in a header, never in the URL.

### 7.2 Failures in the discovery run (environment, not YouTube)

| Source | Error | Cause |
|---|---|---|
| `feed:blog:google-ai` | `access_restricted`: network policy denied access to blog.google | sandbox network policy |
| `github:microsoft/vscode` | `access_restricted`: HTTP 403 from api.github.com | sandbox network policy (unauthenticated GitHub API) |
| `feed:steam:cs2`, `dota2`, `apex`, `pubg` | `access_restricted`: network policy denied access to store.steampowered.com | sandbox network policy |

On a normal machine or server with open egress, these feeds are expected to work. They
were classified correctly as `access_restricted`, and the run continued.

### 7.3 Defect found and fixed

`trend-agent discover sources` displayed `youtube:chart:US:20` as `youtube💹US:20`
because Rich converts `:chart:` to an emoji. The CLI consoles now use `emoji=False`
(`src/trend_agent/cli.py`). A regression test was added:
`test_console_prints_source_keys_verbatim` in `tests/unit/test_cli_basic.py`.

### 7.4 Observations, not fixed (outside this task's scope)

* **Raw titles become candidates.** The `title_phrase` candidates keep hashtags and full
  titles, for example "Fortnite Zero Build Live Event Tactical Gameplay #fortnitezerobuild
  #fnclip #shorts". Hashtag stripping and length limits for title-phrase hypotheses would
  improve candidate quality.
* **Stale source health.** `discover sources` still shows `not_configured` for YouTube
  sources that have not been fetched since the key was added. It is the last recorded
  state and updates on each source's first fetch. The planner's ranking does not use this
  field.
* **Chart category mismatch:** see section 3.

### 7.5 Tests

| Suite | Result |
|---|---|
| Unit: `test_cli_basic`, `test_youtube_provider`, `test_quota_retry`, `tests/unit/discovery` | 176 passed |
| Integration (PostgreSQL test database): `test_ingestion`, `test_discovery`, `test_cli`, `test_ops`, `test_ops_cli`, plus the 40 E2E scenarios | 94 passed |
| Live (opt-in, real key): `tests/live/test_youtube_live.py` | 2 passed |
| Frontend (Vitest): `modeBanner`, `components` | 23 passed |
| Full default regression suite (PostgreSQL test database, no credentials) | **970 passed**, 3 deselected (the opt-in `live` tests), 0 failed, 15 min 11 s |
| `ruff check`, `ruff format --check`, `mypy` on changed files | clean |

## 8. Remaining actions required

1. **Schedule discovery.** The Phase 8 scheduler's stages are:
   * `ops_checks`, `monitor_plan`, `content_analysis`;
   * `forecasting`, `recommendations`, `retention`.

   It does **not** run discovery. Run `trend-agent discover run --all` periodically from
   cron or a systemd timer (section 9). Each run is bounded by `--max-fetches` (default
   24), the 40/day YouTube discovery cap, per-source polling intervals and the quota
   manager.
2. **Start the downstream stages** so candidates reach the dashboard: `trend-agent system
   scheduler` and `trend-agent system worker` (or the compose `app` profile). Until then,
   the live dashboard is empty, and honestly so.
3. **Secure the key in Google Cloud Console** (owner action):
   * Restrict it to the YouTube Data API v3 and, where possible, to the server's IP
     addresses.
   * The key was shared in a chat conversation, so rotating it and updating `.env` is
     prudent.
4. **YouTube API Services compliance review** before showing YouTube-derived data to
   anyone but the operator, or enabling statistics persistence. See
   `docs/api_compliance_audit.md`; the readiness item stays BLOCKED until a human records
   it.
5. **Network access for the other discovery feeds** (only if you run the agent in this
   cloud sandbox): allow `store.steampowered.com`, `blog.google` and `api.github.com`,
   preferably with a GitHub token. On an ordinary host nothing is needed.
6. **Watch YouTube's share of discovery across runs.** In this run, the planner chose
   YouTube only for gaming. The package feeds polled in this run should become less stale
   than the never-fetched YouTube seeds, so the seeds are expected to rotate in on later
   runs; that is not yet observed. To favour YouTube now:
   * run `discover run -c ai` or `-c technology` separately; or
   * raise the chart sources' `reliability` (0.6) in `config/sources/youtube.yaml`.

   Seed-query reliability is fixed at 0.6 in `discovery/service.py`.
7. **Not verified live:** `playlistItems.list`. Channel sources are disabled in
   `config/sources/monitored_channels.yaml`; enabling them costs 2 units per channel per
   fetch.
8. **Search interest is still unavailable.** YouTube Data API v3 has no search-volume
   endpoint. Google Trends API (alpha) access remains the only legitimate route to an
   external search-interest signal. Direct YouTube Search claims stay disabled.

## 9. How to collect live discovery data

One bounded run now, using the key in `.env`:

```bash
.venv/bin/trend-agent discover run --all
```

To keep collecting, run it on a schedule. Example crontab line, hourly; the run's own
limits keep it within quota:

```cron
17 * * * * cd /path/to/Youtube-agent && .venv/bin/trend-agent discover run --all --json >> discovery.jsonl 2>> discovery.err
```

Then run the downstream stages that feed the dashboard, in two terminals:

```bash
.venv/bin/trend-agent system scheduler
.venv/bin/trend-agent system worker
```

Alternatively, `docker compose --profile app up -d --build` starts the API, scheduler,
worker and frontend with the key from `.env`. Discovery still needs the cron entry above,
or `docker compose run --rm worker discover run --all` on a schedule.
