# Model training, selection and promotion (Phase 6)

Training is a scheduled, heavy operation (`trend-agent forecast train`). Inference is
frequent and light (`trend-agent forecast predict`): it only loads registered champions
and never retrains. Nothing is retrained for every new observation, and nothing is
retrained or promoted automatically.

## 1. Dataset construction

`dataset_builder.build_dataset` produces one example per series and prediction time T.
The T grid runs every 6 h for hourly series and every 24 h for daily series, on the
hour. For each example:

1. Point-in-time view of the target series at T (availability and revision rules,
   [forecast_features.md](forecast_features.md)); ineligible times are skipped and
   counted in `skipped_prediction_times`.
2. Reference state and labels from final values ([forecast_targets.md](forecast_targets.md));
   examples whose windows are all unobserved or immature are skipped.
3. The feature vector, computed with the same code used at inference.

The **manifest** records the dataset version (a hash of configuration, feature schema,
target definition, code hash and per-series source digests), a hash of all rows, label
counts, the period, the synthetic flags and the leakage audit. Rebuilding from the same
data gives the same `rows_hash`, and parallel builds (`--workers`) give identical results.
Worker processes start with `forkserver`/`spawn`, never `fork`.

A real-data (`data_origin = real`) training run refuses any synthetic row. Synthetic data
is used only through `--dataset synthetic`, which builds the isolated experimental corpus
(`config/forecasting.yaml` → `corpus`: 240 hourly series × 56 days and 120 daily
series × 240 days, AI/technology/gaming mix) and produces `data_origin = synthetic` models.

## 2. Scopes and chronological splits

Each scope is (target family × granularity × horizon × task). Families are never pooled.
Within a scope, categories are pooled and the category is a feature context. A daily scope
never gets a 6-hour model.

Splits are by **prediction time**, never random across rows of one series:

    | train ............ | embargo | calibration | embargo | test (untouched) |
            | inner validation (last 25 % of train) |  (embargoed)

* train 60 %, calibration 20 %, test the remaining ~20 % of the scope's time span;
* embargo = horizon + 48 h (the longest publication lag), so no label window of one
  segment overlaps the next;
* 20 % of topic groups are **unseen**: excluded from training and calibration and present
  only in the test segment (cold-start generalisation);
* 3 expanding-window rolling origins (logistic regression refit per fold) report stability
  over time.

The test segment is used once, for the final numbers. It never chooses a
hyperparameter, threshold, calibrator or model.

## 3. Candidates

### Breakout and continuation (classification)

| candidate | details |
|---|---|
| climatology | the training base rate: the reference every model must beat |
| logistic regression | standardised features plus explicit missingness indicators (median fill), `class_weight="balanced"`, C ∈ {0.05, 0.3, 1.0} chosen by validation log loss |
| LightGBM | small conservative grid (num_leaves 7/15/31 with min_data_in_leaf 60/40/20; learning rate 0.05, max_depth 4, feature/bagging fraction 0.8, λ₂ = 2), ≤ 400 rounds with early stopping (30) on the validation segment, configuration chosen by validation PR-AUC, `scale_pos_weight` = √(neg/pos) capped at 10, deterministic, 2 threads |

At least 300 training rows and 20 positives are required, else the scope stays
baseline-only. Scores are then calibrated on the calibration segment
([forecast_uncertainty.md](forecast_uncertainty.md)) and the decision threshold is the
F1-optimal threshold on the calibration segment.

### Trajectory (regression of the log change)

Persistence (no change), seasonal naive (same hour yesterday / same day last week,
visible at T), damped trend (φ = 0.97 per hour / 0.8 per day, change capped at log 3)
and a LightGBM regressor (Huber loss, same grid, early stopping on validation MAE). Each
gets a split-conformal 80 % interval from calibration residuals.

### Peak timing and persistence (experimental)

One analog model per family × granularity: up to 6,000 training states with their peak
offsets, censoring flags and elevated-at-lead outcomes. It is evaluated on the test
segment (identified peaks, median absolute timing error, interval coverage, censoring
rate). It is VALIDATED for use only if at least 30 identified test peaks exist, and it
always carries the status reason "experimental capability".

## 4. Acceptance criteria (VALIDATED vs EXPERIMENTAL)

A classifier is VALIDATED only if **all** hold on the untouched test segment:

| check | rule |
|---|---|
| enough_test_examples / positives | ≥ 200 examples and ≥ 20 positives |
| calibrated | the calibrator status is CALIBRATED |
| ece_within_limit | ECE ≤ 0.08 |
| beats_climatology | series-block bootstrap (400 resamples, 90 %) of Brier(model) − Brier(climatology) has upper bound < 0 **and** Brier skill ≥ 0.02 |
| beats_logistic_brier (LightGBM) | the same bootstrap against logistic regression has upper bound < 0 |
| precision_at_k_not_worse (LightGBM) | precision@50 ≥ logistic precision@50 − 0.05 |
| not_overfitted | train-minus-test PR-AUC ≤ 0.25, or the model still beats its reference on held-out data (the gap is reported either way) |

A trajectory model is VALIDATED if it has ≥ 200 test examples, its observed interval
coverage is within ±0.10 of nominal, and (for anything other than the reference baseline)
its MAE beats the reference baseline with a bootstrap interval excluding zero. The
reference baseline is the baseline with the lowest calibration-period MAE.

The bootstrap resamples whole series, which respects within-series dependence. Because
series are not fully independent either, the intervals are approximations. ROC-AUC alone
is never sufficient.

**Selection** follows a fixed rule: LightGBM if it validated (which requires beating
logistic regression), otherwise logistic regression if it validated, otherwise the
scope stays baseline-only. For trajectories: LightGBM if it validated, otherwise the
reference baseline.

## 5. Registry, promotion and champion–challenger

Every candidate except climatology is registered (`forecast_models`) with its full
metadata: target definition, horizon, granularity, category scope, data origin, feature
schema and names, dataset version and hash, training/calibration/test periods,
hyperparameters, metrics, calibration summary, code version, artifact checksum and status.

* Artifacts are JSON parameters plus LightGBM's own text format, stored under
  `TREND_AGENT_FORECAST_ARTIFACT_DIR` (default `var/forecast_models`). Loading verifies
  the SHA-256 checksum, refuses unexpected files (e.g. a `.pkl`) and path traversal, and
  never unpickles anything. A champion whose artifact fails verification is refused at
  inference (FORECAST_UNAVAILABLE), never silently replaced.
* **Promotion is explicit**: `forecast train --promote` or `forecast promote <model>`.
  Only VALIDATED or CHALLENGER models can become CHAMPION. There is at most one champion
  per scope (a partial unique index in the database), and the previous champion is
  RETIRED in the same operation. Every change is a `forecast_model_events` row with its
  evidence. `forecast rollback <model>` retires a champion; the scope then falls back to
  baselines.
* **Champion–challenger** (`forecast compare`, and automatically at `--promote` when a
  champion exists): both models are scored on the same chronological test rows. The
  challenger is promoted only if it is validated, its Brier score is better with a
  bootstrap interval excluding zero, precision@50 and the false-positive rate are not
  worse, its ECE is within the limit and its data origin matches. Otherwise it is marked
  CHALLENGER with the comparison as evidence. Lead time (hours between the first alert and
  the end of a breakout's run-up), inference time and forecast stability (mean absolute
  change of consecutive probabilities) are reported alongside.
* Synthetic models are selected only for synthetic series. A synthetic model can be
  CHAMPION *of the synthetic scope*, which is never a production-validated real-world
  model.

## 6. Drift monitoring

`forecast drift` reports, per champion:

* **data drift**: PSI of each feature against the training deciles stored with the model
  (warn ≥ 0.1, alert ≥ 0.25), and missing-rate changes; at least 50 recent forecasts are
  needed;
* **concept drift**: once labels have matured (horizon + 48 h after the issue time),
  matured Brier score, base rate and ECE against the model's held-out values; at least
  100 matured labels are needed.

Recommendations are conservative and never automatic: `revert_to_baseline` if matured
predictions are worse than climatology, `retrain` if matured performance degraded by
> 0.03 Brier, the base rate shifted by > 50 %, or ≥ 3 important features drifted
strongly. Acting on a recommendation is an explicit, logged command.
