# Phase 6 architecture — predictive trend forecasting & opportunity windows

Phase 6 turns the monitored time series (Phase 3), the causal detector state (Phase 4)
and the content-gap analyses (Phase 5) into **typed, calibrated, abstaining forecasts**:
how likely relative interest is to break out within 6 / 24 / 72 h, where it is likely to
be, when it may peak inside a window, and whether a creator with a given production time
could plausibly publish while interest is still elevated.

Priorities, in order: statistical validity, honest uncertainty, causal evaluation and a
low false-positive rate. Model complexity comes last. Everything runs offline and
deterministically on synthetic data. **No real-world accuracy is claimed**: no real
held-out YouTube Search data exists in this environment.

## Data flow

```
Phase 3 ts_series / ts_observations ─┐        (publication/availability times, revisions)
Phase 2 keywords / entities ─────────┤
Phase 5 content_analysis_runs/gaps ──┤        ForecastingService.bundles()  (read-only)
                                     ▼
                          SeriesBundle (target series + supporting series + content events)
                                     │
             ┌───────────────────────┴────────────────────────┐
             ▼  training (scheduled, heavy)                    ▼  inference (frequent, light)
   dataset_builder.build_dataset                        prediction.ForecastEngine.forecast
     feature_store (point-in-time views,                   same feature code at the issue
       causal DetectorFeed)                                time T (on the hour), eligibility,
     target_definition (independent labels)                champion lookup per scope/origin,
     feature_engineering (versioned features)              calibration, OOD, intervals,
     leakage audit + manifest                              peaks, windows, explanation
             │                                                  │
   model_selection.train_and_register                    ForecastOutput (forecast/1.0)
     evaluation (chronological splits, embargo,                 │
       unseen groups, rolling origins, bootstrap)        ForecastRepository.save (idempotent)
     training (climatology, logistic, LightGBM)                 │
     trajectory (persistence, seasonal naive,            forecast_predictions + trajectories,
       damped trend, LightGBM regressor)                 peak_estimates, opportunity_windows
     calibration, uncertainty (conformal, OOD)
     peak_estimation (analog model)                      drift (data vs concept; never
     acceptance checks → VALIDATED / EXPERIMENTAL          automatic) · replay_adapter
             │                                             (Phase 3 replay, walk-forward)
   model_registry (checksummed JSON/LightGBM text)
     explicit promotion → CHAMPION; events
```

## Modules (`src/trend_agent/forecasting/`)

| module | responsibility |
|---|---|
| `config.py` | strict, versioned configuration (`config/forecasting.yaml`), hashed into every dataset, model and forecast |
| `feature_store.py` | the leakage boundary: `PointInTimeSeries` (what the series looked like at T), `DetectorFeed` (causal Phase 4 engine), bundles and content events |
| `target_definition.py` | independent labels (breakout, continuation, trajectory, peak, persistence at leads) with maturity and coverage rules |
| `feature_engineering.py` | 52 versioned features with units, sources and availability rules |
| `eligibility.py` | which series can be forecast for which horizon, and why not |
| `dataset_builder.py` | causal datasets, manifests, leakage audit, parallel construction |
| `evaluation.py` | chronological splits with embargo, unseen groups, rolling origins, metrics, block bootstrap |
| `baselines.py` | climatology, seasonal naive, damped trend |
| `training.py` | logistic regression and LightGBM classifiers, calibration, acceptance checks |
| `trajectory.py` | trajectory baselines and LightGBM regressor (training) and trajectory points (inference) |
| `calibration.py` | isotonic / Platt calibration with explicit statuses |
| `uncertainty.py` | split-conformal intervals, out-of-distribution reference |
| `peak_estimation.py` | experimental analog model: peak timing with censoring, elevated-at-lead shares |
| `opportunity_window.py` | production-profile windows and their classification |
| `model_registry.py` | metadata + checksummed artifacts, explicit promotion, events |
| `model_selection.py` | training orchestration, registration, champion–challenger |
| `prediction.py` | inference engine and the `ForecastOutput` assembly |
| `explainability.py` | deterministic, evidence-backed explanations (no LLM) |
| `drift.py` | data drift (PSI), concept drift (matured labels), conservative recommendations |
| `replay_adapter.py` | Phase 3 `ReplayConsumer`: causal walk-forward forecasting and scoring |
| `synthetic.py`, `scenarios.py` | deterministic synthetic corpus and scenarios A–M |
| `service.py` | application service (database access, training runs, predictions, drift) |

Contracts: `schemas/forecast.py` (`ForecastOutput`, statuses, peaks, windows,
explanations), `schemas/forecast_features.py` (feature specs), `schemas/model_metadata.py`
(registry metadata). CLI: `cli_forecast.py` (`trend-agent forecast …`).

## Database (migration `0007`)

| table | content |
|---|---|
| `forecast_training_runs` | one row per training run: source, data origin, horizons, configuration and feature-schema versions, manifest (dataset version, rows hash, leakage audit), metrics, status |
| `forecast_models` | registry: full metadata, artifact location and SHA-256, status; a partial unique index allows at most one CHAMPION per scope |
| `forecast_model_events` | every status change (registered, validated, champion, challenger, retired) with reason and evidence |
| `forecast_predictions` | one row per forecast: series/target/query links, issue time, data cutoff, horizon, family, source classification, status, probabilities/scores, calibration status, uncertainty, quality, features, the complete `ForecastOutput`, synthetic flag, expiry; unique per (series, models, issue time, horizon) |
| `forecast_trajectories` | trajectory points with intervals |
| `peak_estimates` | peak status, window, earliest/latest/median offsets, P(beyond window), censoring and confidence status |
| `opportunity_windows` | profile, production hours, earliest publication, status, probability and interval |
| `forecast_evaluations` | held-out test, rolling-origin and re-evaluation metrics with slices |

Writes are idempotent: the prediction id is a UUIDv5 of (series, issue time, horizon,
models). Re-running within the same hour returns identical forecasts and stores nothing
new. Forecasts expire with the earliest-expiring observation of their baseline window
(`trend-agent data retention --purge`), and their trajectories, peaks and windows
cascade. Training never writes observations, and inference only reads them.

## Separation of measurement types

Target families are never pooled and never relabelled: a direct YouTube Search series is
`youtube_search_interest`, an external web-search series is `external_search_interest`,
and video/community activity is `proxy_attention`, explicitly *not* search demand.
Official event signals are features only. Synthetic data is always flagged, trained only
into `data_origin = synthetic` models, and those models serve only synthetic series.

## Phase 7 contract

Phase 7 consumes `ForecastOutput` (`schema_version = forecast/1.0`; JSON schema via
`trend-agent forecast schema`). Every forecast carries its status and reasons, source
classification, proxy and synthetic flags, issue time and data cutoff, horizon, breakout
or continuation probability with calibration status, trajectory with interval, peak
estimate, opportunity windows, data quality, uncertainty (OOD status, calibration support,
historical reliability), a deterministic explanation, the feature values used, model
references and version strings. A consumer must check `status`, `synthetic` and
`is_proxy` before using any number.

## Performance (measured)

Measured with `scripts/benchmark_forecasting.py` on 4 CPUs (no GPU). Full numbers:
[`docs/benchmarks/phase6_benchmark.json`](benchmarks/phase6_benchmark.json); summary in
[phase6_report.md §K](phase6_report.md#k-tests-linting-typing-and-benchmark-results).
Training is the heavy part: about 30 s of dataset construction with 4 processes plus
45 s of training for a 180-series corpus. Inference costs about 160 ms per hourly series
for all horizons, dominated by the strict causal detector replay. A database batch of
1,000 series × 3 horizons with persistence takes about 2 minutes. Training is never
triggered by new observations.
