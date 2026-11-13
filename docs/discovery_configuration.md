# Discovery configuration

Everything that decides *what* discovery looks at is YAML under `config/` (or the directory
in `TREND_AGENT_DISCOVERY_CONFIG_DIR`). Adding a game, AI product, feed or community is a
configuration change, not a code change. Configuration is validated strictly on load
(unknown keys, bad enums, budgets/weights not summing to 1, duplicate source keys and
invalid YAML are errors; the CLI exits with code 2). An explicitly configured directory must
exist; it is never silently replaced by another one.

```
config/
  discovery.yaml           budgets, limits, scoring, lifecycle, provider switches
  seeds/ai.yaml            seed terms + curated entity gazetteer (one file per category)
  seeds/technology.yaml
  seeds/gaming.yaml
  sources/official_feeds.yaml       RSS/Atom: PyPI release feeds, vendor blogs, Steam news
  sources/repositories.yaml         GitHub repositories (releases)
  sources/communities.yaml          subreddits, Hacker News front page
  sources/youtube.yaml              YouTube most-popular charts
  sources/monitored_channels.yaml   YouTube channels (uploads)
  sources/trends.yaml               Google Trends related-query sources
```

Check what was loaded and whether each source can run: `trend-agent discover sources`.

## `discovery.yaml`

| Key | Default | Meaning |
|---|---|---|
| `category_budgets.{ai,technology,gaming}` | 0.45 / 0.25 / 0.30 | share of each run's fetch slots; must sum to 1 |
| `exploration_share` | 0.20 | share of a category's slots reserved for exploration |
| `regions`, `languages` | `[US, GB]`, `[en]` | ISO codes; first region is the default for API queries |
| `run.max_fetches_per_run` | 24 | total source fetches per `discover run --all` |
| `run.max_items_per_source` | 50 | items requested per fetch |
| `run.lookback_days` | 14 | ask sources only for newer material |
| `run.max_item_age_days` | 365 | older items are flagged `outdated` and not expanded |
| `run.fetch_concurrency`, `run.fetch_timeout_seconds` | 4, 60 | |
| `run.max_failure_backoff_multiplier` | 16 | poll interval × 2^failures, capped |
| `expansion.max_depth` | 2 | 0 none, 1 templates, 2 + comparisons |
| `expansion.max_templates_per_entity` / `max_comparisons_per_entity` | 10 / 2 | explosion limits |
| `expansion.max_keywords_per_run` | 400 | candidates per category run (observed queries kept first) |
| `expansion.min_entity_confidence` | 0.6 | |
| `expansion.max_query_words` | 10 | longer candidates are rejected |
| `clustering.backend` | `lexical` | `embedding` requires injecting a `SimilarityBackend` |
| `clustering.intent_similarity_threshold` | 0.5 | facet Jaccard needed to join an intent cluster |
| `scoring.version` | `priority-v1` | stored with every score |
| `scoring.weights.*` | 0.25/0.20/0.20/0.15/0.10/0.10 | must sum to 1 |
| `scoring.freshness_half_life_days`, `recency_half_life_days` | 7, 7 | the recency half-life is also the novelty "new" window |
| `scoring.prioritize_threshold` | 45 | validated → prioritized |
| `lifecycle.dormant_after_days` | 30 | no new evidence → dormant |
| `providers.<name>.enabled` | true | provider switch |
| `providers.<name>.daily_request_limit` | 500 | local daily cap for HTTP providers and for `youtube_discovery` (on top of the YouTube API quota buckets) |
| `providers.<name>.retention_days` | per provider | expiry of stored items (YouTube is capped at 30 days regardless) |
| `providers.youtube_discovery.params.search_results_per_query` | 25 | |
| `providers.hackernews.params.categories` | `[ai, technology]` | categories whose seeds also become HN searches |

## Seeds and gazetteer (`seeds/<category>.yaml`)

```yaml
category: gaming
seeds:
  - {term: Fortnite event, subcategory: fortnite, priority: 95}
  - {term: best settings fps, subcategory: performance, priority: 70, search: true,
     track_as_keyword: false, regions: [US], languages: [en], enabled: true}
entities:
  - {name: Grand Theft Auto, entity_type: game, aliases: [GTA Online],
     case_sensitive_aliases: [GTA], evergreen: true, priority: 85}
  - {name: Claude, entity_type: ai_model, context_terms: [anthropic, model, llm]}
```

**Seed fields:** `term`, `subcategory`, `priority` (0–100, feeds relevance), `entity_type`
(optional), `regions`, `languages`, `enabled`, `search` (turn the seed into query sources:
a YouTube search for every category, plus a Hacker News search for the categories in
`providers.hackernews.params.categories`), `track_as_keyword` (also keep the seed text itself
as a `seed_term` candidate). Seeds are starting points chosen by an operator; they are never
evidence of interest.

**Gazetteer fields:** `name`, `entity_type` (one of the 13 entity types), `aliases`,
`case_sensitive_aliases` (short acronyms like `GTA`, `CS2`), `context_terms` (needed nearby
to accept an ambiguous name), `subcategory`, `priority`, `evergreen` (curated, long-lived:
not expanded itself, but its new versions/events/items are).

Operational state for each source (last check, last success, consecutive failures, health,
expected yield, ETag) is tracked in the `discovery_sources` table, not in YAML.

Current files: AI 20 seeds / 27 entities, technology 15 / 27, gaming 15 / 13.

## Sources (`sources/*.yaml`)

```yaml
sources:
  - key: feed:pypi:vllm                # unique; letters, digits and _ : - . / @
    provider: feeds                    # feeds | github_releases | hackernews | reddit |
                                       # youtube_discovery | google_trends_discovery
    name: PyPI releases - vllm
    source_type: feed                  # feed | api_query | chart | channel | repository |
                                       # community | seed | exploration_query
    category: ai
    reference: https://pypi.org/rss/project/vllm/releases.xml
    evidence_type: code_release        # what the items are evidence of
    publisher: pypi.org                # used for independent-origin grouping
    entity_hint: vLLM                  # entity a release feed is about
    entity_hint_type: software
    reliability: 0.9                   # 0..1, feeds evidence quality and scheduling
    min_poll_interval_minutes: 720
    pool: exploitation                 # or exploration
    region: US                         # optional
    language: en                       # optional
    enabled: true
    verified: true                     # reference confirmed by the configuration author
    params: {}
```

| `source_type` | Provider | `reference` |
|---|---|---|
| `feed` | `feeds` | `https://` RSS/Atom URL |
| `repository` | `github_releases` | `owner/repo` (validated; no `.`/`..` segments) |
| `community` | `reddit` | subreddit name (`LocalLLaMA`) |
| `community` / `api_query` | `hackernews` | `front_page` or a search term |
| `api_query` | `youtube_discovery`, `google_trends_discovery` | query text |
| `chart` | `youtube_discovery` | YouTube video category id (e.g. `20` Gaming, `28` Science & Technology) |
| `channel` | `youtube_discovery` | `@handle` or `UC…` channel id |

Seed-derived sources (`seed:youtube:…`, `seed:hn:…`, `seed:curated:…`) and exploration
sources (`explore:youtube:…`, built from verified entities first seen in the last 7 days)
are generated at run time and need no YAML.

Bundled source files: 26 feeds (20 PyPI release feeds, `verified: true`; 6 vendor blog and
Steam news feeds are `verified: false` because they could not be reached from the build
environment),
10 GitHub repositories, 8 subreddits + Hacker News front page, 3 YouTube charts, 3 YouTube
channels (disabled until their handles are confirmed), 3 Trends related-query sources.

### Adding things

* **A game:** add it to `seeds/gaming.yaml` `entities` (with `case_sensitive_aliases` for
  acronyms) and a seed such as `{term: "<Game> update", priority: 80}`; optionally its official
  news feed (`sources/official_feeds.yaml`, `evidence_type: official_announcement`) and its
  YouTube channel (`sources/monitored_channels.yaml`).
* **An AI product or library:** gazetteer entry in `seeds/ai.yaml`; for a library, a PyPI
  feed `https://pypi.org/rss/project/<package>/releases.xml` with `evidence_type: code_release`,
  `entity_hint: <Display Name>` and `entity_hint_type: software` (or `framework`).
* **A community:** subreddit in `sources/communities.yaml` (needs Reddit credentials).

## Environment variables (Phase 2)

| Variable | Purpose |
|---|---|
| `TREND_AGENT_DISCOVERY_CONFIG_DIR` | configuration directory (default `./config`) |
| `TREND_AGENT_HTTP_USER_AGENT` | descriptive User-Agent for feeds and public APIs (Reddit requires one; put a real contact in it) |
| `TREND_AGENT_GITHUB_TOKEN` | optional read-only token (5,000 requests/hour instead of 60) |
| `TREND_AGENT_REDDIT_CLIENT_ID`, `TREND_AGENT_REDDIT_CLIENT_SECRET` | Reddit OAuth app (client-credentials); Reddit sources report `not_configured` without them |
| `TREND_AGENT_YOUTUBE_API_KEY` (Phase 1) | enables YouTube searches, charts and channels |

Secrets are `SecretStr`, redacted in `trend-agent doctor` / logs, and never stored in the database.
