# Forecast validation (Phase 8)

Command: `trend-agent evaluate forecasting [--scale 0.1–1.0]`. Code:
`src/trend_agent/evaluation/forecasting.py`, which wraps the Phase 6 pipeline. Report:
`docs/benchmarks/phase8_evaluation.json` (`results.forecasting`), measured at scale 0.4.

## 1. Protocol

The Phase 6 pipeline is unchanged:

* the dataset is built with a leakage audit;
* each scope gets a chronological train / validation / calibration / test split;
* the validation checks are explicit;
* promotion is an explicit step.

Phase 8 summarises each scope (family / granularity / horizon / task):

* **classification** (breakout, continuation): Brier skill against climatology (the
  reference, skill 0). LightGBM must beat logistic regression by ≥ 0.02 Brier skill to
  count as meaningful.
* **trajectory**: MAE against the best of persistence, seasonal naive and damped trend.
  LightGBM must cut MAE by ≥ 5 % to count as meaningful.
* **unsupported** scopes are listed and never forecast. In particular, **6-hour horizons
  from daily data** are refused: there is no higher-frequency measurement source.

## 2. Results (synthetic corpus, scale 0.4, chronological test period)

| Scope | Selected | MAE | Best baseline | Baseline MAE | Gain | 80 % interval coverage |
|---|---|---|---|---|---|---|
| day / 24 h / trajectory | LightGBM | 0.0930 | persistence | 0.1118 | −16.8 % | 0.80 |
| day / 72 h / trajectory | LightGBM | 0.1067 | seasonal naive | 0.1261 | −15.4 % | 0.77 |
| hour / 6 h / trajectory | LightGBM | 0.0827 | seasonal naive | 0.1156 | −28.5 % | 0.82 |
| hour / 24 h / trajectory | LightGBM | 0.0979 | persistence | 0.1183 | −17.2 % | 0.82 |
| hour / 72 h / trajectory | LightGBM | 0.1283 | persistence | 0.1498 | −14.4 % | 0.83 |
| day / 6 h / all | — | — | — | — | UNSUPPORTED (daily data) | — |
| breakout and continuation scopes (day and hour, 24 h and 72 h) | no validated model at this scale | — | climatology | — | baseline-only forecasts | — |
| peak scopes | no model at this scale | — | — | — | — | — |

* **Trajectory:** 5 of 18 scopes have a selected model, and all 5 beat the best simple
  baseline by more than the meaningful-gain threshold.
* **Breakout:** the Phase 6 run at scale 1.0 (`phase6_evaluation.json`) did select logistic
  regression or LightGBM for breakout. At scale 0.4 the smaller corpus fails the acceptance
  checks, so those scopes fall back to baselines. This is the intended behaviour: no model
  is preferable to an unvalidated one.
* **Held-out champions (Phase 6, scale 1.0, a different seed):** the daily 24 h breakout
  champion had **negative Brier skill (−0.066)** on the held-out corpus. Its
  discrimination was weak but present (ROC AUC 0.74), and its calibration did not
  transfer. This is reported, not hidden. It is the reason breakout probabilities are
  shown as experimental in the dashboard.

## 3. Real-world validation

**UNKNOWN.** There are 0 real forecast outcomes, against a minimum of 100.

* **To measure:** store real observations for at least the longest horizon after each
  forecast, import the outcomes as `forecast_outcome` labels, and compare the predicted
  trajectory with the realised one.
* **Proxy data only:** in this deployment every real series would be a proxy series. A
  forecast of proxy attention is labelled as such and is never presented as search demand.

## 4. Model maintenance

See `model_maintenance.md`: drift checks, REJECTED status, rollback with restoration of the
previous champion, and no automatic promotion of retrained models.
