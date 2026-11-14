# Phase 6 report — predictive trend forecasting & opportunity-window estimation

Version 0.6.0. Phase 7 was not started.

> **Simulated verification, not production readiness.** No real monitored YouTube Search
> or Google Trends history exists in this environment, and no live API was called. Every
> model was trained and evaluated on deterministic **synthetic** data, isolated as
> `data_origin = synthetic`. The pipeline's behaviour is verified; **real-world forecasting
> accuracy is unknown** and is not claimed.

## A. Implementation summary

Phase 6 adds `src/trend_agent/forecasting/`, a forecasting subsystem that, for every
monitored series and each supported horizon (6 / 24 / 72 h, configurable), produces a typed
`ForecastOutput` (`forecast/1.0`) with:

* **breakout** probability (calibrated, with an interval) when interest is not elevated,
  or **continuation** probability when it is;
* **trajectory**: the index value at T + H of the same source, with a split-conformal 80 %
  interval;
* **peak timing** inside a 72-hour window as a range, with explicit right-censoring and
  `PEAK_NOT_ESTIMABLE` (experimental);
* **opportunity windows** for explicit production profiles (QUICK_REACTION 2 h,
  STANDARD_TUTORIAL 8 h, DEEP_DIVE 24 h): URGENT / TIMELY / LONGER_WINDOW /
  LIKELY_TOO_LATE / UNKNOWN (experimental);
* a typed **status** that abstains when needed (READY, LIMITED_CONFIDENCE, BASELINE_ONLY,
  INSUFFICIENT_HISTORY, UNSUPPORTED_GRANULARITY, SOURCE_UNAVAILABLE, OUT_OF_DISTRIBUTION,
  FORECAST_UNAVAILABLE), data quality, uncertainty, a deterministic explanation, the
  features used and version strings.

Training is separate from inference and runs as scheduled jobs, never per observation.
It covers causal datasets with a leakage audit; baselines, logistic regression and
LightGBM on chronological, embargoed splits with unseen topics; calibration; explicit
acceptance checks; a checksummed registry; and promotion only by an explicit, logged
operation. Inference only loads champions. Drift monitoring recommends but never acts.

Problems found and fixed during this phase's own testing (all covered by tests now):
a feature that leaked label-window information (caught by the leakage audit);
label-time matching with a relative tolerance (periods ~5 h apart matched); labels for
windows running past the data; datasets built "as of" a date seeing later data; model
selection by test-period scores; forecast identity tied to the data cutoff, which made
same-data forecasts at different times collide and differ; matured-label scoring
measured from the data cutoff instead of the issue time; a stale source reported as
"insufficient history"; inference crashing on a tampered artifact; fork-based worker
processes (now forkserver/spawn, with a sequential fallback); a promotion step that,
re-run with stale statuses, could have demoted a champion; continuation models named as
breakout models; and training examples missing their query/entity/topic/region and
availability-cutoff columns.

## B. Updated repository structure

```
config/forecasting.yaml                    Phase 6 operating point (hashed into everything)
migrations/versions/20261113_0007_forecasting.py
src/trend_agent/forecasting/               23 modules (see docs/phase6_architecture.md):
  config, feature_store, target_definition, feature_engineering, eligibility,
  dataset_builder, evaluation, baselines, training, trajectory, calibration, uncertainty,
  peak_estimation, opportunity_window, model_registry, model_selection, prediction,
  explainability, drift, replay_adapter, synthetic, scenarios, service
src/trend_agent/schemas/forecast.py        ForecastOutput contract and statuses
src/trend_agent/schemas/forecast_features.py, schemas/model_metadata.py
src/trend_agent/db/forecast_models.py      8 ORM tables
src/trend_agent/db/repositories/forecast_registry.py, forecasts.py
src/trend_agent/cli_forecast.py            `trend-agent forecast …` (17 commands)
scripts/benchmark_forecasting.py, scripts/evaluate_forecasting.py
tests/unit/forecasting/ (7 modules), tests/integration/test_forecasting.py
docs/: phase6_architecture, forecast_features, forecast_targets, model_training,
       forecast_accuracy, forecast_uncertainty, opportunity_windows, phase6_report;
       benchmarks/phase6_evaluation.json, phase6_benchmark.json
```

Modified: `cli.py` (registers `forecast`), `container.py` (`forecasting()`), `config.py`
(`TREND_AGENT_FORECAST_ARTIFACT_DIR`), `db/models.py`, `ingestion/retention.py` (forecasts
expire with their sources), `pyproject.toml` (scikit-learn, LightGBM; version 0.6.0),
`.env.example`, `.gitignore` (`var/`), test fixtures, README.

## C. Database migrations

Migration `0007` (down-revision `0006`; upgrade, downgrade and `alembic check` verified)
adds `forecast_training_runs`, `forecast_models` (partial unique index: at most one
CHAMPION per scope), `forecast_model_events`, `forecast_predictions` (unique per series,
models, issue time and horizon; indexes on series/cutoff, query/time, status and expiry),
`forecast_trajectories`, `peak_estimates`, `opportunity_windows` (all three cascade from
their forecast) and `forecast_evaluations`. Writes are idempotent: the prediction id is a
UUIDv5 of (series, issue time, horizon, models), and a second run in the same hour stores
nothing. Forecasts carry the expiry of the earliest-expiring observation they used and are
purged by `trend-agent data retention --purge`.

## D. Forecasting algorithms and model selection

* **Targets** ([forecast_targets.md](forecast_targets.md)), independent of the detector:
  trailing-median baseline (7 d hourly / 28 d daily); threshold θ = B + max(0.5B, 10 index
  points on bounded indices, 2 robust SD); breakout = p consecutive future periods ≥ θ
  (p = 3 hourly, 1 daily); continuation = the last quarter of the window still ≥ θ;
  trajectory = log change of the index; peak offset with censoring; labels only for
  matured, ≥ 75 % observed windows.
* **Candidates** ([model_training.md](model_training.md)): climatology; logistic
  regression (balanced, standardised, missingness indicators, C on validation log loss);
  LightGBM (3-point conservative grid, early stopping, √-imbalance weight); trajectory
  persistence, seasonal naive, damped trend and a LightGBM Huber regressor; an analog
  model for peaks and persistence at publication leads.
* **Splits**: by prediction time, train / validation / calibration / test with embargo =
  horizon + 48 h, 20 % unseen topic groups, 3 rolling origins.
* **Acceptance** (on the untouched test segment): ≥ 200 examples, ≥ 20 positives,
  calibrated, ECE ≤ 0.08, beats climatology with a series-block bootstrap interval
  excluding zero and Brier skill ≥ 0.02. LightGBM additionally must beat logistic
  regression with a bootstrap interval excluding zero, without losing precision@50. The
  overfitting gap is reported.
* **Selection** is a fixed rule (LightGBM only if validated, else logistic). Test scores
  never choose between candidates.

## E. Feature engineering and leakage prevention

52 versioned features ([forecast_features.md](forecast_features.md)) in growth,
acceleration, volatility, Phase 4 detector state, seasonality, novelty, attention, Phase 5
content-gap, quality and context groups. Each has a unit, source, availability rule and
missing-value policy (NaN, never zero). Safeguards: point-in-time views on publication
times and revisions; a causal detector replay (strict path at inference); comparability
checks before any cross-series ratio; content analyses only from their completion time;
scale-dependent features disabled for per-request relative indices; capped ages; label
and feature names disjoint. Every dataset manifest contains a **leakage audit** that
recomputes sampled rows from data truncated at T. All runs so far: 0 mismatches (e.g.
0/6 on the 72,098-row evaluation corpus). Unit tests perturb future values, delay
publications, revise values and replay causally.

## F. Training, validation and calibration results (synthetic)

Full corpus: 360 synthetic series, 72,098 examples
([forecast_accuracy.md](forecast_accuracy.md), `docs/benchmarks/phase6_evaluation.json`).
Of 38 registered models, 13 are champions (synthetic scopes), 8 validated and 17 experimental.

* Breakout champions exist for every supported scope (hourly 6/24/72 h, daily 24/72 h).
  All beat climatology on their untouched test segment with bootstrap intervals
  excluding zero, with ECE 0.001–0.008 and false-positive rates of 0.3–3 % at the
  calibrated threshold.
* Continuation: only hourly 6 h validated. The others abstain (too few elevated states:
  149–189 test rows for hourly 24/72 h, 201–215 training rows for daily).
* Trajectory: LightGBM beats the reference baseline in all five scopes (scaled MAE
  0.43–0.92). Observed 80 % interval coverage is 0.79–0.82.
* Calibration: isotonic where supported; ECE before/after and bin support are stored
  with each model. Scenario L shows that with too few labels the calibrator refuses
  (INSUFFICIENT_CALIBRATION_DATA) and no probability is reported.
* **Held-out corpus** (new seed, unseen series): five of six champions keep positive skill
  against the corpus's own base rate (0.03–0.28). **The daily 24 h logistic champion does
  not** (skill −0.07 on 38 positives), even though it passed every acceptance check. One
  held-out period is therefore treated as insufficient proof.
* Gaming is essentially unpredictable at 24/72 h in this corpus (slice skill ≈ 0): no
  run-up precedes limited-time events. The models stay near the base rate there rather
  than alerting.

## G. Baseline versus LightGBM comparison

| scope | Δ Brier LightGBM − logistic [90 % CI] | selected |
|---|---|---|
| hourly 6 h | −0.0000 [−0.0005, 0.0004] | logistic |
| hourly 24 h | −0.0019 [−0.0029, −0.0010] | LightGBM |
| hourly 72 h | −0.0025 [−0.0048, −0.0001] | LightGBM |
| daily 24 h | −0.0005 [−0.0014, 0.0004] | logistic |
| daily 72 h | −0.0029 [−0.0054, −0.0004] | LightGBM |

LightGBM is used only where it is significantly better. Its PR-AUC gain there is
0.04–0.10 and precision@50 is not worse (e.g. hourly 72 h: 0.66 vs 0.40). For
trajectories, LightGBM has a lower MAE than persistence and seasonal naive in every
scope. That is significant (bootstrap) against the reference baseline it is tested
against; against the other baseline it was not separately tested. The damped-trend
extrapolation is the worst baseline (scaled MAE up to 3.3). Scenario I shows
an overfitting LightGBM (in-sample PR-AUC far above held-out) that is rejected, with the
baseline retained.

## H. Synthetic AI, technology and gaming demonstrations

`trend-agent forecast demo` trains on the synthetic corpus in memory and runs 13
deterministic scenarios. All are fictional and labelled synthetic. All 13 pass at corpus
scales 0.3 (also run as unit tests) and 0.5 (`trend-agent forecast demo --scale 0.5`, exit 0; recorded in `docs/benchmarks/phase6_scenarios.json`):

| | scenario | verified behaviour |
|---|---|---|
| A | explosive AI model launch | strong breakout signal or already elevated; uncertainty reported; no search counts claimed |
| B | gradual AI tool adoption | growth persistence recognised; the trajectory does not forecast a collapse; no false immediate peak |
| C | limited-time gaming event | windows estimated; a deep dive is no more likely to be on time than a quick reaction; uncertainty grows with the horizon |
| D | evergreen tutorial (weekly seasonality) | seasonal baseline competitive; no unjustified breakout; daily series: no 6 h forecast |
| E | false viral signal (video rises, search does not) | proxy forecast labelled `proxy_attention`; no YouTube Search forecast from video activity; direct series shows no breakout |
| F | sparse new query | INSUFFICIENT_HISTORY at every horizon; no probability |
| G | provider delay (Monday published Wednesday) | Tuesday's forecast cannot see Monday's value; features identical with or without it |
| H | normalization change / incompatible series | forecast refused (FORECAST_UNAVAILABLE); cross-series ratio rejected |
| I | overfitting | LightGBM fits training data, fails held-out; promotion rejected; baseline retained |
| J | distribution shift (new gaming event type) | OUT_OF_DISTRIBUTION; probability withheld |
| K | peak beyond the horizon | no confirmed peak inside the window; censoring stated |
| L | too few labelled breakouts | INSUFFICIENT_CALIBRATION_DATA; uncalibrated models never validated |
| M | mixed hourly and daily providers | 6 h only for hourly data; daily trajectories at whole days only |

## I. Peak timing and opportunity-window functionality

Peaks are ranges (10–90 % of comparable states' peak offsets), never timestamps, with
`PEAK_LIKELY_BEYOND_HORIZON` for censored outcomes, `PEAK_MAY_HAVE_PASSED` and
`PEAK_NOT_ESTIMABLE`. On the synthetic hourly test segment: median timing error 20 h in a
72 h window, interval coverage 0.79, 7.7 % censored. Daily peaks (3 periods) are mostly
censored or not estimable; their apparent precision is a lattice artefact.

Windows use explicit production profiles ([opportunity_windows.md](opportunity_windows.md)).
The probability is the share of ≥ 25 comparable historical states still above their
threshold at the nearest observable lead, with a Wilson interval. Unobservable leads (2 h
on daily data) are UNKNOWN. Interest that is not yet elevated is UNKNOWN, not "too late".
Windows estimate demand persistence only. They never predict views or promise results.

## J. Model registry and deployment safeguards

* Artifacts are JSON parameters and LightGBM text, never pickle. SHA-256 is verified on
  every load; unexpected files (a `.pkl`), path traversal and tampered files are refused
  (tested). A tampered champion makes its forecasts FORECAST_UNAVAILABLE; it is never
  silently replaced.
* Only VALIDATED/CHALLENGER models can be promoted, only by `--promote` or
  `forecast promote`. Training never promotes on its own. There is one champion per
  scope (database-enforced), the previous one is retired, and every change is an event
  with evidence. `forecast rollback` returns a scope to baselines.
* Champion–challenger uses the same chronological test rows, a Brier improvement with a
  bootstrap interval, precision@K, the false-positive rate, ECE and equal data origin.
  ROC-AUC alone never decides.
* Synthetic models serve synthetic series only (tested). A real training run refuses
  synthetic rows. A model built on another feature schema is never used.
* Drift: PSI data drift and matured-label concept drift, with `retrain` /
  `revert_to_baseline` recommendations that are never executed automatically.

## K. Tests, linting, typing and benchmark results

* Full suite (Phases 1–6, unit + integration against isolated PostgreSQL test databases,
  no network): **730 passed**, 3 live tests deselected by default, on Python 3.12.3
  (12 min, with coverage: **91 %** of 27,633 statements) and Python 3.13 (7.7 min).
  Executed in this environment; `TREND_AGENT_REQUIRE_DB_TESTS=1`, so nothing was skipped
  for lack of a database. After that run, one integration test was strengthened (it now
  always registers a challenger) and re-run on its own: passed.
* Phase 6 tests: 87 unit tests (`tests/unit/forecasting`, offline, fixed seeds) and
  7 integration tests (`tests/integration/test_forecasting.py`, isolated test database),
  covering all 32 required areas: dataset construction, point-in-time joins, availability
  and revision handling, leakage, missing data, irregular sampling, comparability,
  resolution eligibility, baselines, logistic and LightGBM training (determinism, text
  round trip), hyperparameter validation, class imbalance, independent targets, splits
  with embargo and unseen groups, calibration, uncertainty and OOD, trajectory, peaks and
  censoring, windows, registry, promotion, champion–challenger, drift, abstention,
  idempotency, CLI, Phase 4/5 integration, classification preservation, and regression
  of Phases 1–5 (full suite).
* `ruff check`, `ruff format --check`, `mypy src` (207 files) and `mypy tests`: clean.
* Benchmarks (Intel Xeon 2.10 GHz, 4 CPUs, 16 GB RAM, Python 3.12, PostgreSQL 16 in Docker, `docs/benchmarks/phase6_benchmark.json`, measured, synthetic):

| measurement | result |
|---|---|
| feature generation, one prediction time (hourly series, 1,344 points) | fast path (shared causal detector pass, dataset building) p50 1.6 ms / p95 2.4 ms; strict path (detector re-run from data visible at T; inference and audits) p50 291 ms / p95 380 ms |
| dataset construction, 180 series / 175,680 observations → 36,013 rows × 52 features | 90.7 s with 1 process (397 rows/s); 28.5 s with 4 processes (1,263 rows/s); leakage audit 0 mismatches |
| training, all 18 scopes of that dataset (32 models registered) | 44.7 s; explicit promotion < 0.1 s |
| full-corpus evaluation (360 series, 72,098 rows) | dataset 65 s (4 processes) + training/promotion 59 s |
| single forecast, one hourly series (720 points), all 3 horizons | p50 159 ms / p95 174 ms |
| batch inference in memory, 1,000 hourly series × 3 horizons | 131.5 s (7.6 series/s, single process) |
| causal replay (Phase 3 engine), 18 series × 61 steps, 3,294 forecasts | 130.4 s; p50 101 ms / p95 233 ms per series-step |
| database: 1,000 active (120 daily observations each) + 10,000 dormant targets | read all forecastable series: 7.0 s (1,000 series, 120,000 observations) |
| database: batch predict and persist (3,000 forecasts with trajectories, peaks, windows) | 119.4 s (25.1 forecasts/s, one transaction per forecast) |
| database: idempotent re-run (nothing new stored) | 87.4 s, which puts the database writes at about 32 s of the 119 s |
| database: explanation lookup by prediction id | p50 1.6 ms / p95 2.0 ms |
| database: drift report (6 champions) | 0.13 s |
| database: training run persisted to the registry (corpus scale 0.1) | 12.4 s, 29 models registered |

CPU only, no GPU. The strict causal detector replay dominates inference: about 0.2 ms per
visible observation, so hourly issuance for 1,000 series costs about 2 minutes of one
core. Reusing the detector state that Phase 4 already persists would remove the replay;
it was not needed at this scale. In the database benchmark most forecasts are
LIMITED_CONFIDENCE (1,849 of 2,000 eligible): the benchmark series come from Phase 4's
generator, not the training corpus, and the OOD check flags the mismatch as designed.

## L. Working live data sources versus mock providers

| source | status in Phase 6 |
|---|---|
| YouTube Data API | not called (no key; fake transport in tests). Phase 6 reads only stored series |
| Google Trends (official alpha) | not configured: no real search-interest history exists |
| Monitored series (Phase 3) | read from PostgreSQL; only synthetic demo/benchmark series exist here |
| Detector state (Phase 4), content analyses (Phase 5) | consumed from the database (integration-tested with synthetic rows) |
| Training data | synthetic corpus only (`--dataset synthetic`); `--dataset database` refuses synthetic rows and reports "no eligible series" |

Everything verified here is simulated. Production readiness requires real monitored
history, matured labels and confirmation of champions on several later held-out periods.

## M. Known limitations and residual risks

* **No real-world validation.** All accuracy, calibration and coverage figures are
  synthetic. The synthetic generator encodes assumptions (event mix, adoption length,
  noise) that a model can learn and that real data may not share.
* **Small-sample validation is fragile**, as the daily 24 h champion's held-out failure
  shows. Require several held-out periods and watch `forecast drift` before trusting a
  real champion.
* **Bootstrap intervals are approximate**: series are resampled as blocks, but they are
  not independent either.
* **Conformal coverage is nominal.** Serial dependence and drift can reduce real coverage.
* **Baseline absorption**: a long plateau raises the trailing baseline, so "breakout"
  always means relative to the recent past.
* **Peaks and windows are experimental** (analog model). Daily peak timing is very coarse.
* **Continuation** mostly abstains (few elevated states).
* **Gaming** breakouts were not predictable in the synthetic corpus.
* **Pooled models only.** Each scope pools AI, technology and gaming with category
  indicator features, and performance is reported per category slice. Category-specific
  models were not trained: the per-category positive counts (e.g. 43 gaming positives in
  the hourly 24 h test segment) are too small to compare specialised models reliably.
  The registry already supports category-scoped champions (category first, then pooled).
* **Inference cost**: the strict causal detector replay dominates per-series latency
  (about 160 ms per hourly series; 25 forecasts/s in the database batch, with persistence). Batching or caching detector state is the scaling
  lever if needed.
* **Same-hour identity**: requests within one hour return the first forecast of that
  hour by design.
* Revisions after a forecast change its label; historical accuracy uses final values.

## N. Exact commands needed to run the forecasting engine

```bash
docker compose up -d && trend-agent db upgrade          # PostgreSQL + migration 0007
trend-agent forecast demo                               # offline, synthetic scenarios A-M
trend-agent forecast replay --dataset synthetic         # offline causal walk-forward
trend-agent forecast schema > forecast_schema.json      # Phase 7 contract
# with monitored data (Phases 3/4 running):
trend-agent forecast eligibility
trend-agent forecast train                              # real data only; never promotes
trend-agent forecast models
trend-agent forecast promote MODEL_ID                   # or: forecast train --promote
trend-agent forecast predict [--category ai]            # idempotent, stored
trend-agent forecast explain --prediction-id ID
trend-agent forecast peaks && trend-agent forecast windows
trend-agent forecast evaluate --horizon 24h && trend-agent forecast compare
trend-agent forecast drift
# isolated synthetic experiment (synthetic series only):
trend-agent forecast train --dataset synthetic --scale 0.5 --promote
trend-agent forecast predict --include-synthetic
# evaluation and benchmarks:
python scripts/evaluate_forecasting.py --scale 1.0 --workers 4
TREND_AGENT_BENCH_DATABASE_URL=postgresql+psycopg://…/trend_agent_test \
  python scripts/benchmark_forecasting.py --json-out bench.json
```

## O. Phase 7 readiness assessment

**Ready to consume (interface):** Phase 7 can read typed `ForecastOutput` objects
(`forecast/1.0`, JSON schema via `forecast schema`) from `forecast_predictions.output` or
`ForecastingService.predict`. Statuses, calibration status, proxy and synthetic flags,
data cutoff, intervals, peak ranges, windows with their production assumptions, and
deterministic explanations are all explicit.

**Not ready (evidence):** there are no real-data champions. Until real monitored history
has matured labels and a real training run validates and promotes models, every real
series gets BASELINE_ONLY or abstaining forecasts, and Phase 7 must treat forecasts as
advisory. Phase 7 must check `status`, `synthetic` and `is_proxy` before using any number,
and must never present a window as a promise of views.

## Acceptance criteria

| # | criterion | status |
|---|---|---|
| 1 | Phase 1–5 functionality preserved | met: the full Phase 1–5 suites pass unchanged; Phase 6 only adds tables and read paths (retention extended) |
| 2 | datasets without future-data leakage | met: point-in-time views, maturity rules, leakage audit 0 mismatches, perturbation tests |
| 3 | publication and availability times respected | met: Phase 3 availability rule; delayed-publication and revision tests; scenario G |
| 4 | targets independently defined | met: labels from observed values only, never from detector alerts |
| 5 | measurement types separated | met: target families never pooled; proxy labelled; classification preserved (integration test) |
| 6 | stable, comparable features | met: versioned schema, comparability checks, scale-dependent features disabled where invalid |
| 7 | baseline models | met: climatology, persistence, seasonal naive, damped trend, logistic regression |
| 8 | LightGBM breakout classification | met: conservative grid, early stopping, calibrated, selected only when significantly better |
| 9 | chronological validation | met: embargoed splits, unseen topics, rolling origins; no test-period selection |
| 10 | only supported temporal resolutions | met: UNSUPPORTED_GRANULARITY (no 6 h for daily), no interpolation |
| 11 | calibration where data supports it | met: isotonic/Platt; INSUFFICIENT_CALIBRATION_DATA otherwise |
| 12 | uncertainty and OOD statuses | met: conformal intervals with observed coverage; LIMITED_CONFIDENCE / OUT_OF_DISTRIBUTION |
| 13 | peaks handle insufficient data and censoring | met: PEAK_NOT_ESTIMABLE, PEAK_LIKELY_BEYOND_HORIZON, ranges only |
| 14 | windows use explicit production assumptions | met: configured profiles echoed in every window |
| 15 | model registry and versioning | met: metadata, checksums, events, versions of data/features/targets/code/config |
| 16 | champion–challenger evaluation | met: same rows, bootstrap, precision@K, FPR, ECE |
| 17 | invalid models not automatically promoted | met: explicit promotion only; VALIDATED/CHALLENGER only; scenario I |
| 18 | explicit abstention | met: eight typed statuses with reasons |
| 19 | evidence-backed explanations | met: deterministic text from model contributions, metrics and calibration; no LLM |
| 20 | idempotent database writes | met: UUIDv5 ids; re-run stores nothing (tested) |
| 21 | CLI commands | met: all 17 `forecast` commands exercised in tests |
| 22 | synthetic end-to-end tests | met: scenarios A–M pass (unit tests and demo) |
| 23 | regression tests | met: 730 passed on Python 3.12 and 3.13 |
| 24 | compliance and retention enforced | met: forecasts expire with sources (purge tested); read-only data access; synthetic isolation |
| 25 | performance documented | met: measured benchmarks in `docs/benchmarks/phase6_benchmark.json` |
| 26 | no real-world accuracy claimed | met: every accuracy figure is labelled synthetic |
| 27 | Phase 7 can consume typed outputs | met: `ForecastOutput` contract with JSON schema |
