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

## Operator checklist before production use

- [ ] Re-read the YouTube API Services Terms and Developer Policies (links above).
- [ ] Restrict the API key to the YouTube Data API; store it in a secret manager.
- [ ] Schedule `trend-agent youtube refresh` and `trend-agent data retention --purge` at
      least daily (Phase 3).
- [ ] Decide, with policy review, whether statistics may be stored for your use case.
- [ ] Obtain Google Trends alpha access (or a licensed provider) before relying on
      search-interest data; set its retention period from the access terms.
- [ ] Prepare branding/attribution and privacy disclosures before any user-facing release.
