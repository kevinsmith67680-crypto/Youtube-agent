# Dashboard API

FastAPI app (`trend_agent.api.app.create_app`), served by `trend-agent dashboard serve`
(database) or `trend-agent dashboard serve --demo` (isolated synthetic demonstration).
OpenAPI: `/api/openapi.json`, interactive docs: `/api/docs`.

## Authentication and authorisation

* One bearer token: `TREND_AGENT_API_TOKEN` (compare in constant time). It is meant for the
  dashboard's server-side proxy, not for browsers.
* **Token configured**: every endpoint except `/api/health` requires
  `Authorization: Bearer <token>` (401 otherwise, with `WWW-Authenticate: Bearer`).
* **No token configured** (local read-only view): public reads work with every
  user-specific field removed (saved/dismissed flags, production status, notes, feedback);
  private data (`/saved`, `/profile`, `/alerts`) and **every mutation** answer 401.
* View tracking (`?track_view=true`) is only recorded for authenticated requests.

## Limits and hardening

| Control | Value |
|---|---|
| general rate limit per client (token or address) | `api.rate_limit_per_minute` = 240 → 429 + `Retry-After` |
| generation rate limit | `api.generate_rate_limit_per_hour` = 12 |
| opportunities per generation request | `api.max_generate_opportunities` = 10 |
| page size | default 25, maximum 100 (422 above) |
| request body | 64 KB (413 above) |
| CORS | only origins in `TREND_AGENT_API_CORS_ORIGINS` (`*` is ignored); no credentials; GET/POST/PUT/DELETE; `Authorization`, `Content-Type` |
| headers | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` |
| input validation | path IDs (UUID / key patterns), enumerated sorts, categories and orders, bounded numbers and text (120 chars), strict bodies (unknown fields rejected) |

## Errors

Every error has one shape: `{"error": "<code>", "detail": <message or field list>, "status": n}`.
Codes: `unauthorized` 401, `forbidden` 403, `not_found` 404, `invalid_transition` 409
(production status), `payload_too_large` 413, `invalid_request` 422 (field locations and
messages; submitted values are not echoed), `rate_limited` 429, `internal_error` 500 (no
internal details), `unavailable` 503.

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | none | mode (`database`/`demo`), synthetic flag, version, auth mode |
| GET | `/api/overview` | read | counts, top ideas, recent topics, providers, freshness, category distribution, notes |
| GET | `/api/opportunities` | read | filters `category, region, intent, surge, status, verification, min_score, max_score, max_age_hours, q`; `sort=score|updated|severity|query|breakout`; `order`; `page`, `page_size` |
| GET | `/api/opportunities/{id}` | read | detail: demand, gaps, requirements, inspected videos, forecasts, ideas, claims |
| GET | `/api/gaps` | read | `category, gap_type, strength, primary_only, include_unverifiable, q, sort=score|strength|query|updated, order, page, page_size` |
| GET | `/api/trends` | read | `category, q, page, page_size` |
| GET | `/api/trends/{id}` | read | per-measurement series (data class, definition, points), surge episodes, forecasts, evidence, limitations |
| GET | `/api/forecasts/{prediction_id}` | read | one forecast with trajectory, interval, peak and windows |
| GET | `/api/recommendations` | read | `category, status, urgency, min_score, q, include_research, include_suppressed, include_dismissed, sort=score|rank|hours|generated|title, order, page, page_size` |
| GET | `/api/recommendations/{id}` | read | full `recommendation/1.0` document, user state, opportunity summary; `track_view=true` records a VIEWED event (authenticated only) |
| POST | `/api/recommendations/generate` | token | body `{category?, all, max_opportunities (1–100, capped at 10), dry_run}`; returns counts, skips, LLM calls, tokens, estimated cost, generator, notes |
| POST | `/api/recommendations/{id}/save` | token | body `{notes?}`; also starts production tracking at IDEA |
| DELETE | `/api/recommendations/{id}/save` | token | remove from saved ideas |
| POST | `/api/recommendations/{id}/dismiss` | token | hide from default lists (kept, reversible by saving) |
| POST | `/api/recommendations/{id}/feedback` | token | body `{feedback_type, value?, notes?}`; types `USEFUL, NOT_RELEVANT, TOO_COMPETITIVE, TOO_LATE, UNVERIFIED, SAVED, DISMISSED, PUBLISHED, VIEWED, SELECTED, PRODUCED, ABANDONED` |
| POST | `/api/recommendations/{id}/production` | token | body `{status, planned_publication_time?, actual_publication_time?, published_video_id? (11-char YouTube ID, required for PUBLISHED), notes?}`; invalid transitions → 409 |
| GET | `/api/saved` | token | `status, category` |
| GET / PUT | `/api/profile` | token | the creator profile (`creator_profiles.md`) |
| GET | `/api/alerts` | token | `unread_only` |
| POST | `/api/alerts/{id}/read` | token | mark one alert read |
| GET | `/api/system/status` | read | mode, version, database schema, components, LLM mode and token use, versions, counts, alert channels |
| GET | `/api/system/providers` | read | provider and collector status (never credentials) |
| GET | `/api/system/data-quality` | read | freshness per provider, measurement and search property |

"read" = the token when one is configured, otherwise public with user fields removed.

Production status transitions: IDEA → RESEARCHING / SCRIPTING / ABANDONED; RESEARCHING →
IDEA / SCRIPTING / ABANDONED; SCRIPTING → RESEARCHING / RECORDING / ABANDONED; RECORDING →
SCRIPTING / EDITING / ABANDONED; EDITING → RECORDING / READY_TO_PUBLISH / ABANDONED;
READY_TO_PUBLISH → EDITING / PUBLISHED / ABANDONED; ABANDONED → IDEA; PUBLISHED is final.
Publishing records that a video exists; it is never taken as evidence that it succeeded.

## Backends

The API depends only on the `DashboardBackend` protocol (`recommendations/backend.py`):
`DatabaseBackend` (PostgreSQL; synthetic records excluded) or `DemoBackend` (in-memory
snapshot of the fictional scenarios; writes go to memory only and are lost on restart).
