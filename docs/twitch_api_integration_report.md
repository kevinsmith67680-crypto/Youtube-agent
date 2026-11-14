# Twitch Helix API integration report

Date: 2026-11-14 (UTC). Branch `claude/tender-thompson-620dmi`, schema revision **0012**.

## Implementation summary

The agent now monitors Twitch through the official Helix API. Authentication uses an **app access token** obtained with OAuth 2.0 client credentials. Only the public endpoints are used: no user authorisation is requested and nothing is written to Twitch.

What it monitors:
- **Top-game rankings** (`GET /helix/games/top`). These are positions only, because the endpoint returns no viewer numbers and none are invented.
- **Live streams and concurrent viewers** (`GET /helix/streams`). Every collection records whether its sample is complete, partial or unknown.
- **Game lookups** by ID or exact name (`GET /helix/games`).

How it works:
- **Attention detector (`twitch-attention-v1`):** source-specific, versioned and interpretable. It turns repeated *comparable* observations into ranking changes, observed-viewer changes over actual intervals, acceleration, volatility and sustained increases. Sustained increases reuse the Phase 4 CUSUM primitive under Twitch-specific labels.
- **Discovery hand-off:** emerging games are re-verified through Get Games, resolved against existing entities, and sent to the Phase 2 discovery pipeline. Phase 2 generates keyword hypotheses from its existing game templates.
- **Downstream links:** the dashboard and CLI join those hypotheses to Phase 5 content gaps and opportunities, Phase 7 recommendations, and Phase 6 forecasts of the keyword series, read-only.

Interfaces:
- a scheduler stage with per-collection cadence;
- the `trend-agent twitch …` command group;
- six read-only API endpoints;
- a `/twitch` dashboard section with filters.

Twitch data is classified as **livestream activity / external gaming attention** (`LIVESTREAM_ACTIVITY`). It is never YouTube search interest or search volume, never feeds a YouTube Search surge, and never uses Phase 4's search-surge labels.

**Data-policy constraint built in.** The Twitch Developer Services Agreement says: *"Do not store copies of Twitch Content or Program Materials, unless you: (a) obtain prior written authorization from Twitch …; (b) control the rights …; or (c) cache such information for only a twenty-four hour time period without further sharing it with third parties."* Accordingly:
- Every stored Twitch record expires after **24 hours**. That includes Twitch-sourced discovery items.
- The Twitch stage purges expired rows on every 5-minute cycle, so expiry is not left only to the daily retention job.
- Retention longer than 24 hours is refused by configuration unless `retention.extended_storage_authorized: true` asserts that written authorisation exists.
- Live dashboard screenshots and live rankings are **not** committed to this repository for the same reason. This report gives counts, statuses and mechanism results only.

**Decision for the owner:** Section VI (Data Policy) of the same agreement covers data from the Twitch API *including any insights derived from that data*. Section VI.B allows Twitch Data to be used only for: improving the end-user experience; administrative communications; periodic promotional communications to end users; processing transactions; or limited purposes an end user permitted *and* Twitch approved in writing. Section VI.C forbids selling, licensing, monetising or sharing those insights with third parties without Twitch's written permission.

Planning YouTube content from Twitch attention is not obviously one of the listed purposes. Whether it is permitted is a legal judgement this implementation cannot make. Please review the agreement, or get Twitch's written permission, before production or commercial use. The provider's approval note in `trend-agent system providers` says so.

## Audit and integration decisions

| Area | Finding | Decision |
|---|---|---|
| Provider registry / Phase 2 | `DiscoveryProvider` protocol; source state; observer hook; GitHub's `repository_metadata` evidence verifies only its own source-hint name | New `twitch` provider. New `game_catalog` evidence type: a catalog record verifies the game's name only. |
| Phase 3 history | Generic `ts_series` categories. `VIDEO_ACTIVITY` is treated as YouTube-sourced (monitoring collectors) and feeds YouTube-specific forecast features (`video_change_24h_log`). | Twitch is **not** written into the generic series (it would contaminate Phase 6 features). Dedicated snapshot and observation tables with stable Twitch IDs, completeness and expiry. |
| Phase 4 | Surge classification is decided by the Phase 3 category; CUSUM, baseline and statistics primitives are reusable | Source-specific detector reusing the CUSUM primitive; Twitch labels only (rising, sustained_rise, steady, falling, insufficient_observations, incomparable_samples). |
| Evidence | `EvidenceType` → `EvidenceKind` → trust; injection heuristics | `game_catalog` added everywhere (extraction trust, quality, authority order, verification kind, 24 h verification max age, Phase 4 corroborating evidence). Stream titles are sanitised and instruction-like titles withheld. |
| Phases 5–7 | Link by keyword → `query_id` | Read-only joins from the game's entity. Phase 6 forecasts are shown with their own classification; Twitch is never a forecast input. |
| Settings | pydantic-settings with aliases | `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` (both `SecretStr`), `TWITCH_API_ENABLED`, plus `TREND_AGENT_TWITCH_*` aliases. Status shows set/missing only. |
| Baseline tests | 1,144 passed on the previous commit | Regression results are in the tests section. |

## Files changed

New:
- `src/trend_agent/twitch/`:
  - `models.py`: records, definitions, evidence classes, `category_kind`
  - `config.py`: settings and the policy validator
  - `auth.py`: app token manager
  - `client.py`: Helix client
  - `attention.py`: detector
  - `service.py`: monitor, promotion, connection test, read models
- `src/trend_agent/discovery/providers/twitch.py`
- `src/trend_agent/db/twitch_models.py`, `src/trend_agent/db/repositories/twitch.py`
- `migrations/versions/20261114_0012_twitch_integration.py`
- `src/trend_agent/cli_twitch.py`
- `frontend/lib/twitch.ts`, `frontend/components/TwitchViews.tsx`, `frontend/app/twitch/page.tsx`, `frontend/app/twitch/[id]/page.tsx`
- Tests:
  - `tests/twitch_fixtures.py`
  - `tests/unit/twitch/test_auth_client.py`, `tests/unit/twitch/test_attention_provider.py`
  - `tests/integration/test_twitch.py`
  - `tests/live/test_twitch_live.py`
  - `frontend/tests/twitch.test.tsx`
- `docs/twitch_api_integration_report.md`

Modified:
- `config.py`: settings and summary.
- `logging.py`: redacts `client_secret=` and `OAuth <token>`.
- `schemas/enums.py`: 7 capabilities.
- `ops/signals.py`: `LIVESTREAM_ACTIVITY` (a proxy class).
- `ops/providers.py`: credentials, approval and policy notes, smoke probe (1 Helix request).
- `discovery/models.py`, `discovery/extraction.py`, `discovery/evidence.py`, `discovery/service.py`: `game_catalog`.
- `schemas/verification.py`, `verification/sources.py`, `verification/config.py`: `game_catalog`.
- `detection/config.py`: corroborating evidence.
- `discovery/providers/factory.py`: registration.
- `container.py`: `twitch_settings()` and `twitch_monitor()`.
- `ingestion/retention.py`: Twitch purge.
- `ops/pipeline.py`, `ops/config.py`, `config/operations.yaml`: stage `twitch`.
- `api/app.py`: endpoints.
- `cli.py`: command group.
- `db/__init__.py`: expected revision 0012.
- `db/models.py`: model registration.
- `config/discovery.yaml`: `providers.twitch`.
- Frontend: Nav link, Playwright spec (including phone width).
- `.env.example`: empty placeholders only.
- `README.md`, `docs/discovery_configuration.md`.
- Tests pinned to the revision, tables and scheduler tasks; `tests/conftest.py` isolates `TWITCH_*` variables.
- `.secrets.baseline`: line numbers only.

## Authentication status

- **Credentials:** stored only in the git-ignored `.env` (mode 600). They are never printed, logged, stored in the database or sent to the browser. Status output says `set` or `missing`.
- **Token request:** `POST https://id.twitch.tv/oauth2/token` with `client_id`, `client_secret` and `grant_type=client_credentials` in the **form body**, never in the URL.
- **Token handling:**
  - kept in process memory as a `SecretStr`; never persisted, and its `repr` hides it;
  - replaced one hour before expiry (no refresh token exists for this flow);
  - validated at start-up and hourly via `GET /oauth2/validate` (`Authorization: OAuth …`), as Twitch requires; the client ID in the validation response is checked against ours.
- **Recovery:**
  - a 401 from Helix drops the token and retries once with a new one; a second 401 is `InvalidCredentialsError`;
  - a token-endpoint 400/401/403 is `InvalidCredentialsError`: never retried, response body not echoed, provider `access_restricted` for a 30-minute cooldown;
  - token-endpoint 5xx and network errors are retried with backoff.
- **Live status:** a genuine app access token was obtained with your credentials (lifetime about 66 days at issue). `/validate` returned valid, and the rate-limit bucket reported 800 points.

## API connection results (live, 2026-11-14; snapshots collected 15:58–16:15 UTC)

| Step | Result |
|---|---|
| 1. App access token | obtained; validated (`valid`); token value never shown |
| 2. Top 20 games | 20 games, ranks 1–20, sample `complete`. Non-game categories (e.g. "Just Chatting") have no IGDB ID and were classified `non_game` / `unknown`, never `game`. |
| 3. Bounded live streams | 20 streams (1 page, `partial` by design); later cycles read 3 pages = 300 streams across 92 categories (`partial`, page limit) |
| 4. Resolve Fortnite | `GET /helix/games?name=Fortnite` → Twitch ID 33214, IGDB 1905 (the same values Twitch's documentation example shows) |
| 5. Fortnite streams | 1 page of 20 in the connection test; the monitored-game poll read 5 pages: 465 distinct streams after **33 duplicates were removed** (the list re-orders while it is read, as Twitch documents), sample `partial` |
| 6. Observations recorded | 6 snapshots (top games 2 × `complete`; top streams 2 × `partial`; Fortnite 2 × `partial`); 200 rank observations, 186 observed-viewer rows, 1,510 stream rows, 124 games or categories (86 with an IGDB ID; 84 classified `game`, 13 `non_game`, 27 `unknown`); every row expires 24 h after collection |
| 7. Persistence | verified in PostgreSQL (counts above); the second top-games poll produced real rank changes (one game climbed 32 positions in 17 minutes and was labelled `rising` and auto-monitored); Fortnite's two comparable 5-page samples gave a measured viewer change labelled `PARTIAL_SAMPLE` with the comparison "both partial with the same cap" |
| 8. Provider registration | `trend-agent twitch status`: provider `twitch` registered, `available`, credentials `set`, 8 capabilities; the Phase 8 smoke probe spends 1 Helix request |
| 9. Dashboard | API (port 8011) and the production Next.js build (port 3011) against the dev DB: `/twitch` and `/twitch/33214` rendered with no browser errors, labels present, the only "search volume" mentions negations, no credential in the page; screenshots reviewed but deliberately **not committed** (24-hour cache limit) |
| 10. Classification | every Twitch metric carries `LIVESTREAM_ACTIVITY` / `TWITCH_ACTIVITY` labels (plus `OBSERVED_VIEWERS`, `PARTIAL_SAMPLE`, `RANK_CHANGE` where applicable); no Phase 3 series and no search-interest observations were created (integration test) |
| Discovery hand-off | `trend-agent twitch promote --game "Delta Force"`: Twitch ID re-verified; new `game_catalog` item (expires in 1 day); entity created **verified** (catalog source hint), not evergreen; 4 keyword **hypotheses** from Phase 2 game templates (gameplay, beginner guide, how to play, review); "best settings" correctly not generated (no grounded performance attribute) |
| Entity resolution | exact-name matching linked Fortnite, Marvel Rivals and ROBLOX to their existing verified (evergreen) gazetteer entities; evergreen entities expand only on new releases or events |

Requests used in validation: the local budget recorded **26 Helix requests** for the day. The six snapshots used 18 of them (top games 2, top streams 6, Fortnite 10); connection tests, lookups and the promotion used the rest. The opt-in live test used about 5 more under its own in-memory budget. Each CLI process requested one token, held in memory only. Every recorded snapshot saw 799 of 800 rate-limit points remaining.

## Supported endpoints

| Endpoint | Use | Parameters used |
|---|---|---|
| `POST https://id.twitch.tv/oauth2/token` | app access token | `client_id`, `client_secret`, `grant_type=client_credentials` (form body) |
| `GET https://id.twitch.tv/oauth2/validate` | hourly token validation | `Authorization: OAuth <token>` |
| `GET /helix/games/top` | rankings | `first` ≤ 100, `after` |
| `GET /helix/games` | lookup by ID, exact name or IGDB ID | ≤ 100 values per request |
| `GET /helix/streams` | top live streams; streams per game | `first` ≤ 100, `after`, `type=live`, `game_id` (≤ 100), `language` (≤ 100) |

Missing or invalid fields (e.g. no `user_login`, a negative `viewer_count`, `type` not `live`, non-objects) are skipped and counted, never guessed. Empty `game_id` and `igdb_id` strings are treated as absent.

## Database migration 0012

Schema revision `0012`. Upgrade, downgrade and upgrade were verified; `alembic check` reports no drift.

New tables:
- **`twitch_snapshots`:** one row per collection: kind, game, languages, pages requested and fetched, items, duplicates, skipped reasons, `sample_status` (complete / partial / unknown) with its reason, viewer sum and stream count (streams only), requests, rate-limit remaining, trigger, `expires_at`.
- **`twitch_games`:** keyed by Twitch game ID: name, IGDB ID, `category_kind`, latest and best rank, latest own-stream sample, attention JSON, state, score and version, entity and item links, `promoted_at`, `expires_at`.
- **`twitch_rank_observations`:** unique (snapshot, game).
- **`twitch_viewer_observations`:** unique (snapshot, game); status, basis, languages and cap per row. Rows exist **only for games a snapshot covered**; a missing game is unknown, never zero.
- **`twitch_stream_observations`:** unique (snapshot, stream); sanitised, truncated titles.
- **`twitch_monitored_games`:** game ID, the operator's own query text, tier, origin (manual / config / auto) and schedule. Auto-selections expire after 24 hours. Manual and configured selections are configuration, not Twitch data, and stay.

Enum widening: the `game_catalog` value is added to the `discovery_items` evidence and simulated types, `discovery_evidence.evidence_type` and `evidence_records.evidence_kind`. The downgrade refuses to run while any row uses it.

## Monitoring configuration

| Setting | Default | Where |
|---|---|---|
| Scheduler stage `twitch` | every 300 s, timeout 240 s, ≤ 12 monitored-game polls per tick; gated by `TREND_AGENT_SCHEDULED_DISCOVERY=true`, `TWITCH_API_ENABLED` and credentials; skipped in synthetic mode | `config/operations.yaml` |
| Top games | every **15 min**, top 100 (1 request) | `providers.twitch.params.cadence/collection` |
| Top streams | every **10 min**, 3 pages × 100 (`partial` by design), optional `languages` filter | same |
| Selected games (high tier) | every **10 min**, up to 5 pages × 100 of the game's own streams | same; configured `monitor.games: [Fortnite]` and `trend-agent twitch monitor` |
| Lower-priority games | every **45 min** (auto-selected emerging games, 24 h) | `cadence.low_priority_game_minutes` (configurable 30–60) |
| Attention | minimum interval 9 min; rising ≥ 15 %/h or ≥ 5 places climbed; CUSUM k 0.5 / h 3.0 / clip 2.5 over ≥ 4 intervals; weights in `attention.weights` | `providers.twitch.params.attention` |
| Promotion to discovery | score ≥ 45, IGDB-known games only, ≤ 3 per run, 24 h cooldown | `promotion` |
| Retention | 24 h (policy limit); `extended_storage_authorized` must be true to exceed it | `retention` |

The detector compares viewer samples only when basis, languages, page cap and completeness all match. Viewer momentum uses only a game's **own** stream list.

During live validation the first version also used each game's share of the global top-streams sample. A game whose single stream entered the top 300 looked like it had grown 200 %, which flagged 11 games as rising. That share depends on other games' streams, so it is now stored and shown but not used as growth; game-level candidates come from Twitch's own ranking instead. A regression test covers this.

## Source provenance

Every snapshot records:
- what was requested (kind, game, languages, pages, page size) and what came back (pages fetched, items, duplicates removed, skipped records with reasons);
- the completeness status and reason (`cursor_exhausted`, `requested_count_read`, `page_limit_reached`, `error_after_N_pages:<type>`, `pagination_missing_or_malformed`);
- request count, rate-limit remaining, trigger and collection time.

Observation rows point to their snapshot. Attention results carry the detector version, every component (value, weight, contribution, basis), the comparison used, and notes such as "outside the polled top-N in the latest ranking (position unknown)".

Promoted games keep their `game_catalog` discovery item, including `verified_via: GET /helix/games?id=` and the attention summary that triggered promotion. Possible explanations are official discovery items mentioning the entity within seven days. They are labelled "temporal co-occurrence: a possible explanation; causation is not established"; nothing about patches, events or features is inferred from Twitch.

## Live versus synthetic results

- **Live:** everything in the API connection results above. The data lives in the dev DB, expires within 24 hours, and was not copied into this repository.
- **Synthetic / faked:** every automated test (`FakeTwitch` serves the token, validate and Helix endpoints from memory). No test touches the network. The opt-in live test (`tests/live/test_twitch_live.py`) passed against the real API in this session.
- **Not run live:** the scheduler stage over many hours; a promotion triggered automatically by the score threshold (promotion was exercised live with `twitch promote`, and automatically in tests).

## Rate-limit handling

- `Ratelimit-Limit`, `Ratelimit-Remaining` and `Ratelimit-Reset` are tracked from every response.
- At or below the reserve (20 points), the client waits for the reset if it is within 15 s, otherwise it stops with `RateLimitedError`.
- A 429 is reported with the time to reset and retried only when that is within the retry policy.
- Transient 5xx and network errors are retried with jittered exponential backoff; timeouts are typed.
- A local daily budget (8,000 requests) caps usage on top of Twitch's bucket.
- Pagination is bounded per collection.
- A monitored game whose poll fails is retried after the longer of its own cadence and the low-priority cadence (45 min), never in a tight loop.
- `trend-agent twitch quota --live` shows both the local budget and Twitch's bucket.

## Dashboard changes

**Nav:** new "Twitch" item.

**`/twitch` page:**
- provider status and per-collection data freshness;
- counts: games, rising, monitored, ranking observations ("positions, not viewers");
- the retention policy;
- an evidence-class legend: `TWITCH_ACTIVITY`, `OBSERVED_VIEWERS`, `PARTIAL_SAMPLE`, `RANK_CHANGE`, `PROXY_OPPORTUNITY`, `DIRECT_YOUTUBE_SEARCH_INTEREST`;
- filters: game, ranking, observation period, attention status, stream language;
- tables: top games (rank, change since the previous poll, observed viewers with sample badge, attention state and score, evidence labels), attention momentum, emerging gaming opportunities, active streams.

**`/twitch/[id]` page:**
- current observations with sample status and comparison basis;
- the attention score components;
- ranking history, including how many polls the game was outside the top-N;
- observed-viewer history with status, basis and cap;
- the entity, possible explanations, keyword hypotheses and candidate video formats (with what each format still needs);
- linked Phase 5, 6 and 7 results;
- a "YouTube Search interest" card with the `DIRECT_YOUTUBE_SEARCH_INTEREST` label that reads "not measured" unless a live-verified direct YouTube Search provider has data, and says Twitch activity is never used as that signal.

Everything is shown as tables of stored observations; no chart is drawn from data that does not exist. Stream titles render as text, and channel links are checked as safe URLs.

## Tests passed or failed

| Suite | Result |
|---|---|
| Twitch unit tests (`tests/unit/twitch`): token request and form-body secret, caching, refresh before expiry, hourly validation, revoked-token replacement, Helix 401 renewal, invalid / missing credentials, token-endpoint outage, redaction; top games across pages (no viewer numbers), lookup by name and ID, stream filters, pagination, duplicates, partial and unknown samples, missing fields, rate-limit headers and reserve, 429, typed errors, retries, local budget; attention (one observation never growth, actual intervals, incomparable samples, partial labels, short intervals, falling and steady, rank climb, absence ≠ zero, new in top needs observed absence, CUSUM sustained rise, bounded versioned components); category rules; record validation; retention policy enforcement; provider items, exclusions, status, signal class, smoke probe | **42 passed** |
| Twitch integration tests (`tests/integration/test_twitch.py`, PostgreSQL): cycle and completeness, no zero rows for uncovered games, cadence and idempotency, comparable-sample momentum, partial vs complete incomparable, stream endings, category change mid-collection, promotion into Phase 2 (verified entity, hypotheses only, grounded formats), possible explanations (co-occurrence only), 24 h purge including discovery items, retention service, no secret or token in any table and no search-interest data, untrusted stream titles, dashboard API (labels, filters, 404/422, no secrets), demo-mode API, scheduler gating, scheduler config, the full CLI, the global-sample noise regression, entity resolution by exact name | **17 passed** |
| Live (opt-in) `tests/live/test_twitch_live.py` | **1 passed** against the real API (deselected by default) |
| Frontend Vitest (incl. 10 Twitch: six classes, accessible badges, partial-sample labels, outside top-N never shown as zero, safe links, titles as text, histories with basis, components, grounded formats, proxy opportunities) | **68 passed** |
| Playwright (incl. `/twitch`: labels, demo state, filters preserved, axe, phone width) | **18 passed** |
| `ruff check`, `ruff format --check` (src, tests, migrations), `mypy src` (298 files), `tsc --noEmit`, `next build` | clean |
| detect-secrets on all changed files | clean (three false positives marked inline: a set/missing label, its test, a fake fixture value) |
| Full default regression suite | **1,203 passed, 0 failed**, 8 deselected (opt-in live tests) |

### Full regression

`pytest -q` against a fresh PostgreSQL test database, with no Twitch, GitHub or other live credentials in the environment: **1,203 passed, 0 failed, 8 deselected** (the opt-in live tests) in 16 min 4 s. That is the previous baseline of 1,144 plus the 59 new Twitch tests; no existing test was changed except those pinning the schema revision, table list and scheduler task list. The run used the final tree except for one later change to `twitch status`, which now shows catalog names for auto-selected games. The 59 Twitch tests were re-run after that change and passed, and ruff, ruff format and mypy are clean.

## Known limitations

- **No time-of-day baseline.** With 24-hour retention, the detector cannot learn daily seasonality, so evening growth can look like attention growth. It compares only within the retained window and labels every number as livestream activity.
- **Partial samples are the norm for large games.** Fortnite has more than 500 live streams, so 5 pages is a top-500 sample. Partial samples are compared only with partial samples of the same cap ("top-N vs top-N"), never with complete ones.
- **Ranking granularity.** Get Top Games returns positions only, and a game outside the polled top 100 has an unknown position.
- **Promotion is conservative.** The default score threshold (45) means rank-only climbers (score about 25) are auto-monitored but not promoted until their own stream samples show growth. `trend-agent twitch promote` promotes explicitly.
- **Explanations are only as good as the official sources.** Possible explanations come from official items already collected (feeds and similar). Twitch itself provides no cause, and none is inferred.
- **Data-use permission is unresolved.** See the decision for the owner in the implementation summary.
- **Short-lived processes and tokens.** Each CLI process obtains its own app token (held in memory only). The long-running scheduler reuses one.

## Exact startup commands

```bash
# credentials (git-ignored .env): TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_API_ENABLED=true
docker compose up -d postgres            # or: docker start trend-agent-postgres
trend-agent db upgrade                   # applies 0012_twitch_integration

trend-agent twitch status
trend-agent twitch test                  # token, top 20 games, streams, Fortnite (about 5 requests)
trend-agent twitch top-games
trend-agent twitch streams --limit 100
trend-agent twitch game --name "Fortnite"
trend-agent twitch monitor --game "Fortnite"
trend-agent twitch trending
trend-agent twitch history --game "Fortnite"
trend-agent twitch quota --live
trend-agent twitch cycle                 # one scheduler cycle now (purge, polls, attention, promotion)
trend-agent twitch promote --game "Delta Force"

# unattended: the scheduler runs the twitch stage every 5 minutes
TREND_AGENT_SCHEDULED_DISCOVERY=true trend-agent system scheduler

# dashboard
trend-agent dashboard serve --port 8000
cd frontend && npm run build && TREND_AGENT_API_URL=http://127.0.0.1:8000 \
  DASHBOARD_BASIC_AUTH_USER=<user> DASHBOARD_BASIC_AUTH_PASSWORD=<16+ chars> npx next start -p 3000
# open http://127.0.0.1:3000/twitch

# tests
pytest tests/unit/twitch
TREND_AGENT_TEST_DATABASE_URL=postgresql+psycopg://…/trend_agent_test pytest tests/integration/test_twitch.py
TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_twitch_live.py   # needs TWITCH_* in the environment
```

## Remaining configuration requirements

1. **Rotate the client secret.** It was shared in a chat message, so treat it as exposed: rotate it in the Twitch developer console and update `.env`. The integration needs no code change.
2. **Confirm the permitted use.** Check that using Twitch Data and derived insights to plan YouTube content is a permitted purpose under the Developer Services Agreement (Section VI.B), or get Twitch's written permission. Do not share Twitch-derived insights with third parties (Section VI.C). Keep `retention.hours: 24` unless Twitch authorises longer storage in writing; then set `extended_storage_authorized: true` and the new `hours`.
3. Run `trend-agent db upgrade` (schema 0012) in every environment.
   With Docker Compose, both `docker-compose.yml` and `docker-compose.prod.yml` pass `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` and `TWITCH_API_ENABLED` from `.env` (or the production env file) to the backend services.
4. To poll unattended, set `TREND_AGENT_SCHEDULED_DISCOVERY=true` and run `trend-agent system scheduler`. The stage also purges Twitch data past 24 hours on every cycle; without the scheduler, run `trend-agent twitch cycle` or `trend-agent data retention --purge` regularly so cached data does not outlive the limit.
5. In restricted networks, allow `id.twitch.tv` and `api.twitch.tv`. In this session the documentation host `dev.twitch.tv` was not reachable from the web-fetch tool, so the documentation was read through the container's own network.
6. Optional: monitored games (`monitor.games`), languages for top streams, cadence (30–60 minutes for the low tier), attention weights and thresholds, and promotion thresholds, all in `config/discovery.yaml` → `providers.twitch.params`.
