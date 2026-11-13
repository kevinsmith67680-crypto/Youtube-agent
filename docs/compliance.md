# Compliance: provider policies, permissions and retention

> This document describes how the software behaves and what the operator must verify. It
> is not legal advice, and the existence of a feature in this code base is **not** evidence
> that Google has granted any permission. Policy texts change; re-read the official
> documents before enabling any restricted feature.
>
> **Verification note:** `developers.google.com` was blocked by the network policy of the
> environment in which Phase 1 was built, so the official pages could not be re-read in
> full. The summaries below are based on prior knowledge plus search-engine excerpts of the
> official pages; specific section numbers are deliberately not cited. The operator must
> confirm them against the current text before production use.

## Official references

* YouTube API Services Terms of Service — https://developers.google.com/youtube/terms/api-services-terms-of-service
* YouTube API Services Developer Policies — https://developers.google.com/youtube/terms/developer-policies
* YouTube API Services Required Minimum Functionality — https://developers.google.com/youtube/terms/required-minimum-functionality
* YouTube Branding Guidelines — https://developers.google.com/youtube/terms/branding-guidelines
* YouTube Data API quota / costs — https://developers.google.com/youtube/v3/determine_quota_cost and https://developers.google.com/youtube/v3/getting-started#quota
* YouTube API Services audit & quota extension — https://support.google.com/youtube/contact/yt_api_form
* Google Trends API (alpha) — https://developers.google.com/search/apis/trends
* Google Terms of Service — https://policies.google.com/terms

## YouTube API Services: obligations relevant to this project

| Topic | Policy (summary) | How Phase 1 handles it |
|---|---|---|
| **Storage / refresh of API data** | Non-authorised API data may be stored only temporarily and must be refreshed or deleted within **30 calendar days**. | `videos`, `channels`, `video_search_results` carry `metadata_expires_at`/`expires_at` = collection + ≤ 30 days (configuration refuses values > 30). `youtube refresh` re-fetches data nearing expiry and **deletes** videos the API no longer returns. `data retention --purge` deletes expired rows. Snippet data never extends an existing row's expiry. |
| **Statistics** | Storing YouTube statistics (views, likes, comments, subscriber counts) beyond 30 days is not permitted for non-authorised data; longer retention is tied to authorisation from the content owner. | **`TREND_AGENT_YOUTUBE_PERSIST_STATISTICS=false` by default** — statistics are shown but not stored (counted as `withheld`). When enabled, snapshots still expire after ≤ 30 days. Channel statistics are never stored. |
| **Authorised data** | Data accessed with a user's authorisation may be kept longer only while authorisation is valid, re-verified at least every 30 days, and deleted on revocation/request. | Not implemented: Phase 1 uses only an API key (public, non-authorised data). OAuth flows and per-user deletion are out of scope. |
| **Derived / independently calculated metrics** | Metrics computed from API data must not be presented as YouTube metrics and are subject to restrictions; some analytics use cases require review/approval. | Phase 1 computes no metrics. Later phases must label derived metrics as such and review the policies first. `approx_matching_videos` is stored only in run summaries and explicitly labelled "not search volume". |
| **Data combination** | Combining YouTube API data with other data sources, or aggregating across channels, can be restricted. | Phase 1 stores sources side by side with provenance and never merges them into a single metric. Cross-source analytics (Phases 4–7) require a policy review. |
| **Attribution / branding** | Displays of API data must identify YouTube as the source and follow branding guidelines; link back to YouTube content. | CLI output states "Source: YouTube Data API" and shows `youtube.com/watch` URLs. A future dashboard (Phase 7) must implement branding requirements. |
| **Undocumented access** | Only documented API methods may be used; scraping and circumventing limits are prohibited. | Only `search.list`, `videos.list`, `channels.list`, `videoCategories.list`. No scraping, no proxy rotation, no CAPTCHA avoidance, no key pooling. |
| **Quota** | Projects get a default allocation; more requires an audit/extension request. Invalid requests also consume quota. | Per-bucket local accounting with safety margin; exhaustion stops requests; every attempt is counted. Request quota extensions through Google's form, not by multiplying keys/projects. |
| **Security of credentials** | Keep keys confidential. | Key held as `SecretStr`, sent only in the `X-Goog-Api-Key` header, redacted from logs/errors/summaries/run records, `.env` git-ignored. Restrict the key to the YouTube Data API in Cloud Console. |
| **Privacy policy / user data** | Apps serving end users must have privacy disclosures, honour deletion requests within the policy deadline. | Phase 1 has no end users and stores no personal user data. Required before any user-facing deployment. |

### Features that require additional authorisation before enabling

1. `TREND_AGENT_YOUTUBE_PERSIST_STATISTICS=true` — confirm your use case permits storing
   statistics, and that 30-day expiry is sufficient (or obtain the authorisation that
   permits longer retention).
2. Any long-term historical analysis of YouTube statistics or search-result snapshots
   (beyond 30 days) — not supported by default; requires authorisation/approval.
3. Derived analytics products built from YouTube API data (Phases 4–7) — review the
   derived-metrics and data-combination rules; an API compliance audit may be required.
4. Quota above the default allocation — YouTube API Services audit/extension.

## Google Trends

* The official Google Trends API is in limited-access alpha; using it requires Google's
  approval and acceptance of its terms. Retention rules depend on those terms, so Trends
  data is **not stored** until `TREND_AGENT_GOOGLE_TRENDS_RETENTION_DAYS` is set by the
  operator; rejected-record payloads from a source without storage permission are also
  withheld.
* Unofficial access to the Trends website's internal endpoints is not implemented (see
  [data_sources.md](data_sources.md)) because of reliability and Terms-of-Service risk.

## Synthetic data

Synthetic data has no third-party restrictions but must never be presented as real
demand: it is labelled at the record level (`data_nature`, `is_synthetic`,
`synthetic://` reference), enforced by validation and a database constraint, and flagged
in CLI output.

## Keyword discovery sources (Phase 2)

| Source | Access | Stored | Retention (default) | Notes |
|---|---|---|---|---|
| YouTube Data API (search, charts, channel uploads) | API key | title, ≤ 1,500-char description excerpt, channel id, URL, timestamps | **30 days** (capped by code, whatever the config says) | discovery fetches also count against a local daily cap (`providers.youtube_discovery.daily_request_limit`) |
| RSS/Atom feeds | public feeds offered by the publisher | title, ≤ 2,000-char excerpt (HTML stripped), link | 90 days | conditional GET; respect publisher terms and poll intervals |
| GitHub releases | public REST API (optional token) | release name, notes excerpt, URL | 180 days | API rate limits respected (403/429 handling) |
| Hacker News | public Algolia API | title, story text excerpt, URLs | 90 days | |
| Reddit | OAuth Data API (registered app) | title, self-text excerpt, permalink | 30 days | requires accepting Reddit's Data API terms; commercial use may need an agreement |
| Google Trends related queries | official alpha API only | query strings | not stored as interest data | never scraped |
| Curated seeds | local configuration | seed text | — | |

Rules enforced in code: no scraping or undocumented endpoints (a source without a
registered provider is skipped as `unsupported`); network-policy refusals are reported as
`access_restricted` and not retried; untrusted XML is parsed with `defusedxml`; responses
over 5 MB are rejected; secrets are `SecretStr` and never stored or logged; discovery items
expire per provider retention and the purge (`trend-agent data retention --purge`) cascades
to evidence links and entity mentions. Search suggestions and related queries are recorded as
discovery evidence only — never as search-interest observations. YouTube autocomplete
(undocumented endpoint) is **not** used.

## Historical monitoring (Phase 3)

Every stored monitoring observation references a versioned data policy
(`data_policies`, e.g. `youtube-api-services@2026-11-reviewed`), and expires according to
it. Policy and permission flags live in `config/monitoring.yaml` (`policies`,
`compliance`).

| Policy | Retention | Notes |
|---|---|---|
| `synthetic-test-data@1` | none | fixtures only |
| `google-trends-alpha@1` | from `TREND_AGENT_GOOGLE_TRENDS_RETENTION_DAYS`; **storage denied while unset** | requires alpha access; terms supplied to testers |
| `discovery-evidence-derived@1` | earliest expiry of the source discovery items | derived counts never outlive their inputs; YouTube-sourced items excluded unless permitted |
| `youtube-api-services@2026-11-reviewed` | 30 days | collection disabled in Phase 3 |

**YouTube API restrictions considered.** The YouTube API Services policies limit storage
of API data (refresh or delete within 30 days), restrict statistics and *derived metrics*
(values calculated from API data, such as series of view counts over time) unless the
use is authorised, and restrict aggregation and combination with other data. A 30-day
storage allowance does **not** permit building unlimited historical analytics.
Accordingly:

* no YouTube time-series collector is enabled (`youtube_data_api` profile:
  `collection_disabled_reason`);
* `compliance.youtube_derived_metrics_permitted` and `compliance.youtube_statistics_monitoring`
  default to `false`; while false, the event-signal collector also excludes items that
  came from YouTube (reported as `withheld`);
* nothing derived from YouTube data is kept after the source data is deleted (no derived
  YouTube values are stored at all);
* the YouTube Data API is never used as a search-interest source (it has none).

**Required before enabling YouTube-based monitoring:** a documented review of the current
YouTube API Services Terms and Developer Policies for the intended calculation, any
required audit/approval for derived metrics, and statistics persistence
(`TREND_AGENT_YOUTUBE_PERSIST_STATISTICS`). Then set the compliance flags and implement a
collector whose policy expiry matches the permission.

**Unresolved questions (document before production):**

1. Google Trends API alpha terms: permitted retention, redistribution and whether
   derived analytics may be stored. Storage stays denied until answered.
2. Whether consistently scaled Trends values may be stored beyond a rolling window, and
   whether revisions must replace (not supplement) earlier values.
3. Whether event-signal counts from third-party feeds (vendor blogs, Reddit, Hacker News)
   are subject to those services' terms when aggregated over time (currently they expire
   with their source items).
4. Retention of job/attempt records (no third-party content is stored there, only counts
   and error categories).

## Surge detection (Phase 4)

* **Derived data follows its sources.** Every anomaly result carries the expiry of the
  observation it was computed from; every surge episode the *earliest* expiry of its
  evidence. `trend-agent data retention --purge` deletes expired detection rows, and
  foreign keys cascade when source observations are purged. Synthetic data never expires.
* **No new data is collected.** Detection only reads stored Phase 3 observations; it makes
  no provider requests and consumes no quota.
* **YouTube derived metrics stay disabled.** Video-activity series exist only where Phase 3
  was permitted to store them (`compliance.youtube_derived_metrics_permitted`, default
  false); detection on them is labelled a proxy attention signal, never search demand.
* **No search counts.** Relative indices are never converted into numbers of searches; the
  output contract and explanations state this explicitly.
* **Synthetic isolation.** Synthetic series, calibrations, results and episodes are
  flagged; calibrations fitted on synthetic data are never applied to real data; production
  listings and the Phase 5 feed exclude synthetic episodes unless explicitly requested.

Unresolved questions (need the operator / legal review before production):

1. Whether alerts computed from Google Trends API data may be stored for the configured
   Trends retention period or must expire earlier (the Trends alpha terms were not
   available in this environment).
2. Whether surge episodes derived from YouTube video statistics, if that metric is ever
   permitted, count as "derived metrics" under the YouTube API Services policies.

## Verification layer

* **Evidence copies keep their sources' retention.** `evidence_records` carry the
  source's `expires_at` and data policy; verification runs expire with the earliest
  expiry of the evidence they were shown. `trend-agent data retention --purge` deletes
  them (runs first, then unused bundles, then evidence records).
* **Source text is untrusted data.** YouTube titles/descriptions, comments, feeds and
  GitHub pages are stored only as sanitised, bounded excerpts, flagged for injection
  patterns, and can never authorise an action.
* **Optional LLM use sends evidence excerpts to the model provider.** With
  `--generator anthropic`, the request bundle (excerpts of stored source text, the
  monitored query and the engine's statistics) is sent to Anthropic's API. Before
  enabling it, confirm that the provider terms of each source (in particular YouTube API
  data) allow sending that content to a third-party processor; the default generator
  (`template`) sends nothing anywhere.
* **Signing key.** `TREND_AGENT_VERIFICATION_SIGNING_KEY` is required in production; keep
  it in a secret manager. The development key is public.

## Default posture (conservative)

| Control | Default |
|---|---|
| YouTube statistics persistence | off |
| YouTube metadata retention | 30 days (cannot be configured higher) |
| YouTube video descriptions | stored, with the same expiry (`..._PERSIST_DESCRIPTIONS=false` to disable) |
| Trends persistence | off until a retention period is configured |
| Unknown record kinds from any provider | storage denied (`ProviderDescriptor.retention_for`) |
| Live API calls in tests | never (opt-in `live` marker only) |
| Discovery item retention | per provider (YouTube ≤ 30 days, Reddit 30, feeds/HN 90, GitHub 180) |
| YouTube monitored channels | disabled until handles are confirmed |
| Monitoring observations | per data policy; Trends denied until a retention period is set; derived values expire with their sources |
| YouTube derived metrics / statistics history | disabled (`compliance.*` false) |
| Recommendation generator | deterministic template (no external calls); LLM adapter opt-in |
| Synthetic evidence | cannot support claims outside synthetic runs |

## Operator checklist before production use

- [ ] Re-read the YouTube API Services Terms and Developer Policies (links above).
- [ ] Restrict the API key to the YouTube Data API; store it in a secret manager.
- [ ] Schedule `trend-agent youtube refresh` and `trend-agent data retention --purge` at
      least daily (Phase 3).
- [ ] Decide, with policy review, whether statistics may be stored for your use case.
- [ ] Obtain Google Trends alpha access (or a licensed provider) before relying on
      search-interest data; set its retention period from the access terms.
- [ ] Prepare branding/attribution and privacy disclosures before any user-facing release.
