# Data contracts

The canonical models live in `src/trend_agent/schemas/`. They are provider-neutral: no
downstream code reads raw provider responses. This document is normative — code and
database constraints implement it, and tests exercise each rule.

## 1. Semantic labels

### MeasurementType — what a value measures

| Value | Meaning | Allowed `search_property` |
|---|---|---|
| `direct_search_interest` | Measured/provider-supplied interest attributable to **YouTube Search** | `youtube_search` only |
| `search_suggestion` | A suggested phrase; discovery signal only, never volume | any |
| `video_activity` | Video metadata/upload/engagement: supply & attention | `not_applicable` |
| `external_search_interest` | Interest on Google Web Search or another non-YouTube property | anything except `youtube_search`, `not_applicable`, `unknown` |
| `unknown` | Origin or meaning not established; never used as demand | any |

The property rules are enforced by validation **and** by `CHECK` constraints.

### ScaleType — how values are scaled

| Value | Range | Comparable across requests? |
|---|---|---|
| `relative_per_request` | 0–100 (max in request = 100) | **No** — only within the same normalisation batch |
| `consistent_index` | ≥ 0 | Yes, if method, `scale_version` and category match (and property/geo/granularity) |
| `absolute_count` | integer ≥ 0 | Yes, if counting method matches |
| `estimated_count` | ≥ 0 | Only within one estimation model (`method` + `scale_version`) |
| `rank` | integer ≥ 1 | Only within the same batch (same list) |
| `presence` | 0 or 1 | Only within the same batch |

### SampleQuality — completeness of one value

| Value | Value column | Meaning |
|---|---|---|
| `complete` | required | Window fully covered |
| `partial_period` | required | Window still in progress at collection; required whenever `observation_end > collected_at` |
| `low_volume` | optional | Provider flagged insufficient volume (e.g. Trends "0"). **Not zero demand.** |
| `suppressed` | must be NULL | Provider withheld the value |
| `missing` | must be NULL | No data returned for the window |

Low, missing and suppressed values are valid observations; they are never silently
dropped or turned into zeros.

### DataNature

`actual`, `sampled`, `estimated`, `synthetic` (on observations); `unavailable` is a
provider-level declaration only and cannot be stored on an observation. `is_synthetic`
must equal `data_nature = 'synthetic'` (DB-enforced). Synthetic records must have a
`synthetic://` source reference, and only providers of type `synthetic` may emit them.

## 2. Time semantics (all UTC, timezone-aware)

| Field | Meaning |
|---|---|
| `observation_start`, `observation_end` | **Event time**: the half-open window `[start, end)` in which the measured activity happened. For instantaneous snapshots (`granularity = point`) start = end. |
| `granularity` | Declared window size; the duration must match (`hour` = 1 h; `day` 23–25 h to allow local-day DST; `week` 7 d ± 1 h; `month` 28–31 d ± 1 h; `year` 365–366 d ± 1 h). |
| `collected_at` | When *we* obtained the value from the provider. |
| `available_at` | When the *provider* made the value available (e.g. Trends' ~2-day lag), if known. Must lie between `observation_start` and `collected_at`. |
| `created_at` | Row insertion time (database default). |

Naive datetimes are rejected, never assumed to be UTC. Collection time is never assumed
to equal observation time. For point-in-time backtesting, a model evaluated "as of" time
T may only use rows with `available_at` (or, if unknown, `collected_at`) ≤ T.

## 3. ObservationCandidate → ValidatedObservation

Providers emit `ObservationCandidate` (loosely typed, so bad data can be diagnosed).
`ingestion.validation.validate_candidate` returns either a `ValidatedObservation` or the
complete list of issues. Rejection reasons (`RejectionReason`):

| Code | Rule |
|---|---|
| `missing_provenance` | provider name/version, `source_reference`, `collected_at` required |
| `provider_mismatch` | record must come from the provider it was ingested through |
| `unrecognized_provider_version` | version must be current or declared compatible |
| `invalid_timestamp` | missing/naive timestamps, `collected_at` in the future, start before 2004-01-01, `available_at` outside `[start, collected_at]` |
| `invalid_window` | end < start; window starts after collection; incomplete window not labelled `partial_period` |
| `granularity_mismatch` | window duration inconsistent with granularity |
| `non_finite_value` | NaN / ±Inf |
| `value_out_of_range` | outside the scale's bounds or non-integer for integer scales |
| `inconsistent_sample_quality` | value present/absent contrary to the quality rules above |
| `invalid_region` / `invalid_language` | ISO 3166-1 alpha-2 (+ ISO 3166-2 subdivisions, `WORLD`) / ISO 639-1 based tags |
| `invalid_query` | empty, > 200 chars, control characters |
| `unsupported_measurement_type` | unknown, or not declared by the provider |
| `mislabeled_search_property` | property rules in §1, or property not declared by the provider |
| `invalid_scale` | unknown scale, or normalisation context disagrees with the record |
| `missing_normalization_context` | absent; or `relative_per_request` without `batch_id` |
| `synthetic_label_mismatch` | synthetic labelling rules in §1 |
| `invalid_enum_value` | unknown granularity/quality/nature, or `unavailable` nature |
| `duplicate_conflict` | records in one batch share an identity but disagree (all are rejected) |
| `conflicts_with_stored` | identity already stored with a different measurement (stored value kept) |
| `malformed_provider_item` | an item in a provider response could not be parsed |

Rejected records are written to `rejected_records` with the reason, a human-readable
detail and a secret-redacted, JSON-safe payload (or no payload when the source's storage
rules forbid it).

## 4. Queries

`canonical_query` = NFKC-normalised, case-folded, whitespace-collapsed text with
zero-width characters removed; punctuation is preserved (`c++` ≠ `c`). The first-seen
`original_query` is kept. Uniqueness: `(canonical_query, language, region)` with NULLs
treated as equal.

## 5. Normalisation context

```text
NormalizationContext(
  scale_type, method, scale_version, batch_id, co_normalized_terms (sorted),
  reference_geography, reference_start, reference_end, granularity,
  search_property, category, notes)
normalization_key = sha256(canonical JSON of the context)
```

Two values from Google-Trends-style `relative_per_request` scales are on a common scale
only if they share the same `normalization_key` — the same request, terms, geography,
window, property and sample. Re-requesting the same window yields a *different* batch and
therefore distinct rows, not overwrites.

## 6. Idempotency

Identity: `(query, provider, measurement_type, search_property, geography, language,
granularity, observation_start, observation_end, normalization_key)`. `collected_at` is
deliberately **not** part of identity: re-collecting the same observation is a no-op.
Outcomes per record: `stored`, `already_present` (identical), `duplicate_in_batch`,
`rejected` (incl. `conflicts_with_stored`), `withheld` (storage not permitted).

The CLI's default ingestion window ends at 00:00 UTC today so that repeated runs on the
same day issue identical requests (and therefore produce identical normalisation batches).

## 7. Comparison rules

`trend_agent.comparability.check_comparability(a, b)` returns `comparable` plus every
reason it is not. Two observations' values may be compared directly only if **all** hold:

1. Same measurement type (and not `unknown`), same search property, same provider.
2. Same data nature (synthetic never mixes with real; sampled never with actual).
3. Same scale type, granularity, geography and language.
4. Same scale: identical `normalization_key` for batch-scoped scales (`relative_per_request`,
   `rank`, `presence`); identical method + `scale_version` + category for
   `consistent_index`/`estimated_count`; identical method for `absolute_count`.
5. Both have values with quality `complete` (not `partial_period`, `low_volume`,
   `suppressed` or `missing`).

`comparability_key(obs)` hashes rules 1–4; `group_by_comparability` partitions
observations into series that may be analysed together (time-sorted). Growth rates,
z-scores or surges must never be computed across groups. `trend-agent data inspect` shows
the groups and warns when there is more than one.

## 8. Video / channel models

`VideoSearchResult` (rank across pages, page number, HTML-decoded snippet),
`VideoDetails` (duration in seconds — `None` if unparseable, `P0D` → 0 for live/upcoming —
plus raw ISO duration, tags, languages, `VideoStatistics`), `ChannelDetails`
(`ChannelStatistics.subscriber_count = None` when hidden). In every statistics model,
`None` means hidden/unavailable and is **never** coerced to 0. All are labelled
`VIDEO_ACTIVITY`.
