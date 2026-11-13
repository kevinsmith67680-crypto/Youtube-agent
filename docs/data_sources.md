# Data sources

This document states, for each source, what it **measures**, what it **does not**, and
under which conditions it may be used. Facts marked *(verify)* could not be re-checked
against Google's documentation during implementation because `developers.google.com` was
blocked by the build environment's network policy; they come from prior knowledge and
search-engine snippets of the official pages and must be confirmed by the operator.

## Summary

| Provider | Capabilities | Measurement type | Status out of the box |
|---|---|---|---|
| `youtube_data_api` | VIDEO_SEARCH, VIDEO_STATISTICS, CHANNEL_METADATA, TREND_DISCOVERY | `VIDEO_ACTIVITY` | `not_configured` until an API key is set |
| `google_trends` | SEARCH_INTEREST | `EXTERNAL_SEARCH_INTEREST` (web) | `not_configured`; `access_restricted` once configured until a verified transport exists |
| `mock` | SEARCH_INTEREST | `DIRECT_`/`EXTERNAL_SEARCH_INTEREST`, always `SYNTHETIC` | `available` |

**No configured source in Phase 1 provides real YouTube search volume or YouTube-specific
search interest.** `DIRECT_SEARCH_INTEREST` rows can currently only come from the mock
provider (labelled synthetic).

---

## YouTube Data API v3 (`youtube_data_api`)

Official, documented API. Endpoints used: `search.list`, `videos.list` (by ID and
`chart=mostPopular`), `channels.list`, `videoCategories.list`. Nothing else is called;
`videos.insert` is never used.

* **Capabilities**
  * Video search: query, `regionCode`, `relevanceLanguage`, `publishedAfter/Before`,
    `order` (relevance, date, rating, title, viewCount), `maxResults` ≤ 50 per page,
    `pageToken` pagination bounded by `TREND_AGENT_YOUTUBE_MAX_PAGES_PER_REQUEST`.
  * Video details in batches of ≤ 50 IDs: title, description, channel, publish time,
    duration, category, tags, languages, thumbnails, public statistics.
  * Channel details in batches of ≤ 50 IDs (only for channels without fresh stored metadata).
  * Most-popular chart per region, optional category.
* **Measurement meaning:** *supply and attention*, not demand. A search result list is a
  ranked sample of matching videos; `pageInfo.totalResults` is an approximate count of
  matching *videos* (stored as `approx_matching_videos_not_search_volume`), not searches.
  View/like/comment counts are attention indicators. Most-popular charts reflect
  YouTube's popularity ranking, not search volume.
* **Hidden data:** hidden like counts, disabled comments and hidden subscriber counts are
  stored as `NULL`, never 0. Videos returned by search but absent from `videos.list`
  (deleted/private) are reported, not fabricated.
* **Freshness:** live at request time. **Granularity:** point-in-time snapshots
  (`POINT`). **Historical data:** none — past statistics cannot be retrieved; history only
  exists if snapshots are collected over time (and retention rules permit storing them).
* **Geography:** `regionCode` is ISO 3166-1 alpha-2; YouTube supports a subset of
  regions (list via `i18nRegions.list`). `regionCode` *biases* search results; it does not
  restrict them to viewers in that region.
* **Quota** (default allocation, configurable; *(verify)* current values in Cloud Console):
  a separate bucket of **100 `search.list` calls/day**, a separate bucket of 100
  `videos.insert` calls/day (unused here), and **10,000 units/day** for other endpoints,
  where `videos.list`, `channels.list` and `videoCategories.list` cost 1 unit per request.
  Reset at **midnight Pacific Time** *(verify)*. Every attempt — including retries and
  invalid requests — is counted locally. Local counts are **estimates**; Google's records
  are authoritative (other processes or apps sharing the key are invisible to us).
* **Known limitations:** search results are capped by the API (roughly a few hundred per
  query) and are personalised/ranked opaquely; `relevanceLanguage` is a hint; search
  snippets are HTML-escaped (decoded here) and descriptions truncated.
* **Credentials:** an API key (`TREND_AGENT_YOUTUBE_API_KEY`). OAuth is not needed for
  public data and is not implemented.
* **Retention:** see [compliance.md](compliance.md). Metadata expires 30 days after
  collection unless refreshed; statistics are not stored by default.

Empirically verified during implementation (no quota consumed): the API accepts the key
via the `X-Goog-Api-Key` header, and returns HTTP 400 `API_KEY_INVALID` for a bad key and
HTTP 403 `forbidden` for a missing key. Those exact bodies are test fixtures.

---

## Google Trends (`google_trends`)

* **Official access:** Google announced an official **Google Trends API in limited-access
  alpha** (July 2025). Access is by application. Its request/response contract is provided
  to admitted testers and was **not available** to this project, so no endpoint or payload
  format has been invented.
* **What the adapter implements:** the canonical mapping from Trends-style points to
  observations — actual granularity, scale type, normalisation context, search property →
  measurement type, and low-volume handling — behind a `TrendsTransport` protocol. An
  operator with alpha access implements the transport against Google's contract and
  passes it to `GoogleTrendsProvider` (see `container.build_container(trends_transport=...)`).
* **Status logic:** `disabled` → `NOT_CONFIGURED`; `official_alpha` without credentials
  file or endpoint → `NOT_CONFIGURED` (listing what is missing); configured but no
  *verified* transport → `ACCESS_RESTRICTED`. In none of these states is any data returned.
* **Publicly announced alpha features** *(verify; not relied upon)*: consistently scaled
  search interest (comparable across requests), daily/weekly/monthly/yearly aggregation,
  ~5-year rolling window up to about two days before the request, regional breakdowns. No
  announced real-time or hourly data. **YouTube-property support is unconfirmed**, so the
  adapter declares only `GOOGLE_WEB_SEARCH` unless a verified transport declares more.
* **Measurement meaning:** relative/indexed interest, never absolute counts. Web search
  interest is labelled `EXTERNAL_SEARCH_INTEREST` and the validator and database both
  refuse to label it as YouTube demand.
* **Normalisation:** Trends website-style values are 0–100 relative to the maximum *within
  one request* and are sampled, so two requests are not directly comparable. Every
  observation carries a `NormalizationContext` (scale type, method, version, batch/sample
  id, co-normalised terms, reference geography/window, property, category) — see
  [data_contracts.md](data_contracts.md).
* **Low / missing / suppressed values:** Google documents a value of 0 as "not enough
  data", so 0 maps to `LOW_VOLUME`, not zero demand. Suppressed and missing values are
  stored as `NULL` with the corresponding `SampleQuality`.
* **Granularity:** whatever the provider actually returns is recorded (a warning is
  emitted if it differs from the request).
* **Credentials:** issued by Google on alpha admission (`..._CREDENTIALS_FILE`,
  `..._ENDPOINT`).
* **Retention:** unknown until the access terms are known. Storage is **denied** unless
  `TREND_AGENT_GOOGLE_TRENDS_RETENTION_DAYS` is set; until then data is counted as
  `withheld`.

### Unofficial Trends endpoints (deliberately not implemented)

Libraries such as `pytrends` and commercial scrapers call the Trends *website's* internal
endpoints. They are undocumented and change without notice, are aggressively rate-limited
and CAPTCHA-protected, return per-request-normalised sampled values, and their use may
breach Google's Terms of Service. Building production functionality on them is out of
scope; bypassing restrictions (proxy rotation, CAPTCHA avoidance) is prohibited by this
project. If a licensed third-party data vendor is used later, implement it as a separate
provider with `ProviderType.LICENSED_THIRD_PARTY` and document its methodology here.

---

## YouTube search suggestions (not implemented — Phase 2 decision)

YouTube's autocomplete suggestions are served by an undocumented endpoint with no
published terms for programmatic use. They would be a `SEARCH_SUGGESTION` discovery signal
(never volume). Whether and how to use them must be decided — with a terms-of-service
review — before Phase 2 relies on them.

---

## Synthetic provider (`mock`)

* Deterministic: output is a pure function of seed, scenario, query, region, granularity,
  property and requested window. Values are normalised per request to 0–100.
* Every record has `data_nature = synthetic`, `is_synthetic = true`, and a
  `synthetic://mock/<scenario>?...` source reference; the CLI prints a SYNTHETIC banner.
* Scenarios: `stable`, `gradual_growth`, `sudden_surge` (onset in the final 7 windows),
  `declining`, `sparse` (suppressed / low-volume), `missing_data` (every 5th window
  missing), `out_of_order`, `duplicates` (identical + conflicting), `mixed_normalization`
  (two independently normalised batches), `invalid_records` (NaN, out-of-range, reversed
  window, invalid region, missing provenance, naive timestamps), `provider_failure`,
  `rate_limited`.
* Granularities: day, week. The window containing "now" is labelled `partial_period`.
* Retention: no source limit (synthetic).
