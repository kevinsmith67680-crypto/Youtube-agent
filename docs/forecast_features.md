# Forecast features (Phase 6)

Feature schema `features-1.0.0` (`src/trend_agent/forecasting/feature_engineering.py`,
contract types in `src/trend_agent/schemas/forecast_features.py`). The machine-readable
schema, with each feature's unit, source, availability rule and missing-value policy, is
printed by

    trend-agent forecast schema --which features

Every model stores the exact feature names it was trained with. A model whose feature
list differs from the running schema is never used, and the scope falls back to baselines.

## Point-in-time rules (leakage safeguards)

| rule | implementation |
|---|---|
| Only data available at T | `PointInTimeSeries.at(T)` uses the Phase 3 availability time (provider publication time, else the later of collected/ingested time) and the Phase 4 revision rule: the newest revision *available at T*. Later revisions and late arrivals stay invisible until they were published. |
| Actual timestamps | Growth, slopes and windows use period end times. Irregular sampling is not treated as equally spaced (Theil–Sen slopes per hour, window means need ≥ 50 % observed periods). |
| Causal detector state | `DetectorFeed` replays the Phase 4 `SeriesEngine` over the visible data. On revision-free series with in-order availability, one causal pass gives the same evaluations as re-running at every T (Phase 4: incremental = recomputed). Otherwise, and always at inference, the detector is re-run from the data visible at T (strict path). |
| Supporting series | External search, video and community activity and official event signals are read through their own point-in-time views. Level ratios between two series are computed only if Phase 3's `assess_pair` rates them comparable; a rejected comparison is recorded in `rejected` and the feature stays NaN. |
| Phase 5 content analyses | A content-gap analysis is visible only from its `completed_at` time. |
| Catalog facts | Keyword/entity first-seen times come from Phase 2 discovery records. Without them, novelty features are NaN, never 0. |
| Missing values | Missing inputs give NaN (`missing_policy = nan`). Models handle NaN natively (LightGBM) or through median fill plus explicit missingness indicators (logistic regression). No zero imputation. |
| Scale-dependent features | Absolute index levels/differences (`scale_dependent = yes`) are disabled (NaN) for per-request relative scales, where 50 in one request is not 50 in another. |
| Bounded ages | Age features are `log1p(hours)` capped at 720 h, so a model never extrapolates on ever-growing ages. |
| Labels are never features | Label names and feature names are disjoint (tested). A feature that leaked label-window information (`withheld_periods`) was caught by the leakage audit during development and removed. |

### Automated leakage checks

* **Leakage audit in every dataset manifest.** Sampled rows are recomputed from the
  bundle *truncated at T* (every observation, revision, supporting point and content
  event available after T removed), and every feature must be identical. A mismatch is
  reported in `manifest.leakage_audit` (training runs record it; all runs so far:
  0 mismatches).
* **Unit tests.** Perturbing every value published after T leaves all features
  unchanged; revisions become visible only from their own publication time; a delayed
  value (published two days late) is invisible until published; the replay adapter's
  forecasts equal forecasts made from the truncated data (`tests/unit/forecasting/`).

## Measurement separation

Target families are never pooled (`TargetFamily`):

| measurement | family | forecast as |
|---|---|---|
| DIRECT_YOUTUBE_SEARCH_INTEREST | youtube_search_interest | YouTube Search demand (relative index) |
| EXTERNAL_SEARCH_INTEREST | external_search_interest | external web-search interest; never presented as YouTube demand |
| VIDEO_ACTIVITY, COMMUNITY_ACTIVITY | proxy_attention | **proxy attention**, explicitly not search demand |
| OFFICIAL_EVENT_SIGNAL | not a target | supporting feature only (`official_event_age_h`) |
| SYNTHETIC_TEST_DATA | as simulated | always flagged `synthetic`; trained only into synthetic models |

Supporting series contribute *within-series* changes (e.g. `video_change_24h_log`) of
their own measurement. Every forecast carries `source_classification`, `is_proxy`,
`target_family` and `synthetic`, and the explanation names the data type.

## Feature reference

### Growth

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `level_vs_baseline_log` | log_ratio | log((level+1)/(B+1)); level = median of the last p values, B = trailing median | target-series observations available at T (publication time) | no |
| `change_6h_log` | log_ratio | log ratio of the last value to the value 6 h earlier (hourly data only) | target-series observations available at T (publication time) | no |
| `change_24h_log` | log_ratio | log ratio of the last value to the value 24 h earlier | target-series observations available at T (publication time) | no |
| `change_72h_log` | log_ratio | log ratio of the last value to the value 72 h earlier | target-series observations available at T (publication time) | no |
| `change_168h_log` | log_ratio | log ratio of the last value to the value 168 h earlier | target-series observations available at T (publication time) | no |
| `abs_change_24h` | index_points | last value minus the value 24 h earlier | target-series observations available at T (publication time) | yes |
| `slope_recent_log_per_h` | log_per_hour | Theil-Sen slope of log(y+1) over the recent half of the growth window | target-series observations available at T (publication time) | no |
| `growth_persistence` | share | share of increases among the last 6 consecutive changes | target-series observations available at T (publication time) | no |
| `window_mean_change_log` | log_ratio | log ratio of the mean of the last growth window (24 h / 7 d) to the window before it: free of the daily cycle | target-series observations available at T (publication time) | no |
| `window_growth_persistence` | share | share of rising window-over-window steps among the last three windows | target-series observations available at T (publication time) | no |
| `threshold_gap_log` | log_ratio | log((threshold+1)/(level+1)): distance to the breakout level | target-series observations available at T (publication time) | no |
| `level_index` | index_points | current level (consistently scaled indices only) | target-series observations available at T (publication time) | yes |

### Acceleration

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `slope_prior_log_per_h` | log_per_hour | Theil-Sen slope over the earlier half of the growth window | target-series observations available at T (publication time) | no |
| `accel_log_per_h2` | log_per_hour2 | (recent slope - prior slope) / half-window hours (actual timestamps) | target-series observations available at T (publication time) | no |

### Volatility

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `baseline_mad_rel` | share | 1.4826*MAD / (B+1) over the baseline window | target-series observations available at T (publication time) | no |
| `fluctuation_log` | log_ratio | mean \|change of log(y+1)\| between consecutive observations in the growth window | target-series observations available at T (publication time) | no |
| `noise_to_signal` | ratio | spread / (\|level - B\| + spread + 1) | target-series observations available at T (publication time) | no |

### Surge

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `det_anomaly_z` | z_score | robust anomaly z of the last visible period | Phase 4 detector state replayed on observations available at T | no |
| `det_cusum_ratio` | ratio | CUSUM statistic / alarm threshold | Phase 4 detector state replayed on observations available at T | no |
| `det_changepoint_prob` | probability | Bayesian online change-point posterior of a recent change | Phase 4 detector state replayed on observations available at T | no |
| `det_run_length_h` | log1p_hours | log1p of the current segment length in hours, capped at 30 days | Phase 4 detector state replayed on observations available at T | no |
| `det_elevated_streak_h` | hours | consecutive elevated periods (detector persistence) x period | Phase 4 detector state replayed on observations available at T | no |
| `det_triggers_window` | count | detector triggers among the periods of the growth window | Phase 4 detector state replayed on observations available at T | no |
| `det_baseline_ready` | bool | 1 if the detector baseline was ready | Phase 4 detector state replayed on observations available at T | no |

### Seasonality

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `hour_of_day` | hour | UTC hour of the last observation (hourly data only) | target-series observations available at T (publication time) | no |
| `day_of_week` | day | UTC weekday of the last observation (0 = Monday) | target-series observations available at T (publication time) | no |
| `dow_sin` | cyclic | sin(2*pi*weekday/7) of the last observation (weekly position, cyclic) | target-series observations available at T (publication time) | no |
| `dow_cos` | cyclic | cos(2*pi*weekday/7) of the last observation | target-series observations available at T (publication time) | no |
| `hod_sin` | cyclic | sin(2*pi*hour/24) of the last observation (hourly data only) | target-series observations available at T (publication time) | no |
| `hod_cos` | cyclic | cos(2*pi*hour/24) of the last observation (hourly data only) | target-series observations available at T (publication time) | no |
| `seasonal_ratio_log` | log_ratio | log ratio of the last value to the same phase one season earlier (24 h / 7 d) | target-series observations available at T (publication time) | no |

### Novelty

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `series_age_h` | log1p_hours | log1p hours since the first visible observation, capped at 30 days | target-series observations available at T (publication time) | no |
| `keyword_age_h` | log1p_hours | log1p hours since the keyword was first seen (Phase 2, only if before T), capped | keyword first_seen_at <= T | no |
| `entity_age_h` | log1p_hours | log1p hours since the entity was first seen (Phase 2, only if before T), capped | entity first_seen_at <= T | no |

### Attention

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `ext_change_24h_log` | log_ratio | 24 h log change of external search interest for the same query | supporting observations available at T | no |
| `video_change_24h_log` | log_ratio | 24 h log change of video activity for the same query (proxy, not demand) | supporting observations available at T | no |
| `community_change_24h_log` | log_ratio | 24 h log change of community activity (proxy, not demand) | supporting observations available at T | no |
| `official_event_age_h` | hours | hours since the latest official event signal available at T | official event observations available at T | no |
| `cross_level_ratio_log` | log_ratio | log ratio of target and external levels - only if Phase 3 rates them comparable | comparable supporting observation available at T | no |

### Content gap

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `gap_strength_ord` | ordinal | 0 none/insufficient, 1 weak, 2 moderate, 3 strong (latest analysis before T) | analysis completed <= T | no |
| `gap_is_opportunity` | bool | 1 if the latest analysis before T found an opportunity gap | analysis completed <= T | no |
| `content_age_h` | hours | hours since the latest analysis completed before T | analysis completed <= T | no |

### Quality

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `missing_rate` | share | share of expected periods without a value in the baseline window | target-series observations available at T (publication time) | no |
| `staleness_h` | hours | hours between T and the end of the last visible period | target-series observations available at T (publication time) | no |
| `granularity_h` | hours | observation period length | series metadata | no |
| `normalization_stable` | bool | 1 if every visible value shares one normalization context | target-series observations available at T (publication time) | no |
| `revision_share` | share | share of visible values that are revisions | target-series observations available at T (publication time) | no |
| `complete_share` | share | share of visible values marked complete | target-series observations available at T (publication time) | no |
| `baseline_obs` | count | observations in the baseline window | target-series observations available at T (publication time) | no |

### Context

| feature | unit | definition | available from | scale-dependent |
|---|---|---|---|---|
| `cat_ai` | bool | category is AI | series metadata | no |
| `cat_technology` | bool | category is technology | series metadata | no |
| `cat_gaming` | bool | category is gaming | series metadata | no |
