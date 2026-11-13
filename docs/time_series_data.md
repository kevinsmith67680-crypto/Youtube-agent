# Time-series data

## Observation schema (`ts_observations`)

| Field | Meaning |
|---|---|
| `id`, `series_id`, `target_id`, `query_id` | identity and links |
| `provider_name`, `provider_version` | source and the provider adapter version |
| `measurement_category` | `direct_youtube_search_interest`, `external_search_interest`, `search_suggestion_presence`, `video_activity`, `official_event_signal`, `community_activity`, `synthetic_test_observation` |
| `simulated_category` | for synthetic rows only: what the row imitates |
| `is_proxy` | event/community/suggestion signals; never search interest |
| `unit` | `index`, `relative_0_100`, `count`, `items`, `presence`, … |
| `dedup_key`, `provider_observation_id` | deterministic dedup key (series + window); provider id when given |
| `period_start`, `period_end`, `granularity` | observation window (half-open) |
| `event_time` | for point events (e.g. a release) |
| `source_timezone` | provider's own time zone when it matters for the window |
| `provider_published_at` | when the provider published this value; NULL when unknown (never invented) |
| `first_available_at` | earliest known availability of the value (e.g. first revision's publication) |
| `collected_at` | when this system fetched it |
| `ingested_at` | when this system stored it (≥ `collected_at`, enforced) |
| `value`, `raw_value` | value (NULL for missing/suppressed); raw value when a transform applies |
| `scale_type`, `normalization_key` (+ context on `ts_series`) | scale definition and normalisation context |
| `region`, `language`, `search_property` | geography, language, search surface |
| `data_nature` | sampling: `actual`, `sampled`, `estimated`, `synthetic` |
| `sample_quality` | missing/suppression status: `complete`, `partial_period`, `low_volume`, `suppressed`, `missing` |
| `quality_status` | `valid`, `caveat` (partial, low volume, unknown publication time), `conflicting` |
| `source_reference`, `data_policy_id`, `job_id` | provenance: source, policy version, collecting job (→ backfill run) |
| `revision`, `revision_kind`, `supersedes_id`, `is_current`, `superseded_at` | revision lineage |
| `last_confirmed_at`, `confirm_count` | duplicate fetches of an unchanged value |
| `expires_at` | policy-driven expiry |
| `is_synthetic` | must equal `measurement_category = synthetic_test_observation` (CHECK) |

All timestamps are timezone-aware UTC; naive timestamps are rejected.

## Time semantics

Five different times are stored and never substituted for each other:

```
 period_start ── period_end          (what the value describes: event-time window)
                      │ provider publication delay
                      ▼
               provider_published_at (when the world could know it; unknown → NULL)
                      │ our polling lag
                      ▼
                 collected_at → ingested_at   (when this system knew it)
```

Example (synthetic provider, 36 h delay): Monday's value (window Mon 00:00–Tue 00:00) is
published Wednesday 12:00, collected Wednesday 12:00 and stored a moment later.

## Writes: new, unchanged, revised, conflicting, missing

One series = one scale (provider, measurement, simulated category, property, region,
language, granularity, scale type, unit, normalisation key). For each (series, window):

| Incoming value vs stored current value | Result | Stored rows |
|---|---|---|
| nothing stored | `NEW` | revision 1, current |
| identical value and quality | `UNCHANGED` | none; `confirm_count`/`last_confirmed_at` updated |
| different; source revisable (or the stored value was a partial period) | `REVISED` | new revision becomes current; the old one keeps its values with `superseded_at` |
| different; source declared immutable | `CONFLICTING` | non-current `conflict` row linked to the original; original stays current |
| missing/suppressed | `NEW`/`REVISED` | `value` NULL with that `sample_quality` |

Historical knowledge is never overwritten. Uniqueness constraints (series, window,
revision) and a partial unique index (one current row per series and window) make
concurrent or repeated writes idempotent: a lost race re-reads and compares. A NULL value
is never zero: CHECK constraints refuse NULL for `complete`/`partial_period` and refuse a
value for `missing`/`suppressed`. Expected periods that were never returned are not stored
at all. They appear as gaps in readiness and quality reports.

## Normalisation contexts and comparability

Each series carries its `NormalizationContext` (scale type, method, version, batch id,
co-normalised terms, reference geography/window, granularity, property, category). Its
hash is the series' `normalization_key`:

* **consistent index** (e.g. Google Trends API alpha as announced, synthetic provider): the
  key excludes the request window, so successive requests extend one series;
* **per-request 0–100** (website-style Trends, the Phase 1 mock): the batch id and request
  window are part of the key, so every request is its own series and series are never
  stitched together.

`monitoring/comparability.py` answers "may these two values be compared directly?":

| Status | When |
|---|---|
| `comparable` | same provider, effective measurement, property, region, language, granularity, unit, scale type and normalisation key; both have values |
| `comparable_with_caveats` | as above, but `partial_period`, `low_volume`, revised value, conflict, or provider version changed |
| `incompatible` | any of: `different_provider`, `different_measurement_category`, `different_search_property`, `different_region`, `different_language`, `different_granularity`, `different_unit`, `different_scale_type`, `different_normalization_context` (per-request batches), `different_scale_definition` (method/version), `synthetic_vs_real`, `proxy_vs_search_interest`, `no_value` |
| `insufficient_metadata` | `missing_normalization_context`, `missing_batch_id` |

Not done, ever: converting Google web interest into YouTube interest; combining
independently normalised series; combining video activity with search interest;
interpolation or resampling. If a transform is added later it must be explicit, versioned
(new scale method/version and therefore a new series) and documented here.

## Freshness

`monitoring/freshness.py` estimates each provider's publication delay from observed
behaviour: exact samples (`published − period_end`) when the provider states publication
times; collection-time upper bounds otherwise; the declared profile delay until there are
three exact samples. Estimates are reported as estimates (`p50`, `p90`, basis, sample
counts). Target freshness:

| Status | Meaning |
|---|---|
| `source_current` | the latest period the source should have published is stored (or the target is behind only by its planned batching, e.g. a low tier collecting 7 days per request — the reason says so) |
| `collection_delayed` | data should be out, but we have not checked since |
| `provider_delayed` | we checked after the expected time plus p90 delay; still not published |
| `no_new_observation` | checked; nothing new yet, within the normal delay range |
| `source_unavailable` | provider not configured, not permitted, or unhealthy |
| `unknown_freshness` | no data yet, or the source has no regular cadence |

## Baseline readiness (Phase 4 gate)

`monitor readiness` evaluates every series against a configurable profile
(`readiness.profiles`, selectable per provider):

| Profile | Granularity | Min observations | Min span | Min coverage | Max staleness |
|---|---|---|---|---|---|
| `short_term` | day | 14 | 14 days | 80 % | 3 periods |
| `weekly_seasonal` | day | 56 | 56 days | 85 % | 3 periods |
| `weekly_trend` | week | 12 | 84 days | 80 % | 2 periods |

It reports comparable observations, span, expected periods, coverage, missing periods,
missing values, gap distribution, granularity, freshness and the state: `ready`,
`partially_ready`, `insufficient_history`, `incompatible_history` (history split across
incompatible normalisation contexts or wrong granularity: not combined), `stale_history`
or `source_unavailable`, each with the exact unmet requirements. Proxy series are flagged
`usable_for_search_demand: false`. No surge score is computed.

## Data quality

`monitor quality` reports findings with severity at series, target, provider and category
level: missing periods, unexpected gaps, missing values (provider-reported, not zero),
low-volume values (a data property), duplicate records in responses, out-of-order
arrival, stale series/provider, invalid timestamps, incompatible scales, normalisation
changes, provider version changes, suspected schema changes, constant values (only when
the source does not legitimately repeat values), frequent revisions, conflicts, missing
provenance, authentication failures and provider error rates. Source failures and
audience behaviour are kept apart: nothing is read as falling demand.

## Source-specific limitations

| Source | What it is | Limits |
|---|---|---|
| Google Trends API (alpha) | external (Google web) search interest, consistently scaled, daily/weekly/monthly/yearly, ~1,800 days history, ~48 h delay (public descriptions) | limited access; no transport shipped; YouTube property unconfirmed; storage denied until a retention period is configured; **not configured here** |
| Phase 1 mock | synthetic, website-style per-request scaling | every request is its own series |
| synthetic_trends / _web | Phase 3 fixtures (consistent / per-request) | synthetic; registered only by the demo/tests |
| discovery_evidence | daily counts of official/community items and suggestion presence linked to a keyword | proxy only; days without items are unknown; YouTube-sourced items excluded unless derived metrics are permitted; expires with its source items |
| YouTube Data API | video metadata/statistics | no search-interest data; monitoring statistics over time is restricted (derived metrics) → collection disabled |
