# GitHub REST API integration report

Date: 2026-11-14 (UTC). Branch `claude/tender-thompson-620dmi`, schema revision **0010**.

## Result

The agent now discovers new AI and developer-tool repositories, tracks repeated observations of their metadata, monitors releases of established projects, and turns verified projects into keyword hypotheses. All of this runs through the existing Phase 2 discovery pipeline, so `discover run` and the scheduler pick GitHub up unattended. The dashboard has a GitHub page that labels every record with its evidence class.

**What was verified live in this sandbox:**
- authentication headers and API version;
- rate-limit tracking;
- repository search across 13 configured topics;
- repository metadata and the releases endpoint, including a 304 conditional request on an attached repository;
- persistence, provenance, keyword generation and dashboard display.

**What was not verified live:**
- **Your token's validity.** This sandbox's proxy replaces every `api.github.com` credential with its own (section 4).
- **Release ingestion for third-party repositories.** The same proxy blocks `/repos/{owner}/{repo}/…` for repositories not attached to this session. That path is covered by mock-based tests only.

GitHub data is labelled **external developer activity** everywhere. The integration never produces search interest or search volume, and it never contributes to a YouTube Search surge claim.

## 1. Audit before implementation

| Area | Finding | Decision |
|---|---|---|
| Provider abstractions | Phase 1 `Provider` protocol and registry; Phase 2 `DiscoveryProvider` (`supports` / `can_afford` / `discover`); a minimal `github_releases` provider (releases only, no metadata, no search) | Replace it with one `github` provider. Keep the same source keys (`github:owner/repo`) so source history continues. |
| Environment / configuration | pydantic-settings with the `TREND_AGENT_` prefix; `TREND_AGENT_GITHUB_TOKEN` existed | Add `GITHUB_TOKEN` / `GITHUB_API_ENABLED` as aliases; keep the prefixed names working. |
| Phase 2 discovery | Items → entities (extraction, gazetteer, source hints) → template expansion with grounded attributes → scored candidates with evidence links | Feed GitHub material in as items. Add grounded developer-tool templates. |
| Scheduler / quota | `QuotaManager` with persisted daily buckets; retry with jittered backoff; the Phase 8 scheduler had **no discovery stage** | Use per-bucket GitHub budgets. Add a gated `discovery` stage. |
| Database | Enum values are stored as CHECK constraints | Migration 0010 widens them and adds three GitHub tables. |
| Evidence / verification | `EvidenceType` → `EvidenceKind` mapping, trust levels, injection heuristics in `verification/untrusted.py` | Add `repository_metadata` with its own (lower) trust. Reuse the injection signals. |
| Dashboard | Next.js pages over FastAPI; provider health on `/settings` | Add `/github` and `/github/[id]` plus four read-only API endpoints. |
| Baseline tests | 970 passed before this work | Regression results are in section 9. |

## 2. Implementation summary

**Client** (`github/client.py`)
- Read-only: only `GET` is implemented.
- Sends `Authorization: Bearer` when a token exists, `Accept: application/vnd.github+json` and `X-GitHub-Api-Version`.
- Tracks rate limits per resource, keeps a reserve below GitHub's live limit, and handles primary and secondary limits plus `Retry-After`.
- Applies local daily budgets per bucket and ETag conditional requests.
- Deduplicates in-flight and recent requests, paginates boundedly, and follows `Link` only on the API host.
- Maps every failure to a typed error; schema changes are detected.

**Provider** (`discovery/providers/github.py`)
- Capabilities: `REPOSITORY_DISCOVERY`, `REPOSITORY_METADATA`, `RELEASE_MONITORING`, `TOPIC_DISCOVERY`, `DEVELOPER_ACTIVITY` (plus `TREND_DISCOVERY`).
- `repository_search` sources implement strategy A, emerging projects:
  - `emerging`: created in the last 21 days, ≥ 2 stars, no forks or archives, sorted by stars;
  - `active`: pushed in the last 7 days, ≥ 500 stars.
- `repository` sources implement strategy B, established projects: current metadata plus the latest releases, with an ETag on the releases list.

**Quality gates** (`github/analysis.py`)

Each gate either **excludes** a repository or **flags** it for review:

| Gate | Effect |
|---|---|
| Archived, disabled, fork, mirror, private | excluded |
| Empty (`size == 0`) | excluded |
| Description missing or under 20 characters | excluded |
| Curated "awesome" lists | excluded |
| Generic names (`studio`, `harness`, `dotfiles`, …) | excluded |
| Low relevance to the category | excluded |
| Instruction-like (prompt-injection) descriptions | excluded |
| **`fake_star_pattern`**: ≥ 200 stars, forks ≤ 1 % of stars, under 2 MB of code | excluded |
| **`coordinated_star_pattern`**: ≥ 3 results created within 6 h with star counts within 10 % of each other | excluded |
| **`software_lure`**: pirated-software or installer bait wording | excluded |
| `star_pattern_unexplained`: unusual star bursts on otherwise plausible projects | flagged only; earns no adoption credit in ranking |

**Release analysis**
- Parses the exact version from the tag or name and classifies it: major, minor, patch, prerelease or unknown.
- Breaking-change, new-capability and API lines are **quoted verbatim** from the release notes (sanitised, bounded). Nothing is summarised or inferred.
- Release notes containing injection signals are never quoted.

**Momentum** (`github/momentum.py`)
- Computes deltas only between two stored observations over their actual interval.
- Rates (per day) are given only when observations are at least 6 h apart.
- A single cumulative count is never treated as growth.
- Every result carries `signal_class: DEVELOPER_ACTIVITY` and the caveats.

**Persistence**
- A discovery observer hook writes `github_repositories` (keyed by GitHub's stable ID), `github_repository_observations` (one per repository per collection time) and `github_releases` (keyed by GitHub's release ID).
- These writes happen in the same transaction as the discovery items, and the rows link to the entity, item, run, source and provider.

**Keyword expansion and evidence**
- The repository's own name is the only name that GitHub verifies.
- Other names in a self-description stay `needs_verification` until independently corroborated.
- New developer-tool templates require grounded attributes:
  - `repository`;
  - plus `install`, `benchmark` or `build` where the template needs them.
- Releases of established projects expand through the existing release templates: "X 2.0 new features", "how to upgrade to X 2.0", and "X 2.0 breaking changes" when the notes' breaking-changes section says so.
- Comparisons ("X vs Y") are produced only for competitors grounded in the evidence.

**Scheduling**
- `discover run` includes GitHub sources automatically.
- The Phase 8 scheduler gets a `discovery` stage: every hour, at most 24 fetches, enabled by `TREND_AGENT_SCHEDULED_DISCOVERY=true`.
- Per-source poll intervals (12–24 h for searches) and failure backoff come from the existing planner.

**Interfaces**
- CLI `trend-agent github status|test|discover|repositories|releases|quota`.
- API `/api/github/summary|repositories|repositories/{id}|releases`.
- Dashboard pages `/github` and `/github/[id]`.

## 3. Files changed

**New**

| Path | Purpose |
|---|---|
| `src/trend_agent/github/{__init__,models,client,config,analysis,momentum,service}.py` | client, typed models with measurement definitions, settings, gates and release analysis, momentum, recorder and read models |
| `src/trend_agent/discovery/providers/github.py` | discovery provider |
| `src/trend_agent/db/github_models.py`, `src/trend_agent/db/repositories/github.py` | tables and repository layer |
| `migrations/versions/20261114_0010_github_integration.py` | migration 0010 |
| `src/trend_agent/cli_github.py` | `trend-agent github …` |
| `config/sources/github_searches.yaml` | 26 search sources (12 requested seeds, 10 more focus areas, 4 established/active) |
| `frontend/app/github/page.tsx`, `frontend/app/github/[id]/page.tsx`, `frontend/components/GitHubViews.tsx`, `frontend/lib/github.ts` | dashboard |
| `tests/github_fixtures.py` | fake GitHub server and builders |
| `tests/unit/github/test_{client,analysis,provider}.py` | GitHub unit tests |
| `tests/integration/test_github.py` | database-backed GitHub tests |
| `tests/live/test_github_live.py` | opt-in live tests |
| `frontend/tests/github.test.tsx` | frontend component tests |
| `docs/screenshots/github_*.png` | dashboard screenshots |

**Modified**

| Area | Paths |
|---|---|
| Settings | `config.py` (token aliases, `github_api_enabled`, `github_api_version`, `github_api_base_url`, `scheduled_discovery`) |
| Token redaction | `logging.py` |
| Enums | `schemas/enums.py`, `discovery/models.py`, `schemas/verification.py`, `ops/signals.py` |
| Discovery | `discovery/{evidence,extraction,expansion,service}.py`, `discovery/providers/{factory,community}.py` |
| Verification and detection mappings | `verification/{sources,config}.py`, `detection/config.py` |
| Operations | `ops/{config,pipeline,providers}.py`, `ingestion/retention.py`, `container.py` |
| API and CLI | `api/app.py`, `cli.py` |
| Schema revision | `db/{__init__,models}.py` |
| Configuration | `config/{discovery,operations}.yaml`, `config/sources/repositories.yaml` (provider renamed; 9 AI-agent and developer-tool repositories added) |
| Deployment | `.env.example`, `docker-compose{,.prod}.yml` (backend services only) |
| Docs | `README.md`, `docs/discovery_configuration.md` |
| Frontend | `frontend/components/Nav.tsx`, `frontend/e2e/dashboard.spec.ts` |
| Tests | `tests/conftest.py` (strips `GITHUB_TOKEN`, new tables, guarded-downgrade reset), `tests/integration/test_{database,recommendations}.py` (revision 0010), `tests/unit/discovery/test_providers.py` (tests moved to the new provider) |

## 4. Authentication configuration

| Variable | Meaning |
|---|---|
| `GITHUB_TOKEN` (alias `TREND_AGENT_GITHUB_TOKEN`) | Optional. With it: 5,000 core requests/hour and 30 searches/minute. Without it: public unauthenticated access, 60/hour and 10/minute. A fine-grained token with **no repository permissions** is sufficient; write access is never requested or needed. |
| `GITHUB_API_ENABLED` | `true` (default) or `false`. When `false` the provider reports DISABLED and makes no requests. |
| `TREND_AGENT_GITHUB_API_VERSION` | `2026-03-10` (default, the current version) or `2022-11-28` (supported until 2028-03-10). |
| `TREND_AGENT_SCHEDULED_DISCOVERY` | `true` lets `trend-agent system scheduler` run discovery (GitHub included). Default `false`; the compose app profile sets `true`. |

Your token is stored only in the git-ignored `.env` (mode 600) as `GITHUB_TOKEN`. Its handling:
- it is held as a `SecretStr` and sent only in the `Authorization` header;
- it is never logged: `ghp_…` and `github_pat_…` shapes are redacted from all log and error text;
- it is never stored in the database and never returned by the API or the CLI (`credentials: set`, `auth_mode: authenticated`);
- the frontend container never receives it;
- it appears 0 times in Git history, tracked files and command output.

**Sandbox caveat.** This cloud sandbox routes `api.github.com` through a proxy that substitutes the session's own GitHub credential for any `Authorization` header. Evidence:
- a deliberately bogus token received HTTP 200;
- the observed limits (core 15,000/hour) belong to the proxy's credential, not to a personal token (5,000/hour).

So **your PAT's validity was not verified here.** On your own machine, `trend-agent github test` checks it. A 401 is reported as `invalid_credentials`, and status becomes ACCESS_RESTRICTED with the instruction to fix or remove `GITHUB_TOKEN`.

## 5. Supported endpoints (documented REST API only)

| Endpoint | Used for | Rate-limit resource |
|---|---|---|
| `GET /search/repositories?q=…&sort=&order=&per_page=` | strategy A and established-active discovery | `search` |
| `GET /repos/{owner}/{repo}` | monitored-repository metadata (observation) | `core` (304 with ETag) |
| `GET /repos/{owner}/{repo}/releases?per_page=` | strategy B release monitoring (drafts excluded) | `core` (304 with ETag) |
| `GET /rate_limit` | connection test, quota view; does not count against the limit; reads `resources.*` (2026-03-10 removed `rate`) | none |

**Search syntax.** Only documented qualifiers are used: `topic:`, quoted phrase plus `in:name,description`, `created:>=`, `pushed:>=`, `stars:>=`, `language:`, `fork:false`, `archived:false`. All 12 requested seeds and 10 further topics returned results when checked live before configuration.

**Fields read and their meaning.** These include ID, full name, owner, URL, description, timestamps, language, topics, stars, forks, archived and size. Each stored value has a written definition in `github/models.py` (`MEASUREMENT_DEFINITIONS`), returned by `github status --json` and the API. Some key definitions:
- `pushed_at` and `updated_at` are explicitly *not* releases;
- `stargazers_count` is a cumulative total, not growth and not people.

## 6. Real versus mock integration status

| Capability | Live in this sandbox | Mock-tested |
|---|---|---|
| Version and Accept headers, Bearer header | ✅ (sent; replaced by the proxy) | ✅ |
| PAT validity / 401 handling | ❌ not verifiable (proxy substitution) | ✅ |
| `GET /rate_limit` | ✅ | ✅ |
| Repository search (13 topics, 2 strategies) | ✅ | ✅ |
| Bounded pagination | ✅ (1 page per search, configured) | ✅ multi-page, cross-host link refused |
| Repository metadata | ✅ for the attached repository; third-party repositories blocked by the proxy (HTTP 403, reported as `access_restricted`) | ✅ |
| Releases endpoint | ✅ for the attached repository (0 releases), including a live **304** conditional request | ✅ parsing, drafts, prereleases, quoting, ETag |
| Release ingestion for monitored third-party repositories | ❌ blocked by the proxy | ✅ end to end into `github_releases` and keywords |
| Rate limits: primary, secondary, Retry-After, reserve | live headers tracked; no limit was hit | ✅ |
| Persistence, idempotency, momentum, retention | ✅ (2 observations of 17 repositories; rate correctly withheld under 6 h) | ✅ incl. 24 h momentum |
| Dashboard | ✅ built and served against live data (screenshots) | ✅ Vitest and Playwright |

## 7. Actual discovery results (live, 2026-11-14 13:18–13:37 UTC)

The development database was rebuilt from the final code. Two cleanups removed only the GitHub-derived rows of earlier runs, so the counts below come from the final code.

**Final run** (13:36 UTC, 6 search requests: `coding-agent`, `image-generation`, `llm-evaluation`, `developer-tools`, `robotics` emerging, plus `ai-agent` active):
- 117 repositories passed the gates;
- 1 more was found on the re-check below;
- now stored: **116 repositories, 133 observations, 489 keyword hypotheses**;
- every repository is linked to its own entity.

**Quality gates on real data.** A re-check of two searches recorded these exclusions per source:

| Search | Results | Kept | Excluded |
|---|---|---|---|
| `developer-tools` | 30 | 1 | `coordinated_star_pattern` ×29, `fake_star_pattern` ×29, `software_lure` ×16 (one repository can match several gates) |
| `image-generation` | 26 | 15 | `coordinated_star_pattern` ×12, `curated_list` ×2, `low_relevance` ×1, `prompt_injection_suspected` ×1 |

- **developer-tools:** 29 of 30 results were a coordinated wave of pirated-software lure repositories. Examples: "Adobe Acrobat Pro 2026 … Desktop Installer" and "Delta Executor for PC: Full Working Installer". All were created within one hour on 2026-11-06, each had about 1,100–1,210 stars and 0–2 forks.
- **image-generation:** the coordinated cohort was one account's eleven `gpt-image-2-6-*` SEO repositories (100 stars and 0 forks each, created minutes apart).

These gates were added after the first live runs surfaced the patterns; earlier runs had let them through (section 10).

**Examples of genuine projects discovered:**

| Repository | Created | Stars at observation | Description (owner's) |
|---|---|---|---|
| MiniMax-AI/MiniMax-H3-Image | 2026-11-09 | 353 | open-weight model for 16-megapixel image generation and editing |
| huawei-csl/MACaQ | 2026-11-10 | 237 | model-agnostic quantization for GGUF and local inference |
| theRizwan/community-claude-code | 2026-11-12 | 44 | local proxy connecting coding agents to OpenAI-compatible providers |
| CrommaAI/crommacode | 2026-11-12 | 42 | gamified IDE for coding agents |
| devvmhq/devvm | 2026-11-06 | 625 | isolated per-project VM sandbox for agents |
| chessifyme/gmbench | 2026-11-12 | 4 | benchmark of how well LLM agents explain chess |
| wangzan101/Mika (active) | 2026-10-29 | 8,576 | on-device AI agent for Android |

Earlier runs today also surfaced DIYgod/pinky (MCP phone control) and the DeepSeek Harness ecosystem (dsh-Go, DSH-Android, fish-pocket).

**Strategy B (releases).** Live release ingestion for the 19 monitored repositories failed in this sandbox. The failure was `access_restricted` (proxy HTTP 403), recorded per source and contained; the run continued. On a normal network the same code path runs, as demonstrated against the attached repository's endpoint.

## 8. API rate-limit handling

- **Live headers.** `x-ratelimit-limit/remaining/used/reset/resource` are recorded per resource. Calls stop *before* exhaustion, with 25 core and 2 search requests held in reserve.
- **Primary limit.** A 403 or 429 with `remaining: 0` blocks that resource until `x-ratelimit-reset`. No request is sent meanwhile.
- **Secondary limit.** `retry-after`, or a "secondary rate limit" message, causes a wait of at least 60 s without one. Waits longer than the retry budget are not slept through: the fetch fails as `rate_limited`, and the planner retries later with backoff.
- **Local daily budgets (UTC).** Core 1,500 and search 300, persisted, far below GitHub's limits. `/rate_limit` uses a separate, free budget.
- **Conditional requests.** ETags are kept per URL, and the releases ETag is stored per source; a 304 reuses the cached body. Shown live.
- **Deduplication.** Identical concurrent requests share one HTTP call, and repeats within 10 minutes reuse the response. Shown live: a repeated request sent nothing.
- **Bounded pagination.** At most 1 page of 30 results per search by default; the client caps pages, and `Link` is followed only on the API host.
- **No evasion.** One token per deployment, no rotation, no scraping.

**Today's usage:**
- local ledger: 25 search requests and 1 core request by the application;
- plus 5 core requests from a separate verification script (in-memory ledger);
- plus about 25 search requests and a few core requests from manual `curl` checks while configuring (validating every seed query);
- GitHub (proxy credential): core 58 of 15,000.

## 9. Tests and verification

| Suite | Result |
|---|---|
| GitHub unit tests (`tests/unit/github`, 72) and discovery unit tests | **183 passed** |
| GitHub integration tests (`tests/integration/test_github.py`): pipeline, provenance, idempotency, momentum, outage, retention, API, demo-mode API, scheduler stage, CLI, gate reporting | **9 passed** |
| Frontend Vitest (incl. 9 GitHub tests: evidence classes, safe links, quoted releases, momentum without invented growth, "search volume" only negated) | **47 passed** |
| Playwright (incl. the new `/github` page: labels, demo state, axe) | **16 passed** |
| Live (opt-in) `tests/live/test_github_live.py` | **2 passed** against the attached repository; with the default third-party repository it fails only with the proxy's `AccessRestrictedError` |
| Full default regression suite (no credentials, PostgreSQL test database) | **1,051 passed, 0 failed**, 5 deselected (opt-in live tests), 14 min 44 s. An earlier full run had 1 failure: an outdated assertion listing the scheduler tasks, now updated to include `discovery`. |
| `ruff check`, `ruff format --check`, `mypy src` (275 files), `tsc --noEmit`, `next build` | clean |
| Migration 0010 | upgrade, downgrade and upgrade verified; `alembic check` reports no drift; downgrade refuses while new enum values are in use |

Required test areas and where they are covered:

| Area | Tests |
|---|---|
| Authentication, public access, versioning, invalid credentials, permissions | `test_client` |
| Repository search, pagination, duplicates | `test_client`, `test_provider`, integration |
| Rate limits, outages, network errors, schema changes | `test_client`, integration |
| Releases | `test_client`, `test_analysis`, `test_provider`, integration |
| Entity extraction, keyword generation, evidence provenance | `test_provider`, integration |
| Momentum | `test_analysis`, integration |
| Scheduler, database idempotency, dashboard | integration, Vitest, Playwright |
| Prompt injection | `test_analysis`, `test_provider`, integration |
| Unsupported search-demand claims | integration (no search-interest rows or series, `direct_youtube_search_interest.available = false`), Vitest |

## 10. Known limitations

1. **The sandbox proxy** blocks third-party `/repos/*` endpoints and replaces credentials. PAT validity and live release ingestion for monitored repositories remain unverified here.
2. **Momentum needs time.** Rates appear only after two observations at least 6 h apart; none exist yet. Stars and forks remain account actions, not people and not demand.
3. **Gates are heuristics.**
   - Relevance is keyword overlap.
   - The lure, fake-star and cohort gates were calibrated on today's live data.
   - The young-repository star flag can also fire on genuine viral launches; it flags but does not exclude.
   - A legitimately popular repository with almost no forks and little code would be excluded.
   - All thresholds are configurable in `providers.github.params.quality`.
4. **Pre-existing extractor heuristics.** Version-pattern typing labelled "ROS 2" and "Nav2" as AI models, giving odd hypotheses such as "ros 2 benchmark". A sentence-initial verb ("Runs …") had been taken as a name; that is fixed for common description verbs. Keyword *phrasing* still follows the existing Phase 2 templates (e.g. "what is X", "X review" for AI products).
5. **Volume of hypotheses.** Hypotheses are numerous (about 4–7 per verified project). The existing per-run cap and the priority scoring decide what is monitored; they are hypotheses, not observed queries.
6. **Corroboration.** External attention counts only non-GitHub publishers already in the database. Hacker News and Reddit are not configured in this sandbox, so most projects show 0.
7. **Phase 3 monitoring.** It turns GitHub *releases* into official-event signals, as before. Repository metadata is tracked in `github_repository_observations`, not as Phase 3 time series.
8. **Search caps.** GitHub search returns at most 1,000 results per query and can mark results incomplete; incompleteness is surfaced as a warning.

## 11. Commands

```bash
trend-agent db upgrade                                   # schema 0010
trend-agent github status                                # config, auth mode, budgets, stored data (never the token)
trend-agent github test                                  # GET /rate_limit (free) + 1 search request; records a provider check
trend-agent github discover --category ai                # GitHub sources through the discovery pipeline (default 8 fetches)
trend-agent github discover --category technology --strategy emerging --max-fetches 4
trend-agent github discover --category ai --strategy releases   # monitored repositories (metadata + releases)
trend-agent github repositories --recent 7 --order score
trend-agent github repositories --repo owner/name        # metadata, momentum, keyword hypotheses, releases
trend-agent github releases --recent 30
trend-agent github quota --live                          # local budgets + GitHub's own limits
trend-agent discover run --all                           # all providers, GitHub included
TREND_AGENT_SCHEDULED_DISCOVERY=true trend-agent system scheduler   # unattended (plus `system worker`)
TREND_AGENT_RUN_LIVE_TESTS=1 pytest -m live tests/live/test_github_live.py
```

## 12. Remaining configuration steps

1. **Rotate the token.** It was pasted into a chat conversation. Create a fine-grained PAT with **no repository permissions** (public read access is enough) and a short expiry. Then put it in `.env` as `GITHUB_TOKEN`.
2. **Validate it on a normal network.** Run `trend-agent github test`; in this sandbox the proxy prevents that check.
3. **Upgrade the database** with `trend-agent db upgrade` (revision 0010).
4. **Turn on unattended discovery.** Set `TREND_AGENT_SCHEDULED_DISCOVERY=true` (already the default in `docker compose --profile app`) and run the scheduler and a worker.
5. **Optional: add independent corroboration** (Hacker News is already configured; Reddit needs OAuth credentials). This fills the EXTERNAL_ATTENTION class.
6. **Review the gate thresholds** after a few days of real data, especially the fake-star and cohort gates.
